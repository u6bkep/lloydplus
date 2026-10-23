"""Run the protocol simulator behind a TCP socket, or on a real serial port.

For clients in other processes or languages (the Rust crate's tests connect
to it).  One simulated machine per connection; the byte stream is exactly what
the serial port would carry.

    python -m lloydplus.simserver            # ephemeral port, prints "PORT n"
    python -m lloydplus.simserver --port 4321 --forever
    python -m lloydplus.simserver --serial /dev/ttyUSB1   # be the machine on a port

With ``--serial`` the simulator plays the machine end of an RS-232 lead (57600
8N1, RTS/CTS), so a controller under test can be brought up against it with
real wiring and flow control.
"""

from __future__ import annotations

import argparse
import socket
import sys
import threading

from .sim import SimMachine


class SocketPort:
    """A connected socket as a :class:`lloydplus.link.Port`."""

    def __init__(self, sock: socket.socket) -> None:
        sock.settimeout(0.05)
        self.sock = sock
        self.closed = threading.Event()

    def read(self, size: int = 1) -> bytes:
        try:
            data = self.sock.recv(size)
        except socket.timeout:
            return b""
        except OSError:
            self.closed.set()
            return b""
        if not data:
            self.closed.set()
        return data

    def write(self, data: bytes) -> int:
        try:
            self.sock.sendall(data)
        except OSError:
            self.closed.set()
        return len(data)

    def close(self) -> None:
        self.sock.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0, help="TCP port (0 = ephemeral, printed)")
    ap.add_argument("--forever", action="store_true", help="serve connections until killed")
    ap.add_argument("--stiffness", type=float, default=10.0, help="sample stiffness, N/mm")
    ap.add_argument("--local", action="store_true", help="frame not in Remote Control")
    ap.add_argument("--gap", action="store_true",
                    help="compression-only specimen with its surface at 0 mm (free air above)")
    ap.add_argument("--sample-period", type=int, default=10, help="ticks between samples")
    ap.add_argument("--serial", metavar="DEV",
                    help="play the machine on this serial port instead of TCP (57600 8N1 RTS/CTS)")
    ap.add_argument("--no-rtscts", action="store_true", help="with --serial: no hardware flow control")
    args = ap.parse_args(argv)
    kw = dict(stiffness=args.stiffness, remote=not args.local, gap=args.gap,
              sample_period_ticks=args.sample_period)

    if args.serial:
        from .machine import open_serial
        port = open_serial(args.serial, rtscts=not args.no_rtscts)
        print(f"machine on {args.serial} (rtscts={not args.no_rtscts})", file=sys.stderr, flush=True)
        sim = SimMachine(port, **kw)
        sim.start()
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
        finally:
            sim.stop()
            port.close()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    print(f"PORT {srv.getsockname()[1]}", flush=True)
    try:
        while True:
            conn, peer = srv.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            print(f"client {peer[0]}:{peer[1]}", file=sys.stderr, flush=True)
            port = SocketPort(conn)
            sim = SimMachine(port, **kw)
            sim.start()
            port.closed.wait()
            sim.stop()
            port.close()
            print("client gone", file=sys.stderr, flush=True)
            if not args.forever:
                return 0
    except KeyboardInterrupt:
        return 0
    finally:
        srv.close()


if __name__ == "__main__":
    sys.exit(main())
