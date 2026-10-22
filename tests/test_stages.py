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
    assert len(st.HALT_ACTIVE) == 32 and st.HALT_ACTIVE[:10] == bytes(range(10))
    assert st.HALT_ACTIVE[10:16] == bytes((15,) * 6)       # returns and bank A -> DriveNowhere 15
    assert st.HALT_ACTIVE[16:20] == bytes((19,) * 4)       # bank B -> DriveNowhere 19
    assert st.HALT_ACTIVE[20:] == bytes((7,) * 12)


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


# -- regressions from review ---------------------------------------------------

def test_back_to_back_starts_track_the_running_segment(pair):
    m, sim = pair
    take(m)
    b = m.control.start(Drive.to_extension(10.0, 1.0))
    c = m.control.start(Drive.to_extension(-0.5, 5.0, hold=0.2))
    assert b.wait(2) and b.reason == "superseded"
    assert c.holding.wait(3)                     # c really is tracked: hold timer runs
    assert c.wait(3) and c.reason == "limit reached"
    assert sim.current == 15 and sim.position == pytest.approx(-0.5)


def test_start_while_previous_limit_report_in_flight(pair):
    m, sim = pair
    take(m)
    a = m.control.start(Drive.to_extension(30.0, 1.0))
    assert a.started.wait(2)
    with sim._lock:                              # freeze the sim's receive handler
        b = m.control.start(Drive.to_extension(-0.5, 5.0))
        time.sleep(0.2)
        sim._goto(19)                            # a reaches its limit first
        time.sleep(0.2)
    assert a.wait(2) and a.reason == "limit reached"
    assert not b.finished.is_set() or b.reason == "limit reached"
    assert b.wait(3) and b.reason == "limit reached"
    assert sim.position == pytest.approx(-0.5)


def test_link_loss_wakes_waiters(pair):
    m, sim = pair
    take(m)
    seg = m.control.start(Drive.to_extension(30.0, 1.0))
    assert seg.started.wait(2)
    m._on_link_reset()
    assert seg.wait(1) and seg.reason == "link lost"
    with pytest.raises(StageError, match="lost"):
        m.control.return_to_zero(timeout=None)


def test_take_control_again_from_finished_segment(pair):
    m, sim = pair
    take(m)
    m.control.drive(Drive.to_extension(1.0, 20.0), timeout=5)
    n = len(sim.transitions)
    take(m)                                      # same settings: just stops to ready
    assert m.control.ready and sim.position == pytest.approx(1.0)
    assert all(new not in (12, 16) for _, new in sim.transitions[n:])   # no re-run
    m.control.take_control(tensile_limit=100, compressive_limit=100)    # new limits
    assert m.control.ready and sim.grip_limits == (100.0, -100.0)


def test_take_release_take(pair):
    m, sim = pair
    take(m)
    m.control.release_control()
    take(m)
    time.sleep(0.2)
    assert m.control.ready and sim.current == st.SLOT_READY


def test_drive_timeout_stops(pair):
    m, sim = pair
    take(m)
    with pytest.raises(StageError, match="stopped"):
        m.control.drive(Drive.to_extension(30.0, 1.0), timeout=0.3)
    assert m.control.ready


def test_stop_right_after_start_waits_for_it(pair):
    m, sim = pair
    take(m)
    seg = m.control.start(Drive.to_extension(30.0, 1.0))
    m.control.stop()
    assert m.control.ready and seg.wait(1) and seg.reason == "stopped"


# -- stop, as verified on an LF Plus (2026-10-22) ----------------------------

def test_console_stop_write_alone_halts(pair):
    """The console's Stop (drive -> empty slot 7) does stop the machine: on
    hardware it reported 16 -> 7 within 0.23 s and the head stayed put."""
    m, sim = pair
    take(m)
    seg = m.control.start(Drive.to_extension(30.0, 20.0))
    assert seg.started.wait(2)
    assert wait_until(lambda: sim.position > 0.5)
    m.control.arena.change_active(0, st.STOP_ACTIVE)
    assert seg.wait(1) and seg.reason == "stopped"
    where = sim.position
    time.sleep(0.1)
    assert sim.current == 7 and sim.position == where and where < 5.0


def test_identity_write_re_enters_slot(pair):
    """Seen on hardware: HALT_ACTIVE sent while in 19 reports 19 -> 19, and
    STOP_ACTIVE sent while in 7 reports 7 -> 7."""
    m, sim = pair
    take(m)
    n = len(sim.transitions)
    m.control.arena.change_active(0, st.STOP_ACTIVE)
    assert wait_until(lambda: len(sim.transitions) > n)
    assert sim.transitions[n] == (7, 7) and m.control.ready


def test_stop_halts_running_drive(pair):
    m, sim = pair
    take(m)
    seg = m.control.start(Drive.to_extension(30.0, 2.0))
    assert seg.started.wait(2)
    assert wait_until(lambda: sim.position > 0.2)
    m.control.stop()
    where = sim.position
    assert seg.wait(1) and seg.reason == "stopped"
    assert m.control.ready and where < 1.0
    assert sim.arena_packets[-2:] == [b"\x01\x20\x00" + st.HALT_ACTIVE,
                                      b"\x01\x20\x00" + st.STOP_ACTIVE]
    assert (seg.base, seg.base + 3) in sim.transitions and (seg.base + 3, 7) in sim.transitions
    time.sleep(0.1)
    assert sim.position == where                        # and it stays put


def test_stop_during_hold(pair):
    m, sim = pair
    take(m)
    seg = m.control.start(Drive.to_extension(1.0, 20.0, hold=30.0))
    assert seg.holding.wait(3)
    m.control.stop()
    assert seg.wait(1) and seg.reason == "stopped" and m.control.ready
    assert sim.position == pytest.approx(1.0)


def test_stop_during_return(pair):
    m, sim = pair
    take(m)
    m.control.drive(Drive.to_extension(20.0, 20.0), timeout=5)
    m.control.return_to_zero(wait=False)
    assert wait_until(lambda: sim.current == st.SLOT_RETURN and sim.position < 19.0)
    m.control.stop()
    assert m.control.ready and 0.0 < sim.position < 19.0
