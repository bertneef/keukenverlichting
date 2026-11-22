"""What we know about the L&S remote's radio, from the hunt on 2026-11-21.

- 2436.25 MHz, 2-FSK, 250 kBd. Both 62.5 and 127 kHz deviation decode it.
- Preamble 0xAA..., then the sync word 0x2C6E sent twice. (That is the bitwise
  inverse of TI's default 0xD391: our receiver sees the FSK polarity inverted.
  Replaying what we receive keeps that consistent, so it does not matter.)
- 36 bytes after the sync word: 16 data bytes, 8-9 x 0xFF, then 11-12 bytes that
  differ between on and off (probably a checksum). Anything after byte 36 is
  noise. No TI whitening and no standard CC2500 CRC over the first 16 bytes.
  (Until 2026-11-22 only 24 bytes were recorded and replayed: the lights
  ignored those truncated packets.)
- One press sends a train of identical packets for roughly 140 ms; a toggle
  button alternates between two packets (on / off) on successive presses.
"""
from __future__ import annotations

import time

from kitchenlights.cc2500 import FIFO, MARC_IDLE, SFTX, STX, TXBYTES, Profile

FREQ_HZ = 2_436_250_000
SYNC_WORD = 0x2C6E
PACKET_LEN = 36  # bytes received after the sync word: the longest packet the remote sends
PACKET_LENS = (24, 36)  # group / up / down buttons: 36 bytes; power, h1, h2: 24 bytes


def profile(freq_hz: float = FREQ_HZ, deviation_hz: float = 127_000, pkt_len: int = PACKET_LEN) -> Profile:
    """Receive / transmit settings matching the remote. sync_mode 3 = sync word sent and matched twice."""
    return Profile(freq_hz=freq_hz, baud=250_000, deviation_hz=deviation_hz,
                   sync_word=SYNC_WORD, sync_mode=3, pkt_len=pkt_len)


def send(radio, profile: Profile, payload: bytes, seconds: float) -> int:
    """Transmit `payload` back to back for `seconds`; return the number of packets sent."""
    radio.configure(profile)
    sent = 0
    end = time.monotonic() + seconds
    while time.monotonic() < end or sent == 0:
        radio.idle()
        radio.strobe(SFTX)
        radio.write_burst(FIFO, list(payload))
        radio.strobe(STX)  # preamble + sync twice + payload, then back to IDLE (MCSM1)
        deadline = time.monotonic() + 0.05
        while radio.marcstate() != MARC_IDLE:
            if time.monotonic() > deadline:
                raise RuntimeError("The CC2500 did not finish transmitting: check the wiring.")
        left = radio.read_status(TXBYTES)
        if left:  # bytes still in the TX FIFO (or the underflow flag): nothing went out properly
            raise RuntimeError(f"TX FIFO not empty after sending (TXBYTES=0x{left:02X}).")
        sent += 1
    return sent
