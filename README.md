# Keukenverlichting

Apple Home and Siri control for L&S Emotion kitchen lights, by talking to them
the same way their remote does (L&S 5991851: a TI CC2500 2.4 GHz radio and a
PIC16F1509). A small computer (here an Odroid) with a Raspberry Pi Pico and a
CC2500 module on USB does the talking. Nothing in the kitchen changes, and the
remote keeps working.

What's here:

- tools to find the remote's frequency, decode its packets, record every
  button and replay them (`tools/`);
- an Apple Home bridge with two lights, **Top** (groups 3 + 4) and **Bottom**
  (groups 1 + 2), that also follows the physical remote
  (`kitchenlights/homekit.py`, Docker);
- what is known about the radio protocol ([docs/protocol.md](docs/protocol.md)).

**No packets are included.** The remote has no rolling code, so recorded
packets switch the lights for anyone within radio range. Record your own with
the tools below. Other remotes of the same type most likely use a different
address, and possibly a different frequency or sync word: the tools find
those too.

## Hardware (no soldering)

The Odroid has no usable GPIO, so a Raspberry Pi **Pico** acts as a USB-to-SPI
bridge. The Odroid runs all the Python; the Pico runs a 30-line MicroPython
relay (`pico/main.py`).

```
Odroid --USB--> Pico H --6 jumper wires--> CC2500 module
```

| Part | What to look for | Approx. |
|---|---|---|
| Raspberry Pi **Pico H** (not the plain "Pico") | The "H" has the pin header **pre-soldered**. A Pico WH works too. | €5 |
| CC2500 module, plain (no PA/LNA), with antenna | **3.3 V**, pin header **already soldered**, **2.54 mm** pitch (some use 2.0 mm, which doesn't fit standard jumper wires) | €5–10 |
| 6 × female–female Dupont jumper wires | Usually included with the module | – |
| Micro-USB data cable, optionally a USB extension | The extension lets you move the radio out of a metal meter cupboard | – |

At 4 m with one wall a plain module is enough. Amplified (PA/LNA) modules
often need extra enable lines, which would mean more wiring for no gain here.

### Wiring

Unplug the USB cable before wiring. **Never connect VCC to VBUS/VSYS (5 V).**
That destroys the CC2500. The Pico's pin numbers are printed on the underside of
the board.

| CC2500 pin | Pico pin (physical) | Pico name |
|---|---|---|
| VCC / 3V3 | 36 | 3V3(OUT) |
| GND | 38 | GND |
| SI / MOSI | 25 | GP19 |
| SO / MISO | 21 | GP16 |
| SCLK / SCK | 24 | GP18 |
| CSN / CS | 22 | GP17 |
| GDO0, GDO2 | not connected | |

### Computer setup (Debian 13 laptop and the Odroid)

Do this on both machines. The laptop prepares the Pico; the Odroid runs it
afterwards. Nothing on the Pico depends on which computer it is plugged into.

```sh
sudo apt install -y git python3-serial python3-pytest pipx
sudo usermod -aG dialout $USER        # serial access; log out and back in
git clone https://github.com/bertneef/keukenverlichting && cd keukenverlichting
```

Check with `groups` that `dialout` is listed after logging back in.

On the laptop only, install `mpremote`, the tool that copies files onto the
Pico:

```sh
pipx install mpremote
pipx ensurepath                       # then open a new terminal
```

Use `pipx`, not `pip`. Debian 13 refuses `pip install` outside a virtual
environment ("externally-managed-environment"). Also, MicroPython itself is
not a Python package: there is nothing to `pip install` for it. It is the
`.uf2` firmware file in step 2 below.

### Pico setup (once, on the laptop)

1. Hold the Pico's BOOTSEL button while plugging it in. It appears as a USB
   drive named `RPI-RP2` and the desktop mounts it.
2. Download the MicroPython `.uf2` file from micropython.org/download/RPI_PICO
   (for a Pico WH: RPI_PICO_W) and copy it onto that drive. The Pico reboots
   by itself and the drive disappears. That is expected; no need to eject.
3. Copy the bridge on as `main.py`, then unplug and replug the Pico:

   ```sh
   mpremote cp pico/main.py :main.py
   ```

4. Check that the bridge answers. This prints `ok`:

   ```sh
   python3 -c "from kitchenlights.transport import *; open_transport(); print('ok')"
   ```

   If the CC2500 is already wired, run step 1 of *Capturing the protocol* for
   a few seconds to check the radio too.

The Pico shows up as `/dev/serial/by-id/usb-MicroPython_...`. The tools find
it automatically; pass `--port` to choose a different device.

Troubleshooting:
- "Permission denied" on the serial port: the `dialout` change has not taken
  effect yet. Log out and back in (or reboot).
- "Device or resource busy" in the first seconds after plugging in: ModemManager
  is probing the port. Wait 10 seconds and retry. If it keeps happening,
  `sudo systemctl disable --now ModemManager` (only needed for mobile modems).
- `mpremote: command not found`: open a new terminal after `pipx ensurepath`.

To update `main.py` later, first stop the running bridge (it ignores Ctrl-C on
purpose), then run the same `mpremote cp` command:

```sh
python3 -c "from kitchenlights.transport import *; open_transport().stop_bridge()"
```

(If the CC2500 is ever wired straight to a Raspberry Pi instead: SPI0 pins
1/6/19/21/23/24, install `python3-spidev`. The tools fall back to that when no
Pico is found.)

## Capturing the protocol

Testing will switch the kitchen lights on and off. That's expected.

Before you start, switch off the laptop's own Wi-Fi and Bluetooth. A few cm
from the antenna they are louder than anything else on 2.4 GHz:

```sh
nmcli radio wifi off; rfkill block bluetooth   # undo: nmcli radio wifi on; rfkill unblock bluetooth
```

Both steps use the same routine: press Enter once, then keep pressing **one**
button (e.g. group 1 on/off) about twice a second, with the remote ~10 cm from
the module's antenna. A press is a short radio burst, so the tools listen on
one setting at a time for a while rather than sweeping quickly.

**Quickest: the guided run** (about 7 minutes; it chains steps 1–3 below):

```sh
python3 -m tools.hunt
```

It asks for Enter before each stage, so you can rest in between: a quiet
scan, a coarse scan while pressing, a fine scan (0.1 MHz steps) around the
best candidates while pressing, then sniffing at the best fine frequency and
the analysis. Everything lands in a new folder `hunt-YYYYMMDD-HHMMSS/`
(`hunt.log`, `scan.csv`, `fine.csv`, `captures.jsonl`). If you already know roughly where the remote is,
`--around 2435.6` skips the coarse scan and `--freq 2435.7` skips both scans.

The manual steps, if you want to run them one by one:

**1. Find the frequency** (about 3 minutes: 1 minute quiet, then 2 minutes of
pressing).

```sh
python3 -m tools.scan_spectrum --out scan.csv
```

During phase 2, channels that light up print `<- N strong samples` straight
away. In the final table, a real hit has a clear `active` share, about 0 %
`quiet`, and `max act` around -50 dBm or stronger (Wi-Fi shows up in both
columns). Several neighbouring channels lighting up together is normal; take
the strongest. If nothing comes close to -50 dBm, look at `scan.csv` (see
"Plotting the scan in R" below) before going on to step 2.

**2. Find the data rate and sync word** (about 2 minutes). Delete an old
captures file first: new captures are appended to it.

```sh
python3 -m tools.sniff --freq 2433.2 --out captures.jsonl   # use your frequency
```

**3. Analyse:**

```sh
python3 -m tools.analyse captures.jsonl
```

The right profile shows high `repeat` counts: the same packet arriving again
and again. Note its frequency, data rate and sync word: if they differ from
`kitchenlights/remote.py`, change them there before recording buttons.

If the analyser finds nothing: check that step 1's peak was clearly above the
baseline. If Wi-Fi floods the captures, rerun step 2 with `--cs-thr 6`. If
the remote is close, try `--min-rssi -70` in step 3.

## Recording buttons and testing replay

**4. Record every button.** For each button, type a short name, press Enter,
then press that button about once per second for 8 seconds:

```sh
python3 -m tools.record
```

Use names that say what the button is, e.g. `g1` … `g4` for the group
buttons. To tidy a `buttons.json` recorded earlier (noise after short packets,
corrupted presses), run `python3 -m tools.record --clean`. A toggle button shows two variants that alternate per press
(`[1, 2, 1, 2, ...]`): one is "on", the other "off". Everything goes into
`buttons.json`. Keep a copy somewhere safe: without the remote it can't be
recorded again.

**4b. The colour dial** (optional). Record it turning slowly clockwise over
its whole range (name `dial-cw`), then anticlockwise (`dial-ccw`), with longer
packets and a raw log, then build `colours.json`:

```sh
python3 -m tools.record --length 60 --out dial.json --log dial.jsonl
python3 -m tools.dial dial.jsonl
```

**5. Replay a press.** Put the remote away, so that only the module transmits:

```sh
python3 -m tools.replay g1 1
python3 -m tools.replay g1 2
python3 -m tools.replay --dial 1      # a colour, after step 4b
```

Note what the lights do for each variant. If nothing happens, try
`--dev 62500`, then `--freq 2436.3`, and move the module closer to the lights
(both transmit about 1 mW, but its small antenna may reach less far).

## Apple Home bridge (on the Odroid)

Needs the Pico + CC2500 plugged into the Odroid and a cleaned `buttons.json`
(`python3 -m tools.record --clean`) copied into the repo folder there. The
bridge reads the on/off packets of g1–g4 from it.

**1. Configure** (once, in the repo folder on the Odroid):

```sh
ls /dev/serial/by-id/                 # the Pico: usb-MicroPython_...
cp .env.example .env
nano .env                             # set PICO_DEVICE to that /dev/serial/by-id/... path
```

**2. Build and start** the container:

```sh
docker compose up -d --build
docker compose logs -f                # Ctrl-C stops watching, not the bridge
```

The log shows a QR code and an 8-digit setup code. In the Home app: **+**,
Add Accessory, scan the code (or More options, "Kitchen Lights", and type it).
iOS warns that it is an uncertified accessory; add it anyway. Then switch
Top and Bottom from the Home app, and press a group button on the remote:
the log shows `remote: g1 on` and the light in the Home app follows.

**Siri.** Every Home accessory works with Siri; how you phrase it depends on
names and rooms in the Home app:

1. Put Top and Bottom in the room **Kitchen** (accessory settings, Room).
2. Give them names that are easy to say and unique in the house, e.g. "Top
   light" and "Bottom light". The Home app shows names without the room.
3. Then:
   - "Hey Siri, turn off the kitchen lights": both (every light in Kitchen);
   - "Hey Siri, turn on the top light": just that one;
   - for other combinations, a scene (e.g. "Cooking": Top on, Bottom off):
     "Hey Siri, cooking".

They are exposed as lights, not switches, so that "the kitchen lights"
includes them: Siri leaves switches out of that. Bridges paired before this
change (2026-11-22) keep their pairing; after the update the Home app shows
lights instead of switches within a minute or so. If the tiles stay switches,
restart the Home app, or as a last resort remove the bridge in the Home app
and pair again.

How the container is set up (`compose.yaml`):

- `network_mode: host`: Apple Home finds the bridge via mDNS multicast, which
  doesn't cross Docker's bridge network. The bridge listens on TCP port 51826.
- The Pico is passed in as `/dev/pico`.
- `buttons.json` is mounted read-only. Compose refuses to start if it is
  missing, instead of creating an empty folder with that name.
- `homekit-state/` holds the pairing data.
- `restart: unless-stopped`: if the Pico stops answering (10 failed polls in a
  row), the bridge exits and Docker starts it again. If the Pico was unplugged
  and doesn't come back, plug it in and run `docker compose up -d
  --force-recreate`.
- Updating after a `git pull`: `docker compose up -d --build`.

Without Docker, the bridge can also run in a venv with the systemd unit in
`deploy/`: `python3 -m venv .venv && .venv/bin/pip install -r
requirements.txt`, then install the unit as described at its top.

Pairing data and the last known light state are in `homekit-state/`. To pair
from scratch, stop the bridge (`docker compose down`), delete that folder (it is
owned by root when Docker created it: `sudo rm -r homekit-state`), and remove the bridge in
the Home app. The Odroid and the Home hub (Apple TV / HomePod) must be on the
same network: Apple Home finds the bridge via mDNS, and talks to it on TCP
port 51826.

A light shows "on" when any of its groups is on. The bridge only knows what
it sent and what it heard the remote send: if the lights were switched while
the bridge was off, the Home app is wrong until the next switch.

The protocol, including dimming, presets, power and the colour button and
dial, is described in [docs/protocol.md](docs/protocol.md).

### Plotting the scan in R

```r
library(tidyverse)
read_csv("scan.csv") |>
  group_by(phase, freq_mhz) |>
  summarise(rssi = max(rssi_dbm), .groups = "drop") |>   # bursts: max, not a quantile
  ggplot(aes(freq_mhz, rssi, colour = phase)) + geom_line()
```

## Development

The pure helpers and the analyser have offline tests (no radio needed):

```sh
python3 -m pytest -q tests
```

## Licence

[CC0 1.0](LICENSE): public domain, use it however you like.
