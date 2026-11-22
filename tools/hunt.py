"""Find the remote's frequency and packets in one guided run.

    python3 -m tools.hunt                  # full run, about 7 minutes
    python3 -m tools.hunt --around 2435.6  # skip the coarse scan
    python3 -m tools.hunt --freq 2435.7    # skip both scans, only sniff

Stages, each started with Enter so you can rest in between:
  1. coarse scan, quiet: remote away (~45 s)
  2. coarse scan, pressing: 0.8 MHz steps over the whole band (~2 min)
  3. fine scan, pressing: 0.1 MHz steps around the best candidates (~1 min)
  4. sniff, pressing: every data-rate profile at the best fine frequency (~1.5 min)
  5. analysis of the captures

During every pressing stage: hold the remote ~10 cm from the antenna and keep
pressing the SAME button about twice a second until the stage ends.
Everything is saved in a new folder hunt-YYYYMMDD-HHMMSS/.
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from pathlib import Path

from kitchenlights.cc2500 import CC2500, Profile
from tools.analyse import load, report
from tools.scan_spectrum import SCAN_PROFILE, check_live, dwell, rank, run_phase
from tools.sniff import GRID, capture

# Narrow filter for the fine scan: the signal's level then depends clearly on
# how far the tuning is from its carrier.
FINE_PROFILE = Profile(baud=250_000, bandwidth_hz=203_000, sync_word=0x1F35, sync_mode=3)


def candidates(ranked, step_hz: float, top: int = 5, min_share: float = 0.02,
               max_quiet: float = 0.005, min_dbm: float = -75) -> list[tuple[float, float, float]]:
    """Group neighbouring coarse hits; return up to `top` (score, lo_hz, hi_hz) ranges, best first.

    `ranked` rows are rank() output: (excess, freq, active share, quiet share, max active, max quiet).
    A hit is strong only while pressing (Wi-Fi shows up in the quiet phase too).
    """
    hits = sorted((f, a_share) for _, f, a_share, q_share, a_max, _ in ranked
                  if a_share >= min_share and q_share <= max_quiet and a_max >= min_dbm)
    clusters: list[list[tuple[float, float]]] = []
    for f, share in hits:
        if clusters and f - clusters[-1][-1][0] <= 2.5 * step_hz:  # one weak channel may sit inside
            clusters[-1].append((f, share))
        else:
            clusters.append([(f, share)])
    scored = [(max(s for _, s in c), c[0][0] - step_hz, c[-1][0] + step_hz) for c in clusters]
    return sorted(scored, reverse=True)[:top]


def level(samples: list[float], q: float = 0.99) -> float:
    """Signal level while the remote transmits.

    The 99th percentile: the remote is on air for only a small share of each
    dwell, so lower percentiles measure the noise, while the plain maximum
    would follow a single spike.
    """
    ordered = sorted(samples)
    return ordered[int(q * (len(ordered) - 1))]


def best_frequency(points: list[tuple[float, float]], window_db: float = 6) -> tuple[float, float]:
    """(centre_hz, peak_dbm): power-weighted centre of the contiguous run within window_db of the peak.

    With FSK the energy sits at both tones, so the centre of the bright run is a
    better carrier estimate than the single brightest step.
    """
    points = sorted(points)
    i_peak = max(range(len(points)), key=lambda i: points[i][1])
    peak = points[i_peak][1]
    lo = hi = i_peak
    while lo > 0 and points[lo - 1][1] >= peak - window_db:
        lo -= 1
    while hi < len(points) - 1 and points[hi + 1][1] >= peak - window_db:
        hi += 1
    run = points[lo:hi + 1]
    weights = [10 ** (dbm / 10) for _, dbm in run]
    centre = sum(f * w for (f, _), w in zip(run, weights)) / sum(weights)
    return centre, peak


class Tee:
    """Copy everything printed to a log file as well."""

    def __init__(self, path: Path):
        self.file = open(path, "w")
        self.stdout = sys.stdout

    def write(self, text: str) -> None:
        self.stdout.write(text)
        self.file.write(text)

    def flush(self) -> None:
        self.stdout.flush()
        self.file.flush()


def ask(prompt: str) -> None:
    print(prompt, end="", flush=True)
    input()


def coarse(radio, folder: Path, args) -> list[tuple[float, float, float]]:
    n = int((args.stop - args.start) / args.step) + 1
    freqs = [(args.start + i * args.step) * 1e6 for i in range(n)]
    radio.configure(SCAN_PROFILE)
    with open(folder / "scan.csv", "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["phase", "freq_mhz", "sample", "rssi_dbm"])
        ask(f"\n[1/4] Coarse scan, quiet (~{n * args.quiet_dwell / 60:.1f} min). Put the remote AWAY, "
            "don't press anything. Enter to start... ")
        quiet = run_phase(radio, freqs, args.quiet_dwell, "baseline", writer)
        check_live([s for ch in quiet for s in ch])
        ask(f"\n[2/4] Coarse scan, pressing (~{n * args.dwell / 60:.1f} min). Enter, then keep "
            "pressing the SAME button twice a second until the stage ends... ")
        active = run_phase(radio, freqs, args.dwell, "active", writer, quiet)
    found = candidates(rank(freqs, quiet, active, 10), args.step * 1e6)
    print("\nStop pressing. Candidate ranges (strong only while pressing):")
    for score, lo, hi in found:
        print(f"  {lo / 1e6:8.1f} - {hi / 1e6:8.1f} MHz, active share {100 * score:.1f}%")
    if not found:
        raise SystemExit("No channel was strong only while pressing. Check scan.csv in the hunt folder.")
    return found


def fine(radio, folder: Path, ranges, args) -> float:
    step = 0.1e6
    freqs = sorted({round(lo + i * step) for _, lo, hi in ranges
                    for i in range(int(round((hi - lo) / step)) + 1)})
    radio.configure(FINE_PROFILE)
    ask(f"\n[3/4] Fine scan, pressing (~{len(freqs) * args.fine_dwell / 60:.1f} min, "
        f"{len(freqs)} steps). Enter, then keep pressing until the stage ends... ")
    points = []
    with open(folder / "fine.csv", "w", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["phase", "freq_mhz", "sample", "rssi_dbm"])
        for i, f in enumerate(freqs):
            samples = dwell(radio, f, args.fine_dwell)
            writer.writerows(["fine", f"{f / 1e6:.3f}", k, f"{s:.1f}"] for k, s in enumerate(samples))
            points.append((f, level(samples)))
            print(f"  {f / 1e6:8.1f} MHz ({i + 1}/{len(freqs)}): level {points[-1][1]:6.1f} dBm, "
                  f"max {max(samples):6.1f} dBm")
    # Judge each candidate range on its own, then take the strongest.
    results = []
    for _, lo, hi in ranges:
        inside = [(f, v) for f, v in points if lo - 1 <= f <= hi + 1]
        results.append(best_frequency(inside))
    print("\nStop pressing. Fine results:")
    for centre, peak in sorted(results, key=lambda r: -r[1]):
        print(f"  {centre / 1e6:9.3f} MHz, level {peak:6.1f} dBm")
    centre, peak = max(results, key=lambda r: r[1])
    if peak < -70:
        print("Warning: even the best level is weak; is the remote close to the antenna?")
    return centre


def sniff(radio, folder: Path, freq_hz: float, args) -> None:
    path = folder / "captures.jsonl"
    ask(f"\n[4/4] Sniffing at {freq_hz / 1e6:.3f} MHz (~{len(GRID) * args.sniff_seconds / 60:.1f} min, "
        f"{len(GRID)} profiles). Enter, then keep pressing until 'Done'... ")
    with open(path, "a") as out:
        for i, (baud, dev) in enumerate(GRID, 1):
            profile = Profile(freq_hz=freq_hz, baud=baud, deviation_hz=dev)
            print(f"\n  [{i}/{len(GRID)}] {profile.label()}")
            capture(radio, profile, args.sniff_seconds, out, 0)
    print("\nDone, stop pressing.\n")
    report(load(str(path)))


def main(argv: list[str] | None = None, radio_factory=CC2500) -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--around", type=float, help="MHz: skip the coarse scan, fine-scan +-1.2 MHz around this")
    p.add_argument("--freq", type=float, help="MHz: skip both scans and only sniff here")
    p.add_argument("--start", type=float, default=2400.0, help="MHz")
    p.add_argument("--stop", type=float, default=2483.5, help="MHz")
    p.add_argument("--step", type=float, default=0.8, help="coarse step, MHz")
    p.add_argument("--quiet-dwell", type=float, default=0.4, help="coarse seconds per channel, quiet")
    p.add_argument("--dwell", type=float, default=1.0, help="coarse seconds per channel while pressing")
    p.add_argument("--fine-dwell", type=float, default=1.0, help="fine seconds per step")
    p.add_argument("--sniff-seconds", type=float, default=10, help="seconds per sniff profile")
    p.add_argument("--port", help="Pico serial device (default: auto-detect)")
    args = p.parse_args(argv)

    folder = Path(time.strftime("hunt-%Y%m%d-%H%M%S"))
    folder.mkdir()
    tee = sys.stdout = Tee(folder / "hunt.log")
    print(f"Saving everything in {folder}/")
    try:
        run(radio_factory, folder, args)
    finally:
        sys.stdout = tee.stdout
        tee.file.close()


def run(radio_factory, folder: Path, args) -> None:
    with radio_factory(args.port) as radio:
        radio.reset()
        part, version = radio.identify()
        print(f"CC2500 PARTNUM=0x{part:02X} VERSION=0x{version:02X} (expect 0x80 / 0x03)")
        if part != 0x80:
            raise SystemExit("No CC2500 found: check the wiring.")
        if args.freq:
            freq_hz = args.freq * 1e6
        else:
            if args.around:
                ranges = [(1.0, args.around * 1e6 - 1.2e6, args.around * 1e6 + 1.2e6)]
            else:
                ranges = coarse(radio, folder, args)
            freq_hz = fine(radio, folder, ranges, args)
        sniff(radio, folder, freq_hz, args)
    print(f"\nEverything is in {folder}/ (hunt.log, scan.csv, fine.csv, captures.jsonl).")


if __name__ == "__main__":
    main()
