# L&S Emotion remote: radio protocol

What we know about the L&S Emotion remote (L&S 5991851: TI CC2500 radio and
a PIC16F1509), from captures of one remote made on 2026-11-21 and 2026-11-22.

**No packets from that remote are published here.** The remote has no rolling
code, so a recorded packet switches the lights for anyone within radio range.
Byte values that are probably specific to one remote (its address, the
checksums) are shown as placeholders: `A0 … A7` and `B0 … B7` for the two
address headers. Record your own remote with `tools.record` (and
`tools.dial` for the colour dial), and keep the files somewhere safe: once
the remote is gone, its packets can no longer be recorded.

## Radio

| Setting | Value |
|---|---|
| Frequency | 2436.25 MHz (fine scan peak 2436.2–2436.3 MHz) |
| Modulation | 2-FSK, 250 kBd; 62.5 kHz and 127 kHz deviation both decode it, 127 kHz is used for sending |
| Preamble | `0xAA…` (4 bytes are enough for the lights) |
| Sync word | `0x2C6E`, sent twice (CC2500 `SYNC_MODE` 3) |
| Packet | fixed length, no TI whitening, no standard CC2500 CRC |

`0x2C6E` is the bitwise inverse of TI's default sync word `0xD391`, so our
receiver sees every bit inverted compared with how the remote's own CC2500
was probably configured. That doesn't matter for replaying: sending exactly
the bits we received, with the same settings, reproduces the remote's signal.
The settings live in `kitchenlights/remote.py`.

One press sends a train of identical packets for about 100–150 ms. Replaying
one packet back to back for 0.15 s (`tools.replay`) switches the lights.

## Two packet families

All byte positions below count from the first byte after the sync word, as
received (inverted).

### 36-byte packets: groups, up, down

```
A0 … A7 | b8 b9 b10 b11 | b12 | b13 | b14 | b15 | ff × 8 (9) | 12 bytes, probably a checksum
```

- Bytes 0–7 (`A0 … A7`) are the same for every button: probably the remote's
  address.
- Byte 8 holds the command. Inverted and read in reverse bit order, it becomes:
  - high nibble = group (0–3 for g1–g4);
  - low nibble = 3 for on, 2 for off.
- In the first recording, bytes 13 / 15 were `f7` / `ce` in "on"-type
  packets (including up/down) and `f4` / `cc` in "off" packets. Bytes 12–15
  also depend on the remote's state, though (see "Group packets depend on
  the remote's state" below), so only byte 8 reliably tells on from off.
- On and off are explicit commands, not a toggle (tested 2026-11-22: sending g1
  "on" twice leaves group 1 on). The remote's group buttons alternate between
  the two on successive presses.
- The last 12 bytes differ per command and are needed: the lights ignored
  packets that were cut off after 24 bytes. They are probably a checksum over
  the rest; it isn't decoded, so new commands can only be recorded, not
  composed.

| Button | Command | Byte 8 | Decoded byte 8 |
|---|---|---|---|
| g1 (bottom) | on / off | `3f` / `bf` | `03` / `02` |
| g2 (bottom) | on / off | `37` / `b7` | `13` / `12` |
| g3 (top) | on / off | `3b` / `bb` | `23` / `22` |
| g4 (top) | on / off | `33` / `b3` | `33` / `32` |
| up (dimming) | brighter | `03` | `3f` |
| down (dimming) | darker | `23` | `3b` |

Up / down sent one packet per press in the recordings (`packets_per_press`
1.0), so they probably send a shorter train. Which groups they dim (the last
selected group, or all groups) is not known yet.

### 24-byte packets: power and presets

```
B0 … B7 | 16 bytes that differ per command
```

- Bytes 0–7 (`B0 … B7`) are shared by power, h1 and h2, but differ from the
  36-byte family.
- Everything after them varies, so no field layout is known yet.
- `power` alternates between two packets on successive presses (on / off,
  like a group button).
- `h1` and `h2` are presets and always send the same packet.
- Now and then a slightly different packet appears in this family (a
  different header after the first 4 bytes, a few presses in h1, h2 and
  power). It was
  dropped as corrupted, but it may be a second packet type; check this if
  presets ever misbehave when replayed.

## Recording your own packets

1. `python3 -m tools.record` for every button (names such as `g1` … `g4`,
   `power`, `h1`, `h2`, `up`, `down`, `colour`) writes `buttons.json`.
2. `python3 -m tools.record --length 60 --out dial.json --log dial.jsonl`
   with the dial turned slowly clockwise (name `dial-cw`), then anticlockwise
   (`dial-ccw`), followed by `python3 -m tools.dial dial.jsonl`, writes
   `colours.json` (see the colour dial below).
3. Test with `python3 -m tools.replay g1 1`, `--hex <packet>` or `--dial N`.

Which of the two power packets is on and which is off was not tested.

## Colour button and dial

Recorded on 2026-11-22 with `tools.record --log colour.jsonl`. Both send
36-byte packets from the same family as the group buttons.

**Colour button:** alternates between two packets on successive presses,
exactly like a group button:
- 22 of 44 packets have the "on" flags (byte 8 `0b`, bytes 13/15 `f7`/`ce`);
- 20 have the "off" flags (`8b`, `f4`/`cc`);
- the rest are corrupted copies.

Decoded, byte 8 is `2f` / `2e`: the low bit flips like on/off does for the
groups, but the high nibble isn't a group number. Most likely it switches
colour mode on and off.

**Colour dial, first recording (2026-11-22, morning):** while the dial turned,
the remote repeated one constant 36-byte packet (byte 8 `fb`, bytes 10–11
`0e6a`, byte 15 `cf`), the same in both directions. Replaying it for 2 s
changed nothing. Most likely the lights were in white mode then (see the
colour button).

**Colour dial, second recording (2026-11-22, afternoon):** with
`tools.record --length 60` and `tools.timing`, the dial sends its absolute
position:

- **Packet:** 24 bytes, from the power / preset family. Bytes 0–11 are
  constant (`B0 … B7` plus 4 fixed bytes). Bytes 12–23 change with
  every position; bytes 16–23 are probably a checksum. After byte 24 the
  60-byte captures are noise, so the packet is 24 bytes.
- **Direction:** anticlockwise sends exactly the clockwise sequence in reverse.
  57 positions were seen in both directions, in matching order. Direction is
  simply the order in which positions arrive, not a field in the packet.
- **Positions are stored** in clockwise order in `colours.json`, which
  `tools.dial` builds from a recording of both directions.
  `python3 -m tools.replay --dial N` sends position N.
  28 more values were seen in one direction only: corrupted copies, or
  positions passed too quickly to catch twice. Whether the dial wraps around
  (a full circle) is unknown.
- **Timing** (`tools.timing`): while turning, a packet of about 0.7 ms every
  4–5 ms (median start-to-start 4.6 ms); a group button sends a packet about
  every 32 ms. Nothing in the timing differs between the two directions.

So a colour can be set directly: send the packet for its position. **Tested
on 2026-11-22:** `tools.replay --dial 1` and `--dial 30` each changed the
lights' colour, without pressing the colour button first. Every position in
`colours.json` is therefore a reproducible colour command, also without the
remote.

**Group packets depend on the remote's state.** The g1 packets recorded in
the second session differ from the earlier `buttons.json` in bytes 12–15 and
in the last 12 bytes:

| | Bytes 12–15 | Last 12 bytes |
|---|---|---|
| earlier g1 "on" | `fe f7 f8 ce` | (checksum 1) |
| later g1 "on" | `f9 ff cc c6` | (checksum 2, different) |

Byte 8, group and on/off, is unchanged. Bytes 12–15 probably carry the
remote's current mode, colour or brightness, which a group command applies
along with on/off. Both versions switch the lights, but replaying an older
packet may also restore the mode it was recorded in. The bridge uses the
packets in `buttons.json`.
