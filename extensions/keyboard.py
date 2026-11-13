"""The Comet Keyboard extension: a QWERTY keyboard with ZMK firmware, a USB HID keyboard to Linux.

  mechumu ext type "hello, comet"     # type text (US layout)
  mechumu ext press ctrl c            # press a chord, then release it
  mechumu ext press enter
"""
from connector import HidDevice

# HID keyboard usages (USB HID Usage Tables, page 0x07)
KEYS = {**{chr(ord("a") + i): 0x04 + i for i in range(26)},
        **{str((i + 1) % 10): 0x1E + i for i in range(10)},
        "enter": 0x28, "esc": 0x29, "backspace": 0x2A, "del": 0x2A, "tab": 0x2B, "space": 0x2C,
        "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31, ";": 0x33, "'": 0x34, "`": 0x35,
        ",": 0x36, ".": 0x37, "/": 0x38, "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
        **{f"f{i}": 0x39 + i for i in range(1, 13)}}
MODIFIERS = {"ctrl": 0x01, "shift": 0x02, "alt": 0x04, "super": 0x08}
SHIFTED = dict(zip('!@#$%^&*()_+{}|:"~<>?', "1234567890-=[]\\;'`,./"))


def chord_for(ch):
    """(modifier bits, usage) that types character ch."""
    if ch == " ":
        return 0, KEYS["space"]
    if ch == "\n":
        return 0, KEYS["enter"]
    if ch.isupper():
        return MODIFIERS["shift"], KEYS[ch.lower()]
    if ch in SHIFTED:
        return MODIFIERS["shift"], KEYS[SHIFTED[ch]]
    if ch in KEYS:
        return 0, KEYS[ch]
    raise ValueError(f"can't type {ch!r} on a US layout")


class Extension(HidDevice):
    name = "keyboard"
    vid, pid = 0x1D50, 0x615E  # ZMK's USB IDs
    manufacturer, product, serial = "Mecha", "Comet Keyboard", "mechumu-keyboard"
    hid_proto = 1  # boot keyboard
    report_size = 8
    # Boot-protocol keyboard: modifier byte, reserved byte, six key slots; LED output report.
    report_descriptor = bytes.fromhex(
        "05 01 09 06 a1 01"                                  # Generic Desktop / Keyboard / Application
        "05 07 19 e0 29 e7 15 00 25 01 75 01 95 08 81 02"    # 8 modifier bits
        "95 01 75 08 81 03"                                  # reserved byte
        "95 05 75 01 05 08 19 01 29 05 91 02"                # 5 LED outputs
        "95 01 75 03 91 03"                                  # LED padding
        "95 06 75 08 15 00 25 65 05 07 19 00 29 65 81 00"    # 6 key slots
        "c0")

    def tap(self, mods, usage):
        self.report([mods, 0, usage, 0, 0, 0, 0, 0])
        self.report([0] * 8)

    def command(self, args):
        cmd, *rest = args
        if cmd == "type" and rest:
            text = " ".join(rest)
            for ch in text:
                self.tap(*chord_for(ch))
            return f"typed {len(text)} characters"
        if cmd == "press" and rest:
            mods = sum(MODIFIERS.get(k, 0) for k in rest)
            keys = [KEYS[k] for k in rest if k not in MODIFIERS and k in KEYS]
            unknown = [k for k in rest if k not in MODIFIERS and k not in KEYS]
            if unknown or len(keys) > 6:
                raise ValueError(f"unknown keys {unknown}" if unknown else "at most 6 keys at once")
            self.report([mods, 0, *keys, *[0] * (6 - len(keys))])
            self.report([0] * 8)
            return "pressed " + "+".join(rest)
        if cmd == "report" and 1 <= len(rest) <= 7:  # the frame's live keys: modifier bits, then usages
            mods, *keys = (int(v) for v in rest)
            self.report([mods & 0xFF, 0, *keys, *[0] * (6 - len(keys))])
            return "sent"
        raise ValueError('commands: type <text> | press <key> [key...] (e.g. press ctrl c)')
