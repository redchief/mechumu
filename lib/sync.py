#!/usr/bin/env python3
"""Watch a local file or directory and sync changes into the running Virtual Comet."""

import argparse
import io
import os
import posixpath
import shlex
import subprocess
import sys
import tarfile
import time

SSH_OPTS = [
    "-p", os.environ.get("MECHUMU_SSH_PORT", "2222"),
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "LogLevel=ERROR",
]
IGNORE_NAMES = {".git", "__pycache__", ".DS_Store"}


def snapshot(root: str) -> dict[str, tuple[int, int]]:
    """Return {relpath: (mtime_ns, size)} for `root` (a file or directory)."""
    if os.path.isfile(root):
        st = os.stat(root)
        return {os.path.basename(root): (st.st_mtime_ns, st.st_size)}

    state: dict[str, tuple[int, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_NAMES)
        for name in sorted(filenames):
            if name in IGNORE_NAMES or name.endswith(".pyc"):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            state[rel] = (st.st_mtime_ns, st.st_size)
    return state


def diff_snapshots(
    prev: dict[str, tuple[int, int]], curr: dict[str, tuple[int, int]]
) -> tuple[list[str], list[str]]:
    """Return (changed_or_added, deleted) relative paths."""
    changed = [p for p, meta in curr.items() if prev.get(p) != meta]
    deleted = [p for p in prev if p not in curr]
    return changed, deleted


def build_tar(root: str, relpaths: list[str]) -> bytes:
    """Pack `relpaths` from `root` into an in-memory tar stream."""
    buf = io.BytesIO()
    is_file = os.path.isfile(root)
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for rel in relpaths:
            src = root if is_file else os.path.join(root, rel)
            if os.path.exists(src):
                tf.add(src, arcname=rel, recursive=False)
    return buf.getvalue()


def push_changes(
    root: str,
    guest_target: str,
    changed: list[str],
    deleted: list[str],
) -> None:
    """Apply `changed` and `deleted` paths into `guest_target` over SSH."""
    if deleted:
        rm_args = " ".join(
            shlex.quote(posixpath.join(guest_target, p)) for p in deleted
        )
        subprocess.run(
            ["ssh", *SSH_OPTS, "root@localhost", f"rm -rf {rm_args}"],
            check=True,
        )
    if changed:
        tar_bytes = build_tar(root, changed)
        remote_cmd = f"mkdir -p {shlex.quote(guest_target)} && tar -xf - -C {shlex.quote(guest_target)}"
        subprocess.run(
            ["ssh", *SSH_OPTS, "root@localhost", remote_cmd],
            input=tar_bytes,
            check=True,
        )


def restart_app(mechumu_bin: str, cmd: str, proc: subprocess.Popen | None) -> subprocess.Popen:
    """Stop any previous launched process and start `cmd` via `mechumu launch`."""
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    argv = shlex.split(cmd)
    subprocess.run(
        ["ssh", *SSH_OPTS, "root@localhost", f"pkill -f {shlex.quote(argv[0])} 2>/dev/null || true"],
        check=False,
    )
    return subprocess.Popen([mechumu_bin, "launch", *argv])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mechumu sync",
        description="Sync a local file or directory into the running Virtual Comet.",
    )
    parser.add_argument("path", help="Local file or directory to sync")
    parser.add_argument("guest_dir", nargs="?", default="/opt", help="Guest destination directory (default: /opt)")
    parser.add_argument("--once", action="store_true", help="Sync once and exit")
    parser.add_argument("--interval", type=float, default=0.5, help="Poll interval in seconds (default: 0.5)")
    parser.add_argument("--restart", metavar="CMD", help="Launch and restart CMD in the guest session after each sync")
    args = parser.parse_args(argv)

    local = os.path.abspath(args.path)
    if not os.path.exists(local):
        print(f"mechumu sync: not found: {args.path}", file=sys.stderr)
        return 2

    guest_dir = args.guest_dir.rstrip("/") or "/"
    guest_target = guest_dir if os.path.isfile(local) else posixpath.join(guest_dir, os.path.basename(local))
    mechumu_bin = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mechumu")

    prev: dict[str, tuple[int, int]] = {}
    app_proc: subprocess.Popen | None = None
    try:
        while True:
            curr = snapshot(local)
            changed, deleted = diff_snapshots(prev, curr)
            if changed or deleted:
                push_changes(local, guest_target, changed, deleted)
                ts = time.strftime("%H:%M:%S")
                print(f"[{ts}] synced {len(changed)} updated, {len(deleted)} removed -> {guest_target}", flush=True)
                if args.restart:
                    app_proc = restart_app(mechumu_bin, args.restart, app_proc)
                prev = curr
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if app_proc is not None and app_proc.poll() is None:
            app_proc.terminate()
        print("\nmechumu sync: stopped")
    except subprocess.CalledProcessError as exc:
        print(f"mechumu sync: command failed (exit {exc.returncode})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
