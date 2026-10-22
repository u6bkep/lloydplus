"""Channel multiplexer and named-service connections (CChannel 0x40b2f0..).

Every reliable packet is ``[channel, bytes...]``.

Channel 0 is the control channel:

* ``[reply_chan] + b"ServiceName"`` asks the peer to open a service.  The peer
  answers *on reply_chan* with ``[status, service_chan]``; status 0 = ok,
  1 = obsolete, 2 = creation failed, 3 = unknown service.
* ``[chan]`` (a single byte) closes ``chan`` on the receiving side.

After opening, requests go to ``service_chan``.  Nearly every machine service
then expects one byte naming the channel on which *we* want to receive its
output ("subscribe"); the open-reply channel itself is temporary.

Channel numbers are never 0, 0xDC or 0xDD.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from .link import Link

log = logging.getLogger(__name__)

STATUS_OK = 0
STATUS_OBSOLETE = 1
STATUS_FAILED = 2
STATUS_UNKNOWN = 3
_STATUS_TEXT = {1: "obsolete", 2: "creation failed", 3: "unknown service"}

Handler = Callable[[bytes], None]


class ServiceOpenError(Exception):
    def __init__(self, name: str, status: int | None):
        self.name = name
        self.status = status
        why = "timeout" if status is None else _STATUS_TEXT.get(status, f"status {status}")
        super().__init__(f"could not open service {name!r}: {why}")


def _usable(ch: int) -> bool:
    return ch != 0 and ch not in (0xDC, 0xDD)


class ChannelMux:
    def __init__(self, link: Link) -> None:
        self.link = link
        self._lock = threading.Lock()
        self._handlers: dict[int, Handler] = {}
        self._closers: dict[int, Callable[[], None]] = {}
        self._next = 2   # the console seeds its search at 1 and hands out 2 first
        # factories for services the *peer* may open on us
        self.local_services: dict[str, Callable[["ChannelMux", int], Handler | None]] = {}
        self.obsolete_services: set[str] = set()

    # -- channel numbers --------------------------------------------------

    def allocate(self, handler: Handler, on_close: Callable[[], None] | None = None) -> int:
        with self._lock:
            for _ in range(256):
                ch = self._next
                self._next = (self._next + 1) & 0xFF
                if _usable(ch) and ch not in self._handlers:
                    self._handlers[ch] = handler
                    if on_close:
                        self._closers[ch] = on_close
                    return ch
        raise RuntimeError("no free channel numbers")

    def free(self, ch: int) -> None:
        with self._lock:
            self._handlers.pop(ch, None)
            self._closers.pop(ch, None)

    def reset(self) -> None:
        with self._lock:
            self._handlers.clear()
            self._closers.clear()
            self._next = 2

    # -- traffic ----------------------------------------------------------

    def send(self, ch: int, data: bytes = b"") -> None:
        self.link.send(bytes((ch,)) + bytes(data))

    def close_remote(self, remote_ch: int) -> None:
        """Tell the peer to close its end of a service channel."""
        self.send(0, bytes((remote_ch,)))

    def on_data(self, data: bytes) -> None:
        ch, payload = data[0], data[1:]
        if ch == 0:
            with self._lock:
                handled = 0 in self._handlers
            if not handled:
                self._on_control(payload)
                return
        with self._lock:
            h = self._handlers.get(ch)
        if h is None:
            log.debug("data for unknown channel %d: %s", ch, payload.hex(" "))
            return
        h(payload)

    def _on_control(self, payload: bytes) -> None:
        if len(payload) == 1:
            ch = payload[0]
            with self._lock:
                closer = self._closers.pop(ch, None)
                self._handlers.pop(ch, None)
            log.debug("peer closed channel %d", ch)
            if closer:
                closer()
            return
        if len(payload) < 2:
            return
        reply_ch, name = payload[0], payload[1:].decode("latin-1")
        status, ch = STATUS_UNKNOWN, 0
        factory = self.local_services.get(name)
        if name in self.obsolete_services:
            status = STATUS_OBSOLETE
        elif factory is not None:
            status = STATUS_FAILED
            handler = factory(self, reply_ch)
            if handler is not None:
                ch = self.allocate(handler)
                status = STATUS_OK
        log.info("peer opened local service %r -> status %d chan %d", name, status, ch)
        self.send(reply_ch, bytes((status, ch)))

    # -- opening services on the peer --------------------------------------

    def open_service(self, name: str, timeout: float = 5.0) -> int:
        """Open ``name`` on the peer and return the peer's service channel."""
        done = threading.Event()
        result: list[int] = []

        def on_reply(p: bytes) -> None:
            if len(p) == 2 and not done.is_set():
                result[:] = [p[0], p[1]]
                done.set()

        rch = self.allocate(on_reply)
        try:
            self.send(0, bytes((rch,)) + name.encode("latin-1"))
            if not done.wait(timeout):
                raise ServiceOpenError(name, None)
        finally:
            self.free(rch)
        status, svc = result
        if status != STATUS_OK:
            raise ServiceOpenError(name, status)
        log.info("opened %r on peer channel %d", name, svc)
        return svc


class ServiceClient:
    """A machine service we opened, with our own receive channel."""

    def __init__(self, mux: ChannelMux, name: str, handler: Handler | None = None,
                 timeout: float = 5.0) -> None:
        self.mux = mux
        self.name = name
        self.closed = threading.Event()
        self.local = mux.allocate(handler or self.on_packet, on_close=self._on_remote_close)
        try:
            self.remote = mux.open_service(name, timeout)
        except Exception:
            mux.free(self.local)
            raise

    def subscribe(self) -> None:
        """Send our receive channel number to the service."""
        self.send(bytes((self.local,)))

    def send(self, data: bytes) -> None:
        self.mux.send(self.remote, data)

    def on_packet(self, payload: bytes) -> None:  # pragma: no cover - override
        log.debug("%s: %s", self.name, payload.hex(" "))

    def _on_remote_close(self) -> None:
        self.closed.set()

    def close(self) -> None:
        if not self.closed.is_set():
            self.closed.set()
            try:
                self.mux.close_remote(self.remote)
            except Exception:
                pass
        self.mux.free(self.local)
