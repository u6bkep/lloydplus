"""High-level connection to a Plus-series frame."""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

from . import services as svc
from .channels import ChannelMux, ServiceClient, ServiceOpenError
from .link import Link, Port

log = logging.getLogger(__name__)

BAUDRATE = 57600


def open_serial(device: str, *, rtscts: bool = True, dtr: bool = False,
                baudrate: int = BAUDRATE):
    """Open the PC port the way the original console does: 57600 8N1, RTS/CTS
    flow control, DTR off (DCB in CWin32Hardware 0x40ec00).

    RTS/CTS needs a lead that crosses pins 7/8 (Lloyd 09/0639).  With a lead
    that only crosses 2/3, pass ``rtscts=False`` and jumper 7-8 at the
    machine end, otherwise neither side may ever be clear to send.
    """
    import serial  # pyserial

    s = serial.Serial()
    s.port = device
    s.baudrate = baudrate
    s.bytesize = serial.EIGHTBITS
    s.parity = serial.PARITY_NONE
    s.stopbits = serial.STOPBITS_ONE
    s.rtscts = rtscts
    s.dsrdtr = False
    s.timeout = 0.05
    s.write_timeout = 2.0
    s.dtr = dtr
    s.open()
    s.reset_input_buffer()
    return s


class LloydPlus:
    """Connect, read machine information and stream readings.

    Usage::

        with LloydPlus("/dev/ttyUSB0") as m:
            print(m.version, m.description, m.parameters)
            for sample in m.samples(timeout=5):
                print(sample)
    """

    def __init__(self, port: str | Port, *, rtscts: bool = True, dtr: bool = False,
                 on_message: Callable[[svc.MachineMessage], None] | None = None,
                 watchdog_interval: float = 10.0) -> None:
        self._own_port = isinstance(port, str)
        self.port = open_serial(port, rtscts=rtscts, dtr=dtr) if isinstance(port, str) else port
        self.watchdog_interval = watchdog_interval
        self._message_cbs: list[Callable[[svc.MachineMessage], None]] = []
        if on_message:
            self._message_cbs.append(on_message)
        self._sample_cbs: list[Callable[[svc.Sample], None]] = []
        self._sample_queue: queue.Queue[svc.Sample] = queue.Queue(maxsize=100_000)
        self.latest: dict[int, svc.Sample] = {}
        self.messages: list[svc.MachineMessage] = []
        self.lost = threading.Event()

        self.link = Link(self.port, self._on_data, on_reset=self._on_link_reset, name="pc")
        self.mux = ChannelMux(self.link)
        svc.install_local_services(self.mux, on_message=self._on_message)

        self.version: tuple[int, int, int] | None = None
        self.description: tuple[str, str, str] | None = None
        self.parameters: svc.MachineParameters | None = None
        self.sources: list[svc.DataSourceInfo] = []
        self._held: list[ServiceClient] = []
        self._status: svc.StatusMonitor | None = None
        self._stream: svc.DataSetStream | None = None
        self._watchdog: svc.Watchdog | None = None
        self.responder: ServiceClient | None = None
        self.keypad: ServiceClient | None = None

    # -- plumbing ---------------------------------------------------------

    def _on_data(self, data: bytes) -> None:
        self.mux.on_data(data)

    def _on_link_reset(self) -> None:
        log.error("machine restarted the link; connection lost")
        self.mux.reset()
        self.lost.set()

    def _on_message(self, msg: svc.MachineMessage) -> None:
        self.messages.append(msg)
        for cb in list(self._message_cbs):
            try:
                cb(msg)
            except Exception:
                log.exception("message callback failed")

    def _on_sample(self, s: svc.Sample) -> None:
        self.latest[s.source] = s
        try:
            self._sample_queue.put_nowait(s)
        except queue.Full:
            pass
        for cb in list(self._sample_cbs):
            try:
                cb(s)
            except Exception:
                log.exception("sample callback failed")

    def on_message(self, cb: Callable[[svc.MachineMessage], None]) -> None:
        self._message_cbs.append(cb)

    def on_sample(self, cb: Callable[[svc.Sample], None]) -> None:
        self._sample_cbs.append(cb)

    # -- connection -------------------------------------------------------

    def connect(self, timeout: float = 10.0, *, stream: bool = True,
                set_clock: bool = True) -> "LloydPlus":
        """Bring up the link and the services, in the console's order
        (CNextStepMachineVisitor): Responder + NotImplementedDataSources,
        KeypadEZ, SetSystemTime, watchdog, Description, Version,
        StatusInformation, TensileParameters, DataInformation -> DataSet."""
        self.link.start()
        if not self.link.wait_connected(timeout):
            raise TimeoutError(
                "no link handshake from the machine (check lead: RX/TX and RTS/CTS crossed, "
                "57600 baud, machine idle, NEXYGEN not running)")
        m = self.mux
        self.responder = ServiceClient(m, "Responder")
        self._held.append(self.responder)
        self._held.append(self._try_open("NotImplementedDataSources"))
        self.keypad = self._try_open("KeypadEZ")
        self._held.append(self.keypad)
        if set_clock:
            try:
                svc.set_system_time(m)
            except (ServiceOpenError, TimeoutError) as e:
                log.warning("SetSystemTime: %s", e)
        if self.watchdog_interval:
            self._watchdog = svc.Watchdog(m, self.responder, self._on_watchdog_timeout,
                                          self.watchdog_interval)
        self.description = svc.query_description(m)
        self.version = svc.query_version(m)
        self._status = svc.StatusMonitor(m, self._on_message)
        self.parameters = svc.query_parameters(m)
        self.sources = svc.query_data_information(m)
        if stream:
            self.start_stream()
        return self

    def _try_open(self, name: str) -> ServiceClient | None:
        try:
            return ServiceClient(self.mux, name)
        except ServiceOpenError as e:
            log.warning("%s", e)
            return None

    def _on_watchdog_timeout(self) -> None:
        self.lost.set()

    def start_stream(self) -> None:
        if self._stream is None:
            self._stream = svc.DataSetStream(self.mux, self.sources, self._on_sample)

    def stop_stream(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def close(self) -> None:
        if self._watchdog:
            self._watchdog.stop()
        try:
            self.stop_stream()
            if self._status:
                self._status.close()
            for s in self._held:
                if s:
                    s.close()
        except Exception:
            pass
        # give the link a moment to flush the close requests
        threading.Event().wait(0.2)
        self.link.stop()
        if self._own_port:
            self.port.close()

    def __enter__(self) -> "LloydPlus":
        return self.connect()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- readings ---------------------------------------------------------

    def samples(self, timeout: float | None = None):
        """Iterate over incoming samples (all sources); stops after
        ``timeout`` seconds without data."""
        while True:
            try:
                yield self._sample_queue.get(timeout=timeout)
            except queue.Empty:
                return

    def source_index(self, kind: int) -> int | None:
        for s in self.sources:
            if s.kind == kind:
                return s.index
        return None

    @property
    def load(self) -> float | None:
        s = self.latest.get(0)
        return s.value if s else None

    @property
    def extension(self) -> float | None:
        s = self.latest.get(1)
        return s.value if s else None

    def press_key(self, code: int) -> None:
        """Send a raw byte to the "KeypadEZ" service.  The console exposes
        codes 0..4 only from a hidden debug menu; their meaning is unknown."""
        if self.keypad is None:
            raise RuntimeError("KeypadEZ service not available")
        self.keypad.send(bytes((code,)))
