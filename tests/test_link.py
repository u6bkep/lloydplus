import queue
import random
import threading
import time

from lloydplus.link import Link
from lloydplus.sim import PipePort


class LossyPort(PipePort):
    """Drops/corrupts a fraction of written chunks."""

    def __init__(self, rx, tx, rng, loss=0.2):
        super().__init__(rx, tx)
        self.rng, self.loss = rng, loss

    def write(self, data):
        r = self.rng.random()
        if r < self.loss / 2:
            return len(data)                       # dropped
        if r < self.loss:
            b = bytearray(data)
            b[self.rng.randrange(len(b))] ^= 0x10   # corrupted
            data = bytes(b)
        return super().write(data)


def _pair(loss=0.0, seed=0):
    rng = random.Random(seed)
    a, b = queue.Queue(), queue.Queue()
    pa, pb = LossyPort(a, b, rng, loss), LossyPort(b, a, rng, loss)
    got_a, got_b = [], []
    la = Link(pa, got_a.append, name="a", retransmit_timeout=0.05)
    lb = Link(pb, got_b.append, name="b", retransmit_timeout=0.05)
    return la, lb, got_a, got_b


def test_handshake_and_ordered_delivery():
    la, lb, got_a, got_b = _pair()
    la.start(); lb.start()
    try:
        assert la.wait_connected(2) and lb.wait_connected(2)
        msgs = [bytes([i % 250 + 1]) * (i % 40 + 1) for i in range(300)]
        for m in msgs:
            la.send(m)
            lb.send(m)
        deadline = time.time() + 10
        while (len(got_a) < 300 or len(got_b) < 300) and time.time() < deadline:
            time.sleep(0.02)
        assert got_b == msgs and got_a == msgs
    finally:
        la.stop(); lb.stop()


def test_lossy_line():
    la, lb, got_a, got_b = _pair(loss=0.25, seed=3)
    la.start(); lb.start()
    try:
        assert la.wait_connected(5) and lb.wait_connected(5)
        msgs = [bytes([7, i]) for i in range(150)]
        for m in msgs:
            la.send(m)
        deadline = time.time() + 20
        while len(got_b) < 150 and time.time() < deadline:
            time.sleep(0.02)
        assert got_b == msgs
    finally:
        la.stop(); lb.stop()
