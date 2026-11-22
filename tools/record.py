"""Record what each remote button sends, into buttons.json.

    python3 -m tools.record              # asks for button names one by one
    python3 -m tools.record --out buttons.json --seconds 8

For every button: type a short name (e.g. g1, g2, all), press Enter, then
press that button about once per second until the time is up. A toggle button
sends two different packets on alternate presses; they are stored as variants
1 and 2 in the order they were seen. tools.replay then sends one of them.
Each variant is cut where repeated presses stop agreeing (some packets are 24
bytes, the rest of the 36 received is noise), and variants seen in only a few
presses (corrupted packets) are dropped.

    python3 -m tools.record --clean     # apply that to an existing buttons.json
    python3 -m tools.record --log packets.jsonl   # also keep every raw packet

--log is for controls that send many different packets, like the colour
dial: every packet is appended with its time and the name you typed, so the
sequence can be analysed later. buttons.json alone would drop rare packets.

The receiver now listens for the remote's own sync word, so every packet
starts at the same bit and identical presses give identical bytes.
"""
from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from pathlib import Path

from kitchenlights import remote
from kitchenlights.cc2500 import CC2500, MARC_RXFIFO_OVERFLOW, rssi_to_dbm

PRESS_GAP_S = 0.12  # packets further apart than this belong to different presses


MAX_LENGTH = 62  # the 64-byte RX FIFO also holds the 2 appended status bytes


def listen(radio: CC2500, seconds: float, length: int = remote.PACKET_LEN) -> list[tuple[float, bytes, float]]:
    """(time, payload, rssi_dbm) for every packet received within `seconds`.

    `length` bytes are read after the sync word; anything beyond the real
    packet is noise, which summarise() trims off again.
    """
    radio.configure(remote.profile(pkt_len=length))
    radio.flush_rx()
    radio.rx()
    want = length + 2  # payload + appended RSSI, LQI
    packets = []
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
        raw = radio.read_fifo(want)
        radio.flush_rx()
        radio.rx()
        packets.append((time.monotonic(), bytes(raw[:-2]), rssi_to_dbm(raw[-2])))
        print(f"\r  packets: {len(packets)}  last RSSI {packets[-1][2]:6.1f} dBm ", end="", flush=True)
    print()
    return packets


def presses(packets: list[tuple[float, bytes, float]]) -> list[bytes]:
    """The most common payload of each press (a burst of packets close together)."""
    groups: list[list[bytes]] = []
    last = None
    for t, payload, _ in packets:
        if last is None or t - last > PRESS_GAP_S:
            groups.append([])
        groups[-1].append(payload)
        last = t
    return [Counter(g).most_common(1)[0][0] for g in groups]


KEY_BYTES = 16  # presses whose first 16 bytes match belong to the same variant
MIN_SHARE = 0.15  # variants seen in fewer presses than this share are corrupted packets


def common_prefix(members: list[bytes]) -> bytes:
    """The longest prefix that at least 2 and at least half of `members` share.

    Packets shorter than what we receive (24 instead of 36 bytes) are followed
    by noise that differs on every press; this cuts that noise off.
    """
    best = b""
    for length in range(1, max(map(len, members)) + 1):
        prefix, n = Counter(m[:length] for m in members).most_common(1)[0]
        if n >= 2 and n >= len(members) / 2:
            best = prefix
    return best


def summarise(per_press: list[bytes]) -> dict:
    """Variants in order of first appearance, and which variant each press sent (0: corrupted)."""
    groups: dict[bytes, list[bytes]] = {}
    for payload in per_press:
        groups.setdefault(payload[:KEY_BYTES], []).append(payload)
    needed = max(2, MIN_SHARE * len(per_press))
    keys = [k for k, members in groups.items() if len(members) >= needed]
    variants = [common_prefix(groups[k]) for k in keys]
    return {
        "variants": [v.hex() for v in variants],
        "presses": [keys.index(p[:KEY_BYTES]) + 1 if p[:KEY_BYTES] in keys else 0 for p in per_press],
    }


def clean(path: Path) -> None:
    """Re-summarise an existing buttons.json with the current rules."""
    buttons = json.loads(path.read_text())
    for name, entry in buttons.items():
        per_press = [bytes.fromhex(entry["variants"][i - 1]) for i in entry["presses"] if i]
        cleaned = summarise(per_press)
        entry.update(cleaned)
        lengths = ", ".join(str(len(v) // 2) for v in cleaned["variants"])
        print(f"{name}: {len(cleaned['variants'])} variants ({lengths} bytes)")
    path.write_text(json.dumps(buttons, indent=2) + "\n")
    print(f"Cleaned {path}.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="buttons.json")
    p.add_argument("--seconds", type=float, default=8, help="recording time per button")
    p.add_argument("--port", help="Pico serial device (default: auto-detect)")
    p.add_argument("--log", help="also append every received packet to this JSON-lines file")
    p.add_argument("--length", type=int, default=remote.PACKET_LEN,
                   help=f"bytes to read after the sync word (max {MAX_LENGTH}); more shows "
                        "whether a packet is longer than the usual 36")
    p.add_argument("--clean", action="store_true",
                   help="only re-process the existing --out file (trim noise, drop corrupted variants)")
    args = p.parse_args()

    if not 1 <= args.length <= MAX_LENGTH:
        p.error(f"--length must be 1..{MAX_LENGTH}")
    path = Path(args.out)
    if args.clean:
        clean(path)
        return
    buttons = json.loads(path.read_text()) if path.exists() else {}
    with CC2500(args.port) as radio:
        radio.reset()
        if radio.identify()[0] != 0x80:
            raise SystemExit("No CC2500 found: check the wiring.")
        while True:
            name = input("\nButton name (e.g. g1, g2, all); empty to finish: ").strip()
            if not name:
                break
            input(f"Enter, then press '{name}' about once per second for {args.seconds:g} s... ")
            packets = listen(radio, args.seconds, args.length)
            if args.log:
                with open(args.log, "a") as log:
                    for t, payload, rssi in packets:
                        log.write(json.dumps({"button": name, "t": round(t, 4), "rssi_dbm": rssi,
                                              "hex": payload.hex()}) + "\n")
            if not packets:
                print("  Nothing received. Remote ~10-30 cm from the antenna? Try again.")
                continue
            per_press = presses(packets)
            entry = summarise(per_press)
            entry["packets_per_press"] = round(len(packets) / len(per_press), 1)
            buttons[name] = entry
            print(f"  {len(per_press)} presses, variant per press: {entry['presses']}")
            for i, v in enumerate(entry["variants"], 1):
                print(f"  variant {i}: {bytes.fromhex(v).hex(' ')}")
            path.write_text(json.dumps(buttons, indent=2) + "\n")
    print(f"\nSaved {len(buttons)} buttons in {path}. Keep a copy: without the remote they can't be recorded again.")


if __name__ == "__main__":
    main()
