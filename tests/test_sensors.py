"""Register-level checks of the sensor models against what their Linux drivers expect.
Run: python3 tests/test_sensors.py"""
import math, os, struct, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import sensors

bus = sensors.SensorBus()
opt, icm, mag = bus.chips[0x44], bus.chips[0x69], bus.chips[0x1E]

def read(chip, reg, n):
    chip.i2c_write(bytes([reg]))
    return chip.i2c_read(n)

# OPT3001: IDs "TI" / 0x3001, conversion ready, lux = 0.01 × 2^E × M
assert read(opt, 0x7E, 2) == b"TI" and read(opt, 0x7F, 2) == b"\x30\x01"
assert struct.unpack(">H", read(opt, 0x01, 2))[0] & 0x0080
for lux in (0.5, 300, 40000):
    bus.ctl("light", [str(lux)])
    r = struct.unpack(">H", read(opt, 0x00, 2))[0]
    assert math.isclose(0.01 * 2 ** (r >> 12) * (r & 0xFFF), lux, rel_tol=0.01), lux

# ICM-42605: WHO_AM_I, reset done, accel/gyro scaled by the full-scale range the driver set
assert read(icm, 0x75, 1) == b"\x42" and read(icm, 0x2D, 1)[0] & 0x10
icm.i2c_write(bytes([0x50, 0x60]))  # ACCEL_CONFIG0: ±2 g
# Signs as iio-sensor-proxy reads them: flat screen-up "face-up", upright "normal", right edge down "left-up"
assert struct.unpack(">h", read(icm, 0x23, 2))[0] == 16384  # flat, screen up: z = +1 g
bus.ctl("tilt", ["90", "0"])
assert struct.unpack(">h", read(icm, 0x21, 2))[0] == -16384  # upright: y = -1 g
bus.ctl("tilt", ["0", "90"])
assert struct.unpack(">h", read(icm, 0x1F, 2))[0] == 16384  # right edge down: x = +1 g
icm.i2c_write(bytes([0x4F, 0x00]))  # GYRO_CONFIG0: ±2000 °/s
bus.ctl("gyro", ["0", "0", "1000"])
assert struct.unpack(">h", read(icm, 0x29, 2))[0] == 16384

# LIS2MDL: WHO_AM_I, 1.5 mG per bit, heading turns the horizontal field
assert read(mag, 0x4F, 1) == b"\x40"
assert struct.unpack("<h", read(mag, 0x6A, 2))[0] == 133  # 20 µT north along y
bus.ctl("heading", ["90"])
x, y = struct.unpack("<hh", read(mag, 0x68, 4))
assert x == -133 and abs(y) <= 1, (x, y)  # facing east: north is to the left
print("ok")
