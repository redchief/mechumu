"""The Comet Gamepad extension: an RP2040 that shows up on the connector's USB pins as a HID gamepad.

  mechumu ext press a          # tap a button: a b x y, l r (the two small buttons), star, thumb
  mechumu ext hold a / release a
  mechumu ext dpad up          # up, down, left, right, none
  mechumu ext stick 0 -127     # analog stick x y, -127..127
"""
import struct, threading
from connector import HidDevice

BUTTONS = ["a", "b", "x", "y", "l", "r", "star", "thumb"]
HAT = {"up": 0, "right": 2, "down": 4, "left": 6, "none": 8}


class Extension(HidDevice):
    name = "gamepad"
    vid, pid = 0x2E8A, 0x000A  # Raspberry Pi RP2040
    manufacturer, product, serial = "Mecha", "Comet Gamepad", "mechumu-gamepad"
    report_size = 4
    # Report: 8 buttons, hat switch (4 bits) + padding, stick X and Y (signed bytes).
    report_descriptor = bytes.fromhex(
        "05 01 09 05 a1 01"                                     # Generic Desktop / Game Pad / Application
        "05 09 19 01 29 08 15 00 25 01 75 01 95 08 81 02"       # buttons 1-8
        "05 01 09 39 15 00 25 07 35 00 46 3b 01 65 14 75 04 95 01 81 42"  # hat switch, null state
        "65 00 75 04 95 01 81 03"                               # padding
        "09 30 09 31 15 81 25 7f 75 08 95 02 81 02"             # X, Y
        "c0")

    def __init__(self):
        super().__init__()
        self.buttons, self.hat, self.x, self.y = 0, 8, 0, 0
        self.state_lock = threading.Lock()

    def push(self):
        self.report(struct.pack("<BBbb", self.buttons, self.hat, self.x, self.y))

    def command(self, args):
        cmd, *rest = args
        with self.state_lock:
            if cmd in ("press", "hold", "release") and len(rest) == 1 and rest[0] in BUTTONS:
                bit = 1 << BUTTONS.index(rest[0])
                self.buttons = self.buttons & ~bit if cmd == "release" else self.buttons | bit
                self.push()
                if cmd == "press":
                    self.buttons &= ~bit
                    self.push()
                return f"{rest[0]} " + {"press": "pressed", "hold": "held", "release": "released"}[cmd]
            if cmd == "dpad" and len(rest) == 1 and rest[0] in HAT:
                self.hat = HAT[rest[0]]
                self.push()
                return f"dpad {rest[0]}"
            if cmd == "stick" and len(rest) == 2:
                self.x, self.y = (max(-127, min(127, int(v))) for v in rest)
                self.push()
                return f"stick {self.x} {self.y}"
        raise ValueError(f"commands: press|hold|release <{'|'.join(BUTTONS)}> | dpad <up|down|left|right|none> | stick <x> <y>")
