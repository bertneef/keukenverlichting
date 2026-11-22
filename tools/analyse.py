"""Rank sniffing profiles and show repeating packets.

The right profile gives many strong captures whose content after the preamble
is identical between presses of the same button. Wrong data rates give noise
that never repeats.

    python3 -m tools.analyse captures.jsonl [--min-rssi -60]
"""
from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict

from kitchenlights.bits import TI_DEFAULT_SYNC, bits_to_hex, find_word, packet_key, preamble_end, to_bits


def load(path: str) -> list[dict]:
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("captures")
    p.add_argument("--min-rssi", type=float, default=-60, help="ignore weaker captures (remote is close)")
    p.add_argument("--key-bits", type=int, default=64, help="bits after the preamble used for grouping")
    p.add_argument("--show", type=int, default=8, help="packet groups to print for the best profile")
    args = p.parse_args()
    captures = load(args.captures)
    if not captures:
        raise SystemExit(f"{args.captures} has no captures. Sniffing received nothing: "
                         "rerun tools.scan_spectrum to get the right --freq first.")
    report(captures, args.min_rssi, args.key_bits, args.show)


def report(captures: list[dict], min_rssi: float = -60, key_bits: int = 64, show: int = 8) -> bool:
    """Print the profile ranking and the best profile's packets; True if packets repeat."""
    by_profile = defaultdict(list)
    for c in captures:
        by_profile[c["profile"]].append(c)
    if not by_profile:
        print("No captures at all: the receiver never triggered at this frequency.")
        return False

    ranking = []
    for profile, caps in by_profile.items():
        strong = [c for c in caps if c["rssi_dbm"] >= min_rssi]
        keys = Counter(k for c in strong if (k := packet_key(bytes.fromhex(c["hex"]), key_bits)))
        repeated = sum(n for n in keys.values() if n >= 2)
        d391 = sum(find_word(bytes.fromhex(c["hex"])) is not None for c in strong)
        ranking.append((repeated, len(strong), profile, len(caps), d391, keys, strong))
    ranking.sort(reverse=True)

    print(f"{'profile':<52} {'all':>5} {'strong':>6} {'repeat':>6} {'D391':>5}")
    for repeated, n_strong, profile, n_all, d391, _, _ in ranking:
        print(f"{profile:<52} {n_all:5} {n_strong:6} {repeated:6} {d391:5}")

    repeated, _, profile, _, d391, keys, strong = ranking[0]
    if repeated == 0:
        print("\nNo repeating packets yet. Check the frequency, move closer, or lower --min-rssi.")
        return False

    print(f"\nBest profile: {profile}")
    offsets = [c["freq_offset_hz"] for c in strong]
    print(f"Median frequency offset: {statistics.median(offsets) / 1e3:+.1f} kHz "
          "(add to --freq for a sharper tune)")
    if d391:
        print(f"TI default sync word 0x{TI_DEFAULT_SYNC:04X} seen in {d391}/{len(strong)} strong captures.")

    print(f"\nMost common packets (from the end of the preamble, {key_bits} key bits):")
    for key, n in keys.most_common(show):
        example = next(c for c in strong if packet_key(bytes.fromhex(c["hex"]), key_bits) == key)
        bits = to_bits(bytes.fromhex(example["hex"]))
        print(f"  x{n:<3} {bits_to_hex(bits[preamble_end(bits):])}")
    print("\nPress each button separately (see README step 3) to map packets to groups.")
    return True


if __name__ == "__main__":
    main()
