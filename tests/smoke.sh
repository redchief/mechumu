#!/bin/bash
# Boots the Virtual Comet headless and drives every simulated device through `mechumu ctl` and every
# built-in extension through `mechumu ext`, end to end through the guest. Needs out/ (`mechumu build`).
# Usage: tests/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."

log=$(mktemp -t mechumu-smoke.XXXXXX)
./mechumu run --headless </dev/null >"$log" 2>&1 &
qemu=$!
trap 'kill $qemu 2>/dev/null || true; rm -f "$log"' EXIT

echo "booting (serial log: $log)"
for _ in $(seq 60); do
    ./mechumu ctl gpio >/dev/null 2>&1 && break
    kill -0 $qemu 2>/dev/null || { cat "$log"; echo "FAIL: qemu exited"; exit 1; }
    sleep 2
done

expect() {  # expect <text in reply> <mechumu args...>
    local want=$1 out
    shift
    out=$(./mechumu "$@" 2>&1) || true
    [[ $out == *"$want"* ]] || { echo "FAIL: mechumu $* -> $out (want: $want)"; exit 1; }
    echo "ok   mechumu $* -> $out"
}
expect "ok battery 42% charging" ctl battery 42 charging
expect "ok EXT_GPIO3 driven 1" ctl gpio 3 1
expect " 3=1 " ctl gpio
expect "ok can0 <- 123#DEADBEEF" ctl can 123#DEADBEEF
expect "error usage: battery" ctl battery 101 charging
expect "ok home pressed" ctl button home
# The connector's I2C adapter is always there; extensions come and go on its USB pins.
expect "Comet extension connector I2C" ctl usb
expect "ok keyboard attached" ext attach keyboard
expect "Comet Keyboard" ctl usb
expect "ok typed 5 characters" ext type hello
expect "ok gamepad attached" ext attach gamepad
expect "Comet Gamepad" ctl usb
expect "ok a pressed" ext press a
expect "ok io-breakout attached" ext attach io-breakout
expect "CP2102" ctl usb
expect "ok io-breakout detached" ext detach
expect "error nothing attached" ext press a
echo PASS
