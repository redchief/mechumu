# Fast graphics on macOS hosts: where things stand

On a Mac, the Virtual Comet draws everything in software. The guest's Mesa uses
llvmpipe on the CPU, because the guest only gets a plain 2D `virtio-gpu`.
Android's emulator is fast because the guest's OpenGL runs on the host's GPU.
This note records what it would take to do the same for mechumu, as of
November 2026.

## What we have

- **Homebrew's QEMU (11.1.3) has no GPU path for the guest.** It offers
  `virtio-gpu-pci` and no `virtio-gpu-gl-pci`, and none of its display backends
  (`cocoa`, `dbus`, `curses`, `none`) passes OpenGL through on macOS. Checked with
  `qemu-system-aarch64 -device help` and `-display help`.
- **That's why mechumu needs the two opt-ins that are now upstream:**
  - `MECHANIX_ALLOW_SOFTWARE_EGL` lets the compositor accept llvmpipe.
  - `MECHA_GLES_DEVICE` lets apps render through the primary DRM node, since a 2D
    GPU has no render node.
- **On Linux hosts the GPU path already works:** `run-virt.sh` uses
  `virtio-gpu-gl-pci` with virgl and `-display gtk,gl=on`. It doesn't work through
  the device frame, which takes the screen over VNC.

## The options

**1. Virgl through ANGLE onto Metal (OpenGL ES in the guest).** This is the one
that fits. Mechanix renders with OpenGL ES (mecha-wayland's `gles` crate), and
this path gives the guest exactly that.

- **Source:** Akihiko Odaki, who maintains QEMU's macOS display code, keeps a
  patched stack:
  - QEMU's `cocoa` display with OpenGL
  - virglrenderer with better GLES support
  - libepoxy and ANGLE, using ANGLE's Metal backend since December 2025
- **Upstream status:** upstreaming has been "in progress" for a long time, and
  none of it is in QEMU yet.
- **How it runs:** `-device virtio-gpu-gl-pci -display cocoa,gl=es`. His guide
  calls `gl=es` stable and fast, and `gl=core` (through OpenGL.framework) quirky.
- **Packaging:** community Homebrew taps build this stack, for example
  milesbuckton/homebrew-qemu-virgl and startergo/homebrew-qemu-virgl-kosmickrisp.
  Homebrew-core now ships virglrenderer 1.3.0 for macOS, but not a QEMU that uses
  it.
- **Caveats:**
  - The graphics code runs in QEMU's own process: a guest that exploits it owns
    QEMU. That matters for running untrusted guests, much less for running Mecha's
    own image.
  - The screen is a GL surface in QEMU's Cocoa window. The device frame gets its
    screen over VNC, which reads pixels back, so the frame would stay slower than
    QEMU's own window.

**2. Venus (Vulkan) through MoltenVK.** A qemu-devel series from March 2026,
"virtio-gpu: enable Venus/Vulkan without OpenGL display", reached v5, then went
stale.

- **What it does:** lets `virtio-gpu-gl` build on macOS without OpenGL. Vulkan
  scanout isn't implemented, and the guest-side kernel support it needs is still
  pending.
- **Why it doesn't fit:** Mechanix renders with OpenGL ES, not Vulkan, so this
  would only help through Zink (GL on Vulkan) in the guest. It's a longer road
  with nothing to show yet.

**3. libkrun or krunkit (Podman's VMM on macOS).** Venus through MoltenVK works
there for GPU compute (AI inference in containers). It has no display path, and
it would mean leaving QEMU, along with the USB/IP, QMP and Quick Boot machinery
built on it. Not a fit.

## Recommendation

Don't make a patched QEMU the default: it would mean depending on a fork or a
community tap, and the stock path works.

Instead, mechumu has an **opt-in**:
1. `MECHUMU_QEMU=/path/to/qemu-system-aarch64` picks the QEMU binary.
2. When that binary offers `virtio-gpu-gl-pci` (`-device help`), the windowed
   mode on macOS uses `-device virtio-gpu-gl-pci -display cocoa,gl=es`.
3. Nothing else changes: the two opt-ins stay set, and the guest uses the GPU when
   it gets a render node.

The selection is tested with a stand-in QEMU. The GL path itself hasn't run yet:
it needs one of the patched builds above.

So anyone who installs one of those taps can try GPU rendering, while the
default stays on stock Homebrew QEMU. Quick Boot keeps working: the run's
fingerprint includes the QEMU binary and its version, so switching QEMU boots
cold.

Revisit when Odaki's QEMU patches land upstream, and switch the default on macOS
at that point. Until then, one test is worth running: whether a tap's QEMU still
handles everything mechumu relies on:
- QMP multi-touch
- `savevm` with `virtio-gpu-gl`
- HVF

## Sources

- Akihiko Odaki, [Linux Desktop on Apple Silicon in Practice](https://gist.github.com/akihikodaki/87df4149e7ca87f18dc56807ec5a1bc5)
- [virtio-gpu: enable Venus/Vulkan without OpenGL display](https://ratatoskr.run/qemu-devel/2026/03/14433210/t) (qemu-devel, March 2026)
- Community taps: [milesbuckton/homebrew-qemu-virgl](https://github.com/milesbuckton/homebrew-qemu-virgl), [startergo/homebrew-qemu-virgl-kosmickrisp](https://github.com/startergo/homebrew-qemu-virgl-kosmickrisp), [knazarov/homebrew-qemu-virgl](https://github.com/knazarov/homebrew-qemu-virgl) (older)
- [krunkit](https://github.com/containers/krunkit), [libkrun](https://github.com/containers/libkrun); Red Hat, [How we improved AI inference on macOS Podman containers](https://developers.redhat.com/articles/2025/06/05/how-we-improved-ai-inference-macos-podman-containers)
