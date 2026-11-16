#!/bin/bash
# Builds the Virtual Comet into OUT (default ./out): mechanix-os-virt.qcow2 and evk-boot/.
# Runs as root on Fedora 44 arm64: `mechumu build` runs it in a container, CI runs it directly.
#
# Every upstream source is pinned below and patches/<name>/*.patch are applied on top. Then the
# patched compositor, keyboard and extension service are compiled into mecha-make's virt overlay
# along with mechumu's guest/ tree, and mkosi builds the image from mecha-make's `virt` profile.
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OUT=$(realpath -m "${1:-$HERE/out}")
CACHE=${MECHUMU_CACHE:-/var/cache/mechumu}  # checkouts and cargo/dnf/mkosi caches, kept between builds
SRC=$CACHE/src

#      name               repository                                          commit
PINS=" mecha-make         https://github.com/mecha-org/mecha-make.git         b226b4291dd8708a6bad5fdc197b42c9150708fd
       mechanix-comp      https://github.com/mecha-org/mechanix-comp.git      43c9043531392e3bd696d7cf476cbd601bd19462
       mechanix-keyboard  https://github.com/mecha-org/mechanix-keyboard.git  ea50e90b909b24e9f874037bbe0c55616e95e7b8
       mechanix-gui       https://github.com/mecha-org/mechanix-gui.git       f365790458d8bdea310ded1c0f1d90b2990244b7
       mechanix-launcher  https://github.com/mecha-org/mechanix-launcher.git  2a3b9ed91d8fe7bc417c4c8068336f3e07ecb1d2"
# The keyboard's and launcher's lockfiles predate MECHA_GLES_DEVICE (mecha-wayland#78); build them on this dev commit instead.
MECHA_WAYLAND_DEV=ea8b565b345a5277b40e1064df6dff7f4253c972

[[ $(uname -m) == aarch64 ]] || { echo "build.sh: needs an arm64 host; the image and its binaries are aarch64" >&2; exit 1; }
[[ $EUID == 0 ]] || { echo "build.sh: needs root (mkosi, loop devices)" >&2; exit 1; }

echo "==> Build dependencies"
dnf -y -q install --setopt=install_weak_deps=False --setopt=keepcache=True --setopt=cachedir="$CACHE/dnf" \
    mkosi distribution-gpg-keys fedora-repos systemd systemd-repart git python3 util-linux e2fsprogs dosfstools squashfs-tools xz tar qemu-img \
    cargo rust gcc pkgconf-pkg-config libseat-devel libinput-devel systemd-devel libxkbcommon-devel \
    wayland-devel alsa-lib-devel pulseaudio-libs-devel pam-devel mesa-libgbm-devel libdrm-devel mesa-libEGL-devel

fetch() {  # name url commit: a clean checkout of commit, patched
    local dir=$SRC/$1
    [[ -d $dir/.git ]] || git init -q "$dir"
    git -C "$dir" fetch -q --depth 1 "$2" "$3"
    git -C "$dir" checkout -q -f FETCH_HEAD
    git -C "$dir" clean -q -fdx -e mkosi.cache -e mkosi.pkgcache  # keep mkosi's caches
    for p in "$HERE/patches/$1"/*.patch; do
        [[ -e $p ]] || continue
        git -C "$dir" apply "$p"
    done
}
while read -r name url rev; do
    echo "==> $name @ ${rev:0:7}"
    fetch "$name" "$url" "$rev"
done <<<"$PINS"

echo "==> Compositor, keyboard, launcher and patched extension service"
export CARGO_HOME=$CACHE/cargo
OVERLAY=$SRC/mecha-make/mkosi.extra.virt/usr/bin
cargo build --release --locked --manifest-path "$SRC/mechanix-comp/Cargo.toml" --target-dir "$CACHE/target/mechanix-comp"
# RUSTC_BOOTSTRAP: mecha-wayland's workspace manifest enables a nightly cargo feature; no nightly code is used.
RUSTC_BOOTSTRAP=1 cargo update -q --manifest-path "$SRC/mechanix-keyboard/Cargo.toml" -p mecha-wayland --precise "$MECHA_WAYLAND_DEV"
RUSTC_BOOTSTRAP=1 cargo build --release --locked --manifest-path "$SRC/mechanix-keyboard/Cargo.toml" --target-dir "$CACHE/target/mechanix-keyboard"
# The launcher: Mecha's shell (home, lock screen, status bar, its own keyboard), not yet packaged.
RUSTC_BOOTSTRAP=1 cargo update -q --manifest-path "$SRC/mechanix-launcher/Cargo.toml" -p mecha-wayland --precise "$MECHA_WAYLAND_DEV"
RUSTC_BOOTSTRAP=1 cargo build --release --locked --manifest-path "$SRC/mechanix-launcher/Cargo.toml" --target-dir "$CACHE/target/mechanix-launcher"
# mechanix-gui's workspace fetches every member's git dependencies (Flutter among them) to build any
# one crate, so the extension service's two crates get a workspace of their own, on upstream's lockfile.
GUI=$SRC/mechanix-gui EXT=$SRC/mechanix-extensions
rm -rf "$EXT" && mkdir -p "$EXT/services" "$EXT/dbus/mechanix"
cp -R "$GUI/services/extensions" "$EXT/services/"
cp -R "$GUI/dbus/mechanix/extensions" "$EXT/dbus/mechanix/"
cp "$GUI/Cargo.lock" "$EXT/"
{
    printf '[workspace]\nresolver = "3"\nmembers = ["services/extensions", "dbus/mechanix/extensions"]\n\n'
    awk '/^\[workspace.package\]/ {p=1} /^\[workspace.dependencies\]/ {p=0} p' "$GUI/Cargo.toml"
    printf '[workspace.dependencies]\n'
    grep -E '^(tokio|log|anyhow|env_logger) =' "$GUI/Cargo.toml"
} > "$EXT/Cargo.toml"
cargo build --release -p mechanix-extension-service --manifest-path "$EXT/Cargo.toml" --target-dir "$CACHE/target/mechanix-gui"
install -D -m 0755 "$CACHE/target/mechanix-comp/release/compositor" "$OVERLAY/mechanix-compositor"
install -D -m 0755 "$CACHE/target/mechanix-keyboard/release/mechanix-keyboard" "$OVERLAY/mechanix-keyboard"
install -D -m 0755 "$CACHE/target/mechanix-launcher/release/launcher" "$OVERLAY/mechanix-launcher"
# Its lock screen's PAM services (PIN, password); the PIN file comes from MECHANIX_LOCKSCREEN_PIN below.
install -D -m 0644 -t "$SRC/mecha-make/mkosi.extra.virt/etc/pam.d" "$SRC/mechanix-launcher/crates/lockscreen/resources/pam.d/"*
install -D -m 0755 "$CACHE/target/mechanix-gui/release/mechanix-extension-service" "$OVERLAY/mechanix-extension-service"
# mechumu's own guest side: simulated hardware, extension connector, app-only mode (see guest/).
cp -R "$HERE/guest/." "$SRC/mecha-make/mkosi.extra.virt/"

echo "==> Image"
cd "$SRC/mecha-make"
rm -f mkosi.env
# Optional MECHA_USER_PASSWORD / MECHANIX_LOCKSCREEN_PIN; unset, the mecha account ships locked and
# the lock screen's PIN is 1234 (it has no swipe-only mode; the Virtual Comet is a local VM).
[[ -f $HERE/mechumu.env ]] && install -m 0600 "$HERE/mechumu.env" mkosi.env
grep -qs '^MECHANIX_LOCKSCREEN_PIN=' mkosi.env || { echo 'MECHANIX_LOCKSCREEN_PIN=1234' >>mkosi.env; chmod 0600 mkosi.env; }
# The comet-board kernel module (kmod/) is built inside the image build, against the image's kernel.
# Its workspace on the cache volume: the build overlay can't stack on a container's own overlayfs.
mkdir -p "$CACHE/mkosi-workspace"
rm -rf "$CACHE/mkosi-workspace"/mkosi-workspace-*  # a crashed build's leftovers: several GB each
mkosi --profile=virt --image-version="$(date -u +%Y%m%d-%H%M)" \
    --environment=MECHA_COMMIT_SHA="mechumu-${MECHUMU_REV:-local}" \
    --build-package="kernel-devel-7.1.5-0.mecha.*" --build-package=gcc --build-package=make \
    --build-package=elfutils-libelf-devel \
    --build-sources="$HERE:mechumu" --build-script="$HERE/build/comet-board.build.chroot" \
    --workspace-directory="$CACHE/mkosi-workspace" -f build

echo "==> $OUT"
mkdir -p "$OUT"
qemu-img convert -c -O qcow2 mechanix-os-virt.raw "$OUT/mechanix-os-virt.qcow2"
"$HERE/build/extract-evk-boot.sh" mechanix-os-virt.raw "$OUT/evk-boot"
