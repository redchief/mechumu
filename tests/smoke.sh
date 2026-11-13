#!/bin/bash
# Boots the Virtual Comet headless and drives every simulated device through `mechumu ctl`, end to
# end through the guest's comet-simd. Needs out/ (built or from a release). Usage: tests/smoke.sh
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

expect() {  # expect <text in reply> <ctl args...>
    local want=$1 out
    shift
    out=$(./mechumu ctl "$@" 2>&1) || true
    [[ $out == *"$want"* ]] || { echo "FAIL: ctl $* -> $out (want: $want)"; exit 1; }
    echo "ok   ctl $* -> $out"
}
expect "ok battery 42% charging" battery 42 charging
expect "ok EXT_GPIO3 driven 1" gpio 3 1
expect " 3=1 " gpio
expect "ok can0 <- 123#DEADBEEF" can 123#DEADBEEF
expect "error usage: battery" battery 101 charging
echo PASS
