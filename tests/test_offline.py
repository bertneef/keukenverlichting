import json
import random
import subprocess
import sys

from kitchenlights.bits import packet_key, preamble_end, to_bits
from kitchenlights.cc2500 import (FREQ0, FREQ1, FREQ2, MDMCFG3, MDMCFG4, Profile, chanbw_regs,
                                  deviatn_reg, drate_hz, drate_regs, freq_regs, rssi_to_dbm)


def test_freq_matches_ti_preset():
    assert freq_regs(2_433_000_000) == (0x5D, 0x93, 0xB1)


def test_drate_matches_ti_presets():
    assert drate_regs(250_000) == (0x0D, 0x3B)
    assert drate_regs(500_000) == (0x0E, 0x3B)
    for baud in (2_400, 10_000, 38_400, 100_000, 250_000, 500_000):
        assert abs(drate_hz(*drate_regs(baud)) - baud) / baud < 0.002


def test_chanbw_and_deviation():
    assert chanbw_regs(812_000) == (0, 0)
    assert chanbw_regs(58_000) == (3, 3)
    assert deviatn_reg(38_000) == 0x44  # TI preset: 38.085 kHz


def test_profile_registers():
    regs = Profile().registers()
    assert (regs[FREQ2], regs[FREQ1], regs[FREQ0]) == (0x5D, 0x93, 0xB1)
    assert regs[MDMCFG4] & 0x0F == 0x0D and regs[MDMCFG3] == 0x3B
    assert all(0 <= v <= 0xFF for v in regs.values())


def test_rssi():
    assert rssi_to_dbm(0x00) == -72
    assert rssi_to_dbm(0xF0) == -80


def synthetic_capture(payload: bytes, skip_bits: int) -> bytes:
    bits = "10" * 16 + to_bits(b"\xd3\x91" + payload)
    bits = bits[skip_bits:]
    bits += "".join(random.choice("01") for _ in range(480 - len(bits)))
    return bytes(int(bits[i:i + 8], 2) for i in range(0, 480, 8))


def test_packet_key_is_stable_across_trigger_offsets():
    payload = bytes.fromhex("12345678 9abcdef0")
    keys = {packet_key(synthetic_capture(payload, skip)) for skip in (0, 2, 4, 10, 16)}
    assert len(keys) == 1
    bits = to_bits(synthetic_capture(payload, 6))
    assert bits[preamble_end(bits):].startswith("1101001110010001")


def test_analyse_cli(tmp_path):
    path = tmp_path / "c.jsonl"
    with open(path, "w") as fh:
        for i in range(10):
            fh.write(json.dumps({"profile": "good", "rssi_dbm": -40, "freq_offset_hz": 30000,
                                 "hex": synthetic_capture(b"\xab\xcd\x01\x02\x03\x04\x05\x06", 2 * (i % 4)).hex()}) + "\n")
            fh.write(json.dumps({"profile": "bad", "rssi_dbm": -40, "freq_offset_hz": 0,
                                 "hex": random.randbytes(60).hex()}) + "\n")
    out = subprocess.run([sys.executable, "-m", "tools.analyse", str(path)],
                         capture_output=True, text=True, check=True).stdout
    assert "Best profile: good" in out and "d3 91 ab cd 01" in out


class FakePicoSerial:
    """Speaks the pico/main.py framing; echoes PARTNUM 0x80 for status reads."""

    def __init__(self, *args, **kwargs):
        self.out = bytearray()
        self.written = []

    def reset_input_buffer(self):
        self.out.clear()

    def write(self, data):
        if data == b"\x00":
            self.out += b"K"
            return
        n, payload = data[0], data[1:]
        assert n == len(payload)
        self.written.append(bytes(payload))
        self.out += bytes([0] + [0x80] * (n - 1))

    def read(self, n):
        reply, self.out[:] = bytes(self.out[:n]), self.out[n:]
        return reply

    def close(self):
        pass


def test_pico_transport_framing(monkeypatch):
    import types
    from kitchenlights.cc2500 import CC2500, PARTNUM
    from kitchenlights.transport import PicoTransport

    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=FakePicoSerial))
    transport = PicoTransport("/dev/fake")
    radio = CC2500(transport=transport)
    assert radio.read_status(PARTNUM) == 0x80
    assert transport.ser.written[-1] == bytes([PARTNUM | 0xC0, 0])


def test_analyse_empty_file(tmp_path):
    path = tmp_path / "empty.jsonl"
    path.write_text("")
    res = subprocess.run([sys.executable, "-m", "tools.analyse", str(path)], capture_output=True, text=True)
    assert res.returncode != 0 and "no captures" in res.stderr and "Traceback" not in res.stderr


class FakeRadio:
    def __init__(self):
        self.calls = []

    def flush_rx(self):
        self.calls.append("flush")

    def set_frequency(self, f):
        self.calls.append("freq")

    def rx(self):
        self.calls.append("rx")

    def read_status(self, addr):
        return 0x10

    def marcstate(self):
        return 0x0D


def test_dwell_flushes_fifo_and_samples():
    from tools.scan_spectrum import SCAN_PROFILE, dwell
    from kitchenlights.cc2500 import MDMCFG2

    radio = FakeRadio()
    samples = dwell(radio, 2_400e6, 0.01)
    assert radio.calls == ["flush", "freq", "rx"] and len(samples) > 1
    assert SCAN_PROFILE.registers()[MDMCFG2] & 0x07 != 0  # never sync_mode 0 (free-running RX)


def test_stuck_rssi_is_detected():
    import pytest
    from tools.scan_spectrum import check_live

    with pytest.raises(SystemExit):
        check_live([-111.5] * 2000)
    check_live([-90 - i % 7 for i in range(200)])


def test_rank_prefers_burst_only_while_pressing():
    from tools.scan_spectrum import rank

    noise = [-92.0, -91.0, -93.0, -90.0] * 50
    wifi = noise[:-4] + [-45.0] * 4  # strong in both phases
    remote = noise[:-10] + [-38.0] * 10  # strong only while pressing
    freqs = [2_433e6, 2_460e6]
    top = rank(freqs, quiet=[noise, wifi], active=[remote, wifi], margin=10)[0]
    assert top[1] == 2_433e6


class FakeRemoteRadio:
    """Simulated CC2500 next to a remote at 2435.7 MHz that sends in bursts while pressed."""

    CARRIER = 2_435.7e6

    def __init__(self, port=None):
        self.freq = 0.0
        self.bandwidth = 812_000
        self.pressing = False
        self.rng = random.Random(1)
        self.packet = synthetic_capture(b"\xab\xcd\x01\x02\x03\x04\x05\x06", 0)[:60]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def reset(self):
        pass

    def identify(self):
        return 0x80, 0x03

    def configure(self, profile):
        self.freq, self.bandwidth = profile.freq_hz, profile.effective_bandwidth_hz

    def set_frequency(self, f):
        self.freq = f

    def flush_rx(self):
        pass

    def rx(self):
        pass

    def burst(self) -> bool:
        return self.pressing and self.rng.random() < 0.3

    def signal_dbm(self) -> float:
        outside = max(0.0, abs(self.freq - self.CARRIER) - self.bandwidth / 2)
        return -38 - outside / 10_000  # 1 dB per 10 kHz outside the filter

    def read_status(self, addr):
        dbm = max(self.signal_dbm(), -92) if self.burst() else -92 + self.rng.random() * 3
        return int((dbm + 72) * 2) & 0xFF

    # sniffing: a packet is waiting whenever a burst is on and the tuning is close
    def rx_bytes(self):
        return 62 if self.burst() and abs(self.freq - self.CARRIER) < 150_000 else 0

    def marcstate(self):
        return 0x0D

    def freq_offset_hz(self):
        return self.CARRIER - self.freq

    def read_fifo(self, n):
        return list(self.packet) + [0x40, 0x7F]


def test_hunt_end_to_end(tmp_path, monkeypatch):
    import tools.hunt as hunt

    radio = FakeRemoteRadio()
    prompts = []

    def ask(prompt):  # stage 1 is quiet, every later stage is pressing
        prompts.append(prompt)
        radio.pressing = len(prompts) >= 2

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hunt, "ask", ask)
    monkeypatch.setattr("tools.sniff.time.sleep", lambda s: None)
    hunt.main(["--quiet-dwell", "0.01", "--dwell", "0.02", "--fine-dwell", "0.02",
               "--sniff-seconds", "0.05"], radio_factory=lambda port: radio)

    folder = next(tmp_path.glob("hunt-*"))
    log = (folder / "hunt.log").read_text()
    assert len(prompts) == 4
    tuned = float(prompts[3].split("Sniffing at ")[1].split(" MHz")[0])
    assert abs(tuned - 2435.7) <= 0.1, log
    assert "Best profile" in log and "d3 91 ab cd 01" in log


def test_best_frequency_takes_centre_of_bright_run():
    from tools.hunt import best_frequency

    points = [(2435.0e6, -90), (2435.1e6, -60), (2435.2e6, -41), (2435.3e6, -40),
              (2435.4e6, -41), (2435.5e6, -62), (2436.5e6, -40.5)]  # last one: separate run
    centre, peak = best_frequency(points)
    assert abs(centre - 2435.3e6) < 20_000 and peak == -40


class FalseSyncRadio:
    """Leaves RX after 500 RSSI reads (a false sync) and then reports a frozen value."""

    def __init__(self):
        self.reads = 0
        self.tunes = 0
        self.rng = random.Random(2)

    def flush_rx(self):
        pass

    def set_frequency(self, f):
        pass

    def rx(self):
        self.tunes += 1
        self.reads = 0

    def marcstate(self):
        return 0x0D if self.reads < 500 else 0x01

    def read_status(self, addr):
        self.reads += 1
        return 0x10 if self.reads >= 500 else self.rng.randrange(0x20, 0x60)


def test_dwell_recovers_from_false_sync():
    from tools.scan_spectrum import dwell

    radio = FalseSyncRadio()
    samples = dwell(radio, 2_436e6, 0.2)
    assert radio.tunes > 1
    frozen = rssi_to_dbm(0x10)
    assert sum(x == frozen for x in samples) < 0.1 * len(samples)


def test_fine_profiles_use_32_bit_sync():
    from kitchenlights.cc2500 import MDMCFG2
    from tools.hunt import FINE_PROFILE
    from tools.scan_spectrum import SCAN_PROFILE

    for profile in (SCAN_PROFILE, FINE_PROFILE):
        assert profile.registers()[MDMCFG2] & 0x07 == 3


def test_remote_profile_matches_capture():
    from kitchenlights import remote
    from kitchenlights.cc2500 import MDMCFG2, PKTLEN, SYNC0, SYNC1

    regs = remote.profile().registers()
    assert (regs[SYNC1], regs[SYNC0]) == (0x2C, 0x6E)
    assert regs[MDMCFG2] & 0x07 == 3  # sync word sent / matched twice, as the remote does
    assert regs[PKTLEN] == 36


def test_record_groups_presses_and_variants():
    from tools.record import presses, summarise

    a, b = bytes.fromhex("a0a1a2"), bytes.fromhex("a0a1a3")
    packets = [(0.00, a, -40), (0.03, a, -40), (0.06, a, -40),
               (1.00, b, -40), (1.03, b, -40), (1.06, a, -40), (1.09, b, -40),  # one corrupted packet
               (2.00, a, -40)]
    per_press = presses(packets)
    assert per_press == [a, b, a]
    assert summarise(per_press + [b]) == {"variants": [a.hex(), b.hex()], "presses": [1, 2, 1, 2]}


def test_record_trims_noise_and_drops_corrupted_presses():
    from tools.record import summarise

    rng = random.Random(3)
    short = bytes(rng.randrange(256) for _ in range(24))  # a 24-byte packet...
    per_press = [short + bytes(rng.randrange(256) for _ in range(12)) for _ in range(20)]  # ...then noise
    corrupted = bytes(rng.randrange(256) for _ in range(36))
    result = summarise(per_press[:10] + [corrupted] + per_press[10:])
    assert result["variants"] == [short.hex()]
    assert result["presses"] == [1] * 10 + [0] + [1] * 10


class FakeTxRadio:
    def __init__(self):
        self.calls = []
        self.state = 0x01

    def configure(self, profile):
        self.calls.append(("configure", profile.pkt_len))

    def idle(self):
        self.state = 0x01

    def strobe(self, cmd):
        self.calls.append(("strobe", cmd))
        if cmd == 0x35:  # STX: finishes after one status read
            self.state = 0x13

    def write_burst(self, addr, values):
        self.calls.append(("fifo", addr, bytes(values)))

    def marcstate(self):
        state, self.state = self.state, 0x01
        return state

    def read_status(self, addr):
        return 0  # TXBYTES: FIFO emptied


def test_replay_sends_payload_until_time_is_up():
    from kitchenlights import remote
    from tools.replay import send

    radio = FakeTxRadio()
    payload = bytes.fromhex("a0a1a2a3a4a5a6a73fc7cedafef7f8ce" + "ff" * 8 + "c0c1c2c3c4c5c6c7c8c9cacb")  # dummy address/checksum
    n = send(radio, remote.profile(pkt_len=len(payload)), payload, 0.01)
    assert n >= 1
    assert radio.calls[0] == ("configure", 36)
    assert radio.calls.count(("fifo", 0x3F, payload)) == n
    assert radio.calls.count(("strobe", 0x35)) == n


def fake_commands():
    """g1..g4 with on/off packets shaped like the real ones (bit 7 of byte 8 = 0 means on)."""
    commands = {}
    for i, g in enumerate(("g1", "g2", "g3", "g4")):
        base = bytearray(b"\xa0\xa1\xa2\xa3\xa4\xa5\xa6\xa7" + bytes(28))  # dummy address
        base[9] = i
        on, off = bytearray(base), bytearray(base)
        on[8], off[8] = 0x3F - 4 * i, 0xBF - 4 * i
        commands[g] = {"on": bytes(on), "off": bytes(off)}
    return commands


def test_load_commands_labels_on_and_off_whatever_the_order(tmp_path):
    from kitchenlights.lights import load_commands

    cmds = fake_commands()
    buttons = {g: {"variants": [c["off"].hex(), c["on"].hex()] if g in ("g2", "g4") else
                   [c["on"].hex(), c["off"].hex()], "presses": [1, 2]} for g, c in cmds.items()}
    path = tmp_path / "buttons.json"
    path.write_text(json.dumps(buttons))
    assert load_commands(path) == cmds


class FakeLightsRadio:
    """Records transmitted packets; hands out one queued received packet at a time."""

    def __init__(self):
        self.sent = []
        self.inbox = []
        self.state = 0x01

    def configure(self, profile):
        pass

    def idle(self):
        pass

    def strobe(self, cmd):
        pass

    def write_burst(self, addr, values):
        self.sent.append(bytes(values))

    def marcstate(self):
        return 0x01

    def read_status(self, addr):
        return 0

    def flush_rx(self):
        pass

    def rx(self):
        pass

    def rx_bytes(self):
        return 38 if self.inbox else 0

    def read_fifo(self, n):
        return list(self.inbox.pop(0)) + [0x40, 0x7F]


def test_lights_send_and_remember(tmp_path, monkeypatch):
    from kitchenlights import lights as lights_mod
    from kitchenlights.lights import Lights

    monkeypatch.setattr(lights_mod, "SEND_SECONDS", 0.0)
    cmds = fake_commands()
    radio = FakeLightsRadio()
    lights = Lights(radio, cmds, tmp_path / "lights.json")
    lights.set(("g3", "g4"), True)
    assert radio.sent == [cmds["g3"]["on"], cmds["g4"]["on"]]
    assert lights.state == {"g1": False, "g2": False, "g3": True, "g4": True}
    assert Lights(radio, cmds, tmp_path / "lights.json").state["g4"] is True  # survives a restart


def test_lights_follow_the_remote():
    from kitchenlights.lights import Lights

    cmds = fake_commands()
    radio = FakeLightsRadio()
    lights = Lights(radio, cmds)
    seen = []
    lights.on_change = lambda g, on: seen.append((g, on))
    lights.poll()  # first poll puts the radio in RX
    radio.inbox = [cmds["g2"]["on"], cmds["g2"]["on"], b"\x00" * 36]
    assert lights.poll() == ("g2", True)
    assert lights.poll() == ("g2", True)  # repeats of a press don't notify again
    assert lights.poll() is None  # unknown packet
    assert seen == [("g2", True)] and lights.state["g2"] is True


def test_homekit_switches(tmp_path, monkeypatch):
    pytest = __import__("pytest")
    pytest.importorskip("pyhap")
    from pyhap.accessory_driver import AccessoryDriver

    from kitchenlights import lights as lights_mod
    from kitchenlights.homekit import build
    from kitchenlights.lights import Lights

    monkeypatch.setattr(lights_mod, "SEND_SECONDS", 0.0)
    cmds = fake_commands()
    radio = FakeLightsRadio()
    lights = Lights(radio, cmds)
    driver = AccessoryDriver(address="127.0.0.1", port=51899, persist_file=str(tmp_path / "acc.state"),
                             advertise=False)
    monkeypatch.setattr(driver, "add_job", lambda target, *args: target(*args))
    bridge = build(driver, lights)
    top, bottom = (bridge.accessories[aid] for aid in sorted(bridge.accessories))
    assert (top.display_name, bottom.display_name) == ("Top", "Bottom")
    from pyhap.const import CATEGORY_LIGHTBULB
    for accessory in (top, bottom):  # lights, so Siri includes them in "the kitchen lights"
        assert accessory.category == CATEGORY_LIGHTBULB
        assert accessory.get_service("Lightbulb") is not None and accessory.get_service("Switch") is None

    top.char_on.client_update_value(True)  # Apple Home switches Top on
    assert radio.sent == [cmds["g3"]["on"], cmds["g4"]["on"]]

    lights.poll()
    radio.inbox = [cmds["g1"]["on"]]  # someone uses the remote
    lights.poll()
    assert bottom.char_on.value is True and top.char_on.value is True
    radio.inbox = [cmds["g3"]["off"]]
    lights.poll()
    assert top.char_on.value is True  # g4 is still on
    radio.inbox = [cmds["g4"]["off"]]
    lights.poll()
    assert top.char_on.value is False


def test_listener_gives_up_when_the_radio_is_gone(monkeypatch):
    from kitchenlights import lights as lights_mod
    from kitchenlights.lights import Lights

    class DeadRadio(FakeLightsRadio):
        def configure(self, profile):
            raise OSError("device disconnected")

    monkeypatch.setattr(lights_mod.time, "sleep", lambda s: None)
    lights = Lights(DeadRadio(), fake_commands())
    called = []
    lights.on_fatal = lambda: called.append(True)
    lights.listen_forever(max_failures=3)  # returns instead of spinning forever
    assert called == [True]


def test_timing_finds_bursts_and_gaps():
    from tools.timing import bursts

    samples = []
    for i in range(400):  # 0.7 ms per sample, a 1.4 ms packet every 35 ms
        t = i * 0.7
        on = (t % 35.0) < 1.4
        samples.append((t, -40.0 if on else -92.0 + (i % 3)))
    found = bursts(samples)
    starts = [s for s, _ in found]
    assert len(found) == 8
    assert all(abs((b - a) - 35.0) < 1.0 for a, b in zip(starts, starts[1:]))


def test_record_length_reveals_longer_packets():
    from tools.record import summarise

    rng = random.Random(4)
    long_packet = bytes(rng.randrange(256) for _ in range(48))  # longer than the usual 36
    per_press = [long_packet + bytes(rng.randrange(256) for _ in range(12)) for _ in range(6)]
    assert summarise(per_press)["variants"] == [long_packet.hex()]


def test_dial_positions_from_both_directions():
    from tools.dial import positions

    head = "b0b1b2b3b4b5b6b7f0f1f2f3"  # dummy 12-byte constant start
    pos = [head + f"{i:02x}" * 12 for i in range(10)]
    noise = "ee" * 12
    rows = [{"button": "dial-cw", "hex": p + noise} for p in pos for _ in range(3)]
    rows.insert(5, {"button": "dial-cw", "hex": head + "77" * 12 + noise})  # corrupted, one direction only
    rows += [{"button": "dial-ccw", "hex": p + noise} for p in reversed(pos)]
    order, mirrored = positions(rows)
    assert order == pos and mirrored
