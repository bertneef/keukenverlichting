"""Build colours.json (the colour dial's positions) from a dial recording.

    python3 -m tools.record --length 60 --out dial.json --log dial.jsonl
        (name 'dial-cw': turn the dial slowly clockwise over its whole range;
         name 'dial-ccw': the same anticlockwise)
    python3 -m tools.dial dial.jsonl            # writes colours.json

The dial sends its absolute position: one 24-byte packet per position, and
anticlockwise is the clockwise sequence in reverse. Only positions seen in
both directions are kept (the rest are corrupted packets or positions passed
too quickly to catch twice), in clockwise order. tools.replay --dial N then
sends position N.
"""
from __future__ import annotations

import argparse
import datetime
import json
from collections import Counter

DIAL_LEN = 24  # bytes; anything after that in a --length 60 capture is noise


def sequence(rows: list[dict], name: str, prefix: str) -> list[str]:
    """Positions in the order they arrived, repeats of the same position collapsed."""
    out: list[str] = []
    for row in rows:
        if row["button"] != name or not row["hex"].startswith(prefix):
            continue
        packet = row["hex"][:2 * DIAL_LEN]
        if not out or out[-1] != packet:
            out.append(packet)
    return out


def dial_prefix(rows: list[dict], names=("dial-cw", "dial-ccw"), key_bytes: int = 12) -> str:
    """The constant start of the dial's packets: the most common first 12 bytes."""
    counts = Counter(r["hex"][:2 * key_bytes] for r in rows if r["button"] in names)
    if not counts:
        raise SystemExit(f"No packets named {' or '.join(names)} in the recording.")
    return counts.most_common(1)[0][0]


def positions(rows: list[dict], cw: str = "dial-cw", ccw: str = "dial-ccw") -> tuple[list[str], bool]:
    """(positions in clockwise order, whether anticlockwise is exactly its reverse)."""
    prefix = dial_prefix(rows, (cw, ccw))
    clockwise, anticlockwise = sequence(rows, cw, prefix), sequence(rows, ccw, prefix)
    both = set(clockwise) & set(anticlockwise)
    order = list(dict.fromkeys(p for p in clockwise if p in both))
    reverse = list(dict.fromkeys(p for p in anticlockwise if p in both))
    return order, order == reverse[::-1]


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("log", help="JSON-lines file written by tools.record --log")
    p.add_argument("--out", default="colours.json")
    p.add_argument("--cw", default="dial-cw", help="name used for the clockwise recording")
    p.add_argument("--ccw", default="dial-ccw", help="name used for the anticlockwise recording")
    args = p.parse_args()

    with open(args.log) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    order, mirrored = positions(rows, args.cw, args.ccw)
    if not order:
        raise SystemExit("No position was seen in both directions: record both again, slowly.")
    data = {
        "recorded": datetime.date.today().isoformat(),
        "note": "Colour dial positions in clockwise order. Send one with: python3 -m tools.replay --dial N",
        "positions": order,
    }
    with open(args.out, "w") as fh:
        fh.write(json.dumps(data, indent=2) + "\n")
    print(f"{len(order)} positions written to {args.out}.")
    if not mirrored:
        print("Note: the anticlockwise order is not exactly the clockwise order reversed; "
              "some positions may be out of place. Recording both directions again, slowly, helps.")


if __name__ == "__main__":
    main()
