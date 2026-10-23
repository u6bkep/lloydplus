//! Link layer: boot handshake and the reliable "transmission" protocol.
//!
//! Each frame body starts with a protocol id (CProtocolMultiplex 0x40d090):
//!
//! * `0xCE` BOOT — 1 byte payload, the sender's link state 0/1/2. While a side
//!   is below state 2 it keeps sending its state. Transition table
//!   (CBoot 0x40b1e0, table at 0x44ec50), `me` = my state, `rx` = received:
//!
//!   ```text
//!   me=0: rx 0/1 -> me=1
//!   me=1: rx 1/2 -> me=2 (connected)
//!   me=2: rx 1   -> reply 2 ; rx 0 -> peer restarted, reset the link
//!   ```
//!
//! * `0x02` TRANSMISSION — payload `[ack, seq, data...]` or just `[ack]`.
//!   Accepted only once connected. `seq` is assigned when a packet is sent and
//!   starts at 0; `ack` is the seq of the last packet delivered in order (also
//!   starts at 0). The receiver keeps an 80-packet reorder window and ignores
//!   anything older. The sender drops a queued packet once the peer's ack
//!   *changes* to a value at or after it (CTransmission 0x40db90); an ack equal
//!   to the initial value never releases anything, exactly like the original.
//!   Max data per packet 252 bytes (0x40d7c0).
//! * `0x05` ECHO (payload returned as proto 0x04), `0x04`/`0x06` discarded.
//!
//! The original PC console streams continuously (pure acks when idle) and
//! resends unacknowledged packets round-robin. Here an idle ack is sent at a
//! fixed interval and unacked packets are resent after a timeout.
//!
//! [`Link::run`] owns the byte port and drives both directions; everything
//! else talks to the link through `&Link`, so it is shared between tasks as a
//! `&'static`.

use alloc::{
    collections::{BTreeMap, VecDeque},
    string::String,
    vec::Vec,
};
use core::cell::RefCell;

use embassy_futures::{join::join, select::select};
use embassy_sync::{
    blocking_mutex::{Mutex, raw::CriticalSectionRawMutex},
    channel::Channel,
    signal::Signal,
};
use embassy_time::{Duration, Instant, Timer};
use embedded_io_async::{Read, Write};

use crate::fmt::{debug, info, warn};
use crate::framing::{FrameDecoder, encode_frame};

pub const PROTO_TRANSMISSION: u8 = 0x02;
pub const PROTO_RAW: u8 = 0x04;
pub const PROTO_ECHO: u8 = 0x05;
pub const PROTO_DISCARD: u8 = 0x06;
pub const PROTO_BOOT: u8 = 0xCE;

/// Reorder / ack window used by the original.
pub const WINDOW: u8 = 0x50;
/// Max transmission data bytes per packet.
pub const MAX_DATA: usize = 0xFC;
/// `(ack - seq) mod 256 <= this` → acknowledged.
const RELEASE_LIMIT: u8 = 0xAF;

type CS = CriticalSectionRawMutex;

/// Timing of the link. The defaults match the Python driver (which the LF Plus
/// was verified against): a 50 ms boot beacon, a 100 ms idle ack, 300 ms
/// retransmit, 16 packets in flight.
#[derive(Clone, Copy, Debug)]
pub struct LinkConfig {
    pub boot_interval: Duration,
    pub idle_ack_interval: Duration,
    pub retransmit_timeout: Duration,
    pub max_in_flight: usize,
}

impl Default for LinkConfig {
    fn default() -> Self {
        Self {
            boot_interval: Duration::from_millis(50),
            idle_ack_interval: Duration::from_millis(100),
            retransmit_timeout: Duration::from_millis(300),
            max_in_flight: 16,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum LinkEvent {
    /// The boot handshake completed.
    Connected,
    /// The peer went back to boot state 0: everything above the link is gone.
    Reset,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum LinkError {
    NotConnected,
    /// Packet longer than [`MAX_DATA`] or empty.
    BadLength,
    /// [`Link::try_send`] found the send queue full.
    QueueFull,
}

impl core::fmt::Display for LinkError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            LinkError::NotConnected => f.write_str("link not connected"),
            LinkError::BadLength => f.write_str("packet length out of range"),
            LinkError::QueueFull => f.write_str("link send queue full"),
        }
    }
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub struct LinkStats {
    pub tx_frames: u32,
    pub rx_frames: u32,
    pub retransmits: u32,
    pub rx_duplicates: u32,
    pub resets: u32,
}

/// Receiver of delivered transmission packets (`[channel, bytes...]`).
pub trait PacketSink: Sync {
    fn on_packet(&self, data: &[u8]);
}

struct Unacked {
    seq: u8,
    data: Vec<u8>,
    sent: Instant,
}

struct State {
    boot_state: u8,
    last_boot_tx: Option<Instant>,
    tx_seq: u8,
    unacked: VecDeque<Unacked>,
    peer_ack: u8,
    /// What we acknowledge.
    rx_ack: u8,
    rx_expected: u8,
    reorder: BTreeMap<u8, Vec<u8>>,
    ack_dirty: bool,
    last_tx: Option<Instant>,
    /// Frames to send at once regardless of state (boot replies, echoes).
    raw_out: VecDeque<Vec<u8>>,
    stats: LinkStats,
    last_trace: Option<String>,
}

impl State {
    const fn new() -> Self {
        Self {
            boot_state: 0,
            last_boot_tx: None,
            tx_seq: 0,
            unacked: VecDeque::new(),
            peer_ack: 0,
            rx_ack: 0,
            rx_expected: 0,
            reorder: BTreeMap::new(),
            ack_dirty: false,
            last_tx: None,
            raw_out: VecDeque::new(),
            stats: LinkStats {
                tx_frames: 0,
                rx_frames: 0,
                retransmits: 0,
                rx_duplicates: 0,
                resets: 0,
            },
            last_trace: None,
        }
    }

    fn reset(&mut self) {
        let stats = self.stats;
        let trace = self.last_trace.take();
        *self = Self::new();
        self.stats = stats;
        self.last_trace = trace;
    }
}

/// Symmetric link endpoint (the PC side here; the simulator uses the Python
/// twin).
pub struct Link {
    cfg: LinkConfig,
    state: Mutex<CS, RefCell<State>>,
    /// Packets accepted by [`Link::send`], not yet given a sequence number.
    queue: Channel<CS, Vec<u8>, 32>,
    tx_wake: Signal<CS, ()>,
    conn_changed: Signal<CS, ()>,
    events: Channel<CS, LinkEvent, 8>,
}

impl Default for Link {
    fn default() -> Self {
        Self::new(LinkConfig::default())
    }
}

impl Link {
    pub const fn new(cfg: LinkConfig) -> Self {
        Self {
            cfg,
            state: Mutex::new(RefCell::new(State::new())),
            queue: Channel::new(),
            tx_wake: Signal::new(),
            conn_changed: Signal::new(),
            events: Channel::new(),
        }
    }

    pub fn connected(&self) -> bool {
        self.state.lock(|s| s.borrow().boot_state == 2)
    }

    pub fn boot_state(&self) -> u8 {
        self.state.lock(|s| s.borrow().boot_state)
    }

    pub fn stats(&self) -> LinkStats {
        self.state.lock(|s| s.borrow().stats)
    }

    /// The last printable RAW (0x04) frame: the firmware's trace text
    /// ("Growing", "Maturing", "Now idle", "comm1 reset").
    pub fn last_trace(&self) -> Option<String> {
        self.state.lock(|s| s.borrow().last_trace.clone())
    }

    /// Wait until the handshake has completed.
    pub async fn wait_connected(&self) {
        loop {
            if self.connected() {
                return;
            }
            self.conn_changed.wait().await;
        }
    }

    /// Next link event (connected / reset). Events queue (8 deep).
    pub async fn next_event(&self) -> LinkEvent {
        self.events.receive().await
    }

    /// Queue `data` (`[channel, ...]`) for reliable delivery, waiting for
    /// queue room if necessary.
    pub async fn send(&self, data: Vec<u8>) -> Result<(), LinkError> {
        if data.is_empty() || data.len() > MAX_DATA {
            return Err(LinkError::BadLength);
        }
        if !self.connected() {
            return Err(LinkError::NotConnected);
        }
        self.queue.send(data).await;
        self.tx_wake.signal(());
        Ok(())
    }

    /// Non-blocking [`Link::send`], for use from packet handlers.
    pub fn try_send(&self, data: Vec<u8>) -> Result<(), LinkError> {
        if data.is_empty() || data.len() > MAX_DATA {
            return Err(LinkError::BadLength);
        }
        if !self.connected() {
            return Err(LinkError::NotConnected);
        }
        self.queue
            .try_send(data)
            .map_err(|_| LinkError::QueueFull)?;
        self.tx_wake.signal(());
        Ok(())
    }

    /// Drive the link on a byte port, forever. Delivered packets go to `sink`
    /// in order, from this future's context.
    pub async fn run<R: Read, W: Write>(&self, rx: R, tx: W, sink: &dyn PacketSink) {
        join(self.rx_loop(rx, sink), self.tx_loop(tx)).await;
    }

    // -- receiving ----------------------------------------------------------

    async fn rx_loop<R: Read>(&self, mut rx: R, sink: &dyn PacketSink) {
        let mut decoder = FrameDecoder::new();
        let mut buf = [0u8; 512];
        loop {
            match rx.read(&mut buf).await {
                Ok(0) => Timer::after_millis(20).await,
                Ok(n) => decoder.feed(&buf[..n], |body| self.on_body(body, sink)),
                Err(_) => {
                    warn!("link: port read error");
                    Timer::after_millis(100).await;
                }
            }
        }
    }

    fn on_body(&self, body: &[u8], sink: &dyn PacketSink) {
        // CMultiProtocol 0x40c6e0 needs proto + 1 byte.
        if body.len() < 2 {
            return;
        }
        self.state.lock(|s| s.borrow_mut().stats.rx_frames += 1);
        let (proto, payload) = (body[0], &body[1..]);
        match proto {
            PROTO_BOOT => {
                if payload.len() == 1 && payload[0] <= 2 {
                    self.on_boot(payload[0]);
                }
            }
            PROTO_TRANSMISSION => {
                if self.connected() {
                    self.on_transmission(payload, sink);
                }
            }
            PROTO_ECHO => {
                // CEcho 0x40c000 answers with the same payload as proto 0x04
                // (RAW), which is discarded on receipt, so echoes never
                // ping-pong.
                let mut f = Vec::with_capacity(payload.len() + 1);
                f.push(PROTO_RAW);
                f.extend_from_slice(payload);
                self.state.lock(|s| s.borrow_mut().raw_out.push_back(f));
                self.tx_wake.signal(());
            }
            _ => self.on_raw(proto, payload),
        }
    }

    fn on_raw(&self, proto: u8, payload: &[u8]) {
        // The firmware uses RAW (0x04) frames for trace text.
        if proto == PROTO_RAW
            && !payload.is_empty()
            && payload.iter().all(|&b| (32..127).contains(&b))
        {
            let text = String::from_utf8_lossy(payload).into_owned();
            let changed = self.state.lock(|s| {
                let mut s = s.borrow_mut();
                let changed = s.last_trace.as_deref() != Some(text.as_str());
                s.last_trace = Some(text.clone());
                changed
            });
            if changed {
                info!("link: machine trace {:?}", text.as_str());
            } else {
                debug!("link: machine trace again {:?}", text.as_str());
            }
        } else {
            debug!("link: discarding proto {:#x} [{}]", proto, payload.len());
        }
    }

    fn on_boot(&self, rx: u8) {
        let mut became_connected = false;
        let mut reset = false;
        let mut reply = false;
        self.state.lock(|s| {
            let mut s = s.borrow_mut();
            match (s.boot_state, rx) {
                (0, 0 | 1) => s.boot_state = 1,
                (1, 1 | 2) => {
                    s.boot_state = 2;
                    became_connected = true;
                }
                (2, 1) => reply = true,
                (2, 0) => {
                    reset = true;
                    s.stats.resets += 1;
                    s.reset();
                    // we have just seen the peer's state 0
                    s.boot_state = 1;
                }
                _ => {}
            }
            if reply {
                s.raw_out.push_back(alloc::vec![PROTO_BOOT, 2]);
            }
        });
        if reset {
            while self.queue.try_receive().is_ok() {}
            warn!("link: peer restarted the link");
            let _ = self.events.try_send(LinkEvent::Reset);
            self.conn_changed.signal(());
        }
        if became_connected {
            info!("link: connected");
            let _ = self.events.try_send(LinkEvent::Connected);
            self.conn_changed.signal(());
        }
        self.tx_wake.signal(());
    }

    fn on_transmission(&self, payload: &[u8], sink: &dyn PacketSink) {
        let mut deliver: Vec<Vec<u8>> = Vec::new();
        self.state.lock(|s| {
            let mut s = s.borrow_mut();
            if let Some(&ack) = payload.first()
                && ack.wrapping_sub(s.peer_ack) < WINDOW
                && ack != s.peer_ack
            {
                s.peer_ack = ack;
                s.unacked
                    .retain(|p| ack.wrapping_sub(p.seq) > RELEASE_LIMIT);
            }
            if payload.len() >= 2 {
                let (seq, data) = (payload[1], &payload[2..]);
                let off = seq.wrapping_sub(s.rx_expected);
                if off >= WINDOW {
                    s.stats.rx_duplicates += 1;
                    s.ack_dirty = true; // peer may have missed our ack
                } else {
                    s.reorder.insert(seq, data.to_vec());
                    loop {
                        let expected = s.rx_expected;
                        let Some(d) = s.reorder.remove(&expected) else {
                            break;
                        };
                        if !d.is_empty() {
                            s.rx_ack = expected;
                            deliver.push(d);
                        }
                        s.rx_expected = expected.wrapping_add(1);
                    }
                    s.ack_dirty = true;
                }
            }
        });
        self.tx_wake.signal(());
        for d in deliver {
            sink.on_packet(&d);
        }
    }

    // -- sending ------------------------------------------------------------

    async fn tx_loop<W: Write>(&self, mut tx: W) {
        loop {
            let (frames, next) = self.tx_step(Instant::now());
            if frames.is_empty() {
                match next {
                    Some(at) => {
                        select(self.tx_wake.wait(), Timer::at(at)).await;
                    }
                    None => self.tx_wake.wait().await,
                }
                continue;
            }
            for body in frames {
                if tx.write_all(&encode_frame(&body)).await.is_err() {
                    warn!("link: port write error");
                    Timer::after_millis(100).await;
                }
            }
        }
    }

    /// One pass of the sender: frames to write now and when to look again.
    fn tx_step(&self, now: Instant) -> (Vec<Vec<u8>>, Option<Instant>) {
        let cfg = self.cfg;
        let mut frames: Vec<Vec<u8>> = Vec::new();
        // Pull new packets outside the state lock (the channel has its own).
        let room = self.state.lock(|s| {
            let s = s.borrow();
            if s.boot_state == 2 {
                cfg.max_in_flight.saturating_sub(s.unacked.len())
            } else {
                0
            }
        });
        let mut fresh: Vec<Vec<u8>> = Vec::new();
        while fresh.len() < room {
            match self.queue.try_receive() {
                Ok(d) => fresh.push(d),
                Err(_) => break,
            }
        }
        let next = self.state.lock(|s| {
            let mut s = s.borrow_mut();
            frames.extend(s.raw_out.drain(..));
            let next;
            if s.boot_state < 2 {
                let due = s
                    .last_boot_tx
                    .is_none_or(|t| now.saturating_duration_since(t) >= cfg.boot_interval);
                if due {
                    s.last_boot_tx = Some(now);
                    frames.push(alloc::vec![PROTO_BOOT, s.boot_state]);
                }
                next = Some(s.last_boot_tx.unwrap_or(now) + cfg.boot_interval);
            } else {
                let ack = s.rx_ack;
                let mut sent_any = false;
                for data in fresh.drain(..) {
                    let seq = s.tx_seq;
                    s.tx_seq = s.tx_seq.wrapping_add(1);
                    let mut f = Vec::with_capacity(data.len() + 3);
                    f.extend_from_slice(&[PROTO_TRANSMISSION, ack, seq]);
                    f.extend_from_slice(&data);
                    frames.push(f);
                    s.unacked.push_back(Unacked {
                        seq,
                        data,
                        sent: now,
                    });
                    sent_any = true;
                }
                if !sent_any {
                    // retransmit the oldest unacked packet that timed out
                    let timed_out = s.unacked.front().is_some_and(|p| {
                        now.saturating_duration_since(p.sent) >= cfg.retransmit_timeout
                    });
                    if timed_out {
                        let mut p = s.unacked.pop_front().unwrap();
                        p.sent = now;
                        let mut f = Vec::with_capacity(p.data.len() + 3);
                        f.extend_from_slice(&[PROTO_TRANSMISSION, ack, p.seq]);
                        f.extend_from_slice(&p.data);
                        frames.push(f);
                        s.unacked.push_back(p);
                        s.stats.retransmits += 1;
                        sent_any = true;
                    }
                }
                if !sent_any {
                    let idle = s
                        .last_tx
                        .is_none_or(|t| now.saturating_duration_since(t) >= cfg.idle_ack_interval);
                    if s.ack_dirty || idle {
                        frames.push(alloc::vec![PROTO_TRANSMISSION, ack]);
                    }
                }
                if !frames.is_empty() {
                    s.ack_dirty = false;
                }
                let last_tx = if frames.is_empty() {
                    s.last_tx.unwrap_or(now)
                } else {
                    now
                };
                let mut n = last_tx + cfg.idle_ack_interval;
                if let Some(p) = s.unacked.front() {
                    let r = p.sent + cfg.retransmit_timeout;
                    if r < n {
                        n = r;
                    }
                }
                next = Some(n);
            }
            if !frames.is_empty() {
                s.last_tx = Some(now);
                s.stats.tx_frames += frames.len() as u32;
            }
            next
        });
        (frames, next)
    }
}
