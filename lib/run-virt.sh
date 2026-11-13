#!/bin/bash
# mechumu run [--app <desktop id|command>] [--headless] [-- <qemu args>]
# Boots $MECHUMU_OUT/mechanix-os-virt.qcow2 on QEMU's arm64 `virt` machine: the screen in a
# window, the serial console in this terminal (quit with Ctrl-A then X). Changes persist in the image.
set -euo pipefail

APP= HEADLESS=
while (($#)); do
    case $1 in
        --app) APP=${2:?--app needs a desktop id or command}; shift 2 ;;
        --headless) HEADLESS=1; shift ;;
        --) shift; break ;;
        *) break ;;
    esac
done

cd "${MECHUMU_OUT:?}"
IMAGE=mechanix-os-virt.qcow2
[[ -f $IMAGE ]] || { echo "no $PWD/$IMAGE - run 'mechumu build' or unpack a release here" >&2; exit 1; }

# UEFI firmware: Homebrew qemu ships edk2-aarch64-code.fd, Fedora/Debian ship AAVMF/QEMU_EFI.
QEMU_SHARE=$(dirname "$(command -v qemu-system-aarch64 || echo /usr/bin/x)")/../share/qemu
for cand in "${EFI_CODE:-}" "$QEMU_SHARE/edk2-aarch64-code.fd" \
            /usr/share/edk2/aarch64/QEMU_EFI-pflash.raw /usr/share/AAVMF/AAVMF_CODE.fd; do
    [[ -n "$cand" && -f "$cand" ]] && { EFI_CODE=$cand; break; }
done
[[ -f "${EFI_CODE:-}" ]] || { echo "qemu-system-aarch64 or its aarch64 UEFI firmware not found - install qemu, or set EFI_CODE" >&2; exit 1; }

# pflash units must be exactly 64 MiB; a fresh vars store every run so stale boot entries never carry over.
# Fixed names in $MECHUMU_OUT, overwritten each run: exec below means no exit trap could delete temp files.
EFI_VARS=.efi-vars.fd
EFI_CODE_PADDED=.efi-code.fd
rm -f "$EFI_VARS"
truncate -s 64M "$EFI_VARS"
cp "$EFI_CODE" "$EFI_CODE_PADDED"
truncate -s 64M "$EFI_CODE_PADDED"

case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  ACCEL=(-accel hvf -cpu host) ;;
    Linux-aarch64) ACCEL=(-accel kvm -cpu host) ;;
    *)             ACCEL=(-accel tcg -cpu max) ;;  # x86 hosts: full emulation, slow
esac

# The Comet panel's 1080x1240. Homebrew qemu has no virgl, so on macOS the guest renders with
# Mesa llvmpipe; on Linux, virgl gives the guest host GL.
# shortcut: macOS cocoa sends mouse, not touch, events; touch only works through the gtk UI on Linux.
if [[ -n $HEADLESS ]]; then
    GPU=(-device virtio-gpu-pci,xres=1080,yres=1240 -display none)
elif [[ "$(uname -s)" == Darwin ]]; then
    GPU=(-device virtio-gpu-pci,xres=1080,yres=1240 -display cocoa,zoom-to-fit=on)
else
    GPU=(-device virtio-gpu-gl-pci,xres=1080,yres=1240 -display gtk,gl=on,zoom-to-fit=on)
fi

# Settings for the guest travel as systemd credentials in SMBIOS OEM strings; QEMU option values
# double their commas. --app is read by comet-app.service; your SSH public key lets you in as root
# (ssh -p 2222 root@localhost), since released images ship the mecha account locked.
CREDS=()  # expanded with a guard below: macOS bash 3.2 treats an empty array as unset under set -u
[[ -n $APP ]] && CREDS+=(-smbios "type=11,value=io.systemd.credential:mechanix.app=${APP//,/,,}")
for key in ~/.ssh/id_ed25519.pub ~/.ssh/id_ecdsa.pub ~/.ssh/id_rsa.pub; do
    [[ -f $key ]] || continue
    k=$(<"$key")
    CREDS+=(-smbios "type=11,value=io.systemd.credential:ssh.authorized_keys.root=${k//,/,,}")
    break
done

exec qemu-system-aarch64 \
    -machine virt \
    "${ACCEL[@]}" \
    -smp 4 \
    -m 4096 \
    -drive if=pflash,format=raw,readonly=on,file="$EFI_CODE_PADDED" \
    -drive if=pflash,format=raw,file="$EFI_VARS" \
    -drive file="$IMAGE",if=none,id=bootdisk,format=qcow2 \
    -device virtio-blk-pci,drive=bootdisk,bootindex=1 \
    -device virtio-rng-pci \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:2222-:22 \
    -device virtio-net-pci,netdev=net0 \
    "${GPU[@]}" \
    -device virtio-tablet-pci \
    -device virtio-multitouch-pci \
    -device virtio-keyboard-pci \
    -device virtio-serial-pci \
    -chardev socket,id=cometctl,path=comet-ctl.sock,server=on,wait=off \
    -device virtserialport,chardev=cometctl,name=org.mecha.comet.ctl \
    -serial mon:stdio \
    ${CREDS[@]+"${CREDS[@]}"} \
    "$@"
