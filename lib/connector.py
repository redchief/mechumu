"""Virtual USB devices for the Comet's extension connector, served to the guest over USB/IP.

An extension model subclasses Device (or HidDevice) and describes itself with descriptors; the
guest's own kernel drivers (usbhid, cp210x, dln2, ...) bind to it exactly as they would to
the real extension on the connector's USB pins. See extensions/example.py for a from-scratch model.
"""
import queue, socket, struct, threading

# --- descriptors ------------------------------------------------------------------------------

def ep(addr, kind, size, interval=0):
    """One endpoint: addr 0x81 = IN 1, 0x02 = OUT 2; kind 'bulk' | 'int'."""
    return {"addr": addr, "kind": {"bulk": 2, "int": 3}[kind], "size": size, "interval": interval}


def iface(cls, sub=0, proto=0, eps=(), extra=b""):
    """One interface; extra goes between the interface and its endpoints (e.g. a HID descriptor)."""
    return {"cls": cls, "sub": sub, "proto": proto, "eps": list(eps), "extra": extra}


class Device:
    vid, pid, bcd = 0x1209, 0x0001, 0x0100  # 0x1209: pid.codes open-source vendor ID
    cls = sub = proto = 0
    manufacturer, product, serial = "mechumu", "Extension", ""
    speed = 2  # USB_SPEED_FULL, like the RP2040 and CP2102 on Mecha's extensions
    interfaces = []

    # Override for the extension's behaviour --------------------------------------------------
    def control(self, setup, data):
        """Class/vendor control request. Return bytes (IN data, or b"" to ack) or None to stall."""
        return None

    def out(self, ep, data):
        """Data the guest wrote to an OUT endpoint."""

    def command(self, args):
        """`mechumu ext <args>` while attached: drive the extension from the host. Returns a reply."""
        raise ValueError(f"{type(self).__name__} takes no commands")

    # The connector's other pins (see Bridge and the hub). Numbering follows the connector's
    # labels: GPIO1-5, ADC1-4, UART1-2.
    connector = None  # set by the hub while attached

    def on_gpio(self, n, value):
        """The Comet drove GPIO<n> (configured as an output) to value."""

    def set_gpio(self, n, value):
        """Drive GPIO<n> as the extension: the Comet reads it as an input."""
        if self.connector:
            self.connector.bridge.drive(n - 1, value)

    def adc(self, n):
        """Volts on ADC<n> when the Comet samples it (0-3.3), or None for the `mechumu ctl adc` value."""
        return None

    def on_uart(self, n, data):
        """The Comet sent bytes on UART<n>."""

    def uart_send(self, n, data):
        """Send bytes to the Comet on UART<n>."""
        if self.connector:
            self.connector.uart_send(n, data)

    # The connector's I2C pins: return True to ACK a write, bytes to answer a read.
    # By default an extension answers mechanix's detection probe: the extensions service writes an
    # 8-byte request to 0x16 and accepts any 8-byte reply framed 0xAA ... 0xFF.
    # shortcut: echoing the request is a guess (Mecha's extensions spec is unpublished); replace with the real reply once known.
    detect_addr = 0x16

    def i2c_write(self, addr, data):
        if addr == self.detect_addr:
            self._probe = data
            return True
        return False

    def i2c_read(self, addr, n):
        if addr == self.detect_addr and getattr(self, "_probe", None):
            return self._probe[:n]
        return None

    # Plumbing -----------------------------------------------------------------------------------
    def __init__(self):
        self._pending = {}  # IN endpoint -> queue of waiting (conn, seqnum, length)
        self._ready = {}    # IN endpoint -> queue of data the guest hasn't asked for yet
        self._lock = threading.Lock()

    def send(self, ep, data):
        """Deliver data on an IN endpoint (a HID report, serial bytes, ...)."""
        with self._lock:
            waiting = self._pending.setdefault(ep, queue.SimpleQueue())
            while not waiting.empty():
                conn, seqnum, length = waiting.get()
                if conn.complete(seqnum, 0, data[:length]):
                    return
            self._ready.setdefault(ep, queue.SimpleQueue()).put(data)

    def _in(self, conn, seqnum, ep, length):
        with self._lock:
            ready = self._ready.get(ep)
            if ready and not ready.empty():
                return conn.complete(seqnum, 0, ready.get()[:length])
            self._pending.setdefault(ep, queue.SimpleQueue()).put((conn, seqnum, length))

    def descriptor(self, kind, index, lang):
        if kind == 1:
            return struct.pack("<BBHBBBBHHHBBBB", 18, 1, 0x0200, self.cls, self.sub, self.proto, 64,
                               self.vid, self.pid, self.bcd, 1, 2, 3 if self.serial else 0, 1)
        if kind == 2:
            body = b""
            for n, i in enumerate(self.interfaces):
                body += struct.pack("<BBBBBBBBB", 9, 4, n, 0, len(i["eps"]), i["cls"], i["sub"], i["proto"], 0)
                body += i["extra"]
                for e in i["eps"]:
                    body += struct.pack("<BBBBHB", 7, 5, e["addr"], e["kind"], e["size"], e["interval"])
            return struct.pack("<BBHBBBBB", 9, 2, 9 + len(body), len(self.interfaces), 1, 0, 0x80, 50) + body
        if kind == 3:
            if index == 0:
                return b"\x04\x03\x09\x04"  # en-US
            text = {1: self.manufacturer, 2: self.product, 3: self.serial}.get(index)
            if text is None:
                return None
            raw = text.encode("utf-16-le")
            return bytes([2 + len(raw), 3]) + raw
        return None

    def _control(self, setup, data):
        rtype, req, value, index, length = struct.unpack("<BBHHH", setup)
        if rtype & 0x60 == 0:  # standard
            if req == 6:  # GET_DESCRIPTOR
                d = self.descriptor(value >> 8, value & 0xFF, index) if rtype == 0x80 else None
                if d is None:
                    d = self.control(setup, data)  # e.g. a HID report descriptor (recipient: interface)
                return None if d is None else d[:length]
            if req == 0:  # GET_STATUS
                return b"\x00\x00"[:length]
            if req == 8:  # GET_CONFIGURATION
                return b"\x01"[:length]
            if req in (1, 3, 5, 9, 11):  # CLEAR/SET_FEATURE, SET_ADDRESS, SET_CONFIGURATION, SET_INTERFACE
                return b""
            return None
        return self.control(setup, data)


class HidDevice(Device):
    """A USB HID device (keyboard, gamepad, ...): report descriptor in, reports out on endpoint 0x81."""
    report_descriptor = b""
    report_size = 8
    hid_proto = 0  # 1 keyboard, 2 mouse (boot protocol), 0 none

    def __init__(self):
        super().__init__()
        hid = struct.pack("<BBHBBBH", 9, 0x21, 0x0111, 0, 1, 0x22, len(self.report_descriptor))
        self.interfaces = [iface(3, 1 if self.hid_proto else 0, self.hid_proto,
                                 [ep(0x81, "int", max(8, self.report_size), 1)], hid)]

    def control(self, setup, data):
        rtype, req, value, _, _ = struct.unpack("<BBHHH", setup)
        if rtype == 0x81 and req == 6 and value >> 8 == 0x22:
            return self.report_descriptor
        if rtype == 0x21 and req == 9:  # SET_REPORT: keyboard LEDs and the like
            self.on_output(data)
            return b""
        if rtype == 0x21 and req in (0x0A, 0x0B):  # SET_IDLE, SET_PROTOCOL
            return b""
        if rtype == 0xA1 and req == 1:  # GET_REPORT
            return bytes(self.report_size)
        return None

    def on_output(self, data):
        pass

    def report(self, data):
        self.send(0x81, bytes(data))


class Bridge(Device):
    """The connector's low-speed pins: I2C, GPIO1-5 and ADC1-4, as a Diolan DLN-2 USB adapter, whose
    mainline drivers (dln2, i2c-dln2, gpio-dln2, dln2-adc, spi-dln2) give the guest a real I2C bus,
    gpiochip and IIO ADC. Whatever the guest does on them reaches the attached extension's model.

    Protocol: requests arrive on bulk OUT as a header (size, id, echo, handle) plus payload; each gets
    one reply on bulk IN with the same header and a 16-bit result (<= 0x80 is success) before its
    data. Events (GPIO edges) go out unasked with handle 0. Formats follow drivers/mfd/dln2.c and the
    per-function drivers next to it.
    """
    vid, pid, bcd = 0xA257, 0x2013, 0x0100
    manufacturer, product = "mechumu", "Comet extension connector"
    interfaces = [iface(0xFF, eps=[ep(0x81, "bulk", 64), ep(0x01, "bulk", 64)])]
    GPIO_PINS, ADC_CHANNELS = 5, 4
    OK, FAIL = 0, 0x83  # results: success, and a generic failure (NAK on I2C)

    def __init__(self, attached):
        super().__init__()
        self.attached = attached  # () -> the attached extension Device, or None
        self.gpio_dir = [0] * self.GPIO_PINS    # the Comet's side: 0 input, 1 output
        self.gpio_out = [0] * self.GPIO_PINS    # what the Comet drives
        self.gpio_event = [0] * self.GPIO_PINS  # interrupt type the Comet asked for (0 none)
        self.unplugged()

    def unplugged(self):
        """The extension's side of the pins with nothing attached: inputs low, ADCs at 0 V."""
        self.gpio_in = [0] * self.GPIO_PINS
        self.adc_volts = [0.0] * self.ADC_CHANNELS

    # --- the extension's side ------------------------------------------------------------------
    def drive(self, pin, value):
        """The extension drives GPIO<pin+1>; an edge raises the guest's interrupt if it asked."""
        old = self.gpio_in[pin]
        self.gpio_in[pin] = int(bool(value))
        ev = self.gpio_event[pin] & 0x0F
        if ev and (old != self.gpio_in[pin] or ev in (2, 3)):
            # DLN2_GPIO_CONDITION_MET_EV: count, type, pin, value
            self.reply(0, 0x0F | 0x01 << 8, 0, struct.pack("<HBHB", 1, self.gpio_event[pin], pin, self.gpio_in[pin]))

    def level(self, pin):
        return self.gpio_out[pin] if self.gpio_dir[pin] else self.gpio_in[pin]

    # --- the guest's side ------------------------------------------------------------------------
    def reply(self, handle, cmd, echo, data=b"", result=OK):
        body = (struct.pack("<H", result) if handle else b"") + data
        self.send(0x81, struct.pack("<HHHH", 8 + len(body), cmd, echo, handle) + body)

    def out(self, ep, data):
        while len(data) >= 8:
            size, cmd, echo, handle = struct.unpack("<HHHH", data[:8])
            payload, data = data[8:size], data[size:]
            result, rdata = self.handle(handle, cmd, payload)
            self.reply(handle, cmd, echo, rdata, result)

    def handle(self, handle, cmd, p):
        module, op = cmd >> 8, cmd & 0xFF
        ext = self.attached()
        if module == 0x00:  # generic: device version (DLN2_HW_ID), serial number
            return self.OK, struct.pack("<I", 0x200 if op == 0x30 else 0x4D454348)
        if module == 0x01:  # GPIO
            if op == 0x01:  # GET_PIN_COUNT
                return self.OK, struct.pack("<H", self.GPIO_PINS)
            (pin,) = struct.unpack_from("<H", p)
            if pin >= self.GPIO_PINS:
                return self.FAIL, b""
            if op in (0x0B, 0x0D, 0x14):  # GET_VAL (pin level), GET_OUT_VAL, GET_DIRECTION
                value = {0x0B: self.level(pin), 0x0D: self.gpio_out[pin], 0x14: self.gpio_dir[pin]}[op]
                return self.OK, struct.pack("<HB", pin, value)
            if op == 0x0C:  # SET_OUT_VAL
                self.gpio_out[pin] = p[2] & 1
                if self.gpio_dir[pin] and ext:
                    ext.on_gpio(pin + 1, self.gpio_out[pin])
            elif op == 0x13:  # SET_DIRECTION
                self.gpio_dir[pin] = p[2] & 1
                if self.gpio_dir[pin] and ext:
                    ext.on_gpio(pin + 1, self.gpio_out[pin])
            elif op == 0x1E:  # SET_EVENT_CFG: pin, type, period
                self.gpio_event[pin] = p[2]
            return self.OK, b""  # ENABLE, DISABLE, debounce: nothing to model
        if module == 0x03:  # I2C: WRITE / READ carry port, addr, mem_addr_len, mem_addr, buf_len
            if op in (0x06, 0x07):
                _, addr, _, _, n = struct.unpack_from("<BBBIH", p)
                if op == 0x06:
                    acked = ext.i2c_write(addr, bytes(p[9:9 + n])) if ext else False
                    return (self.OK if acked else self.FAIL), b""
                data = ext.i2c_read(addr, n) if ext else None
                if data is None:
                    return self.FAIL, b""
                return self.OK, struct.pack("<H", n) + bytes(data).ljust(n, b"\0")[:n]
            return self.OK, b""  # ENABLE / DISABLE
        if module == 0x06:  # ADC: 10 bits against 3.3 V
            if op == 0x01:  # GET_CHANNEL_COUNT
                return self.OK, bytes([self.ADC_CHANNELS])
            if op == 0x02:  # ENABLE: no pin conflicts
                return self.OK, b"\0\0"
            if op == 0x0A:  # CHANNEL_GET_VAL: port, channel
                ch = p[1]
                if ch >= self.ADC_CHANNELS:
                    return self.FAIL, b""
                volts = ext.adc(ch + 1) if ext else None
                volts = self.adc_volts[ch] if volts is None else volts
                return self.OK, struct.pack("<H", max(0, min(1023, round(volts / 3.3 * 1024))))
            return self.OK, b""
        if module == 0x02:  # SPI: enough for the controller to register
            # shortcut: no SPI transfers; Linux only makes spidev devices from DT/ACPI, which QEMU's virt machine lacks here.
            replies = {0x44: struct.pack("<H", 1), 0x45: struct.pack("<I", 1000), 0x46: struct.pack("<I", 24_000_000),
                       0x43: bytes([1, 8]).ljust(37, b"\0")}
            return self.OK, replies.get(op, b"")
        return self.FAIL, b""


# --- USB/IP server ---------------------------------------------------------------------------

OP_REQ_IMPORT, OP_REP_IMPORT = 0x8003, 0x0003
EPIPE, ECONNRESET = 32, 104  # Linux errno values (the guest's), not the host's: macOS ECONNRESET is 54
CMD_SUBMIT, CMD_UNLINK, RET_SUBMIT, RET_UNLINK = 1, 2, 3, 4


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("guest closed the connection")
        buf += chunk
    return buf


class Connection:
    """One imported device: the guest's vhci-hcd sends URBs, we answer them."""

    def __init__(self, sock, device):
        self.sock, self.device = sock, device
        self.wlock = threading.Lock()
        self.open = set()  # seqnums submitted and not yet answered
        self.olock = threading.Lock()

    def complete(self, seqnum, status, data=b"", sent=0):
        """Answer a URB with IN data, or with the byte count of an OUT; False if the guest unlinked it."""
        with self.olock:
            if seqnum not in self.open:
                return False
            self.open.discard(seqnum)
        hdr = struct.pack(">IIIII iiiii 8x", RET_SUBMIT, seqnum, 0, 0, 0, status, len(data) or sent, 0, 0, 0)
        try:
            with self.wlock:
                self.sock.sendall(hdr + data)
        except OSError:
            return False
        return True

    def serve(self):
        while True:
            cmd, seqnum, devid, direction, epnum = struct.unpack(">IIIII", recv_exact(self.sock, 20))
            rest = recv_exact(self.sock, 28)
            if cmd == CMD_UNLINK:
                (victim,) = struct.unpack(">I", rest[:4])
                with self.olock:
                    found = victim in self.open
                    self.open.discard(victim)
                with self.wlock:
                    self.sock.sendall(struct.pack(">IIIII i 24x", RET_UNLINK, seqnum, 0, 0, 0,
                                                  -ECONNRESET if found else 0))
                continue
            if cmd != CMD_SUBMIT:
                raise ConnectionError(f"unexpected USB/IP command {cmd}")
            _flags, length, _sf, npackets, _interval = struct.unpack(">Iiiii", rest[:20])
            setup = rest[20:28]
            data = recv_exact(self.sock, length) if direction == 0 and length > 0 else b""
            if npackets > 0:
                recv_exact(self.sock, 16 * npackets)  # isochronous descriptors: not supported
            with self.olock:
                self.open.add(seqnum)
            try:
                if epnum == 0:
                    reply = self.device._control(setup, data)
                    if reply is None:
                        self.complete(seqnum, -EPIPE)  # stall
                    elif direction == 1:
                        self.complete(seqnum, 0, reply)
                    else:
                        self.complete(seqnum, 0, sent=len(data))
                elif direction == 1:
                    self.device._in(self, seqnum, 0x80 | epnum, length)
                else:
                    self.device.out(epnum, data)
                    self.complete(seqnum, 0, sent=len(data))
            except Exception as e:  # a bug in a model stalls the request instead of killing the port
                print(f"mechumu: {type(self.device).__name__}: {e!r}", flush=True)
                self.complete(seqnum, -EPIPE)


def import_reply(busid, device):
    """OP_REP_IMPORT: the usbip_usb_device record the guest's attach needs."""
    busnum, devnum = (int(x) for x in busid.split("-"))
    return struct.pack(">HHI", 0x0111, OP_REP_IMPORT, 0) + struct.pack(
        ">256s32sIIIHHHBBBBBB", f"/mechumu/{busid}".encode(), busid.encode(), busnum, devnum,
        device.speed, device.vid, device.pid, device.bcd, device.cls, device.sub, device.proto,
        1, 1, len(device.interfaces))


def handshake(sock, lookup):
    """Read OP_REQ_IMPORT; return (busid, device) or None after refusing it."""
    version, code, _status = struct.unpack(">HHI", recv_exact(sock, 8))
    busid = recv_exact(sock, 32).rstrip(b"\0").decode()
    device = lookup(busid) if code == OP_REQ_IMPORT else None
    if device is None:
        sock.sendall(struct.pack(">HHI", 0x0111, OP_REP_IMPORT, 1))
        return None
    sock.sendall(import_reply(busid, device))
    return busid, device
