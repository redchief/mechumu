#!/bin/bash
# mechumu run [--app <desktop id|command>] [--frame [--browser] | --headless] [--cold] [-- <qemu args>]
# Boots $MECHUMU_OUT/mechanix-os-virt.qcow2 on QEMU's arm64 `virt` machine: the screen in a
# window (or, with --frame, in a window drawn as the device, buttons and extensions included: the
# frame app in app/, or a browser tab with --browser or without Rust),
# the serial console in this terminal (quit with Ctrl-A then X). Changes persist in the image.
# Quick Boot: `mechumu stop` saves the running device; the next run resumes it (--cold: boot fresh).
set -euo pipefail

APP= HEADLESS= FRAME= COLD= BROWSER= KERNEL= INITRD= CMDLINE=
while (($#)); do
    case $1 in
        --app) APP=${2:?--app needs a desktop id or command}; shift 2 ;;
        --headless) HEADLESS=1; shift ;;
        --frame) FRAME=1; shift ;;
        --cold) COLD=1; shift ;;
        --browser) FRAME=1 BROWSER=1; shift ;;
        --kernel) KERNEL=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "${2:?--kernel needs a path}"); COLD=1; shift 2 ;;
        --initrd) INITRD=$(python3 -c 'import os,sys; print(os.path.abspath(sys.argv[1]))' "${2:?--initrd needs a path}"); COLD=1; shift 2 ;;
        --cmdline) CMDLINE=${2:?--cmdline needs kernel arguments}; shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done

LIB=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "${MECHUMU_OUT:?}"
SSH_PORT=${MECHUMU_SSH_PORT:-2222}
USBIP_PORT=${MECHUMU_USBIP_PORT:-3240}
FRAME_PORT=${MECHUMU_FRAME_PORT:-8090}
printf 'MECHUMU_SSH_PORT=%s\nMECHUMU_USBIP_PORT=%s\nMECHUMU_FRAME_PORT=%s\n' "$SSH_PORT" "$USBIP_PORT" "$FRAME_PORT" > .ports
IMAGE=mechanix-os-virt.qcow2
[[ -f $IMAGE ]] || { echo "no $PWD/$IMAGE - run 'mechumu build'" >&2; exit 1; }

# UEFI firmware: Homebrew qemu ships edk2-aarch64-code.fd, Fedora/Debian ship AAVMF/QEMU_EFI.
# MECHUMU_QEMU: another QEMU, e.g. one with virgl on macOS (docs/macos-gpu.md).
QEMU=${MECHUMU_QEMU:-qemu-system-aarch64}
QEMU_SHARE=$(dirname "$(command -v "$QEMU" || echo /usr/bin/x)")/../share/qemu
for cand in "${EFI_CODE:-}" "$QEMU_SHARE/edk2-aarch64-code.fd" \
            /usr/share/edk2/aarch64/QEMU_EFI-pflash.raw /usr/share/AAVMF/AAVMF_CODE.fd; do
    [[ -n "$cand" && -f "$cand" ]] && { EFI_CODE=$cand; break; }
done
[[ -f "${EFI_CODE:-}" ]] || { echo "qemu-system-aarch64 or its aarch64 UEFI firmware not found - install qemu, or set EFI_CODE" >&2; exit 1; }

# Quick Boot. `mechumu stop` has the hub save the device (RAM and disks) as snapshot "quickboot"
# in the image and the vars store, then quit. A run with the same settings resumes from it; the hub
# then deletes it, so a run that ends any other way (Ctrl-A X, a crash, powering off) boots cold
# next time instead of bringing back a state its disk has moved on from. A snapshot only loads
# into the same machine, so other settings (--frame, --headless, --app, -- args), another QEMU or
# another version of this script boot cold too.
CONFIG="$(uname -s) headless=$HEADLESS frame=$FRAME app=$APP kernel=$KERNEL initrd=$INITRD cmdline=$CMDLINE args=$* $(cksum <"${BASH_SOURCE[0]}") $(command -v "$QEMU") $("$QEMU" --version | head -1)"
RESUME=
if [[ -z $COLD && -f .efi-vars.qcow2 && $(cat .quickboot-config 2>/dev/null) == "$CONFIG" ]] &&
   qemu-img snapshot -l "$IMAGE" 2>/dev/null | grep -q ' quickboot '; then
    RESUME=1
    echo "mechumu: resuming where 'mechumu stop' left off (--cold boots fresh)"
elif qemu-img snapshot -l "$IMAGE" 2>/dev/null | grep -q ' quickboot '; then
    qemu-img snapshot -d quickboot "$IMAGE"
fi
printf '%s' "$CONFIG" > .quickboot-config

# pflash units must be exactly 64 MiB. A fresh vars store every cold boot so stale boot entries never
# carry over; qcow2, so it can hold its part of a Quick Boot snapshot.
# Fixed names in $MECHUMU_OUT, overwritten each run: exec below means no exit trap could delete temp files.
EFI_VARS=.efi-vars.qcow2
EFI_CODE_PADDED=.efi-code.fd
rm -f .efi-vars.fd  # the raw store of older versions
if [[ -z $RESUME ]]; then
    rm -f "$EFI_VARS"
    qemu-img create -q -f qcow2 "$EFI_VARS" 64M
fi
cp "$EFI_CODE" "$EFI_CODE_PADDED"
truncate -s 64M "$EFI_CODE_PADDED"

case "$(uname -s)-$(uname -m)" in
    Darwin-arm64)  ACCEL=(-accel hvf -cpu host) ;;
    Linux-aarch64) ACCEL=(-accel kvm -cpu host) ;;
    *)             ACCEL=(-accel tcg -cpu max) ;;  # x86 hosts: full emulation, slow
esac

# The Comet panel's 1080x1240. Homebrew qemu has no virgl, so on macOS the guest renders with
# Mesa llvmpipe, unless MECHUMU_QEMU is a QEMU with virgl (GLES through ANGLE: docs/macos-gpu.md);
# on Linux, virgl gives the guest host GL.
# shortcut: macOS cocoa sends mouse, not touch, events; touch comes from the frame (--frame) and
# `mechumu ctl tap/swipe`, both through QMP, or from the gtk UI on Linux.
if [[ -n $HEADLESS ]]; then
    GPU=(-device virtio-gpu-pci,xres=1080,yres=1240 -display none)
elif [[ -n $FRAME ]]; then
    # VNC on a Unix socket only; the hub relays it to the frame page (lib/frame.html).
    GPU=(-device virtio-gpu-pci,xres=1080,yres=1240 -display none -vnc unix:vnc.sock)
elif [[ "$(uname -s)" == Darwin ]] && "$QEMU" -device help 2>/dev/null | grep -q '"virtio-gpu-gl-pci"'; then
    GPU=(-device virtio-gpu-gl-pci,xres=1080,yres=1240 -display cocoa,gl=es,zoom-to-fit=on)
elif [[ "$(uname -s)" == Darwin ]]; then
    GPU=(-device virtio-gpu-pci,xres=1080,yres=1240 -display cocoa,zoom-to-fit=on)
else
    GPU=(-device virtio-gpu-gl-pci,xres=1080,yres=1240 -display gtk,gl=on,zoom-to-fit=on)
fi

# Sound: the Comet's MAX98090 (speaker, headphones, mic) becomes an HDA sound card, played on the
# host's audio output: HDA, not virtio-sound, because QEMU can't snapshot virtio-sound (Quick Boot).
# Headless runs make no sound. QEMU's coreaudio backend is playback-only, so on macOS (and headless,
# for the same machine) the card has no mic (hda-output) rather than one that records silence.
if [[ -n $HEADLESS ]]; then
    AUDIO=(-audiodev none,id=snd0 -device intel-hda -device hda-output,audiodev=snd0)
elif [[ "$(uname -s)" == Darwin ]]; then
    AUDIO=(-audiodev coreaudio,id=snd0 -device intel-hda -device hda-output,audiodev=snd0)
else
    AUDIO=(-device intel-hda -device hda-duplex)  # QEMU's default backend: PipeWire, PulseAudio, ...
fi

# Settings for the guest travel as systemd credentials in SMBIOS OEM strings; QEMU option values
# double their commas. --app is read by comet-app.service; your SSH public key lets you in as root
# (ssh -p 2222 root@localhost), since images built without a password ship the mecha account locked.
CREDS=()  # expanded with a guard below: macOS bash 3.2 treats an empty array as unset under set -u
[[ -n $APP ]] && CREDS+=(-smbios "type=11,value=io.systemd.credential:mechanix.app=${APP//,/,,}")
[[ $USBIP_PORT != 3240 ]] && CREDS+=(-smbios "type=11,value=io.systemd.credential:mechumu.usbip_port=${USBIP_PORT}")
for key in ~/.ssh/id_ed25519.pub ~/.ssh/id_ecdsa.pub ~/.ssh/id_rsa.pub; do
    [[ -f $key ]] || continue
    k=$(<"$key")
    CREDS+=(-smbios "type=11,value=io.systemd.credential:ssh.authorized_keys.root=${k//,/,,}")
    break
done

# The extension connector (`mechumu ext`): USB/IP for the connector's USB, I2C, GPIO and ADC pins,
# and the other ends of UART1/UART2 above. It exits when QEMU, which takes over this process, does.
# MECHUMU_RESUME: after a Quick Boot the hub sets the guest's clock and reattaches its buses.
MECHUMU_QEMU_PID=$$ MECHUMU_RESUME=$RESUME MECHUMU_USBIP_PORT=$USBIP_PORT MECHUMU_FRAME_PORT=$FRAME_PORT python3 "$LIB/hub.py" >hub.log 2>&1 &  # $$ becomes QEMU at the exec below
if [[ -n $FRAME ]]; then
    url=http://127.0.0.1:${FRAME_PORT}/
    echo "mechumu: the device frame is at $url"
    # The frame app (app/), built on first use if Rust is installed; otherwise a browser tab.
    frame_app() {
        [[ -z $BROWSER ]] && command -v cargo >/dev/null || return 1
        [[ -x $LIB/../app/target/release/mechumu-frame ]] || echo "mechumu: building the frame app (once, about a minute)"
        cargo build -q --release --manifest-path "$LIB/../app/Cargo.toml" >>frame-app.log 2>&1 || {
            echo "mechumu: couldn't build the frame app (see $PWD/frame-app.log); opening a browser instead"; return 1; }
        "$LIB/../app/target/release/mechumu-frame" "$url" >>frame-app.log 2>&1 || true  # it ran: no browser too
    }
    (frame_app || { sleep 2; open "$url" 2>/dev/null || xdg-open "$url" 2>/dev/null || true; }) </dev/null &
fi

# acpi=off: the firmware hands Linux a devicetree, as a Comet's bootloader does; the onboard
# sensors' drivers take their configuration from it (kmod/comet-board).
LOADVM=(-name "Virtual Comet")  # never empty: macOS bash 3.2 treats an empty array as unset under set -u
[[ -n $RESUME ]] && LOADVM+=(-loadvm quickboot)
if [[ -n $KERNEL ]]; then
    [[ -f $KERNEL ]] || { echo "mechumu: kernel not found: $KERNEL" >&2; exit 1; }
    LOADVM+=(-kernel "$KERNEL")
    if [[ -n $INITRD ]]; then
        [[ -f $INITRD ]] || { echo "mechumu: initrd not found: $INITRD" >&2; exit 1; }
        LOADVM+=(-initrd "$INITRD")
    elif [[ -f evk-boot/initrd ]]; then
        LOADVM+=(-initrd evk-boot/initrd)
    fi
    LOADVM+=(-append "console=ttyAMA0 root=PARTLABEL=root-arm64 rw $CMDLINE")
fi
exec "$QEMU" \
    -machine virt,acpi=off \
    "${LOADVM[@]}" \
    "${ACCEL[@]}" \
    -smp 4 \
    -m 4096 \
    -drive if=pflash,format=raw,readonly=on,file="$EFI_CODE_PADDED" \
    -drive if=pflash,format=qcow2,file="$EFI_VARS" \
    -drive file="$IMAGE",if=none,id=bootdisk,format=qcow2 \
    -device virtio-blk-pci,drive=bootdisk,bootindex=1 \
    -device virtio-rng-pci \
    -netdev user,id=net0,hostfwd=tcp:127.0.0.1:${SSH_PORT}-:22 \
    -device virtio-net-pci,netdev=net0 \
    "${GPU[@]}" \
    "${AUDIO[@]}" \
    -device virtio-tablet-pci \
    -device virtio-multitouch-pci \
    -qmp unix:qmp.sock,server=on,wait=off \
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
