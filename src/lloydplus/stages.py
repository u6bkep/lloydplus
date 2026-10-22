"""Test control: the stage arena and the stage factories (PROTOCOL.md §11).

The machine runs "stages" out of a 32-slot arena.  Each stage kind has its own
factory service; the PC creates a stage by sending ``[slot, params...]`` to it.
``StageArenaReference5`` holds two tables: ``next[i]`` (where to go when the
stage in slot i completes) and ``active[i]`` (a redirect out of slot i,
applied when written).  The machine reports every slot change.

:class:`StageControl` reproduces the slot layout and command sequences of the
NEXYGEN 4.1 console (``CMachineStageWrapper``, 0x4256f0..0x4289d0).

Units on the wire are N, mm, mm/s, N/s and N/mm (PROTOCOL.md §10).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .channels import ChannelMux, ServiceClient, ServiceOpenError

log = logging.getLogger(__name__)

ARENA_SERVICE = "StageArenaReference5"
ARENA_SLOTS = 32

# Slot layout used by the console (PROTOCOL.md §11.4)
SLOT_PARKED = 0
SLOT_WAIT_EXCLUSIVE = 1
SLOT_WAIT_SHARED = 2
SLOT_GAIN_EXCLUSIVE = 3
SLOT_GRIP_PROTECTION = 4
SLOT_STIFFNESS = 5
SLOT_DATASOURCE_RANGE = 6
SLOT_READY = 7
SLOT_RELEASE_EXCLUSIVE = 8
SLOT_ZERO = 9
SLOT_RETURN = 10
SLOT_RETURN_RELEASE = 11
SLOT_STOP_A = 15
SLOT_STOP_B = 19
BANKS = (16, 12)              # segment bases, in the order the console uses them

NO_SOURCE = 0xFF

# Factory service names
DO_NOTHING = "DoNothingStageFactory"
WAIT_SHARED = "WaitSharedStageFactory"
WAIT_EXCLUSIVE = "WaitExclusiveStageFactory"
GAIN_EXCLUSIVE = "GainExclusiveStageFactory"
RELEASE_EXCLUSIVE = "ReleaseExclusiveStageFactory"
CROSSHEAD_RATE = "CrossheadRateStageFactory"
CROSSHEAD_POSITIONING = "CrossheadPositioningStageFactory"
DRIVE_NOWHERE = "DriveNowhereStageFactory"
ZERO_READINGS = "ZeroReadingsStageFactory"
SET_DATASOURCE_RANGE = "SetDatasourceRangeStageFactory"
GRIP_PROTECTION = "GripProtectionStageFactory"
LOAD_RATE = "LoadRateStageFactory"
TRUE_STRAIN_RATE = "TrueStrainRateStageFactory"
SET_MANUAL_STIFFNESS = "SetManualSampleStiffnessStageFactory"
SET_AUTO_STIFFNESS = "SetAutoSampleStiffnessStageFactory"

# Open order of CMachineStageWrapper_v63 (0x4259b0).  A failure in the first
# group is fatal for the console; the second group is optional equipment.
MANDATORY_FACTORIES = (
    DO_NOTHING, WAIT_SHARED, WAIT_EXCLUSIVE, GAIN_EXCLUSIVE, RELEASE_EXCLUSIVE,
    CROSSHEAD_RATE, CROSSHEAD_POSITIONING, DRIVE_NOWHERE, ZERO_READINGS,
    SET_DATASOURCE_RANGE, GRIP_PROTECTION,
)
OPTIONAL_FACTORIES = (LOAD_RATE, TRUE_STRAIN_RATE, SET_MANUAL_STIFFNESS, SET_AUTO_STIFFNESS)
# Factories whose stages are installed once at connect, then closed (0x4265cd)
PERMANENT_FACTORIES = (
    DO_NOTHING, WAIT_EXCLUSIVE, WAIT_SHARED, GAIN_EXCLUSIVE, RELEASE_EXCLUSIVE,
    DRIVE_NOWHERE, ZERO_READINGS, CROSSHEAD_POSITIONING,
)

# Table writes copied from the console
CONNECT_NEXT = bytes((0, 2, 1, 4, 5, 6, 7, 7, 1, 7, 7, 8))           # 0x426573
RELEASE_ACTIVE = bytes((0, 1, 2, 1) + (8,) * 6 + (11, 11) + (8,) * 19 + (7,))  # 0x426b20
STOP_ACTIVE = bytes(range(10)) + bytes((7, 8)) + bytes((7,)) * 20    # 0x427580
_SEGMENT_ACTIVE = (bytes(range(7)) + b"\xff\x08\x09\xff\xff" + b"\xff" * 8
                   + bytes((7,)) * 12)                                # 0x4280e0


# --------------------------------------------------------------------------
# Packet encoders
# --------------------------------------------------------------------------

def fmt_e(x: float) -> bytes:
    """``x`` as MSVC's ``printf("%e")``: six decimals, three-digit exponent
    (``5.000000e+001``)."""
    x = float(x)
    if not math.isfinite(x):
        raise ValueError(f"cannot send non-finite value {x!r}")
    mant, exp = f"{x:.6e}".split("e")
    e = int(exp)
    return f"{mant}e{'-' if e < 0 else '+'}{abs(e):03d}".encode("ascii")


def _byte(v: int) -> bytes:
    if not 0 <= v <= 0xFF:
        raise ValueError(f"byte field out of range: {v}")
    return bytes((v,))


def motion_stage(slot: int, rate: float, limit_src: int, limit: float,
                 strain_src: int = NO_SOURCE, gauge: float = 0.0) -> bytes:
    """CrossheadRate / CrossheadPositioning / LoadRate / TrueStrainRate stage
    (0x4282f0, ``%c%e%c%c%e%c%c%e%c``)."""
    return (_byte(slot) + fmt_e(rate) + b"\0" + _byte(limit_src) + fmt_e(limit) + b"\0"
            + _byte(strain_src) + fmt_e(gauge) + b"\0")


def grip_protection_stage(slot: int, tensile: float, compressive: float) -> bytes:
    """``%c%e%c-%e%c`` (0x426bd0): tensile and compressive load limits, N."""
    return _byte(slot) + fmt_e(tensile) + b"\0-" + fmt_e(compressive) + b"\0"


def manual_stiffness_stage(slot: int, stiffness: float) -> bytes:
    """``%c%e%c`` (0x426d00): sample stiffness, N/mm."""
    return _byte(slot) + fmt_e(stiffness) + b"\0"


def datasource_range_stage(slot: int, source: int, value: float) -> bytes:
    """``%c%c%e%c`` (0x426d00): range of extension source ``source``, mm."""
    return _byte(slot) + _byte(source) + fmt_e(value) + b"\0"


def zero_readings_stage(slot: int, sources: Iterable[int]) -> bytes:
    """``[slot, m0..mK]`` with 0xFF for each source to zero (0x426020)."""
    idx = sorted(set(sources))
    if not idx:
        return _byte(slot)
    mask = bytearray(idx[-1] + 1)
    for i in idx:
        mask[i] = 0xFF
    return _byte(slot) + bytes(mask)


def clamp_speed(speed: float, min_speed: float, max_speed: float) -> float:
    """The console's speed limiting (0x428790), mm/s: above max -> max; above
    min -> unchanged; above min/10 -> min; otherwise 0."""
    if speed > max_speed:
        return max_speed
    if speed > min_speed:
        return speed
    if speed > min_speed / 10.0:
        return min_speed
    return 0.0


# --------------------------------------------------------------------------
# The arena service
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Transition:
    old: int
    new: int
    time: float          # time.monotonic() when the report arrived


class StageArena(ServiceClient):
    """``StageArenaReference5``: table writes out, slot transitions in."""

    def __init__(self, mux: ChannelMux, timeout: float = 5.0) -> None:
        self.current = 0
        self.transitions: list[Transition] = []
        self.listeners: list[Callable[[Transition], None]] = []
        self.out_of_sequence = 0
        self._cv = threading.Condition()
        super().__init__(mux, ARENA_SERVICE, timeout=timeout)
        self.send(bytes((0x00, self.local)))          # subscribe (0x424e10)

    def on_packet(self, payload: bytes) -> None:      # 0x40e2a0
        if not payload:
            return
        if payload[0] == 0x00 and len(payload) == 3:
            t = Transition(payload[1], payload[2], time.monotonic())
            with self._cv:
                if t.old != self.current:
                    self.out_of_sequence += 1
                    log.warning("stage changed out of sequence: expected %d, got %d -> %d",
                                self.current, t.old, t.new)
                self.current = t.new
                self.transitions.append(t)
                self._cv.notify_all()
            log.debug("stage %d -> %d", t.old, t.new)
            for cb in list(self.listeners):
                try:
                    cb(t)
                except Exception:
                    log.exception("stage listener failed")
        elif payload[0] == 0x01:
            log.warning("machine closed the stage notification channel")
            self.closed.set()
            with self._cv:
                self._cv.notify_all()

    def _write(self, kind: int, start: int, values: bytes) -> None:
        values = bytes(values)
        if not values or start < 0 or start + len(values) > ARENA_SLOTS:
            raise ValueError(f"bad table write: start={start} count={len(values)}")
        self.send(bytes((kind, len(values), start)) + values)

    def change_active(self, start: int, values: bytes | Iterable[int]) -> None:
        """``active[start + k] = values[k]`` (packet type 1, 0x424ff0)."""
        self._write(0x01, start, bytes(values))

    def change_next(self, start: int, values: bytes | Iterable[int]) -> None:
        """``next[start + k] = values[k]`` (packet type 2, 0x424f60)."""
        self._write(0x02, start, bytes(values))

    def mark(self) -> int:
        """Position in :attr:`transitions`, for :meth:`wait_transition`."""
        with self._cv:
            return len(self.transitions)

    def wait_transition(self, pred: Callable[[Transition], bool], since: int,
                        timeout: float | None) -> Transition | None:
        """First transition at index >= ``since`` that satisfies ``pred``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cv:
            i = since
            while True:
                while i < len(self.transitions):
                    t = self.transitions[i]
                    i += 1
                    if pred(t):
                        return t
                if self.closed.is_set():
                    return None
                left = None if deadline is None else deadline - time.monotonic()
                if left is not None and left <= 0:
                    return None
                self._cv.wait(left)

    def wait_slot(self, pred: Callable[[int], bool], timeout: float | None) -> bool:
        """Wait until the current slot satisfies ``pred``."""
        with self._cv:
            return self._cv.wait_for(lambda: pred(self.current) or self.closed.is_set(),
                                     timeout) and pred(self.current)


# --------------------------------------------------------------------------
# High-level control
# --------------------------------------------------------------------------

class StageError(Exception):
    pass


@dataclass
class Drive:
    """One driven stage: move until ``limit`` is reached, then optionally hold.

    ``limit`` is absolute: mm of extension (from the last zero) or N of load.
    ``speed`` is mm/s, or N/s for a load-rate stage (needs the LoadRate
    option).  ``hold`` is seconds; during it the machine holds the limit
    (load, if the limit is a load; position otherwise).
    """
    limit: float
    speed: float
    limit_unit: str = "mm"        # "mm" or "N"
    speed_unit: str = "mm/s"      # "mm/s" or "N/s"
    hold: float = 0.0

    def __post_init__(self) -> None:
        if self.limit_unit not in ("mm", "N"):
            raise ValueError(f"limit_unit must be 'mm' or 'N', not {self.limit_unit!r}")
        if self.speed_unit not in ("mm/s", "N/s"):
            raise ValueError(f"speed_unit must be 'mm/s' or 'N/s', not {self.speed_unit!r}")
        if not self.speed > 0:
            raise ValueError("speed must be positive (direction comes from the limit)")
        if self.hold < 0:
            raise ValueError("hold must be >= 0")

    @classmethod
    def to_extension(cls, mm: float, speed_mm_s: float, hold: float = 0.0) -> "Drive":
        return cls(mm, speed_mm_s, "mm", "mm/s", hold)

    @classmethod
    def to_load(cls, newtons: float, speed_mm_s: float, hold: float = 0.0) -> "Drive":
        return cls(newtons, speed_mm_s, "N", "mm/s", hold)

    @classmethod
    def at_load_rate(cls, newtons: float, rate_n_s: float, hold: float = 0.0) -> "Drive":
        return cls(newtons, rate_n_s, "N", "N/s", hold)


class Segment:
    """A started :class:`Drive` (plus optional follow-on drive) in one bank.

    Slots: ``base`` drive, ``base+1`` hold, ``base+2`` next drive,
    ``base+3`` DriveNowhere (stop and stay).
    """

    def __init__(self, base: int, drive: Drive, next_drive: Drive | None) -> None:
        self.base = base
        self.drive = drive
        self.next_drive = next_drive
        self.started = threading.Event()
        self.holding = threading.Event()
        self.next_started = threading.Event()
        self.finished = threading.Event()
        self.reason: str | None = None
        self._hold_timer: threading.Timer | None = None

    @property
    def slots(self) -> range:
        return range(self.base, self.base + 4)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until the whole segment is finished."""
        return self.finished.wait(timeout)

    def _finish(self, reason: str) -> None:
        if self._hold_timer:
            self._hold_timer.cancel()
        if not self.finished.is_set():
            self.reason = reason
            self.finished.set()

    def __repr__(self) -> str:
        state = ("finished: " + str(self.reason) if self.finished.is_set()
                 else "running" if self.started.is_set() else "pending")
        return f"<Segment base={self.base} {self.drive} {state}>"


class StageControl:
    """The console's use of the arena: fixed slot layout, take/release
    control, Stop, Zero, ReturnToZero and drive segments.

    Created by :meth:`LloydPlus.connect`; it installs the permanent stages and
    the connect-time tables but does not move anything.

    Differences from the console:

    * speeds that the console's clamp would turn into 0 (at or below
      MinSpeed/10) are rejected instead of being sent;
    * when a hold time expires, this redirects the machine out of the hold
      slot to the next drive or the stop slot (the console sends nothing and
      leaves the machine holding);
    * if GainExclusive fails (slot 0), :meth:`take_control` re-arms
      ``active[0] = 2`` so the machine returns to the wait loop.
    * TrueStrainRate stages are not offered: the rate's unit is unresolved.
    """

    def __init__(self, mux: ChannelMux, *, min_speed: float, max_speed: float,
                 sources: Iterable[int], extension_source: int = 1,
                 timeout: float = 5.0) -> None:
        self.mux = mux
        self.min_speed = min_speed
        self.max_speed = max_speed
        self.extension_source = extension_source
        self.timeout = timeout
        self.factories: dict[str, ServiceClient] = {}
        self.missing: dict[str, ServiceOpenError] = {}
        self._lock = threading.RLock()
        self._bank_flag = False            # wrapper+0x1b0: False -> 16, True -> 12
        self._segments: list[Segment] = []
        self.arena = StageArena(mux, timeout)
        self.arena.listeners.append(self._on_transition)
        try:
            self._install(list(sources))
        except Exception:
            self.close()
            raise

    # -- connect ----------------------------------------------------------

    def _install(self, sources: list[int]) -> None:
        for name in MANDATORY_FACTORIES:
            self.factories[name] = ServiceClient(self.mux, name, timeout=self.timeout)
        for name in OPTIONAL_FACTORIES:
            try:
                self.factories[name] = ServiceClient(self.mux, name, timeout=self.timeout)
            except ServiceOpenError as e:
                log.info("optional stage not fitted: %s", e)
                self.missing[name] = e
        f = self.factories
        f[WAIT_EXCLUSIVE].send(bytes((SLOT_WAIT_EXCLUSIVE,)))
        f[WAIT_SHARED].send(bytes((SLOT_WAIT_SHARED,)))
        f[GAIN_EXCLUSIVE].send(bytes((SLOT_GAIN_EXCLUSIVE,)))
        f[RELEASE_EXCLUSIVE].send(bytes((SLOT_RELEASE_EXCLUSIVE,)))
        f[DRIVE_NOWHERE].send(bytes((SLOT_STOP_A,)))
        f[DRIVE_NOWHERE].send(bytes((SLOT_STOP_B,)))
        f[ZERO_READINGS].send(zero_readings_stage(SLOT_ZERO, sources))
        for slot in (SLOT_RETURN, SLOT_RETURN_RELEASE):
            f[CROSSHEAD_POSITIONING].send(
                motion_stage(slot, self.max_speed, self._ext_source(), 0.0))
        self.arena.change_next(0, CONNECT_NEXT)
        self.arena.change_active(0, bytes((SLOT_WAIT_SHARED,)))
        for name in PERMANENT_FACTORIES:
            f.pop(name).close()

    def close(self) -> None:
        for s in list(self.factories.values()):
            s.close()
        self.factories.clear()
        self.arena.close()

    # -- state ------------------------------------------------------------

    @property
    def slot(self) -> int:
        return self.arena.current

    @property
    def available(self) -> bool:
        """The console's "machine available" test: current slot > 1 (0x425210)."""
        return self.arena.current > 1

    @property
    def ready(self) -> bool:
        """Under PC control and not moving."""
        return self.arena.current == SLOT_READY

    @property
    def in_control(self) -> bool:
        s = self.arena.current
        return SLOT_GRIP_PROTECTION <= s <= SLOT_READY or s in (SLOT_ZERO, SLOT_RETURN) \
            or s >= BANKS[1]

    def has(self, factory: str) -> bool:
        return factory in self.factories

    def _ext_source(self) -> int:
        return self.extension_source if self.extension_source >= 1 else 1

    # -- control ----------------------------------------------------------

    def take_control(self, *, tensile_limit: float, compressive_limit: float,
                     stiffness: float | None = None,
                     extensometer_range: float | None = None,
                     timeout: float = 10.0) -> None:
        """Gain exclusive control and arm the protections (0x426980).

        ``tensile_limit`` / ``compressive_limit`` (N, both positive) go to the
        grip-protection stage.  ``stiffness`` (N/mm) selects manual sample
        stiffness; None selects automatic.  ``extensometer_range`` (mm) sets
        the extension source's range.  Returns when the machine is ready
        (slot 7); raises :class:`StageError` if control can't be gained.
        """
        if self.ready:
            return
        a, f = self.arena, self.factories
        mark = a.mark()
        f[GRIP_PROTECTION].send(
            grip_protection_stage(SLOT_GRIP_PROTECTION, tensile_limit, compressive_limit))
        last = SLOT_GRIP_PROTECTION
        if self.has(SET_MANUAL_STIFFNESS) and self.has(SET_AUTO_STIFFNESS):
            a.change_next(SLOT_STIFFNESS, bytes((SLOT_STIFFNESS,)))   # as the console does
            if stiffness is None:
                f[SET_AUTO_STIFFNESS].send(bytes((SLOT_STIFFNESS,)))
            else:
                f[SET_MANUAL_STIFFNESS].send(manual_stiffness_stage(SLOT_STIFFNESS, stiffness))
            last = SLOT_STIFFNESS
        elif stiffness is not None:
            raise StageError("sample stiffness stages are not fitted")
        if extensometer_range is not None:
            a.change_next(last, bytes((SLOT_DATASOURCE_RANGE,)))
            f[SET_DATASOURCE_RANGE].send(datasource_range_stage(
                SLOT_DATASOURCE_RANGE, self._ext_source(), extensometer_range))
        else:
            a.change_next(last, bytes((SLOT_READY,)))
        a.change_active(SLOT_WAIT_EXCLUSIVE, bytes((SLOT_GAIN_EXCLUSIVE,) * 2))
        a.change_active(SLOT_GAIN_EXCLUSIVE, bytes((SLOT_PARKED,)))
        t = a.wait_transition(lambda t: t.new in (SLOT_READY, SLOT_PARKED), mark, timeout)
        if t is None:
            raise StageError(f"timed out taking control (machine in slot {self.slot}; "
                             "is the frame set to Remote Control?)")
        if t.new == SLOT_PARKED:
            a.change_active(SLOT_PARKED, bytes((SLOT_WAIT_SHARED,)))
            raise StageError("exclusive control could not be gained")

    def release_control(self, timeout: float = 10.0) -> None:
        """Hand control back to the frame (0x426b20)."""
        self._cancel_holds()
        self.arena.change_active(0, RELEASE_ACTIVE)
        if not self.arena.wait_slot(lambda s: s in (SLOT_WAIT_EXCLUSIVE, SLOT_WAIT_SHARED),
                                    timeout):
            raise StageError(f"timed out releasing control (machine in slot {self.slot})")

    def stop(self, timeout: float | None = 5.0) -> None:
        """``Console.Stop`` (0x427580): any running segment or return -> ready."""
        self._cancel_holds()
        self.arena.change_active(0, STOP_ACTIVE)
        if timeout is not None and self.in_control:
            if not self.arena.wait_slot(lambda s: s == SLOT_READY, timeout):
                raise StageError(f"timed out stopping (machine in slot {self.slot})")

    def zero(self, timeout: float = 10.0) -> None:
        """``Console.Zero`` (0x427570): zero all readings.  Stops a segment
        first; otherwise only from ready."""
        self._require_ready("zero")
        mark = self.arena.mark()
        self.arena.change_active(SLOT_READY, bytes((SLOT_ZERO,)))
        if self.arena.wait_transition(lambda t: t.old == SLOT_ZERO, mark, timeout) is None:
            raise StageError(f"timed out zeroing (machine in slot {self.slot})")

    def return_to_zero(self, wait: bool = True, timeout: float | None = None) -> None:
        """``Console.ReturnToZero`` (0x427550): drive to 0 mm at MaxSpeed.
        Stops a segment first; otherwise only from ready."""
        self._require_ready("return to zero")
        self._cancel_holds()
        mark = self.arena.mark()
        self.arena.change_active(SLOT_READY, bytes((SLOT_RETURN,)))
        if wait and self.arena.wait_transition(lambda t: t.old == SLOT_RETURN, mark,
                                               timeout) is None:
            raise StageError(f"timed out returning to zero (machine in slot {self.slot})")

    def _require_ready(self, what: str) -> None:
        if self.slot >= BANKS[1]:
            self.stop()      # a finished or running segment: Stop first, as the manual says
        if not self.ready:
            raise StageError(f"cannot {what}: machine is in slot {self.slot}, not ready (7); "
                             "take control and stop first")

    # -- drive segments ----------------------------------------------------

    def start(self, drive: Drive, next_drive: Drive | None = None) -> Segment:
        """Load a segment into the idle bank and start it (0x427910).

        Starting from ready, from a return in progress, or while another
        segment runs (which it pre-empts).  ``next_drive`` runs straight after
        ``drive`` (and its hold) without a pause; its own hold is ignored, as
        in the console.
        """
        if not (self.ready or self.slot in (SLOT_RETURN, SLOT_RETURN_RELEASE)
                or self.slot >= BANKS[1]):
            raise StageError(f"cannot start: machine is in slot {self.slot}; take control first")
        with self._lock:
            b = BANKS[1] if self._bank_flag else BANKS[0]
            seg = Segment(b, drive, next_drive)
            self._send_drive(b, drive)
            if drive.hold > 0:
                self._send_hold(b + 1, drive)
            if next_drive is not None:
                self._send_drive(b + 2, next_drive)
            n1 = b + 2 if next_drive is not None else b + 3
            n0 = b + 1 if drive.hold > 0 else n1
            self.arena.change_next(b, bytes((n0, n1, b + 3)))
            self._bank_flag = not self._bank_flag
            self._segments.append(seg)
            self.arena.change_active(0, _SEGMENT_ACTIVE.replace(b"\xff", bytes((b,))))
        return seg

    def drive(self, drive: Drive, next_drive: Drive | None = None,
              timeout: float | None = None) -> Segment:
        """:meth:`start` and wait for the segment to finish."""
        seg = self.start(drive, next_drive)
        if not seg.wait(timeout):
            raise StageError(f"timed out waiting for {seg}")
        return seg

    def _limit(self, d: Drive) -> tuple[int, float]:
        return (0, d.limit) if d.limit_unit == "N" else (self._ext_source(), d.limit)

    def _send_drive(self, slot: int, d: Drive) -> None:
        src, limit = self._limit(d)
        if d.speed_unit == "N/s":
            if not self.has(LOAD_RATE):
                raise StageError("load-rate control is not fitted (no LoadRateStageFactory)")
            self.factories[LOAD_RATE].send(motion_stage(slot, d.speed, src, limit))
            return
        speed = clamp_speed(d.speed, self.min_speed, self.max_speed)
        if speed <= 0:
            raise ValueError(f"speed {d.speed} mm/s is below the machine's minimum "
                             f"({self.min_speed} mm/s)")
        if speed != d.speed:
            log.warning("speed %g mm/s limited to %g mm/s", d.speed, speed)
        self.factories[CROSSHEAD_RATE].send(motion_stage(slot, speed, src, limit))

    def _send_hold(self, slot: int, d: Drive) -> None:
        src, limit = self._limit(d)
        if d.limit_unit == "N":
            if not self.has(LOAD_RATE):
                raise StageError("holding a load needs load-rate control "
                                 "(no LoadRateStageFactory)")
            self.factories[LOAD_RATE].send(motion_stage(slot, 0.0, src, limit))
        else:
            self.factories[CROSSHEAD_RATE].send(motion_stage(slot, 0.0, src, limit))

    def _cancel_holds(self) -> None:
        with self._lock:
            for s in self._segments:
                if s._hold_timer:
                    s._hold_timer.cancel()

    def _end_hold(self, seg: Segment) -> None:
        with self._lock:
            if seg.finished.is_set() or self.slot != seg.base + 1:
                return
            target = seg.base + 2 if seg.next_drive is not None else seg.base + 3
            log.debug("hold expired in slot %d -> %d", seg.base + 1, target)
            self.arena.change_active(seg.base + 1, bytes((target,)))

    def _on_transition(self, t: Transition) -> None:   # cf. CMachineStageWrapper_v37
        with self._lock:
            for seg in list(self._segments):
                if seg.finished.is_set():
                    continue
                if t.new == seg.base:
                    seg.started.set()
                elif t.new == seg.base + 1 and seg.started.is_set():
                    seg.holding.set()
                    seg._hold_timer = threading.Timer(seg.drive.hold, self._end_hold, (seg,))
                    seg._hold_timer.daemon = True
                    seg._hold_timer.start()
                elif t.new == seg.base + 2 and seg.started.is_set():
                    if seg._hold_timer:
                        seg._hold_timer.cancel()
                    seg.next_started.set()
                elif t.new == seg.base + 3 and seg.started.is_set():
                    seg._finish("limit reached")
                elif seg.started.is_set() and t.old in seg.slots:
                    seg._finish("stopped" if t.new == SLOT_READY
                                else "superseded" if t.new >= BANKS[1]
                                else f"left for slot {t.new}")
                elif not seg.started.is_set() and t.new >= BANKS[1] and t.new not in seg.slots:
                    seg._finish("superseded before starting")
            if t.new == SLOT_RELEASE_EXCLUSIVE or t.new <= SLOT_GAIN_EXCLUSIVE:
                for seg in self._segments:
                    seg._finish("control lost")
            self._segments = [s for s in self._segments if not s.finished.is_set()]
