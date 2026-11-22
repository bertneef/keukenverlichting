"""Apple Home bridge: two lights, Top (g3 + g4) and Bottom (g1 + g2).

    python3 -m kitchenlights.homekit --buttons buttons.json

On the first start it prints a setup code: in the Home app, Add Accessory,
More options, then pick "Kitchen Lights" and enter the code. Pairing data is
kept in --state-dir; delete that folder to pair again from scratch.

The physical remote keeps working. The bridge listens for it between its own
transmissions and updates the lights, so Apple Home shows what the remote
did. A light is on when any of its groups is on.

They are exposed as lights (a Lightbulb service with only on/off), not as
switches: Siri then counts them as lights, so "turn off the kitchen lights"
switches both once they are in the room Kitchen, and "turn on the top light"
just the one. Changing this from Switch (before 2026-11-22) keeps the pairing:
HAP-python raises the configuration number and the Home app re-reads it.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_LIGHTBULB

from kitchenlights.lights import Lights, load_commands

SWITCHES = {"Top": ("g3", "g4"), "Bottom": ("g1", "g2")}


class GroupLight(Accessory):
    """One Apple Home light (on/off only) for one or more light groups."""

    category = CATEGORY_LIGHTBULB

    def __init__(self, driver, name: str, groups: tuple[str, ...], lights: Lights):
        super().__init__(driver, name)
        self.groups = groups
        self.lights = lights
        service = self.add_preload_service("Lightbulb")
        self.char_on = service.configure_char("On", value=self.current(), setter_callback=self.set_on)

    def current(self) -> bool:
        return any(self.lights.state[g] for g in self.groups)

    def set_on(self, value: bool) -> None:
        # Called inside the network event loop: do the ~0.3 s of radio work on a worker thread.
        self.driver.add_job(self.lights.set, self.groups, bool(value))

    def refresh(self) -> None:
        self.char_on.set_value(self.current())  # notifies Apple Home only if it changed


def build(driver, lights: Lights, switches=SWITCHES) -> Bridge:
    bridge = Bridge(driver, "Kitchen Lights")
    accessories = {name: GroupLight(driver, name, groups, lights) for name, groups in switches.items()}
    for accessory in accessories.values():
        bridge.add_accessory(accessory)

    def on_change(group: str, on: bool) -> None:  # from the listener thread
        print(f"remote: {group} {'on' if on else 'off'}", flush=True)
        for accessory in accessories.values():
            if group in accessory.groups:
                accessory.refresh()

    lights.on_change = on_change
    return bridge


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--buttons", default="buttons.json", help="recorded commands (tools.record)")
    p.add_argument("--state-dir", default="homekit-state", help="pairing data and last known light state")
    p.add_argument("--port", help="Pico serial device (default: auto-detect)")
    p.add_argument("--hap-port", type=int, default=51826, help="TCP port for Apple Home")
    p.add_argument("--no-listen", action="store_true", help="don't follow the physical remote")
    args = p.parse_args()

    from kitchenlights.cc2500 import CC2500

    commands = load_commands(args.buttons)
    state_dir = Path(args.state_dir)
    state_dir.mkdir(exist_ok=True)

    radio = CC2500(args.port)
    radio.reset()
    if radio.identify()[0] != 0x80:
        raise SystemExit("No CC2500 found: check the wiring.")
    lights = Lights(radio, commands, state_dir / "lights.json")

    driver = AccessoryDriver(port=args.hap_port, persist_file=str(state_dir / "accessory.state"))
    driver.add_accessory(accessory=build(driver, lights))
    signal.signal(signal.SIGTERM, driver.signal_handler)
    # Pico unplugged or hung: stop, and let Docker / systemd start a fresh process.
    def fatal() -> None:
        lights.failed = True
        os.kill(os.getpid(), signal.SIGTERM)

    lights.on_fatal = fatal
    if not args.no_listen:
        lights.start_listening()
    try:
        driver.start()  # blocks until SIGINT / SIGTERM
    finally:
        lights.stop()
        try:
            with lights.lock:
                radio.close()
        except Exception as exc:  # the radio may be the reason we are stopping
            print(f"closing the radio: {exc!r}", flush=True)
    if lights.failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
