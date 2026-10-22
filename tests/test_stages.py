import logging
import time

import pytest

from lloydplus import Drive, LloydPlus, StageError
from lloydplus import stages as st
from lloydplus.sim import SimMachine, port_pair


# -- encoders ----------------------------------------------------------------

@pytest.mark.parametrize("x, text", [
    (50.0, b"5.000000e+001"),
    (5.0, b"5.000000e+000"),
    (100 / 60, b"1.666667e+000"),
    (0.0, b"0.000000e+000"),
    (-0.5, b"-5.000000e-001"),
    (1e-7, b"1.000000e-007"),
    (1e120, b"1.000000e+120"),
])
def test_fmt_e_matches_msvc(x, text):
    assert st.fmt_e(x) == text


def test_fmt_e_rejects_nan():
    with pytest.raises(ValueError):
        st.fmt_e(float("nan"))


def test_worked_example_stage_bytes():
    # PROTOCOL.md §11.6 worked example: 50 mm at 100 mm/min, slot 16, source 1
    assert st.motion_stage(16, 100 / 60, 1, 50.0) == (
        b"\x10" b"1.666667e+000\x00" b"\x01" b"5.000000e+001\x00" b"\xff" b"0.000000e+000\x00")


def test_small_stage_encoders():
    assert st.grip_protection_stage(4, 1000, 500) == b"\x041.000000e+003\x00-5.000000e+002\x00"
    assert st.manual_stiffness_stage(5, 2.5) == b"\x052.500000e+000\x00"
    assert st.datasource_range_stage(6, 2, 10) == b"\x06\x021.000000e+001\x00"
    assert st.zero_readings_stage(9, [0, 1]) == b"\x09\xff\xff"
    assert st.zero_readings_stage(9, [2]) == b"\x09\x00\x00\xff"


def test_clamp_speed_matches_console():
    assert st.clamp_speed(50, 0.01, 30) == 30
    assert st.clamp_speed(5, 0.01, 30) == 5
    assert st.clamp_speed(0.005, 0.01, 30) == 0.01
    assert st.clamp_speed(0.0009, 0.01, 30) == 0.0


def test_table_constants():
    assert st.CONNECT_NEXT == bytes.fromhex("00 02 01 04 05 06 07 07 01 07 07 08")
    assert len(st.RELEASE_ACTIVE) == len(st.STOP_ACTIVE) == len(st._SEGMENT_ACTIVE) == 32
    assert st.RELEASE_ACTIVE[:12] == bytes.fromhex("00 01 02 01 08 08 08 08 08 08 0b 0b")
    assert st.RELEASE_ACTIVE[31] == 7
    assert st.STOP_ACTIVE[:12] == bytes.fromhex("00 01 02 03 04 05 06 07 08 09 07 08")


# -- against the simulator ----------------------------------------------------

def make(**sim_kw):
    pc_port, sim_port = port_pair()
    sim = SimMachine(sim_port, **sim_kw)
    sim.start()
    m = LloydPlus(pc_port, watchdog_interval=0)
    m.connect(timeout=3)
    return m, sim


@pytest.fixture
def pair():
    m, sim = make()
    yield m, sim
    m.close()
    sim.stop()


def take(m):
    m.control.take_control(tensile_limit=900, compressive_limit=800)


def wait_until(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_connect_installs_layout(pair):
    m, sim = pair
    c = m.control
    assert c is not None
    assert wait_until(lambda: c.slot == st.SLOT_WAIT_SHARED)
    assert sim.next[:12] == list(st.CONNECT_NEXT)
    assert sim.stages[1][0] == st.WAIT_EXCLUSIVE and sim.stages[8][0] == st.RELEASE_EXCLUSIVE
    assert sim.stages[15][0] == sim.stages[19][0] == st.DRIVE_NOWHERE
    assert sim.stages[9] == (st.ZERO_READINGS, b"\xff\xff")
    assert sim.stages[10][1] == st.motion_stage(10, 33.8667, 1, 0.0)[1:]
    # permanent factories were closed, the per-test ones kept
    assert st.CROSSHEAD_POSITIONING not in c.factories and st.CROSSHEAD_RATE in c.factories
    assert sim.position == 0.0


def test_take_and_release_control(pair):
    m, sim = pair
    take(m)
    assert m.control.ready and m.control.in_control
    assert sim.grip_limits == (900.0, -800.0)
    m.control.release_control()
    assert m.control.slot == st.SLOT_WAIT_SHARED and not m.control.in_control


def test_drive_to_extension(pair):
    m, sim = pair
    take(m)
    seg = m.control.drive(Drive.to_extension(5.0, 10.0), timeout=5)
    assert seg.base == 16 and seg.reason == "limit reached"
    assert m.control.slot == 19                          # DriveNowhere: stopped, holding
    assert sim.position == pytest.approx(5.0)
    assert wait_until(lambda: m.extension == pytest.approx(5.0, abs=1e-3))
    sent = [p for n, p in sim.factory_packets if n == st.CROSSHEAD_RATE]
    assert sent[-1] == st.motion_stage(16, 10.0, 1, 5.0)
    assert sim.arena_packets[-2] == bytes((2, 3, 16, 19, 19, 19))


def test_stop_zero_and_return(pair):
    m, sim = pair
    take(m)
    m.control.drive(Drive.to_extension(4.0, 20.0), timeout=5)
    m.control.zero()                              # stops first, then zeroes
    assert m.control.ready
    assert wait_until(lambda: m.extension == pytest.approx(0.0, abs=1e-3))
    m.control.start(Drive.to_extension(7.0, 20.0))
    assert wait_until(lambda: sim.position > 5.0)
    m.control.stop()
    assert m.control.ready
    m.control.return_to_zero(timeout=5)
    assert sim.position == pytest.approx(4.0)     # 0 mm reading = where we zeroed
    assert m.control.ready


def test_hold_then_next(pair):
    m, sim = pair
    take(m)
    seg = m.control.start(Drive.to_extension(2.0, 20.0, hold=0.3),
                          Drive.to_extension(1.0, 20.0))
    assert seg.holding.wait(3)
    t0 = time.monotonic()
    assert seg.next_started.wait(3)
    assert time.monotonic() - t0 >= 0.25
    assert seg.wait(3) and seg.reason == "limit reached"
    assert sim.position == pytest.approx(1.0)
    assert sim.next[16:19] == [17, 18, 19]


def test_load_limit_and_load_rate(pair):
    m, sim = pair
    take(m)
    m.control.drive(Drive.to_load(20.0, 10.0), timeout=5)
    assert sim.load == pytest.approx(20.0)
    seg = m.control.drive(Drive.at_load_rate(50.0, 200.0), timeout=5)
    assert seg.base == 12                         # banks alternate
    assert sim.load == pytest.approx(50.0)
    assert [n for n, _ in sim.factory_packets][-1] == st.LOAD_RATE


def test_new_segment_preempts(pair):
    m, sim = pair
    take(m)
    slow = m.control.start(Drive.to_extension(30.0, 1.0))
    assert slow.started.wait(2)
    fast = m.control.start(Drive.to_extension(-1.0, 20.0))
    assert slow.wait(2) and slow.reason == "superseded"
    assert fast.wait(3) and fast.reason == "limit reached"
    assert sim.position == pytest.approx(-1.0)


def test_speed_limits(pair, caplog):
    m, sim = pair
    take(m)
    with pytest.raises(ValueError):
        m.control.start(Drive.to_extension(1.0, 1e-6))
    with caplog.at_level(logging.WARNING, logger="lloydplus.stages"):
        m.control.drive(Drive.to_extension(1.0, 500.0), timeout=5)
    assert "limited" in caplog.text
    assert [p for n, p in sim.factory_packets if n == st.CROSSHEAD_RATE][-1] == \
        st.motion_stage(16, 33.8667, 1, 1.0)


def test_must_take_control_first(pair):
    m, sim = pair
    with pytest.raises(StageError):
        m.control.start(Drive.to_extension(1.0, 1.0))
    with pytest.raises(StageError):
        m.control.zero()


def test_gain_fails_in_local_mode():
    m, sim = make(remote=False)
    try:
        with pytest.raises(StageError, match="could not be gained"):
            take(m)
        sim.set_remote(True)
        assert wait_until(lambda: m.control.slot == st.SLOT_WAIT_SHARED)
        take(m)
        assert m.control.ready
    finally:
        m.close()
        sim.stop()


def test_optional_stages_missing():
    m, sim = make(fitted=set())
    try:
        assert st.LOAD_RATE in m.control.missing
        with pytest.raises(StageError):
            m.control.take_control(tensile_limit=1, compressive_limit=1, stiffness=2.0)
        take(m)
        with pytest.raises(StageError):
            m.control.start(Drive.at_load_rate(10, 10))
        with pytest.raises(StageError):
            m.control.start(Drive.to_load(10, 1.0, hold=1.0))
    finally:
        m.close()
        sim.stop()


def test_close_releases_control():
    m, sim = make()
    take(m)
    m.control.start(Drive.to_extension(30.0, 1.0))
    m.close()
    try:
        assert wait_until(lambda: sim.current == st.SLOT_WAIT_SHARED)
    finally:
        sim.stop()
