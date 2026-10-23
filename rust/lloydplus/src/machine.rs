//! High-level connection to a Plus-series frame: [`LloydPlus`].
//!
//! ```ignore
//! let m = LloydPlus::new(Some(Duration::from_secs(2)), 1);
//! join(m.run(rx, tx), async {
//!     m.connect(ConnectOptions::default()).await?;
//!     let c = m.control().unwrap();
//!     c.take_control(Protection::limits(100.0, 100.0), Duration::from_secs(10)).await?;
//!     c.drive(Drive::to_extension(10.0, 50.0 / 60.0), None, None).await?;
//!     m.close().await;
//! }).await;
//! ```

use alloc::{boxed::Box, string::String, sync::Arc, vec::Vec};
use core::{
    cell::RefCell,
    future::pending,
    sync::atomic::{AtomicBool, AtomicU32, Ordering},
};

use embassy_futures::{
    join::{join, join3},
    select::{Either, Either3, select, select3},
};
use embassy_sync::{
    blocking_mutex::{Mutex, raw::CriticalSectionRawMutex},
    channel::Channel,
    signal::Signal,
};
use embassy_time::{Duration, Timer, with_timeout};
use embedded_io_async::{Read, Write};

use crate::channels::{Mux, ServiceClient};
use crate::fmt::{error, info, warn};
use crate::link::{Link, LinkConfig};
use crate::services::{
    self as svc, DataSetStream, DataSourceInfo, MachineMessage, MachineParameters, MessageQueue,
    Sample, SampleSink, ServiceError, UtcTime,
};
use crate::stages::{Notify, StageControl};

type CS = CriticalSectionRawMutex;

/// The port speed of the PC link.
pub const BAUDRATE: u32 = 57600;

#[derive(Debug, Clone, Copy)]
pub struct ConnectOptions {
    /// How long to wait for the boot handshake.
    pub handshake_timeout: Duration,
    /// Per-service request timeout.
    pub service_timeout: Duration,
    /// Subscribe to `DataSet` readings.
    pub stream: bool,
    /// Set the machine clock from these UTC fields.
    pub clock: Option<UtcTime>,
    /// Open the stage arena and factories ([`LloydPlus::control`]).
    pub control: bool,
}

impl Default for ConnectOptions {
    fn default() -> Self {
        Self {
            handshake_timeout: Duration::from_secs(10),
            service_timeout: svc::TIMEOUT,
            stream: true,
            clock: None,
            control: true,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConnectError {
    /// No link handshake: check the lead (RX/TX and RTS/CTS crossed), 57600
    /// baud, machine idle, NEXYGEN not running.
    Handshake,
    Service(ServiceError),
}

impl From<ServiceError> for ConnectError {
    fn from(e: ServiceError) -> Self {
        ConnectError::Service(e)
    }
}

impl core::fmt::Display for ConnectError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            ConnectError::Handshake => f.write_str(
                "no link handshake from the machine (check lead: RX/TX and RTS/CTS crossed, \
                 57600 baud, machine idle, NEXYGEN not running)",
            ),
            ConnectError::Service(e) => write!(f, "{e}"),
        }
    }
}

#[derive(Default)]
struct Info {
    version: Option<(u8, u8, u8)>,
    description: Option<[String; 3]>,
    parameters: Option<MachineParameters>,
    sources: Vec<DataSourceInfo>,
    latest: [Option<Sample>; 8],
}

type Slot<T> = Mutex<CS, RefCell<Option<T>>>;

/// Everything opened by one `connect`.
struct Session {
    responder: ServiceClient,
    /// NotImplementedDataSources, KeypadEZ (whichever opened).
    held: Vec<ServiceClient>,
    status: Slot<ServiceClient>,
    stream: Slot<Arc<DataSetStream>>,
    control: Slot<Arc<StageControl>>,
    /// The controller is in place: start its timer loop.
    control_ready: Signal<CS, ()>,
    closing: Signal<CS, ()>,
}

impl Session {
    fn control(&self) -> Option<Arc<StageControl>> {
        self.control.lock(|c| c.borrow().clone())
    }
}

/// Connect, read machine information, stream readings and control tests.
///
/// One instance per port, created with [`LloydPlus::new`] (which leaks it to
/// `'static`, the lifetime the shared protocol state needs). Poll
/// [`LloydPlus::run`] for the lifetime of the port; call everything else from
/// any other task.
pub struct LloydPlus {
    pub link: &'static Link,
    pub mux: &'static Mux,
    /// Messages from the machine (local Error service, StatusInformation),
    /// before the stage controller has acted on them.
    internal: Arc<MessageQueue>,
    /// Messages from the machine, for the application.
    pub messages: MessageQueue,
    samples: Channel<CS, Sample, 1024>,
    sample_drops: AtomicU32,
    /// An extra consumer of every sample (a recorder), called from the
    /// link's receive context before the sample is queued.
    tap: Mutex<CS, RefCell<Option<Arc<dyn SampleSink>>>>,
    info: Mutex<CS, RefCell<Info>>,
    session: Slot<Arc<Session>>,
    session_started: Signal<CS, ()>,
    lost: AtomicBool,
    /// Bumped when the connection is lost.
    pub notify: Arc<Notify>,
    watchdog_interval: Option<Duration>,
    extension_source: u8,
}

struct Sink(&'static LloydPlus);

impl SampleSink for Sink {
    fn sample(&self, s: Sample) {
        self.0.info.lock(|i| {
            if let Some(slot) = i.borrow_mut().latest.get_mut(s.source as usize) {
                *slot = Some(s);
            }
        });
        let tap = self.0.tap.lock(|t| t.borrow().clone());
        if let Some(t) = tap {
            t.sample(s);
        }
        if self.0.samples.try_send(s).is_err() {
            self.0.sample_drops.fetch_add(1, Ordering::Relaxed);
        }
    }
}

impl LloydPlus {
    /// `watchdog_interval`: ping period (the machine drops a silent PC after
    /// about 4 s; the default of 2 s is what the LF Plus was verified with).
    /// `extension_source`: data source index of the crosshead extension (1).
    pub fn new(watchdog_interval: Option<Duration>, extension_source: u8) -> &'static Self {
        Self::with_link(LinkConfig::default(), watchdog_interval, extension_source)
    }

    pub fn with_link(
        cfg: LinkConfig,
        watchdog_interval: Option<Duration>,
        extension_source: u8,
    ) -> &'static Self {
        let link: &'static Link = Box::leak(Box::new(Link::new(cfg)));
        let mux: &'static Mux = Box::leak(Box::new(Mux::new(link)));
        let internal = Arc::new(MessageQueue::new());
        svc::install_local_services(mux, internal.clone());
        Box::leak(Box::new(Self {
            link,
            mux,
            internal,
            messages: MessageQueue::new(),
            samples: Channel::new(),
            sample_drops: AtomicU32::new(0),
            tap: Mutex::new(RefCell::new(None)),
            info: Mutex::new(RefCell::new(Info::default())),
            session: Mutex::new(RefCell::new(None)),
            session_started: Signal::new(),
            lost: AtomicBool::new(false),
            notify: Arc::new(Notify::new()),
            watchdog_interval,
            extension_source,
        }))
    }

    // -- plumbing ---------------------------------------------------------------

    /// Drive the port and the background duties (link, machine messages,
    /// watchdog, hold timers). Never returns.
    pub async fn run<R: Read, W: Write>(&'static self, rx: R, tx: W) {
        join3(
            self.link.run(rx, tx, self.mux),
            self.pump(),
            self.supervise(),
        )
        .await;
    }

    /// Link events and machine messages.
    async fn pump(&'static self) {
        let events = async {
            loop {
                // latched, so a reset is never missed (the mux itself is
                // reset synchronously by the link)
                self.link.wait_reset().await;
                error!("machine restarted the link; connection lost");
                self.on_lost();
            }
        };
        let messages = async {
            loop {
                let m = match select(self.internal.wait_trip(), self.internal.receive()).await {
                    Either::First(m) | Either::Second(m) => m,
                };
                if let Some(c) = self.current_session().and_then(|s| s.control()) {
                    c.on_machine_message(&m).await;
                }
                self.messages.push_user(m);
            }
        };
        join(events, messages).await;
    }

    /// Per-session duties: the watchdog and the stage controller's timers.
    async fn supervise(&'static self) {
        loop {
            self.session_started.wait().await;
            let Some(sess) = self.current_session() else {
                continue;
            };
            let watchdog = async {
                match self.watchdog_interval {
                    Some(i) => {
                        svc::watchdog(self.mux, &sess.responder, i).await;
                        self.on_lost();
                    }
                    None => pending::<()>().await,
                }
            };
            let control = async {
                sess.control_ready.wait().await;
                match sess.control() {
                    Some(c) => c.run().await,
                    None => pending::<()>().await,
                }
            };
            match select3(watchdog, control, sess.closing.wait()).await {
                Either3::First(()) | Either3::Second(()) => sess.closing.wait().await,
                Either3::Third(()) => {}
            }
        }
    }

    fn current_session(&self) -> Option<Arc<Session>> {
        self.session.lock(|s| s.borrow().clone())
    }

    fn on_lost(&self) {
        if self.lost.swap(true, Ordering::AcqRel) {
            return;
        }
        self.mux.reset();
        if let Some(sess) = self.current_session() {
            if let Some(c) = sess.control() {
                c.on_lost();
            }
            if let Some(st) = sess.stream.lock(|s| s.borrow_mut().take()) {
                st.abandon();
            }
            sess.closing.signal(());
        }
        self.notify.bump();
    }

    pub fn is_lost(&self) -> bool {
        self.lost.load(Ordering::Acquire)
    }

    /// Wait until the connection is lost.
    pub async fn wait_lost(&self) {
        self.notify.wait_until(None, || self.is_lost()).await;
    }

    // -- connection ---------------------------------------------------------------

    /// Bring up the link and the services, in the console's order
    /// (CNextStepMachineVisitor): Responder + NotImplementedDataSources,
    /// KeypadEZ, SetSystemTime, watchdog, Description, Version,
    /// StatusInformation, TensileParameters, DataInformation → DataSet, then
    /// the stage arena and factories ([`LloydPlus::control`]).
    ///
    /// Setting up test control installs stages but moves nothing. If the
    /// machine lacks the stage services, `control()` stays `None`.
    pub async fn connect(&'static self, opts: ConnectOptions) -> Result<(), ConnectError> {
        if with_timeout(opts.handshake_timeout, self.link.wait_connected())
            .await
            .is_err()
        {
            return Err(ConnectError::Handshake);
        }
        self.lost.store(false, Ordering::Release);
        let to = opts.service_timeout;
        let m = self.mux;
        let responder = ServiceClient::open(m, "Responder", to)
            .await
            .map_err(ServiceError::from)?;
        let mut held = Vec::new();
        for name in ["NotImplementedDataSources", "KeypadEZ"] {
            match ServiceClient::open(m, name, to).await {
                Ok(s) => held.push(s),
                Err(e) => warn!("could not open {}: {}", name, crate::fmt::Debug2Format(&e)),
            }
        }
        if let Some(t) = &opts.clock
            && let Err(e) = svc::set_system_time(m, t, to).await
        {
            warn!("SetSystemTime: {}", crate::fmt::Debug2Format(&e));
        }
        // The session exists from here, so the watchdog starts now.
        let sess = Arc::new(Session {
            responder,
            held,
            status: Mutex::new(RefCell::new(None)),
            stream: Mutex::new(RefCell::new(None)),
            control: Mutex::new(RefCell::new(None)),
            control_ready: Signal::new(),
            closing: Signal::new(),
        });
        self.session.lock(|s| *s.borrow_mut() = Some(sess.clone()));
        self.session_started.signal(());

        let description = svc::query_description(m, to).await?;
        let version = svc::query_version(m, to).await?;
        let status = svc::open_status_monitor(m, self.internal.clone(), to).await?;
        sess.status.lock(|s| *s.borrow_mut() = Some(status));
        let parameters = svc::query_parameters(m, to).await?;
        let sources = svc::query_data_information(m, to).await?;
        info!(
            "machine: {} {}.{}.{}",
            description[2].as_str(),
            version.0,
            version.1,
            version.2
        );
        self.info.lock(|i| {
            let mut i = i.borrow_mut();
            i.description = Some(description);
            i.version = Some(version);
            i.parameters = Some(parameters);
            i.sources = sources;
            i.latest = Default::default();
        });
        if opts.stream {
            self.start_stream(to).await?;
        }
        if opts.control {
            let (min_speed, max_speed) = self
                .parameters()
                .map(|p| (p.min_speed, p.max_speed))
                .unwrap_or((f64::NAN, f64::NAN));
            let idx: Vec<u8> = self.sources().iter().map(|s| s.index).collect();
            match StageControl::open(m, min_speed, max_speed, &idx, self.extension_source, to).await
            {
                Ok(c) => sess.control.lock(|s| *s.borrow_mut() = Some(Arc::new(c))),
                Err(e) => warn!("test control unavailable: {}", crate::fmt::Debug2Format(&e)),
            }
        }
        sess.control_ready.signal(());
        Ok(())
    }

    /// Subscribe to readings (done by `connect` unless `stream: false`).
    pub async fn start_stream(&'static self, timeout: Duration) -> Result<(), ServiceError> {
        let Some(sess) = self.current_session() else {
            return Err(ServiceError::Closed("session"));
        };
        if sess.stream.lock(|s| s.borrow().is_some()) {
            return Ok(());
        }
        let sources = self.sources();
        let st = DataSetStream::open(self.mux, &sources, Arc::new(Sink(self)), timeout).await?;
        sess.stream.lock(|s| *s.borrow_mut() = Some(Arc::new(st)));
        Ok(())
    }

    pub async fn stop_stream(&self) {
        let st = self
            .current_session()
            .and_then(|s| s.stream.lock(|st| st.borrow_mut().take()));
        if let Some(st) = st {
            st.close().await;
        }
    }

    /// Disconnect. If the PC has control, stop and hand control back first.
    pub async fn close(&self) {
        let sess = self.session.lock(|s| s.borrow_mut().take());
        let Some(sess) = sess else {
            return;
        };
        if let Some(c) = sess.control.lock(|c| c.borrow_mut().take()) {
            if c.take_attempted() && !self.is_lost() {
                if let Err(e) = c.stop(Some(Duration::from_secs(2))).await {
                    warn!(
                        "could not stop before closing: {}",
                        crate::fmt::Debug2Format(&e)
                    );
                }
                if let Err(e) = c.release_control(Duration::from_secs(5)).await {
                    warn!(
                        "could not release control before closing: {}",
                        crate::fmt::Debug2Format(&e)
                    );
                }
            }
            c.close().await;
        }
        sess.closing.signal(());
        if !self.is_lost() {
            if let Some(st) = sess.stream.lock(|s| s.borrow_mut().take()) {
                st.close().await;
            }
            if let Some(s) = sess.status.lock(|s| s.borrow_mut().take()) {
                s.close().await;
            }
            for s in &sess.held {
                s.close().await;
            }
            sess.responder.close().await;
            // give the link a moment to flush the close requests
            Timer::after_millis(200).await;
        }
    }

    // -- information --------------------------------------------------------------

    pub fn version(&self) -> Option<(u8, u8, u8)> {
        self.info.lock(|i| i.borrow().version)
    }

    pub fn description(&self) -> Option<[String; 3]> {
        self.info.lock(|i| i.borrow().description.clone())
    }

    pub fn parameters(&self) -> Option<MachineParameters> {
        self.info.lock(|i| i.borrow().parameters.clone())
    }

    pub fn sources(&self) -> Vec<DataSourceInfo> {
        self.info.lock(|i| i.borrow().sources.clone())
    }

    pub fn source_index(&self, kind: u8) -> Option<u8> {
        self.sources()
            .iter()
            .find(|s| s.kind == kind)
            .map(|s| s.index)
    }

    /// The stage controller, once `connect` has set it up.
    pub fn control(&self) -> Option<Arc<StageControl>> {
        self.current_session().and_then(|s| s.control())
    }

    // -- readings -----------------------------------------------------------------

    pub fn latest(&self, source: u8) -> Option<Sample> {
        self.info
            .lock(|i| i.borrow().latest.get(source as usize).copied().flatten())
    }

    /// Latest load reading, N.
    pub fn load(&self) -> Option<f64> {
        self.latest(0).map(|s| s.value)
    }

    /// Latest crosshead extension, mm.
    pub fn extension(&self) -> Option<f64> {
        self.latest(1).map(|s| s.value)
    }

    /// Next sample from the stream (all sources, in arrival order). The queue
    /// holds 1024 samples; the number dropped since start is
    /// [`LloydPlus::sample_drops`].
    pub async fn next_sample(&self) -> Sample {
        self.samples.receive().await
    }

    pub fn try_sample(&self) -> Option<Sample> {
        self.samples.try_receive().ok()
    }

    pub fn sample_drops(&self) -> u32 {
        self.sample_drops.load(Ordering::Relaxed)
    }

    /// Install (or remove) a sink that sees every sample as it arrives, in
    /// addition to the queue behind [`LloydPlus::next_sample`].
    pub fn set_sample_tap(&self, tap: Option<Arc<dyn SampleSink>>) {
        self.tap.lock(|t| *t.borrow_mut() = tap);
    }

    /// Next machine message (status, error or warning).
    pub async fn next_message(&self) -> MachineMessage {
        self.messages.receive().await
    }

    /// Send a raw byte to the "KeypadEZ" service. The console exposes codes
    /// 0..4 only from a hidden debug menu; their meaning is unknown.
    pub async fn press_key(&self, code: u8) -> Result<(), ServiceError> {
        let Some(sess) = self.current_session() else {
            return Err(ServiceError::Closed("session"));
        };
        match sess.held.iter().find(|s| s.name == "KeypadEZ") {
            Some(k) => k.send(&[code]).await.map_err(Into::into),
            None => Err(ServiceError::Closed("KeypadEZ")),
        }
    }
}
