"""The connector's SPI, PWM and I2S pins: the DLN-2 bridge's SPI commands, as spi-dln2 sends them,
the PWM interface's requests, as comet-board sends them, and isochronous audio over USB/IP, as
vhci-hcd sends it, reaching an extension model (the example's 74HC595 LED bar, PWM backlight and
I2S speaker and mic). Run: python3 tests/test_connector.py"""
import importlib.util, os, socket, struct, sys, threading, time
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

# I2S: isochronous URBs over USB/IP, as vhci-hcd sends them; each completes in real time.
pad = example.Extension()
i2s = connector.I2s(lambda: pad)
cfg = i2s.descriptor(2, 0, 0)
assert cfg[4] == 3 and struct.unpack_from("<H", cfg, 2)[0] == len(cfg)  # three interfaces, all of it counted
guest, host = socket.socketpair()
guest.settimeout(5)
threading.Thread(target=connector.Connection(host, i2s).serve, daemon=True).start()


def submit(seqnum, direction, epnum, packets, data=b""):
    """CMD_SUBMIT of an isochronous URB with packets of the given sizes; returns (header, data, descriptors)."""
    sizes = list(packets)
    guest.sendall(struct.pack(">IIIII Iiiii 8x", 1, seqnum, 0, direction, epnum, 0, sum(sizes), 0, len(sizes), 1)
                  + data + b"".join(struct.pack(">IIII", sum(sizes[:i]), n, 0, 0) for i, n in enumerate(sizes)))


def reply(direction):
    hdr = connector.recv_exact(guest, 48)
    _, seqnum, _, _, _, status, actual, _, npackets, _ = struct.unpack(">IIIII iiiii", hdr[:40])
    data = connector.recv_exact(guest, actual) if actual and direction else b""  # IN replies carry data
    descs = [struct.unpack(">IIII", connector.recv_exact(guest, 16)) for _ in range(npackets)]
    return seqnum, status, actual, data, descs


tone = struct.pack("<hh", 16384, -16384) * 48 * 8  # 8 ms of a full-scale-half square wave
start = time.monotonic()
submit(1, 0, 1, [192] * 8, tone)    # playback: 8 frames
submit(2, 0, 1, [192] * 8, tone)    # and 8 more, queued behind them
first, second = reply(0), reply(0)
took = time.monotonic() - start
assert first[:3] == (1, 0, 192 * 8) and second[0] == 2 and all(d[2] == 192 for d in first[4]), first
assert 0.014 <= took < 0.1, took    # 16 ms of audio took 16 ms: the guest's playback clock
assert pad.heard == 16 * 48 and pad.loudest == 16384 and i2s.played == 16 * 48
pad.command(["mic", "50"])
submit(3, 1, 2, [200] * 4)          # capture: the guest asks for up to 200 bytes per frame
seqnum, status, actual, data, descs = reply(1)
assert (seqnum, status, actual) == (3, 0, 192 * 4) and [d[2] for d in descs] == [192] * 4
samples = [s for (s,) in struct.iter_unpack("<h", data)]
assert 16000 < max(samples) <= 16384 and samples[0::2] == samples[1::2]  # the mic's 440 Hz tone, both channels
print("ok")
