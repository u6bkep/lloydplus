# lloydplus

Python driver for the RS-232 PC link of Lloyd Instruments "Plus" series test frames
(LF Plus, LRX Plus, LR Plus, EZ20/50). It replaces the NEXYGEN "EZ / LR Console"
(`LrCon.exe`). The protocol was reverse engineered from NEXYGEN 4.1; see
[PROTOCOL.md](PROTOCOL.md) for the wire format, with binary addresses as evidence.

Status: built from static analysis, then run against an LF Plus (firmware 3.0.94,
1 kN cell) on 2026-10-22. Verified on that machine, unchanged from the static
analysis: the lead and CTS behaviour, the boot handshake and reliable transport, all
service opens, machine information, readings at ~1 kHz per source, taking and
releasing control, crosshead-rate segments, stop (both this library's sequence and the
console's own write), and the transition reports. Not yet exercised on hardware: load
limits, holds, next-drive chaining, return-to-zero, zero, the load-rate option.

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
runs with hardware flow control, and the machine gates its own transmitter on CTS
(verified on an LF Plus: with a 2↔3-only lead it was silent at every baud rate, with
a null modem it answered first time). If you can't rewire, jumper 7–8 on the
machine-facing connector and run with `--no-rtscts`.

## Zero-protocol check: the firmware's self-test menu

```
uv run lloydplus diag --port /dev/ttyUSB0
```

The firmware answers `test` + CR at 57600 8N1 with a plain-text menu (Plus Series
Service Manual §11.14) and `v` with its analogue channels: 5 V and 4 V rails,
load-cell excitation, ±supply/2 and the live load-cell amplifier output. Five bytes
tell you the port, the lead and the analogue front end are alive, with no driver in
the loop. Quirks seen on an LF Plus: the menu is headed "System selt test" (sic),
lines end LF CR, there is an undocumented `X` to exit, and a second `test` toggles
"tracing on/off" instead of reprinting the menu. `diag` sends `test` once, undoes an
accidental toggle, and exits with `X`. Run it with the frame idle, not during a PC
session. **Caveat:** on our LF Plus the menu did not answer after the frame had
already been through a PC session; the colleague's result came from a freshly
powered machine. Try it before the first `info` after power-on.

## First test: listen only

```
uv run lloydplus sniff --port /dev/ttyUSB0      # Windows: --port COM3
```

`sniff` never writes to the port. It asserts RTS, so a machine that waits for CTS
can talk. An idle machine floods `BOOT state=0` frames at the full line rate (about
990 six-byte frames a second), so a working cable shows them immediately. If `sniff`
shows nothing, check the lead (pins 7–8). The next step is `info`, which does
transmit (only link-handshake and query traffic, no motion).

Don't run NEXYGEN at the same time (one program per COM port). After a PC session,
the frame's own Print function is disabled until power-cycle (LR/EZ user manual §9,
"Print unavailable"). The Plus Series Service Manual reportedly says the bootloader
listens for `++-` in the first ~2 s after power-on, so don't connect during that
window. PROTOCOL.md §0 lists the vendor documents by name.

## Read information and log readings

```
uv run lloydplus info --port /dev/ttyUSB0
uv run lloydplus log  --port /dev/ttyUSB0 --csv run1.csv --seconds 60
```

```python
from lloydplus import LloydPlus

with LloydPlus("/dev/ttyUSB0") as m:
    print(m.version, m.description, m.parameters, m.sources)
    for s in m.samples(timeout=5):          # s.source: 0 = load (N), 1 = extension (mm)
        print(f"{s.t:8.3f}s  src{s.source}  {s.value:.6g}")
```

`info` and `log` connect without setting up test control, so they send only link
handshake and query traffic.

## Test control

**This moves the crosshead.** Keep a hand on the emergency stop the first time, and
run the first moves in free air.

Verified on an LF Plus: taking control, a crosshead-rate segment (5.4 mm at 10 mm/min
took 32.5 s against 32.4 s expected), and stopping. `stop()` sends two arena writes,
first a redirect of the running drive into the bank's DriveNowhere stage and then the
console's own Stop; the machine reported `16 → 19 → 7` within a millisecond and the
head halted about 0.1 mm past the last reading. The console's write alone also stops
the machine (`16 → 7` in 0.23 s), so a colleague's report that NEXYGEN's OLE `Stop`
left the crosshead running points at the OLE path, not the firmware. Grip protection
defaults to 10 % of the frame's load range (the console's factory default is 100 N
either way); it is the backstop that ended those runaway moves.

A Plus-series frame connects automatically; there is no Remote Control setting to
make (that step belongs to the older LR/LRX/LF keypads, NEXYGEN manual ch. 5).

```
uv run lloydplus move --port /dev/ttyUSB0 --to 10 --speed 50          # to 10 mm at 50 mm/min
uv run lloydplus move --port /dev/ttyUSB0 --load -50 --speed 10        # compress to 50 N
uv run lloydplus move --port /dev/ttyUSB0 --load 200 --speed 20 --hold 30   # pull to 200 N, hold
uv run lloydplus zero --port /dev/ttyUSB0                             # zero load and extension
uv run lloydplus home --port /dev/ttyUSB0                             # back to 0 mm, max speed
```

Each command takes control, does its job, then stops and hands control back. Ctrl+C
during a move stops the crosshead (verified), and a repeated Ctrl+C keeps stopping
rather than aborting. Grip protection defaults to
10 % of the frame's load range; set it with `--tensile-limit` / `--compressive-limit`
(both positive, N). On a thin or stiff specimen the approach speed, not the load
limit, is what protects it: at 10 mm/min a 2 N touch-off was detected at 2.2 N, at
60 mm/min the same threshold overshot to 104 N.

From Python (units: mm, N, mm/s, N/s, s):

```python
from lloydplus import Drive, LloydPlus

with LloydPlus("/dev/ttyUSB0") as m:
    c = m.control                            # None if the machine has no stage services
    c.take_control(tensile_limit=500, compressive_limit=500)
    c.zero()
    seg = c.start(Drive.to_extension(20.0, 50 / 60, hold=10.0),   # 20 mm at 50 mm/min, hold 10 s
                  next_drive=Drive.to_extension(0.0, 5.0))        # then back, no pause
    while not seg.wait(0.5):
        print(m.load, m.extension)
    c.stop()
    c.return_to_zero()
# leaving the block stops and releases control
```

* **Limits** are absolute (from the last zero). `Drive.to_load` uses a load limit;
  `Drive.at_load_rate` drives at N/s and needs the machine's LoadRate option.
* **Direction** comes from the limit; speeds are always positive.
* **Sign convention** (measured on an LF Plus): tension is positive load and
  increasing extension; **compression is negative load and decreasing extension**.
  A compressive load limit is a negative number, and a "load above threshold" test
  written for tension can never fire in compression.
* **Pre-empting:** starting a new segment pre-empts the running one, as in NEXYGEN.
* **Break detection** is a PC-side job in NEXYGEN as well: watch the readings and call
  `c.stop()`.

How this maps onto the wire, and where it differs from the original console, is in
PROTOCOL.md §11.

## Layout

| module | contents |
|---|---|
| `framing.py` | 0xDC framing, escaping, CRC-16/ARC |
| `link.py` | boot handshake, reliable sliding-window transport |
| `channels.py` | channel multiplexer, opening services by name |
| `services.py` | Version, Description, TensileParameters, DataInformation, DataSet readings, StatusInformation, watchdog, SetSystemTime, PC-side services |
| `machine.py` | `LloydPlus`: connect sequence and the high-level API |
| `stages.py` | test control: stage arena, stage-factory encoders, `StageControl`, `Drive` |
| `sim.py` | machine-side simulator used by the tests (including a stage engine and a crosshead/sample model) |
| `cli.py` | `sniff`, `diag`, `info`, `log`, `move`, `zero`, `home` |
| `re/` | the reverse-engineering scripts (RTTI vtable recovery, Ghidra decompile and naming, string-table dump); see PROTOCOL.md §14 |

## Tests

```
uv run pytest -q
```
