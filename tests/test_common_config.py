"""Tests for session init, configuration, and CLI wiring."""

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

sys.path.insert(0, str(helpers.REPO / "paseo-explain" / "scripts"))

from explainlib import common, config

SESSION_FIELDS = {
    "explain_schema",
    "slug",
    "kind",
    "identity",
    "mode",
    "levels",
    "default_level",
    "lang",
    "theme",
    "created_at",
    "round",
    "content_sha256",
    "export_dir",
    "reader_grade",
    "skipped",
    "urls",
    "tab_opened",
    "depth",
}

SUBCOMMANDS = (
    "init frame ingest survey-prepare survey-report validate check-prepare check-report apply-corrections "
    "skip grade md render serve show stop inject tab result config lint leaks"
).split()


def _read_session(path: Path) -> dict:
    return json.loads((path / "session.json").read_text(encoding="utf-8"))


def _write_config(env, obj) -> None:
    path = Path(env["XDG_CONFIG_HOME"]) / "paseo-explain" / "config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj), encoding="utf-8")


def _load_cli():
    spec = importlib.util.spec_from_file_location("explain_cli_under_test", helpers.SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CommonConfigTests(unittest.TestCase):
    def test_version(self):
        code, out, err = helpers.run_cli("--version")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.strip(), "paseo-explain 0.2.1")
        self.assertEqual(err, "")

    def test_config_missing_defaults(self):
        with helpers.temp_home() as env:
            with mock.patch.dict(os.environ, env, clear=True):
                report = config.config_report()
        self.assertTrue(report["ok"])
        self.assertFalse(report["exists"])
        self.assertEqual(report["warnings"], [])
        self.assertTrue(report["path"].endswith("/cfg/paseo-explain/config.json"))
        for key, value in (("reading_level", 3), ("levels", 3), ("theme", "dark"), ("lang", "en"),
                           ("serve_host", "127.0.0.1"), ("serve_port", None), ("public_base_url", None)):
            self.assertEqual(report["values"][key], {"value": value, "source": "default"})

    def test_config_file_values(self):
        with helpers.temp_home() as env:
            _write_config(env, {"reading_level": 4, "levels": 5, "theme": "light", "lang": "nl"})
            with mock.patch.dict(os.environ, env, clear=True):
                report = config.config_report()
        self.assertTrue(report["exists"])
        self.assertEqual(report["warnings"], [])
        self.assertEqual(report["values"]["reading_level"], {"value": 4, "source": "config"})
        self.assertEqual(report["values"]["levels"], {"value": 5, "source": "config"})
        self.assertEqual(report["values"]["theme"], {"value": "light", "source": "config"})
        self.assertEqual(report["values"]["lang"], {"value": "nl", "source": "config"})

    def test_direct_serving_config_values(self):
        with helpers.temp_home() as env:
            _write_config(env, {"serve_host": "::1", "serve_port": 8300,
                                "public_base_url": "https://my-host:8300/base/"})
            with mock.patch.dict(os.environ, env, clear=True):
                report = config.config_report()
        self.assertEqual(report["warnings"], [])
        for key, value in (("serve_host", "::1"), ("serve_port", 8300),
                           ("public_base_url", "https://my-host:8300/base/")):
            self.assertEqual(report["values"][key], {"value": value, "source": "config"})

    def test_direct_serving_invalid_values_warned(self):
        bad = [
            ("serve_host", "example.com"), ("serve_host", 42),
            ("serve_port", True), ("serve_port", 1023), ("serve_port", 65536),
            ("public_base_url", "relative/path"),
            ("public_base_url", "ftp://my-host"),
            ("public_base_url", "http://my-host/?q=1"),
            ("public_base_url", "http://my-host/#part"),
        ]
        for key, value in bad:
            with self.subTest(key=key, value=value), helpers.temp_home() as env:
                _write_config(env, {key: value})
                with mock.patch.dict(os.environ, env, clear=True):
                    report = config.config_report()
                self.assertEqual(len(report["warnings"]), 1)
                self.assertIn(key, report["warnings"][0])
                self.assertEqual(report["values"][key]["source"], "default")

    def test_config_invalid_values_warned(self):
        with helpers.temp_home() as env:
            _write_config(env, {"reading_level": 9, "levels": 4, "theme": "blue", "extra": 1})
            with mock.patch.dict(os.environ, env, clear=True):
                report = config.config_report()
        self.assertEqual(len(report["warnings"]), 4)
        joined = "\n".join(report["warnings"])
        self.assertIn("reading_level", joined)
        self.assertIn("levels", joined)
        self.assertIn("theme", joined)
        self.assertIn("unknown", joined)
        for key, value in (("reading_level", 3), ("levels", 3), ("theme", "dark"), ("lang", "en")):
            self.assertEqual(report["values"][key], {"value": value, "source": "default"})

    def test_config_path_when_xdg_unset(self):
        with helpers.temp_home() as env:
            kept = {"HOME": env["HOME"], "PASEO_EXPLAIN_HOME": env["PASEO_EXPLAIN_HOME"]}
            with mock.patch.dict(os.environ, kept, clear=True):
                path = config.config_path()
                report = config.config_report()
        expected = Path(env["HOME"]) / ".config" / "paseo-explain" / "config.json"
        self.assertEqual(path, expected)
        self.assertEqual(report["path"], str(expected))
        self.assertFalse(report["exists"])

    def test_snap_level(self):
        self.assertEqual(config.snap_level(2, [1, 3, 5]), 1)
        self.assertEqual(config.snap_level(4, [1, 3, 5]), 3)
        self.assertEqual(config.snap_level(5, [1, 3, 5]), 5)

    def test_resolve_default_level(self):
        cfg = {"values": {"reading_level": 4, "levels": 3, "theme": "dark", "lang": "en"}}
        self.assertEqual(config.resolve_default_level(None, cfg, [1, 3, 5]), 3)

    def test_init_session_fields(self):
        with helpers.temp_home() as env:
            code, out, err = helpers.run_cli("init", "--kind", "plan", "--slug", "test-plan", env=env)
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            session = Path(payload["session"])
            data = _read_session(session)
        self.assertEqual(set(data), SESSION_FIELDS)
        self.assertEqual(data["explain_schema"], 1)
        self.assertEqual(data["slug"], "test-plan")
        self.assertEqual(data["kind"], "plan")
        self.assertEqual(data["identity"], {"source": "user", "kind": "plan"})
        self.assertEqual(data["mode"], "standard")
        self.assertEqual(data["levels"], [1, 3, 5])
        self.assertEqual(data["default_level"], 3)
        self.assertEqual(data["lang"], "en")
        self.assertEqual(data["theme"], "dark")
        self.assertRegex(data["created_at"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(data["round"], 0)
        self.assertIsNone(data["content_sha256"])
        self.assertIsNone(data["export_dir"])
        self.assertIsNone(data["reader_grade"])
        self.assertEqual(data["skipped"], {"fact_check": None, "reader_test": None})
        self.assertIsNone(data["urls"])
        self.assertIsNone(data["tab_opened"])
        self.assertEqual(payload["ok"], True)
        self.assertEqual(payload["slug"], "test-plan")
        self.assertEqual(payload["round"], 0)
        self.assertEqual(payload["default_level"], 3)
        self.assertEqual(payload["levels"], [1, 3, 5])
        self.assertEqual(payload["mode"], "standard")
        self.assertEqual(session.name, "test-plan")

    def test_init_default_level_from_config(self):
        with helpers.temp_home() as env:
            _write_config(env, {"reading_level": 4, "theme": "light", "lang": "nl"})
            session = helpers.make_session(env, "plan")
            data = _read_session(session)
            self.assertEqual(data["levels"], [1, 3, 5])
            self.assertEqual(data["default_level"], 3)
            self.assertEqual(data["theme"], "light")
            self.assertEqual(data["lang"], "nl")
            snapped = helpers.make_session(env, "plan", slug="lvl", level=2)
            self.assertEqual(_read_session(snapped)["default_level"], 1)
            wide = helpers.make_session(env, "plan", slug="wide", levels=5, level=4)
            wide_data = _read_session(wide)
            self.assertEqual(wide_data["levels"], [1, 2, 3, 4, 5])
            self.assertEqual(wide_data["default_level"], 4)

    def test_autopilot_slug_punctuation(self):
        slug = common.autopilot_slug("/x/20260930T143752Z-Some Run!!", "plan")
        self.assertIsNotNone(common.SLUG_RE.fullmatch(slug))
        self.assertTrue(slug.startswith("ap-20260930t143752z-some-run-"))
        self.assertTrue(slug.endswith("-plan"))

    def test_autopilot_slug_long_basename(self):
        with tempfile.TemporaryDirectory(prefix="pe-test-") as parent:
            directory = Path(parent) / ("c" * 80)
            directory.mkdir()
            spec = common.autopilot_slug(str(directory), "spec")
            plan = common.autopilot_slug(str(directory), "plan")
        self.assertTrue(spec.endswith("-spec"))
        self.assertTrue(plan.endswith("-plan"))
        self.assertNotEqual(spec, plan)
        self.assertLessEqual(len(spec), 57)
        self.assertLessEqual(len(plan), 57)
        self.assertIsNotNone(common.SLUG_RE.fullmatch(spec))
        self.assertIsNotNone(common.SLUG_RE.fullmatch(plan))

    def test_autopilot_slug_same_basename_different_dirs(self):
        with tempfile.TemporaryDirectory(prefix="pe-test-") as one:
            with tempfile.TemporaryDirectory(prefix="pe-test-") as two:
                left = Path(one) / "same"
                right = Path(two) / "same"
                left.mkdir()
                right.mkdir()
                self.assertNotEqual(
                    common.autopilot_slug(str(left), "plan"),
                    common.autopilot_slug(str(right), "plan"),
                )

    def test_autopilot_slug_exact_value(self):
        with tempfile.TemporaryDirectory(prefix="pe-test-") as parent:
            directory = Path(parent) / (("a" * 39) + "-b")
            directory.mkdir()
            slug = common.autopilot_slug(str(directory), "spec")
            real = os.path.realpath(directory)
            digest = hashlib.sha1(real.encode()).hexdigest()[:8]
        self.assertEqual(slug, "ap-" + ("a" * 39) + "--" + digest + "-spec")

    def test_init_reuse_keeps_created_at(self):
        with helpers.temp_home() as env:
            session = helpers.make_session(env, "plan")
            data = _read_session(session)
            data["created_at"] = "2000-01-01T00:00:00Z"
            data["round"] = 4
            (session / "session.json").write_text(json.dumps(data), encoding="utf-8")
            again = helpers.make_session(env, "plan")
            kept = _read_session(again)
        self.assertEqual(again, session)
        self.assertEqual(kept["created_at"], "2000-01-01T00:00:00Z")
        self.assertEqual(kept["round"], 4)

    def test_init_identity_mismatch(self):
        with helpers.temp_home() as env:
            run = Path(env["HOME"]) / "run-a"
            run.mkdir()
            slug = common.autopilot_slug(str(run), "spec")
            session = helpers.make_session(env, "plan", slug=slug)
            code, out, err = helpers.run_cli("init", "--autopilot", str(run), "--doc", "spec", env=env)
            kept = _read_session(session)
        self.assertEqual(code, 1)
        self.assertIn("session identity mismatch", out)
        self.assertEqual(kept["identity"], {"source": "user", "kind": "plan"})

    def test_init_out_records_export_dir(self):
        with helpers.temp_home() as env:
            export = Path(env["HOME"]) / "exp"
            session = helpers.make_session(env, "plan", out=str(export))
            recorded = _read_session(session)["export_dir"]
            self.assertEqual(recorded, os.path.abspath(export))
            helpers.make_session(env, "plan")
            self.assertEqual(_read_session(session)["export_dir"], recorded)

    def test_init_quick_deletes_checks(self):
        with helpers.temp_home() as env:
            session = helpers.make_session(env, "plan")
            (session / "factcheck.json").write_text("{}", encoding="utf-8")
            (session / "reader.json").write_text("{}", encoding="utf-8")
            checks = session / "checks"
            checks.mkdir()
            (checks / "dummy.txt").write_text("x", encoding="utf-8")
            data = _read_session(session)
            data["reader_grade"] = {"correct": 1, "total": 2, "report_sha256": "abc"}
            data["created_at"] = "2000-01-01T00:00:00Z"
            (session / "session.json").write_text(json.dumps(data), encoding="utf-8")
            helpers.make_session(env, "plan", mode="quick")
            after = _read_session(session)
            self.assertFalse((session / "factcheck.json").exists())
            self.assertFalse((session / "reader.json").exists())
            self.assertFalse(checks.exists())
        self.assertIsNone(after["reader_grade"])
        self.assertEqual(after["mode"], "quick")
        self.assertEqual(after["created_at"], "2000-01-01T00:00:00Z")

    def test_run_cli_config_uses_temp_home(self):
        code, out, err = helpers.run_cli("config")
        self.assertEqual(code, 0, err)
        path = json.loads(out)["path"]
        self.assertIn("pe-test-", path)
        self.assertTrue(path.endswith("/cfg/paseo-explain/config.json"))
        self.assertNotIn(str(Path.home() / ".config"), path)

    def test_run_cli_partial_override_keeps_temp_homes(self):
        override = "/tmp/pe-T1-att-T1-1-only-pe"
        code, out, err = helpers.run_cli("config", env={"PASEO_EXPLAIN_HOME": override})
        self.assertEqual(code, 0, err)
        path = json.loads(out)["path"]
        self.assertIn("pe-test-", path)
        self.assertTrue(path.endswith("/cfg/paseo-explain/config.json"))
        self.assertFalse(path.startswith(override))
        self.assertNotIn(str(Path.home() / ".config"), path)
        captured = {}

        def fake_run(cmd, capture_output=True, text=True, env=None, timeout=None):
            captured["env"] = env
            return subprocess.CompletedProcess(cmd, 0, stdout="{}\n", stderr="")

        with mock.patch("helpers.subprocess.run", fake_run):
            helpers.run_cli("config", env={"PASEO_EXPLAIN_HOME": override})
        child = captured["env"]
        self.assertEqual(child["PASEO_EXPLAIN_HOME"], override)
        self.assertIn("pe-test-", child["HOME"])
        self.assertIn("pe-test-", child["XDG_CONFIG_HOME"])
        self.assertEqual(Path(child["HOME"]), Path(child["XDG_CONFIG_HOME"]).parent)
        self.assertNotEqual(child["HOME"], str(Path.home()))
        self.assertEqual(child["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("PASEO_EXPLAIN_PASEO_BIN", child)

    def test_subcommand_help(self):
        self.assertEqual(len(SUBCOMMANDS), 22)
        for name in SUBCOMMANDS:
            code, out, err = helpers.run_cli(name, "--help")
            self.assertEqual(code, 0, (name, out, err))
            self.assertIn("usage", out.lower())

    def test_usage_error_is_argparse(self):
        code, out, err = helpers.run_cli("init")
        self.assertEqual(code, 2)
        self.assertIn("usage", err.lower())
        self.assertEqual(out, "")

    def test_cli_result_formatting(self):
        module = _load_cli()
        payload = {"ok": True, "msg": "café"}
        self.assertEqual(
            module.format_success(payload),
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        self.assertEqual(module.format_success("raw-text"), "raw-text")
        self.assertEqual(module.format_success(None), "")
        err = common.ExplainError(3, "missing paseo", extra={"fallback": "python3 serve"})
        body = json.loads(module.format_error(err))
        self.assertEqual(err.code, 3)
        self.assertEqual(str(err), "missing paseo")
        self.assertFalse(body["ok"])
        self.assertEqual(body["errors"], [{"path": "", "message": "missing paseo"}])
        self.assertEqual(body["fallback"], "python3 serve")
        self.assertIs(module.true_false("true"), True)
        self.assertIs(module.true_false("false"), False)
        with self.assertRaises(argparse.ArgumentTypeError):
            module.true_false("yes")


if __name__ == "__main__":
    unittest.main()
