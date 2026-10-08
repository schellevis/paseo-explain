"""End-to-end CLI workflows with isolated homes and a loopback Paseo double."""

import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

import helpers
from test_show import FAKE_BODY

sys.path.insert(0, str(helpers.REPO / "paseo-explain" / "scripts"))

from explainlib.serve import make_server


SUBCOMMANDS = (
    "init", "frame", "ingest", "survey-prepare", "survey-report", "validate",
    "check-prepare", "check-report", "apply-corrections", "skip", "grade", "md", "render", "serve",
    "show", "stop", "inject", "tab", "result", "config", "lint", "leaks",
)


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="pe-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {
            "HOME": str(self.root / "home"),
            "PASEO_EXPLAIN_HOME": str(self.root / "pe"),
            "XDG_CONFIG_HOME": str(self.root / "cfg"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def cli(self, *args):
        code, out, err = helpers.run_cli(*args, env=self.env)
        self.assertEqual(code, 0, f"explain.py {' '.join(map(str, args))}: exit {code}\nstdout: {out}\nstderr: {err}")
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            self.fail(f"invalid JSON from {args}: {exc}: {out}")

    def start_session(self, *, kind, mode="standard", export=False):
        args = ["init", "--mode", mode, "--levels", "3", "--lang", "en"]
        if kind == "plan":
            args += ["--autopilot", str(helpers.FIXTURES / "autopilot-run"), "--doc", "plan"]
        else:
            args += ["--kind", "idea", "--slug", "tool-shelf"]
        export_dir = self.root / "export"
        if export:
            args += ["--out", str(export_dir)]
        initialized = self.cli(*args)
        session = Path(initialized["session"])
        self.assertTrue(session.is_dir())
        self.assertEqual(initialized["mode"], mode)
        self.assertEqual(session.parent, self.root / "pe" / "sessions")
        self.assertEqual(json.loads((session / "session.json").read_text())["export_dir"], str(export_dir) if export else None)

        brief = self.cli(
            "frame", "--session", session,
            "--audience", "Neighbours at the community hall",
            "--question", "What changes for neighbours?",
            "--orchestrator-model", "claude/claude-example",
        )
        self.assertEqual(brief["orchestrator_model"], "claude/claude-example")
        if kind == "plan":
            ingested = self.cli(
                "ingest", "--session", session,
                "--autopilot", helpers.FIXTURES / "autopilot-run", "--doc", "plan",
            )
        else:
            ingested = self.cli(
                "ingest", "--session", session,
                "--text-file", helpers.FIXTURES / "idea.md",
            )
        self.assertTrue(ingested["ok"])
        self.assertGreater(ingested["fragments"], 0)
        fixture = helpers.FIXTURES / f"{kind}-explain.json"
        shutil.copyfile(fixture, session / "explain.json")
        validated = self.cli("validate", "--session", session)
        self.assertTrue(validated["ok"])
        self.assertTrue((session / "validate.json").is_file())
        return session

    def submit_factcheck(self, session, report):
        prepared = self.cli("check-prepare", "--session", session, "--kind", "factcheck")
        self.assertTrue(Path(prepared["request"]).is_file())
        request = json.loads(Path(prepared["request"]).read_text(encoding="utf-8"))
        report = copy.deepcopy(report)
        report["explain_sha256"] = request["explain_sha256"]
        report_path = self.root / "incoming-factcheck.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        accepted = self.cli(
            "check-report", "--session", session, "--kind", "factcheck", "--file", report_path,
        )
        self.assertTrue(accepted["ok"])
        self.assertEqual(json.loads((session / "factcheck.json").read_text()), report)
        return request

    def render_result(self, session):
        rendered = self.cli("render", "--session", session)
        self.assertTrue(rendered["ok"])
        self.assertEqual(rendered["round"], 1)
        for name in ("explain.html", "explain.md", "result.json"):
            self.assertTrue((session / name).is_file(), name)
        result = self.cli("result", "--session", session)
        self.assertEqual(result, json.loads((session / "result.json").read_text()))
        self.assertEqual(result["round"], 1)
        self.assertEqual(result["checks"]["validate"], "pass")
        self.assertEqual(result["models"]["orchestrator"], "claude/claude-example")
        return result

    def test_version_help_and_usage_error(self):
        code, out, err = helpers.run_cli("--version", env=self.env)
        self.assertEqual((code, out.strip(), err), (0, "paseo-explain 0.3.0", ""))
        for command in SUBCOMMANDS:
            with self.subTest(command=command):
                code, out, err = helpers.run_cli(command, "--help", env=self.env)
                self.assertEqual(code, 0, out + err)
                self.assertIn("usage:", out)
        code, out, err = helpers.run_cli("init", "--kind", "plan", env=self.env)
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("usage:", err)

    def test_codebase_end_to_end(self):
        import repo_helpers

        target = repo_helpers.copy_fixture(self.root / "repo")
        initialized = self.cli("init", "--repo", target, "--mode", "quick", "--levels", "3", "--lang", "en")
        self.assertEqual(initialized["depth"], "docs")
        session = Path(initialized["session"])
        self.cli("frame", "--session", session, "--audience", "A newcomer", "--question", "What is this?")
        ingested = self.cli("ingest", "--session", session)
        self.assertTrue(ingested["ok"])
        shutil.copyfile(helpers.FIXTURES / "codebase-explain.json", session / "explain.json")
        self.assertTrue(self.cli("validate", "--session", session)["ok"])
        rendered = self.cli("render", "--session", session)
        self.assertTrue(rendered["ok"])
        for name in ("explain.html", "explain.md", "result.json"):
            self.assertTrue((session / name).is_file(), name)
        code, text, err = helpers.run_cli("md", "--session", session, "--level", "1", env=self.env)
        self.assertEqual(code, 0, err)
        self.assertIn("Based on documentation; code not read", text)
        self.assertIn("| Path | Role |", text)
        result = self.cli("result", "--session", session)
        self.assertEqual(result["kind"], "codebase")
        self.assertIsNone(result["doc"])
        self.assertEqual(result["version"], "0.3.0")
        self.assertEqual(result["checks"]["validate"], "pass")

    def test_serve_accepts_all_interfaces_ipv4(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, **self.env)
        process = subprocess.Popen(
            [sys.executable, str(helpers.SCRIPT), "serve", "--root", str(self.root),
             "--port", str(port), "--host", "0.0.0.0"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    stdout, stderr = process.communicate(timeout=1)
                    self.fail(f"serve exited {process.returncode}: {stdout}\n{stderr}")
                try:
                    with urlopen(f"http://127.0.0.1:{port}/", timeout=0.2) as response:
                        self.assertEqual(response.status, 200)
                    break
                except (OSError, URLError):
                    time.sleep(0.05)
            else:
                self.fail("serve did not answer HTTP 200 within five seconds")
        finally:
            process.terminate()
            stdout, stderr = process.communicate(timeout=5)
        self.assertIn(f"serving on 0.0.0.0:{port}, reachable from the network", stderr)

    def test_serve_rejects_hostname_without_traceback(self):
        code, out, err = helpers.run_cli(
            "serve", "--root", self.root, "--port", "0", "--host", "example.com", env=self.env,
        )
        self.assertIn(code, (1, 2))
        self.assertNotIn("Traceback", out + err)
        self.assertTrue("usage:" in err or "errors" in json.loads(out))

    def test_plan_factcheck_corrections_end_to_end(self):
        session = self.start_session(kind="plan")
        fixture = json.loads((helpers.FIXTURES / "factcheck.json").read_text(encoding="utf-8"))
        request = self.submit_factcheck(session, fixture)
        self.assertEqual(len(request["leaves"]), len(fixture["claims"]))
        unsupported = [claim["ref"] for claim in fixture["claims"] if claim["verdict"] == "unsupported"]
        units = sorted({ref.rsplit("/", 1)[0] for ref in unsupported})
        self.assertEqual(len(units), 2)
        applied = self.cli(
            "apply-corrections", "--session", session,
            "--remove", units[0], "--remove", units[1],
        )
        self.assertEqual((applied["corrected"], applied["removed"]), (1, 2))
        result = self.render_result(session)
        self.assertEqual(result["kind"], "plan")
        self.assertEqual(result["doc"], "plan")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checks"]["fact_check"], "pass")
        self.assertEqual(result["checks"]["fact_check_corrections"], 3)
        self.assertEqual(result["checks"]["check_label"], "independent_corrected")

    def test_idea_quick_end_to_end(self):
        session = self.start_session(kind="idea", mode="quick")
        result = self.render_result(session)
        self.assertEqual(result["kind"], "idea")
        self.assertEqual(result["mode"], "quick")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checks"]["fact_check"], "skipped")
        self.assertEqual(result["checks"]["check_label"], "not_checked")

    def test_idea_standard_verified_factcheck(self):
        session = self.start_session(kind="idea")
        prepared = self.cli("check-prepare", "--session", session, "--kind", "factcheck")
        request = json.loads(Path(prepared["request"]).read_text(encoding="utf-8"))
        report = {
            "explain_report": 1,
            "kind": "factcheck",
            "model": "codex/gpt-example",
            "explain_sha256": request["explain_sha256"],
            "claims": [
                {"ref": ref, "claim": "This leaf matches the evidence.", "verdict": "verified", "evidence": ["E1"], "correction": None}
                for ref in request["leaves"]
            ],
            "plan_checks": [],
            "summary": "Every leaf is verified.",
        }
        report_path = self.root / "idea-factcheck.json"
        report_path.write_text(json.dumps(report), encoding="utf-8")
        checked = self.cli("check-report", "--session", session, "--kind", "factcheck", "--file", report_path)
        self.assertTrue(checked["ok"])
        result = self.render_result(session)
        self.assertEqual(result["kind"], "idea")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["checks"]["fact_check"], "pass")
        self.assertTrue(result["checks"]["independent"])
        self.assertEqual(result["checks"]["check_label"], "independent")

    def test_export_stays_equal_through_show_tab_result(self):
        session = self.start_session(kind="idea", mode="quick", export=True)
        export = self.root / "export"

        def assert_export_equal():
            self.assertEqual((session / "result.json").read_bytes(), (export / "result.json").read_bytes())

        self.render_result(session)
        assert_export_equal()
        for name in ("explain.html", "explain.md"):
            self.assertEqual((session / name).read_bytes(), (export / name).read_bytes())

        server = make_server(self.root / "pe" / "sessions", "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        fake_dir = self.root / "bin"
        fake_dir.mkdir()
        fake = fake_dir / "paseo"
        fake.write_text("#!" + sys.executable + "\n" + FAKE_BODY, encoding="utf-8")
        fake.chmod(0o755)
        (fake_dir / "state.json").write_text(json.dumps({
            "registered": True,
            "project_exists": True,
            "running": True,
            "port": server.server_address[1],
            "local": "http://explain.localhost",
            "public": "https://example.invalid/explain",
        }), encoding="utf-8")
        self.env["PASEO_EXPLAIN_PASEO_BIN"] = str(fake)

        shown = self.cli("show", "--session", session)
        self.assertTrue(shown["ok"])
        self.assertEqual(shown["local_url"], f"http://explain.localhost/{session.name}/")
        self.assertEqual(shown["public_url"], f"https://example.invalid/explain/{session.name}/")
        stored_session = json.loads((session / "session.json").read_text())
        self.assertEqual(stored_session["urls"], {"local": shown["local_url"], "public": shown["public_url"]})
        self.assertEqual(json.loads((session / "result.json").read_text())["urls"], stored_session["urls"])
        assert_export_equal()

        tabbed = self.cli("tab", "--session", session, "--opened", "true")
        self.assertTrue(tabbed["tab_opened"])
        self.assertTrue(json.loads((session / "session.json").read_text())["tab_opened"])
        assert_export_equal()
        final = self.cli("result", "--session", session)
        self.assertEqual(final["urls"], stored_session["urls"])
        self.assertTrue(final["tab_opened"])
        self.assertEqual(final, json.loads((session / "result.json").read_text()))
        assert_export_equal()

    def test_repository_lint_and_leaks(self):
        for command in ("lint", "leaks"):
            with self.subTest(command=command):
                args = (command, "--repo") if command == "leaks" else (command,)
                code, out, err = helpers.run_cli(*args, env=self.env)
                self.assertEqual(code, 0, f"{command}: {out}\n{err}")
                report = json.loads(out)
                self.assertTrue(report["ok"], f"{command} errors: {report.get('errors') or report.get('findings')}")


if __name__ == "__main__":
    unittest.main()
