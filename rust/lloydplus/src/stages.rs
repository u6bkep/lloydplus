//! Test control: the stage arena and the stage factories (PROTOCOL.md §11).
//!
//! The machine runs "stages" out of a 32-slot arena. Each stage kind has its
//! own factory service; the PC creates a stage by sending `[slot, params...]`
//! to it. `StageArenaReference5` holds two tables: `next[i]` (where to go when
//! the stage in slot i completes) and `active[i]` (a redirect out of slot i,
//! applied when written, even when `active[i] == i`, which re-enters slot i).
//! The machine reports every slot change. Verified on an LF Plus (firmware
//! 3.0.94) on 2026-10-22: connect, take and release control, drive segments,
//! holds, stop, zero, return and grip protection all behaved as described here.
//!
//! [`StageControl`] reproduces the slot layout and command sequences of the
//! NEXYGEN 4.1 console (`CMachineStageWrapper`, 0x4256f0..0x4289d0).
//!
//! Units on the wire are N, mm, mm/s, N/s and N/mm (PROTOCOL.md §10).

use alloc::{
    collections::{BTreeMap, VecDeque},
    format,
    string::String,
    sync::Arc,
    vec::Vec,
};
use core::cell::RefCell;

use embassy_futures::select::{Either, select};
use embassy_sync::{
    blocking_mutex::{Mutex, raw::CriticalSectionRawMutex},
    watch::Watch,
};
use embassy_time::{Duration, Instant, Timer};

use crate::channels::{Handler, Mux, ServiceClient, ServiceOpenError};
use crate::fmt::{debug, info, warn};
use crate::link::LinkError;
use crate::services::{MachineMessage, SAFETY_LIMIT_CODE};

type CS = CriticalSectionRawMutex;

pub const ARENA_SERVICE: &str = "StageArenaReference5";
pub const ARENA_SLOTS: usize = 32;

// Slot layout used by the console (PROTOCOL.md §11.4)
pub const SLOT_PARKED: u8 = 0;
pub const SLOT_WAIT_EXCLUSIVE: u8 = 1;
pub const SLOT_WAIT_SHARED: u8 = 2;
pub const SLOT_GAIN_EXCLUSIVE: u8 = 3;
pub const SLOT_GRIP_PROTECTION: u8 = 4;
pub const SLOT_STIFFNESS: u8 = 5;
pub const SLOT_DATASOURCE_RANGE: u8 = 6;
pub const SLOT_READY: u8 = 7;
pub const SLOT_RELEASE_EXCLUSIVE: u8 = 8;
pub const SLOT_ZERO: u8 = 9;
pub const SLOT_RETURN: u8 = 10;
pub const SLOT_RETURN_RELEASE: u8 = 11;
pub const SLOT_STOP_A: u8 = 15;
pub const SLOT_STOP_B: u8 = 19;
/// Segment bases, in the order the console uses them.
pub const BANKS: [u8; 2] = [16, 12];

pub const NO_SOURCE: u8 = 0xFF;

// Factory service names
pub const DO_NOTHING: &str = "DoNothingStageFactory";
pub const WAIT_SHARED: &str = "WaitSharedStageFactory";
pub const WAIT_EXCLUSIVE: &str = "WaitExclusiveStageFactory";
pub const GAIN_EXCLUSIVE: &str = "GainExclusiveStageFactory";
pub const RELEASE_EXCLUSIVE: &str = "ReleaseExclusiveStageFactory";
pub const CROSSHEAD_RATE: &str = "CrossheadRateStageFactory";
pub const CROSSHEAD_POSITIONING: &str = "CrossheadPositioningStageFactory";
pub const DRIVE_NOWHERE: &str = "DriveNowhereStageFactory";
pub const ZERO_READINGS: &str = "ZeroReadingsStageFactory";
pub const SET_DATASOURCE_RANGE: &str = "SetDatasourceRangeStageFactory";
pub const GRIP_PROTECTION: &str = "GripProtectionStageFactory";
pub const LOAD_RATE: &str = "LoadRateStageFactory";
pub const TRUE_STRAIN_RATE: &str = "TrueStrainRateStageFactory";
pub const SET_MANUAL_STIFFNESS: &str = "SetManualSampleStiffnessStageFactory";
pub const SET_AUTO_STIFFNESS: &str = "SetAutoSampleStiffnessStageFactory";

/// Open order of CMachineStageWrapper_v63 (0x4259b0). A failure in the first
/// group is fatal for the console; the second group is optional equipment.
pub const MANDATORY_FACTORIES: [&str; 11] = [
    DO_NOTHING,
    WAIT_SHARED,
    WAIT_EXCLUSIVE,
    GAIN_EXCLUSIVE,
    RELEASE_EXCLUSIVE,
    CROSSHEAD_RATE,
    CROSSHEAD_POSITIONING,
    DRIVE_NOWHERE,
    ZERO_READINGS,
    SET_DATASOURCE_RANGE,
    GRIP_PROTECTION,
];
pub const OPTIONAL_FACTORIES: [&str; 4] = [
    LOAD_RATE,
    TRUE_STRAIN_RATE,
    SET_MANUAL_STIFFNESS,
    SET_AUTO_STIFFNESS,
];
/// Factories whose stages are installed once at connect, then closed (0x4265cd).
pub const PERMANENT_FACTORIES: [&str; 8] = [
    DO_NOTHING,
    WAIT_EXCLUSIVE,
    WAIT_SHARED,
    GAIN_EXCLUSIVE,
    RELEASE_EXCLUSIVE,
    DRIVE_NOWHERE,
    ZERO_READINGS,
    CROSSHEAD_POSITIONING,
];

// Table writes copied from the console
/// 0x426573
pub const CONNECT_NEXT: [u8; 12] = [0, 2, 1, 4, 5, 6, 7, 7, 1, 7, 7, 8];
/// 0x426b20
pub const RELEASE_ACTIVE: [u8; 32] = [
    0, 1, 2, 1, 8, 8, 8, 8, 8, 8, 11, 11, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8,
    7,
];
/// 0x427580
pub const STOP_ACTIVE: [u8; 32] = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 7, 8, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
];
/// lloydplus addition (PROTOCOL.md §11.9): every motion slot → its bank's
/// DriveNowhere stage (15 or 19). Sent before [`STOP_ACTIVE`]. Both writes
/// stop a drive on hardware (16 → 19 → 7, or 16 → 7 for `STOP_ACTIVE` alone);
/// this one goes through a stage whose job is to stop, so it is kept first.
pub const HALT_ACTIVE: [u8; 32] = [
    0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 15, 15, 15, 15, 15, 15, 19, 19, 19, 19, 7, 7, 7, 7, 7, 7, 7, 7,
    7, 7, 7, 7,
];

/// 0x4280e0 with the bank base filled in.
pub fn segment_active(base: u8) -> [u8; 32] {
    let b = base;
    [
        0, 1, 2, 3, 4, 5, 6, b, 8, 9, b, b, b, b, b, b, b, b, b, b, 7, 7, 7, 7, 7, 7, 7, 7, 7, 7,
        7, 7,
    ]
}

// --------------------------------------------------------------------------
// Errors
// --------------------------------------------------------------------------

#[derive(Debug, Clone, PartialEq)]
pub enum StageError {
    /// The machine must be ready (slot 7) for this.
    NotReady {
        what: &'static str,
        slot: u8,
    },
    /// Starting a segment needs control (ready, returning, or a segment running).
    NotInControl {
        slot: u8,
    },
    /// The machine did not report the expected transition in time.
    Timeout {
        what: &'static str,
        slot: u8,
    },
    /// The connection went away while waiting.
    Lost {
        what: &'static str,
    },
    /// GainExclusive failed (the machine fell into slot 0).
    ControlRefused,
    /// A needed optional stage factory is not fitted.
    NotFitted(&'static str),
    /// The speed would be clamped to zero by the console's own rule.
    SpeedTooLow {
        speed: f64,
        min_speed: f64,
    },
    /// A value could not be encoded (non-finite, or a negative speed/hold).
    BadValue(&'static str),
    Open(ServiceOpenError),
    Link(LinkError),
}

impl From<ServiceOpenError> for StageError {
    fn from(e: ServiceOpenError) -> Self {
        StageError::Open(e)
    }
}
impl From<LinkError> for StageError {
    fn from(e: LinkError) -> Self {
        StageError::Link(e)
    }
}

impl core::fmt::Display for StageError {
    fn fmt(&self, f: &mut core::fmt::Formatter<'_>) -> core::fmt::Result {
        match self {
            StageError::NotReady { what, slot } => write!(
                f,
                "cannot {what}: machine is in slot {slot}, not ready (7); take control and stop first"
            ),
            StageError::NotInControl { slot } => {
                write!(
                    f,
                    "cannot start: machine is in slot {slot}; take control first"
                )
            }
            StageError::Timeout { what, slot } => {
                write!(f, "timed out {what} (machine in slot {slot})")
            }
            StageError::Lost { what } => write!(f, "{what}: connection to the machine lost"),
            StageError::ControlRefused => f.write_str("exclusive control could not be gained"),
            StageError::NotFitted(name) => write!(f, "{name} is not fitted"),
            StageError::SpeedTooLow { speed, min_speed } => write!(
                f,
                "speed {speed} mm/s is below the machine's minimum ({min_speed} mm/s)"
            ),
            StageError::BadValue(what) => write!(f, "bad value: {what}"),
            StageError::Open(e) => write!(f, "could not open stage service: {e}"),
            StageError::Link(e) => write!(f, "{e}"),
        }
    }
}

// --------------------------------------------------------------------------
// Packet encoders
// --------------------------------------------------------------------------

/// `x` as MSVC's `printf("%e")`: six decimals, three-digit exponent
/// (`5.000000e+001`). `None` for a non-finite value.
pub fn fmt_e(x: f64) -> Option<String> {
    if !x.is_finite() {
        return None;
    }
    let s = format!("{x:.6e}");
    let (mant, exp) = s.split_once('e')?;
    let e: i32 = exp.parse().ok()?;
    Some(format!(
        "{mant}e{}{:03}",
        if e < 0 { '-' } else { '+' },
        e.abs()
    ))
}

fn e(x: f64) -> Result<String, StageError> {
    fmt_e(x).ok_or(StageError::BadValue("non-finite number"))
}

/// CrossheadRate / CrossheadPositioning / LoadRate / TrueStrainRate stage
/// (0x4282f0, `%c%e%c%c%e%c%c%e%c`).
pub fn motion_stage(
    slot: u8,
    rate: f64,
    limit_src: u8,
    limit: f64,
    strain_src: u8,
    gauge: f64,
) -> Result<Vec<u8>, StageError> {
    let mut p = Vec::with_capacity(48);
    p.push(slot);
    p.extend(e(rate)?.bytes());
    p.push(0);
    p.push(limit_src);
    p.extend(e(limit)?.bytes());
    p.push(0);
    p.push(strain_src);
    p.extend(e(gauge)?.bytes());
    p.push(0);
    Ok(p)
}

/// `%c%e%c-%e%c` (0x426bd0): tensile and compressive load limits, N.
pub fn grip_protection_stage(
    slot: u8,
    tensile: f64,
    compressive: f64,
) -> Result<Vec<u8>, StageError> {
    let mut p = Vec::with_capacity(32);
    p.push(slot);
    p.extend(e(tensile)?.bytes());
    p.extend_from_slice(b"\0-");
    p.extend(e(compressive)?.bytes());
    p.push(0);
    Ok(p)
}

/// `%c%e%c` (0x426d00): sample stiffness, N/mm.
pub fn manual_stiffness_stage(slot: u8, stiffness: f64) -> Result<Vec<u8>, StageError> {
    let mut p = Vec::with_capacity(16);
    p.push(slot);
    p.extend(e(stiffness)?.bytes());
    p.push(0);
    Ok(p)
}

/// `%c%c%e%c` (0x426d00): range of extension source `source`, mm.
pub fn datasource_range_stage(slot: u8, source: u8, value: f64) -> Result<Vec<u8>, StageError> {
    let mut p = Vec::with_capacity(17);
    p.push(slot);
    p.push(source);
    p.extend(e(value)?.bytes());
    p.push(0);
    Ok(p)
}

/// `[slot, m0..mK]` with 0xFF for each source to zero (0x426020).
pub fn zero_readings_stage(slot: u8, sources: &[u8]) -> Vec<u8> {
    let mut p = Vec::new();
    p.push(slot);
    if let Some(&max) = sources.iter().max() {
        p.resize(2 + max as usize, 0);
        for &i in sources {
            p[1 + i as usize] = 0xFF;
        }
    }
    p
}

/// The console's speed limiting (0x428790), mm/s: above max → max; above min
/// → unchanged; above min/10 → min; otherwise 0.
pub fn clamp_speed(speed: f64, min_speed: f64, max_speed: f64) -> f64 {
    if speed > max_speed {
        max_speed
    } else if speed > min_speed {
        speed
    } else if speed > min_speed / 10.0 {
        min_speed
    } else {
        0.0
    }
}

// --------------------------------------------------------------------------
// Change notification shared by every waiter
// --------------------------------------------------------------------------

/// "Something changed": a counter every waiter can block on. Waiters re-check
/// their predicate on each change, so a spurious wake is harmless.
pub struct Notify {
    w: Watch<CS, u64, MAX_WAITERS>,
}

/// Concurrent waiters a [`Notify`] can wake directly; further waiters poll.
pub const MAX_WAITERS: usize = 8;

impl Default for Notify {
    fn default() -> Self {
        Self::new()
    }
}

impl Notify {
    pub const fn new() -> Self {
        Self { w: Watch::new() }
    }

    pub fn bump(&self) {
        let v = self.w.try_get().unwrap_or(0);
        self.w.sender().send(v.wrapping_add(1));
    }

    /// A receiver whose first `changed()` returns at once if anything has
    /// ever happened; loop on a predicate. `None` if [`MAX_WAITERS`]
    /// receivers already exist.
    pub fn receiver(&self) -> Option<embassy_sync::watch::Receiver<'_, CS, u64, MAX_WAITERS>> {
        self.w.receiver()
    }

    /// Wait until `pred()` holds, or `timeout` passes (→ false).
    pub async fn wait_until(
        &self,
        timeout: Option<Duration>,
        mut pred: impl FnMut() -> bool,
    ) -> bool {
        let deadline = timeout.map(|t| Instant::now().checked_add(t).unwrap_or(Instant::MAX));
        let mut rx = self.receiver();
        loop {
            if pred() {
                return true;
            }
            if let Some(d) = deadline
                && Instant::now() >= d
            {
                return false;
            }
            // Wait for a change, a deadline, or (without a receiver slot) a
            // short poll interval.
            let wake = async {
                match rx.as_mut() {
                    Some(rx) => {
                        rx.changed().await;
                    }
                    None => Timer::after_millis(10).await,
                }
            };
            match deadline {
                Some(d) => {
                    if let Either::Second(()) = select(wake, Timer::at(d)).await {
                        return pred();
                    }
                }
                None => wake.await,
            }
        }
    }
}

// --------------------------------------------------------------------------
// The arena service
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub struct Transition {
    pub old: u8,
    pub new: u8,
    /// Position in the report sequence (for [`StageArena::mark`]).
    pub seq: u64,
    pub at: Instant,
}

type Listener = Arc<dyn Fn(&Transition) + Send + Sync>;

struct ArenaState {
    current: u8,
    next_seq: u64,
    recent: VecDeque<Transition>,
    out_of_sequence: u32,
    closed: bool,
    listener: Option<Listener>,
}

/// Keep this many recent transitions for waiters to scan.
const RECENT: usize = 256;

struct ArenaHandler {
    st: Mutex<CS, RefCell<ArenaState>>,
    notify: Arc<Notify>,
}

impl ArenaHandler {
    fn lost(&self) {
        self.st.lock(|s| s.borrow_mut().closed = true);
        self.notify.bump();
    }
}

impl Handler for ArenaHandler {
    fn on_packet(&self, _mux: &Mux, p: &[u8]) {
        // 0x40e2a0
        if p.is_empty() {
            return;
        }
        if p[0] == 0 && p.len() == 3 {
            let (t, listener) = self.st.lock(|s| {
                let mut s = s.borrow_mut();
                let t = Transition {
                    old: p[1],
                    new: p[2],
                    seq: s.next_seq,
                    at: Instant::now(),
                };
                s.next_seq += 1;
                if t.old != s.current {
                    s.out_of_sequence += 1;
                    warn!(
                        "stage changed out of sequence: expected {}, got {} -> {}",
                        s.current, t.old, t.new
                    );
                }
                s.current = t.new;
                if s.recent.len() >= RECENT {
                    s.recent.pop_front();
                }
                s.recent.push_back(t);
                (t, s.listener.clone())
            });
            debug!("stage {} -> {}", t.old, t.new);
            if let Some(l) = listener {
                l(&t);
            }
            self.notify.bump();
        } else if p[0] == 1 {
            warn!("machine closed the stage notification channel");
            self.lost();
        }
    }

    fn on_close(&self, _mux: &Mux) {
        self.lost();
    }
}

/// `StageArenaReference5`: table writes out, slot transitions in.
pub struct StageArena {
    pub client: ServiceClient,
    h: Arc<ArenaHandler>,
    notify: Arc<Notify>,
}

impl StageArena {
    pub async fn open(
        mux: &'static Mux,
        notify: Arc<Notify>,
        timeout: Duration,
    ) -> Result<Self, StageError> {
        let h = Arc::new(ArenaHandler {
            st: Mutex::new(RefCell::new(ArenaState {
                current: 0,
                next_seq: 0,
                recent: VecDeque::with_capacity(RECENT),
                out_of_sequence: 0,
                closed: false,
                listener: None,
            })),
            notify: notify.clone(),
        });
        let client = ServiceClient::open_with(mux, ARENA_SERVICE, h.clone(), timeout).await?;
        // subscribe (0x424e10)
        if let Err(e) = client.send(&[0x00, client.local]).await {
            client.close().await;
            return Err(e.into());
        }
        Ok(Self { client, h, notify })
    }

    pub fn current(&self) -> u8 {
        self.h.st.lock(|s| s.borrow().current)
    }

    pub fn closed(&self) -> bool {
        self.h.st.lock(|s| s.borrow().closed)
    }

    pub fn out_of_sequence(&self) -> u32 {
        self.h.st.lock(|s| s.borrow().out_of_sequence)
    }

    /// Sequence number the next transition will get, for [`Self::wait_transition`].
    pub fn mark(&self) -> u64 {
        self.h.st.lock(|s| s.borrow().next_seq)
    }

    pub fn set_listener(&self, l: Listener) {
        self.h.st.lock(|s| s.borrow_mut().listener = Some(l));
    }

    /// The notification channel is gone: wake every waiter.
    pub fn lost(&self) {
        self.h.lost();
    }

    async fn write(&self, kind: u8, start: u8, values: &[u8]) -> Result<(), StageError> {
        if values.is_empty() || start as usize + values.len() > ARENA_SLOTS {
            return Err(StageError::BadValue("arena table write out of range"));
        }
        let mut p = Vec::with_capacity(values.len() + 3);
        p.extend_from_slice(&[kind, values.len() as u8, start]);
        p.extend_from_slice(values);
        self.client.send(&p).await.map_err(Into::into)
    }

    /// `active[start + k] = values[k]` (packet type 1, 0x424ff0).
    pub async fn change_active(&self, start: u8, values: &[u8]) -> Result<(), StageError> {
        self.write(0x01, start, values).await
    }

    /// `next[start + k] = values[k]` (packet type 2, 0x424f60).
    pub async fn change_next(&self, start: u8, values: &[u8]) -> Result<(), StageError> {
        self.write(0x02, start, values).await
    }

    /// First transition with `seq >= since` that satisfies `pred`; `None` on
    /// timeout or when the channel is lost.
    pub async fn wait_transition(
        &self,
        pred: impl Fn(&Transition) -> bool,
        since: u64,
        timeout: Option<Duration>,
    ) -> Option<Transition> {
        let mut found = None;
        self.notify
            .wait_until(timeout, || {
                let (f, closed) = self.h.st.lock(|s| {
                    let s = s.borrow();
                    (
                        s.recent
                            .iter()
                            .filter(|t| t.seq >= since)
                            .find(|t| pred(t))
                            .copied(),
                        s.closed,
                    )
                });
                found = f;
                found.is_some() || closed
            })
            .await;
        found
    }

    /// Wait until the current slot satisfies `pred`.
    pub async fn wait_slot(&self, pred: impl Fn(u8) -> bool, timeout: Option<Duration>) -> bool {
        self.notify
            .wait_until(timeout, || pred(self.current()) || self.closed())
            .await
            && pred(self.current())
    }
}

// --------------------------------------------------------------------------
// High-level control
// --------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum LimitUnit {
    Mm,
    N,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum SpeedUnit {
    MmPerS,
    NPerS,
}

/// One driven stage: move until `limit` is reached, then optionally hold.
///
/// `limit` is absolute: mm of extension (from the last zero) or N of load.
/// `speed` is mm/s, or N/s for a load-rate stage (needs the LoadRate option).
/// During `hold` the machine holds the limit (load, if the limit is a load;
/// position otherwise).
///
/// Direction comes from the limit, so the sign matters. Measured on an LF
/// Plus: tension is positive load and increasing extension; **compression is
/// negative load and decreasing extension**. `Drive::to_load(-50.0, ..)`
/// compresses to 50 N; `Drive::to_load(50.0, ..)` pulls.
#[derive(Debug, Clone, Copy, PartialEq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub struct Drive {
    pub limit: f64,
    pub speed: f64,
    pub limit_unit: LimitUnit,
    pub speed_unit: SpeedUnit,
    pub hold: Duration,
}

impl Drive {
    pub fn to_extension(mm: f64, speed_mm_s: f64) -> Self {
        Self {
            limit: mm,
            speed: speed_mm_s,
            limit_unit: LimitUnit::Mm,
            speed_unit: SpeedUnit::MmPerS,
            hold: Duration::from_ticks(0),
        }
    }

    pub fn to_load(newtons: f64, speed_mm_s: f64) -> Self {
        Self {
            limit: newtons,
            speed: speed_mm_s,
            limit_unit: LimitUnit::N,
            speed_unit: SpeedUnit::MmPerS,
            hold: Duration::from_ticks(0),
        }
    }

    pub fn at_load_rate(newtons: f64, rate_n_s: f64) -> Self {
        Self {
            limit: newtons,
            speed: rate_n_s,
            limit_unit: LimitUnit::N,
            speed_unit: SpeedUnit::NPerS,
            hold: Duration::from_ticks(0),
        }
    }

    pub fn with_hold(mut self, hold: Duration) -> Self {
        self.hold = hold;
        self
    }

    pub fn has_hold(&self) -> bool {
        self.hold.as_ticks() > 0
    }

    fn validate(&self) -> Result<(), StageError> {
        if !self.speed.is_finite() || self.speed <= 0.0 {
            return Err(StageError::BadValue(
                "speed must be positive (direction comes from the limit)",
            ));
        }
        if !self.limit.is_finite() {
            return Err(StageError::BadValue("limit must be finite"));
        }
        Ok(())
    }
}

/// Why a [`Segment`] ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub enum SegmentEnd {
    /// The drive (and hold, and next drive) ran to its limit.
    LimitReached,
    /// [`StageControl::stop`] ended it.
    Stopped,
    /// Another segment pre-empted it while running.
    Superseded,
    /// Another segment overtook it before the machine entered it.
    SupersededBeforeStarting,
    /// Grip protection tripped (machine warning 10).
    LoadSafetyLimit,
    /// The machine left PC control.
    ControlLost,
    LinkLost,
    Closed,
    /// The machine went somewhere unexpected.
    LeftForSlot(u8),
}

#[derive(Default)]
struct SegState {
    started: bool,
    holding: bool,
    next_started: bool,
    end: Option<SegmentEnd>,
}

/// A started [`Drive`] (plus optional follow-on drive) in one bank.
///
/// Slots: `base` drive, `base+1` hold, `base+2` next drive, `base+3`
/// DriveNowhere (stop and stay).
pub struct Segment {
    pub base: u8,
    pub drive: Drive,
    pub next_drive: Option<Drive>,
    st: Mutex<CS, RefCell<SegState>>,
    notify: Arc<Notify>,
}

impl Segment {
    fn new(base: u8, drive: Drive, next_drive: Option<Drive>, notify: Arc<Notify>) -> Self {
        Self {
            base,
            drive,
            next_drive,
            st: Mutex::new(RefCell::new(SegState::default())),
            notify,
        }
    }

    pub fn slots(&self) -> core::ops::RangeInclusive<u8> {
        self.base..=self.base + 3
    }

    pub fn started(&self) -> bool {
        self.st.lock(|s| s.borrow().started)
    }
    pub fn holding(&self) -> bool {
        self.st.lock(|s| s.borrow().holding)
    }
    pub fn next_started(&self) -> bool {
        self.st.lock(|s| s.borrow().next_started)
    }
    pub fn finished(&self) -> bool {
        self.st.lock(|s| s.borrow().end.is_some())
    }
    pub fn end(&self) -> Option<SegmentEnd> {
        self.st.lock(|s| s.borrow().end)
    }

    /// Wait until the whole segment is finished (`false` on timeout).
    pub async fn wait(&self, timeout: Option<Duration>) -> bool {
        self.notify.wait_until(timeout, || self.finished()).await
    }

    fn set(&self, f: impl FnOnce(&mut SegState)) {
        self.st.lock(|s| f(&mut s.borrow_mut()));
        self.notify.bump();
    }

    fn finish(&self, end: SegmentEnd) {
        self.set(|s| {
            if s.end.is_none() {
                s.end = Some(end);
            }
        });
    }
}

/// Protection settings given to [`StageControl::take_control`].
#[derive(Debug, Clone, Copy, PartialEq)]
#[cfg_attr(feature = "defmt", derive(defmt::Format))]
pub struct Protection {
    /// Grip-protection tensile limit, N (positive).
    pub tensile_limit: f64,
    /// Grip-protection compressive limit, N (positive).
    pub compressive_limit: f64,
    /// Manual sample stiffness, N/mm; `None` selects automatic.
    pub stiffness: Option<f64>,
    /// Range of the extension source, mm.
    pub extensometer_range: Option<f64>,
}

impl Protection {
    pub fn limits(tensile_limit: f64, compressive_limit: f64) -> Self {
        Self {
            tensile_limit,
            compressive_limit,
            stiffness: None,
            extensometer_range: None,
        }
    }
}

struct CtlState {
    /// wrapper+0x1b0: false → 16, true → 12
    bank_flag: bool,
    /// Started, not yet entered (cf. +0x1a4).
    pending: VecDeque<Arc<Segment>>,
    running: Option<Arc<Segment>>,
    /// A stop() is in flight: b+3 means "stopped".
    halting: bool,
    safety_trips: u32,
    take_attempted: bool,
    taken_with: Option<Protection>,
    hold_deadline: Option<Instant>,
}

struct Core {
    st: Mutex<CS, RefCell<CtlState>>,
    notify: Arc<Notify>,
}

impl Core {
    fn finish_all(&self, end: SegmentEnd) {
        let segs: Vec<Arc<Segment>> = self.st.lock(|s| {
            let mut s = s.borrow_mut();
            let mut v: Vec<Arc<Segment>> = s.pending.drain(..).collect();
            v.extend(s.running.take());
            s.hold_deadline = None;
            v
        });
        for seg in segs {
            seg.finish(end);
        }
        self.notify.bump();
    }

    /// cf. CMachineStageWrapper_v37
    fn on_transition(&self, t: &Transition) {
        let mut finishes: Vec<(Arc<Segment>, SegmentEnd)> = Vec::new();
        let mut entered: Option<Arc<Segment>> = None;
        let mut holding: Option<Arc<Segment>> = None;
        let mut next_started: Option<Arc<Segment>> = None;
        let lost_control = self.st.lock(|s| {
            let mut s = s.borrow_mut();
            let run = s.running.clone();
            if let Some(pos) = s.pending.iter().position(|sg| sg.base == t.new) {
                // the machine took one of our starts; older ones were overtaken
                for _ in 0..pos {
                    let sg = s.pending.pop_front().unwrap();
                    finishes.push((sg, SegmentEnd::SupersededBeforeStarting));
                }
                let sg = s.pending.pop_front().unwrap();
                if let Some(r) = run {
                    finishes.push((r, SegmentEnd::Superseded));
                }
                s.running = Some(sg.clone());
                s.hold_deadline = None;
                entered = Some(sg);
            } else if let Some(r) = run {
                if r.slots().contains(&t.new) && t.new != r.base {
                    if t.new == r.base + 1 {
                        s.hold_deadline =
                            Some(t.at.checked_add(r.drive.hold).unwrap_or(Instant::MAX));
                        holding = Some(r);
                    } else if t.new == r.base + 2 {
                        s.hold_deadline = None;
                        next_started = Some(r);
                    } else {
                        // b+3, DriveNowhere
                        let end = if s.halting {
                            SegmentEnd::Stopped
                        } else {
                            SegmentEnd::LimitReached
                        };
                        finishes.push((r, end));
                        s.running = None;
                        s.hold_deadline = None;
                    }
                } else if r.slots().contains(&t.old) && !r.slots().contains(&t.new) {
                    let end = if t.new == SLOT_READY {
                        SegmentEnd::Stopped
                    } else {
                        SegmentEnd::LeftForSlot(t.new)
                    };
                    finishes.push((r, end));
                    s.running = None;
                    s.hold_deadline = None;
                }
            }
            if t.new == SLOT_READY {
                s.halting = false;
            }
            t.new == SLOT_RELEASE_EXCLUSIVE || t.new <= SLOT_GAIN_EXCLUSIVE
        });
        if let Some(sg) = entered {
            sg.set(|s| s.started = true);
        }
        if let Some(sg) = holding {
            sg.set(|s| s.holding = true);
        }
        if let Some(sg) = next_started {
            sg.set(|s| s.next_started = true);
        }
        for (sg, end) in finishes {
            sg.finish(end);
        }
        if lost_control {
            self.finish_all(SegmentEnd::ControlLost);
        }
        self.notify.bump();
    }
}

/// The console's use of the arena: fixed slot layout, take/release control,
/// Stop, Zero, ReturnToZero and drive segments.
///
/// Created by `LloydPlus::connect`; it installs the permanent stages and the
/// connect-time tables but does not move anything. [`StageControl::run`] must
/// be polled for hold timers to expire.
///
/// Differences from the console:
///
/// * speeds that the console's clamp would turn into 0 (at or below
///   MinSpeed/10) are rejected instead of being sent;
/// * when a hold time expires, this redirects the machine out of the hold
///   slot to the next drive or the stop slot (the console sends nothing and
///   leaves the machine holding);
/// * if GainExclusive fails (slot 0), [`take_control`](Self::take_control)
///   re-arms `active[0] = 2` so the machine returns to the wait loop;
/// * [`stop`](Self::stop) first redirects any running drive into the bank's
///   DriveNowhere stage ([`HALT_ACTIVE`]), then sends the console's own Stop
///   write. Either alone stops the machine; both are verified on hardware.
/// * TrueStrainRate stages are not offered: the rate's unit is unresolved.
pub struct StageControl {
    pub arena: StageArena,
    pub min_speed: f64,
    pub max_speed: f64,
    pub extension_source: u8,
    factories: BTreeMap<&'static str, ServiceClient>,
    pub missing: Vec<&'static str>,
    core: Arc<Core>,
    /// Serializes [`start`](Self::start): bank choice, stage sends and the
    /// flag flip must not interleave between two callers.
    start_lock: embassy_sync::mutex::Mutex<CS, ()>,
}

impl StageControl {
    /// Open the arena and the factories and install the permanent stages, in
    /// the console's order (open-reply handler 0x426020).
    pub async fn open(
        mux: &'static Mux,
        min_speed: f64,
        max_speed: f64,
        sources: &[u8],
        extension_source: u8,
        timeout: Duration,
    ) -> Result<Self, StageError> {
        let notify = Arc::new(Notify::new());
        let arena = StageArena::open(mux, notify.clone(), timeout).await?;
        let core = Arc::new(Core {
            st: Mutex::new(RefCell::new(CtlState {
                bank_flag: false,
                pending: VecDeque::new(),
                running: None,
                halting: false,
                safety_trips: 0,
                take_attempted: false,
                taken_with: None,
                hold_deadline: None,
            })),
            notify: notify.clone(),
        });
        {
            let c = core.clone();
            arena.set_listener(Arc::new(move |t| c.on_transition(t)));
        }
        let ext = if extension_source >= 1 {
            extension_source
        } else {
            1
        };
        let mut factories: BTreeMap<&'static str, ServiceClient> = BTreeMap::new();

        async fn abort(arena: &StageArena, factories: &BTreeMap<&'static str, ServiceClient>) {
            for f in factories.values() {
                f.close().await;
            }
            arena.client.close().await;
        }

        for name in MANDATORY_FACTORIES {
            let svc = match ServiceClient::open(mux, name, timeout).await {
                Ok(s) => s,
                Err(e) => {
                    abort(&arena, &factories).await;
                    return Err(e.into());
                }
            };
            let packets: Result<Vec<Vec<u8>>, StageError> = match name {
                WAIT_SHARED => Ok(alloc::vec![alloc::vec![SLOT_WAIT_SHARED]]),
                WAIT_EXCLUSIVE => Ok(alloc::vec![alloc::vec![SLOT_WAIT_EXCLUSIVE]]),
                GAIN_EXCLUSIVE => Ok(alloc::vec![alloc::vec![SLOT_GAIN_EXCLUSIVE]]),
                RELEASE_EXCLUSIVE => Ok(alloc::vec![alloc::vec![SLOT_RELEASE_EXCLUSIVE]]),
                CROSSHEAD_POSITIONING => (|| {
                    Ok(alloc::vec![
                        motion_stage(SLOT_RETURN, max_speed, ext, 0.0, NO_SOURCE, 0.0)?,
                        motion_stage(SLOT_RETURN_RELEASE, max_speed, ext, 0.0, NO_SOURCE, 0.0)?,
                    ])
                })(),
                DRIVE_NOWHERE => Ok(alloc::vec![
                    alloc::vec![SLOT_STOP_A],
                    alloc::vec![SLOT_STOP_B]
                ]),
                ZERO_READINGS => Ok(alloc::vec![zero_readings_stage(SLOT_ZERO, sources)]),
                _ => Ok(Vec::new()),
            };
            let packets = match packets {
                Ok(p) => p,
                Err(e) => {
                    // a non-finite max speed from the parameters query
                    svc.close().await;
                    abort(&arena, &factories).await;
                    return Err(e);
                }
            };
            for p in packets {
                if let Err(e) = svc.send(&p).await {
                    svc.close().await;
                    abort(&arena, &factories).await;
                    return Err(e.into());
                }
            }
            factories.insert(name, svc);
        }
        if let Err(e) = async {
            arena.change_next(0, &CONNECT_NEXT).await?;
            arena.change_active(0, &[SLOT_WAIT_SHARED]).await
        }
        .await
        {
            abort(&arena, &factories).await;
            return Err(e);
        }
        for name in PERMANENT_FACTORIES {
            if let Some(s) = factories.remove(name) {
                s.close().await;
            }
        }
        let mut missing = Vec::new();
        for name in OPTIONAL_FACTORIES {
            match ServiceClient::open(mux, name, timeout).await {
                Ok(s) => {
                    factories.insert(name, s);
                }
                Err(e) => {
                    info!(
                        "optional stage not fitted: {} ({})",
                        name,
                        crate::fmt::Debug2Format(&e)
                    );
                    missing.push(name);
                }
            }
        }
        Ok(Self {
            arena,
            min_speed,
            max_speed,
            extension_source,
            factories,
            missing,
            core,
            start_lock: embassy_sync::mutex::Mutex::new(()),
        })
    }

    /// Close every service. Call after `stop` / `release_control`.
    pub async fn close(&self) {
        for f in self.factories.values() {
            f.close().await;
        }
        self.arena.client.close().await;
        self.arena.lost();
        self.core.finish_all(SegmentEnd::Closed);
    }

    /// The change notifier shared by segments and waiters.
    pub fn notify(&self) -> &Arc<Notify> {
        &self.core.notify
    }

    /// Hold-timer maintenance: must be polled while control is in use.
    pub async fn run(&self) {
        let mut rx = self.core.notify.receiver();
        loop {
            let deadline = self.core.st.lock(|s| s.borrow().hold_deadline);
            let wake = async {
                match rx.as_mut() {
                    Some(rx) => {
                        rx.changed().await;
                    }
                    None => Timer::after_millis(10).await,
                }
            };
            match deadline {
                Some(d) => {
                    if let Either::Second(()) = select(wake, Timer::at(d)).await {
                        self.end_hold().await;
                    }
                }
                None => wake.await,
            }
        }
    }

    async fn end_hold(&self) {
        let seg = self.core.st.lock(|s| {
            let mut s = s.borrow_mut();
            match s.hold_deadline {
                Some(d) if Instant::now() >= d => {
                    s.hold_deadline = None;
                    s.running.clone()
                }
                _ => None,
            }
        });
        if let Some(seg) = seg {
            if seg.finished() || self.slot() != seg.base + 1 {
                return;
            }
            let target = if seg.next_drive.is_some() {
                seg.base + 2
            } else {
                seg.base + 3
            };
            debug!("hold expired in slot {} -> {}", seg.base + 1, target);
            if let Err(e) = self.arena.change_active(seg.base + 1, &[target]).await {
                warn!("could not end hold: {}", crate::fmt::Debug2Format(&e));
            }
        }
    }

    fn cancel_holds(&self) {
        self.core.st.lock(|s| s.borrow_mut().hold_deadline = None);
    }

    /// Called for every machine message. Code 10, "Load has exceeded safety
    /// limit", is the grip-protection trip. Verified on an LF Plus: the
    /// machine stops the crosshead itself about 1 % of the load range past
    /// the limit, stays in the drive slot and reports no transition, so the
    /// segment would otherwise never finish. This ends the running and pending
    /// segments with [`SegmentEnd::LoadSafetyLimit`] and sends the stop writes
    /// (the machine then reports `b -> b+3 -> 7`); a new segment can be started
    /// straight after, no action at the frame is needed.
    pub async fn on_machine_message(&self, msg: &MachineMessage) {
        if msg.code != SAFETY_LIMIT_CODE {
            return;
        }
        self.core.st.lock(|s| s.borrow_mut().safety_trips += 1);
        warn!(
            "grip protection tripped (slot {}): segments ended",
            self.slot()
        );
        self.core.finish_all(SegmentEnd::LoadSafetyLimit);
        let slot = self.slot();
        if (slot >= BANKS[1] || slot == SLOT_RETURN || slot == SLOT_RETURN_RELEASE)
            && let Err(e) = self.stop(None).await
        {
            warn!(
                "stop after grip trip failed: {}",
                crate::fmt::Debug2Format(&e)
            );
        }
    }

    /// The link or the machine went away: wake all waiters, end all segments.
    pub fn on_lost(&self) {
        self.arena.lost();
        self.core.finish_all(SegmentEnd::LinkLost);
    }

    fn fail(&self, what: &'static str) -> StageError {
        if self.arena.closed() {
            StageError::Lost { what }
        } else {
            StageError::Timeout {
                what,
                slot: self.slot(),
            }
        }
    }

    // -- state ----------------------------------------------------------------

    pub fn slot(&self) -> u8 {
        self.arena.current()
    }

    /// The console's "machine available" test: current slot > 1 (0x425210).
    pub fn available(&self) -> bool {
        self.slot() > 1
    }

    /// Under PC control and not moving.
    pub fn ready(&self) -> bool {
        self.slot() == SLOT_READY
    }

    pub fn in_control(&self) -> bool {
        let s = self.slot();
        (SLOT_GRIP_PROTECTION..=SLOT_READY).contains(&s)
            || s == SLOT_ZERO
            || s == SLOT_RETURN
            || s >= BANKS[1]
    }

    pub fn has(&self, factory: &str) -> bool {
        self.factories.contains_key(factory)
    }

    pub fn safety_trips(&self) -> u32 {
        self.core.st.lock(|s| s.borrow().safety_trips)
    }

    pub fn take_attempted(&self) -> bool {
        self.core.st.lock(|s| s.borrow().take_attempted)
    }

    /// The segment the machine is currently executing, if any.
    pub fn running(&self) -> Option<Arc<Segment>> {
        self.core.st.lock(|s| s.borrow().running.clone())
    }

    fn ext_source(&self) -> u8 {
        if self.extension_source >= 1 {
            self.extension_source
        } else {
            1
        }
    }

    fn factory(&self, name: &'static str) -> Result<&ServiceClient, StageError> {
        self.factories.get(name).ok_or(StageError::NotFitted(name))
    }

    // -- control --------------------------------------------------------------

    /// Gain exclusive control and arm the protections (0x426980). Returns when
    /// the machine is ready (slot 7).
    pub async fn take_control(&self, p: Protection, timeout: Duration) -> Result<(), StageError> {
        if self.in_control() {
            self.stop(Some(Duration::from_secs(5))).await?;
            let same = self.core.st.lock(|s| s.borrow().taken_with == Some(p));
            if self.ready() && same {
                return Ok(());
            }
            // re-take with the new protection settings
            self.release_control(Duration::from_secs(10)).await?;
        }
        self.core.st.lock(|s| {
            let mut s = s.borrow_mut();
            s.take_attempted = true;
            s.taken_with = None;
        });
        let a = &self.arena;
        let mark = a.mark();
        self.factory(GRIP_PROTECTION)?
            .send(&grip_protection_stage(
                SLOT_GRIP_PROTECTION,
                p.tensile_limit,
                p.compressive_limit,
            )?)
            .await?;
        let mut last = SLOT_GRIP_PROTECTION;
        if self.has(SET_MANUAL_STIFFNESS) && self.has(SET_AUTO_STIFFNESS) {
            // as the console does
            a.change_next(SLOT_STIFFNESS, &[SLOT_STIFFNESS]).await?;
            match p.stiffness {
                None => {
                    self.factory(SET_AUTO_STIFFNESS)?
                        .send(&[SLOT_STIFFNESS])
                        .await?
                }
                Some(k) => {
                    self.factory(SET_MANUAL_STIFFNESS)?
                        .send(&manual_stiffness_stage(SLOT_STIFFNESS, k)?)
                        .await?
                }
            }
            last = SLOT_STIFFNESS;
        } else if p.stiffness.is_some() {
            return Err(StageError::NotFitted(SET_MANUAL_STIFFNESS));
        }
        match p.extensometer_range {
            Some(r) => {
                a.change_next(last, &[SLOT_DATASOURCE_RANGE]).await?;
                self.factory(SET_DATASOURCE_RANGE)?
                    .send(&datasource_range_stage(
                        SLOT_DATASOURCE_RANGE,
                        self.ext_source(),
                        r,
                    )?)
                    .await?;
            }
            None => a.change_next(last, &[SLOT_READY]).await?,
        }
        a.change_active(
            SLOT_WAIT_EXCLUSIVE,
            &[SLOT_GAIN_EXCLUSIVE, SLOT_GAIN_EXCLUSIVE],
        )
        .await?;
        a.change_active(SLOT_GAIN_EXCLUSIVE, &[SLOT_PARKED]).await?;
        let t = a
            .wait_transition(
                |t| t.new == SLOT_READY || t.new == SLOT_PARKED,
                mark,
                Some(timeout),
            )
            .await;
        match t {
            None => Err(self.fail("taking control")),
            Some(t) if t.new == SLOT_PARKED => {
                a.change_active(SLOT_PARKED, &[SLOT_WAIT_SHARED]).await?;
                Err(StageError::ControlRefused)
            }
            Some(_) => {
                self.core.st.lock(|s| s.borrow_mut().taken_with = Some(p));
                Ok(())
            }
        }
    }

    /// Hand control back to the frame (0x426b20).
    pub async fn release_control(&self, timeout: Duration) -> Result<(), StageError> {
        self.cancel_holds();
        self.arena.change_active(0, &RELEASE_ACTIVE).await?;
        self.core.st.lock(|s| s.borrow_mut().taken_with = None);
        if !self
            .arena
            .wait_slot(
                |s| s == SLOT_WAIT_EXCLUSIVE || s == SLOT_WAIT_SHARED,
                Some(timeout),
            )
            .await
        {
            return Err(self.fail("releasing control"));
        }
        Ok(())
    }

    /// Halt the crosshead and go to ready (slot 7).
    ///
    /// Two arena writes. [`HALT_ACTIVE`] redirects a running drive, hold, next
    /// drive or return into its bank's DriveNowhere stage (`b+3`); then the
    /// console's own Stop (0x427580, [`STOP_ACTIVE`]) takes the machine from
    /// there to ready. Verified on an LF Plus: the machine reports `b -> b+3
    /// -> 7` within a millisecond of each other and the head halts within
    /// ~0.1 mm at 10 mm/min (PROTOCOL.md §11.9).
    ///
    /// A segment ended this way finishes with [`SegmentEnd::Stopped`]. With
    /// `timeout = None` the writes are sent and the call returns at once.
    pub async fn stop(&self, timeout: Option<Duration>) -> Result<(), StageError> {
        self.cancel_holds();
        let pending = self.core.st.lock(|s| {
            let mut s = s.borrow_mut();
            let pending = !s.pending.is_empty();
            s.halting = pending || s.running.is_some();
            pending
        });
        let mark = self.arena.mark();
        self.arena.change_active(0, &HALT_ACTIVE).await?;
        self.arena.change_active(0, &STOP_ACTIVE).await?;
        let Some(timeout) = timeout else {
            return Ok(());
        };
        if pending {
            // a start the machine hasn't reported yet: wait for its b -> 7
            if self
                .arena
                .wait_transition(|t| t.new == SLOT_READY, mark, Some(timeout))
                .await
                .is_none()
            {
                return Err(self.fail("stopping"));
            }
        } else if self.in_control()
            && !self
                .arena
                .wait_slot(|s| s == SLOT_READY, Some(timeout))
                .await
        {
            return Err(self.fail("stopping"));
        }
        Ok(())
    }

    /// `Console.Zero` (0x427570): zero all readings. Stops a segment first;
    /// otherwise only from ready.
    pub async fn zero(&self, timeout: Duration) -> Result<(), StageError> {
        self.require_ready("zero").await?;
        let mark = self.arena.mark();
        self.arena.change_active(SLOT_READY, &[SLOT_ZERO]).await?;
        if self
            .arena
            .wait_transition(|t| t.old == SLOT_ZERO, mark, Some(timeout))
            .await
            .is_none()
        {
            return Err(self.fail("zeroing"));
        }
        Ok(())
    }

    /// `Console.ReturnToZero` (0x427550): drive to 0 mm at MaxSpeed. Stops a
    /// segment first; otherwise only from ready. With `wait`, returns once the
    /// machine has left the return slot.
    pub async fn return_to_zero(
        &self,
        wait: bool,
        timeout: Option<Duration>,
    ) -> Result<(), StageError> {
        self.require_ready("return to zero").await?;
        self.cancel_holds();
        let mark = self.arena.mark();
        self.arena.change_active(SLOT_READY, &[SLOT_RETURN]).await?;
        if wait
            && self
                .arena
                .wait_transition(|t| t.old == SLOT_RETURN, mark, timeout)
                .await
                .is_none()
        {
            return Err(self.fail("returning to zero"));
        }
        Ok(())
    }

    async fn require_ready(&self, what: &'static str) -> Result<(), StageError> {
        if self.slot() >= BANKS[1] {
            // a finished or running segment: Stop first, as the manual says
            self.stop(Some(Duration::from_secs(5))).await?;
        }
        if !self.ready() {
            return Err(StageError::NotReady {
                what,
                slot: self.slot(),
            });
        }
        Ok(())
    }

    // -- drive segments ---------------------------------------------------------

    /// Load a segment into the idle bank and start it (0x427910).
    ///
    /// Starting from ready, from a return in progress, or while another
    /// segment runs (which it pre-empts). `next_drive` runs straight after
    /// `drive` (and its hold) without a pause; its own hold is ignored, as in
    /// the console.
    pub async fn start(
        &self,
        drive: Drive,
        next_drive: Option<Drive>,
    ) -> Result<Arc<Segment>, StageError> {
        let slot = self.slot();
        if !(slot == SLOT_READY
            || slot == SLOT_RETURN
            || slot == SLOT_RETURN_RELEASE
            || slot >= BANKS[1])
        {
            return Err(StageError::NotInControl { slot });
        }
        drive.validate()?;
        if let Some(n) = &next_drive {
            n.validate()?;
        }
        let _serial = self.start_lock.lock().await;
        let b = self.core.st.lock(|s| {
            if s.borrow().bank_flag {
                BANKS[1]
            } else {
                BANKS[0]
            }
        });
        let seg = Arc::new(Segment::new(b, drive, next_drive, self.core.notify.clone()));
        self.send_drive(b, &drive).await?;
        if drive.has_hold() {
            self.send_hold(b + 1, &drive).await?;
        }
        if let Some(n) = &next_drive {
            self.send_drive(b + 2, n).await?;
        }
        let n1 = if next_drive.is_some() { b + 2 } else { b + 3 };
        let n0 = if drive.has_hold() { b + 1 } else { n1 };
        self.arena.change_next(b, &[n0, n1, b + 3]).await?;
        self.core.st.lock(|s| {
            let mut s = s.borrow_mut();
            s.bank_flag = !s.bank_flag;
            s.halting = false;
            s.pending.push_back(seg.clone());
        });
        self.arena.change_active(0, &segment_active(b)).await?;
        Ok(seg)
    }

    /// [`start`](Self::start) and wait for the segment to finish. On timeout
    /// the machine is stopped before the error is returned.
    pub async fn drive(
        &self,
        drive: Drive,
        next_drive: Option<Drive>,
        timeout: Option<Duration>,
    ) -> Result<Arc<Segment>, StageError> {
        let seg = self.start(drive, next_drive).await?;
        if !seg.wait(timeout).await {
            let _ = self.stop(Some(Duration::from_secs(5))).await;
            return Err(self.fail("waiting for segment"));
        }
        match seg.end() {
            Some(SegmentEnd::LinkLost) | Some(SegmentEnd::Closed) => {
                Err(StageError::Lost { what: "driving" })
            }
            _ => Ok(seg),
        }
    }

    fn limit(&self, d: &Drive) -> (u8, f64) {
        match d.limit_unit {
            LimitUnit::N => (0, d.limit),
            LimitUnit::Mm => (self.ext_source(), d.limit),
        }
    }

    async fn send_drive(&self, slot: u8, d: &Drive) -> Result<(), StageError> {
        let (src, limit) = self.limit(d);
        if d.speed_unit == SpeedUnit::NPerS {
            if !self.has(LOAD_RATE) {
                return Err(StageError::NotFitted(LOAD_RATE));
            }
            return self
                .factory(LOAD_RATE)?
                .send(&motion_stage(slot, d.speed, src, limit, NO_SOURCE, 0.0)?)
                .await
                .map_err(Into::into);
        }
        let speed = clamp_speed(d.speed, self.min_speed, self.max_speed);
        if speed <= 0.0 {
            return Err(StageError::SpeedTooLow {
                speed: d.speed,
                min_speed: self.min_speed,
            });
        }
        if speed != d.speed {
            warn!("speed {} mm/s limited to {} mm/s", d.speed, speed);
        }
        self.factory(CROSSHEAD_RATE)?
            .send(&motion_stage(slot, speed, src, limit, NO_SOURCE, 0.0)?)
            .await
            .map_err(Into::into)
    }

    async fn send_hold(&self, slot: u8, d: &Drive) -> Result<(), StageError> {
        let (src, limit) = self.limit(d);
        let factory = match d.limit_unit {
            LimitUnit::N => {
                if !self.has(LOAD_RATE) {
                    return Err(StageError::NotFitted(LOAD_RATE));
                }
                LOAD_RATE
            }
            LimitUnit::Mm => CROSSHEAD_RATE,
        };
        self.factory(factory)?
            .send(&motion_stage(slot, 0.0, src, limit, NO_SOURCE, 0.0)?)
            .await
            .map_err(Into::into)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fmt_e_matches_msvc() {
        for (x, text) in [
            (50.0, "5.000000e+001"),
            (5.0, "5.000000e+000"),
            (100.0 / 60.0, "1.666667e+000"),
            (0.0, "0.000000e+000"),
            (-0.5, "-5.000000e-001"),
            (1e-7, "1.000000e-007"),
            (1e120, "1.000000e+120"),
        ] {
            assert_eq!(fmt_e(x).unwrap(), text, "{x}");
        }
        assert!(fmt_e(f64::NAN).is_none());
    }

    #[test]
    fn worked_example_stage_bytes() {
        // PROTOCOL.md §11.6 worked example: 50 mm at 100 mm/min, slot 16, source 1
        assert_eq!(
            motion_stage(16, 100.0 / 60.0, 1, 50.0, NO_SOURCE, 0.0).unwrap(),
            b"\x101.666667e+000\x00\x015.000000e+001\x00\xff0.000000e+000\x00"
        );
    }

    #[test]
    fn small_stage_encoders() {
        assert_eq!(
            grip_protection_stage(4, 1000.0, 500.0).unwrap(),
            b"\x041.000000e+003\x00-5.000000e+002\x00"
        );
        assert_eq!(
            manual_stiffness_stage(5, 2.5).unwrap(),
            b"\x052.500000e+000\x00"
        );
        assert_eq!(
            datasource_range_stage(6, 2, 10.0).unwrap(),
            b"\x06\x021.000000e+001\x00"
        );
        assert_eq!(zero_readings_stage(9, &[0, 1]), b"\x09\xff\xff");
        assert_eq!(zero_readings_stage(9, &[2]), b"\x09\x00\x00\xff");
        assert_eq!(zero_readings_stage(9, &[]), b"\x09");
    }

    #[test]
    fn clamp_speed_matches_console() {
        assert_eq!(clamp_speed(50.0, 0.01, 30.0), 30.0);
        assert_eq!(clamp_speed(5.0, 0.01, 30.0), 5.0);
        assert_eq!(clamp_speed(0.005, 0.01, 30.0), 0.01);
        assert_eq!(clamp_speed(0.0009, 0.01, 30.0), 0.0);
    }

    #[test]
    fn table_constants() {
        assert_eq!(RELEASE_ACTIVE[..12], [0, 1, 2, 1, 8, 8, 8, 8, 8, 8, 11, 11]);
        assert_eq!(RELEASE_ACTIVE[31], 7);
        assert_eq!(STOP_ACTIVE[..12], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 7, 8]);
        assert!(STOP_ACTIVE[12..].iter().all(|&v| v == 7));
        assert_eq!(HALT_ACTIVE[..10], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
        assert!(HALT_ACTIVE[10..16].iter().all(|&v| v == 15));
        assert!(HALT_ACTIVE[16..20].iter().all(|&v| v == 19));
        assert!(HALT_ACTIVE[20..].iter().all(|&v| v == 7));
        let s = segment_active(16);
        assert_eq!(&s[..7], &[0, 1, 2, 3, 4, 5, 6]);
        assert_eq!(&s[7..12], &[16, 8, 9, 16, 16]);
        assert!(s[12..20].iter().all(|&v| v == 16));
        assert!(s[20..].iter().all(|&v| v == 7));
    }
}
