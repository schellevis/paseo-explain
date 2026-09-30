"""Shared helpers for the paseo-explain test suite."""

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "paseo-explain" / "scripts" / "explain.py"
FIXTURES = REPO / "tests" / "fixtures"


def _home_env(d):
    return {
        "HOME": d,
        "PASEO_EXPLAIN_HOME": str(Path(d) / "pe"),
        "XDG_CONFIG_HOME": str(Path(d) / "cfg"),
    }


@contextlib.contextmanager
def temp_home():
    with tempfile.TemporaryDirectory(prefix="pe-test-") as d:
        yield _home_env(d)


def run_cli(*args, env=None):
    with tempfile.TemporaryDirectory(prefix="pe-test-") as d:
        full = dict(os.environ)
        full.update(_home_env(d))
        full["PYTHONDONTWRITEBYTECODE"] = "1"
        full.pop("PASEO_EXPLAIN_PASEO_BIN", None)
        if env:
            full.update(env)
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), *map(str, args)],
            capture_output=True,
            text=True,
            env=full,
            timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr


def make_session(home_env, kind, **init_opts):
    args = ["init", "--kind", kind] if "autopilot" not in init_opts else ["init"]
    if "slug" not in init_opts and "autopilot" not in init_opts:
        init_opts["slug"] = f"test-{kind}"
    for key, value in init_opts.items():
        args += [f"--{key.replace('_', '-')}", str(value)]
    code, out, err = run_cli(*args, env=home_env)
    if code != 0:
        raise AssertionError(f"init failed: {code} {out} {err}")
    return Path(json.loads(out)["session"])
