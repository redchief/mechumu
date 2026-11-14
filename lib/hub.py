"""mechumu ext: the Virtual Comet's extension connector.

  mechumu ext attach keyboard        # snap on a built-in extension: keyboard, gamepad, io-breakout
  mechumu ext attach ./my_ext.py     # or your own model (see extensions/example.py)
  mechumu ext detach                 # take it off
  mechumu ext status                 # what's attached
  mechumu ext <command...>           # drive the attached extension, e.g. `mechumu ext type hello`

Runs alongside QEMU (`mechumu run` starts it). The connector's USB pins are a USB/IP server the
guest imports devices from (port 3240, localhost only): bus ID 1-1 is the attached extension, 1-2
an always-present DLN-2 adapter carrying the connector's I2C, GPIO and ADC pins (connector.Bridge),
1-3 the Comet's internal sensor bus (sensors.SensorBus).
comet-simd in the guest does the importing when told to over the control port. UART1 and UART2 are
QEMU PCI serial ports whose other ends (uart1.sock, uart2.sock) the hub holds for the model.
"""
import base64, hashlib, http.server, importlib.util, os, socket, struct, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import connector, sensors

USBIP_PORT = 3240
FRAME_PORT = 8090
EXTENSIONS = os.path.join(os.path.dirname(HERE), "extensions")


class Hub:
    def __init__(self):
        self.ext, self.name = None, None
        self.bridge = connector.Bridge(lambda: self.ext)
        self.i2s = connector.I2s(lambda: self.ext)
        self.sensors = sensors.SensorBus()
        self.uarts = {1: None, 2: None}
        self.lock = threading.Lock()
        self.port, self.port_lock, self.seq = None, threading.Lock(), 0

    def lookup(self, busid):
        i2s = self.i2s if self.ext and self.ext.i2s else None  # its sound card only with an I2S extension
        return {"1-1": self.ext, "1-2": self.bridge, "1-3": self.sensors, "1-4": i2s}.get(busid)

    def load(self, spec):
        path = spec if spec.endswith(".py") else os.path.join(EXTENSIONS, f"{spec}.py")
        if not os.path.isfile(path):
            builtin = sorted(f[:-3] for f in os.listdir(EXTENSIONS) if f.endswith(".py") and f != "example.py")
            raise ValueError(f"no extension {spec!r}; built in: {', '.join(builtin)}, or a path to a .py model")
        mod_spec = importlib.util.spec_from_file_location(f"ext_{abs(hash(path))}", path)
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)
        return mod.Extension(), getattr(mod.Extension, "name", os.path.basename(path)[:-3])

    def guest_raw(self, line, timeout=10):
        """One command to comet-simd over the control port; returns its "ok ..."/"error ..." line.

        The hub holds the port's only connection, so commands never race a reconnect (a command sent
        just as the previous client hung up can be lost). Each carries an ID its reply echoes, so a
        reply that arrives after we gave up on it is never taken for the next command's."""
        with self.port_lock:
            self.seq += 1
            tag = f"#{self.seq} "
            try:
                if self.port is None:
                    self.port = socket.socket(socket.AF_UNIX)
                    self.port.connect("comet-ctl.sock")
                    self.port_lines = self.port.makefile("rb")
                self.port.settimeout(timeout)
                self.port.sendall((tag + line + "\n").encode())
                while True:
                    reply = self.port_lines.readline().decode(errors="replace").strip()
                    if not reply:
                        raise ConnectionError("control port closed")
                    if reply.startswith(tag):
                        return reply[len(tag):]
            except (OSError, ConnectionError):
                if self.port:
                    self.port.close()
                self.port = None  # unanswered commands die with the connection
                return "error no answer from the Virtual Comet; is it still booting?"

    def unplug(self):
        if self.ext:
            self.ext.connector = None
        self.ext = self.name = None
        self.bridge.unplugged()

    # --- pins the hub simulates itself: `mechumu ctl gpio` / `ctl adc` / `ctl pwm` ---------------
    def pins(self, cmd, args):
        b = self.bridge
        if cmd == "gpio" and not args:
            return "ok " + " ".join(f"GPIO{i + 1}={'out' if b.gpio_dir[i] else 'in'}:{b.level(i)}"
                                    for i in range(b.GPIO_PINS))
        if cmd == "gpio" and len(args) == 2 and args[1] in ("0", "1") and args[0].isdigit() \
                and 1 <= int(args[0]) <= b.GPIO_PINS:
            b.drive(int(args[0]) - 1, int(args[1]))
            return f"ok GPIO{args[0]} driven {args[1]}"
        if cmd == "adc" and len(args) == 2 and args[0].isdigit() and 1 <= int(args[0]) <= b.ADC_CHANNELS:
            v = float(args[1])
            if not 0 <= v <= 3.3:
                return "error volts must be 0-3.3"
            b.adc_volts[int(args[0]) - 1] = v
            return f"ok ADC{args[0]} at {v:g} V"
        if cmd == "i2s" and args:  # playing or recording: the guest does that, through its sound card
            return self.guest_raw("i2s " + " ".join(args))
        if cmd == "i2s":
            if not (self.ext and self.ext.i2s):
                return "ok no I2S extension attached"
            played, peak = self.i2s.played / self.i2s.RATE, self.i2s.peak / 32768
            self.i2s.peak = 0
            return f"ok I2S played {played:.2f} s, peak {peak:.0%}"
        if cmd == "pwm" and args:  # setting one: the guest does that, through its own pwmchip
            return self.guest_raw("pwm " + " ".join(args))
        if cmd == "pwm":
            return "ok " + " ".join(f"PWM{n}=" + (f"{1e9 / p:g}Hz@{d / p:.0%}" if e and p else "off")
                                    for n, (p, d, e) in b.pwm.items())
        return f"error usage: gpio [<1-{b.GPIO_PINS}> <0|1>] | adc <1-{b.ADC_CHANNELS}> <volts> | pwm | i2s"

    # --- UART1/UART2: QEMU's PCI serial ports, guest side /dev/comet-uart1|2 ----------------------
    def serve_uart(self, n):
        while True:
            try:
                sock = socket.socket(socket.AF_UNIX)
                sock.connect(f"uart{n}.sock")
            except OSError:
                time.sleep(0.5)  # QEMU makes the socket once it starts
                continue
            self.uarts[n] = sock
            try:
                while data := sock.recv(4096):
                    if self.ext:
                        self.ext.on_uart(n, data)
            except OSError:
                pass
            except Exception as e:  # a model bug mustn't take the UART down
                print(f"mechumu: {self.name}: on_uart: {e!r}", flush=True)
            self.uarts[n] = None
            sock.close()

    def uart_send(self, n, data):
        if self.uarts.get(n):
            self.uarts[n].sendall(data)

    def guest(self, line):
        reply = self.guest_raw(line)
        if not reply.startswith("ok"):
            raise RuntimeError(reply.removeprefix("error "))
        return reply[3:]

    def handle(self, line):
        cmd, *args = line.split()
        with self.lock:
            if cmd == "attach" and len(args) == 1:
                ext, name = self.load(args[0])
                self.unplug()
                ext.connector = self
                self.ext, self.name = ext, name  # the guest's attach swaps out any previous one
                try:
                    self.guest("ext attach 1-1")
                    self.guest("bus detach 1-4")  # the previous extension's I2S sound card, if any
                    self.i2s.played = self.i2s.peak = 0
                    if ext.i2s:
                        self.guest("bus attach 1-4")
                except Exception:
                    self.unplug()
                    raise
                return f"{name} attached"
            if cmd == "detach" and not args:
                if not self.ext:
                    return "nothing attached"
                name = self.name
                self.unplug()
                self.guest("ext detach 1-1")
                self.guest("bus detach 1-4")
                return f"{name} detached"
            if cmd == "status" and not args:
                return f"{self.name} attached" if self.ext else "nothing attached"
            if not self.ext:
                raise ValueError("nothing attached; try: mechumu ext attach keyboard")
            return self.ext.command([cmd, *args]) or "done"

    # --- servers -----------------------------------------------------------------------------
    def serve_usbip(self, sock):
        while True:
            conn, _ = sock.accept()
            threading.Thread(target=self.usbip_conn, args=(conn,), daemon=True).start()

    def usbip_conn(self, sock):
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            imported = connector.handshake(sock, self.lookup)
            if imported:
                connector.Connection(sock, imported[1]).serve()
        except (ConnectionError, OSError):
            pass
        finally:
            sock.close()

    def serve_control(self, sock):
        while True:
            conn, _ = sock.accept()
            threading.Thread(target=self.control_conn, args=(conn,), daemon=True).start()

    def reply(self, line):
        if line.startswith("ctl "):
            cmd, *args = line[4:].split() or [""]
            try:
                if cmd in ("gpio", "adc", "pwm", "i2s"):
                    return self.pins(cmd, args)
                if cmd in ("light", "tilt", "accel", "gyro", "heading", "sensors"):
                    return self.sensors.ctl(cmd, args) or ("error usage: light <lux> | tilt <pitch> <roll> | "
                                                          "accel|gyro <x> <y> <z> | heading <degrees> | sensors")
            except ValueError as e:
                return f"error {e}"
        if line.startswith("ctl "):  # the rest of `mechumu ctl`: straight through to comet-simd
            return self.guest_raw(line[4:])
        try:
            return "ok " + self.handle(line)
        except Exception as e:
            return f"error {e}"

    def control_conn(self, conn):
        with conn:
            conn.sendall(self.reply(conn.makefile().readline().strip()).encode() + b"\n")


class Frame(http.server.BaseHTTPRequestHandler):
    """The device frame (`mechumu run --frame`): the page, its commands, and the screen's VNC stream
    as a WebSocket. Localhost only, and only for the page itself: commands need a header a
    cross-site request can't send, the WebSocket checks Origin, and Host is checked against DNS
    rebinding; QEMU's VNC listens on a Unix socket, never on TCP."""
    hub = None

    def log_message(self, *args):
        pass

    def allowed(self):
        return self.headers.get("Host") == f"127.0.0.1:{FRAME_PORT}"

    def do_GET(self):
        if not self.allowed():
            return self.send_error(403)
        if self.path == "/":
            body = open(os.path.join(HERE, "frame.html"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/vnc" and self.headers.get("Upgrade", "").lower() == "websocket":
            if self.headers.get("Origin") != f"http://127.0.0.1:{FRAME_PORT}":
                return self.send_error(403)
            self.websocket_vnc()
        else:
            self.send_error(404)

    def do_POST(self):
        if not self.allowed() or self.headers.get("X-Mechumu") != "1" or self.path != "/cmd":
            return self.send_error(403)
        line = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode().strip()
        body = self.hub.reply(line).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def websocket_vnc(self):
        """RFC 6455, just enough for noVNC: binary frames both ways, to QEMU's VNC socket."""
        key = self.headers["Sec-WebSocket-Key"].encode() + b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key).digest()).decode())
        self.end_headers()
        self.wfile.flush()
        vnc = socket.socket(socket.AF_UNIX)
        try:
            vnc.connect("vnc.sock")
        except OSError:
            return
        client = self.connection

        def to_browser():
            try:
                while data := vnc.recv(65536):
                    n = len(data)
                    hdr = bytes([0x82, n]) if n < 126 else (
                        struct.pack(">BBH", 0x82, 126, n) if n < 65536 else struct.pack(">BBQ", 0x82, 127, n))
                    client.sendall(hdr + data)
            except OSError:
                pass
            client.close()

        threading.Thread(target=to_browser, daemon=True).start()
        read = self.rfile.read
        try:
            while True:
                b0, b1 = read(2)
                n = b1 & 0x7F
                n = struct.unpack(">H", read(2))[0] if n == 126 else struct.unpack(">Q", read(8))[0] if n == 127 else n
                mask = read(4) if b1 & 0x80 else b"\0\0\0\0"
                payload = bytes(c ^ mask[i % 4] for i, c in enumerate(read(n)))
                op = b0 & 0x0F
                if op == 8:  # close
                    break
                if op == 9:  # ping -> pong
                    client.sendall(bytes([0x8A, len(payload)]) + payload)
                elif op in (0, 2):
                    vnc.sendall(payload)
        except (OSError, ValueError):
            pass
        vnc.close()


def main():
    os.chdir(os.environ["MECHUMU_OUT"])
    hub = Hub()
    usbip = socket.create_server(("127.0.0.1", USBIP_PORT))
    if os.path.exists("hub.sock"):
        os.unlink("hub.sock")
    control = socket.socket(socket.AF_UNIX)
    control.bind("hub.sock")
    control.listen()
    for target, sock in ((hub.serve_usbip, usbip), (hub.serve_control, control)):
        threading.Thread(target=target, args=(sock,), daemon=True).start()
    for n in (1, 2):
        threading.Thread(target=hub.serve_uart, args=(n,), daemon=True).start()
    Frame.hub = hub
    frame = http.server.ThreadingHTTPServer(("127.0.0.1", FRAME_PORT), Frame)
    threading.Thread(target=frame.serve_forever, daemon=True).start()
    # `mechumu run` execs QEMU in our parent's process; when QEMU exits we're reparented: stop too.
    parent = os.getppid()
    while os.getppid() == parent:
        time.sleep(1)
    os.unlink("hub.sock")


if __name__ == "__main__":
    try:
        main()
    except OSError as e:
        sys.exit(f"mechumu: extension connector not started ({e}); is another Virtual Comet running?")
