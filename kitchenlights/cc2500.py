"""Minimal TI CC2500 driver (via a Pico USB bridge or a Raspberry Pi's SPI0).

Only what is needed to sniff and (later) replay the kitchen remote:
register access, strobes, a parameterised radio profile and RSSI/FIFO reads.
Datasheet: https://www.ti.com/lit/ds/symlink/cc2500.pdf
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

FXOSC = 26_000_000  # both the remote (G1 = 26.000 MHz) and common modules

# Configuration registers
IOCFG2, IOCFG1, IOCFG0 = 0x00, 0x01, 0x02
FIFOTHR, SYNC1, SYNC0, PKTLEN = 0x03, 0x04, 0x05, 0x06
PKTCTRL1, PKTCTRL0, ADDR, CHANNR = 0x07, 0x08, 0x09, 0x0A
FSCTRL1, FSCTRL0, FREQ2, FREQ1, FREQ0 = 0x0B, 0x0C, 0x0D, 0x0E, 0x0F
MDMCFG4, MDMCFG3, MDMCFG2, MDMCFG1, MDMCFG0 = 0x10, 0x11, 0x12, 0x13, 0x14
DEVIATN, MCSM2, MCSM1, MCSM0 = 0x15, 0x16, 0x17, 0x18
FOCCFG, BSCFG, AGCCTRL2, AGCCTRL1, AGCCTRL0 = 0x19, 0x1A, 0x1B, 0x1C, 0x1D
FREND1, FREND0 = 0x21, 0x22
FSCAL3, FSCAL2, FSCAL1, FSCAL0 = 0x23, 0x24, 0x25, 0x26
TEST2, TEST1, TEST0 = 0x2C, 0x2D, 0x2E

# Command strobes
SRES, SCAL, SRX, STX, SIDLE, SFRX, SFTX, SNOP = 0x30, 0x33, 0x34, 0x35, 0x36, 0x3A, 0x3B, 0x3D

# Status registers (read with the burst bit set)
PARTNUM, VERSION, FREQEST, LQI, RSSI, MARCSTATE = 0x30, 0x31, 0x32, 0x33, 0x34, 0x35
TXBYTES, RXBYTES = 0x3A, 0x3B

PATABLE, FIFO = 0x3E, 0x3F
READ, BURST = 0x80, 0x40

MARC_IDLE, MARC_RX, MARC_RXFIFO_OVERFLOW = 0x01, 0x0D, 0x11

MOD_FORMATS = {"2-FSK": 0, "GFSK": 1, "OOK": 3, "MSK": 7}

# Receiver filter bandwidths: (bandwidth_hz, CHANBW_E, CHANBW_M)
CHANNEL_BANDWIDTHS = sorted(
    (FXOSC / (8 * (4 + m) * 2**e), e, m) for e in range(4) for m in range(4)
)


def freq_regs(freq_hz: float) -> tuple[int, int, int]:
    word = round(freq_hz * 2**16 / FXOSC)
    return (word >> 16) & 0xFF, (word >> 8) & 0xFF, word & 0xFF


def drate_regs(baud: float) -> tuple[int, int]:
    """Return (DRATE_E, DRATE_M) for the closest achievable data rate."""
    e = int(math.floor(math.log2(baud * 2**20 / FXOSC)))
    m = round(baud * 2**28 / (FXOSC * 2**e) - 256)
    if m == 256:
        e, m = e + 1, 0
    return e, m


def drate_hz(e: int, m: int) -> float:
    return (256 + m) * 2**e * FXOSC / 2**28


def chanbw_regs(min_bandwidth_hz: float) -> tuple[int, int]:
    """Return (CHANBW_E, CHANBW_M) for the narrowest filter >= min_bandwidth_hz."""
    for bw, e, m in CHANNEL_BANDWIDTHS:
        if bw >= min_bandwidth_hz:
            return e, m
    _, e, m = CHANNEL_BANDWIDTHS[-1]
    return e, m


def deviatn_reg(deviation_hz: float) -> int:
    best = min(
        ((e, m) for e in range(8) for m in range(8)),
        key=lambda em: abs(FXOSC / 2**17 * (8 + em[1]) * 2 ** em[0] - deviation_hz),
    )
    return (best[0] << 4) | best[1]


def rssi_to_dbm(raw: int, offset: int = 72) -> float:
    value = raw - 256 if raw >= 128 else raw
    return value / 2 - offset


def freqest_to_hz(raw: int) -> float:
    value = raw - 256 if raw >= 128 else raw
    return value * FXOSC / 2**14


@dataclass(frozen=True)
class Profile:
    """A receive/transmit configuration of the CC2500."""

    freq_hz: float = 2_433_000_000
    baud: float = 250_000
    deviation_hz: float = 62_500
    mod: str = "2-FSK"
    bandwidth_hz: float | None = None  # None: derive from baud, deviation and crystal tolerance
    sync_word: int = 0xAAAA  # 0xAAAA triggers on the preamble itself (sniffing trick)
    sync_mode: int = 6  # 16/16 sync bits + carrier sense
    cs_abs_thr: int = 0  # carrier sense threshold relative to MAGN_TARGET, -7..7 dB
    pkt_len: int = 60  # fixed length; +2 appended status bytes must fit the 64 byte FIFO

    @property
    def effective_bandwidth_hz(self) -> float:
        if self.bandwidth_hz is not None:
            return self.bandwidth_hz
        # Signal bandwidth plus ~2 x 40 ppm crystal error at 2.4 GHz
        return self.baud + 2 * self.deviation_hz + 200_000

    def label(self) -> str:
        return (
            f"{self.freq_hz / 1e6:.3f}MHz {self.baud / 1e3:g}kBd {self.mod} "
            f"dev{self.deviation_hz / 1e3:g}k bw{self.effective_bandwidth_hz / 1e3:.0f}k"
        )

    def registers(self) -> dict[int, int]:
        high_rate = self.baud > 100_000
        bw_e, bw_m = chanbw_regs(self.effective_bandwidth_hz)
        dr_e, dr_m = drate_regs(self.baud)
        bw = FXOSC / (8 * (4 + bw_m) * 2**bw_e)
        if_freq = 0x10 if bw > 600_000 else 0x0A if bw > 300_000 else 0x06
        agcctrl1 = (0x00 if high_rate else 0x40) | (self.cs_abs_thr & 0x0F)
        f2, f1, f0 = freq_regs(self.freq_hz)
        return {
            IOCFG2: 0x29,  # CHIP_RDYn
            IOCFG0: 0x06,  # asserts on sync word, deasserts at end of packet
            FIFOTHR: 0x07,
            SYNC1: (self.sync_word >> 8) & 0xFF,
            SYNC0: self.sync_word & 0xFF,
            PKTLEN: self.pkt_len,
            PKTCTRL1: 0x04,  # append RSSI + LQI, no address check
            PKTCTRL0: 0x00,  # fixed length, no CRC, no whitening
            ADDR: 0x00,
            CHANNR: 0x00,
            FSCTRL1: if_freq,
            FSCTRL0: 0x00,
            FREQ2: f2,
            FREQ1: f1,
            FREQ0: f0,
            MDMCFG4: (bw_e << 6) | (bw_m << 4) | dr_e,
            MDMCFG3: dr_m,
            MDMCFG2: (MOD_FORMATS[self.mod] << 4) | (self.sync_mode & 0x07),
            MDMCFG1: 0x22,  # no FEC, 4 preamble bytes on TX, CHANSPC_E = 2
            MDMCFG0: 0xF8,
            DEVIATN: 0x00 if self.mod == "MSK" else deviatn_reg(self.deviation_hz),
            MCSM2: 0x07,
            MCSM1: 0x30,  # back to IDLE after RX and TX
            MCSM0: 0x18,  # calibrate when going IDLE -> RX/TX
            FOCCFG: 0x1D if high_rate else 0x16,
            BSCFG: 0x1C if high_rate else 0x6C,
            AGCCTRL2: 0xC7 if high_rate else 0x03,
            AGCCTRL1: agcctrl1,
            AGCCTRL0: 0xB0 if high_rate else 0x91,
            FREND1: 0xB6 if high_rate else 0x56,
            FREND0: 0x10,
            FSCAL3: 0xEA,
            FSCAL2: 0x0A,
            FSCAL1: 0x00,
            FSCAL0: 0x11,
            TEST2: 0x88 if high_rate else 0x81,
            TEST1: 0x31 if high_rate else 0x35,
            TEST0: 0x0B,
        }


class CC2500:
    def __init__(self, port: str | None = None, transport=None):
        """`transport` is anything with xfer2()/close(); see kitchenlights.transport."""
        if transport is None:
            from kitchenlights.transport import open_transport

            transport = open_transport(port)
        self.spi = transport

    def close(self) -> None:
        self.idle()
        self.spi.close()

    def __enter__(self) -> "CC2500":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- low level -------------------------------------------------------
    def strobe(self, cmd: int) -> int:
        return self.spi.xfer2([cmd])[0]

    def write_reg(self, addr: int, value: int) -> None:
        self.spi.xfer2([addr, value])

    def write_burst(self, addr: int, values: list[int]) -> None:
        self.spi.xfer2([addr | BURST, *values])

    def read_reg(self, addr: int) -> int:
        return self.spi.xfer2([addr | READ, 0])[1]

    def read_status(self, addr: int) -> int:
        return self.spi.xfer2([addr | READ | BURST, 0])[1]

    def read_status_stable(self, addr: int) -> int:
        # Errata: RXBYTES/TXBYTES/MARCSTATE can be wrong if read while updating
        previous = self.read_status(addr)
        while True:
            current = self.read_status(addr)
            if current == previous:
                return current
            previous = current

    def read_fifo(self, n: int) -> list[int]:
        return self.spi.xfer2([FIFO | READ | BURST] + [0] * n)[1:]

    # --- high level ------------------------------------------------------
    def reset(self) -> None:
        self.strobe(SRES)
        time.sleep(0.002)

    def identify(self) -> tuple[int, int]:
        """(PARTNUM, VERSION); a CC2500 answers (0x80, 0x03)."""
        return self.read_status(PARTNUM), self.read_status(VERSION)

    def configure(self, profile: Profile) -> None:
        self.idle()
        for addr, value in profile.registers().items():
            self.write_reg(addr, value)
        self.write_burst(PATABLE, [0xFF])  # +1 dBm, the CC2500 maximum

    def set_frequency(self, freq_hz: float) -> None:
        self.write_burst(FREQ2, list(freq_regs(freq_hz)))

    def idle(self) -> None:
        self.strobe(SIDLE)
        while self.read_status_stable(MARCSTATE) & 0x1F != MARC_IDLE:
            pass

    def rx(self) -> None:
        self.strobe(SRX)

    def flush_rx(self) -> None:
        self.idle()
        self.strobe(SFRX)

    def marcstate(self) -> int:
        return self.read_status_stable(MARCSTATE) & 0x1F

    def rx_bytes(self) -> int:
        return self.read_status_stable(RXBYTES)

    def rssi_dbm(self) -> float:
        return rssi_to_dbm(self.read_status(RSSI))

    def freq_offset_hz(self) -> float:
        return freqest_to_hz(self.read_status(FREQEST))
