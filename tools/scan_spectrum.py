"""Step 1: find the remote's frequency.

Steps through 2400-2483.5 MHz and stays on each channel for a while, sampling
the signal strength as fast as possible. A button press is a short burst, so
a fast sweep almost never lands on the right channel at the right moment;
dwelling on each channel while you keep pressing does.

First a quiet baseline (Wi-Fi, neighbours, ...), then the same steps while
you keep pressing one button close to the antenna. Channels where strong
samples appear only in the second phase are the remote.

    python3 -m tools.scan_spectrum --out scan.csv
"""
from __future__ import annotations

import argparse
import csv
import time

from kitchenlights.cc2500 import CC2500, MARC_RX, RSSI, Profile, rssi_to_dbm

# Receive settings for the scan. The radio must keep listening: once it
# "receives" a packet it leaves RX and RSSI freezes. So the sync word is one
# the remote is unlikely to send, and 32 bits long (sync_mode 3, 30/32 bits):
# random noise matches a 16-bit word several times a second, which froze most
# of each fine-scan dwell. dwell() also re-enters RX if the radio left anyway.
SCAN_PROFILE = Profile(baud=250_000, bandwidth_hz=812_000, sync_word=0x1F35, sync_mode=3)
CHECK_EVERY = 32  # RSSI samples between checks that the radio is still in RX


def tune(radio: CC2500, freq_hz: float) -> None:
    radio.flush_rx()  # IDLE + empty the FIFO; an overflowed FIFO freezes RSSI
    radio.set_frequency(freq_hz)
    radio.rx()
    time.sleep(0.001)  # synthesiser calibration + RSSI settling


def dwell(radio: CC2500, freq_hz: float, seconds: float) -> list[float]:
    """RSSI samples (dBm) on one channel, as many as fit in `seconds`."""
    tune(radio, freq_hz)
    samples = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        samples.append(rssi_to_dbm(radio.read_status(RSSI)))
        if len(samples) % CHECK_EVERY == 0 and radio.marcstate() != MARC_RX:
            tune(radio, freq_hz)  # left RX (false sync): RSSI would be frozen from here on
    return samples


def threshold(baseline: list[float], margin: float) -> float:
    """Level that counts as a hit: the baseline's 99th percentile plus a margin."""
    ordered = sorted(baseline)
    return ordered[int(0.99 * (len(ordered) - 1))] + margin


def check_live(samples: list[float]) -> None:
    """Stop early if RSSI never changes: the radio is not really receiving."""
    distinct = set(samples)
    if len(distinct) < 5:
        raise SystemExit(
            f"RSSI is stuck ({len(distinct)} distinct values: {sorted(distinct)} dBm). "
            "The radio is not receiving. Check the antenna and the GND/3V3 wires, "
            "then send this output.")


def rank(freqs, quiet, active, margin):
    """Rows (excess share, freq, active share, baseline share, max active, max baseline), best first."""
    rows = []
    for f, q, a in zip(freqs, quiet, active):
        thr = threshold(q, margin)
        a_share = sum(x > thr for x in a) / len(a)
        q_share = sum(x > thr for x in q) / len(q)
        rows.append((a_share - q_share, f, a_share, q_share, max(a), max(q)))
    return sorted(rows, reverse=True)


def run_phase(radio, freqs, seconds, name, writer, quiet=None, margin=10.0) -> list[list[float]]:
    per_channel = []
    for i, f in enumerate(freqs):
        samples = dwell(radio, f, seconds)
        per_channel.append(samples)
        writer.writerows([name, f"{f / 1e6:.3f}", k, f"{s:.1f}"] for k, s in enumerate(samples))
        line = f"\r  {name}: {f / 1e6:8.3f} MHz ({i + 1}/{len(freqs)}), max {max(samples):6.1f} dBm "
        if quiet is not None:
            thr = threshold(quiet[i], margin)
            hits = sum(s > thr for s in samples)
            if hits:  # live feedback: this channel lit up while you were pressing
                line += f"<- {hits} strong samples\n"
        print(line, end="", flush=True)
    print()
    return per_channel


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=float, default=2400.0, help="MHz")
    p.add_argument("--stop", type=float, default=2483.5, help="MHz")
    p.add_argument("--step", type=float, default=0.8, help="MHz (the filter is 812 kHz wide)")
    p.add_argument("--baseline", type=float, default=0.4, help="seconds per channel without pressing")
    p.add_argument("--active", type=float, default=1.0, help="seconds per channel while pressing")
    p.add_argument("--margin", type=float, default=10, help="dB above the baseline that counts as a hit")
    p.add_argument("--port", help="Pico serial device (default: auto-detect)")
    p.add_argument("--out", default="scan.csv", help="raw data, one row per RSSI sample")
    args = p.parse_args()

    n = int((args.stop - args.start) / args.step) + 1
    freqs = [(args.start + i * args.step) * 1e6 for i in range(n)]

    with CC2500(args.port) as radio, open(args.out, "w", newline="") as fh:
        radio.reset()
        part, version = radio.identify()
        print(f"CC2500 PARTNUM=0x{part:02X} VERSION=0x{version:02X} (expect 0x80 / 0x03)")
        if part != 0x80:
            raise SystemExit("No CC2500 found: check the wiring.")
        radio.configure(SCAN_PROFILE)
        writer = csv.writer(fh)
        writer.writerow(["phase", "freq_mhz", "sample", "rssi_dbm"])

        print(f"\n{len(freqs)} channels: about {len(freqs) * args.baseline / 60:.1f} min quiet, "
              f"then {len(freqs) * args.active / 60:.1f} min of pressing.")
        input("Phase 1: put the remote AWAY and don't press anything. Enter to start... ")
        quiet = run_phase(radio, freqs, args.baseline, "baseline", writer)
        check_live([s for ch in quiet for s in ch])
        input("\nPhase 2: hold the remote ~10 cm from the antenna. Keep pressing the SAME "
              "button about twice a second until the end. Enter to start, then start pressing... ")
        active = run_phase(radio, freqs, args.active, "active", writer, quiet, args.margin)

    print(f"\nTop channels (hit = more than {args.margin:g} dB above the 99th percentile baseline):")
    print(f"{'MHz':>9} {'active':>7} {'quiet':>7} {'max act':>8} {'max quiet':>9}")
    for excess, f, a_share, q_share, a_max, q_max in rank(freqs, quiet, active, args.margin)[:12]:
        print(f"{f / 1e6:9.3f} {100 * a_share:6.1f}% {100 * q_share:6.1f}% {a_max:8.1f} {q_max:9.1f}")
    print("\nA real hit: a clear 'active' share, ~0% 'quiet', and 'max act' around -50 dBm or "
          "stronger.\nNeighbouring channels lighting up together is normal; take the strongest.")
    print(f"Raw data written to {args.out}")


if __name__ == "__main__":
    main()
