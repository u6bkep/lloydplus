"""Test control: the stage arena and the stage factories (PROTOCOL.md §11).

The machine runs "stages" out of a 32-slot arena.  Each stage kind has its own
factory service; the PC creates a stage by sending ``[slot, params...]`` to it.
``StageArenaReference5`` holds two tables: ``next[i]`` (where to go when the
stage in slot i completes) and ``active[i]`` (a redirect out of slot i,
applied when written, even when ``active[i] == i``, which re-enters slot i).
The machine reports every slot change.  Verified on an LF Plus (firmware
3.0.94) on 2026-10-22: connect, take and release control, drive segments,
stop and the transition reports all behaved as described here.

:class:`StageControl` reproduces the slot layout and command sequences of the
NEXYGEN 4.1 console (``CMachineStageWrapper``, 0x4256f0..0x4289d0).

Units on the wire are N, mm, mm/s, N/s and N/mm (PROTOCOL.md §10).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
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

# Machine warning "Load has exceeded safety limit." (services.MESSAGE_TEXT): the
# grip-protection trip.  Verified on an LF Plus: the machine halts the
# crosshead, stays in the drive slot, and sends this on the PC's Error service.
SAFETY_LIMIT_CODE = 10

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
# lloydplus addition (PROTOCOL.md §11.9): every motion slot -> its bank's
# DriveNowhere stage (15 or 19).  Sent before STOP_ACTIVE.  Both writes stop
# a drive on hardware (16 -> 19 -> 7, or 16 -> 7 for STOP_ACTIVE alone); this
# one goes through a stage whose job is to stop, so it is kept first.
HALT_ACTIVE = (bytes(range(10)) + bytes((SLOT_STOP_A,)) * 6
               + bytes((SLOT_STOP_B,)) * 4 + bytes((7,)) * 12)
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

    def _on_remote_close(self) -> None:
        self.lost()

    def lost(self) -> None:
        """The notification channel is gone: wake every waiter."""
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

    Direction comes from the limit, so the sign matters.  Measured on an LF
    Plus: tension is positive load and increasing extension; **compression
    is negative load and decreasing extension**.  ``Drive.to_load(-50, ...)``
    compresses to 50 N; ``Drive.to_load(50, ...)`` pulls.
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
      ``active[0] = 2`` so the machine returns to the wait loop;
    * :meth:`stop` first redirects any running drive into the bank's
      DriveNowhere stage (``HALT_ACTIVE``), then sends the console's own Stop
      write.  Either alone stops the machine; both are verified on hardware.
    * TrueStrainRate stages are not offered: the rate's unit is unresolved.

    See PROTOCOL.md §11.9 for the full list.
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
        self._pending: deque[Segment] = deque()   # started, not yet entered (cf. +0x1a4)
        self._running: Segment | None = None
        self._halting = False              # a stop() is in flight: b+3 means "stopped"
        self.safety_trips = 0              # grip-protection trips seen (code 10)
        self.take_attempted = False
        self._taken_with: tuple | None = None
        self.arena = StageArena(mux, timeout)
        self.arena.listeners.append(self._on_transition)
        try:
            self._install(list(sources))
        except Exception:
            self.close()
            raise

    # -- connect ----------------------------------------------------------

    def _install(self, sources: list[int]) -> None:
        # Each permanent stage is sent as soon as its factory opens, in the
        # console's open order (open-reply handler 0x426020).
        ext = self._ext_source()
        permanent = {
            WAIT_SHARED: [bytes((SLOT_WAIT_SHARED,))],
            WAIT_EXCLUSIVE: [bytes((SLOT_WAIT_EXCLUSIVE,))],
            GAIN_EXCLUSIVE: [bytes((SLOT_GAIN_EXCLUSIVE,))],
            RELEASE_EXCLUSIVE: [bytes((SLOT_RELEASE_EXCLUSIVE,))],
            CROSSHEAD_POSITIONING: [motion_stage(s, self.max_speed, ext, 0.0)
                                    for s in (SLOT_RETURN, SLOT_RETURN_RELEASE)],
            DRIVE_NOWHERE: [bytes((SLOT_STOP_A,)), bytes((SLOT_STOP_B,))],
            ZERO_READINGS: [zero_readings_stage(SLOT_ZERO, sources)],
        }
        for name in MANDATORY_FACTORIES:
            svc = self.factories[name] = ServiceClient(self.mux, name, timeout=self.timeout)
            for packet in permanent.get(name, ()):
                svc.send(packet)
        self.arena.change_next(0, CONNECT_NEXT)
        self.arena.change_active(0, bytes((SLOT_WAIT_SHARED,)))
        for name in PERMANENT_FACTORIES:
            self.factories.pop(name).close()
        for name in OPTIONAL_FACTORIES:
            try:
                self.factories[name] = ServiceClient(self.mux, name, timeout=self.timeout)
            except ServiceOpenError as e:
                log.info("optional stage not fitted: %s", e)
                self.missing[name] = e

    def close(self) -> None:
        for s in list(self.factories.values()):
            s.close()
        self.factories.clear()
        self.arena.close()
        self._finish_all("closed")

    def on_machine_message(self, msg) -> None:
        """Called by :class:`LloydPlus` for every machine message.

        Code 10, "Load has exceeded safety limit", is the grip-protection
        trip.  Verified on an LF Plus: the machine stops the crosshead itself
        about 1 % of the load range past the limit, stays in the drive slot
        and reports no transition, so the segment would otherwise never
        finish.  This ends the running and pending segments with reason
        ``"load safety limit"`` and sends the stop writes (the machine then
        reports ``b -> b+3 -> 7``); a new segment can be started straight
        after, no action at the frame is needed.
        """
        if getattr(msg, "code", None) != SAFETY_LIMIT_CODE:
            return
        with self._lock:
            self.safety_trips += 1
        log.warning("grip protection tripped (slot %d): segments ended", self.slot)
        self._finish_all("load safety limit")
        if self.slot >= BANKS[1] or self.slot in (SLOT_RETURN, SLOT_RETURN_RELEASE):
            self.stop(timeout=None)     # on the reader thread: must not wait

    def on_lost(self) -> None:
        """The link or the machine went away: wake all waiters, end all segments."""
        self.arena.lost()
        self._finish_all("link lost")

    def _finish_all(self, reason: str) -> None:
        with self._lock:
            for seg in [*self._pending, self._running]:
                if seg is not None:
                    seg._finish(reason)
            self._pending.clear()
            self._running = None

    def _fail(self, what: str) -> StageError:
        if self.arena.closed.is_set():
            return StageError(f"{what}: connection to the machine lost")
        return StageError(f"timed out {what} (machine in slot {self.slot})")

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
        params = (tensile_limit, compressive_limit, stiffness, extensometer_range)
        if self.in_control:
            self.stop()
            if self.ready and params == self._taken_with:
                return
            self.release_control()       # re-take with the new protection settings
        self.take_attempted = True
        self._taken_with = None
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
            if a.closed.is_set():
                raise self._fail("taking control")
            raise StageError(f"timed out taking control (machine in slot {self.slot}; "
                             "is the frame set to Remote Control?)")
        if t.new == SLOT_PARKED:
            a.change_active(SLOT_PARKED, bytes((SLOT_WAIT_SHARED,)))
            raise StageError("exclusive control could not be gained")
        self._taken_with = params

    def release_control(self, timeout: float = 10.0) -> None:
        """Hand control back to the frame (0x426b20)."""
        self._cancel_holds()
        self.arena.change_active(0, RELEASE_ACTIVE)
        self._taken_with = None
        if not self.arena.wait_slot(lambda s: s in (SLOT_WAIT_EXCLUSIVE, SLOT_WAIT_SHARED),
                                    timeout):
            raise self._fail("releasing control")

    def stop(self, timeout: float | None = 5.0) -> None:
        """Halt the crosshead and go to ready (slot 7).

        Two arena writes.  ``HALT_ACTIVE`` redirects a running drive, hold,
        next drive or return into its bank's DriveNowhere stage (``b+3``);
        then the console's own Stop (0x427580, ``STOP_ACTIVE``) takes the
        machine from there to ready.  Verified on an LF Plus: the machine
        reports ``b -> b+3 -> 7`` within a millisecond of each other and the
        head halts within ~0.1 mm at 10 mm/min (PROTOCOL.md §11.9).

        A segment ended this way finishes with reason ``"stopped"``.
        """
        self._cancel_holds()
        with self._lock:
            pending = bool(self._pending)
            self._halting = pending or self._running is not None
        mark = self.arena.mark()
        self.arena.change_active(0, HALT_ACTIVE)
        self.arena.change_active(0, STOP_ACTIVE)
        if timeout is None:
            return
        if pending:
            # a start the machine hasn't reported yet: wait for its b -> 7
            if self.arena.wait_transition(lambda t: t.new == SLOT_READY, mark, timeout) is None:
                raise self._fail("stopping")
        elif self.in_control and not self.arena.wait_slot(lambda s: s == SLOT_READY, timeout):
            raise self._fail("stopping")

    def zero(self, timeout: float = 10.0) -> None:
        """``Console.Zero`` (0x427570): zero all readings.  Stops a segment
        first; otherwise only from ready."""
        self._require_ready("zero")
        mark = self.arena.mark()
        self.arena.change_active(SLOT_READY, bytes((SLOT_ZERO,)))
        if self.arena.wait_transition(lambda t: t.old == SLOT_ZERO, mark, timeout) is None:
            raise self._fail("zeroing")

    def return_to_zero(self, wait: bool = True, timeout: float | None = None) -> None:
        """``Console.ReturnToZero`` (0x427550): drive to 0 mm at MaxSpeed.
        Stops a segment first; otherwise only from ready."""
        self._require_ready("return to zero")
        self._cancel_holds()
        mark = self.arena.mark()
        self.arena.change_active(SLOT_READY, bytes((SLOT_RETURN,)))
        if wait and self.arena.wait_transition(lambda t: t.old == SLOT_RETURN, mark,
                                               timeout) is None:
            raise self._fail("returning to zero")

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
            self._halting = False
            self._pending.append(seg)
            self.arena.change_active(0, _SEGMENT_ACTIVE.replace(b"\xff", bytes((b,))))
        return seg

    def drive(self, drive: Drive, next_drive: Drive | None = None,
              timeout: float | None = None) -> Segment:
        """:meth:`start` and wait for the segment to finish.  On timeout the
        machine is stopped before :class:`StageError` is raised."""
        seg = self.start(drive, next_drive)
        if not seg.wait(timeout):
            try:
                self.stop()
            finally:
                raise StageError(f"timed out waiting for {seg}; stopped")
        if seg.reason in ("link lost", "closed"):
            raise StageError(f"{seg}: connection to the machine lost")
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
            if self._running and self._running._hold_timer:
                self._running._hold_timer.cancel()

    def _end_hold(self, seg: Segment) -> None:
        with self._lock:
            if seg.finished.is_set() or self.slot != seg.base + 1:
                return
            target = seg.base + 2 if seg.next_drive is not None else seg.base + 3
            log.debug("hold expired in slot %d -> %d", seg.base + 1, target)
            self.arena.change_active(seg.base + 1, bytes((target,)))

    def _on_transition(self, t: Transition) -> None:   # cf. CMachineStageWrapper_v37
        with self._lock:
            run = self._running
            entered = next((sg for sg in self._pending if sg.base == t.new), None)
            if entered is not None:
                # the machine took one of our starts; older ones were overtaken
                while self._pending[0] is not entered:
                    self._pending.popleft()._finish("superseded before starting")
                self._pending.popleft()
                if run is not None:
                    run._finish("superseded")
                self._running = run = entered
                entered.started.set()
            elif run is not None and t.new in run.slots[1:]:
                if t.new == run.base + 1:
                    run.holding.set()
                    if run._hold_timer:
                        run._hold_timer.cancel()
                    run._hold_timer = threading.Timer(run.drive.hold, self._end_hold, (run,))
                    run._hold_timer.daemon = True
                    run._hold_timer.start()
                elif t.new == run.base + 2:
                    if run._hold_timer:
                        run._hold_timer.cancel()
                    run.next_started.set()
                else:                              # b+3, DriveNowhere
                    run._finish("stopped" if self._halting else "limit reached")
                    self._running = None
            elif run is not None and t.old in run.slots and t.new not in run.slots:
                run._finish("stopped" if t.new == SLOT_READY else f"left for slot {t.new}")
                self._running = None
            if t.new == SLOT_READY:
                self._halting = False
            if t.new == SLOT_RELEASE_EXCLUSIVE or t.new <= SLOT_GAIN_EXCLUSIVE:
                self._finish_all("control lost")
