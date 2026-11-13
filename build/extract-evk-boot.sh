#!/bin/bash
# Copies the kernel, initrd and imx8mp-evk devicetree out of a raw virt image into
# OUT, for `mechumu evk` (QEMU's imx8mp-evk loads them directly, no UEFI).
# Usage: extract-evk-boot.sh IMAGE OUT. Needs root and loop devices; build.sh runs it.
set -euo pipefail

IMAGE=$1
OUT=$2
mnt=$(mktemp -d)
trap 'umount "$mnt"/* 2>/dev/null || true; rm -rf "$mnt"' EXIT

# Mount each partition at its offset (no partition nodes inside containers).
part() { sfdisk -J "$IMAGE" | python3 -c "import json,sys; p=json.load(sys.stdin)['partitiontable']['partitions']; \
    print(*next((x['start'] * 512, x['size'] * 512) for x in p if x.get('name') == sys.argv[1]))" "$1"; }
read -r off size < <(part esp)
mkdir "$mnt/esp" && mount -o ro,loop,offset="$off",sizelimit="$size" "$IMAGE" "$mnt/esp"
read -r off size < <(part root-arm64)
mkdir "$mnt/root" && mount -o ro,noload,loop,offset="$off",sizelimit="$size" "$IMAGE" "$mnt/root"

entry=$(ls "$mnt"/esp/loader/entries/*.conf | head -1)
kver=$(awk '$1 == "version" {print $2}' "$entry")
mkdir -p "$OUT"
cp "$mnt/root/usr/lib/modules/$kver/vmlinuz" "$OUT/vmlinuz"
cp "$mnt/root/usr/lib/modules/$kver/dtb/freescale/imx8mp-evk.dtb" "$OUT/"
# QEMU takes one initrd; concatenated cpio archives unpack as one, in the entry's order. The kernel
# only finds an archive that starts 4-byte aligned, so zero-pad each part (as bootloaders do).
: > "$OUT/initrd"
awk '$1 == "initrd" {print $2}' "$entry" | while read -r f; do
    cat "$mnt/esp$f" >> "$OUT/initrd"
    truncate -s %4 "$OUT/initrd"
done
ls -l "$OUT"
