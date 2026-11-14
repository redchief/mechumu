#!/bin/bash
# mechumu run [--app <desktop id|command>] [--frame | --headless] [-- <qemu args>]
# Boots $MECHUMU_OUT/mechanix-os-virt.qcow2 on QEMU's arm64 `virt` machine: the screen in a
# window (or, with --frame, in a browser page drawn as the device, buttons and extensions included),
# the serial console in this terminal (quit with Ctrl-A then X). Changes persist in the image.
set -euo pipefail

APP= HEADLESS= FRAME=
while (($#)); do
    case $1 in
        --app) APP=${2:?--app needs a desktop id or command}; shift 2 ;;
        --headless) HEADLESS=1; shift ;;
        --frame) FRAME=1; shift ;;
        --) shift; break ;;
        *) break ;;
    esac
done

LIB=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "${MECHUMU_OUT:?}"
IMAGE=mechanix-os-virt.qcow2
[[ -f $IMAGE ]] || { echo "no $PWD/$IMAGE - run 'mechumu build'" >&2; exit 1; }

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
elif [[ -n $FRAME ]]; then
    # VNC on a Unix socket only; the hub relays it to the frame page (lib/frame.html).
    GPU=(-device virtio-gpu-pci,xres=1080,yres=1240 -display none -vnc unix:vnc.sock)
elif [[ "$(uname -s)" == Darwin ]]; then
    GPU=(-device virtio-gpu-pci,xres=1080,yres=1240 -display cocoa,zoom-to-fit=on)
else
    GPU=(-device virtio-gpu-gl-pci,xres=1080,yres=1240 -display gtk,gl=on,zoom-to-fit=on)
fi

# Sound: the Comet's MAX98090 (speaker, headphones, mic) becomes a virtio sound card, played on the
# host's audio output. Headless runs make no sound. QEMU's coreaudio backend is playback-only, so on
# macOS the card has no mic (streams=1: output only) rather than one that records silence.
if [[ -n $HEADLESS ]]; then
    AUDIO=(-audiodev none,id=snd0 -device virtio-sound-pci,audiodev=snd0)
elif [[ "$(uname -s)" == Darwin ]]; then
    AUDIO=(-audiodev coreaudio,id=snd0 -device virtio-sound-pci,audiodev=snd0,streams=1)
else
    AUDIO=(-device virtio-sound-pci)  # QEMU's default backend: PipeWire, PulseAudio, ...
fi

# Settings for the guest travel as systemd credentials in SMBIOS OEM strings; QEMU option values
# double their commas. --app is read by comet-app.service; your SSH public key lets you in as root
# (ssh -p 2222 root@localhost), since images built without a password ship the mecha account locked.
CREDS=()  # expanded with a guard below: macOS bash 3.2 treats an empty array as unset under set -u
[[ -n $APP ]] && CREDS+=(-smbios "type=11,value=io.systemd.credential:mechanix.app=${APP//,/,,}")
for key in ~/.ssh/id_ed25519.pub ~/.ssh/id_ecdsa.pub ~/.ssh/id_rsa.pub; do
    [[ -f $key ]] || continue
    k=$(<"$key")
    CREDS+=(-smbios "type=11,value=io.systemd.credential:ssh.authorized_keys.root=${k//,/,,}")
    break
done

# The extension connector (`mechumu ext`): USB/IP for the connector's USB, I2C, GPIO and ADC pins,
# and the other ends of UART1/UART2 above. It exits when QEMU, which takes over this process, does.
python3 "$LIB/hub.py" >hub.log 2>&1 &
if [[ -n $FRAME ]]; then
    url=http://127.0.0.1:8090/
    echo "mechumu: the device frame is at $url"
    (sleep 2; open "$url" 2>/dev/null || xdg-open "$url" 2>/dev/null || true) &
fi

# acpi=off: the firmware hands Linux a devicetree, as a Comet's bootloader does; the onboard
# sensors' drivers take their configuration from it (kmod/comet-board).
exec qemu-system-aarch64 \
    -machine virt,acpi=off \
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
    "${AUDIO[@]}" \
    -device virtio-tablet-pci \
    -device virtio-multitouch-pci \
    -device virtio-keyboard-pci \
    -device virtio-serial-pci \
    -chardev socket,id=cometctl,path=comet-ctl.sock,server=on,wait=off \
    -device virtserialport,chardev=cometctl,name=org.mecha.comet.ctl \
    -chardev socket,id=uart1,path=uart1.sock,server=on,wait=off \
    -device pci-serial,chardev=uart1,addr=0x1e \
    -chardev socket,id=uart2,path=uart2.sock,server=on,wait=off \
    -device pci-serial,chardev=uart2,addr=0x1f \
    -serial mon:stdio \
    ${CREDS[@]+"${CREDS[@]}"} \
    "$@"
