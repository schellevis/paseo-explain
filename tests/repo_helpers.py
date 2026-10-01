"""Helpers for tests that read a repository: tree hashing and git copies."""

import hashlib
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

FIXTURE_REPO = Path(__file__).resolve().parent / "fixtures" / "sample-repo"


def tree_state(path) -> dict:
    """Map every file under path (including .git/) to (sha256, st_mtime_ns)."""
    root = os.fspath(path)
    state = {}
    for folder, _dirs, files in os.walk(root, followlinks=False):
        for name in files:
            full = os.path.join(folder, name)
            info = os.lstat(full)
            if os.path.islink(full):
                data = os.fsencode(os.readlink(full))
            else:
                with open(full, "rb") as handle:
                    data = handle.read()
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            state[rel] = (hashlib.sha256(data).hexdigest(), info.st_mtime_ns)
    return state


def copy_fixture(dst) -> Path:
    dst = Path(dst)
    shutil.copytree(FIXTURE_REPO, dst, symlinks=True, dirs_exist_ok=True)
    return dst


def make_git_repo(src, dst) -> Path:
    """Copy a tree to dst, run git init and git add -A (no commit)."""
    dst = Path(dst)
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    env = dict(os.environ)
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    with tempfile.TemporaryDirectory(prefix="pe-home-") as home:
        env["HOME"] = home
        env["XDG_CONFIG_HOME"] = home
        for args in (["init", "-q"], ["add", "-A"]):
            subprocess.run(["git", "-C", str(dst), *args], env=env, check=True, capture_output=True, timeout=60)
    return dst
