"""Application services of the Plus-series machine, and the PC-side services
the machine may open on us.

Function addresses refer to NEXYGEN 4.1 ``EZConsole/LrCon.exe``.
"""

from __future__ import annotations

import datetime as _dt
import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .channels import ChannelMux, ServiceClient

log = logging.getLogger(__name__)

# Version reported by the original console (file version 4.1, issue 9).
PC_VERSION = bytes((4, 1, 9))
# InternalName + " Version " + FileVersion of LrCon.exe (0x416160)
PC_DESCRIPTION = "LrCon Version 4.1 Issue 9"

_NUM = __import__("re").compile(rb"\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def atof(text: bytes) -> float:
    """C ``atof``: parse the longest numeric prefix, 0.0 if there is none."""
    m = _NUM.match(text)
    return float(m.group(0)) if m else 0.0


# Machine message codes (FUN_00401dd0 -> LrConRes.dll string table 0x402..)
MESSAGE_TEXT = {
    0: "Machine communications protocol violation.",
    1: "Motor fault.",
    2: "Upper extension limit switch reached.",
    3: "Lower extension limit switch reached.",
    4: "Safe line is open.",
    5: "No load cell connected.",
    6: "Guard not closed.",
    7: "Load cell rating exceeded.",
    8: "Compression load limit for manual control exceeded.",
    9: "Machine frame load rating exceeded.",
    10: "Load has exceeded safety limit.",
    0x1B: "Load cell calibration due.",
    0x1C: "Fatal hardware fault.",
}


@dataclass
class MachineMessage:
    """Status/error message from the machine: 16-bit code plus free text.

    For codes not in :data:`MESSAGE_TEXT` the original console shows ``text``.
    """
    code: int
    text: str
    source: str = "status"      # "status", "error" or "warning"

    @property
    def description(self) -> str:
        return MESSAGE_TEXT.get(self.code, self.text)

    @classmethod
    def parse(cls, data: bytes, source: str) -> "MachineMessage":
        hi = data[0] - 256 if data[0] >= 0x80 else data[0]
        code = (hi << 8) | data[1]
        return cls(code, data[2:].split(b"\0", 1)[0].decode("latin-1"), source)


# --------------------------------------------------------------------------
# Services the machine opens on the PC (CLrStack_v3 0x40a140)
# --------------------------------------------------------------------------

def install_local_services(mux: ChannelMux,
                           on_message: Callable[[MachineMessage], None] | None = None,
                           on_debug: Callable[[bytes], None] | None = None) -> None:
    def version(m: ChannelMux, _reply: int):          # CVersionService 0x40e180
        def h(p: bytes) -> None:
            if p:
                m.send(p[0], PC_VERSION)
        return h

    def description(m: ChannelMux, _reply: int):      # CDescriptionService 0x40bed0
        def h(p: bytes) -> None:
            if len(p) >= 2:
                m.send(p[0], PC_DESCRIPTION.encode("latin-1"))
        return h

    def responder(m: ChannelMux, _reply: int):        # CResponderService 0x40d450
        def h(p: bytes) -> None:
            if p:
                m.send(p[0], p[1:])
        return h

    def error(m: ChannelMux, _reply: int):            # CErrorService 0x4089a0
        def h(p: bytes) -> None:
            if len(p) > 2:
                msg = MachineMessage.parse(p[1:], "warning" if p[0] == 2 else "error")
                log.warning("machine %s %d: %s", msg.source, msg.code, msg.description)
                if on_message:
                    on_message(msg)
        return h

    def debug2(m: ChannelMux, _reply: int):           # CWin32Debug2Service
        def h(p: bytes) -> None:
            log.debug("machine debug: %r", p)
            if on_debug:
                on_debug(p)
        return h

    mux.local_services.update({
        "Version": version, "Description": description, "Responder": responder,
        "Error": error, "Debug2": debug2,
    })
    mux.obsolete_services.add("Obsolete")


# --------------------------------------------------------------------------
# Machine services
# --------------------------------------------------------------------------

class QueuedService(ServiceClient):
    """Service whose replies are collected into a queue."""

    def __init__(self, mux: ChannelMux, name: str, timeout: float = 5.0) -> None:
        self.packets: queue.Queue[bytes] = queue.Queue()
        super().__init__(mux, name, timeout=timeout)

    def on_packet(self, payload: bytes) -> None:
        self.packets.put(payload)

    def get(self, timeout: float = 5.0) -> bytes:
        try:
            return self.packets.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"no reply from {self.name}") from None


def query_version(mux: ChannelMux, timeout: float = 5.0) -> tuple[int, int, int]:
    """Machine firmware version (major, minor, issue).  CVersionChannelHandler 0x429ab0."""
    s = QueuedService(mux, "Version", timeout)
    try:
        s.subscribe()
        p = s.get(timeout)
        if len(p) != 3:
            raise ValueError(f"version packet length bad: {p.hex(' ')}")
        return p[0], p[1], p[2]
    finally:
        s.close()


def query_description(mux: ChannelMux, timeout: float = 5.0) -> tuple[str, str, str]:
    """Three machine description strings, requested with length limits 0, 15
    and 30 (CDescriptionChannelHandler 0x422fb0 / 0x4231f0)."""
    s = QueuedService(mux, "Description", timeout)
    try:
        out = []
        for limit in (0x00, 0x0F, 0x1E):
            s.send(bytes((s.local, limit)))
        for _ in range(3):
            out.append(s.get(timeout).split(b"\0", 1)[0].decode("latin-1"))
        return out[0], out[1], out[2]
    finally:
        s.close()


@dataclass
class MachineParameters:
    """Frame limits from the "TensileParameters" service (0x424990).

    Field meaning follows the COM properties the console derives from them
    (CLimitsServer: LoadRange, ExtensionRange, MinSpeed, MaxSpeed).  The console
    reads them as N, mm, mm/s, mm/s (PROTOCOL.md §10).
    """
    load_range: float
    extension_range: float
    min_speed: float
    max_speed: float
    raw: list[str] = field(default_factory=list)


def query_parameters(mux: ChannelMux, timeout: float = 5.0) -> MachineParameters:
    s = QueuedService(mux, "TensileParameters", timeout)
    try:
        s.subscribe()
        p = s.get(timeout)
        if not 2 <= len(p) <= 100 or p[-2:] != b"\0\0":
            raise ValueError(f"TensileParameters packet bad: {p.hex(' ')}")
        fields = []
        for f in p.split(b"\0")[:4]:       # 0x424990 stops at the first empty field
            if not f:
                break
            fields.append(f)
        vals = [atof(f) for f in fields] + [float("nan")] * (4 - len(fields))
        return MachineParameters(*vals, raw=[f.decode("latin-1") for f in fields])
    finally:
        s.close()


@dataclass
class DataSourceInfo:
    """One data source from the "DataInformation" service (0x420620).

    ``index`` 0 is the load channel, 1 the crosshead extension; further
    indices are optional transducers.  ``kind`` 0 = not fitted, 1 = load,
    2 = extension, 3 = other (e.g. width).  ``range`` is max(a, b) of the two
    numeric fields, which is what the console keeps.
    """
    index: int
    kind: int
    extra: int
    a: float
    b: float
    name: str

    @property
    def range(self) -> float:
        return max(self.a, self.b)

    @classmethod
    def parse(cls, p: bytes) -> "DataSourceInfo":
        parts = p[3:].split(b"\0")
        def num(i: int) -> float:
            return atof(parts[i]) if i < len(parts) else 0.0
        name = parts[2].decode("latin-1") if len(parts) > 2 else ""
        return cls(p[0], p[1], p[2], num(0), num(1), name)


def query_data_information(mux: ChannelMux, timeout: float = 5.0) -> list[DataSourceInfo]:
    s = QueuedService(mux, "DataInformation", timeout)
    try:
        s.subscribe()
        out = []
        while True:
            p = s.get(timeout)
            if not p:
                return out
            if not 5 <= len(p) <= 0xFA:
                raise ValueError(f"DataInformation packet length bad: {p.hex(' ')}")
            out.append(DataSourceInfo.parse(p))
    finally:
        s.close()


TICK_MS = 0.9765625   # DAT_0043e9e8: ms per machine time tick (1/1024 s)


@dataclass
class Sample:
    source: int          # data source index (0 = load, 1 = extension, ...)
    ticks: int           # machine time in 1/1024 s ticks since stream start
    raw: int             # accumulated counts since the last zero
    value: float         # raw * scale

    @property
    def t(self) -> float:
        """Seconds since stream start (assuming a 1024 Hz tick)."""
        return self.ticks * TICK_MS / 1000.0


class _SourceDecoder:
    """Per-source reading stream (CDataSourcesChannelHandler 0x41ec20).

    Packets are ``[type, ...]``:
      0 data: big-endian int16 deltas; each adds to the accumulator and
        advances time by the current increment -> one sample each
      1 DS_ACCUM: int32 BE added to the accumulator (no sample)
      2 DS_ZERO: int32 BE raw zero offset; accumulator reset to 0
      3 DS_TIME: int16 BE time increment per sample (> 0)
      4 ASCII decimal scale (value units per count)
    """

    def __init__(self, index: int, sink: Callable[[Sample], None]) -> None:
        self.index = index
        self.sink = sink
        self.accum = 0
        self.ticks = 0
        self.increment = 1
        self.scale = 0.0
        self.zero_offset = 0
        self.errors = 0

    def feed(self, p: bytes) -> None:
        if not p:
            return
        kind, d = p[0], p[1:]
        if kind == 0:
            for i in range(0, len(d) - 1, 2):
                self.accum += int.from_bytes(d[i:i + 2], "big", signed=True)
                self.ticks += self.increment
                self.sink(Sample(self.index, self.ticks, self.accum, self.accum * self.scale))
        elif kind == 1 and len(d) == 4:
            self.accum += int.from_bytes(d, "big", signed=True)
        elif kind == 2 and len(d) == 4:
            self.accum = 0
            self.zero_offset = int.from_bytes(d, "big", signed=True)
        elif kind == 3 and len(d) == 2:
            inc = int.from_bytes(d, "big", signed=True)
            if inc > 0:
                self.increment = inc
            else:   # the console stores it anyway and reports "time_increment bad"
                self.errors += 1
                log.warning("source %d: bad time increment %d ignored", self.index, inc)
        elif kind == 4:
            self.scale = atof(d.split(b"\0", 1)[0])
        else:
            self.errors += 1
            log.warning("source %d: bad packet %s", self.index, p.hex(" "))


class DataSetStream:
    """Live readings: opens "DataSet" and subscribes one channel per source.

    The subscription packet is the list of our per-source channel numbers in
    data-source index order (FUN_0041e250).
    """

    def __init__(self, mux: ChannelMux, sources: list[DataSourceInfo],
                 sink: Callable[[Sample], None], timeout: float = 5.0) -> None:
        self.mux = mux
        self.decoders: dict[int, _SourceDecoder] = {}
        self._channels: list[int] = []
        unique = {s.index: s for s in sources}   # console keeps one entry per index
        for info in sorted(unique.values(), key=lambda s: s.index):
            dec = _SourceDecoder(info.index, sink)
            self.decoders[info.index] = dec
            self._channels.append(mux.allocate(dec.feed))
        try:
            self.service = ServiceClient(mux, "DataSet", timeout=timeout)
        except Exception:
            for ch in self._channels:
                mux.free(ch)
            raise
        self.service.send(bytes(self._channels))

    def close(self) -> None:
        self.service.close()
        for ch in self._channels:
            self.mux.free(ch)


class StatusMonitor(ServiceClient):
    """"StatusInformation": the machine pushes messages ``[code_hi, code_lo,
    text...]``; an empty packet ends a batch (0x428f70)."""

    def __init__(self, mux: ChannelMux, on_message: Callable[[MachineMessage], None],
                 timeout: float = 5.0) -> None:
        self.on_message = on_message
        super().__init__(mux, "StatusInformation", timeout=timeout)
        self.subscribe()

    def on_packet(self, payload: bytes) -> None:
        if len(payload) >= 2:
            msg = MachineMessage.parse(payload, "status")
            log.info("machine status %d: %s", msg.code, msg.description)
            self.on_message(msg)


class Watchdog:
    """Every ``interval`` s send ``[chan]`` to the machine's "Responder"; the
    machine echoes an empty packet back on ``chan``.  The console declares
    "Timeout waiting for machine reply" if a ping is still outstanding at the
    next tick (CMachineWatchdog 0x429e00/0x429f00, 10 s)."""

    def __init__(self, mux: ChannelMux, responder: ServiceClient,
                 on_timeout: Callable[[], None], interval: float = 10.0) -> None:
        self.mux = mux
        self.responder = responder
        self.on_timeout = on_timeout
        self.interval = interval
        self.outstanding = False
        self.last_reply = time.monotonic()
        self.chan = mux.allocate(self._on_reply)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="watchdog", daemon=True)
        self._thread.start()

    def _on_reply(self, _p: bytes) -> None:
        self.outstanding = False
        self.last_reply = time.monotonic()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if self.outstanding:
                log.error("watchdog: machine did not answer")
                self.on_timeout()
                return
            self.outstanding = True
            try:
                self.responder.send(bytes((self.chan,)))
            except Exception:
                log.exception("watchdog send failed")

    def stop(self) -> None:
        self._stop.set()
        self.mux.free(self.chan)


def set_system_time(mux: ChannelMux, when: _dt.datetime | None = None,
                    timeout: float = 5.0) -> None:
    """"SetSystemTime": NUL-terminated decimal fields year, month, day-of-week
    (0 = Sunday), day, hour, minute, second, in UTC (GetSystemTime), then the
    channel is closed (0x424020, 0x423ff0)."""
    t = when or _dt.datetime.now(_dt.timezone.utc)
    dow = (t.weekday() + 1) % 7
    fields = (t.year, t.month, dow, t.day, t.hour, t.minute, t.second)
    # "%d%c" x7 with %c = NUL, sent including snprintf's own terminator
    data = b"".join(str(v).encode() + b"\0" for v in fields) + b"\0"
    s = ServiceClient(mux, "SetSystemTime", timeout=timeout)
    try:
        s.send(data)
    finally:
        s.close()
