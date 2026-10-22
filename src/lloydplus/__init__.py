"""Driver for the RS-232 PC link of Lloyd Instruments "Plus" series frames.

The protocol was reverse engineered from NEXYGEN 4.1 (``EZConsole/LrCon.exe``);
see PROTOCOL.md for the wire format and the evidence behind each claim.
"""

from .channels import ChannelMux, ServiceClient, ServiceOpenError
from .framing import FrameDecoder, crc16_arc, encode_frame
from .link import Link
from .machine import LloydPlus, open_serial
from .services import DataSourceInfo, MachineMessage, MachineParameters, Sample
from .stages import Drive, Segment, StageControl, StageError

__all__ = [
    "ChannelMux", "ServiceClient", "ServiceOpenError", "FrameDecoder", "crc16_arc",
    "encode_frame", "Link", "LloydPlus", "open_serial", "DataSourceInfo",
    "MachineMessage", "MachineParameters", "Sample", "Drive", "Segment", "StageControl",
    "StageError",
]
