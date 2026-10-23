//! Minimal command line for the Rust driver, mirroring the Python `lloydplus`
//! CLI's `info`, `log` and `move` against a serial port or the simulator.
//!
//! ```text
//! lloydplus-host info --port /dev/ttyUSB0
//! lloydplus-host log  --port 127.0.0.1:4321 --seconds 5
//! lloydplus-host move --port /dev/ttyUSB0 --to 10 --speed 50 [--hold 5]
//! lloydplus-host move --port /dev/ttyUSB0 --load -50 --speed 10
//! ```
//! Speeds are mm/min as on the frame's own display. `--no-rtscts` for a lead
//! without pins 7/8.

use std::{process::exit, time::Instant};

use embassy_futures::select::{Either, select};
use embassy_time::{Duration, Timer};
use lloydplus::{ConnectOptions, Drive, LloydPlus, Protection};
use lloydplus_host::{block_on, now_utc, open_target};

struct Args {
    cmd: String,
    port: String,
    rtscts: bool,
    seconds: f64,
    to: Option<f64>,
    load: Option<f64>,
    speed: f64,
    hold: f64,
    tensile: Option<f64>,
    compressive: Option<f64>,
}

fn parse() -> Args {
    let mut a = Args {
        cmd: String::new(),
        port: String::new(),
        rtscts: true,
        seconds: 5.0,
        to: None,
        load: None,
        speed: 10.0,
        hold: 0.0,
        tensile: None,
        compressive: None,
    };
    let mut it = std::env::args().skip(1);
    while let Some(arg) = it.next() {
        let mut val = |name: &str| -> String {
            it.next().unwrap_or_else(|| {
                eprintln!("{name} needs a value");
                exit(2)
            })
        };
        match arg.as_str() {
            "--port" => a.port = val("--port"),
            "--no-rtscts" => a.rtscts = false,
            "--seconds" => a.seconds = val("--seconds").parse().unwrap_or(5.0),
            "--to" => a.to = val("--to").parse().ok(),
            "--load" => a.load = val("--load").parse().ok(),
            "--speed" => a.speed = val("--speed").parse().unwrap_or(10.0),
            "--hold" => a.hold = val("--hold").parse().unwrap_or(0.0),
            "--tensile-limit" => a.tensile = val("--tensile-limit").parse().ok(),
            "--compressive-limit" => a.compressive = val("--compressive-limit").parse().ok(),
            s if !s.starts_with('-') && a.cmd.is_empty() => a.cmd = s.to_string(),
            s => {
                eprintln!("unknown argument {s}");
                exit(2);
            }
        }
    }
    if a.cmd.is_empty() || a.port.is_empty() {
        eprintln!("usage: lloydplus-host (info|log|move) --port DEV|HOST:PORT [options]");
        exit(2);
    }
    a
}

fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();
    let a = parse();
    let (rx, tx) = match open_target(&a.port, a.rtscts) {
        Ok(p) => p,
        Err(e) => {
            eprintln!("cannot open {}: {e}", a.port);
            exit(1);
        }
    };
    let m = LloydPlus::new(Some(Duration::from_secs(2)), 1);
    let code = block_on(async {
        match select(m.run(rx, tx), command(m, &a)).await {
            Either::First(()) => 1,
            Either::Second(code) => code,
        }
    });
    exit(code);
}

async fn command(m: &'static LloydPlus, a: &Args) -> i32 {
    let opts = ConnectOptions {
        control: a.cmd == "move",
        clock: Some(now_utc()),
        ..Default::default()
    };
    if let Err(e) = m.connect(opts).await {
        eprintln!("connect failed: {e}");
        return 1;
    }
    let code = match a.cmd.as_str() {
        "info" => {
            let d = m.description().unwrap_or_default();
            let v = m.version().unwrap_or((0, 0, 0));
            println!("machine:  {} / {} / {}", d[0], d[1], d[2]);
            println!("firmware: {}.{}.{}", v.0, v.1, v.2);
            if let Some(p) = m.parameters() {
                println!(
                    "limits:   load {} N, extension {} mm, speed {}..{} mm/s ({:.3}..{:.1} mm/min)",
                    p.load_range,
                    p.extension_range,
                    p.min_speed,
                    p.max_speed,
                    p.min_speed * 60.0,
                    p.max_speed * 60.0
                );
            }
            for s in m.sources() {
                println!(
                    "source {}: kind {} range {} {:?}",
                    s.index,
                    s.kind,
                    s.range(),
                    s.name
                );
            }
            Timer::after_millis(500).await;
            println!(
                "readings: load {:?} N, extension {:?} mm, link {:?}",
                m.load(),
                m.extension(),
                m.link.stats()
            );
            0
        }
        "log" => {
            println!("t_s,source,value");
            let t0 = Instant::now();
            let mut n = 0u32;
            while t0.elapsed().as_secs_f64() < a.seconds {
                if let Ok(s) =
                    embassy_time::with_timeout(Duration::from_millis(500), m.next_sample()).await
                {
                    println!("{:.4},{},{:.6}", s.t(), s.source, s.value);
                    n += 1;
                }
                if m.is_lost() {
                    eprintln!("connection lost");
                    break;
                }
            }
            eprintln!(
                "{n} samples, {} dropped, {:?}",
                m.sample_drops(),
                m.link.stats()
            );
            0
        }
        "move" => move_cmd(m, a).await,
        other => {
            eprintln!("unknown command {other}");
            2
        }
    };
    m.close().await;
    code
}

async fn move_cmd(m: &'static LloydPlus, a: &Args) -> i32 {
    let Some(c) = m.control() else {
        eprintln!("test control unavailable on this machine");
        return 1;
    };
    let range = m.parameters().map(|p| p.load_range).unwrap_or(1000.0);
    let prot = Protection::limits(
        a.tensile.unwrap_or(range * 0.1),
        a.compressive.unwrap_or(range * 0.1),
    );
    println!(
        "grip protection: tensile {} N, compressive {} N",
        prot.tensile_limit, prot.compressive_limit
    );
    if let Err(e) = c.take_control(prot, Duration::from_secs(10)).await {
        eprintln!("take control: {e}");
        return 1;
    }
    let speed = a.speed / 60.0;
    let drive = match (a.to, a.load) {
        (Some(mm), None) => Drive::to_extension(mm, speed),
        (None, Some(n)) => Drive::to_load(n, speed),
        _ => {
            eprintln!("give exactly one of --to MM or --load N");
            return 2;
        }
    }
    .with_hold(Duration::from_millis((a.hold * 1000.0) as u64));
    println!(
        "start: load {:?} N, extension {:?} mm",
        m.load(),
        m.extension()
    );
    let seg = match c.start(drive, None).await {
        Ok(s) => s,
        Err(e) => {
            eprintln!("start: {e}");
            return 1;
        }
    };
    let t0 = Instant::now();
    while !seg.wait(Some(Duration::from_millis(500))).await {
        println!(
            "{:7.2}s slot {:2}  load {:9.3} N  extension {:10.4} mm",
            t0.elapsed().as_secs_f64(),
            c.slot(),
            m.load().unwrap_or(f64::NAN),
            m.extension().unwrap_or(f64::NAN)
        );
        if m.is_lost() {
            eprintln!("connection lost");
            return 1;
        }
    }
    println!(
        "finished: {:?} after {:.2}s, load {:?} N, extension {:?} mm",
        seg.end(),
        t0.elapsed().as_secs_f64(),
        m.load(),
        m.extension()
    );
    0
}
