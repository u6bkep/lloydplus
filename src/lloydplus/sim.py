"""Protocol-level simulator of a Plus-series machine, for tests and demos.

It implements the machine side as understood from the PC console's code, so
it validates this library's self-consistency, not the real firmware.  Its
link layer is the same symmetric :class:`~lloydplus.link.Link`.
"""

from __future__ import annotations

import math
import queue
import threading
import time

from .channels import ChannelMux
from .link import Link


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


class SimMachine:
    def __init__(self, port, *, version=(2, 1, 7),
                 descriptions=("LF", "LF Plus", "Lloyd Instruments LF Plus"),
                 sample_period_ticks: int = 10) -> None:
        self.version = bytes(version)
        self.descriptions = descriptions
        self.sample_period_ticks = sample_period_ticks
        self.link = Link(port, self._on_data, name="sim")
        self.mux = ChannelMux(self.link)
        self.status_channel: int | None = None
        self.keypad: list[int] = []
        self.system_time: bytes | None = None
        self.load_counts = 0.0
        self._stream_stop = threading.Event()
        self._stream_thread: threading.Thread | None = None
        self.sources = [
            # index, kind, extra, a, b, name
            (0, 1, 0, "-1000", "1000", "Load"),
            (1, 2, 0, "-1.1", "1.1", "Extension"),
        ]
        self.scales = {0: "0.01", 1: "0.000001"}
        self._install()

    def _on_data(self, data: bytes) -> None:
        self.mux.on_data(data)

    def start(self) -> None:
        self.link.start()

    def stop(self) -> None:
        self._stream_stop.set()
        self.link.stop()

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
            return b"\0".join(s.encode() for s in ("1000", "1.1", "0.0000002", "0.0166667")) + b"\0\0"

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
        })

    def push_status(self, code: int, text: str = "") -> None:
        if self.status_channel is None:
            raise RuntimeError("PC has not subscribed to StatusInformation")
        self.mux.send(self.status_channel, bytes(((code >> 8) & 0xFF, code & 0xFF)) + text.encode())
        self.mux.send(self.status_channel, b"")

    def _start_stream(self, channels: list[int]) -> None:
        def run():
            mux, n = self.mux, self.sample_period_ticks
            for i, ch in enumerate(channels):
                mux.send(ch, b"\x04" + self.scales.get(i, "1").encode() + b"\0")
                mux.send(ch, b"\x03" + n.to_bytes(2, "big"))
                mux.send(ch, b"\x02" + (12345).to_bytes(4, "big", signed=True))
            prev = [0] * len(channels)
            k = 0
            while not self._stream_stop.wait(0.02):
                for i, ch in enumerate(channels):
                    deltas = bytearray()
                    for j in range(4):
                        x = int(round(1000 * math.sin((k + j) / 20.0))) if i == 0 else (k + j) * 5
                        d = x - prev[i]
                        prev[i] = x
                        deltas += d.to_bytes(2, "big", signed=True)
                    mux.send(ch, b"\x00" + bytes(deltas))
                k += 4
        self._stream_thread = threading.Thread(target=run, name="sim-stream", daemon=True)
        self._stream_thread.start()
