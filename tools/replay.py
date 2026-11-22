"""Send a recorded button press, to test whether the lights react.

    python3 -m tools.replay g1 1        # button g1, variant 1
    python3 -m tools.replay g1 2        # its other variant (the opposite on/off)
    python3 -m tools.replay --hex 1b1b...  # a raw packet (36 bytes after the sync word)
    python3 -m tools.replay --dial 20     # colour dial position 20 (of colours.json)

Repeats the packet for --seconds (default 0.15 s, about as long as a real
press). If the lights don't react, try --dev 62500 and then --freq 2436.3.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from kitchenlights import remote
from kitchenlights.cc2500 import CC2500
from kitchenlights.remote import send  # noqa: F401  (tests import it from here)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("button", nargs="?", help="name used in tools.record")
    p.add_argument("variant", type=int, nargs="?", default=1, help="1 or 2 for a toggle button")
    p.add_argument("--file", default="buttons.json")
    p.add_argument("--hex", help="send this packet instead of a recorded button")
    p.add_argument("--dial", type=int, help="send this colour dial position (1..N, from --colours)")
    p.add_argument("--colours", default="colours.json", help="dial positions recorded in clockwise order")
    p.add_argument("--seconds", type=float, default=0.15, help="how long to keep repeating the packet")
    p.add_argument("--dev", type=float, default=127_000, help="FSK deviation in Hz")
    p.add_argument("--freq", type=float, default=remote.FREQ_HZ / 1e6, help="MHz")
    p.add_argument("--port", help="Pico serial device (default: auto-detect)")
    args = p.parse_args()

    if args.dial is not None:
        positions = json.loads(Path(args.colours).read_text())["positions"]
        if not 1 <= args.dial <= len(positions):
            raise SystemExit(f"--dial must be 1..{len(positions)}")
        payload, what = bytes.fromhex(positions[args.dial - 1]), f"dial position {args.dial}/{len(positions)}"
    elif args.hex:
        payload, what = bytes.fromhex(args.hex), "packet"
    else:
        if not args.button:
            p.error("give a button name or --hex")
        buttons = json.loads(Path(args.file).read_text())
        if args.button not in buttons:
            raise SystemExit(f"No button '{args.button}' in {args.file}; known: {', '.join(buttons)}")
        variants = buttons[args.button]["variants"]
        if not 1 <= args.variant <= len(variants):
            raise SystemExit(f"'{args.button}' has variants 1..{len(variants)}")
        payload, what = bytes.fromhex(variants[args.variant - 1]), f"'{args.button}' variant {args.variant}"
    if len(payload) < min(remote.PACKET_LENS):
        raise SystemExit(f"Packet is {len(payload)} bytes; the remote sends {' or '.join(map(str, remote.PACKET_LENS))}. "
                         "Run tools.record --clean, or record again.")
    if len(payload) not in remote.PACKET_LENS:
        print(f"Note: {len(payload)}-byte packet; the known commands are "
              f"{' or '.join(map(str, remote.PACKET_LENS))} bytes.")
    if len(payload) > 62:
        raise SystemExit("At most 62 bytes fit in the CC2500's FIFO in one go.")
    profile = remote.profile(freq_hz=args.freq * 1e6, deviation_hz=args.dev, pkt_len=len(payload))

    with CC2500(args.port) as radio:
        radio.reset()
        if radio.identify()[0] != 0x80:
            raise SystemExit("No CC2500 found: check the wiring.")
        start = time.monotonic()
        try:
            n = send(radio, profile, payload, args.seconds)
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
        took = time.monotonic() - start
    print(f"Sent {what}: {n} packets in {took:.2f} s "
          f"({args.freq:.3f} MHz, deviation {args.dev / 1e3:g} kHz). Did the lights react?")


if __name__ == "__main__":
    main()
