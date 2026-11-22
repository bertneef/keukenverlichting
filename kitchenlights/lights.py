"""Switch light groups on/off with recorded commands, and follow the physical remote.

Every group button sends one of two fixed commands. Tests on 2026-11-22: g1
variant 1 turned group 1 on, sending it again changed nothing, variant 2
turned it off. So the commands are explicit on / off, not a toggle.

The on and off commands differ in bit 7 of byte 8 (0 = on). That is used
here to label the variants in buttons.json, whichever order they were
recorded in. (Bytes 13 and 15 also differed in the first recording, but
bytes 12-15 change with the remote's state, so they are not used.)
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from kitchenlights import remote
from kitchenlights.cc2500 import MARC_RXFIFO_OVERFLOW

GROUPS = ("g1", "g2", "g3", "g4")
SEND_SECONDS = 0.15  # one replayed press; tested to switch a group reliably


def is_on_command(packet: bytes) -> bool:
    return not packet[8] & 0x80


def load_commands(path: str | Path, groups=GROUPS) -> dict[str, dict[str, bytes]]:
    """{group: {"on": packet, "off": packet}} from a buttons.json written by tools.record."""
    buttons = json.loads(Path(path).read_text())
    commands = {}
    for group in groups:
        if group not in buttons:
            raise SystemExit(f"{path} has no button '{group}': record it with tools.record.")
        variants = [bytes.fromhex(v) for v in buttons[group]["variants"]]
        on = [v for v in variants if is_on_command(v)]
        off = [v for v in variants if not is_on_command(v)]
        if len(on) != 1 or len(off) != 1:
            raise SystemExit(f"'{group}' in {path} should have one on and one off variant; "
                             "run tools.record --clean, or record it again.")
        commands[group] = {"on": on[0], "off": off[0]}
    return commands


class Lights:
    """Owns the radio: sends on/off commands and, in between, listens for the remote.

    The Pico bridge handles one request at a time, so every radio access goes
    through `self.lock`.
    """

    def __init__(self, radio, commands: dict[str, dict[str, bytes]], state_file: str | Path | None = None):
        self.radio = radio
        self.commands = commands
        self.lock = threading.Lock()
        self.state_file = Path(state_file) if state_file else None
        self.state = {g: False for g in commands}
        if self.state_file and self.state_file.exists():
            self.state.update(json.loads(self.state_file.read_text()))
        self.receiving = False
        self.on_change: Callable[[str, bool], None] | None = None
        self.on_fatal: Callable[[], None] | None = None  # e.g. stop the service so it gets restarted
        self.failed = False
        self._stop = threading.Event()

    # --- sending ---------------------------------------------------------
    def set(self, groups, on: bool) -> None:
        for group in groups:
            packet = self.commands[group]["on" if on else "off"]
            with self.lock:
                remote.send(self.radio, remote.profile(pkt_len=len(packet)), packet, SEND_SECONDS)
                self.receiving = False  # send() left the radio configured for TX
            self._update(group, on, notify=False)

    # --- listening -------------------------------------------------------
    def match(self, packet: bytes) -> tuple[str, bool] | None:
        """(group, on) if `packet` is one of the known commands."""
        for group, cmds in self.commands.items():
            for name, cmd in cmds.items():
                if packet[:len(cmd)] == cmd:
                    return group, name == "on"
        return None

    def poll(self) -> tuple[str, bool] | None:
        """Check the radio once for a packet from the remote; returns what it switched."""
        want = remote.PACKET_LEN + 2  # payload + appended RSSI, LQI
        with self.lock:
            radio = self.radio
            if not self.receiving:
                radio.configure(remote.profile())
                radio.flush_rx()
                radio.rx()
                self.receiving = True
                return None
            n = radio.rx_bytes()
            if n & 0x80 or radio.marcstate() == MARC_RXFIFO_OVERFLOW:
                radio.flush_rx()
                radio.rx()
                return None
            if n < want:
                return None
            packet = bytes(radio.read_fifo(want)[:-2])
            radio.flush_rx()
            radio.rx()
        hit = self.match(packet)
        if hit:
            self._update(*hit, notify=True)
        return hit

    def listen_forever(self, interval: float = 0.005, max_failures: int = 10) -> None:
        """Poll until stop(); after `max_failures` errors in a row call `on_fatal` (radio gone)."""
        failures = 0
        while not self._stop.is_set():
            try:
                self.poll()
                failures = 0
            except Exception as exc:  # keep following the remote after a glitch
                failures += 1
                print(f"listen: {exc!r} ({failures}/{max_failures})", flush=True)
                self.receiving = False
                if failures >= max_failures:
                    print("listen: radio not responding, stopping", flush=True)
                    if self.on_fatal:
                        self.on_fatal()
                    return
                time.sleep(1)
            time.sleep(interval)

    def start_listening(self) -> threading.Thread:
        thread = threading.Thread(target=self.listen_forever, name="remote-listener", daemon=True)
        thread.start()
        return thread

    def stop(self) -> None:
        self._stop.set()

    # --- state -----------------------------------------------------------
    def _update(self, group: str, on: bool, notify: bool) -> None:
        if self.state.get(group) == on:
            return
        self.state[group] = on
        if self.state_file:
            self.state_file.write_text(json.dumps(self.state) + "\n")
        if notify and self.on_change:
            self.on_change(group, on)
