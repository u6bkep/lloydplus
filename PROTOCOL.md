# Lloyd Instruments "Plus" series PC link: protocol reference

This document records what is known about the RS-232 protocol between a Lloyd
Instruments "Plus" series frame (LRX Plus, LR Plus, EZ20/EZ50 and LF Plus) and its PC
software.

## 0. Status, scope and sources

### Status

This document was reverse engineered statically from the vendor's PC software. Claims
carry the address of the code they come from, so anyone can re-check them. Where a
claim is an inference rather than a reading of the code, it says so.

**Hardware verification (2026-10-22).** `lloydplus` ran against an LF Plus
(firmware 3.0.94, "LF Plus CS4921", 1 kN cell, 500 mm) over an FTDI adapter and a
null-modem lead. Everything it exercised behaved as this document describes, with no
change to the protocol code:

**Second pass (2026-10-23), with the Rust port** (`rust/lloydplus`, same frame, same
lead): connect and information, 1024 samples/s per source with none dropped, free-air
drives (2 mm at 60 mm/min in 2.24 s), Stop mid-drive (slot 7 reported 0.19 s after the
writes, 0.23 mm overrun at 60 mm/min, no drift after), a 3 s position hold and its PC-timed
exit, load-limited drives at 10 and 60 mm/min, a source-selective Zero (load alone in air,
then extension alone at contact), a grip trip mid-move (3 N limit tripped at −12.3 N) with
recovery by Stop and a new drive, and a probe-style touch-off (rough touch at 60 mm/min,
back off, 0.5 mm clearance, load zero in air, 5 mm/min touch to 2 N, extension zero) in
10 s. One correction came out of it: a rate-0 LoadRate stage holds **position**, not load
(§11.7). Both drivers saw the machine resend every data packet until acked (about 40 %
of received frames are duplicates at the PC's 100 ms idle-ack cadence); that is the
frame's transport, not a fault.

* boot handshake, reliable transport and all service opens (§4–§8), including the
  stage factories; `Version` 3.0.94, `TensileParameters` 1000 N / 500 mm /
  0.000833–21.17 mm/s, `DataInformation` records for load and extension;
* `DataSet` streaming at 1024 samples/s per source with 1 ms spacing (§9);
* take control (`1 → 3 → 4 → 5 → 7`), a crosshead-rate segment (`7 → 16`, limit
  reached `16 → 19` after 32.5 s for 5.4 mm at 10 mm/min, 32.4 s expected), stop
  (`16 → 19 → 7`), release (`7 → 8 → 1`) (§11);
* the console's own Stop write, sent alone during a drive, also stops the machine:
  `16 → 7` after 0.23 s, head stationary afterwards (§11.1, §11.9);
* a write that covers the current slot re-enters it even when `active[i] == i`
  (`19 → 19`, `7 → 7` reports) (§11.1);
* the firmware's RAW frames carry trace text (§4), an idle machine floods BOOT frames
  at line rate (§4.1), and it drops a silent PC after about 4 s (§8.2);
* holds (`16 → 17`, position held, `17 → 19` on the PC's expiry write), Next
  (`16 → 17 → 18 → 19`, back within 0.01 mm), a load-limited drive (direction inferred
  from the sign, stopped at −5.38 N for −5 N at 10 mm/min), a load hold through the
  LoadRate factory (`12 → 13`; see §11.7 for what it holds), Zero
  (`7 → 9 → 7`, reading to 0.000), ReturnToZero (`7 → 10 → 7`, 5 mm in 1.1 s, rest at
  0.006 mm) (§11);
* grip protection: trips only on extension-driven moves, about 1 % of the load range
  past the set limit, reported as warning 10 on the PC's Error service with no stage
  transition; `stop()` then a new drive recovers, nothing at the frame (§11.3).

Not yet exercised on hardware: TrueStrainRate, slots 0 and 11, the `KeypadEZ` codes.

Earlier the same day a colleague had driven the same machine through NEXYGEN's own
console, `LrCon.exe`, via its OLE interface (GitHub issue #1 on this repository), which
established:

* the machine gates its transmitter on CTS (§1): silent with a 2↔3-only lead, answered
  first time with a null modem;
* the LF Plus is an `LrCon` machine: `Lloyd.LrLrxConsole` opened the port and never
  connected, `Lloyd.LrConsole` connected and reported the firmware version;
* direction comes from the limit, not from the console's tension/compression mode
  (§11.3);
* compression reads as negative load and decreasing extension (§10);
* NEXYGEN's OLE `Console.Stop` did not halt a running drive, while starting another
  stage pre-empted it. Since the machine does stop on the arena write that call is
  meant to send (above), the fault is in the OLE path (§11.5);
* positioning and speed were accurate: 0.02 mm error after 42 mm of travel, 20.25 s
  for a 20 s move at 60 mm/min;
* the self-test menu (§1, Practical constraints) works as the service manual says,
  with small differences.

Those were observations of the console's behaviour, not captures of its traffic; the
wire-level claims rest on the `lloydplus` run above.

Confidence by area:

| Area | Confidence |
|---|---|
| Transport (§2–§6) | Verified on hardware. |
| Services and readings (§7–§9) | Verified on hardware for everything the library uses. Medium for the meaning of some `DataInformation` fields. |
| Units (§10) | Verified: mm and mm/s (a commanded 5.4 mm at 10 mm/min took 32.5 s and the extension reading moved 5.4 mm); N by the sign and magnitude of the readings against a known foam compression. |
| Test control / stages (§11) | Verified on hardware: connect-time setup, take and release control, crosshead-rate and load-rate segments, load limits, holds, Next, stop, Zero, ReturnToZero, grip protection, transition reports. Untested: TrueStrainRate, slots 0 and 11. |

### Source binary

| | |
|---|---|
| CD | NEXYGEN 4.1 (October 2000) |
| File | `EZConsole/LrCon.exe`, "EZ / LR Console", file version 4.1, issue 9 |
| Format | Image base 0x400000, MSVC6/MFC42 |
| Addresses | Virtual addresses in this file. Names like `CMachineVersion_v63` mean vtable slot 63 of that class (recovered from RTTI, §14). |

NEXYGEN itself never touches the serial port. The NEXYGEN applications (Batch, Graph,
Ondio) drive the console process through OLE automation (`Lloyd.LrConsole` /
`Lloyd.StdConsole`). The console owns the COM port.

The vendor documents the automation interface in *Controlling NEXYGEN V4.0 using
Excel* (part 01/2987, on the CD as `Manuals/NEXYGEN V4.0 Automation Issue 1.0.PDF`,
chapter 5 "Console Object"). That is the supported way to script the machine through
NEXYGEN.

Older LR / LRX / LF500 (non-Plus) machines use a different console, `LrLrxCon.exe`. That
console is **not** covered here.

### Reference documents

Vendor documents cited here (not included in this repository):

| short name | document |
|---|---|
| LR/EZ manual | *LR_Plus Series and EZ Series Universal Test Machine User Manual*, Lloyd Instruments, Part No. 01/3461, May 2014 (`dual_column_test_stands_lr_and_ez_manual_english.pdf`) |
| Plus service manual | *Lloyd Instruments Plus Series Service Manual*, issue 10 (`service-manuals-plus-series-issue-10.pdf`), covering the LF Plus, LR Plus, LRX Plus and EZ. §2 microprocessor circuits and RS232, §3.2 upgrading the program, §11.14 PC diagnostic mode, §11.15 load-cell data points. |
| Automation manual | *Controlling NEXYGEN V4.0 using Excel*, part 01/2987 (on the NEXYGEN 4.1 CD) |

### Machine compatibility

NEXYGEN 4.1 lists the LRX Plus, EZ20 and EZ50 as the "Easy Test Series" it supports
("NEXYGEN V4.1 and Ondio V4.1" manual §1.2). The LF Plus isn't named. The Plus service
manual §2.2 says the machine is "operated by a computer using the Lloyd Instruments
NEXYGEN software which automatically uses a Baud rate of 57,600", that the flash program
is upgraded at 57600 8N1, and that printer/datalogger/HyperTerminal output is 9600 8N1;
its lead table is the same 09/0639 pinout as §1. The LR/EZ manual §11.10 gives 57600 8N1
for the PC link. Both are consistent with this protocol.

The hardware behind the protocol (service manual §2.1): a Motorola 68332 at about
21 MHz on the "MT Control PCB", program in a 1 MB flash (IC4), parameters in two 128 KB
SRAMs, test set-ups and data in a 32 KB battery-backed RAM, with a reset switch S1 on
the board.

Confirmed on hardware (issue #1): an LF Plus with firmware "3.0 Issue 94" connects to
`Lloyd.LrConsole` (`LrCon.exe`) and not to `Lloyd.LrLrxConsole`. Two tells that need
no machine: `LrLrxCon`'s port-settings dialog has a baud-rate field and `LrCon`'s
does not (its DCB is fixed at 57600, §1), and the NEXYGEN manual's chapter split puts
the Plus-series frames with the LRX Plus (ch. 6), not with the older LR/LRX/LF
(ch. 5). `Lloyd.StdConsole` is an alias for whichever console registered last; don't
rely on it.

## 1. Physical layer

### Serial settings

The DCB is built in `CWin32Hardware` 0x40ec00:

| Setting | Value |
|---|---|
| Baud | 57600 (stored in the registry as "Baud"; default per the LR/EZ manual §11.10) |
| Framing | 8 data bits, no parity, 1 stop bit |
| `fBinary` | 1 |
| `fOutxCtsFlow` | 1 (wait for CTS before sending) |
| `fRtsControl` | `RTS_CONTROL_HANDSHAKE` |
| `fDtrControl` | `DISABLE` (DTR held low) |

Other driver settings:

* Driver buffers: `SetupComm(in 0x6000, out 0x1000)`.
* Read timeouts: `MAXDWORD, 0, 0`, so reads return immediately. Overlapped I/O.
* Event mask: `EV_RXFLAG | EV_ERR`. `EvtChar = 0xDC`, the frame start byte.
* Line errors: on `EV_ERR` the driver calls `ClearCommError`. Any of
  `CE_OVERRUN | CE_RXPARITY | CE_FRAME | CE_BREAK` (mask 0x1E) is reported to the
  stack (0x40f120).

### The lead

The lead is Lloyd **09/0639**, female–female (LR/EZ manual §11.10):

| machine pin | PC pin | signal |
|---|---|---|
| 2 | 3 | data |
| 3 | 2 | data |
| 7 | 8 | RTS → CTS |
| 8 | 7 | CTS ← RTS |
| 5 | 5 | ground |

With hardware flow control enabled, a lead that crosses only 2↔3 leaves both CTS
inputs floating. The PC console then never transmits, and neither does the machine:
it gates its transmitter on CTS. Verified on an LF Plus (issue #1): with a 2↔3-only
lead the machine was silent at 4800–115200 baud, including to the self-test command
below, although its TX idled at a healthy −9.2 V; with a null modem fitted the PC's
CTS read true even with its own RTS driven low (so the machine asserts RTS), and the
machine answered at the first attempt. The printer lead (09/0721) is wired
differently and isn't for PC use.

### Practical constraints from the manuals

* After a PC session, the frame's own "Print" is refused until power-cycle ("Print
  unavailable. Power down and retry", LR/EZ manual §9).
* **Bootloader** (Plus service manual §3.2). Typing `++-` within 2 s of power-on, at
  57600 8N1 with hardware flow control, answers "Bootloader V1.1 ready for flash
  upgrade". Its only documented function is to receive a new program as a text file
  (HyperTerminal "Send Text File"; progress as "assembly id accepted" messages and
  dots, "flash upgrade OK" at the end, `*` to restart). An upgrade erases all user test
  set-ups and stored test data. No read-back command is documented. Don't transmit in
  that window.
* Only one program can own the COM port. Don't run NEXYGEN alongside another client.
* **Self-test menu** (Plus service manual §11.14, confirmed on an LF Plus, issue #1).
  `test` CR at 57600 8N1, outside a PC session, prints a plain-text menu; it is not
  framed and has nothing to do with the link protocol. As the firmware actually
  behaves: the heading is "System selt test:" (sic); options `r` (loopback test),
  `v` (display voltages), `m` (menu) and an undocumented `X` (exit, silent); lines
  end LF CR (`0a 0d`); a second `test` does not reprint the menu but toggles
  "tracing on" / "tracing off" (unexplored). `v` lists channels 0–15: ch0 ≈ 5 V
  supply, ch1 ≈ 4 V reference, ch5 ≈ 4 V load-cell excitation, ch6 and ch15 ≈ ±7.4 V
  (supply ÷ 2), ch7 the load-cell amplifier output (live load plus a small offset).
  `lloydplus diag` runs this exchange.

## 2. Layer 1: framing

Code: `CFraming`, receive 0x40c2e0, transmit 0x40c410.

```
0xDC  escape( crc_hi  crc_lo  len  body[len] )
```

* **Start byte:** 0xDC starts every frame. The transmitter writes only this leading
  0xDC; there is no trailer.
* **Escaping:** inside a frame, 0xDC is sent as `DD 5C` and 0xDD as `DD 5D`. In
  general a byte `b` in {0xDC, 0xDD} becomes `0xDD, b ^ 0x80`.
* **Receiver:** it accumulates un-escaped bytes. When the next 0xDC arrives, it
  passes the buffer (if non-empty, and at most 0x102 bytes) up to layer 2, then
  clears it. Bytes beyond 0x102 are dropped.
* **Resync burst:** `FUN_0040c5a0` writes 256 × 0xDC. The console sends it when its
  link has failed (§4.2).
* **`lloydplus` difference:** it delivers a frame as soon as the length byte and CRC
  say it's complete, so the last frame isn't held back waiting for the next 0xDC.
  Trailing bytes after a complete frame are ignored. The original would reject such
  a frame on length; this only matters for corrupted input.

## 3. Layer 2: fault control

Code: `CFaultControl`, receive 0x40c070, transmit 0x40c170.

* Decoded frame: `[crc_hi, crc_lo, len, body...]`.
* `len` must equal `len(body)`.
* `crc` is **CRC-16/ARC** over `body`: reflected polynomial 0xA001, initial value 0,
  no final XOR. It is stored big-endian. The table at 0x43c734 is byte-identical to
  the 0xA001 table; the initial value is set in 0x40be20. The check value is
  `crc("123456789") = 0xBB3D`.
* A frame failing either check is dropped silently. There is no NAK; the reliable
  layer's retransmission recovers it.

## 4. Layer 3: protocol id

Code: `CMultiProtocol` 0x40c6e0, dispatch 0x40d090.

`body = [proto, payload...]`. Bodies shorter than 2 bytes are ignored.

| proto | name | handling on receipt |
|---|---|---|
| 0x02 | TRANSMISSION | reliable layer (§5). Ignored until the boot handshake is complete. |
| 0x04 | RAW | discarded by the console (0x40be40 → 0x40fe20). **The machine sends trace text in these:** `Growing` and `Maturing` as its link state rises, `Now idle` about every 250 ms while connected, `comm1 reset` when it drops the link (§8.2). `lloydplus` logs them. |
| 0x05 | ECHO | the payload is sent back as **proto 0x04** (0x40c000), so echoes can't ping-pong |
| 0x06, other | DISCARD | discarded |
| 0xCE | BOOT | link handshake (§4.1) |

### 4.1 Boot handshake

Code: `CBoot` 0x40b1e0. The transition table is at 0x44ec50: `{0,0,1, 1,3,3, 4,2,1}`.

Each side has a link state of 0, 1 or 2 (connected). While below 2, a side keeps
sending `[0xCE, state]`. The PC sends it whenever its transmitter wants data (0x40d240),
so in practice continuously. The machine does the same: an idle LF Plus floods
`DC 60 54 02 CE 00` back to back at the full line rate, about 990 frames/s, whenever
its CTS is asserted. On receiving `[0xCE, rx]` with a 1-byte payload and `rx <= 2`:

| my state | rx | action |
|---|---|---|
| 0 | 0 or 1 | → 1 |
| 0 | 2 | nothing |
| 1 | 0 | nothing |
| 1 | 1 or 2 | → 2: connected; start accepting proto 2 |
| 2 | 1 | reply `[0xCE, 2]` (the peer hasn't seen our 2 yet) |
| 2 | 0 | the peer has restarted: link failure (§4.2) |
| 2 | 2 | nothing |

A typical bring-up looks like this:

```
PC   CE 00   CE 00 ...
M    CE 00 ...                 (PC 0→1, M 0→1)
PC   CE 01 ...
M    CE 01 ...                 (PC 1→2, M 1→2)
M    CE 02                     (reply to a late CE 01)
```

The exact interleaving depends on timing. The machine side is assumed to run the same
state machine.

### 4.2 Link failure

When the console receives boot 0 while connected (`0x40feb0`), it:

* sets a "failed" flag that only reconstruction clears;
* posts window message 0x466, which surfaces as "Connection closed remotely"
  (string 0x3F2);
* ignores all further received bytes (the framing receive path checks the flag via
  `0x40fea0`);
* replaces data frames with 256 × 0xDC bursts;
* tears the connection down. Reconnecting builds a fresh stack, starting at boot
  state 0.

A failed attempt to queue a send (`0x40dce0`) takes the same path.

**`lloydplus` difference:** it resets its link state, re-handshakes in place, and sets
`LloydPlus.lost`. The caller should then reconnect the services.

## 5. Layer 4: reliable transmission (proto 0x02)

Code: `CTransmission` 0x40d510 and related, `CTransmissionReorder` 0x40dd10 and
related.

### 5.1 Packet format

```
[ack, seq, data...]     data packet (data = layer-5 bytes, 1..252)
[ack]                   pure acknowledgement
```

* **`seq`:** assigned when a packet is queued (0x40d780). It starts at 0 and wraps
  modulo 256.
* **`ack`:** the seq of the last packet *delivered in order that carried at least one
  data byte* (0x40d6d0). It is inserted when the packet is built (0x40d860) and also
  starts at 0. A bare `[ack, seq]` with no data advances the receiver's expected seq
  but not its ack; nothing legitimate sends one.

Initial state of both sides (0x40d510 / 0x40dd10):

| variable | initial value |
|---|---|
| tx seq | 0 |
| expected rx seq | 0 |
| ack sent | 0 |
| last ack received | 0 |
| last ack processed | 0 |

### 5.2 Receiver

Code: 0x40de70.

* **Ack field:** accepted if `(ack − last_ack) mod 256 < 0x50`, unsigned, and equal
  counts.
* **Data:** let `off = (seq − expected) mod 256`.
  * If `off < 0x50`, the packet is stored in an 80-slot reorder buffer. Consecutive
    packets from `expected` onward are then delivered and `expected` advances.
  * Otherwise it's a duplicate or out of range, and is ignored.
* **Delivery:** in-order packets go to the inbound queue, which holds 160 packets
  (0x40d6d0). The console has a flaw here. If that queue is full, the packet is
  dropped *after* `expected` has advanced, and the ack isn't updated. The peer then
  retransmits forever and the packet is never accepted. Don't flood the machine.

### 5.3 Sender

Queues (0x40d510):

| queue | capacity |
|---|---|
| untransmitted | 80 packets, and at most 0x2800 bytes ("Untransmitted queue full" / "too long") |
| sent / unacknowledged | 80 packets |

Rules:

* **Release (0x40db90):** the sent queue is re-scanned **only when the last-received
  ack changes**. Each packet with `(ack − seq) mod 256 <= 0xAF` is then dropped.
* **Consequence of the initial values:** an ack of 0 at start-up releases nothing. So
  packet seq 0 stays queued, and keeps being resent, until the peer acks seq ≥ 1. The
  peer ignores the duplicates.
* **What goes out next (0x40d980):**
  1. If the sent queue is full, or holds more than 0x27FF bytes, resend its oldest
     packet.
  2. Otherwise, if anything is untransmitted, send it and move it to the sent queue.
  3. Otherwise, if anything is unacknowledged, resend it round-robin.
  4. Otherwise, send a pure ack.

### 5.4 Pacing

The console's writer keeps the line busy (0x40f6b0, 0x40f8e0):

* The UART is fed from a 3200-byte ring.
* Whenever fewer than 100 bytes are pending and at least 1000 are free, it asks the
  stack for another frame.
* With fewer than 100 bytes pending it sleeps `(100 − pending)/5` ms.

So while connected the PC transmits continuously: data, retransmissions, or bare acks.
`lloydplus` instead sends acks immediately when needed, plus one every 100 ms when
idle, and resends after a 300 ms timeout. Both are valid against the receiver rules
above.

## 6. Layer 5: channels and services

Code: `CChannel` 0x40b2f0; receive 0x40b5d0 / 0x40b740; send 0x40b4f0.

### 6.1 Channels

* Every reliable data packet is `[channel, bytes...]`.
* A handler table covers 256 channels. Allocation (0x40b8e0 / 0x40b960 / 0x40b980)
  takes the next free number after the last one issued, and skips **0, 0xDC and
  0xDD**. The console's first allocation is 2 (0x40b2f0).

### 6.2 Channel 0: control

Channel 0 is handled specially, unless a handler has been registered on it.

| packet on ch 0 | meaning |
|---|---|
| `[reply_ch] + name` (≥ 2 bytes) | open service `name` (ASCII, no terminator, exact case-sensitive match). The receiver answers **on `reply_ch`** with exactly 2 bytes: `[status, service_ch]`. |
| `[ch]` (1 byte) | close: the receiver deletes its handler on its own channel `ch` |

Open status codes (0x40b9c0, 0x40bce0):

| status | meaning | console's site state |
|---|---|---|
| 0 | ok, `service_ch` valid | 4 |
| 1 | obsolete | 2 |
| 2 | creation failed | 1 |
| 3 | unknown service | 3 |

* **Optional features:** the console treats status 3 from a feature service as "not
  fitted".
* **Open failure while connecting:** the console shows one of three messages:
  * "...may need to upgrade the machine" (0x3E9) for unknown,
  * "...may need to upgrade the software" (0x3EA) for obsolete,
  * "...may need to reset the machine" (0x3EB) otherwise.

### 6.3 Conversation pattern

1. **Open.** Allocate a temporary reply channel `r` and send `[0, r, name...]`. The
   reply `[status, svc]` arrives on `r`, and `r` is then freed (0x40ba90, 0x40bc50).
2. **Subscribe.** For services that talk back, allocate a permanent receive channel
   `L` and send it to the service in-band. Almost always this is the single byte
   `[L]`, sent on `svc`.
3. **Exchange.** Requests go on `svc`; the service's output arrives on `L`.
4. **Close.** Send `[0, svc]`.

Example (simulated, decoded with `lloydplus sniff`; `TX ack/seq` is the reliable header
and `ch` the channel):

```
TX ack=0 seq=0 ch=0 [10] 03 52 65 73 70 6f 6e 64 65 72   OPEN 'Responder' reply->ch3
TX ack=0 seq=0 ch=3 [2]  00 02                            status 0, service on ch2
```

## 7. Services provided by the PC

The machine may open these on the PC. The lookup is in `CLrStack_v3` 0x40a140.

| name | request (on the service channel) | response |
|---|---|---|
| `Version` | `[reply_ch, ...]` (≥ 1 byte) | `[4, 1, 9]` on reply_ch: the console's file version 4.1, issue 9 (0x40e180) |
| `Description` | `[reply_ch, maxlen]` (≥ 2 bytes) | `"LrCon Version 4.1 Issue 9"` on reply_ch. No terminator, and the same string whatever `maxlen` is (0x40bed0). The string is InternalName + " Version " + FileVersion (0x416160). |
| `Responder` | `[reply_ch, data...]` | `data` on reply_ch (0x40d450) |
| `Error` | `[type, code_hi, code_lo, text...]` (> 2 bytes) | none. Type 2 is a warning, anything else an error. The message format is the same as StatusInformation (§8.7) (0x4089a0). |
| `Debug2` | text | none (debug window) |
| `Obsolete` | — | the open is refused with status 1 |

## 8. Services on the machine

### 8.1 Connect sequence

The console builds a chain of machine "layers" (`CNextStepMachineVisitor`
0x42a0d0..0x42a790). Each layer opens its services when the previous layer has
finished. The resulting order:

| # | Layer | Services |
|---|---|---|
| 1 | Mandatory | `Responder`, `NotImplementedDataSources`. Both must open (0x423ab0, 0x423cd0). |
| 2 | Keypad | `KeypadEZ` |
| 3 | Optional | `SetSystemTime` |
| 4 | Watchdog | (uses Responder) |
| 5 | Description | `Description` |
| 6 | Version | `Version` |
| 7 | Status | `StatusInformation` |
| 8 | Parameters | `TensileParameters` |
| 9 | Stage arena | `StageArenaReference5` (§11) |
| 10 | Data sources | `DataInformation`, then `DataSet` |
| 11 | Stage wrapper | stage factories (§11) |

### 8.2 `Responder` and the watchdog

Code: 0x429e00, timer handler 0x429f00, reply handler 0x429fe0, send path 0x423c50.

* **Responder:** echoes `[reply_ch, data...]` as `data` on `reply_ch`, the same as the
  PC's own Responder.
* **Watchdog channel:** the console allocates a channel `W` and starts a 10 s timer
  (`SetTimer` id 0x65, 10000 ms).
* **On each tick:** if the previous ping is still outstanding, the console reports
  "Timeout waiting for machine reply" (string 0x400) and stops pinging. Otherwise it
  sends `[W]` to Responder and marks a ping outstanding.
* **Machine's reply:** an empty packet on `W`. Any packet on `W` clears the
  outstanding flag.
* The first ping goes out 10 s after connecting. A machine has up to ~10 s to answer
  each one.
* **The machine's side (observed).** After the PC goes quiet the LF Plus keeps the
  session up for about 4 s, still streaming `DataSet` packets, then prints
  `comm1 reset` (a RAW trace frame) and returns to boot state 0, flooding BOOT frames.
  Closing the `DataSet` service does not stop the stream; only the reset does. In a
  normal session the PC's acknowledgements of the 1 kHz stream keep the link busy; a
  client that does not subscribe to readings must still send something within that
  window. `lloydplus` therefore pings every 2 s instead of the console's 10 s.

### 8.3 `NotImplementedDataSources`

It is only opened. Nothing is sent or received on it by this console. It is mandatory:
an unknown-service reply is treated as a fatal "upgrade" error.

### 8.4 `KeypadEZ`

Code: 0x4236c0, send 0x4237e0.

* Single bytes 0, 1, 2, 3 or 4 can be sent.
* The only senders are menu commands 0x8020, 0x801C, 0x801D, 0x801E and 0x801F (in
  that order for codes 0–4). Those items sit in a hidden menu with placeholder text
  (`XXXX`) in LrConRes.dll.
* Meaning unknown. Probably simulated front-panel keys for debugging.

### 8.5 `SetSystemTime`

Code: open 0x423f20, send 0x424020, close 0x423ff0.

* Immediately after opening, the console sends the current **UTC** time
  (`GetSystemTime`), formatted as
  `snprintf("%d%c%d%c%d%c%d%c%d%c%d%c%d%c", year,0, month,0, dayOfWeek,0, day,0,
  hour,0, minute,0, second,0)`.
* The length sent includes snprintf's terminator. So it is 7 NUL-terminated decimal
  fields plus one more NUL.
* Day of week: 0 = Sunday.
* Example for Wed 2026-10-21 18:35:07Z:
  `"2026\0" "10\0" "3\0" "21\0" "18\0" "35\0" "7\0" "\0"`.
* The channel is then closed. There is no reply.

### 8.6 `Description` and `Version`

**Description** (open 0x422e70, requests 0x422fb0, replies 0x4231f0):

* The console sends three requests, `[L, 0x00]`, `[L, 0x0F]` and `[L, 0x1E]`: reply
  channel plus length limit 0, 15 and 30.
* Three text replies arrive on `L`, in that order.
* The console keeps them as three description strings, exposed through its vtable
  slots 25, 26 and 27.
* Whether the machine adds a terminator is unknown. Strip at the first NUL.

**Version** (open 0x429820, subscribe 0x429930, reply 0x429ab0):

* Subscribe with `[L]`.
* Reply on `L`: exactly 3 bytes, `[major, minor, issue]`. Any other length is reported
  as "Version packet length bad".

### 8.7 `StatusInformation`

Code: open 0x428d50, subscribe 0x428e70, receive 0x428f70, parse 0x401cb0.

* Subscribe with `[L]`. The machine then pushes packets on `L`.

| length | meaning |
|---|---|
| ≥ 2 | message `[code_hi, code_lo, text...]`. `code = (int8)code_hi << 8 | code_lo`; text is the rest, NUL-trimmed. |
| 1 | error "Status information packet length bad" |
| 0 | end of batch: the console now shows each queued message in a modal dialog with a warning beep |

Message codes. The text comes from the console's string table, LrConRes.dll IDs
0x402 + code, mapped in `FUN_00401dd0`:

| code | text |
|---|---|
| 0 | Machine communications protocol violation. |
| 1 | Motor fault. |
| 2 | Upper extension limit switch reached. |
| 3 | Lower extension limit switch reached. |
| 4 | Safe line is open. |
| 5 | No load cell connected. |
| 6 | Guard not closed. |
| 7 | Load cell rating exceeded. |
| 8 | Compression load limit for manual control exceeded. |
| 9 | Machine frame load rating exceeded. |
| 10 | Load has exceeded safety limit. |
| 0x1B | Load cell calibration due. |
| 0x1C | Fatal hardware fault. |
| other | show the text carried in the packet |

Compare the frame-side error list in LR/EZ manual §9.1. The machine-side wording
differs slightly.

### 8.8 `TensileParameters`

Code: open 0x4245a0, subscribe 0x4246b0, receive 0x424830, parse 0x424990.

* Subscribe with `[L]`. One reply arrives on `L`.
* **Validation:** the length must be 2..100 and the last two bytes must both be NUL.
  Otherwise the console reports "Parameters packet length bad" or "...content bad".
* **Content:** NUL-separated decimal strings, parsed with C `atof`. Parsing stops at
  the first empty field, so at most 4 fields are used:

| field | unit (§10) | exposed as |
|---|---|---|
| 1 | N | `LoadRange` |
| 2 | mm | `ExtensionRange` |
| 3 | mm/s | `MinSpeed` |
| 4 | mm/s | `MaxSpeed` |

The names come from the console's `CLimitsServer` automation properties (dispatch map
at 0x440a00).

### 8.9 `DataInformation`

Code: open 0x420370, subscribe 0x4204a0, receive 0x420620, store 0x41e3c0.

* Subscribe with `[L]`. The machine sends one record per data source, then an empty
  packet. On the empty packet the console closes this service and opens `DataSet`.
* Record (5..250 bytes; otherwise "Parameters packet length bad"):

```
[index, kind, extra, a "\0", b "\0", name "\0"]
```

| field | meaning |
|---|---|
| index | data source number. 0 = load, 1 = crosshead extension, 2+ = extra transducers. Index 0 must have kind 0 or 1 and index 1 kind 0 or 2; otherwise "Load/Extension data information bad". |
| kind | 0 = not fitted (inferred), 1 = load, 2 = extension, 3 = other (e.g. width). Must be < 4 ("Data source type bad"). |
| extra | stored for kinds 2 and 3 (record offset +0x108). Meaning unknown. |
| a, b | decimal strings. The console keeps `max(atof(a), atof(b))` as the source's range, used for display scaling. |
| name | label shown in the console's Data Sources dialog |

* A repeated `index` is reported ("Data information index already used") and the
  source is kept once.

## 9. Live readings: `DataSet`

Code: open 0x41e170, subscribe 0x41e250, per-source receive 0x41ec20.

### 9.1 Subscribing

* The console allocates one receive channel per known data source and sends the list
  of those channel numbers, in ascending source index, as one packet to `DataSet`:

```
[ch_src0, ch_src1, ...]
```

* Each source then streams to its own channel. There is no explicit start or stop:
  the stream ends when the service or channels are closed.

### 9.2 Per-source packets

Each packet on a source channel starts with a type byte. Lengths below count the bytes
after the type byte.

| type | rest | effect | console error on wrong length |
|---|---|---|---|
| 0 | n × int16 big-endian | for each delta: `accum += delta`, then `ticks += increment`, then emit a sample (0x41edd0) | — |
| 1 | int32 BE (exactly 4) | `accum += value`; no sample (DS_ACCUM) | "DS_ACCUM packet bad" |
| 2 | int32 BE (exactly 4) | `accum = 0`; the value is kept as the raw zero offset (DS_ZERO) | "DS_ZERO packet bad" |
| 3 | int16 BE (exactly 2) | `increment = value`. If ≤ 0 the console still stores it but reports "time_increment bad". | "DS_TIME packet bad" |
| 4 | ASCII decimal | `scale = atof(text)` | — |

### 9.3 Initial state and output

* Initial state per subscription: `accum = 0`, `ticks = 0` (64-bit), `increment = 1`,
  `scale = 0`. **Values read 0 until a type-4 scale message arrives.**
* Sample output: `value = accum × scale` (N for load, mm for extension, §10), and time `= ticks × 0.9765625` (constant at
  0x43e9e8). So a tick is 1/1024 s and the product is milliseconds.
* Observed on an LF Plus: both sources stream at 1024 samples/s (one sample per
  tick), delivered in packets of 95–115 bytes about every 50 ms, each on its own
  channel. NEXYGEN's OLE interface exposes the same data at 17–20 Hz by polling.

Example (simulated) of a source being configured and then streaming:

```
ch=21 [6] 04 30 2e 30 31 00        scale "0.01"
ch=21 [3] 03 00 0a                 time increment 10 ticks/sample
ch=21 [5] 02 00 00 30 39           zero; raw offset 12345
ch=21 [9] 00 00 00 00 32 00 32 00 31   four deltas: 0, +50, +50, +49
```

## 10. Units

Earlier versions of this document guessed SI (m, m/s). The code says otherwise: the
console's machine-facing units are **newtons, millimetres and seconds**.

How this was established:

* The console's quantity class (`CQuantity`, constructor 0x41a100) stores values in SI,
  but every quantity is built as "1.0 × a named unit" and scaled through a unit table
  looked up by symbol. The unit constants are built by static initialisers at
  0x416cc0..0x417220:

  | address | unit | | address | unit |
  |---|---|---|---|---|
  | 0x451460 | m | | 0x451370 | ms |
  | 0x451430 | mm | | 0x451340 | min |
  | 0x451400 | in | | 0x451280 | m/s |
  | 0x4513d0 | N | | 0x451250 | mm/min |
  | 0x4513a0 | s | | 0x451220 | mm/s |

* Converting for the wire is `q / unit` (0x41a830) followed by "to double, must be
  dimensionless" (0x41a5e0). So the number on the wire is the value in that unit.
* What each machine-facing value is divided by:

  | value | unit | evidence |
  |---|---|---|
  | `DataInformation` source 0 (load) | N | unit assigned at 0x41e3c0 |
  | `DataInformation` sources 1+ (extension, kinds 2 and 3) | mm | 0x41e3c0 |
  | `TensileParameters` LoadRange / ExtensionRange | N / mm | 0x424a0e, 0x424a74 |
  | `TensileParameters` MinSpeed / MaxSpeed | mm/s | 0x424ad7, 0x424b3a |
  | stage limits, rates, ranges, stiffness | N, mm, N/s, mm/s, N/mm | §11.3 |

* So the `DataSet` scale string (§9.2) is N per count for load and mm per count for
  extension. The NEXYGEN automation manual agrees: `Console.Load.Name` is "in N" and
  `Console.Extension.Name` "in mm" (*Controlling NEXYGEN V4.0 using Excel* §5.2).
* **Sign** (measured on an LF Plus through the console, issue #1): tension is
  positive load and increasing extension; compression is negative load and decreasing
  extension (contact at −134.6 mm / −5.49 N, compressed at −135.4 mm / −64.81 N). A
  compressive load limit is therefore a negative number (§11.3, Direction), and
  code that takes `max()` of loads or tests `load >= threshold` is wrong in
  compression.
* **Still worth one hardware check** on the wire itself (a known weight, a measured
  crosshead move, read through `lloydplus log`), but this is now a reading of the
  code confirmed through the console's display, not a guess.

## 11. Test control (stages)

Implemented in `lloydplus.stages` (§11.9 lists where it differs from the console).
Everything here is read from `LrCon.exe`; the byte layouts are checked against the
disassembly wherever Ghidra dropped call arguments. Semantics that are inferred rather
than read are marked **(inferred)**.

### 11.1 Model

* **Arena.** The machine holds a **stage arena of 32 slots** (0..31). Each slot holds
  at most one stage, and exactly one slot is current. A stage is a small machine-side
  program: "drive the crosshead at a rate until a limit", "zero the readings", "release
  exclusive control" and so on.
* **Creating stages.** The PC puts a stage into slot N by sending `[N, params...]` to
  the matching **stage-factory service**, one service per stage kind, opened by name
  (§11.3). Factories never reply.
* **Two 32-entry tables** in `StageArenaReference5` decide what runs next:
  * `next[i]`: where the machine goes when the stage in slot i **completes** by
    itself (e.g. the limit is reached).
  * `active[i]`: a **redirect**. When a write covers the current slot i and sets
    `active[i] != i`, the machine leaves i for `active[i]` at once, whether or not the
    stage has completed. `active[i] == i` means "no redirect" **(inferred** from how
    the console writes the table: every bulk write keeps `active[i] = i` for slots it
    must not disturb, and Stop is a write of `active[running slots] = 7`).
  * The redirect is checked **only for the slots the packet writes**, and **not
    re-checked when a slot is entered** **(inferred**; the console's own traffic would
    misbehave otherwise):
    * Zero writes `active[7] = 9`, and slot 9's `next` is 7. If entering a slot
      re-checked `active`, the machine would loop 7 → 9 → 7 forever; ReturnToZero
      would loop through 10 the same way.
    * Release writes `active[7] = 8` and nothing resets it. A later take-control
      writes `active[3] = 0` while the machine is already in 7. If every write
      re-checked the current slot, that would bounce the machine straight back out of
      control on every test after the first.
  * **Verified on hardware:** a write that covers the current slot makes the
    machine enter `active[current]` at once, whether or not the stage has completed,
    and **even when `active[i] == i`**: the machine then re-enters the same slot and
    reports it (`19 → 19` after a Stop sent while sitting in DriveNowhere, `7 → 7`
    after one sent from ready). So "no redirect" is only a property of slots a write
    does not cover, not of identity entries. The console's Stop, `active[16] = 7`
    sent during a drive, moved the machine `16 → 7` within 0.23 s and the crosshead
    stopped there: an empty slot is a stopped machine.
* **Reports.** The machine reports every slot change `(old, new)` to the PC. Nothing
  else in the stage subsystem flows machine → PC.
* **Done on the PC, not the machine:** hold timing (§11.6), break detection (§11.7),
  and deciding that a stage is "finished".

### 11.2 `StageArenaReference5`

Code: open 0x424d20, open reply 0x424e10, change-next 0x424f60 (vtable slot 34),
change-active 0x424ff0 (slot 35), sender 0x425080, transition handler 0x425170, notify
receive 0x40e2a0.

The trailing "5" looks like an interface version **(inferred)**; there is no other
version handshake.

**Subscribe.** After the open succeeds, the console allocates a notify channel `N`
(0x4252a0) and sends, on the arena service channel:

```
[0x00, N]
```

**PC → machine** (on the arena service channel):

| packet | meaning |
|---|---|
| `[0x01, count, start, v0 .. v(count-1)]` | `active[start + k] = v_k` |
| `[0x02, count, start, v0 .. v(count-1)]` | `next[start + k] = v_k` |

The console checks `count ≥ 1`, `start ≥ 0` and `start + count ≤ 32`. On failure it
reports "Change next stage bad parameters" / "Change active stage bad parameters", but
**it still sends the packet**.

**Machine → PC** (on `N`):

| packet | meaning |
|---|---|
| `[0x00, old, new]` (exactly 3 bytes) | the current slot changed from `old` to `new` |
| `[0x01, ...]` | the machine closed the notification; the console deletes its handler |
| other | ignored |

On a transition report the console:

* checks that `old` equals the slot it last recorded (initially 0), else reports
  "Stage changed out of sequence";
* records `new`;
* fires a "control state changed" notification when the slot crosses between
  {0, 1} and {≥ 2}. The console's "machine available" test is `current > 1`
  (0x425210). Whether that is what drives the keypad's grey/black state was not traced.
* runs the per-slot reaction in §11.8.

### 11.3 Stage factories

Each factory is a service (§6.3), opened by name. The PC sends one packet per stage on
the factory's service channel. There is no subscribe and no reply.

**Number encoding.** Parameters are ASCII, produced by MSVC `_snprintf`:

* `%c` puts a raw byte (a slot number, a data-source index, a NUL separator);
* `%e` puts a decimal in MSVC's format, six decimals and a **three-digit exponent**,
  e.g. `5.000000e+001`.

So `"%c%e%c"` with (5, 2.5, 0) is the bytes `05 "2.500000e+000" 00`. Whether the
machine also accepts two-digit exponents is unknown; to be safe, emit MSVC's form.

| factory | packet | units | console use |
|---|---|---|---|
| `DoNothingStageFactory` | — | | never sent to; opened as a presence check, then closed |
| `WaitExclusiveStageFactory` | `[slot]` | | slot 1 |
| `WaitSharedStageFactory` | `[slot]` | | slot 2 |
| `GainExclusiveStageFactory` | `[slot]` | | slot 3 |
| `ReleaseExclusiveStageFactory` | `[slot]` | | slot 8 |
| `DriveNowhereStageFactory` | `[slot]` | | slots 15 and 19 (stop and stay put) |
| `ZeroReadingsStageFactory` | `[slot, m0, m1, .. mK]` | | slot 9. `m_i = 0xFF` zeroes data source i, `0x00` leaves it; the list ends at the highest source to zero (0x426020) |
| `GripProtectionStageFactory` | `[slot] "T" 00 "-" "C" 00` | N | slot 4. T = tensile load limit, C = compressive load limit, both from the machine-setup scheme. The `-` is literal in the format string `%c%e%c-%e%c` (0x426bd0) **Verified:** guards extension-driven moves only (a load-limited drive to −10 N ran through a 3 N limit untouched). It trips about 1 % of the load range beyond the limit (3 N → −10.8 N, 20 N → −27 N, 100 N → 107 N on a 1 kN cell), stops the crosshead itself, leaves the machine in the drive slot with no transition report, sends warning 10 "Load has exceeded safety limit." on the PC's `Error` service, and the frame beeps "grip over load, press enter". Stop then a new drive recovers from the PC; no key press needed. |
| `SetAutoSampleStiffnessStageFactory` | `[slot]` | | slot 5 |
| `SetManualSampleStiffnessStageFactory` | `[slot] "k" 00` | N/mm | slot 5 (0x426d00) |
| `SetDatasourceRangeStageFactory` | `[slot, src] "r" 00` | mm | slot 6: set the range of extension source `src` (format `%c%c%e%c`) |
| `CrossheadRateStageFactory` | motion (below) | | drive segments, holds |
| `CrossheadPositioningStageFactory` | motion (below) | | slots 10, 11: return to 0 mm |
| `LoadRateStageFactory` | motion (below) | | drive segments with a force rate, creep holds. Optional |
| `TrueStrainRateStageFactory` | motion (below) | | drive segments with a strain source. Optional |

**Motion stages.** One encoder, 0x4282f0, format `%c%e%c%c%e%c%c%e%c`:

```
[slot] "rate" 00  [limit_src] "limit" 00  [strain_src] "gauge" 00
```

| field | meaning |
|---|---|
| `slot` | arena slot |
| `rate` | mm/s if the requested rate is a velocity, otherwise N/s (a load rate) |
| `limit_src` | data source the limit applies to: `0` for a force limit; for a length limit, the index of the scheme's "Extension Readings Data Source" if it is ≥ 1, else `1` (crosshead) |
| `limit` | end-of-stage limit: N for a force, mm for a length. Absolute, not relative **(inferred** from ReturnToZero using limit 0 mm, and the manual's "Stage2.limit = 0mm" example) |
| `strain_src` | `0xFF` (none) for every use except TrueStrainRate, where it is the strain data-source index |
| `gauge` | gauge length in mm for TrueStrainRate; `0.000000e+000` otherwise |

Further details:

* **Speed clamping.** When the rate is a velocity and `strain_src` is none, the console
  first limits it with 0x428790. That uses the machine's MinSpeed/MaxSpeed (vtable
  slots 46/47, ultimately `TensileParameters`). The rule is:
  * above MaxSpeed → MaxSpeed;
  * above MinSpeed → unchanged;
  * above MinSpeed/10 → MinSpeed;
  * otherwise → **0**.

  So a rate-0 hold really is sent as 0.
* **True strain.** The unit of the strain rate was not resolved. As written, 0x4282f0
  would convert a 1/s rate as N/s and throw. So either the automation layer passes it
  differently, or that path is unused by this console.
* **Direction.** The rate is always sent as a positive magnitude. `Console.Compression`
  only flips a ±1 multiplier on the PC (0x42cec0), which is applied to the **limit**
  when a stage starts (0x42df1e). Nothing about tension/compression is sent. The
  machine presumably drives toward the limit from wherever it is **(inferred)**.

**Which factories must exist.** `CMachineStageWrapper_v63` (0x4259b0) opens, in this
order: DoNothing, WaitShared, WaitExclusive, GainExclusive, ReleaseExclusive,
CrossheadRate, CrossheadPositioning, DriveNowhere, ZeroReadings, SetDatasourceRange,
GripProtection. Any failure there is reported (§6.2). It then opens LoadRate,
TrueStrainRate, SetManualSampleStiffness and SetAutoSampleStiffness, ignoring failures.
An "unknown service" reply (site state 3) means the option isn't fitted:

* without LoadRate, force-rate stages and creep holds are refused (0x427a63);
* without both stiffness factories, slot 5 is skipped (slot 62, 0x4289b0).

### 11.4 Slot map and connect-time setup

The console uses the arena as a fixed layout. When each factory open succeeds, it
installs that factory's permanent stage (0x426020):

| slot | stage | `next` at connect | role |
|---|---|---|---|
| 0 | none | 0 | parked. Also where a failed GainExclusive lands |
| 1 | WaitExclusive | 2 | wait loop while not under PC control |
| 2 | WaitShared | 1 | wait loop |
| 3 | GainExclusive | 4 | take control |
| 4 | GripProtection | 5 | rewritten at take-control (§11.5) |
| 5 | Set{Auto,Manual}SampleStiffness | 6 | |
| 6 | SetDatasourceRange | 7 | |
| 7 | none | 7 | **ready**: under PC control, not moving |
| 8 | ReleaseExclusive | 1 | give control back |
| 9 | ZeroReadings | 7 | `Console.Zero` |
| 10 | CrossheadPositioning → 0 mm at MaxSpeed | 7 | `Console.ReturnToZero` |
| 11 | CrossheadPositioning → 0 mm at MaxSpeed | 8 | return, then release (shows a cancel dialog) |
| 12–15 | segment bank A: drive, hold, next drive, DriveNowhere | rewritten per segment | §11.6 |
| 16–19 | segment bank B: same layout | rewritten per segment | §11.6 |
| 20–31 | unused | | |

The connect sequence on the wire:

1. Open `StageArenaReference5`, then send `[0x00, N]` to subscribe.
2. Open the factories (§11.3). As each one opens:
   * WaitExclusive `[01]`
   * WaitShared `[02]`
   * GainExclusive `[03]`
   * ReleaseExclusive `[08]`
   * DriveNowhere `[0F]`, then `[13]`
   * ZeroReadings `[09, mask...]`
   * CrossheadPositioning slots 10 and 11, with rate = MaxSpeed (vtable slot 47) and
     limit 0 mm. Both are sent when the open reply arrives.
3. When all eleven mandatory factories are open (0x426562), send to the arena:
   * `02 0C 00  00 02 01 04 05 06 07 07 01 07 07 08`: `next[0..11]`
   * `01 01 00  02`: `active[0] = 2`, i.e. leave slot 0 for the wait loop
4. Close the factories whose stages are permanent: DoNothing, WaitExclusive,
   WaitShared, GainExclusive, ReleaseExclusive, DriveNowhere, ZeroReadings and
   CrossheadPositioning. CrossheadRate, LoadRate, TrueStrainRate, GripProtection,
   SetDatasourceRange and the stiffness factories stay open for later use.
5. If the scheme's extension data source supports ranging, run take-control then
   release once (§11.5), to push the range setting.

**What slots 1 and 2 wait for (inferred).** WaitExclusive and WaitShared hand over to
each other through `next`. The PC breaks out with `active`. The NEXYGEN training
material (*NEXYGEN V4.0 Training* §2) says an LR/LRX must be put in "Remote Control"
on its keypad before the console goes active, but that applies to the older
LR/LRX/LF family (NEXYGEN manual ch. 5). Neither Plus-series manual mentions a remote
mode; the LR/EZ manual §6.1 says connection is automatic, and on an LF Plus control
was granted with nothing set on the frame (issue #1). So on a Plus frame these stages
most likely wait for "no local activity" (a running keypad test, say) rather than a
mode switch. The exact conditions are unverified.

### 11.5 Control operations

Notation: `A(start, [...])` = `[0x01, len, start, ...]` and `N(start, [...])` =
`[0x02, len, start, ...]`, both sent to the arena.

| operation | console entry | wire traffic |
|---|---|---|
| **Take control** (`Console.Control`, start of a test) | slot 32, 0x426980 | see below |
| **Release control** | slot 33, 0x426b20 | `A(0, [00 01 02 01 08 08 08 08 08 08 0B 0B 08×19 07])`: running slots → ReleaseExclusive (8); a return in progress (10, 11) → 11; GainExclusive → back to 1. Slot 31 → 7 is as found in the code |
| **Stop** (`Console.Stop`) | slot 53, 0x427580 | cancel the hold timer; `A(0, [00 01 02 03 04 05 06 07 08 09 07 08, 07×20])`: segment slots and slot 10 → 7 (ready); slot 11 → 8. Verified on hardware: sent alone during a drive it gives `16 → 7` and the head stops. The OLE `Stop` (dispatch thunk 0x42cee0, behind a precondition check at 0x42bb80 that throws if the console has no control) and the GUI (`CLrMachine_v53`) both reach this routine; the OLE call's failure to stop a drive in issue #1 therefore happened before this write was sent |
| **Zero** (`Console.Zero`) | slot 51, 0x427570 | `A(7, [09])`. Only acts while in slot 7, which is why the manual says Stop first |
| **ReturnToZero** | slot 50, 0x427550 | cancel the hold timer; `A(7, [0A])`. Only from slot 7, matching the manual's "only if the machine has been stopped" |
| cancel the return dialog | 0x4289a0 | `A(11, [08])` |
| **Start a drive stage** (`DriveStage.Start`) | slots 56/57 → 0x427910 | §11.6 |

`Console.Tension` and `Console.Compression` send nothing (§11.3, Direction).

**Take control**, in order:

1. GripProtection: `[04] "T" 00 "-C" 00`, from the scheme's Tensile/Compressive Load
   Limit, in N.
2. If both stiffness factories are open:
   * `N(5, [05])`, i.e. bytes `02 01 05 05` (0x426d40: count 1, start 5, value 5).
     This sets `next[5] = 5`, which step 3 overwrites at once. It looks like a bug in
     the original for `N(4, [05])`. It does no harm, because `next[4] = 5` is already
     set at connect;
   * then either `[05]` to SetAutoSampleStiffness, or `[05] "k" 00` to
     SetManualSampleStiffness, following the scheme's "Manual Sample Stiffness" flag.
     `k` is "Sample Stiffness" in N/mm.
3. Let `L` be 5 if step 2 ran, else 4. The extension source is the one the scheme names
   (default index 1). If that source supports ranging (its record field +0x68 is
   non-zero):
   * `N(L, [06])`;
   * `[06, src] "r" 00` to SetDatasourceRange, with `r` = the scheme's "Extensometer
     Range" in mm.

   Otherwise `N(L, [07])`.
4. `A(1, [03 03])`: leave the wait loop into GainExclusive.
5. `A(3, [00])`: if GainExclusive is still current, divert to slot 0. The console
   reports "Exclusive control could not be gained" when slot 0 is entered. This only
   works if GainExclusive normally completes before this packet is processed, so it
   reads as "fail if not gained at once" **(inferred**; it is timing-dependent and
   worth confirming on hardware).

The result is the chain 3 → 4 → (5) → (6) → 7. The machine ends up in slot 7, ready.

### 11.6 Drive segments (DriveStage)

The automation `DriveStage` object has Limit, Speed, HoldTime and Next (manual §5.4).
`Start` (0x42de20) calls the machine with (limit × direction, speed, hold time, …), plus
the Next stage's limit and speed if Next is set (slot 56) or not (slot 57). Both end in
0x427910, which writes one **segment** into a bank:

* The bank base `b` alternates between **16 and 12** (flag at wrapper+0x1b0, toggled
  after every segment). The first segment after the stage-wrapper object is constructed
  uses 16; the flag is zeroed only in the constructor (~0x42563b). So a new segment is always loaded into
  the bank that isn't running, and can be switched to without a gap.
* Slot `b`: **the drive.** The factory is chosen by the rate:
  * a force rate (N/s) → LoadRate (refused if not fitted);
  * otherwise, a strain source ≥ 0 → TrueStrainRate;
  * otherwise → CrossheadRate.

  Packet: `[b] "speed" 00 [src] "limit" 00 [FF] "0.000000e+000" 00`.
* Slot `b+1`, only if HoldTime ≠ 0: **the hold.** A motion stage with the same limit
  and **rate 0**:
  * a force limit → LoadRate at 0 N/s (creep: hold load);
  * otherwise → CrossheadRate (or TrueStrainRate) at 0 mm/s (relaxation: hold
    position).

  The hold time itself (stored in ms at wrapper+0x1a8) is **not sent** (§11.7).
* Slot `b+2`, only if Next is set: **the next drive**, built like slot `b` from Next's
  limit and speed. This is how Next avoids the pause between stages that the manual
  describes.
* Slot `b+3`: DriveNowhere, installed at connect.

Then two arena writes:

```
N(b, [n0, n1, n2])   with  n0 = b+1 if hold, else b+2 if next, else b+3
                           n1 = b+2 if next, else b+3
                           n2 = b+3
A(0, [00 01 02 03 04 05 06  b  08 09  b  b,  b×8 (slots 12–19),  07×12 (slots 20–31)])
```

The `active` write starts the segment from slot 7 (ready), from a return in progress
(10, 11), or from any slot of either bank. That last case is the manual's "a stage will
be terminated early by starting another stage". From slots 0–6, 8 and 9, nothing
happens: you can't start a drive while not in control or while zeroing.

**Worked example.** The manual's `Stage1.limit = "50mm"`,
`Stage1.speed = "100mm/min"`, `Stage1.Start`, in tension, first segment (b = 16),
extension source 1:

```
CrossheadRate       10 "1.666667e+000" 00 01 "5.000000e+001" 00 FF "0.000000e+000" 00
StageArenaReference5  02 03 10  13 13 13
StageArenaReference5  01 20 00  00 01 02 03 04 05 06 10 08 09 10 10
                                10 10 10 10 10 10 10 10 07 07 07 07 07 07 07 07 07 07 07 07
machine → notify      00 07 10            (7 → 16: driving)
machine → notify      00 10 13            (16 → 19: limit reached, DriveNowhere)
```

(100 mm/min = 1.6666667 mm/s. Hand-assembled from the code paths above, not captured.)

### 11.7 What the PC decides

* **Hold time.** Entering `b+1` starts a PC timer (0x408b70, timer id 0x65) for the hold
  time. When it fires (0x427520 → 0x4274f0), the console marks the stage finished and
  detaches the readings from it. **Nothing is sent**: the machine stays in the hold
  stage, still holding, until the client starts another segment or calls Stop. Verified:
  a rate-0 stage does not complete by itself (position and load holds both sat in `b+1`
  until redirected). **A rate-0 LoadRate stage holds position, not load**: measured on
  2026-10-23 with both drivers, a hold entered at −21.2 N relaxed to −17.4 N over 3 s with
  the extension frozen at −0.4260 mm (an earlier note that it re-regulated was a 0.02 mm
  reading within noise). A constant-force hold therefore has to be a PC-side loop of
  load-limited drives.
* **Finished.** A `DriveStage` reads as finished when the machine enters 7, 8, `b+2`
  or `b+3` (§11.8).
* **Break detection.** `NewGradientBreakDetectorStage` (fast break) and
  `NewDropBreakDetectorStage` (percentage drop) run on the PC over the readings. They
  only raise their own Finished event; no machine traffic was found in them. The
  manual's pull-to-break example calls `Console.Stop` itself when either object
  finishes.

### 11.8 Console reaction to transition reports

`CMachineStageWrapper_v37` (0x427300), keyed on the new slot:

| new slot | console action |
|---|---|
| 0 | error "Exclusive control could not be gained" |
| 1, 2, 4, 5, 6, 9, 10 | nothing |
| 3 | clear the "take control pending" flag (wrapper+0x38) |
| 7, 8, 14, 15, 18, 19 | the current drive stage is finished (notify its automation object) |
| 11 | show the cancel-return dialog |
| 12, 16 (`b`) | one pending segment has started; when none remain pending, the new stage object becomes the running one |
| 13, 17 (`b+1`) | start the hold timer, if no newer segment is pending |
| 20–31 | error "Unexpected stage entered" |

Independently, leaving slot 11 (`old == 11`) hides the cancel-return dialog.

### 11.9 `lloydplus.stages` versus the console

`StageControl` sends the console's packets, with these deliberate differences:

* **Too-slow speeds.** A speed that the clamp would turn into 0 raises `ValueError`
  instead of being sent as a non-moving drive. A clamp to MaxSpeed is logged.
* **Hold expiry.** When a hold time expires, it writes `active[b+1] = b+2` (or `b+3`).
  The machine then moves on to the next drive or the stop slot. The console sends
  nothing and leaves the machine holding (§11.7), so a Next after a hold would never
  run.
* **Failed take-control.** After GainExclusive fails (slot 0), it re-writes
  `active[0] = 2` so the machine goes back to the wait loop and a retry can work.
* **Stop.** `stop()` sends two writes: first `HALT_ACTIVE`,
  `A(0, [00..09, 0F×6 (slots 10–15), 13×4 (slots 16–19), 07×12])`, which redirects a
  running drive, hold, next drive or return into its bank's DriveNowhere stage (15 or
  19), then the console's `STOP_ACTIVE` to go from there to 7. Verified on an LF Plus:
  the machine reports `16 → 19 → 7` within a millisecond and the head halts about
  0.1 mm on at 10 mm/min. The console's single write also works (§11.1); the extra
  write was added when issue #1 suggested otherwise, and is kept because it stops
  through a stage built for the purpose and costs one report. A segment ended by
  `stop()` finishes with reason `"stopped"` even if it entered `b+3` by reaching its
  limit in the same instant.
* **Grip-protection trip.** On warning 10 (§11.3) `StageControl` ends the running and
  pending segments with reason `"load safety limit"` and issues the stop writes, so
  `drive()` returns instead of waiting forever in the drive slot. The console leaves
  the stage object waiting.
* **Stop before Zero/ReturnToZero.** If a segment is running or finished in a bank,
  Zero and ReturnToZero issue Stop first. The console requires the script to do it.
* **Release on close.** `LloydPlus.close()` stops and releases control if the PC holds
  it.
* **Not offered:** TrueStrainRate stages (unit unresolved), the cancel-return slot 11
  flow, and the extension-source ranging check at connect (§11.4 step 5).
* **Stage setup is opt-in for `info` / `log`.** The CLI's read-only commands connect
  with `control=False`, so they still send only handshake and query traffic.
* **Re-taking control.** Calling `take_control` while already in control first stops.
  With the same settings it then returns. With different protection settings it
  releases and takes control again, so the new grip limits really are sent.
* **Timeouts and link loss.** `drive()` stops the machine before raising on timeout.
  A link reset or watchdog timeout ends all segments with reason "link lost" and wakes
  every waiting call.
* **Connect order.** Permanent stages are sent as each factory opens, in the console's
  open order. The `next`/`active` writes come after the mandatory factories. The
  optional factories are opened afterwards; the console opens them concurrently.

## 12. Console messages relevant to the link

From the LrConRes.dll string table:

| id | text |
|---|---|
| 0x3E8 | An error has ocurred: %1 Press OK to reconnect |
| 0x3E9–0x3EB | could not connect: upgrade machine / upgrade software / reset machine (§6.2) |
| 0x3F1 | User disconnected. |
| 0x3F2 | Connection closed remotely. |
| 0x3F6 | The controller does not have exclusive control of the machine. |
| 0x3FE | Machine is no longer connected. |
| 0x400 | Timeout waiting for machine reply. |
| 0x402.. | machine message codes (§8.7) |

The console's internal protocol error strings are useful when matching behaviour in
a capture:

* "Packet too long"
* "Untransmitted queue full" / "Untransmitted queue too long"
* "Cannot allocate channel number"
* "Channel not yet ready" / "obsolete" / "unknown" / "creation failed"
* "Version packet length bad"
* "Parameters packet length bad" / "Parameters packet content bad"
* "DS_ACCUM packet bad" / "DS_ZERO packet bad" / "DS_TIME packet bad"
* "time_increment bad"
* "Load data information bad" / "Extension data information bad"
* "Data source type bad"
* "Data information index already used"
* "Status information packet length bad"

Stage subsystem (§11):

* "Change next stage bad parameters" / "Change active stage bad parameters"
* "Stage changed out of sequence"
* "Unexpected stage entered"
* "Exclusive control could not be gained"
* "Feature used but not available" (a machine call that no layer implements)

## 13. Open questions and how to settle them

| question | how to settle |
|---|---|
| Does the LF Plus speak this protocol? | **Answered:** yes, verified with this library (§0). |
| Does the machine gate its TX on CTS? | **Answered** (issue #1): yes. |
| Does `stop()` halt a drive? (§11.9) | **Answered:** yes, `16 → 19 → 7`, halt within ~0.1 mm at 10 mm/min. |
| Does a redirect into slot 7 stop motion? | **Answered:** yes, `16 → 7` in 0.23 s at 60 mm/min, head stationary after. |
| Units (§10) | **Answered** for mm and mm/s (commanded travel and time agree with the readings). N: consistent with a known foam compression; a calibrated weight would make it exact. |
| Machine-side boot behaviour | **Answered:** BOOT state 0 flooded at line rate while idle; `Growing` / `Maturing` trace frames as the link comes up (§4.1). |
| `DataInformation` `extra` and the a/b fields | Read real records with `lloydplus -v info`. |
| `KeypadEZ` codes | Unknown; not needed. |
| Exact machine-side timing | A capture of a real NEXYGEN session through a passive RS-232 tap, decoded with `lloydplus sniff --file`, would confirm the whole of §2–§11 at once. |
| The take-control race (§11.5 step 5) | Worked three times out of three (`1 → 3 → 4 → 5 → 7`, all reports within 1 ms). Still worth a look at the wire timing of `01 01 03 00` relative to the `3 → 4` report. |
| What WaitExclusive / WaitShared wait for (§11.4) | Still open. Three take-controls in a row went straight through; nothing was done at the frame during them. |
| What an empty slot does | **Answered** for 7: the machine sits there, motor stopped, until redirected. Slot 0 (failed gain) not yet seen. |
| Does a rate-0 hold ever complete by itself? (§11.7) | **Answered:** no; both position and load holds waited for the PC's write. A load hold does not re-regulate either. |
| Does the machine accept two-digit exponents? | Send `5.0e+01` instead of `5.000000e+001` and compare. Until then, emit MSVC's form (verified accepted). |
| True-strain-rate units (§11.3) | Unresolved statically; a NEXYGEN capture of a strain-controlled test. |
| The self-test menu (§1) | Answered `test` for the colleague on a fresh machine; did not answer for us after a PC session, with RTS high or low. Try `lloydplus diag` first thing after power-on. |
| Grip protection on load-limited drives | **Answered:** it does not apply to them (§11.3); only the drive's own limit stops a load-limited stage, so give it a sane target. |

## 14. Reproducing the analysis

The tools are in `re/`:

* **Extract the installer:** the InstallShield cabinets on the CD unpack with
  `unshield` (built from github.com/twogood/unshield):
  `unshield -d out x data1.cab`. The console is `out/EZConsole/LrCon.exe`.
* **Recover vtables:** `re/rtti.py LrCon.exe > vtables.txt` scans MSVC RTTI
  (TypeDescriptor → CompleteObjectLocator → vtable). `re/ApplyNames.java` takes a
  names file derived from that output and names the functions `Class_vN` in Ghidra.
* **Decompile:** `re/DecompAll.java` dumps a full Ghidra decompile, e.g.
  `analyzeHeadless proj lrcon -import LrCon.exe -postScript DecompAll.java out.c`.
  Ghidra often drops stack arguments of `__thiscall` calls, notably channel and
  protocol bytes, so check those with `objdump -d -M intel`.
* **Message text:** `re/strtab.py LrConRes.dll` dumps the string table (from
  `Lang_English(UK)`).
