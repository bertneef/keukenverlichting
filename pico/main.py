# MicroPython firmware for a Raspberry Pi Pico: USB serial <-> CC2500 SPI bridge.
# Frame: [length][bytes] in, the same number of bytes out. Length 0 = ping -> 'K',
# length 255 = stop the bridge and return to the REPL (needed to update this file).
#
# Wiring (Pico pin numbers): 3V3(OUT) 36, GND 38, SCK GP18 (24), MOSI GP19 (25),
# MISO GP16 (21), CSN GP17 (22).
import sys

import micropython
from machine import SPI, Pin

micropython.kbd_intr(-1)  # byte 0x03 is data here, not Ctrl-C

spi = SPI(0, baudrate=1_000_000, polarity=0, phase=0, sck=Pin(18), mosi=Pin(19), miso=Pin(16))
cs = Pin(17, Pin.OUT, value=1)
led = Pin("LED", Pin.OUT, value=1)
stdin, stdout = sys.stdin.buffer, sys.stdout.buffer


def read_exact(n):
    data = b""
    while len(data) < n:
        data += stdin.read(n - len(data))
    return bytearray(data)


while True:
    n = stdin.read(1)[0]
    if n == 0:
        stdout.write(b"K")
        continue
    if n == 255:
        micropython.kbd_intr(3)
        led.value(0)
        break
    data = read_exact(n)
    cs.value(0)
    spi.write_readinto(data, data)
    cs.value(1)
    stdout.write(data)
