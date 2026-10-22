import time

import pytest

from lloydplus import LloydPlus
from lloydplus.sim import SimMachine, port_pair


@pytest.fixture
def pair():
    pc_port, sim_port = port_pair()
    sim = SimMachine(sim_port)
    sim.start()
    m = LloydPlus(pc_port, watchdog_interval=0.3)
    yield m, sim
    m.close()
    sim.stop()


def test_connect_and_info(pair):
    m, sim = pair
    m.connect(timeout=3)
    assert m.version == (2, 1, 7)
    assert m.description == ("LF", "LF Plus", "Lloyd Instruments LF Plus")
    assert m.parameters.load_range == 1000.0
    assert m.parameters.max_speed == pytest.approx(0.0166667)
    assert [(s.index, s.kind, s.name, s.range) for s in m.sources] == [
        (0, 1, "Load", 1000.0), (1, 2, "Extension", 1.1)]
    assert sim.system_time is not None and sim.system_time.endswith(b"\0\0")


def test_stream(pair):
    m, sim = pair
    m.connect(timeout=3)
    got = []
    deadline = time.time() + 3
    for s in m.samples(timeout=1):
        got.append(s)
        if len(got) >= 80 or time.time() > deadline:
            break
    loads = [s for s in got if s.source == 0]
    exts = [s for s in got if s.source == 1]
    assert loads and exts
    assert loads[0].ticks == 10 and loads[1].ticks == 20
    assert exts[3].raw == 15 and exts[3].value == pytest.approx(15e-6)
    assert m.extension is not None


def test_status_and_watchdog(pair):
    m, sim = pair
    m.connect(timeout=3)
    sim.push_status(2)
    time.sleep(1.0)            # also lets the 0.3 s watchdog cycle a few times
    assert m.messages and m.messages[0].code == 2
    assert "Upper extension limit" in m.messages[0].description
    assert not m.lost.is_set()
