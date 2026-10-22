"""Command line: ``lloydplus {sniff,info,log}``."""

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


def _connect(args):
    from .machine import LloydPlus
    m = LloydPlus(args.port, rtscts=not args.no_rtscts)
    m.on_message(lambda msg: print(f"# machine {msg.source}: {msg.code} {msg.description}",
                                   file=sys.stderr))
    m.connect(timeout=args.timeout)
    return m


def cmd_info(args) -> int:
    m = _connect(args)
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
    m = _connect(args)
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

    for name, func, hlp in (("info", cmd_info, "connect and print machine information"),
                            ("log", cmd_log, "stream readings to CSV")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--port", required=True)
        p.add_argument("--timeout", type=float, default=10.0)
        p.add_argument("--no-rtscts", action="store_true",
                       help="disable RTS/CTS (only with 7-8 jumpered at the machine)")
        if name == "log":
            p.add_argument("--csv")
            p.add_argument("--seconds", type=float)
        p.set_defaults(func=func)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.cmd == "sniff" and not (args.port or args.file):
        ap.error("sniff needs --port or --file")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
