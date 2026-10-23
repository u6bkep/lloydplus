//! Driver for the RS-232 PC link of Lloyd Instruments "Plus" series frames
//! (LF Plus, LRX Plus, LR Plus, EZ20/50), `no_std` and async.
//!
//! The protocol was reverse engineered from NEXYGEN 4.1 (`EZConsole/LrCon.exe`)
//! and verified on an LF Plus; see `PROTOCOL.md` at the repository root for the
//! wire format and the evidence behind each claim. This crate is a port of the
//! Python driver in the same repository, layer for layer:
//!
//! | module | contents |
//! |---|---|
//! | [`framing`] | 0xDC framing, escaping, CRC-16/ARC |
//! | [`link`] | boot handshake, reliable sliding-window transport |
//! | [`channels`] | channel multiplexer, opening services by name |
//! | [`services`] | machine information, `DataSet` readings, status, watchdog, PC-side services |
//! | [`stages`] | test control: stage arena, stage encoders, [`StageControl`], [`Drive`] |
//! | [`machine`] | [`LloydPlus`]: the connect sequence and the high-level API |
//!
//! Byte ports are `embedded-io-async` readers and writers; time comes from
//! `embassy-time`; shared state uses `embassy-sync` with a critical-section
//! mutex, so the crate runs under embassy on a microcontroller and, with the
//! `std` feature, under any executor on a host.

#![cfg_attr(not(feature = "std"), no_std)]

extern crate alloc;

pub mod channels;
pub mod fmt;
pub mod framing;
pub mod link;
pub mod machine;
pub mod services;
pub mod stages;

pub use channels::{Mux, ServiceClient, ServiceOpenError};
pub use framing::{FrameDecoder, crc16_arc, encode_frame};
pub use link::{Link, LinkConfig, LinkError, LinkEvent};
pub use machine::{BAUDRATE, ConnectError, ConnectOptions, LloydPlus};
pub use services::{
    DataSourceInfo, MachineMessage, MachineParameters, MessageSource, Sample, SampleSink,
    ServiceError, UtcTime,
};
pub use stages::{
    Drive, LimitUnit, Protection, Segment, SegmentEnd, SpeedUnit, StageControl, StageError,
};
