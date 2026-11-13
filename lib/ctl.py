"""mechumu ctl: drive the running Virtual Comet's simulated hardware.

  mechumu ctl battery 15 discharging     # also: charging, full
  mechumu ctl gpio 3 1                   # drive EXT_GPIO3 high, as an external signal would
  mechumu ctl gpio                       # read all 16 header lines
  mechumu ctl can 123#DEADBEEF           # inject a frame on can0, as if from an extension

Sends one line over the VM's control port to comet-simd in the guest and prints its
one-line reply; exits 0 if it starts with "ok".
"""
import os, socket, sys

if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
    sys.exit(__doc__)
sock = socket.socket(socket.AF_UNIX)
sock.settimeout(10)
try:
    # relative path: macOS limits socket paths to 104 bytes
    os.chdir(os.environ["MECHUMU_OUT"])
    sock.connect("comet-ctl.sock")
    sock.sendall((" ".join(sys.argv[1:]) + "\n").encode())
    reply = sock.makefile().readline().strip()
except (OSError, socket.timeout) as e:
    sys.exit(f"mechumu ctl: no answer from the Virtual Comet ({e}); is 'mechumu run' running and booted?")
if not reply:
    sys.exit("mechumu ctl: the Virtual Comet closed the connection; is it still booting?")
print(reply)
sys.exit(0 if reply.startswith("ok") else 1)
