"""Byte framing and fault-control layer of the Lloyd "Plus" PC link.

Wire format of one frame (reverse engineered from NEXYGEN 4.1 LrCon.exe,
CFraming 0x40c2e0/0x40c410 and CFaultControl 0x40c070/0x40c170)::

    0xDC  escape( crc_hi crc_lo len body[len] )

* 0xDC starts a frame.  Inside a frame the bytes 0xDC and 0xDD are sent as
  0xDD followed by (byte ^ 0x80).
* ``crc`` is CRC-16/ARC (reflected poly 0xA001, init 0) over ``body``,
  transmitted big-endian.  ``len`` is ``len(body)``.
* ``body[0]`` is the protocol id (see :mod:`lloydplus.link`).

The original receiver only delivers a frame when the *next* 0xDC arrives.
This decoder additionally delivers as soon as the length byte and CRC say
the frame is complete, so a trailing frame is never held back.
"""

from __future__ import annotations

FRAME_START = 0xDC
ESCAPE = 0xDD
MAX_BODY = 0xFF
_MAX_RAW = 0x102  # receive buffer limit in the original (CFraming)


def _make_table() -> list[int]:
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
        table.append(c)
    return table


_CRC_TABLE = _make_table()


def crc16_arc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = _CRC_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
    return crc


def encode_frame(body: bytes) -> bytes:
    """Wrap a protocol body (``[proto, payload...]``) into wire bytes."""
    if not 1 <= len(body) <= MAX_BODY:
        raise ValueError(f"frame body length {len(body)} out of range")
    crc = crc16_arc(body)
    raw = bytes((crc >> 8, crc & 0xFF, len(body))) + body
    out = bytearray((FRAME_START,))
    for b in raw:
        if b == FRAME_START or b == ESCAPE:
            out += bytes((ESCAPE, b ^ 0x80))
        else:
            out.append(b)
    return bytes(out)


class FrameDecoder:
    """Incremental decoder: feed raw serial bytes, get validated bodies."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._escaped = False
        self._done = False  # current frame already delivered/abandoned
        self.crc_errors = 0
        self.length_errors = 0
        self.frames = 0

    def feed(self, data: bytes) -> list[bytes]:
        bodies: list[bytes] = []
        for b in data:
            if b == FRAME_START:
                if self._buf and not self._done:
                    self._finish(bodies, final=True)
                self._buf.clear()
                self._escaped = False
                self._done = False
                continue
            if self._done:
                continue
            if b == ESCAPE:
                self._escaped = True
                continue
            if self._escaped:
                b ^= 0x80
                self._escaped = False
            if len(self._buf) >= _MAX_RAW:
                self.length_errors += 1
                self._done = True
                continue
            self._buf.append(b)
            if len(self._buf) >= 4 and len(self._buf) - 3 == self._buf[2]:
                self._finish(bodies, final=False)
        return bodies

    def _finish(self, bodies: list[bytes], final: bool) -> None:
        buf = self._buf
        if len(buf) < 4 or len(buf) - 3 != buf[2]:
            if final:
                self.length_errors += 1
            return
        body = bytes(buf[3:])
        if crc16_arc(body) != (buf[0] << 8 | buf[1]):
            self.crc_errors += 1
            self._done = True
            return
        self.frames += 1
        self._done = True
        bodies.append(body)
