//! Application services of the Plus-series machine, and the PC-side services
//! the machine may open on us.
//!
//! Function addresses refer to NEXYGEN 4.1 `EZConsole/LrCon.exe`.

use alloc::{string::String, sync::Arc, vec::Vec};
use core::{cell::RefCell, sync::atomic::Ordering};

use embassy_sync::{
    blocking_mutex::{Mutex, raw::CriticalSectionRawMutex},
    channel::Channel,
    signal::Signal,
};
use embassy_time::{Duration, Instant, Timer, with_timeout};

use crate::channels::{Handler, Mux, PacketQueue, ServiceClient, ServiceOpenError, local_service};
use crate::fmt::{debug, error, info, warn};
use crate::link::LinkError;

type CS = CriticalSectionRawMutex;

/// Version reported by the original console (file version 4.1, issue 9).
pub const PC_VERSION: [u8; 3] = [4, 1, 9];
/// InternalName + " Version " + FileVersion of LrCon.exe (0x416160).
pub const PC_DESCRIPTION: &str = "LrCon Version 4.1 Issue 9";

/// Default per-request timeout.
pub const TIMEOUT: Duration = Duration::from_secs(5);

/// C `atof`: parse the longest numeric prefix, 0.0 if there is none.
pub fn atof(text: &[u8]) -> f64 {
    let mut i = 0;
    while i < text.len() && (text[i] == b' ' || (9..=13).contains(&text[i])) {
        i += 1;
    }
    let start = i;
    if i < text.len() && (text[i] == b'+' || text[i] == b'-') {
        i += 1;
    }
    let mut digits = 0;
    while i < text.len() && text[i].is_ascii_digit() {
        i += 1;
        digits += 1;
    }
    if i < text.len() && text[i] == b'.' {
        i += 1;
        while i < text.len() && text[i].is_ascii_digit() {
            i += 1;
            digits += 1;
        }
    }
    if digits == 0 {
        return 0.0;
    }
    // optional exponent, only if well formed
    if i < text.len() && (text[i] == b'e' || text[i] == b'E') {
        let mut j = i + 1;
        if j < text.len() && (text[j] == b'+' || text[j] == b'-') {
            j += 1;
        }
        let ds = j;
        while j < text.len() && text[j].is_ascii_digit() {
            j += 1;
        }
        if j > ds {
            i = j;
        }
    }
    core::str::from_utf8(&text[start..i])
        .ok()
        .and_then(|s| s.parse::<f64>().ok())
        .unwrap_or(0.0)
}

/// Text up to the first NUL, decoded as Latin-1.
fn cstr(data: &[u8]) -> String {
    data.iter()
        .take_while(|&&b| b != 0)
        .map(|&b| b as char)
        .collect()
}

/// Machine message codes (FUN_00401dd0 -> LrConRes.dll string table 0x402..).
pub fn message_text(code: i16) -> Option<&'static str> {
    Some(match code {
        0 => "Machine communications protocol violation.",
        1 => "Motor fault.",
        2 => "Upper extension limit switch reached.",
        3 => "Lower extension limit switch reached.",
        4 => "Safe line is open.",
        5 => "No load cell connected.",
        6 => "Guard not closed.",
        7 => "Load cell rating exceeded.",
        8 => "Compression load limit for manual control exceeded.",
        9 => "Machine frame load rating exceeded.",
        10 => "Load has exceeded safety limit.",
        0x1B => "Load cell calibration due.",
        0x1C => "Fatal hardware fault.",
        _ => return None,
    })
}

/// Machine warning "Load has exceeded safety limit.": the grip-protection trip.
pub const SAFETY_LIMIT_CODE: i16 = 10;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum MessageSource {
    Status,
    Error,
    Warning,
}

/// Status/error message from the machine: 16-bit code plus free text. For
/// codes not in [`message_text`] the original console shows `text`.
#[derive(Debug, Clone, PartialEq)]
pub struct MachineMessage {
    pub code: i16,
    pub text: String,
    pub source: MessageSource,
}

impl MachineMessage {
    pub fn parse(data: &[u8], source: MessageSource) -> Self {
        let code = i16::from_be_bytes([data[0], data[1]]);
        Self {
            code,
            text: cstr(&data[2..]),
            source,
        }
    }

    pub fn description(&self) -> &str {
        message_text(self.code).unwrap_or(&self.text)
    }
}

/// Queue of machine messages from the local Error service and the status
/// monitor.
pub struct MessageQueue {
    ch: Channel<CS, MachineMessage, 16>,
    /// The grip-protection trip (code 10) is latched separately so it can
    /// never be lost to a full queue: the machine sends no transition for
    /// it, so a missed one would leave a segment running forever.
    trip: Signal<CS, MachineMessage>,
}

impl Default for MessageQueue {
    fn default() -> Self {
        Self::new()
    }
}

impl MessageQueue {
    pub const fn new() -> Self {
        Self {
            ch: Channel::new(),
            trip: Signal::new(),
        }
    }
    pub fn push(&self, m: MachineMessage) {
        if m.code == SAFETY_LIMIT_CODE {
            self.trip.signal(m);
            return;
        }
        if self.ch.try_send(m).is_err() {
            warn!("machine message queue full; message dropped");
        }
    }
    /// The next grip-protection trip message.
    pub async fn wait_trip(&self) -> MachineMessage {
        self.trip.wait().await
    }
    /// Queue for the application: every message, in order, no latching.
    pub fn push_user(&self, m: MachineMessage) {
        if self.ch.try_send(m).is_err() {
            warn!("machine message queue full; message dropped");
        }
    }
    pub async fn receive(&self) -> MachineMessage {
        self.ch.receive().await
    }
    pub fn try_receive(&self) -> Option<MachineMessage> {
        self.ch.try_receive().ok()
    }
}

// --------------------------------------------------------------------------
// Services the machine opens on the PC (CLrStack_v3 0x40a140)
// --------------------------------------------------------------------------

struct ReplyHandler<F: Fn(&Mux, &[u8]) + Send + Sync>(F);

impl<F: Fn(&Mux, &[u8]) + Send + Sync> Handler for ReplyHandler<F> {
    fn on_packet(&self, mux: &Mux, payload: &[u8]) {
        (self.0)(mux, payload)
    }
}

/// Install Version, Description, Responder, Error and Debug2 (plus the
/// obsolete "Obsolete") so the machine's own stack can open them on us.
pub fn install_local_services(mux: &Mux, messages: Arc<MessageQueue>) {
    // CVersionService 0x40e180
    mux.add_local_service(
        "Version",
        local_service(|_, _| {
            Some(Arc::new(ReplyHandler(|m: &Mux, p: &[u8]| {
                if let Some(&ch) = p.first()
                    && m.try_send(ch, &PC_VERSION).is_err()
                {
                    warn!("Version reply dropped: link queue full");
                }
            })))
        }),
    );
    // CDescriptionService 0x40bed0
    mux.add_local_service(
        "Description",
        local_service(|_, _| {
            Some(Arc::new(ReplyHandler(|m: &Mux, p: &[u8]| {
                if p.len() >= 2 && m.try_send(p[0], PC_DESCRIPTION.as_bytes()).is_err() {
                    warn!("Description reply dropped: link queue full");
                }
            })))
        }),
    );
    // CResponderService 0x40d450
    mux.add_local_service(
        "Responder",
        local_service(|_, _| {
            Some(Arc::new(ReplyHandler(|m: &Mux, p: &[u8]| {
                if let Some((&ch, rest)) = p.split_first()
                    && m.try_send(ch, rest).is_err()
                {
                    warn!("Responder reply dropped: link queue full");
                }
            })))
        }),
    );
    // CErrorService 0x4089a0: [kind, code_hi, code_lo, text]; kind 2 = warning
    let q = messages.clone();
    mux.add_local_service(
        "Error",
        local_service(move |_, _| {
            let q = q.clone();
            Some(Arc::new(ReplyHandler(move |_m: &Mux, p: &[u8]| {
                if p.len() > 2 {
                    let source = if p[0] == 2 {
                        MessageSource::Warning
                    } else {
                        MessageSource::Error
                    };
                    let msg = MachineMessage::parse(&p[1..], source);
                    warn!(
                        "machine {:?} {}: {}",
                        crate::fmt::Debug2Format(&msg.source),
                        msg.code,
                        msg.description()
                    );
                    q.push(msg);
                }
            })))
        }),
    );
    // CWin32Debug2Service
    mux.add_local_service(
        "Debug2",
        local_service(|_, _| {
            Some(Arc::new(ReplyHandler(|_m: &Mux, p: &[u8]| {
                debug!("machine debug [{}]", p.len());
            })))
        }),
    );
    mux.add_obsolete_service("Obsolete");
}

// --------------------------------------------------------------------------
// Machine services
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum ServiceError {
    Open(ServiceOpenError),
    Link(LinkError),
    /// No reply within the timeout, from the named service.
    Timeout(&'static str),
    /// The reply did not have the documented shape.
    Malformed(&'static str),
    /// The peer closed the channel.
    Closed(&'static str),
}

impl From<ServiceOpenError> for ServiceError {
    fn from(e: ServiceOpenError) -> Self {
        ServiceError::Open(e)
    }
}
impl From<LinkError> for ServiceError {
    fn from(e: LinkError) -> Self {
        ServiceError::Link(e)
    }
}

impl core::fmt::Display for ServiceError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            ServiceError::Open(e) => write!(f, "could not open service: {e}"),
            ServiceError::Link(e) => write!(f, "{e}"),
            ServiceError::Timeout(s) => write!(f, "no reply from {s}"),
            ServiceError::Malformed(s) => write!(f, "{s} reply malformed"),
            ServiceError::Closed(s) => write!(f, "{s} closed by the machine"),
        }
    }
}

async fn reply(
    s: &ServiceClient,
    name: &'static str,
    timeout: Duration,
) -> Result<Vec<u8>, ServiceError> {
    match s.recv(timeout).await {
        Ok(Some(p)) => Ok(p),
        Ok(None) => Err(ServiceError::Closed(name)),
        Err(_) => Err(ServiceError::Timeout(name)),
    }
}

/// Machine firmware version (major, minor, issue). CVersionChannelHandler 0x429ab0.
pub async fn query_version(
    mux: &'static Mux,
    timeout: Duration,
) -> Result<(u8, u8, u8), ServiceError> {
    let s = ServiceClient::open(mux, "Version", timeout).await?;
    let r = async {
        s.subscribe().await?;
        let p = reply(&s, "Version", timeout).await?;
        if p.len() != 3 {
            return Err(ServiceError::Malformed("Version"));
        }
        Ok((p[0], p[1], p[2]))
    }
    .await;
    s.close().await;
    r
}

/// Three machine description strings, requested with length limits 0, 15 and
/// 30 (CDescriptionChannelHandler 0x422fb0 / 0x4231f0).
pub async fn query_description(
    mux: &'static Mux,
    timeout: Duration,
) -> Result<[String; 3], ServiceError> {
    let s = ServiceClient::open(mux, "Description", timeout).await?;
    let r = async {
        for limit in [0x00u8, 0x0F, 0x1E] {
            s.send(&[s.local, limit]).await?;
        }
        let mut out: [String; 3] = Default::default();
        for slot in out.iter_mut() {
            *slot = cstr(&reply(&s, "Description", timeout).await?);
        }
        Ok(out)
    }
    .await;
    s.close().await;
    r
}

/// Frame limits from the "TensileParameters" service (0x424990), read as N,
/// mm, mm/s, mm/s (PROTOCOL.md §10). Missing fields are NaN.
#[derive(Debug, Clone, PartialEq)]
pub struct MachineParameters {
    pub load_range: f64,
    pub extension_range: f64,
    pub min_speed: f64,
    pub max_speed: f64,
    pub raw: Vec<String>,
}

pub async fn query_parameters(
    mux: &'static Mux,
    timeout: Duration,
) -> Result<MachineParameters, ServiceError> {
    let s = ServiceClient::open(mux, "TensileParameters", timeout).await?;
    let r = async {
        s.subscribe().await?;
        let p = reply(&s, "TensileParameters", timeout).await?;
        if !(2..=100).contains(&p.len()) || p[p.len() - 2..] != [0, 0] {
            return Err(ServiceError::Malformed("TensileParameters"));
        }
        // 0x424990 stops at the first empty field
        let mut vals = [f64::NAN; 4];
        let mut raw = Vec::new();
        for (i, f) in p.split(|&b| b == 0).take(4).enumerate() {
            if f.is_empty() {
                break;
            }
            vals[i] = atof(f);
            raw.push(cstr(f));
        }
        Ok(MachineParameters {
            load_range: vals[0],
            extension_range: vals[1],
            min_speed: vals[2],
            max_speed: vals[3],
            raw,
        })
    }
    .await;
    s.close().await;
    r
}

/// One data source from the "DataInformation" service (0x420620). `index` 0 is
/// the load channel, 1 the crosshead extension; further indices are optional
/// transducers. `kind` 0 = not fitted, 1 = load, 2 = extension, 3 = other.
#[derive(Debug, Clone, PartialEq)]
pub struct DataSourceInfo {
    pub index: u8,
    pub kind: u8,
    pub extra: u8,
    pub a: f64,
    pub b: f64,
    pub name: String,
}

impl DataSourceInfo {
    /// max(a, b): what the console keeps as the source range.
    pub fn range(&self) -> f64 {
        if self.a >= self.b { self.a } else { self.b }
    }

    pub fn parse(p: &[u8]) -> Self {
        let mut parts = p[3..].split(|&b| b == 0);
        let a = parts.next().map(atof).unwrap_or(0.0);
        let b = parts.next().map(atof).unwrap_or(0.0);
        let name = parts.next().map(cstr).unwrap_or_default();
        Self {
            index: p[0],
            kind: p[1],
            extra: p[2],
            a,
            b,
            name,
        }
    }
}

pub async fn query_data_information(
    mux: &'static Mux,
    timeout: Duration,
) -> Result<Vec<DataSourceInfo>, ServiceError> {
    let s = ServiceClient::open(mux, "DataInformation", timeout).await?;
    let r = async {
        s.subscribe().await?;
        let mut out = Vec::new();
        loop {
            let p = reply(&s, "DataInformation", timeout).await?;
            if p.is_empty() {
                return Ok(out);
            }
            if !(5..=0xFA).contains(&p.len()) {
                return Err(ServiceError::Malformed("DataInformation"));
            }
            out.push(DataSourceInfo::parse(&p));
        }
    }
    .await;
    s.close().await;
    r
}

// -- readings -------------------------------------------------------------------

/// Milliseconds per machine time tick (DAT_0043e9e8: 1/1024 s).
pub const TICK_MS: f64 = 0.9765625;

#[derive(Debug, Clone, Copy, PartialEq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub struct Sample {
    /// Data source index (0 = load, 1 = extension, ...).
    pub source: u8,
    /// Machine time in 1/1024 s ticks since stream start.
    pub ticks: u64,
    /// Accumulated counts since the last zero.
    pub raw: i64,
    /// `raw * scale`: N for load, mm for extension.
    pub value: f64,
}

impl Sample {
    /// Seconds since stream start.
    pub fn t(&self) -> f64 {
        self.ticks as f64 * TICK_MS / 1000.0
    }
}

/// Where decoded samples go. Called from the link's receive context.
pub trait SampleSink: Send + Sync {
    fn sample(&self, s: Sample);
}

struct DecState {
    accum: i64,
    ticks: u64,
    increment: i64,
    scale: f64,
    zero_offset: i32,
    errors: u32,
}

/// Per-source reading stream (CDataSourcesChannelHandler 0x41ec20).
///
/// Packets are `[type, ...]`:
/// - 0 data: big-endian int16 deltas; each adds to the accumulator and advances
///   time by the current increment → one sample each
/// - 1 DS_ACCUM: int32 BE added to the accumulator (no sample)
/// - 2 DS_ZERO: int32 BE raw zero offset; accumulator reset to 0
/// - 3 DS_TIME: int16 BE time increment per sample (> 0)
/// - 4 ASCII decimal scale (value units per count)
pub struct SourceDecoder {
    pub index: u8,
    sink: Arc<dyn SampleSink>,
    st: Mutex<CS, RefCell<DecState>>,
}

impl SourceDecoder {
    pub fn new(index: u8, sink: Arc<dyn SampleSink>) -> Self {
        Self {
            index,
            sink,
            st: Mutex::new(RefCell::new(DecState {
                accum: 0,
                ticks: 0,
                increment: 1,
                scale: 0.0,
                zero_offset: 0,
                errors: 0,
            })),
        }
    }

    pub fn errors(&self) -> u32 {
        self.st.lock(|s| s.borrow().errors)
    }

    /// Decode one packet into samples (pure; no sink calls).
    fn decode(&self, p: &[u8], out: &mut Vec<Sample>) {
        let Some((&kind, d)) = p.split_first() else {
            return;
        };
        self.st.lock(|s| {
            let mut s = s.borrow_mut();
            match kind {
                0 => {
                    for pair in d.chunks_exact(2) {
                        s.accum = s
                            .accum
                            .wrapping_add(i16::from_be_bytes([pair[0], pair[1]]) as i64);
                        s.ticks = s.ticks.wrapping_add(s.increment as u64);
                        out.push(Sample {
                            source: self.index,
                            ticks: s.ticks,
                            raw: s.accum,
                            value: s.accum as f64 * s.scale,
                        });
                    }
                }
                1 if d.len() == 4 => {
                    s.accum = s
                        .accum
                        .wrapping_add(i32::from_be_bytes([d[0], d[1], d[2], d[3]]) as i64);
                }
                2 if d.len() == 4 => {
                    s.accum = 0;
                    s.zero_offset = i32::from_be_bytes([d[0], d[1], d[2], d[3]]);
                }
                3 if d.len() == 2 => {
                    let inc = i16::from_be_bytes([d[0], d[1]]) as i64;
                    if inc > 0 {
                        s.increment = inc;
                    } else {
                        // the console stores it anyway and reports "time_increment bad"
                        s.errors += 1;
                        warn!("source {}: bad time increment {} ignored", self.index, inc);
                    }
                }
                4 => {
                    let text: Vec<u8> = d.iter().copied().take_while(|&b| b != 0).collect();
                    s.scale = atof(&text);
                }
                _ => {
                    s.errors += 1;
                    warn!(
                        "source {}: bad packet kind {} [{}]",
                        self.index,
                        kind,
                        d.len()
                    );
                }
            }
        });
    }
}

impl Handler for SourceDecoder {
    fn on_packet(&self, _mux: &Mux, payload: &[u8]) {
        let mut out = Vec::with_capacity(payload.len() / 2);
        self.decode(payload, &mut out);
        for s in out {
            self.sink.sample(s);
        }
    }
}

/// Live readings: opens "DataSet" and subscribes one channel per source. The
/// subscription packet is the list of our per-source channel numbers in
/// data-source index order (FUN_0041e250).
pub struct DataSetStream {
    mux: &'static Mux,
    pub service: ServiceClient,
    pub decoders: Vec<Arc<SourceDecoder>>,
    channels: Vec<u8>,
}

impl DataSetStream {
    pub async fn open(
        mux: &'static Mux,
        sources: &[DataSourceInfo],
        sink: Arc<dyn SampleSink>,
        timeout: Duration,
    ) -> Result<Self, ServiceError> {
        // the console keeps one entry per index, ascending
        let mut indices: Vec<u8> = sources.iter().map(|s| s.index).collect();
        indices.sort_unstable();
        indices.dedup();
        let mut decoders = Vec::new();
        let mut channels = Vec::new();
        for idx in indices {
            let dec = Arc::new(SourceDecoder::new(idx, sink.clone()));
            match mux.allocate(dec.clone()) {
                Ok(ch) => channels.push(ch),
                Err(e) => {
                    for &ch in &channels {
                        mux.free(ch);
                    }
                    return Err(e.into());
                }
            }
            decoders.push(dec);
        }
        let service = match ServiceClient::open(mux, "DataSet", timeout).await {
            Ok(s) => s,
            Err(e) => {
                for &ch in &channels {
                    mux.free(ch);
                }
                return Err(e.into());
            }
        };
        if let Err(e) = service.send(&channels).await {
            service.close().await;
            for &ch in &channels {
                mux.free(ch);
            }
            return Err(e.into());
        }
        Ok(Self {
            mux,
            service,
            decoders,
            channels,
        })
    }

    pub async fn close(&self) {
        self.service.close().await;
        for &ch in &self.channels {
            self.mux.free(ch);
        }
    }

    pub fn abandon(&self) {
        self.service.abandon();
        for &ch in &self.channels {
            self.mux.free(ch);
        }
    }
}

/// "StatusInformation": the machine pushes messages `[code_hi, code_lo,
/// text...]`; an empty packet ends a batch (0x428f70).
struct StatusHandler(Arc<MessageQueue>);

impl Handler for StatusHandler {
    fn on_packet(&self, _mux: &Mux, payload: &[u8]) {
        if payload.len() >= 2 {
            let msg = MachineMessage::parse(payload, MessageSource::Status);
            info!("machine status {}: {}", msg.code, msg.description());
            self.0.push(msg);
        }
    }
}

pub async fn open_status_monitor(
    mux: &'static Mux,
    messages: Arc<MessageQueue>,
    timeout: Duration,
) -> Result<ServiceClient, ServiceError> {
    let s = ServiceClient::open_with(
        mux,
        "StatusInformation",
        Arc::new(StatusHandler(messages)),
        timeout,
    )
    .await?;
    if let Err(e) = s.subscribe().await {
        s.close().await;
        return Err(e.into());
    }
    Ok(s)
}

/// Every `interval` send `[chan]` to the machine's "Responder"; the machine
/// echoes an empty packet back on `chan`. Returns when a ping goes unanswered
/// for a whole interval (the console declares "Timeout waiting for machine
/// reply", CMachineWatchdog 0x429e00/0x429f00, 10 s there).
pub async fn watchdog(mux: &'static Mux, responder: &ServiceClient, interval: Duration) {
    let q = Arc::new(PacketQueue::new());
    let Ok(ch) = mux.allocate(q.clone()) else {
        error!("watchdog: no channel");
        return;
    };
    // The console pings after the first interval, not at once.
    Timer::after(interval).await;
    loop {
        let t0 = Instant::now();
        if let Err(e) = responder.send(&[ch]).await {
            warn!("watchdog: ping not sent: {}", crate::fmt::Debug2Format(&e));
        } else {
            match with_timeout(interval, q.receive()).await {
                Ok(Some(_)) => {}
                _ => {
                    error!("watchdog: machine did not answer");
                    break;
                }
            }
        }
        Timer::at(t0 + interval).await;
    }
    mux.free(ch);
}

/// UTC wall-clock fields for [`set_system_time`]. `dow` is 0 = Sunday.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct UtcTime {
    pub year: u16,
    pub month: u8,
    pub dow: u8,
    pub day: u8,
    pub hour: u8,
    pub minute: u8,
    pub second: u8,
}

/// "SetSystemTime": NUL-terminated decimal fields year, month, day-of-week
/// (0 = Sunday), day, hour, minute, second, in UTC (GetSystemTime), then the
/// channel is closed (0x424020, 0x423ff0).
pub async fn set_system_time(
    mux: &'static Mux,
    t: &UtcTime,
    timeout: Duration,
) -> Result<(), ServiceError> {
    use core::fmt::Write as _;
    let mut data = String::new();
    for v in [
        t.year as u32,
        t.month as u32,
        t.dow as u32,
        t.day as u32,
        t.hour as u32,
        t.minute as u32,
        t.second as u32,
    ] {
        let _ = write!(data, "{v}\0");
    }
    // "%d%c" x7 with %c = NUL, sent including snprintf's own terminator
    data.push('\0');
    let s = ServiceClient::open(mux, "SetSystemTime", timeout).await?;
    let r = s.send(data.as_bytes()).await;
    s.close().await;
    r.map_err(Into::into)
}

#[allow(dead_code)]
fn _unused(_: Ordering) {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn atof_prefixes() {
        assert_eq!(atof(b"1000"), 1000.0);
        assert_eq!(atof(b" -1100abc"), -1100.0);
        assert_eq!(atof(b"0.000166667"), 0.000166667);
        assert_eq!(atof(b"1.5e3x"), 1500.0);
        assert_eq!(atof(b"1.5e"), 1.5);
        assert_eq!(atof(b".5"), 0.5);
        assert_eq!(atof(b"abc"), 0.0);
        assert_eq!(atof(b""), 0.0);
    }

    #[test]
    fn message_parse() {
        let m = MachineMessage::parse(
            &[0, 10, b'L', b'o', b'a', b'd', 0, b'x'],
            MessageSource::Warning,
        );
        assert_eq!(m.code, 10);
        assert_eq!(m.text, "Load");
        assert_eq!(m.description(), "Load has exceeded safety limit.");
        let m = MachineMessage::parse(&[0xFF, 0xFE, b'?'], MessageSource::Status);
        assert_eq!(m.code, -2);
        assert_eq!(m.description(), "?");
    }

    #[test]
    fn data_source_parse() {
        let p = b"\x00\x01\x00-1000\x001000\x00Load\x00";
        let d = DataSourceInfo::parse(p);
        assert_eq!((d.index, d.kind, d.extra), (0, 1, 0));
        assert_eq!(d.range(), 1000.0);
        assert_eq!(d.name, "Load");
    }
}
