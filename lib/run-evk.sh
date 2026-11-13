#!/bin/bash
# mechumu evk [-- <qemu args>]
# Boots the Virtual Comet's Mecha kernel and rootfs on QEMU's chip-level NXP i.MX8MP EVK model
# (real i.MX UART, GPIO, FlexCAN, uSDHC), for kernel and driver work. Serial console only: the
# model has no GPU or display. Quit with Ctrl-A then X. Changes are discarded at exit.
set -euo pipefail
[[ ${1:-} == -- ]] && shift

cd "${MECHUMU_OUT:?}"
IMAGE=mechanix-os-virt.qcow2
[[ -f $IMAGE && -f evk-boot/vmlinuz ]] || { echo "no $PWD/$IMAGE and evk-boot/ - run 'mechumu build'" >&2; exit 1; }

# QEMU's SD card must be a power of two in size; a throwaway overlay, recreated each run, gives it 8G
# and keeps the image untouched.
OVERLAY=.evk-overlay.qcow2
qemu-img create -q -f qcow2 -b "$PWD/$IMAGE" -F qcow2 "$OVERLAY" 8G

case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  ACCEL=(-accel hvf -cpu host) ;;
    Linux-aarch64) ACCEL=(-accel kvm -cpu host) ;;
    *)             ACCEL=(-accel tcg) ;;  # the model's own Cortex-A53s; boot takes minutes
esac

# The Virtual Comet's simulators (the EVK has a real FlexCAN can0) and greetd (no display) are masked.
# Drivers probing EVK peripherals QEMU doesn't model (PMIC, I2C expanders) stall udev, so allow slow devices.
MASK="systemd.mask=greetd.service systemd.mask=comet-simd.service systemd.mask=comet-sim-upower.service systemd.mask=comet-sim-ppd.service"
CMDLINE="console=ttymxc1,115200 root=PARTLABEL=root-arm64 rw selinux=0 systemd.default_device_timeout_sec=300 $MASK"

# The EVK's debug console is UART2 (ttymxc1), QEMU's second serial port; the first is unused.
exec qemu-system-aarch64 \
    -machine imx8mp-evk \
    "${ACCEL[@]}" \
    -smp 4 \
    -m 3G \
    -display none \
    -serial null \
    -serial mon:stdio \
    -kernel evk-boot/vmlinuz \
    -initrd evk-boot/initrd \
    -dtb evk-boot/imx8mp-evk.dtb \
    -append "$CMDLINE" \
    -drive file="$OVERLAY",if=sd,bus=2,format=qcow2 \
    "$@"
