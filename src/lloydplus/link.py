"""Link layer: boot handshake and the reliable "transmission" protocol.

Each frame body starts with a protocol id (CProtocolMultiplex 0x40d090):

* ``0xCE`` BOOT — 1 byte payload, the sender's link state 0/1/2.  While a side
  is below state 2 it keeps sending its state.  Transition table
  (CBoot 0x40b1e0, table at 0x44ec50), ``me`` = my state, ``rx`` = received::

      me=0: rx 0/1 -> me=1
      me=1: rx 1/2 -> me=2 (connected)
      me=2: rx 1   -> reply 2 ; rx 0 -> peer restarted, reset the link

* ``0x02`` TRANSMISSION — payload ``[ack, seq, data...]`` or just ``[ack]``.
  Accepted only once connected.  ``seq`` is assigned when a packet is queued
  and starts at 0; ``ack`` is the seq of the last packet delivered in order
  (also starts at 0).  The receiver keeps an 80-packet reorder window and
  ignores anything older.  The sender drops a queued packet once the peer's
  ack *changes* to a value at or after it (CTransmission 0x40db90) — note an
  ack equal to the initial value never releases anything, exactly like the
  original.  Max data per packet 252 bytes (0x40d7c0).
* ``0x05`` ECHO (payload returned as proto 0x04), ``0x04``/``0x06`` discarded.

The original PC console streams continuously (pure acks when idle) and
resends unacknowledged packets round-robin.  Here an idle ack is sent at a
fixed interval and unacked packets are resent after a timeout.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Callable, Protocol

from .framing import FrameDecoder, encode_frame

log = logging.getLogger(__name__)

PROTO_TRANSMISSION = 0x02
PROTO_RAW = 0x04
PROTO_ECHO = 0x05
PROTO_DISCARD = 0x06
PROTO_BOOT = 0xCE

WINDOW = 0x50           # reorder / ack window used by the original
MAX_DATA = 0xFC         # max transmission data bytes per packet
_RELEASE_LIMIT = 0xAF   # (ack - seq) mod 256 <= this  ->  acknowledged


class Port(Protocol):
    """What the link needs from a serial port (pyserial.Serial satisfies it)."""

    def read(self, size: int = 1) -> bytes: ...
    def write(self, data: bytes) -> int | None: ...


class LinkReset(Exception):
    pass


class Link:
    """Symmetric link endpoint.  Used for the PC side and for the simulator."""

    def __init__(
        self,
        port: Port,
        on_data: Callable[[bytes], None],
        *,
        on_connect: Callable[[], None] | None = None,
        on_reset: Callable[[], None] | None = None,
        boot_interval: float = 0.05,
        idle_ack_interval: float = 0.1,
        retransmit_timeout: float = 0.3,
        max_in_flight: int = 16,
        name: str = "link",
    ) -> None:
        self.port = port
        self.on_data = on_data
        self.on_connect = on_connect
        self.on_reset = on_reset
        self.boot_interval = boot_interval
        self.idle_ack_interval = idle_ack_interval
        self.retransmit_timeout = retransmit_timeout
        self.max_in_flight = max_in_flight
        self.name = name

        self._lock = threading.RLock()
        self._wlock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.connected = threading.Event()
        self._decoder = FrameDecoder()
        self._threads: list[threading.Thread] = []
        self.stats = {"tx_frames": 0, "rx_frames": 0, "retransmits": 0,
                      "rx_duplicates": 0, "resets": 0}
        self._reset_state()

    # -- state ------------------------------------------------------------

    def _reset_state(self) -> None:
        self.boot_state = 0
        self._last_boot_tx = 0.0
        self._tx_seq = 0
        self._queue: deque[tuple[int, bytes]] = deque()        # not yet sent
        self._unacked: deque[list] = deque()                   # [seq, data, t_sent]
        self._peer_ack = 0
        self._rx_ack = 0            # what we acknowledge
        self._rx_expected = 0
        self._reorder: dict[int, bytes] = {}
        self._ack_dirty = False
        self._last_tx = 0.0
        self.connected.clear()

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        for target, nm in ((self._rx_loop, "rx"), (self._tx_loop, "tx")):
            t = threading.Thread(target=target, name=f"{self.name}-{nm}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for t in self._threads:
            t.join(timeout=2)
        self._threads.clear()

    def wait_connected(self, timeout: float | None = None) -> bool:
        return self.connected.wait(timeout)

    # -- sending ----------------------------------------------------------

    def send(self, data: bytes) -> None:
        """Queue ``data`` (``[channel, ...]``) for reliable delivery."""
        if not data:
            raise ValueError("empty transmission packet")
        if len(data) > MAX_DATA:
            raise ValueError(f"packet too long ({len(data)} > {MAX_DATA})")
        with self._lock:
            if self.boot_state != 2:
                raise LinkReset("link not connected")
            seq = self._tx_seq
            self._tx_seq = (self._tx_seq + 1) & 0xFF
            self._queue.append((seq, bytes(data)))
        self._wake.set()

    def _write_frame(self, body: bytes) -> None:
        frame = encode_frame(body)
        with self._wlock:
            self.port.write(frame)
        self.stats["tx_frames"] += 1
        self._last_tx = time.monotonic()

    def _tx_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(0.01)
            self._wake.clear()
            try:
                self._tx_step()
            except Exception:  # pragma: no cover - keep the link alive
                log.exception("%s: tx error", self.name)
                time.sleep(0.1)

    def _tx_step(self) -> None:
        now = time.monotonic()
        frames: list[bytes] = []
        with self._lock:
            if self.boot_state < 2:
                if now - self._last_boot_tx >= self.boot_interval:
                    self._last_boot_tx = now
                    frames.append(bytes((PROTO_BOOT, self.boot_state)))
            else:
                ack = self._rx_ack
                # new packets
                while self._queue and len(self._unacked) < self.max_in_flight:
                    seq, data = self._queue.popleft()
                    self._unacked.append([seq, data, now])
                    frames.append(bytes((PROTO_TRANSMISSION, ack, seq)) + data)
                # retransmit the oldest unacked packet that timed out
                if not frames and self._unacked:
                    oldest = self._unacked[0]
                    if now - oldest[2] >= self.retransmit_timeout:
                        self._unacked.rotate(-1)
                        oldest[2] = now
                        frames.append(bytes((PROTO_TRANSMISSION, ack, oldest[0])) + oldest[1])
                        self.stats["retransmits"] += 1
                if not frames and (self._ack_dirty or now - self._last_tx >= self.idle_ack_interval):
                    frames.append(bytes((PROTO_TRANSMISSION, ack)))
                if frames:
                    self._ack_dirty = False
        for body in frames:
            self._write_frame(body)

    # -- receiving --------------------------------------------------------

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                chunk = self.port.read(4096)
            except Exception:
                if self._stop.is_set():
                    break
                log.exception("%s: read error", self.name)
                time.sleep(0.2)
                continue
            if not chunk:
                continue
            for body in self._decoder.feed(chunk):
                self.stats["rx_frames"] += 1
                try:
                    self._on_body(body)
                except Exception:
                    log.exception("%s: error handling frame %s", self.name, body.hex(" "))

    def _on_body(self, body: bytes) -> None:
        if len(body) < 2:          # CMultiProtocol 0x40c6e0 needs proto + 1 byte
            return
        proto, payload = body[0], body[1:]
        if proto == PROTO_BOOT:
            if len(payload) == 1 and payload[0] <= 2:
                self._on_boot(payload[0])
        elif proto == PROTO_TRANSMISSION:
            if self.boot_state == 2:
                self._on_transmission(payload)
        elif proto == PROTO_ECHO:
            # CEcho 0x40c000 answers with the same payload as proto 0x04 (RAW),
            # which is discarded on receipt, so echoes never ping-pong
            self._write_frame(bytes((PROTO_RAW,)) + payload)
        else:
            log.debug("%s: discarding proto %#x", self.name, proto)

    def _on_boot(self, rx: int) -> None:
        reply = None
        became_connected = reset = False
        with self._lock:
            me = self.boot_state
            if me == 0 and rx in (0, 1):
                self.boot_state = 1
            elif me == 1 and rx in (1, 2):
                self.boot_state = 2
                became_connected = True
            elif me == 2 and rx == 1:
                reply = bytes((PROTO_BOOT, 2))
            elif me == 2 and rx == 0:
                reset = True
                self.stats["resets"] += 1
                self._reset_state()
                self.boot_state = 1     # we have just seen the peer's state 0
        if reply:
            self._write_frame(reply)
        self._wake.set()
        if reset:
            log.warning("%s: peer restarted the link", self.name)
            if self.on_reset:
                self.on_reset()
        if became_connected:
            self.connected.set()
            log.info("%s: link connected", self.name)
            if self.on_connect:
                self.on_connect()

    def _on_transmission(self, payload: bytes) -> None:
        deliver: list[bytes] = []
        with self._lock:
            if payload:
                ack = payload[0]
                if ((ack - self._peer_ack) & 0xFF) < WINDOW and ack != self._peer_ack:
                    self._peer_ack = ack
                    self._unacked = deque(
                        p for p in self._unacked if ((ack - p[0]) & 0xFF) > _RELEASE_LIMIT)
            if len(payload) >= 2:
                seq, data = payload[1], payload[2:]
                off = (seq - self._rx_expected) & 0xFF
                if off >= WINDOW:
                    self.stats["rx_duplicates"] += 1
                    self._ack_dirty = True          # peer may have missed our ack
                else:
                    self._reorder[seq] = data
                    while self._rx_expected in self._reorder:
                        d = self._reorder.pop(self._rx_expected)
                        if d:
                            self._rx_ack = self._rx_expected
                            deliver.append(d)
                        self._rx_expected = (self._rx_expected + 1) & 0xFF
                    self._ack_dirty = True
        if deliver:
            self._wake.set()
        for d in deliver:
            self.on_data(d)
