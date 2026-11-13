"""The Comet's onboard sensors, modelled at the register level for their mainline Linux drivers.

On a Comet they sit on the SoC's I2C3 bus (see imx8mp-mecha-comet.dts): an ICM-42605 IMU at 0x69,
an OPT3001 ambient light sensor at 0x44 and an LIS2MDL magnetometer at 0x1e. Here that bus is an
i2c-tiny-usb adapter the hub serves over USB/IP ("Comet board sensors"); the guest's comet-board
module registers the three devices on it, their stock drivers bind, and iio-sensor-proxy picks them
up as it would on a device. `mechumu ctl light|tilt|accel|gyro|heading|sensors` sets what they sense.
"""
import math, struct, threading
from connector import Device, iface

G = 9.80665


class Environment:
    """What the sensors sense: the physical state `mechumu ctl` changes. Axes and signs are what
    iio-sensor-proxy, which gives apps orientation, reads correctly (checked against it): upright
    y = -g, right edge down x = +g, flat with the screen up z = +g.
    shortcut: assumes the IMU and magnetometer are mounted with these axes; correct them per the Comet's layout once known."""

    def __init__(self):
        self.lux = 300.0                    # indoor light
        self.accel = [0.0, 0.0, G]          # m/s², lying flat, screen up
        self.gyro = [0.0, 0.0, 0.0]         # degrees/s
        self.field = [0.0, 20.0, -40.0]     # µT: top edge toward magnetic north, mid-latitude dip
        self.temp = 30.0                    # °C, the IMU's die temperature

    def tilt(self, pitch, roll):
        """Gravity for the device pitched (top edge up) and rolled (right edge down), in degrees."""
        p, r = math.radians(pitch), math.radians(roll)
        self.accel = [G * math.sin(r) * math.cos(p), -G * math.sin(p), G * math.cos(p) * math.cos(r)]

    def heading(self, degrees):
        """Turn the device so its top edge points `degrees` clockwise from magnetic north."""
        h = math.radians(degrees)
        horizontal = math.hypot(self.field[0], self.field[1]) or 20.0
        self.field[0], self.field[1] = -horizontal * math.sin(h), horizontal * math.cos(h)

    def describe(self):
        a, g, f = (" ".join(f"{v:.2f}" for v in vec) for vec in (self.accel, self.gyro, self.field))
        return f"light {self.lux:g} lux | accel {a} m/s² | gyro {g} °/s | field {f} µT | temp {self.temp:g} °C"


class Icm42605:
    """InvenSense ICM-42605 (inv_icm42600 driver): banked 8-bit registers, big-endian data."""
    ACCEL_FS_G = [16, 8, 4, 2]                            # ACCEL_CONFIG0[7:5]
    GYRO_FS_DPS = [2000 / 2 ** i for i in range(8)]       # GYRO_CONFIG0[7:5]

    def __init__(self, env):
        self.env, self.bank, self.ptr, self.regs = env, 0, 0, {}

    def data(self):
        def s16(v):
            return struct.pack(">h", max(-32767, min(32767, round(v))))
        afs = self.ACCEL_FS_G[self.regs.get(0x50, 0x06) >> 5 & 3]
        gfs = self.GYRO_FS_DPS[self.regs.get(0x4F, 0x06) >> 5 & 7]
        e = self.env
        return (s16((e.temp - 25) * 132.48)                                  # 0x1D TEMP_DATA
                + b"".join(s16(a / G / afs * 32768) for a in e.accel)        # 0x1F ACCEL_DATA_X/Y/Z
                + b"".join(s16(w / gfs * 32768) for w in e.gyro))            # 0x25 GYRO_DATA_X/Y/Z

    def read(self, reg):
        if self.bank == 0:
            if reg == 0x75:
                return 0x42             # WHO_AM_I: ICM-42605
            if reg == 0x2D:
                return 0x10             # INT_STATUS: reset done
            if 0x1D <= reg <= 0x2A:
                return self.data()[reg - 0x1D]
            if reg == 0x76:
                return self.bank
        return self.regs.get((self.bank << 8) | reg if self.bank else reg, 0)

    def write(self, reg, value):
        if reg == 0x76:
            self.bank = value & 7       # REG_BANK_SEL
        elif self.bank == 0 and reg == 0x11 and value & 1:
            self.regs = {}              # DEVICE_CONFIG soft reset
        else:
            self.regs[(self.bank << 8) | reg if self.bank else reg] = value

    def i2c_write(self, data):
        if data:
            self.ptr = data[0]
            for i, b in enumerate(data[1:]):
                self.write(self.ptr + i, b)
        return True

    def i2c_read(self, n):
        out = bytes(self.read(self.ptr + i) for i in range(n))
        self.ptr += n
        return out


class Opt3001:
    """TI OPT3001 (opt3001 driver): 16-bit big-endian registers, lux as exponent and mantissa."""

    def __init__(self, env):
        self.env, self.ptr = env, 0
        self.regs = {0x01: 0xC810, 0x02: 0x0000, 0x03: 0xBFFF, 0x7E: 0x5449, 0x7F: 0x3001}

    def result(self):
        m, e = round(self.env.lux / 0.01), 0  # lux = 0.01 × 2^e × m, m < 4096
        while m > 4095 and e < 11:
            e += 1
            m = round(self.env.lux / 0.01 / 2 ** e)
        return e << 12 | min(m, 4095)

    def read16(self, reg):
        if reg == 0x00:
            return self.result()
        if reg == 0x01:
            return self.regs[0x01] | 0x0080  # CRF: a conversion is always ready
        return self.regs.get(reg, 0)

    def i2c_write(self, data):
        if data:
            self.ptr = data[0]
            if len(data) >= 3:
                self.regs[self.ptr] = data[1] << 8 | data[2]
        return True

    def i2c_read(self, n):
        return struct.pack(">H", self.read16(self.ptr))[:n].ljust(n, b"\0")


class Lis2mdl:
    """ST LIS2MDL (st_magn driver): 8-bit registers, little-endian data at 1.5 mG per bit."""

    def __init__(self, env):
        self.env, self.ptr, self.regs = env, 0, {}

    def read(self, reg):
        if reg == 0x4F:
            return 0x40  # WHO_AM_I
        if 0x68 <= reg <= 0x6D:
            raw = b"".join(struct.pack("<h", max(-32767, min(32767, round(b * 10 / 1.5)))) for b in self.env.field)
            return raw[reg - 0x68]  # µT × 10 = mG
        return self.regs.get(reg, 0)

    def i2c_write(self, data):
        if data:
            self.ptr = data[0]
            for i, b in enumerate(data[1:]):
                self.regs[self.ptr + i] = b
        return True

    def i2c_read(self, n):
        out = bytes(self.read(self.ptr + i) for i in range(n))
        self.ptr += n
        return out


class SensorBus(Device):
    """The Comet's I2C3, as an i2c-tiny-usb adapter (bus ID 1-3) carrying the three sensors."""
    vid, pid, bcd = 0x0403, 0xC631, 0x0104
    manufacturer, product = "mechumu", "Comet board sensors"
    interfaces = [iface(0xFF)]

    def __init__(self):
        super().__init__()
        self.env = Environment()
        self.chips = {0x69: Icm42605(self.env), 0x44: Opt3001(self.env), 0x1E: Lis2mdl(self.env)}
        self.status, self.lock = 0, threading.Lock()

    def control(self, setup, data):
        rtype, req, value, addr, length = struct.unpack("<BBHHH", setup)
        if req == 1:  # CMD_GET_FUNC: I2C_FUNC_I2C | I2C_FUNC_SMBUS_EMUL
            return struct.pack("<I", 0x1 | 0x0EFF0008)
        if req in (0, 2):  # CMD_ECHO, CMD_SET_DELAY
            return b""
        if req == 3:  # CMD_GET_STATUS: 1 = address ACKed, 2 = NAKed
            return bytes([self.status])
        if req & ~3 == 4:  # CMD_I2C_IO (+ BEGIN/END flags)
            chip = self.chips.get(addr)
            with self.lock:
                if rtype & 0x80:
                    self.status = 1 if chip else 2
                    return (chip.i2c_read(length) if chip else b"").ljust(length, b"\0")[:length]
                self.status = 1 if chip and chip.i2c_write(bytes(data)) else 2
            return b""
        return None

    def ctl(self, cmd, args):
        """`mechumu ctl <cmd> <args>` for the sensors; None if cmd isn't a sensor command."""
        e = self.env
        nums = [float(a) for a in args]
        if cmd == "light" and len(nums) == 1 and nums[0] >= 0:
            e.lux = min(nums[0], 83865.6)  # the OPT3001's full scale
            return f"ok light {e.lux:g} lux"
        if cmd == "tilt" and len(nums) == 2:
            e.tilt(*nums)
            return f"ok tilted: pitch {nums[0]:g}°, roll {nums[1]:g}°"
        if cmd in ("accel", "gyro") and len(nums) == 3:
            setattr(e, cmd, nums)
            return f"ok {cmd} {' '.join(f'{v:g}' for v in nums)}"
        if cmd == "heading" and len(nums) == 1:
            e.heading(nums[0])
            return f"ok heading {nums[0]:g}°"
        if cmd == "sensors" and not args:
            return "ok " + e.describe()
        return None
