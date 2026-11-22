"""Step 2: find the remote's data rate, sync word and packets.

Tries a grid of data rates / deviations at the frequency found in step 1. The
receiver triggers on the preamble (sync word 0xAAAA), so it captures whatever
the remote sends after it, including the real sync word. Keep pressing the same
button for the whole run (the profiles switch by themselves); tools.analyse
then shows which profile produced consistent, repeating packets.

    python3 -m tools.sniff --freq 2433.0 --out captures.jsonl
"""
from __future__ import annotations

import argparse
import json
import time

from kitchenlights.cc2500 import CC2500, MARC_RXFIFO_OVERFLOW, Profile, rssi_to_dbm

# (baud, deviation): TI SmartRF presets plus MSK-like (deviation = baud / 4) variants
GRID = [
    (2_400, 38_000),
    (10_000, 38_000),
    (38_400, 20_000),
    (100_000, 25_000),
    (100_000, 50_000),
    (250_000, 62_500),
    (250_000, 127_000),
    (500_000, 125_000),
]


def capture(radio: CC2500, profile: Profile, seconds: float, out, cs_abs_thr: int) -> int:
    radio.configure(profile)
    radio.flush_rx()
    radio.rx()
    count = 0
    want = profile.pkt_len + 2  # payload + appended RSSI, LQI
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        n = radio.rx_bytes()
        if n & 0x80 or radio.marcstate() == MARC_RXFIFO_OVERFLOW:
            radio.flush_rx()
            radio.rx()
            continue
        if n < want:
            time.sleep(0.0005)
            continue
        freq_offset = radio.freq_offset_hz()
        raw = radio.read_fifo(want)
        radio.flush_rx()
        radio.rx()
        data, rssi_raw, lqi = bytes(raw[:-2]), raw[-2], raw[-1]
        count += 1
        out.write(json.dumps({
            "profile": profile.label(),
            "baud": profile.baud,
            "deviation_hz": profile.deviation_hz,
            "freq_hz": profile.freq_hz,
            "cs_abs_thr": cs_abs_thr,
            "t": round(time.time(), 4),
            "rssi_dbm": rssi_to_dbm(rssi_raw),
            "lqi": lqi & 0x7F,
            "freq_offset_hz": round(freq_offset),
            "hex": data.hex(),
        }) + "\n")
        print(f"\r  captures: {count}  last RSSI {rssi_to_dbm(rssi_raw):6.1f} dBm ", end="", flush=True)
    print()
    return count


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--freq", type=float, required=True, help="MHz, from tools.scan_spectrum")
    p.add_argument("--seconds", type=float, default=15, help="capture time per profile")
    p.add_argument("--cs-thr", type=int, default=0, choices=range(-7, 8), metavar="-7..7",
                   help="carrier sense threshold in dB; raise it if Wi-Fi floods the captures")
    p.add_argument("--only", type=int, nargs="*", help="grid indexes to run (default: all)")
    p.add_argument("--port", help="Pico serial device (default: auto-detect)")
    p.add_argument("--out", default="captures.jsonl")
    args = p.parse_args()

    grid = [g for i, g in enumerate(GRID) if args.only is None or i in args.only]
    with CC2500(args.port) as radio, open(args.out, "a") as out:
        radio.reset()
        if radio.identify()[0] != 0x80:
            raise SystemExit("No CC2500 found: check the wiring.")
        input(f"\n{len(grid)} profiles x {args.seconds:g} s = {len(grid) * args.seconds / 60:.1f} min. "
              "Hold the remote ~10 cm from the antenna and keep pressing the SAME button\n"
              "about twice a second until 'Done'. Enter to start, then start pressing... ")
        for i, (baud, dev) in enumerate(grid, 1):
            profile = Profile(freq_hz=args.freq * 1e6, baud=baud, deviation_hz=dev, cs_abs_thr=args.cs_thr)
            print(f"\n[{i}/{len(grid)}] {profile.label()}  (keep pressing)")
            capture(radio, profile, args.seconds, out, args.cs_thr)
    print(f"\nDone. Now run: python3 -m tools.analyse {args.out}")


if __name__ == "__main__":
    main()
