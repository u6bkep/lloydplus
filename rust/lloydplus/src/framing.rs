//! Byte framing and fault-control layer of the Lloyd "Plus" PC link.
//!
//! Wire format of one frame (reverse engineered from NEXYGEN 4.1 LrCon.exe,
//! CFraming 0x40c2e0/0x40c410 and CFaultControl 0x40c070/0x40c170):
//!
//! ```text
//! 0xDC  escape( crc_hi crc_lo len body[len] )
//! ```
//!
//! * `0xDC` starts a frame. Inside a frame the bytes `0xDC` and `0xDD` are sent
//!   as `0xDD` followed by `byte ^ 0x80`.
//! * `crc` is CRC-16/ARC (reflected poly 0xA001, init 0) over `body`,
//!   transmitted big-endian. `len` is `body.len()`.
//! * `body[0]` is the protocol id (see [`crate::link`]).
//!
//! The original receiver only delivers a frame when the *next* `0xDC` arrives.
//! This decoder additionally delivers as soon as the length byte and CRC say
//! the frame is complete, so a trailing frame is never held back.

use alloc::vec::Vec;

pub const FRAME_START: u8 = 0xDC;
pub const ESCAPE: u8 = 0xDD;
pub const MAX_BODY: usize = 0xFF;
/// Receive buffer limit in the original (CFraming).
const MAX_RAW: usize = 0x102;

const fn make_table() -> [u16; 256] {
    let mut table = [0u16; 256];
    let mut i = 0;
    while i < 256 {
        let mut c = i as u16;
        let mut k = 0;
        while k < 8 {
            c = if c & 1 != 0 {
                (c >> 1) ^ 0xA001
            } else {
                c >> 1
            };
            k += 1;
        }
        table[i] = c;
        i += 1;
    }
    table
}

static CRC_TABLE: [u16; 256] = make_table();

/// CRC-16/ARC over `data`.
pub fn crc16_arc(data: &[u8]) -> u16 {
    let mut crc: u16 = 0;
    for &b in data {
        crc = CRC_TABLE[((crc ^ b as u16) & 0xFF) as usize] ^ (crc >> 8);
    }
    crc
}

/// Wrap a protocol body (`[proto, payload...]`) into wire bytes. The body must
/// be 1..=[`MAX_BODY`] bytes.
pub fn encode_frame(body: &[u8]) -> Vec<u8> {
    debug_assert!((1..=MAX_BODY).contains(&body.len()));
    let crc = crc16_arc(body);
    let mut out = Vec::with_capacity(body.len() + 8);
    out.push(FRAME_START);
    let header = [(crc >> 8) as u8, crc as u8, body.len() as u8];
    for &b in header.iter().chain(body) {
        if b == FRAME_START || b == ESCAPE {
            out.push(ESCAPE);
            out.push(b ^ 0x80);
        } else {
            out.push(b);
        }
    }
    out
}

/// Incremental decoder: feed raw serial bytes, get validated bodies.
#[derive(Default)]
pub struct FrameDecoder {
    buf: Vec<u8>,
    escaped: bool,
    /// The current frame has already been delivered or abandoned.
    done: bool,
    pub crc_errors: u32,
    pub length_errors: u32,
    pub frames: u32,
}

impl FrameDecoder {
    pub fn new() -> Self {
        Self {
            buf: Vec::with_capacity(MAX_RAW),
            ..Default::default()
        }
    }

    /// Feed `data`; `deliver` is called once per complete, CRC-valid body.
    pub fn feed(&mut self, data: &[u8], mut deliver: impl FnMut(&[u8])) {
        for &byte in data {
            let mut b = byte;
            if b == FRAME_START {
                if !self.buf.is_empty() && !self.done {
                    self.finish(true, &mut deliver);
                }
                self.buf.clear();
                self.escaped = false;
                self.done = false;
                continue;
            }
            if self.done {
                continue;
            }
            if b == ESCAPE {
                self.escaped = true;
                continue;
            }
            if self.escaped {
                b ^= 0x80;
                self.escaped = false;
            }
            if self.buf.len() >= MAX_RAW {
                self.length_errors += 1;
                self.done = true;
                continue;
            }
            self.buf.push(b);
            if self.buf.len() >= 4 && self.buf.len() - 3 == self.buf[2] as usize {
                self.finish(false, &mut deliver);
            }
        }
    }

    fn finish(&mut self, final_: bool, deliver: &mut impl FnMut(&[u8])) {
        let buf = &self.buf;
        if buf.len() < 4 || buf.len() - 3 != buf[2] as usize {
            if final_ {
                self.length_errors += 1;
            }
            return;
        }
        let body = &buf[3..];
        if crc16_arc(body) != ((buf[0] as u16) << 8 | buf[1] as u16) {
            self.crc_errors += 1;
            self.done = true;
            return;
        }
        self.frames += 1;
        self.done = true;
        deliver(body);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloc::vec;

    #[test]
    fn crc_vectors() {
        assert_eq!(crc16_arc(b"123456789"), 0xBB3D);
        assert_eq!(crc16_arc(&[]), 0);
    }

    #[test]
    fn round_trip_with_escapes() {
        let body = [0x02u8, 0xDC, 0xDD, 0x10];
        let wire = encode_frame(&body);
        assert_eq!(wire[0], FRAME_START);
        assert!(wire[1..].iter().all(|&b| b != FRAME_START));
        let mut dec = FrameDecoder::new();
        let mut got = vec![];
        dec.feed(&wire, |b| got.push(b.to_vec()));
        assert_eq!(got, vec![body.to_vec()]);
        assert_eq!(dec.frames, 1);
    }

    #[test]
    fn delivers_without_waiting_for_next_start() {
        let a = encode_frame(&[0xCE, 0]);
        let b = encode_frame(&[0xCE, 1]);
        let mut dec = FrameDecoder::new();
        let mut got = vec![];
        dec.feed(&a, |x| got.push(x.to_vec()));
        assert_eq!(got.len(), 1);
        dec.feed(&b[..3], |x| got.push(x.to_vec()));
        assert_eq!(got.len(), 1);
        dec.feed(&b[3..], |x| got.push(x.to_vec()));
        assert_eq!(got, vec![vec![0xCE, 0], vec![0xCE, 1]]);
    }

    #[test]
    fn bad_crc_is_counted_and_dropped() {
        let mut wire = encode_frame(&[0x02, 1, 2, 3]);
        let last = wire.len() - 1;
        wire[last] ^= 1;
        let mut dec = FrameDecoder::new();
        let mut n = 0;
        dec.feed(&wire, |_| n += 1);
        dec.feed(&[FRAME_START], |_| n += 1);
        assert_eq!(n, 0);
        assert_eq!(dec.crc_errors, 1);
    }
}
