"""A from-scratch extension: copy this file and make it yours.

The Comet's 40-pin connector gives an extension USB, I2C, UART, SPI, CAN, PWM, ADC and GPIO. In
the Virtual Comet a model like this one plays the extension: its USB device is plugged into the
guest's USB stack and the connector's I2C transfers are answered by its i2c_* methods, so the
guest's own drivers and your apps talk to it exactly as they would to the hardware.

This one is a macro pad (four keys, a USB HID keyboard) with a TMP102 temperature sensor on the
connector's I2C bus, an LED on GPIO1, a knob (potentiometer) on ADC1, a debug console on UART1,
an 8-LED bar on SPI (a 74HC595 shift register), a key backlight dimmed by PWM3, and a speaker
amplifier and microphone on I2S (which gives the Comet a "Comet SAI2" sound card).
Try it:

  mechumu ext attach extensions/example.py
  mechumu ext press 2                   # the pad's second key types F14
  mechumu ext temp 31.5                 # what the sensor will read
  mechumu ext knob 2.2                  # turn the knob: ADC1 reads 2.2 V
  mechumu ext led                       # is the LED on?
  mechumu ctl spi 81                    # the Comet lights the bar's end LEDs...
  mechumu ext bar                       # ...and here they are
  mechumu ctl pwm 3 1000 25             # the Comet dims the backlight to 25%...
  mechumu ext backlight                 # ...which the pad sees
  mechumu ctl i2s play                  # the Comet plays a tone on I2S...
  mechumu ext speaker                   # ...which the amplifier heard
  mechumu ext mic 30                    # a 440 Hz tone at 30% on the mic

and in the Virtual Comet (ssh -p 2222 root@localhost):

  echo tmp102 0x48 > /sys/class/i2c-dev/$(basename $(readlink /dev/i2c-comet-ext))/device/new_device
  cat /sys/class/hwmon/hwmon*/temp1_input       # 31500: millidegrees, from this model
  gpioset -c gpiochip-comet-ext -t 0 0=1        # light the LED (GPIO1 is line 0)
  cat /sys/bus/iio/devices/iio:device0/in_voltage0_raw   # the knob, 0-1023
  echo hi > /dev/comet-uart1                    # the console answers on UART1
  printf '\xff' > /dev/spidev-comet-ext          # light the whole LED bar (any spidev app works)
  ls /sys/bus/usb/drivers/comet-pwm/*/pwm       # two pwmchips: PWM3, then PWM4; use them via sysfs
  aplay -l                                      # the "Comet SAI2" card: any ALSA or PipeWire app

`mechumu ext attach` reloads the file, so edit, re-attach, repeat. Errors in your model show up in
out/hub.log.
"""
import math, struct
from connector import HidDevice


class Extension(HidDevice):
    # The name `mechumu ext status` shows.
    name = "macro-pad"

    # USB identity. 0x1209 is pid.codes, which hands out free product IDs to open hardware;
    # use your own VID/PID once you have them. serial becomes the input device's unique ID,
    # which mechanix's extensions service matches against its config.toml.
    vid, pid = 0x1209, 0x0001
    manufacturer, product, serial = "You", "Macro Pad", "macro-pad-0001"

    # A HID report descriptor says what the reports mean. This is a plain keyboard report
    # (modifier byte, reserved byte, six key slots), so Linux's usbhid binds with no driver work.
    hid_proto = 1
    report_size = 8
    report_descriptor = bytes.fromhex(
        "05 01 09 06 a1 01"
        "05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02"
        "95 01 75 08 81 03"
        "95 06 75 08 15 00 25 73 05 07 19 00 29 73 81 00"
        "c0")
    KEYS = {"1": 0x69, "2": 0x6A, "3": 0x6B, "4": 0x6C}  # F14-F17: keys no other device sends

    TMP102 = 0x48  # the sensor's I2C address (its ADD0 pin tied to ground)

    def __init__(self):
        super().__init__()
        self.led, self.knob = 0, 1.0
        self.reg, self.temp = 0, 25.0
        self.shift, self.bar = 0, 0  # the 74HC595's shift register and its latched outputs
        self.backlight = 0.0         # brightness, 0-1
        self.heard, self.loudest = 0, 0  # audio frames the amplifier got, and their peak (0-32767)
        self.mic, self.mic_phase = 0.0, 0  # mic tone level (0-1) and where its sine wave is
        # TMP102 registers (datasheet SBOS397): 0 temperature, 1 configuration, 2/3 alert limits.
        self.regs = {1: b"\x60\xa0", 2: self.celsius(75.0), 3: self.celsius(80.0)}

    def celsius(self, t):
        """Temperature register format: 0.0625 °C steps, left-aligned; 12 bits, or 13 in extended
        mode (config bit EM, which Linux's tmp102 driver turns on)."""
        extended = self.regs[1][1] & 0x10 if hasattr(self, "regs") else 0
        return struct.pack(">h", round(t / 0.0625) << (3 if extended else 4))

    # --- the connector's I2C pins --------------------------------------------------------------
    # Return True to ACK a write, bytes to answer a read, or fall back to the base class, which
    # answers mechanix's extension-detection probe at 0x16.
    def i2c_write(self, addr, data):
        if addr != self.TMP102:
            return super().i2c_write(addr, data)
        if data:
            self.reg = data[0] & 3  # first byte selects a register...
        if len(data) == 3 and self.reg != 0:
            self.regs[self.reg] = data[1:3]  # ...and two more bytes write it (temperature is read-only)
        return True

    def i2c_read(self, addr, n):
        if addr != self.TMP102:
            return super().i2c_read(addr, n)
        return (self.celsius(self.temp) if self.reg == 0 else self.regs[self.reg])[:n]

    # --- the connector's GPIO, ADC and UART pins ----------------------------------------------
    def on_gpio(self, n, value):
        """The Comet drove GPIO<n> as an output: GPIO1 is our LED."""
        if n == 1:
            self.led = value

    def adc(self, n):
        """The Comet samples ADC<n>: ADC1 is the knob's wiper, 0-3.3 V."""
        return self.knob if n == 1 else None

    def on_uart(self, n, data):
        """Bytes from the Comet on UART<n>: a tiny console that echoes lines back on UART1."""
        if n == 1:
            self.uart_send(1, b"macro-pad: " + data)

    def on_pwm(self, n, period_ns, duty_ns, enabled):
        """The Comet set PWM<n>: PWM3 drives the backlight LEDs, brightness = duty cycle."""
        if n == 3:
            self.backlight = duty_ns / period_ns if enabled and period_ns else 0.0

    # --- the connector's I2S pins: 48 kHz, 16-bit stereo, 4 bytes per frame ------------------
    i2s = True

    def on_i2s(self, frames):
        """The Comet played audio on TXD: the amplifier drives the speaker with it."""
        self.heard += len(frames) // 4
        self.loudest = max([self.loudest] + [abs(s) for (s,) in struct.iter_unpack("<h", frames)])

    def i2s_capture(self, nbytes):
        """The Comet records RXD: the microphone hears a 440 Hz tone at self.mic."""
        out = b""
        for _ in range(nbytes // 4):
            s = round(32767 * self.mic * math.sin(2 * math.pi * 440 * self.mic_phase / 48000))
            out += struct.pack("<hh", s, s)
            self.mic_phase = (self.mic_phase + 1) % 48000
        return out

    # --- the connector's SPI pins ------------------------------------------------------------
    def spi_transfer(self, data):
        """Bits shift in on MOSI; what falls out the far end (QH', wired to MISO) is the byte
        clocked in before, so the Comet can read back what it last wrote."""
        miso = bytes([self.shift]) + bytes(data[:-1])
        self.shift = data[-1]
        return miso

    def spi_deselect(self):
        """Chip select (wired to the latch clock) going high puts the shifted byte on the LEDs."""
        self.bar = self.shift

    # --- driving it from the host: `mechumu ext <command...>` --------------------------------
    def command(self, args):
        cmd, *rest = args
        if cmd == "press" and len(rest) == 1 and rest[0] in self.KEYS:
            self.report([0, 0, self.KEYS[rest[0]], 0, 0, 0, 0, 0])  # key down
            self.report([0] * 8)                                     # key up
            return f"key {rest[0]} pressed"
        if cmd == "temp" and len(rest) == 1:
            self.temp = float(rest[0])
            return f"sensor now reads {float(rest[0]):g} °C"
        if cmd == "knob" and len(rest) == 1 and 0 <= float(rest[0]) <= 3.3:
            self.knob = float(rest[0])
            return f"knob at {self.knob:g} V"
        if cmd == "led" and not rest:
            return "LED on" if self.led else "LED off"
        if cmd == "bar" and not rest:  # LED 1 is the 595's QA output, the last bit shifted in
            return "LED bar " + "".join("#" if self.bar >> i & 1 else "." for i in range(8))
        if cmd == "backlight" and not rest:
            return f"backlight {self.backlight:.0%}"
        if cmd == "speaker" and not rest:
            return f"speaker heard {self.heard / 48000:.2f} s, peak {self.loudest / 32768:.0%}"
        if cmd == "mic" and len(rest) == 1 and 0 <= float(rest[0]) <= 100:
            self.mic = float(rest[0]) / 100
            return f"mic tone at {float(rest[0]):g}%"
        raise ValueError("commands: press <1-4> | temp <celsius> | knob <0-3.3> | led | bar | backlight"
                         " | speaker | mic <0-100>")
