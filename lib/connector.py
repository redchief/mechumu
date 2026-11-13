"""Virtual USB devices for the Comet's extension connector, served to the guest over USB/IP.

An extension model subclasses Device (or HidDevice) and describes itself with descriptors; the
guest's own kernel drivers (usbhid, cp210x, i2c-tiny-usb, ...) bind to it exactly as they would to
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

    # The connector's I2C pins (see I2cBridge): return True to ACK a write, bytes to answer a read.
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


class I2cBridge(Device):
    """The connector's I2C pins, as an i2c-tiny-usb adapter (the guest's i2c-tiny-usb driver gives
    it a /dev/i2c-N). Each transfer is answered by whichever extension is attached; none = NAK."""
    vid, pid, bcd = 0x0403, 0xC631, 0x0104
    product = "Comet extension connector I2C"
    interfaces = [iface(0xFF)]

    def __init__(self, attached):
        super().__init__()
        self.attached = attached  # () -> the attached extension Device, or None
        self.status = 0

    def control(self, setup, data):
        rtype, req, value, addr, length = struct.unpack("<BBHHH", setup)
        if req == 1:  # CMD_GET_FUNC: I2C_FUNC_I2C | I2C_FUNC_SMBUS_EMUL
            return struct.pack("<I", 0x1 | 0x0EFF0008)
        if req in (0, 2):  # CMD_ECHO, CMD_SET_DELAY
            return b""
        if req == 3:  # CMD_GET_STATUS: 1 = address ACKed, 2 = NAKed
            return bytes([self.status])
        if req & ~3 == 4:  # CMD_I2C_IO (+ BEGIN/END flags); value carries the I2C_M_RD flag
            ext = self.attached()
            if rtype & 0x80:
                reply = ext.i2c_read(addr, length) if ext else None
                self.status = 2 if reply is None else 1
                return (reply or b"").ljust(length, b"\0")[:length]
            self.status = 1 if ext and ext.i2c_write(addr, bytes(data)) else 2
            return b""
        return None


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
