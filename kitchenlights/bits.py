"""Bit-level helpers for making sense of raw captures."""
from __future__ import annotations

TI_DEFAULT_SYNC = 0xD391


def to_bits(data: bytes) -> str:
    return "".join(f"{b:08b}" for b in data)


def bits_to_hex(bits: str) -> str:
    bits = bits[: len(bits) - len(bits) % 8]
    return bytes(int(bits[i : i + 8], 2) for i in range(0, len(bits), 8)).hex(" ")


def preamble_end(bits: str) -> int:
    """Index of the first bit that breaks the leading 1010... pattern.

    The capture is triggered on 0xAAAA, so it starts inside the preamble at an
    arbitrary even bit offset. The first repeated bit marks where the real sync
    word starts (give or take one bit, which is the same for every capture of
    the same packet, so it does not hurt clustering).
    """
    for i in range(len(bits) - 1):
        if bits[i] == bits[i + 1]:
            return i
    return len(bits)


def packet_key(data: bytes, n_bits: int = 64) -> str | None:
    """Content after the preamble, used to group identical packets together."""
    bits = to_bits(data)
    start = preamble_end(bits)
    if len(bits) - start < n_bits:
        return None
    return bits[start : start + n_bits]


def find_word(data: bytes, word: int = TI_DEFAULT_SYNC, width: int = 16) -> int | None:
    """Bit offset of `word` in the capture, or None."""
    pos = to_bits(data).find(f"{word:0{width}b}")
    return None if pos < 0 else pos
