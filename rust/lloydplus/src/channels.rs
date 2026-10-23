//! Channel multiplexer and named-service connections (CChannel 0x40b2f0..).
//!
//! Every reliable packet is `[channel, bytes...]`.
//!
//! Channel 0 is the control channel:
//!
//! * `[reply_chan] + b"ServiceName"` asks the peer to open a service. The peer
//!   answers *on reply_chan* with `[status, service_chan]`; status 0 = ok,
//!   1 = obsolete, 2 = creation failed, 3 = unknown service.
//! * `[chan]` (a single byte) closes `chan` on the receiving side.
//!
//! After opening, requests go to `service_chan`. Nearly every machine service
//! then expects one byte naming the channel on which *we* want to receive its
//! output ("subscribe"); the open-reply channel itself is temporary.
//!
//! Channel numbers are never 0, 0xDC or 0xDD.
//!
//! Packets arrive from the link's receive context; a [`Handler`] runs there
//! and must not block. [`PacketQueue`] is the handler that hands packets to an
//! async consumer.

use alloc::{boxed::Box, collections::BTreeMap, string::String, sync::Arc, vec::Vec};
use core::{
    cell::RefCell,
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
};

use embassy_futures::select::{Either, select};
use embassy_sync::{
    blocking_mutex::{Mutex, raw::CriticalSectionRawMutex},
    channel::Channel,
    signal::Signal,
};
use embassy_time::{Duration, with_timeout};

use crate::fmt::{debug, info};
use crate::link::{Link, LinkError, PacketSink};

type CS = CriticalSectionRawMutex;

pub const STATUS_OK: u8 = 0;
pub const STATUS_OBSOLETE: u8 = 1;
pub const STATUS_FAILED: u8 = 2;
pub const STATUS_UNKNOWN: u8 = 3;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum ServiceOpenError {
    /// No reply within the timeout.
    Timeout,
    Obsolete,
    Failed,
    Unknown,
    Status(u8),
    Link(LinkError),
    /// All 253 channel numbers are in use.
    NoChannel,
}

impl From<LinkError> for ServiceOpenError {
    fn from(e: LinkError) -> Self {
        ServiceOpenError::Link(e)
    }
}

impl core::fmt::Display for ServiceOpenError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            ServiceOpenError::Timeout => f.write_str("timeout"),
            ServiceOpenError::Obsolete => f.write_str("obsolete"),
            ServiceOpenError::Failed => f.write_str("creation failed"),
            ServiceOpenError::Unknown => f.write_str("unknown service"),
            ServiceOpenError::Status(s) => write!(f, "status {s}"),
            ServiceOpenError::Link(e) => write!(f, "{e}"),
            ServiceOpenError::NoChannel => f.write_str("no free channel numbers"),
        }
    }
}

/// Receives the packets of one channel. Called from the link's receive
/// context: keep it short and never block.
pub trait Handler: Send + Sync {
    fn on_packet(&self, mux: &Mux, payload: &[u8]);
    /// The peer closed this channel.
    fn on_close(&self, _mux: &Mux) {}
}

/// A [`Handler`] from a closure.
pub struct FnHandler<F>(pub F);

impl<F: Fn(&Mux, &[u8]) + Send + Sync> Handler for FnHandler<F> {
    fn on_packet(&self, mux: &Mux, payload: &[u8]) {
        (self.0)(mux, payload)
    }
}

/// Factory for a service the *peer* may open on us: given the mux and the
/// peer's reply channel, return the handler for the new channel (or `None`
/// for "creation failed").
pub type LocalFactory = Arc<dyn Fn(&Mux, u8) -> Option<Arc<dyn Handler>> + Send + Sync>;

fn usable(ch: u8) -> bool {
    ch != 0 && ch != 0xDC && ch != 0xDD
}

struct MuxState {
    handlers: BTreeMap<u8, Arc<dyn Handler>>,
    /// The console seeds its search at 1 and hands out 2 first.
    next: u8,
    local: BTreeMap<String, LocalFactory>,
    obsolete: Vec<String>,
}

pub struct Mux {
    link: &'static Link,
    state: Mutex<CS, RefCell<MuxState>>,
}

impl Mux {
    pub fn new(link: &'static Link) -> Self {
        Self {
            link,
            state: Mutex::new(RefCell::new(MuxState {
                handlers: BTreeMap::new(),
                next: 2,
                local: BTreeMap::new(),
                obsolete: Vec::new(),
            })),
        }
    }

    pub fn link(&self) -> &'static Link {
        self.link
    }

    // -- channel numbers ------------------------------------------------------

    pub fn allocate(&self, handler: Arc<dyn Handler>) -> Result<u8, ServiceOpenError> {
        self.state.lock(|s| {
            let mut s = s.borrow_mut();
            for _ in 0..256 {
                let ch = s.next;
                s.next = s.next.wrapping_add(1);
                if usable(ch) && !s.handlers.contains_key(&ch) {
                    s.handlers.insert(ch, handler);
                    return Ok(ch);
                }
            }
            Err(ServiceOpenError::NoChannel)
        })
    }

    pub fn free(&self, ch: u8) {
        self.state.lock(|s| {
            s.borrow_mut().handlers.remove(&ch);
        });
    }

    /// Forget every channel (after a link reset).
    pub fn reset(&self) {
        self.state.lock(|s| {
            let mut s = s.borrow_mut();
            s.handlers.clear();
            s.next = 2;
        });
    }

    pub fn add_local_service(&self, name: &str, factory: LocalFactory) {
        self.state.lock(|s| {
            s.borrow_mut().local.insert(String::from(name), factory);
        });
    }

    pub fn add_obsolete_service(&self, name: &str) {
        self.state
            .lock(|s| s.borrow_mut().obsolete.push(String::from(name)));
    }

    // -- traffic --------------------------------------------------------------

    fn packet(ch: u8, data: &[u8]) -> Vec<u8> {
        let mut p = Vec::with_capacity(data.len() + 1);
        p.push(ch);
        p.extend_from_slice(data);
        p
    }

    pub async fn send(&self, ch: u8, data: &[u8]) -> Result<(), LinkError> {
        self.link.send(Self::packet(ch, data)).await
    }

    pub fn try_send(&self, ch: u8, data: &[u8]) -> Result<(), LinkError> {
        self.link.try_send(Self::packet(ch, data))
    }

    /// Tell the peer to close its end of a service channel.
    pub async fn close_remote(&self, remote_ch: u8) -> Result<(), LinkError> {
        self.send(0, &[remote_ch]).await
    }

    fn on_control(&self, payload: &[u8]) {
        if payload.len() == 1 {
            let ch = payload[0];
            let h = self.state.lock(|s| s.borrow_mut().handlers.remove(&ch));
            debug!("mux: peer closed channel {}", ch);
            if let Some(h) = h {
                h.on_close(self);
            }
            return;
        }
        if payload.len() < 2 {
            return;
        }
        let reply_ch = payload[0];
        let name: String = payload[1..].iter().map(|&b| b as char).collect();
        let (obsolete, factory) = self.state.lock(|s| {
            let s = s.borrow();
            (s.obsolete.contains(&name), s.local.get(&name).cloned())
        });
        let mut status = STATUS_UNKNOWN;
        let mut ch = 0u8;
        if obsolete {
            status = STATUS_OBSOLETE;
        } else if let Some(factory) = factory {
            status = STATUS_FAILED;
            if let Some(handler) = factory(self, reply_ch)
                && let Ok(c) = self.allocate(handler)
            {
                ch = c;
                status = STATUS_OK;
            }
        }
        info!(
            "mux: peer opened local service {:?} -> status {} chan {}",
            name.as_str(),
            status,
            ch
        );
        let _ = self.try_send(reply_ch, &[status, ch]);
    }

    // -- opening services on the peer ----------------------------------------

    /// Open `name` on the peer and return the peer's service channel.
    pub async fn open_service(
        &self,
        name: &str,
        timeout: Duration,
    ) -> Result<u8, ServiceOpenError> {
        let q = Arc::new(PacketQueue::new());
        let rch = self.allocate(q.clone())?;
        let mut req = Vec::with_capacity(name.len() + 1);
        req.push(rch);
        req.extend(name.bytes());
        let result = async {
            self.send(0, &req).await?;
            match with_timeout(timeout, q.receive()).await {
                Ok(Some(p)) if p.len() == 2 => match p[0] {
                    STATUS_OK => Ok(p[1]),
                    STATUS_OBSOLETE => Err(ServiceOpenError::Obsolete),
                    STATUS_FAILED => Err(ServiceOpenError::Failed),
                    STATUS_UNKNOWN => Err(ServiceOpenError::Unknown),
                    s => Err(ServiceOpenError::Status(s)),
                },
                _ => Err(ServiceOpenError::Timeout),
            }
        }
        .await;
        self.free(rch);
        if let Ok(ch) = result {
            info!("mux: opened {:?} on peer channel {}", name, ch);
        }
        result
    }
}

impl PacketSink for Mux {
    fn on_packet(&self, data: &[u8]) {
        let Some((&ch, payload)) = data.split_first() else {
            return;
        };
        if ch == 0 {
            let handled = self.state.lock(|s| s.borrow().handlers.contains_key(&0));
            if !handled {
                self.on_control(payload);
                return;
            }
        }
        let h = self.state.lock(|s| s.borrow().handlers.get(&ch).cloned());
        match h {
            Some(h) => h.on_packet(self, payload),
            None => debug!("mux: data for unknown channel {} [{}]", ch, payload.len()),
        }
    }
}

/// A [`Handler`] that queues packets for an async reader. Packets beyond the
/// queue depth are dropped and counted.
pub struct PacketQueue {
    ch: Channel<CS, Vec<u8>, 32>,
    closed: AtomicBool,
    closed_sig: Signal<CS, ()>,
    pub dropped: AtomicU32,
}

impl Default for PacketQueue {
    fn default() -> Self {
        Self::new()
    }
}

impl PacketQueue {
    pub const fn new() -> Self {
        Self {
            ch: Channel::new(),
            closed: AtomicBool::new(false),
            closed_sig: Signal::new(),
            dropped: AtomicU32::new(0),
        }
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire)
    }

    /// Mark closed and wake the reader (used when the link is lost).
    pub fn close(&self) {
        self.closed.store(true, Ordering::Release);
        self.closed_sig.signal(());
    }

    /// Next packet, or `None` once the channel is closed and drained.
    pub async fn receive(&self) -> Option<Vec<u8>> {
        loop {
            if let Ok(p) = self.ch.try_receive() {
                return Some(p);
            }
            if self.is_closed() {
                return None;
            }
            match select(self.ch.receive(), self.closed_sig.wait()).await {
                Either::First(p) => return Some(p),
                Either::Second(()) => continue,
            }
        }
    }
}

impl Handler for PacketQueue {
    fn on_packet(&self, _mux: &Mux, payload: &[u8]) {
        if self.ch.try_send(payload.to_vec()).is_err() {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
    }

    fn on_close(&self, _mux: &Mux) {
        self.close();
    }
}

/// A machine service we opened, with our own receive channel.
pub struct ServiceClient {
    mux: &'static Mux,
    pub name: String,
    /// Our channel (the handler's).
    pub local: u8,
    /// The peer's service channel.
    pub remote: u8,
    queue: Option<Arc<PacketQueue>>,
    closed: AtomicBool,
}

impl ServiceClient {
    /// Open `name` with a [`PacketQueue`] as the receive handler.
    pub async fn open(
        mux: &'static Mux,
        name: &str,
        timeout: Duration,
    ) -> Result<Self, ServiceOpenError> {
        let q = Arc::new(PacketQueue::new());
        let mut s = Self::open_with(mux, name, q.clone(), timeout).await?;
        s.queue = Some(q);
        Ok(s)
    }

    /// Open `name` with a custom receive handler.
    pub async fn open_with(
        mux: &'static Mux,
        name: &str,
        handler: Arc<dyn Handler>,
        timeout: Duration,
    ) -> Result<Self, ServiceOpenError> {
        let local = mux.allocate(handler)?;
        match mux.open_service(name, timeout).await {
            Ok(remote) => Ok(Self {
                mux,
                name: String::from(name),
                local,
                remote,
                queue: None,
                closed: AtomicBool::new(false),
            }),
            Err(e) => {
                mux.free(local);
                Err(e)
            }
        }
    }

    pub fn mux(&self) -> &'static Mux {
        self.mux
    }

    /// Send our receive channel number to the service.
    pub async fn subscribe(&self) -> Result<(), LinkError> {
        self.send(&[self.local]).await
    }

    pub async fn send(&self, data: &[u8]) -> Result<(), LinkError> {
        self.mux.send(self.remote, data).await
    }

    pub fn try_send(&self, data: &[u8]) -> Result<(), LinkError> {
        self.mux.try_send(self.remote, data)
    }

    /// The receive queue (only for clients made with [`ServiceClient::open`]).
    pub fn queue(&self) -> Option<&Arc<PacketQueue>> {
        self.queue.as_ref()
    }

    /// Next reply, `Ok(None)` if the peer closed the channel, `Err` on timeout.
    pub async fn recv(&self, timeout: Duration) -> Result<Option<Vec<u8>>, ServiceOpenError> {
        let q = self
            .queue
            .as_ref()
            .expect("recv on a client without a queue");
        with_timeout(timeout, q.receive())
            .await
            .map_err(|_| ServiceOpenError::Timeout)
    }

    pub fn is_closed(&self) -> bool {
        self.closed.load(Ordering::Acquire) || self.queue.as_ref().is_some_and(|q| q.is_closed())
    }

    /// Close the peer's end and free our channel.
    pub async fn close(&self) {
        if !self.closed.swap(true, Ordering::AcqRel) {
            let _ = self.mux.close_remote(self.remote).await;
        }
        self.mux.free(self.local);
    }

    /// Free our channel without talking to the peer (after a link loss).
    pub fn abandon(&self) {
        self.closed.store(true, Ordering::Release);
        self.mux.free(self.local);
    }
}

/// Convenience: box a closure into a [`LocalFactory`] whose channels use the
/// given handler constructor.
pub fn local_service<F>(f: F) -> LocalFactory
where
    F: Fn(&Mux, u8) -> Option<Arc<dyn Handler>> + Send + Sync + 'static,
{
    Arc::new(f)
}

/// Helper for `Box<dyn Handler>`-style construction sites.
pub fn handler<H: Handler + 'static>(h: H) -> Arc<dyn Handler> {
    Arc::new(h)
}

#[allow(dead_code)]
fn _assert_sync() {
    fn check<T: Sync>() {}
    check::<Mux>();
    check::<Box<dyn Handler>>();
}
