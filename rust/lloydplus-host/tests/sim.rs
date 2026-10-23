//! The Rust driver against the Python simulator (`python -m
//! lloydplus.simserver`), the same machine model the Python tests use and the
//! one that was corrected against the LF Plus. Needs `uv` and the repository's
//! Python environment.

use std::{
    io::{BufRead, BufReader},
    path::PathBuf,
    process::{Child, Command, Stdio},
    time::Instant,
};

use embassy_futures::select::{Either, select};
use embassy_time::{Duration, Timer, with_timeout};
use lloydplus::{
    ConnectOptions, Drive, LloydPlus, Protection, SegmentEnd,
    stages::{SLOT_READY, SLOT_WAIT_EXCLUSIVE, SLOT_WAIT_SHARED},
};
use lloydplus_host::{block_on, now_utc, open_tcp};

struct SimServer {
    child: Child,
    port: u16,
}

impl SimServer {
    fn start(args: &[&str]) -> Self {
        let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
        let mut child = Command::new("uv")
            .args(["run", "--quiet", "python", "-m", "lloydplus.simserver"])
            .args(args)
            .current_dir(&root)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .expect("start the simulator server with uv");
        let stdout = child.stdout.take().unwrap();
        let mut line = String::new();
        BufReader::new(stdout).read_line(&mut line).unwrap();
        let port: u16 = line
            .trim()
            .strip_prefix("PORT ")
            .and_then(|p| p.parse().ok())
            .unwrap_or_else(|| panic!("unexpected simserver banner {line:?}"));
        Self { child, port }
    }
}

impl Drop for SimServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

/// Start a simulator, connect a driver to it, run `body`, close.
fn with_machine<F, Fut>(sim_args: &[&str], opts: ConnectOptions, body: F)
where
    F: FnOnce(&'static LloydPlus) -> Fut,
    Fut: core::future::Future<Output = ()>,
{
    let _ = env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info"))
        .is_test(true)
        .try_init();
    let sim = SimServer::start(sim_args);
    let (rx, tx) = open_tcp(("127.0.0.1", sim.port)).expect("connect to simserver");
    let m = LloydPlus::new(Some(Duration::from_secs(2)), 1);
    block_on(async {
        match select(m.run(rx, tx), async {
            with_timeout(Duration::from_secs(60), async {
                m.connect(opts).await.expect("connect");
                body(m).await;
                m.close().await;
            })
            .await
            .expect("test timed out");
        })
        .await
        {
            Either::First(()) => unreachable!(),
            Either::Second(()) => {}
        }
    });
    drop(sim);
}

fn opts() -> ConnectOptions {
    ConnectOptions {
        clock: Some(now_utc()),
        ..Default::default()
    }
}

async fn settle() {
    Timer::after_millis(300).await;
}

#[test]
fn connect_queries_information_and_streams() {
    with_machine(&[], opts(), |m| async move {
        assert_eq!(m.version(), Some((2, 1, 7)));
        let d = m.description().unwrap();
        assert_eq!(d[2], "Lloyd Instruments LF Plus");
        let p = m.parameters().unwrap();
        assert_eq!(p.load_range, 1000.0);
        assert_eq!(p.extension_range, 1100.0);
        assert!((p.max_speed - 33.8667).abs() < 1e-6);
        let s = m.sources();
        assert_eq!(s.len(), 2);
        assert_eq!((s[0].kind, s[1].kind), (1, 2));
        assert_eq!(s[1].name, "Extension");
        // readings flow on both sources
        let mut seen = [false, false];
        let t0 = Instant::now();
        while !(seen[0] && seen[1]) && t0.elapsed().as_secs() < 3 {
            if let Ok(s) = with_timeout(Duration::from_millis(500), m.next_sample()).await {
                if (s.source as usize) < 2 {
                    seen[s.source as usize] = true;
                }
            }
        }
        assert!(seen[0] && seen[1], "samples from both sources");
        assert_eq!(m.load(), Some(0.0));
        assert_eq!(m.extension(), Some(0.0));
        let c = m.control().expect("stage control");
        assert_eq!(c.slot(), SLOT_WAIT_SHARED);
        assert!(!c.in_control());
        assert!(c.has(lloydplus::stages::LOAD_RATE));
        assert!(!m.is_lost());
    });
}

#[test]
fn take_control_drive_and_release() {
    with_machine(&[], opts(), |m| async move {
        let c = m.control().unwrap();
        c.take_control(Protection::limits(100.0, 100.0), Duration::from_secs(5))
            .await
            .expect("take control");
        assert_eq!(c.slot(), SLOT_READY);
        assert!(c.ready() && c.in_control());

        let seg = c
            .drive(
                Drive::to_extension(5.0, 10.0),
                None,
                Some(Duration::from_secs(5)),
            )
            .await
            .expect("drive");
        assert_eq!(seg.end(), Some(SegmentEnd::LimitReached));
        assert!(seg.started());
        settle().await;
        let x = m.extension().unwrap();
        assert!((x - 5.0).abs() < 0.15, "extension {x}");
        let f = m.load().unwrap();
        assert!((f - 50.0).abs() < 1.5, "load {f} (10 N/mm sample)");
        assert_eq!(c.slot(), seg.base + 3);

        // a second segment uses the other bank
        let seg2 = c
            .drive(
                Drive::to_extension(0.0, 10.0),
                None,
                Some(Duration::from_secs(5)),
            )
            .await
            .unwrap();
        assert_ne!(seg2.base, seg.base);
        assert_eq!(seg2.end(), Some(SegmentEnd::LimitReached));

        c.stop(Some(Duration::from_secs(2))).await.expect("stop");
        assert_eq!(c.slot(), SLOT_READY);
        c.release_control(Duration::from_secs(5))
            .await
            .expect("release");
        assert!(c.slot() == SLOT_WAIT_EXCLUSIVE || c.slot() == SLOT_WAIT_SHARED);
        assert!(!c.in_control());
    });
}

#[test]
fn stop_halts_a_running_drive() {
    with_machine(&[], opts(), |m| async move {
        let c = m.control().unwrap();
        c.take_control(Protection::limits(100.0, 100.0), Duration::from_secs(5))
            .await
            .unwrap();
        let seg = c.start(Drive::to_extension(20.0, 5.0), None).await.unwrap();
        Timer::after_millis(600).await;
        assert!(seg.started() && !seg.finished());
        c.stop(Some(Duration::from_secs(2))).await.expect("stop");
        assert_eq!(seg.end(), Some(SegmentEnd::Stopped));
        assert_eq!(c.slot(), SLOT_READY);
        settle().await;
        let x = m.extension().unwrap();
        assert!(x > 1.0 && x < 6.0, "stopped mid-way at {x} mm");
        let before = x;
        settle().await;
        assert_eq!(m.extension().unwrap(), before, "head stays put after stop");
        // back home for the release in close()
        c.drive(
            Drive::to_extension(0.0, 10.0),
            None,
            Some(Duration::from_secs(5)),
        )
        .await
        .unwrap();
    });
}

#[test]
fn hold_then_next_drive() {
    with_machine(&[], opts(), |m| async move {
        let c = m.control().unwrap();
        c.take_control(Protection::limits(100.0, 100.0), Duration::from_secs(5))
            .await
            .unwrap();
        let t0 = Instant::now();
        let seg = c
            .drive(
                Drive::to_extension(2.0, 10.0).with_hold(Duration::from_millis(700)),
                Some(Drive::to_extension(0.0, 10.0)),
                Some(Duration::from_secs(10)),
            )
            .await
            .expect("hold + next");
        let dt = t0.elapsed().as_secs_f64();
        assert_eq!(seg.end(), Some(SegmentEnd::LimitReached));
        assert!(seg.holding(), "entered the hold slot");
        assert!(seg.next_started(), "entered the next drive");
        // 0.7 s hold plus two 0.2 s moves, less the simulator's 10 ms tick slop
        assert!(dt >= 0.7 + 0.3, "hold time was honoured ({dt:.2} s)");
        settle().await;
        assert!(m.extension().unwrap().abs() < 0.15);
    });
}

#[test]
fn pre_empting_supersedes_the_running_segment() {
    with_machine(&[], opts(), |m| async move {
        let c = m.control().unwrap();
        c.take_control(Protection::limits(100.0, 100.0), Duration::from_secs(5))
            .await
            .unwrap();
        let first = c.start(Drive::to_extension(30.0, 5.0), None).await.unwrap();
        Timer::after_millis(400).await;
        let second = c
            .drive(
                Drive::to_extension(0.0, 10.0),
                None,
                Some(Duration::from_secs(10)),
            )
            .await
            .unwrap();
        assert_eq!(first.end(), Some(SegmentEnd::Superseded));
        assert_eq!(second.end(), Some(SegmentEnd::LimitReached));
    });
}

#[test]
fn grip_protection_trip_ends_segment_and_recovers() {
    with_machine(&[], opts(), |m| async move {
        let c = m.control().unwrap();
        c.take_control(Protection::limits(100.0, 3.0), Duration::from_secs(5))
            .await
            .unwrap();
        // compression: -5 mm would be -50 N; the sim trips 10 N past the 3 N limit
        let seg = c
            .drive(
                Drive::to_extension(-5.0, 10.0),
                None,
                Some(Duration::from_secs(10)),
            )
            .await
            .expect("segment ends on the trip");
        assert_eq!(seg.end(), Some(SegmentEnd::LoadSafetyLimit));
        assert_eq!(c.safety_trips(), 1);
        let msg = with_timeout(Duration::from_secs(2), m.next_message())
            .await
            .expect("warning delivered");
        assert_eq!(msg.code, 10);
        assert_eq!(msg.description(), "Load has exceeded safety limit.");
        settle().await;
        let f = m.load().unwrap();
        assert!(
            f < -12.0 && f > -20.0,
            "halted just past the limit at {f} N"
        );
        assert_eq!(
            c.slot(),
            SLOT_READY,
            "stop after the trip took the machine to ready"
        );
        // recovery: a new drive starts at once
        let back = c
            .drive(
                Drive::to_extension(0.0, 10.0),
                None,
                Some(Duration::from_secs(10)),
            )
            .await
            .expect("retreat");
        assert_eq!(back.end(), Some(SegmentEnd::LimitReached));
    });
}

#[test]
fn load_limited_drive_and_load_hold() {
    with_machine(&[], opts(), |m| async move {
        let c = m.control().unwrap();
        c.take_control(Protection::limits(100.0, 100.0), Duration::from_secs(5))
            .await
            .unwrap();
        let seg = c
            .drive(
                Drive::to_load(-20.0, 10.0).with_hold(Duration::from_millis(400)),
                None,
                Some(Duration::from_secs(10)),
            )
            .await
            .expect("to -20 N");
        assert_eq!(seg.end(), Some(SegmentEnd::LimitReached));
        assert!(seg.holding());
        settle().await;
        let f = m.load().unwrap();
        assert!((f + 20.0).abs() < 1.5, "load {f}");
        let x = m.extension().unwrap();
        assert!(
            (x + 2.0).abs() < 0.2,
            "extension {x} (compression is negative)"
        );
        c.drive(
            Drive::to_extension(0.0, 10.0),
            None,
            Some(Duration::from_secs(10)),
        )
        .await
        .unwrap();
    });
}

#[test]
fn zero_and_return_to_zero() {
    with_machine(&[], opts(), |m| async move {
        let c = m.control().unwrap();
        c.take_control(Protection::limits(100.0, 100.0), Duration::from_secs(5))
            .await
            .unwrap();
        c.drive(
            Drive::to_extension(3.0, 10.0),
            None,
            Some(Duration::from_secs(5)),
        )
        .await
        .unwrap();
        c.zero(Duration::from_secs(5)).await.expect("zero");
        settle().await;
        assert!(m.extension().unwrap().abs() < 0.05, "extension re-zeroed");
        assert!(m.load().unwrap().abs() < 0.5, "load re-zeroed");
        c.drive(
            Drive::to_extension(2.0, 10.0),
            None,
            Some(Duration::from_secs(5)),
        )
        .await
        .unwrap();
        c.return_to_zero(true, Some(Duration::from_secs(5)))
            .await
            .expect("return");
        assert_eq!(c.slot(), SLOT_READY);
        settle().await;
        assert!(m.extension().unwrap().abs() < 0.05, "back at the new zero");
    });
}

#[test]
fn local_frame_refuses_control() {
    with_machine(&["--local"], opts(), |m| async move {
        let c = m.control().unwrap();
        let r = c
            .take_control(Protection::limits(100.0, 100.0), Duration::from_secs(2))
            .await;
        assert!(r.is_err(), "no control without Remote Control: {r:?}");
        assert!(!c.in_control());
    });
}

#[test]
fn zero_sources_zeros_only_the_named_source() {
    with_machine(&[], opts(), |m| async move {
        let c = m.control().unwrap();
        c.take_control(Protection::limits(100.0, 100.0), Duration::from_secs(5))
            .await
            .unwrap();
        // 3 mm into the 10 N/mm sample: 30 N
        c.drive(Drive::to_extension(3.0, 10.0), None, Some(Duration::from_secs(5)))
            .await
            .unwrap();
        settle().await;
        c.zero_sources(&[1], Duration::from_secs(5)).await.expect("zero extension");
        settle().await;
        assert!(m.extension().unwrap().abs() < 0.05, "extension zeroed");
        assert!((m.load().unwrap() - 30.0).abs() < 1.5, "load untouched: {:?}", m.load());
        c.zero_sources(&[0], Duration::from_secs(5)).await.expect("zero load");
        settle().await;
        assert!(m.load().unwrap().abs() < 0.5, "load zeroed: {:?}", m.load());
        assert!(m.extension().unwrap().abs() < 0.05, "extension still zero");
        c.drive(Drive::to_extension(-3.0, 10.0), None, Some(Duration::from_secs(5)))
            .await
            .unwrap();
    });
}
