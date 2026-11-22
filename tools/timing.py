"""Measure when the remote transmits, sample by sample, on its frequency.

    python3 -m tools.timing --out timing.csv

For each control, type a name (e.g. dial-cw), press Enter, and use the
control for the given time. The signal strength is sampled as fast as the
USB bridge allows (about every 0.7 ms) without decoding packets, so packet
starts, lengths and gaps show even when packets are identical. That is for
the colour dial: its packets are the same in both directions, so if the
direction is in the timing, this is where it shows.

Writes one row per sample (name, time in ms, RSSI) and prints a summary of
bursts (packets) and the gaps between them.
"""
from __future__ import annotations

import argparse
import csv
import statistics
import time

from kitchenlights import remote
from kitchenlights.cc2500 import CC2500, MARC_RX, RSSI, Profile, rssi_to_dbm
from tools.scan_spectrum import CHECK_EVERY, tune

# Listen on the remote's frequency but never "receive" a packet, so RSSI stays live.
TIMING_PROFILE = Profile(freq_hz=remote.FREQ_HZ, baud=250_000, bandwidth_hz=812_000,
                         sync_word=0x1F35, sync_mode=3)


def sample(radio, seconds: float) -> list[tuple[float, float]]:
    """(time_ms, rssi_dbm) as fast as possible for `seconds`."""
    tune(radio, remote.FREQ_HZ)
    out = []
    start = time.perf_counter()
    end = start + seconds
    while (now := time.perf_counter()) < end:
        out.append(((now - start) * 1e3, rssi_to_dbm(radio.read_status(RSSI))))
        if len(out) % CHECK_EVERY == 0 and radio.marcstate() != MARC_RX:
            tune(radio, remote.FREQ_HZ)
    return out


def bursts(samples: list[tuple[float, float]], margin_db: float = 20) -> list[tuple[float, float]]:
    """(start_ms, duration_ms) of runs of samples more than margin_db above the median level."""
    if not samples:
        return []
    threshold = statistics.median(level for _, level in samples) + margin_db
    found = []
    start = last = None
    for t, level in samples:
        if level > threshold:
            if start is None:
                start = t
            last = t
        elif start is not None:
            found.append((start, last - start))
            start = None
    if start is not None:
        found.append((start, last - start))
    return found


def summarise(name: str, samples: list[tuple[float, float]]) -> None:
    found = bursts(samples)
    period = (samples[-1][0] - samples[0][0]) / max(len(samples) - 1, 1)
    print(f"  {name}: {len(samples)} samples (every {period:.2f} ms), {len(found)} bursts")
    if len(found) < 2:
        return
    durations = [d for _, d in found]
    gaps = [b[0] - a[0] for a, b in zip(found, found[1:])]
    print(f"    burst length ms: median {statistics.median(durations):.1f}, max {max(durations):.1f}")
    print(f"    start-to-start ms: median {statistics.median(gaps):.1f}, "
          f"min {min(gaps):.1f}, max {max(gaps):.1f}")
    print("    first gaps ms:", " ".join(f"{g:.1f}" for g in gaps[:20]))


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="timing.csv")
    p.add_argument("--seconds", type=float, default=6, help="sampling time per control")
    p.add_argument("--port", help="Pico serial device (default: auto-detect)")
    args = p.parse_args()

    with CC2500(args.port) as radio, open(args.out, "a", newline="") as fh:
        radio.reset()
        if radio.identify()[0] != 0x80:
            raise SystemExit("No CC2500 found: check the wiring.")
        radio.configure(TIMING_PROFILE)
        writer = csv.writer(fh)
        if fh.tell() == 0:
            writer.writerow(["name", "t_ms", "rssi_dbm"])
        while True:
            name = input("\nControl name (e.g. dial-cw, dial-ccw, g1); empty to finish: ").strip()
            if not name:
                break
            input(f"Enter, then use '{name}' for {args.seconds:g} s... ")
            samples = sample(radio, args.seconds)
            writer.writerows([name, f"{t:.3f}", f"{level:.1f}"] for t, level in samples)
            fh.flush()
            summarise(name, samples)
    print(f"\nSaved in {args.out}.")


if __name__ == "__main__":
    main()
