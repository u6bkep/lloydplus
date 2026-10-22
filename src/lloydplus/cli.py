"""Command line: ``lloydplus {sniff,diag,info,log,move,zero,home}``."""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time

from .framing import FrameDecoder
from .link import PROTO_BOOT, PROTO_TRANSMISSION


def describe_body(body: bytes) -> str:
    proto, p = body[0], body[1:]
    if proto == PROTO_BOOT:
        return f"BOOT state={p[0] if p else '?'}"
    if proto == PROTO_TRANSMISSION:
        if len(p) == 1:
            return f"ACK {p[0]}"
        if len(p) < 3:
            return f"TX {p.hex(' ')}"
        ack, seq, ch, data = p[0], p[1], p[2], p[3:]
        extra = ""
        if ch == 0 and len(data) > 1 and all(32 <= c < 127 for c in data[1:]):
            extra = f"  OPEN {data[1:].decode()!r} reply->ch{data[0]}"
        elif ch == 0 and len(data) == 1:
            extra = f"  CLOSE ch{data[0]}"
        return f"TX ack={ack} seq={seq} ch={ch} [{len(data)}] {data.hex(' ')}{extra}"
    return f"proto={proto:#04x} {p.hex(' ')}"


def cmd_sniff(args) -> int:
    """Listen only.  Never writes a byte to the port."""
    dec = FrameDecoder()
    if args.file:
        data = open(args.file, "rb").read()
        for body in dec.feed(data):
            print(describe_body(body))
        print(f"# frames={dec.frames} crc_errors={dec.crc_errors}", file=sys.stderr)
        return 0
    import serial
    s = serial.Serial(args.port, args.baud, rtscts=False, timeout=0.2)
    s.rts = True     # let a machine that gates its TX on CTS talk
    s.dtr = False
    raw = open(args.raw, "ab") if args.raw else None
    t0 = time.monotonic()
    total = 0
    print(f"listening on {args.port} @ {args.baud} (Ctrl+C to stop)", file=sys.stderr)
    try:
        while True:
            chunk = s.read(4096)
            if not chunk:
                continue
            total += len(chunk)
            if raw:
                raw.write(chunk)
                raw.flush()
            bodies = dec.feed(chunk)
            for body in bodies:
                print(f"{time.monotonic() - t0:9.3f}  {describe_body(body)}", flush=True)
            if not bodies and args.verbose:
                print(f"{time.monotonic() - t0:9.3f}  raw {chunk.hex(' ')}", flush=True)
    except KeyboardInterrupt:
        pass
    print(f"# bytes={total} frames={dec.frames} crc_errors={dec.crc_errors}", file=sys.stderr)
    return 0


def _raw_port(port: str, baud: int, rts: bool = True):
    import serial
    s = serial.Serial(port, baud, rtscts=False, timeout=0.2)
    s.rts = rts          # the machine only transmits while its CTS is asserted
    s.dtr = False
    return s


def cmd_diag(args) -> int:
    """The firmware's plain-text self-test menu (Plus service manual §11.14).

    No link protocol involved: ``test\r`` at 57600 8N1 prints a menu, ``v``
    dumps the analogue channels, ``X`` leaves.  Quirks seen on an LF Plus
    (issue #1): lines end LF CR; a second ``test`` toggles "tracing on" /
    "tracing off" instead of reprinting the menu, so this command sends it
    once and undoes an accidental toggle.  The frame must be idle: for ~4 s
    after a PC session it still streams readings, and when idle it floods
    ``BOOT`` frames, which this strips from the output.
    """
    s = _raw_port(args.port, args.baud, rts=args.rts)

    def ask(cmd: bytes, quiet: float = 0.4) -> str:
        s.write(cmd)
        buf = b""
        t0 = time.monotonic()
        t_end = t0 + args.wait
        while time.monotonic() < min(t_end, t0 + 2 * args.wait):   # bounded even if it streams
            chunk = s.read(256)
            if chunk:
                buf += chunk
                t_end = time.monotonic() + quiet
        # An idle machine floods the line with 6-byte BOOT frames; keep only text.
        import re
        text = b"".join(m + b"\n" for m in re.findall(rb"[\x20-\x7e]{4,}", buf))
        return text.decode("ascii")

    try:
        menu = ask(b"test\r")
        if "tracing on" in menu:
            print("# firmware was already in its menu: 'test' switched tracing on; "
                  "switching it off", file=sys.stderr)
            print(ask(b"test\r"), end="")
            menu = ask(b"m")
        print(menu, end="")
        if not menu.strip():
            print("# no answer: check the lead (pins 7-8 crossed), the port and the baud "
                  "rate; the frame must be idle, not in a PC session", file=sys.stderr)
            return 1
        if args.voltages:
            v = ask(b"v")
            if not v.strip():
                v = ask(b"v\r")
            print(v, end="")
    finally:
        try:
            s.write(b"X")
        finally:
            s.close()
    return 0


def _connect(args, control: bool = True):
    """``control=False`` keeps to link handshake and queries (no stage setup)."""
    from .machine import LloydPlus
    m = LloydPlus(args.port, rtscts=not args.no_rtscts)
    m.on_message(lambda msg: print(f"# machine {msg.source}: {msg.code} {msg.description}",
                                   file=sys.stderr))
    m.connect(timeout=args.timeout, control=control)
    return m


def cmd_info(args) -> int:
    m = _connect(args, control=False)
    try:
        print("version     :", ".".join(map(str, m.version)))
        print("description :", " | ".join(m.description))
        print("parameters  :", m.parameters)
        for s in m.sources:
            print(f"source {s.index}    : kind={s.kind} range={s.range:g} name={s.name!r}")
        time.sleep(1.0)
        for idx, smp in sorted(m.latest.items()):
            print(f"latest {idx}    : {smp.value:.6g} (raw {smp.raw}, t={smp.t:.3f}s)")
    finally:
        m.close()
    return 0


def cmd_log(args) -> int:
    m = _connect(args, control=False)
    names = {s.index: (s.name or f"source{s.index}") for s in m.sources}
    out = open(args.csv, "w", newline="") if args.csv else sys.stdout
    w = csv.writer(out)
    w.writerow(["t_s", "source", "name", "value", "raw"])
    t_end = time.monotonic() + args.seconds if args.seconds else None
    try:
        for smp in m.samples(timeout=2.0):
            w.writerow([f"{smp.t:.4f}", smp.source, names.get(smp.source, ""),
                        repr(smp.value), smp.raw])
            if t_end and time.monotonic() > t_end:
                break
    except KeyboardInterrupt:
        pass
    finally:
        m.close()
        if out is not sys.stdout:
            out.close()
    return 0


def _take_control(args, m) -> None:
    if m.control is None:
        raise SystemExit("this machine did not offer the stage services; no test control")
    # Default grip protection: 10 % of the frame's load range, which is what the
    # console's factory defaults (100 N / 100 N) come to on a 1 kN frame.  On
    # hardware this latch is what ended two runaway moves (issue #1).
    default = m.parameters.load_range / 10
    tensile = args.tensile_limit if args.tensile_limit is not None else default
    compressive = args.compressive_limit if args.compressive_limit is not None else default
    print(f"grip protection: +{tensile:g} N tension / -{compressive:g} N compression",
          file=sys.stderr)
    m.control.take_control(tensile_limit=tensile, compressive_limit=compressive)


def _interruptible_stop(m, seg=None) -> None:
    """Stop on Ctrl+C.  A terminal delivers SIGINT to the whole foreground
    group, so a second KeyboardInterrupt can arrive while we wait for the
    machine; keep stopping rather than unwinding through it."""
    for _ in range(5):
        try:
            print("stopping", file=sys.stderr)
            m.control.stop()
            if seg is not None:
                seg.wait(1.0)
            return
        except KeyboardInterrupt:
            continue


def _readings(m) -> str:
    load, ext = m.load, m.extension
    return (f"load {load:10.3f} N   extension {ext:10.4f} mm"
            if load is not None and ext is not None else "(no readings yet)")


def cmd_move(args) -> int:
    from .stages import Drive
    if (args.to is None) == (args.load is None):
        raise SystemExit("give exactly one of --to MM or --load N")
    speed = args.speed / 60.0
    drive = (Drive.to_extension(args.to, speed, args.hold) if args.to is not None
             else Drive.to_load(args.load, speed, args.hold))
    m = _connect(args)
    try:
        _take_control(args, m)
        seg = m.control.start(drive)
        print(f"moving: {drive}  (Ctrl+C stops)", file=sys.stderr)
        try:
            while not seg.wait(0.5):
                print(_readings(m), flush=True)
        except KeyboardInterrupt:
            _interruptible_stop(m, seg)
        print(f"done ({seg.reason}): {_readings(m)}")
    finally:
        m.close()           # stops and releases control
    return 0


def cmd_zero(args) -> int:
    m = _connect(args)
    try:
        _take_control(args, m)
        m.control.zero()
        time.sleep(0.5)
        print(_readings(m))
    finally:
        m.close()
    return 0


def cmd_home(args) -> int:
    m = _connect(args)
    try:
        _take_control(args, m)
        print("returning to 0 mm at maximum speed (Ctrl+C stops)", file=sys.stderr)
        try:
            m.control.return_to_zero(wait=False)
            while m.control.slot != 7:
                print(_readings(m), flush=True)
                time.sleep(0.5)
        except KeyboardInterrupt:
            _interruptible_stop(m)
        print(_readings(m))
    finally:
        m.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lloydplus")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sniff", help="listen-only frame decoder (never transmits)")
    sp.add_argument("--port")
    sp.add_argument("--baud", type=int, default=57600)
    sp.add_argument("--file", help="decode a raw capture file instead of a port")
    sp.add_argument("--raw", help="append raw received bytes to this file")
    sp.set_defaults(func=cmd_sniff)

    dp = sub.add_parser("diag", help="firmware self-test menu and voltages (plain text, "
                                     "no link protocol)")
    dp.add_argument("--port", required=True)
    dp.add_argument("--baud", type=int, default=57600)
    dp.add_argument("--wait", type=float, default=2.0, help="seconds to wait for an answer")
    dp.add_argument("--no-voltages", dest="voltages", action="store_false",
                    help="only print the menu")
    dp.add_argument("--no-rts", dest="rts", action="store_false",
                    help="leave RTS low (the machine then keeps its BOOT flood to itself)")
    dp.set_defaults(func=cmd_diag)

    for name, func, hlp in (("info", cmd_info, "connect and print machine information"),
                            ("log", cmd_log, "stream readings to CSV"),
                            ("move", cmd_move, "drive the crosshead to an extension or load"),
                            ("zero", cmd_zero, "zero the load and extension readings"),
                            ("home", cmd_home, "return the crosshead to 0 mm")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--port", required=True)
        p.add_argument("--timeout", type=float, default=10.0)
        p.add_argument("--no-rtscts", action="store_true",
                       help="disable RTS/CTS (only with 7-8 jumpered at the machine)")
        if name == "log":
            p.add_argument("--csv")
            p.add_argument("--seconds", type=float)
        if name in ("move", "zero", "home"):
            p.add_argument("--tensile-limit", type=float, metavar="N",
                           help="grip protection, tension, positive N "
                                "(default: 10%% of the frame's load range)")
            p.add_argument("--compressive-limit", type=float, metavar="N",
                           help="grip protection, compression, positive N "
                                "(default: 10%% of the frame's load range)")
        if name == "move":
            p.add_argument("--to", type=float, metavar="MM",
                           help="extension limit, mm from the last zero (down is negative)")
            p.add_argument("--load", type=float, metavar="N",
                           help="load limit, N (tension positive, compression negative)")
            p.add_argument("--speed", type=float, required=True, metavar="MM_PER_MIN")
            p.add_argument("--hold", type=float, default=0.0, metavar="S",
                           help="hold at the limit for S seconds")
        p.set_defaults(func=func)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.cmd == "sniff" and not (args.port or args.file):
        ap.error("sniff needs --port or --file")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
