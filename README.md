# lloydplus

Python driver for the RS-232 PC link of Lloyd Instruments "Plus" series test frames
(LF Plus, LRX Plus, LR Plus, EZ20/50). It replaces the NEXYGEN "EZ / LR Console"
(`LrCon.exe`). The protocol was reverse engineered from NEXYGEN 4.1; see
[PROTOCOL.md](PROTOCOL.md) for the wire format, with binary addresses as evidence.

Status: built from static analysis and tested only against the bundled protocol
simulator. **It has not yet talked to a real machine.**

## Wiring

The rear "RS232" connector on the frame (not the crosshead "GAUGE" port) needs the
equivalent of the Lloyd 09/0639 lead:

| machine pin | PC pin | |
|---|---|---|
| 2 | 3 | data |
| 3 | 2 | data |
| 7 | 8 | RTS → CTS |
| 8 | 7 | CTS ← RTS |
| 5 | 5 | ground |

A lead that crosses only 2↔3 leaves both CTS inputs floating. The original console
runs with hardware flow control, so neither side may ever transmit. If you can't
rewire, jumper 7–8 on the machine-facing connector and run with `--no-rtscts`.

## First test: listen only

```
uv run lloydplus sniff --port /dev/ttyUSB0      # Windows: --port COM3
```

`sniff` never writes to the port. It asserts RTS, so a machine that waits for CTS
can talk. The PC console repeats `BOOT state=0` until it gets an answer. If the
machine firmware does the same (likely, but unverified), a working cable shows a
steady stream of `BOOT state=0` frames. If `sniff` shows nothing, the machine may
simply be waiting for the PC to speak first. The next step is then `info`, which
does transmit (only link-handshake and query traffic, no motion).

Don't run NEXYGEN at the same time (one program per COM port). After a PC session,
the frame's own Print function is disabled until power-cycle (user manual, "Print
unavailable"). The Plus service manual warns that the bootloader listens for `++-`
in the first ~2 s after power-on, so don't connect during that window.

## Read information and log readings

```
uv run lloydplus info --port /dev/ttyUSB0
uv run lloydplus log  --port /dev/ttyUSB0 --csv run1.csv --seconds 60
```

```python
from lloydplus import LloydPlus

with LloydPlus("/dev/ttyUSB0") as m:
    print(m.version, m.description, m.parameters, m.sources)
    for s in m.samples(timeout=5):          # s.source: 0 = load, 1 = extension
        print(f"{s.t:8.3f}s  src{s.source}  {s.value:.6g}")
```

## Layout

| module | contents |
|---|---|
| `framing.py` | 0xDC framing, escaping, CRC-16/ARC |
| `link.py` | boot handshake, reliable sliding-window transport |
| `channels.py` | channel multiplexer, opening services by name |
| `services.py` | Version, Description, TensileParameters, DataInformation, DataSet readings, StatusInformation, watchdog, SetSystemTime, PC-side services |
| `machine.py` | `LloydPlus`: connect sequence and the high-level API |
| `sim.py` | machine-side simulator used by the tests |
| `cli.py` | `sniff`, `info`, `log` |
| `re/` | the reverse-engineering scripts (RTTI vtable recovery, Ghidra decompile and naming, string-table dump); see PROTOCOL.md §14 |

## Tests

```
uv run pytest -q
```
