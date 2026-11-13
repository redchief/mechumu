"""mechumu ctl: drive the running Virtual Comet's simulated hardware.

  mechumu ctl battery 15 discharging     # also: charging, full
  mechumu ctl gpio 3 1                   # drive connector pin GPIO3 high, as an extension would
  mechumu ctl gpio                       # GPIO1-5: direction and level
  mechumu ctl adc 1 1.65                 # put 1.65 V on connector pin ADC1 (0-3.3)
  mechumu ctl can 123#DEADBEEF           # inject a frame on can0, as if from an extension
  mechumu ctl button home                # press a hardware button: power, home, volup, voldown

Sends one line to the hub (lib/hub.py), which passes it over the VM's control port to comet-simd
in the guest, and prints the one-line reply; exits 0 if it starts with "ok". With --hub
(`mechumu ext`), the hub's own extension connector handles the line instead.
"""
import os, socket, sys


def ask(path, line, timeout=10):
    """Send one line to a control socket (relative to the cwd); return its one-line reply."""
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(timeout)
    sock.connect(path)
    with sock:
        sock.sendall((line + "\n").encode())
        return sock.makefile().readline().strip()


def main(argv):
    hub = argv[:1] == ["--hub"]
    args = argv[1:] if hub else argv
    if not args or args[0] in ("-h", "--help"):
        sys.exit(__import__("hub").__doc__ if hub else __doc__)
    name = "mechumu ext" if hub else "mechumu ctl"
    if hub and args[0] == "attach" and len(args) == 2 and args[1].endswith(".py"):
        args = ["attach", os.path.abspath(args[1])]  # the hub runs in MECHUMU_OUT
    os.chdir(os.environ["MECHUMU_OUT"])  # relative socket paths: macOS caps them at 104 bytes
    try:
        reply = ask("hub.sock", ("" if hub else "ctl ") + " ".join(args), 30)
    except (OSError, socket.timeout) as e:
        sys.exit(f"{name}: no answer from the Virtual Comet ({e}); is 'mechumu run' running and booted?")
    if not reply:
        sys.exit(f"{name}: the Virtual Comet closed the connection; is it still booting?")
    print(reply)
    sys.exit(0 if reply.startswith("ok") else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
