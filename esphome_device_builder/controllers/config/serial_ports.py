"""Serial-port enumeration enriched with USB metadata for the port pickers."""

from __future__ import annotations

from typing import Literal, TypedDict

from esphome.util import get_serial_ports
from serial.tools.list_ports import comports

# Espressif's USB VID — the built-in USB-Serial-JTAG / USB-OTG peripheral
# on C3/C6/S2/S3/P4 chips, so a port with this VID *is* an ESP device.
ESPRESSIF_VID = 0x303A
# USB-UART bridge chips commonly wired to an ESP on dev boards: FTDI,
# Prolific, Silicon Labs CP210x, QinHeng CH340/CH9102.
_BRIDGE_VIDS = frozenset({0x0403, 0x067B, 0x10C4, 0x1A86})

SerialPortHint = Literal["esp", "bridge"]


class SerialPortInfo(TypedDict):
    """One ``config/serial_ports`` entry."""

    port: str
    desc: str
    vid: int | None
    pid: int | None
    hint: SerialPortHint | None


def list_serial_ports() -> list[SerialPortInfo]:
    """
    Enumerate serial ports with USB ids and a picker hint (blocking).

    ``hint`` is ``esp`` for Espressif native-USB devices, ``bridge`` for
    common USB-UART bridge chips, ``None`` when the VID is unknown.
    """
    usb_info = {p.device: p for p in comports(include_links=True) if p.device}
    result: list[SerialPortInfo] = []
    for port in get_serial_ports():
        info = usb_info.get(port.path)
        vid: int | None = info.vid if info is not None else None
        pid: int | None = info.pid if info is not None else None
        result.append(
            SerialPortInfo(
                port=port.path,
                desc=port.description if port.description != "n/a" else port.path,
                vid=vid,
                pid=pid,
                hint=_hint_for_vid(vid),
            )
        )
    return result


def _hint_for_vid(vid: int | None) -> SerialPortHint | None:
    if vid == ESPRESSIF_VID:
        return "esp"
    if vid in _BRIDGE_VIDS:
        return "bridge"
    return None
