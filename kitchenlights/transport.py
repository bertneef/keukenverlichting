"""Ways to reach the CC2500's SPI bus: a Pico USB bridge or local spidev.

Both expose `xfer2(list[int]) -> list[int]` (one chip-select-framed transfer)
and `close()`, which is all the CC2500 driver needs.
"""
from __future__ import annotations

import glob

PICO_GLOB = "/dev/serial/by-id/usb-MicroPython*"
PING = b"\x00"
PONG = b"K"
STOP = b"\xff"


class PicoTransport:
    """Raspberry Pi Pico running pico/main.py, connected over USB.

    Frame: host sends [length][bytes], Pico answers with the same number of
    bytes clocked back from the CC2500. Length 0 is a ping answered with 'K';
    length 255 stops the bridge so mpremote can reach the REPL.
    """

    def __init__(self, port: str, timeout: float = 1.0):
        import serial  # pyserial

        self.ser = serial.Serial(port, 115200, timeout=timeout)
        self.ser.reset_input_buffer()
        self.ser.write(PING)
        if self.ser.read(1) != PONG:
            raise RuntimeError(f"No KitchenLights bridge answering on {port}; is pico/main.py installed?")

    def xfer2(self, data: list[int]) -> list[int]:
        if not 1 <= len(data) <= 254:
            raise ValueError("transfer must be 1..254 bytes")
        self.ser.write(bytes([len(data), *data]))
        reply = self.ser.read(len(data))
        if len(reply) != len(data):
            raise TimeoutError("bridge did not answer; unplug and replug the Pico")
        return list(reply)

    def stop_bridge(self) -> None:
        self.ser.write(STOP)

    def close(self) -> None:
        self.ser.close()


class SpidevTransport:
    """CC2500 wired straight to a Raspberry Pi's SPI0 pins."""

    def __init__(self, bus: int = 0, device: int = 0, speed_hz: int = 1_000_000):
        import spidev

        self.spi = spidev.SpiDev()
        self.spi.open(bus, device)
        self.spi.max_speed_hz = speed_hz
        self.spi.mode = 0

    def xfer2(self, data: list[int]) -> list[int]:
        return self.spi.xfer2(data)

    def close(self) -> None:
        self.spi.close()


def open_transport(port: str | None = None):
    """`port` names a serial device; None auto-detects a Pico, then falls back to spidev."""
    if port is None:
        found = sorted(glob.glob(PICO_GLOB))
        if found:
            port = found[0]
    if port:
        return PicoTransport(port)
    try:
        return SpidevTransport()
    except (ImportError, OSError) as exc:
        raise SystemExit(f"No Pico bridge found ({PICO_GLOB}) and no local SPI ({exc}). "
                         "Plug in the Pico or pass --port.") from exc
