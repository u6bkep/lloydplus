"""Protocol-level simulator of a Plus-series machine, for tests and demos.

It implements the machine side as understood from the PC console's code, so
it validates this library's self-consistency, not the real firmware.  Its
link layer is the same symmetric :class:`~lloydplus.link.Link`.

The stage engine follows the model in PROTOCOL.md §11 as confirmed on an LF
Plus: ``next`` is followed when a stage completes; an ``active`` write that
covers the current slot makes the machine enter ``active[current]`` at once,
even when that is the same slot (a ``19 -> 19`` re-entry report).  The
mechanics are a crosshead at ``position`` mm pulling a linear sample of
``stiffness`` N/mm (load = stiffness * position): tension positive,
compression negative, as measured.
"""

from __future__ import annotations

import math
import queue
import threading
import time

from .channels import ChannelMux
from .link import Link, LinkReset
from .services import atof
from . import stages as st


class PipePort:
    """One end of an in-memory full-duplex byte pipe."""

    def __init__(self, rx: "queue.Queue[bytes]", tx: "queue.Queue[bytes]",
                 timeout: float = 0.05) -> None:
        self._rx, self._tx, self.timeout = rx, tx, timeout
        self._pending = b""
        self.tap = None   # optional callable(bytes) observing writes

    def read(self, size: int = 1) -> bytes:
        if not self._pending:
            try:
                self._pending = self._rx.get(timeout=self.timeout)
            except queue.Empty:
                return b""
        out, self._pending = self._pending[:size], self._pending[size:]
        return out

    def write(self, data: bytes) -> int:
        if self.tap:
            self.tap(data)
        self._tx.put(bytes(data))
        return len(data)

    def close(self) -> None:
        pass


def port_pair() -> tuple[PipePort, PipePort]:
    a, b = queue.Queue(), queue.Queue()
    return PipePort(a, b), PipePort(b, a)


# What each factory's stage does in the simulator
_INSTANT = {st.GRIP_PROTECTION, st.SET_AUTO_STIFFNESS, st.SET_MANUAL_STIFFNESS,
            st.SET_DATASOURCE_RANGE, st.RELEASE_EXCLUSIVE}
_MOTION = {st.CROSSHEAD_RATE, st.CROSSHEAD_POSITIONING, st.LOAD_RATE}


def parse_motion(p: bytes) -> tuple[float, int, float, int, float]:
    """Inverse of :func:`lloydplus.stages.motion_stage` (without the slot)."""
    rate, rest = p.split(b"\0", 1)
    src, rest = rest[0], rest[1:]
    limit, rest = rest.split(b"\0", 1)
    strain, gauge = rest[0], rest[1:].split(b"\0", 1)[0]
    return atof(rate), src, atof(limit), strain, atof(gauge)


class SimMachine:
    TICK = 0.01        # s of simulated time per step (also one reading per source)

    def __init__(self, port, *, version=(2, 1, 7),
                 descriptions=("LF", "LF Plus", "Lloyd Instruments LF Plus"),
                 sample_period_ticks: int = 10, stiffness: float = 10.0,
                 remote: bool = True, gap: bool = False,
                 fitted: set[str] | None = None) -> None:
        self.version = bytes(version)
        self.descriptions = descriptions
        self.sample_period_ticks = sample_period_ticks
        self.link = Link(port, self._on_data, name="sim", on_reset=self._on_link_reset)
        self.mux = ChannelMux(self.link)
        self.status_channel: int | None = None
        self.keypad: list[int] = []
        self.system_time: bytes | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self.sources = [
            # index, kind, extra, a, b, name
            (0, 1, 0, "-1000", "1000", "Load"),
            (1, 2, 0, "-1100", "1100", "Extension"),
        ]
        self.scales = {0: 0.01, 1: 0.001}      # N/count, mm/count
        self.parameters = ("1000", "1100", "0.000166667", "33.8667")   # N, mm, mm/s, mm/s
        # mechanics
        self.stiffness = stiffness
        # gap=True: a compression-only specimen whose surface is at position 0;
        # above it the crosshead is in free air and the load reads 0.
        self.gap = gap
        self.position = 0.0                     # mm, absolute
        self.zero = {0: 0.0, 1: 0.0}            # readings subtracted after Zero
        self.remote = remote                    # frame set to Remote Control
        # stage engine
        self.fitted = set(st.MANDATORY_FACTORIES) | set(
            st.OPTIONAL_FACTORIES if fitted is None else fitted)
        self.stages: dict[int, tuple[str, bytes]] = {}
        self.next = list(range(st.ARENA_SLOTS))
        self.active = list(range(st.ARENA_SLOTS))
        self.current = 0
        self.notify_channel: int | None = None
        self.transitions: list[tuple[int, int]] = []
        self.factory_packets: list[tuple[str, bytes]] = []
        self.arena_packets: list[bytes] = []
        self.grip_limits: tuple[float, float] | None = None
        self.tripped = False                    # grip protection has halted the motor
        self.grip_margin = 10.0                 # N past the limit before it trips (1 % of 1 kN)
        self._error_ch: int | None = None
        self._channels: list[int] = []
        self._rezero: set[int] = set()
        self._install()

    def _on_data(self, data: bytes) -> None:
        self.mux.on_data(data)

    def _on_link_reset(self) -> None:
        """The PC restarted its link (BOOT 0 while we were connected). Every
        channel the old session opened is gone, as on the real frame, whose
        console re-opens its services from scratch; readings stop until the
        new session enables them. The crosshead stays where it is."""
        with self._lock:
            self.mux.reset()
            self.status_channel = None
            self.notify_channel = None
            self._error_ch = None
            self._channels = []
            self._rezero.clear()
            self.keypad.clear()
            self.stages.clear()
            self.current = 0
            self.tripped = False
            self._install()

    def start(self) -> None:
        self.link.start()
        self._thread = threading.Thread(target=self._run, name="sim-machine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.link.stop()

    # -- mechanics and readings ---------------------------------------------

    @property
    def load(self) -> float:
        if self.gap and self.position > 0:
            return 0.0
        return self.stiffness * self.position

    def reading(self, index: int) -> float:
        raw = self.load if index == 0 else self.position
        return raw - self.zero.get(index, 0.0)

    def _run(self) -> None:
        while not self._stop.wait(self.TICK):
            with self._lock:
                try:
                    self._step(self.TICK)
                    self._emit()
                except LinkReset:
                    pass        # between sessions: nothing to send to

    def _emit(self) -> None:
        for i, ch in enumerate(self._channels):
            if i in self._rezero:
                self._rezero.discard(i)
                self.mux.send(ch, b"\x02" + (0).to_bytes(4, "big", signed=True))
                self._prev[i] = 0
            counts = int(round(self.reading(i) / self.scales.get(i, 1.0)))
            d = max(-32768, min(32767, counts - self._prev[i]))
            self._prev[i] += d
            self.mux.send(ch, b"\x00" + d.to_bytes(2, "big", signed=True))

    # -- stage engine -------------------------------------------------------

    def _goto(self, slot: int) -> None:
        self.tripped = False
        for _ in range(64):              # instant stages chain through `next`
            old, self.current = self.current, slot
            self.transitions.append((old, slot))
            if self.notify_channel is not None:
                self.mux.send(self.notify_channel, bytes((0, old, slot)))
            if not self._enter(slot):
                return
            slot = self.next[slot]
        raise RuntimeError("stage loop")

    def _enter(self, slot: int) -> bool:
        """Run the entry action of ``slot``; True if it completed at once."""
        kind, p = self.stages.get(slot, (None, b""))
        if kind in _INSTANT:
            if kind == st.GRIP_PROTECTION:
                t, c = p.split(b"\0")[:2]
                self.grip_limits = (atof(t), abs(atof(c)))     # sent as "T" and "-C"
            return True
        if kind == st.ZERO_READINGS:
            for i, m in enumerate(p):
                if m == 0xFF:
                    self.zero[i] = (self.load if i == 0 else self.position)
                    self._rezero.add(i)
            return True
        if kind == st.GAIN_EXCLUSIVE:
            return self.remote
        if kind == st.WAIT_EXCLUSIVE:
            return self.remote
        if kind == st.WAIT_SHARED:
            return not self.remote
        if kind in _MOTION:
            return self._motion_done(p, 0.0)
        return False                     # empty slot, DoNothing, DriveNowhere: idle

    def _motion_done(self, p: bytes, dt: float) -> bool:
        rate, src, limit, _strain, _gauge = parse_motion(p)
        kind = self.stages[self.current][0]
        if rate == 0:
            return False                 # a hold never completes by itself
        speed = rate / self.stiffness if kind == st.LOAD_RATE else rate   # mm/s
        here = self.reading(src)
        gap = limit - here
        per_mm = self.stiffness if src == 0 else 1.0
        if src == 0 and self.gap and self.position > 0 and limit < here:
            # load-driven move down through free air: nothing changes until
            # the surface; just travel
            self.position = max(0.0, self.position - speed * dt)
            return False
        step = speed * dt * per_mm
        if abs(gap) <= max(step, 1e-9):
            self.position += gap / per_mm
            return True
        self.position += math.copysign(speed * dt, gap)
        return False

    def _step(self, dt: float) -> None:
        kind, p = self.stages.get(self.current, (None, b""))
        if kind in _MOTION and self.tripped:
            return                        # halted by grip protection, still in the slot
        if kind in _MOTION and self.grip_limits and parse_motion(p)[1] != 0:
            # as measured: extension-driven moves only, ~1 % of range past the limit,
            # motor stops, no transition, warning 10 on the PC's Error service.  A
            # move away from the overload (the retreat) is let through.
            t, c = self.grip_limits
            _rate, src, limit, _s, _g = parse_motion(p)
            going_up = limit > self.reading(src)
            if ((going_up and self.load > t + self.grip_margin)
                    or (not going_up and self.load < -(c + self.grip_margin))):
                self.tripped = True
                threading.Thread(target=self.push_warning,
                                 args=(10, "Load has exceeded safety limit."), daemon=True).start()
                return
        if kind in _MOTION and self._motion_done(p, dt):
            self._goto(self.next[self.current])
        elif kind == st.WAIT_EXCLUSIVE and self.remote:
            self._goto(self.next[self.current])
        elif kind == st.WAIT_SHARED and not self.remote:
            self._goto(self.next[self.current])

    def _on_arena(self, p: bytes) -> None:
        self.arena_packets.append(p)
        if len(p) == 2 and p[0] == 0:
            self.notify_channel = p[1]
            return
        if len(p) < 4 or p[0] not in (1, 2) or p[1] != len(p) - 3:
            return
        count, start, values = p[1], p[2], p[3:]
        table = self.active if p[0] == 1 else self.next
        table[start:start + count] = list(values)
        # a redirect is checked only for the slots this packet wrote, and is
        # followed even when active[current] == current (PROTOCOL.md §11.1)
        if p[0] == 1 and start <= self.current < start + count:
            self._goto(self.active[self.current])

    # -- services ---------------------------------------------------------

    def _install(self) -> None:
        m = self.mux

        def reply_service(build):
            def factory(mux, _r):
                def h(p):
                    if p:
                        out = build(p)
                        if out is not None:
                            mux.send(p[0], out)
                return h
            return factory

        def description(p):
            # same selection rule as the console's own CDescriptionService
            # (0x40bed0): start at the first string, advance while the next fits
            limit = p[1] if len(p) > 1 else 0
            best = self.descriptions[0]
            for d in self.descriptions[1:]:
                if len(d) > limit:
                    break
                best = d
            return best.encode()

        def parameters(p):
            return b"\0".join(s.encode() for s in self.parameters) + b"\0\0"

        def data_information(p):
            for idx, kind, extra, a, b, name in self.sources:
                rec = bytes((idx, kind, extra)) + a.encode() + b"\0" + b.encode() + b"\0" + name.encode() + b"\0"
                self.mux.send(p[0], rec)
            return b""

        def status(mux, _r):
            def h(p):
                if p:
                    self.status_channel = p[0]
            return h

        def dataset(mux, _r):
            def h(p):
                self._start_stream(list(p))
            return h

        def locked(fn):
            def h(p):
                with self._lock:
                    fn(p)
            return h

        def arena(mux, _r):
            return locked(self._on_arena)

        def factory(name):
            def make(mux, _r):
                def h(p):
                    self.factory_packets.append((name, bytes(p)))
                    if p:
                        self.stages[p[0]] = (name, bytes(p[1:]))
                return locked(h)
            return make

        def responder(mux, _r):
            def h(p):
                if p:
                    mux.send(p[0], p[1:])
            return h

        def keypad(mux, _r):
            return lambda p: self.keypad.extend(p)

        def set_time(mux, _r):
            def h(p):
                self.system_time = p
            return h

        m.local_services.update({
            "Version": reply_service(lambda p: self.version),
            "Description": reply_service(description),
            "TensileParameters": reply_service(parameters),
            "DataInformation": reply_service(data_information),
            "StatusInformation": status,
            "DataSet": dataset,
            "Responder": responder,
            "NotImplementedDataSources": lambda mux, r: (lambda p: None),
            "KeypadEZ": keypad,
            "SetSystemTime": set_time,
            st.ARENA_SERVICE: arena,
        })
        for name in self.fitted:
            m.local_services[name] = factory(name)

    def push_status(self, code: int, text: str = "") -> None:
        if self.status_channel is None:
            raise RuntimeError("PC has not subscribed to StatusInformation")
        self.mux.send(self.status_channel, bytes(((code >> 8) & 0xFF, code & 0xFF)) + text.encode())
        self.mux.send(self.status_channel, b"")

    def push_warning(self, code: int, text: str = "") -> None:
        """Send ``[2, code_hi, code_lo, text]`` on the PC's "Error" service, as the
        LF Plus does for the grip-protection trip."""
        if self._error_ch is None:
            self._error_ch = self.mux.open_service("Error", timeout=5.0)
        self.mux.send(self._error_ch, bytes((2, (code >> 8) & 0xFF, code & 0xFF)) + text.encode())

    def set_remote(self, remote: bool) -> None:
        """Flip the frame between Local and Remote Control."""
        with self._lock:
            self.remote = remote
            self._step(0.0)

    def _start_stream(self, channels: list[int]) -> None:
        with self._lock:
            n = self.sample_period_ticks
            for i, ch in enumerate(channels):
                self.mux.send(ch, b"\x04" + repr(self.scales.get(i, 1.0)).encode() + b"\0")
                self.mux.send(ch, b"\x03" + n.to_bytes(2, "big"))
                self.mux.send(ch, b"\x02" + (12345).to_bytes(4, "big", signed=True))
            self._prev = [0] * len(channels)
            self._channels = channels
