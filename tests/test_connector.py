"""The connector's SPI and PWM pins: the DLN-2 bridge's SPI commands, as spi-dln2 sends them, and
the PWM interface's requests, as comet-board sends them, reaching an extension model (the example's
74HC595 LED bar and PWM backlight). Run: python3 tests/test_connector.py"""
import importlib.util, os, struct, sys
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, "..", "lib"))
import connector

spec = importlib.util.spec_from_file_location("example", os.path.join(here, "..", "extensions", "example.py"))
example = importlib.util.module_from_spec(spec)
spec.loader.exec_module(example)


def bridge(ext):
    b, sent = connector.Bridge(lambda: ext), []
    b.send = lambda ep, data: sent.append(data)

    def cmd(op, payload):  # one request (header: size, cmd, echo, handle); returns the reply after its result code
        b.out(0x01, struct.pack("<HHHH", 8 + len(payload), 0x0200 | op, 7, 1) + payload)
        reply = sent.pop()
        assert reply[8:10] == b"\0\0", reply
        return reply[10:]
    return cmd


def xfer(op, data, attr=0):  # READ_WRITE 0x1A / READ 0x1B / WRITE 0x1C: port, size, attr, data
    return struct.pack("<BHB", 0, len(data), attr) + (data if op != 0x1B else b"")


pad = example.Extension()
spi = bridge(pad)
assert spi(0x18, b"\0" + struct.pack("<I", 500_000)) == struct.pack("<I", 500_000)  # SET_FREQUENCY echoes
assert spi(0x1A, xfer(0x1A, b"\xa5")) == b"\x01\x00\x00"                     # MISO: what was shifted in before
assert spi(0x1A, xfer(0x1A, b"\x3c\x81")) == b"\x02\x00\xa5\x3c"
assert pad.command(["bar"]) == "LED bar #......#"                           # latched on chip select release
assert spi(0x1C, xfer(0x1C, b"\xff", attr=1)) == b"" and pad.bar == 0x81    # LEAVE_SS_LOW: no latch yet
assert spi(0x1B, xfer(0x1B, b"\0")) == b"\x01\x00\xff" and pad.bar == 0     # a read clocks out zeros
assert bridge(None)(0x1A, xfer(0x1A, b"\x01")) == b"\x01\x00\x00"           # nothing attached: MISO reads zeros

# PWM: SET_PWM to interface 1, wValue 0 = PWM3; period ns, duty ns, inversed, enabled
b = connector.Bridge(lambda: pad)
set_pwm = lambda pin, *v: b._control(struct.pack("<BBHHH", 0x41, 1, pin, 1, 18), struct.pack("<QQBB", *v))
assert set_pwm(0, 1_000_000, 250_000, 0, 1) == b"" and pad.backlight == 0.25 and b.pwm[3] == (1_000_000, 250_000, True)
assert set_pwm(0, 1_000_000, 250_000, 1, 1) == b"" and pad.backlight == 0.75   # inversed: high the rest of the time
assert set_pwm(1, 500, 100, 0, 0) == b"" and pad.backlight == 0.75 and b.pwm[4] == (500, 100, False)  # PWM4: not ours
assert set_pwm(0, 1_000_000, 0, 0, 0) == b"" and pad.backlight == 0.0
assert set_pwm(2, 1, 1, 0, 1) is None                                           # no such pin: stall
assert len(connector.Bridge(lambda: None).interfaces) == 2
print("ok")
