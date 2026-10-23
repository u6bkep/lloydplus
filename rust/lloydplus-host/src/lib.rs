//! Host-side plumbing for the `lloydplus` driver: blocking `std::io` ports
//! (a serial port, a TCP socket to the Python simulator) adapted to the
//! `embedded-io-async` reader/writer pair the driver wants, plus small
//! helpers for running it under `futures-lite`.

use std::{
    io::{Read, Write},
    net::{TcpStream, ToSocketAddrs},
    sync::Arc,
    thread,
    time::{Duration as StdDuration, SystemTime, UNIX_EPOCH},
};

use embassy_sync::{blocking_mutex::raw::CriticalSectionRawMutex, pipe::Pipe};
use lloydplus::{BAUDRATE, UtcTime};

const PIPE: usize = 16384;

/// Async reader half: a thread pumps the blocking reader into a pipe.
pub struct PortReader {
    pipe: Arc<Pipe<CriticalSectionRawMutex, PIPE>>,
}

/// Async writer half: writes go straight to the blocking writer (frames are
/// small and the ports are fast, so this never stalls for long).
pub struct PortWriter {
    w: Box<dyn Write + Send>,
}

/// Split a blocking reader/writer pair into the async halves.
pub fn threaded_port<R, W>(mut reader: R, writer: W) -> (PortReader, PortWriter)
where
    R: Read + Send + 'static,
    W: Write + Send + 'static,
{
    let pipe: Arc<Pipe<CriticalSectionRawMutex, PIPE>> = Arc::new(Pipe::new());
    let p2 = pipe.clone();
    thread::Builder::new()
        .name("lloydplus-port-rx".into())
        .spawn(move || {
            let mut buf = [0u8; 4096];
            loop {
                match reader.read(&mut buf) {
                    Ok(0) => {
                        // EOF (socket closed). Stay alive but idle so the
                        // async side simply sees silence.
                        thread::sleep(StdDuration::from_millis(50));
                    }
                    Ok(n) => {
                        let mut off = 0;
                        while off < n {
                            match p2.try_write(&buf[off..n]) {
                                Ok(k) => off += k,
                                Err(_) => thread::sleep(StdDuration::from_millis(1)),
                            }
                        }
                    }
                    Err(e) => match e.kind() {
                        std::io::ErrorKind::TimedOut
                        | std::io::ErrorKind::WouldBlock
                        | std::io::ErrorKind::Interrupted => {}
                        _ => {
                            log::warn!("port reader: {e}");
                            thread::sleep(StdDuration::from_millis(100));
                        }
                    },
                }
            }
        })
        .expect("spawn port reader thread");
    (
        PortReader { pipe },
        PortWriter {
            w: Box::new(writer),
        },
    )
}

impl embedded_io_async::ErrorType for PortReader {
    type Error = embedded_io_async::ErrorKind;
}

impl embedded_io_async::Read for PortReader {
    async fn read(&mut self, buf: &mut [u8]) -> Result<usize, Self::Error> {
        Ok(self.pipe.read(buf).await)
    }
}

impl embedded_io_async::ErrorType for PortWriter {
    type Error = embedded_io_async::ErrorKind;
}

impl embedded_io_async::Write for PortWriter {
    async fn write(&mut self, buf: &[u8]) -> Result<usize, Self::Error> {
        self.w
            .write_all(buf)
            .map(|_| buf.len())
            .map_err(|_| embedded_io_async::ErrorKind::Other)
    }

    async fn flush(&mut self) -> Result<(), Self::Error> {
        self.w
            .flush()
            .map_err(|_| embedded_io_async::ErrorKind::Other)
    }
}

/// Open a serial port the way the original console does: 57600 8N1, RTS/CTS
/// flow control, DTR off (DCB in CWin32Hardware 0x40ec00).
///
/// RTS/CTS needs a lead that crosses pins 7/8 (Lloyd 09/0639). With a lead
/// that only crosses 2/3, pass `rtscts = false` and jumper 7-8 at the machine
/// end, otherwise neither side may ever be clear to send.
pub fn open_serial(path: &str, rtscts: bool) -> std::io::Result<(PortReader, PortWriter)> {
    let mut port = serialport::new(path, BAUDRATE)
        .data_bits(serialport::DataBits::Eight)
        .parity(serialport::Parity::None)
        .stop_bits(serialport::StopBits::One)
        .flow_control(if rtscts {
            serialport::FlowControl::Hardware
        } else {
            serialport::FlowControl::None
        })
        .timeout(StdDuration::from_millis(50))
        .open()
        .map_err(std::io::Error::other)?;
    port.write_data_terminal_ready(false)
        .map_err(std::io::Error::other)?;
    if !rtscts {
        // Without flow control the machine still gates its transmitter on CTS.
        port.write_request_to_send(true)
            .map_err(std::io::Error::other)?;
    }
    port.clear(serialport::ClearBuffer::Input)
        .map_err(std::io::Error::other)?;
    let writer = port.try_clone().map_err(std::io::Error::other)?;
    Ok(threaded_port(port, writer))
}

/// Connect to a `python -m lloydplus.simserver` (or any TCP-bridged frame).
pub fn open_tcp<A: ToSocketAddrs>(addr: A) -> std::io::Result<(PortReader, PortWriter)> {
    let s = TcpStream::connect(addr)?;
    s.set_nodelay(true)?;
    s.set_read_timeout(Some(StdDuration::from_millis(50)))?;
    let w = s.try_clone()?;
    Ok(threaded_port(s, w))
}

/// Open `--port PATH` or `--tcp HOST:PORT` style targets: a string with a
/// colon and no slash is a TCP address, anything else a serial device.
pub fn open_target(target: &str, rtscts: bool) -> std::io::Result<(PortReader, PortWriter)> {
    if target.contains(':') && !target.contains('/') {
        open_tcp(target)
    } else {
        open_serial(target, rtscts)
    }
}

/// Current UTC time as the fields `SetSystemTime` wants.
pub fn now_utc() -> UtcTime {
    let secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let days = secs.div_euclid(86_400);
    let rem = secs.rem_euclid(86_400);
    // civil-from-days (Howard Hinnant)
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = if m <= 2 { y + 1 } else { y };
    UtcTime {
        year: y as u16,
        month: m as u8,
        dow: ((days + 4).rem_euclid(7)) as u8, // 1970-01-01 was a Thursday
        day: d as u8,
        hour: (rem / 3600) as u8,
        minute: ((rem % 3600) / 60) as u8,
        second: (rem % 60) as u8,
    }
}

/// Run a future to completion on this thread.
pub fn block_on<F: core::future::Future>(f: F) -> F::Output {
    futures_lite::future::block_on(f)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn utc_fields_are_sane() {
        let t = now_utc();
        assert!(t.year >= 2026);
        assert!((1..=12).contains(&t.month));
        assert!((1..=31).contains(&t.day));
        assert!(t.dow <= 6);
        assert!(t.hour <= 23 && t.minute <= 59 && t.second <= 59);
    }
}
