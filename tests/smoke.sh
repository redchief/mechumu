#!/bin/bash
# Boots the Virtual Comet headless and drives every simulated device through `mechumu ctl` and every
# built-in extension through `mechumu ext`, end to end through the guest, then saves and resumes it
# (Quick Boot). Needs out/ (`mechumu build`). Boots --cold, so it discards a state saved by `mechumu stop`.
# Usage: tests/smoke.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# Another Virtual Comet would answer for this one: they share the hub's sockets and ports.
! ./mechumu ctl usb >/dev/null 2>&1 || { echo "FAIL: a Virtual Comet is already running; stop it first (mechumu stop)"; exit 1; }
log=$(mktemp -t mechumu-smoke.XXXXXX)
./mechumu run --headless --cold </dev/null >"$log" 2>&1 &
qemu=$!
trap 'kill $qemu 2>/dev/null || true; rm -f "$log"' EXIT

echo "booting (serial log: $log)"
for _ in $(seq 60); do
    ./mechumu ctl usb 2>/dev/null | grep -q "Comet extension connector" && break  # guest up, connector attached
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
expect "ok GPIO3 driven 1" ctl gpio 3 1
expect "GPIO3=in:1" ctl gpio
expect "ok ADC1 at 1.65 V" ctl adc 1 1.65
expect "ok can0 <- 123#DEADBEEF" ctl can 123#DEADBEEF
expect "error usage: battery" ctl battery 101 charging
expect "ok home pressed" ctl button home
expect "mechanix-launch" shell pgrep -l mechanix-launch
expect "ok unlocked" ctl unlock
expect "ok brightness 70%" ctl brightness
expect "ok brightness 40%" ctl brightness 40
expect "192.168.77." shell 'for i in 1 2 3 4 5 6 7 8; do nmcli dev wifi connect "Mecha Lab" password mechumu-wifi ifname wlan0 >/dev/null 2>&1 && break; nmcli dev wifi rescan 2>/dev/null; sleep 2; done; ip -4 -br addr show wlan0'
# The onboard sensors: set on the host, read back through the guest's own drivers.
expect "ok light 500 lux" ctl light 500
expect "opt3001=500lux" ctl iio
expect "icm42605-accel" ctl iio
expect "lis2mdl" ctl iio
expect "ok tilted" ctl tilt 90 0
expect "HDA Intel" ctl sound
expect "ok tapped 540,620" ctl tap 540 620
expect "aarch64" shell uname -m
expect "pushed to /tmp/$(basename "$log")" push "$log" /tmp
expect "mecha wayland-1" launch sh -c 'echo $(id -un) $WAYLAND_DISPLAY'
expect "ok swiped 540,900 -> 540,300" ctl swipe 540 900 540 300 100
expect "error usage: tap" ctl tap 1
shot="$log.png"
expect "ok saved $shot" ctl screenshot "$shot"
[[ $(head -c 8 "$shot" | xxd -p) == 89504e470d0a1a0a ]] || { echo "FAIL: $shot isn't a PNG"; exit 1; }
rm -f "$shot"
# The connector's pin bridge is always there; extensions come and go on its USB pins.
expect "Comet extension connector" ctl usb
expect "ok keyboard attached" ext attach keyboard
expect "Comet Keyboard" ctl usb
expect "ok typed 5 characters" ext type hello
expect "ok gamepad attached" ext attach gamepad
expect "Comet Gamepad" ctl usb
expect "ok a pressed" ext press a
# The from-scratch example: its LED bar on the connector's SPI pins (the guest's spidev) and its
# backlight on PWM3 (the guest's pwmchip), and its speaker and mic on I2S (the guest's sound card).
expect "ok macro-pad attached" ext attach extensions/example.py
expect "ok spi a5 -> 00" ctl spi a5
expect "ok spi 3c81 -> a53c" ctl spi 3c81
expect "LED bar #......#" ext bar
expect "ok PWM3 1000 Hz, 25% duty" ctl pwm 3 1000 25
expect "backlight 25%" ext backlight
expect "ok PWM3=1000Hz@25% PWM4=off" ctl pwm
expect "Comet SAI2" ctl sound
expect "ok played 0.5 s" ctl i2s play
expect "peak 50%" ext speaker
expect "ok I2S played" ctl i2s
expect "ok mic tone at 50%" ext mic 50
expect "peak 50%" ctl i2s record
expect "ok io-breakout attached" ext attach io-breakout
expect "CP2102" ctl usb
expect "ok no I2S extension attached" ctl i2s
expect "ok io-breakout detached" ext detach
expect "error nothing attached" ext press a

expect "ok saved" stop
wait $qemu || true
start=$(date +%s)
./mechumu run --headless </dev/null >"$log" 2>&1 &
qemu=$!
for _ in $(seq 60); do
    ./mechumu ctl iio 2>/dev/null | grep -q "dln2-adc" && ./mechumu ctl iio | grep -q opt3001 && break
    kill -0 $qemu 2>/dev/null || { cat "$log"; echo "FAIL: qemu exited on resume"; exit 1; }
    sleep 0.5
done
grep -q "resuming" "$log" || { echo "FAIL: booted cold instead of resuming"; exit 1; }
took=$(( $(date +%s) - start ))
(( took < 20 )) || { echo "FAIL: resuming took ${took}s"; exit 1; }
echo "ok   resumed in ${took}s"
expect "opt3001" ctl iio
expect "ok keyboard attached" ext attach keyboard
expect "ok typed 2 characters" ext type hi
expect "ok tapped" ctl tap 540 620
echo PASS
