"""The Comet IO Breakout extension: the 40 connector pins on 2.54 mm headers, plus a CP2102
USB-to-UART chip whose USB-C port goes to your computer.

In the Virtual Comet the CP2102 is /dev/ttyUSB0 (the guest's own cp210x driver), and the other end of
that UART, the breakout's USB-C port, is a terminal on your machine:

  mechumu ext uart             # prints its path; open it with e.g. `screen <path> 115200`

The header's pins are the connector's simulated I2C, GPIO, ADC, UART and CAN: drive them from the
host with `mechumu ctl gpio` / `adc`, or write a model of what you'd wire to them (extensions/example.py).
"""
import os, struct, threading, tty
from connector import Device, ep, iface


class Extension(Device):
    name = "io-breakout"
    vid, pid, bcd = 0x10C4, 0xEA60, 0x0100  # Silicon Labs CP210x
    manufacturer, product, serial = "Silicon Labs", "CP2102 USB to UART Bridge Controller", "mechumu-io"
    interfaces = [iface(0xFF, eps=[ep(0x81, "bulk", 64), ep(0x01, "bulk", 64)])]

    def __init__(self):
        super().__init__()
        self.line_ctl = 0x0800  # 8N1
        self.baud = 115200
        self.master, slave = os.openpty()
        tty.setraw(slave)  # no echo: guest output must not bounce back into the guest
        self.tty = os.ttyname(slave)  # keep slave open so the pty survives terminal programs closing it
        threading.Thread(target=self.pump, daemon=True).start()

    def pump(self):
        """Host terminal -> guest: bytes typed into the pty arrive on /dev/ttyUSB0."""
        while True:
            try:
                data = os.read(self.master, 64)
            except OSError:
                return
            self.send(0x81, data)

    def out(self, ep, data):
        """Guest -> host terminal."""
        os.write(self.master, data)

    def control(self, setup, data):
        rtype, req, value, _, length = struct.unpack("<BBHHH", setup)
        if rtype == 0xC0 and req == 0xFF and value == 0x370B:  # VENDOR_SPECIFIC / GET_PARTNUM
            return b"\x02"  # CP2102
        if rtype == 0x41:  # host to interface: settings writes
            if req == 0x03:  # SET_LINE_CTL
                self.line_ctl = value
            elif req == 0x1E and len(data) == 4:  # SET_BAUDRATE
                (self.baud,) = struct.unpack("<I", data)
            return b""
        if rtype == 0xC1:  # interface to host: settings reads
            if req == 0x04:  # GET_LINE_CTL
                return struct.pack("<H", self.line_ctl)[:length]
            if req == 0x1D:  # GET_BAUDRATE
                return struct.pack("<I", self.baud)[:length]
            return bytes(length)  # modem status, comm status, flow control: all idle
        if rtype == 0x40:  # vendor writes (GPIO latch etc.)
            return b""
        if rtype == 0xC0:
            return bytes(length)
        return None

    def command(self, args):
        if args == ["uart"]:
            return f"breakout UART at {self.tty} ({self.baud} baud); in the Virtual Comet it is /dev/ttyUSB0"
        raise ValueError("commands: uart")
