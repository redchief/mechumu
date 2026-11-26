#!/usr/bin/env python3
"""Unit tests for lib/sync.py (no VM required)."""

import io
import os
import sys
import tarfile
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import sync


def test_snapshot_and_diff():
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "app.bin"), "wb") as f:
            f.write(b"v1")
        os.makedirs(os.path.join(tmp, "assets"))
        with open(os.path.join(tmp, "assets", "icon.png"), "wb") as f:
            f.write(b"png")
        os.makedirs(os.path.join(tmp, ".git"))
        with open(os.path.join(tmp, ".git", "HEAD"), "w") as f:
            f.write("ref: refs/heads/main\n")
        with open(os.path.join(tmp, ".DS_Store"), "wb") as f:
            f.write(b"junk")

        s1 = sync.snapshot(tmp)
        assert sorted(s1) == ["app.bin", "assets/icon.png"], s1

        changed, deleted = sync.diff_snapshots({}, s1)
        assert sorted(changed) == ["app.bin", "assets/icon.png"]
        assert deleted == []

        with open(os.path.join(tmp, "app.bin"), "wb") as f:
            f.write(b"v2-longer")
        os.remove(os.path.join(tmp, "assets", "icon.png"))

        s2 = sync.snapshot(tmp)
        changed, deleted = sync.diff_snapshots(s1, s2)
        assert changed == ["app.bin"], changed
        assert deleted == ["assets/icon.png"], deleted

        tar_bytes = sync.build_tar(tmp, changed)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tf:
            assert tf.getnames() == ["app.bin"]
            assert tf.extractfile("app.bin").read() == b"v2-longer"


def test_single_file_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "hello")
        with open(p, "wb") as f:
            f.write(b"123")
        s = sync.snapshot(p)
        assert list(s) == ["hello"]
        tar_bytes = sync.build_tar(p, ["hello"])
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r") as tf:
            assert tf.getnames() == ["hello"]
            assert tf.extractfile("hello").read() == b"123"


if __name__ == "__main__":
    test_snapshot_and_diff()
    test_single_file_snapshot()
    print("ok")
