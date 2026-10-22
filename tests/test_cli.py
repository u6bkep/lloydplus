import threading

import pytest

from lloydplus import LloydPlus, cli
from lloydplus.sim import SimMachine, port_pair


@pytest.fixture
def sim_cli(monkeypatch):
    sims = []

    def fake_connect(args, control=True):
        pc_port, sim_port = port_pair()
        sim = SimMachine(sim_port)
        sim.start()
        sims.append(sim)
        m = LloydPlus(pc_port, watchdog_interval=0)
        return m.connect(timeout=3, control=control)

    monkeypatch.setattr(cli, "_connect", fake_connect)
    yield sims
    for s in sims:
        s.stop()


def test_move_zero_home(sim_cli, capsys):
    assert cli.main(["move", "--port", "x", "--to", "3", "--speed", "600"]) == 0
    sim = sim_cli[-1]
    assert sim.position == pytest.approx(3.0)
    assert sim.current == 2                      # control handed back on exit
    assert "limit reached" in capsys.readouterr().out
    assert cli.main(["zero", "--port", "x"]) == 0
    assert cli.main(["home", "--port", "x"]) == 0


def test_info_does_not_set_up_stages(sim_cli, capsys):
    assert cli.main(["info", "--port", "x"]) == 0
    assert sim_cli[-1].arena_packets == [] and sim_cli[-1].factory_packets == []


def test_diag(monkeypatch, capsys):
    pc, fw = port_pair()
    menu = b"System selt test:\n\roptions:\n\rr - run loopback test\n\rv - display voltages\n\rm - menu\n\rX - exit\n\r"
    log = []

    def firmware():
        tests = 0
        while True:
            c = fw.read(64)
            if not c:
                continue
            log.append(c)
            if c == b"test\r":
                tests += 1
                fw.write(menu if tests == 1 else b"tracing on\n\r")
            elif c == b"v":
                fw.write(b"ch[0] = 4.9652\n\rch[1] = 3.9975\n\r")
            elif c == b"X":
                return

    t = threading.Thread(target=firmware, daemon=True)
    t.start()
    monkeypatch.setattr(cli, "_raw_port", lambda port, baud: pc)
    assert cli.main(["diag", "--port", "x", "--wait", "0.5"]) == 0
    out = capsys.readouterr().out
    assert "System selt test:" in out and "ch[1] = 3.9975" in out
    assert "\n\r" not in out
    t.join(2)
    assert log == [b"test\r", b"v", b"X"]
