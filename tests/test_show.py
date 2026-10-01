"""Paseo scratch project, show/stop, and the result-module double."""

import contextlib
import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers

sys.path.insert(0, str(helpers.REPO / "paseo-explain" / "scripts"))

from explainlib import serve
from explainlib.common import ExplainError

FAKE_BODY = r"""
import json
import sys
from pathlib import Path

root = Path(sys.argv[0]).resolve().parent
log_path = root / "log.jsonl"
state_path = root / "state.json"
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\n")
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]


def emit(obj, code=0):
    stream = sys.stderr if code and not state.get("stdout_error") else sys.stdout
    stream.write(json.dumps(obj))
    raise SystemExit(code)


def save():
    state_path.write_text(json.dumps(state), encoding="utf-8")


def flag(name):
    return args[args.index(name) + 1]


def entry():
    running = bool(state.get("running"))
    public = state.get("public")
    local = state.get("local") or "http://explain.localhost"
    return {
        "scriptName": "explain",
        "type": "service",
        "hostname": "explain.localhost",
        "port": state.get("port") if running else None,
        "localProxyUrl": local,
        "publicProxyUrl": public if isinstance(public, str) and public != "" else None,
        "proxyUrl": local,
        "lifecycle": "running" if running else "stopped",
        "health": "healthy" if running else None,
        "exitCode": None,
        "terminalId": None,
    }


def not_found(directory):
    emit(
        {
            "error": {
                "code": "WORKSPACE_NOT_FOUND",
                "message": f"No Paseo workspace found for {directory}",
                "details": "Open the directory in Paseo first, or pass --workspace <workspace-id>.",
            }
        },
        1,
    )


if state.get("force_error"):
    err = state["force_error"]
    emit({"error": {"code": err["code"], "message": err["message"]}}, 1)

if args[:2] == ["script", "ls"]:
    if not state.get("registered"):
        not_found(flag("--cwd"))
    emit([entry()])
elif args[:2] == ["project", "ls"]:
    if state.get("project_exists"):
        emit(
            [
                {
                    "projectId": state.get("projectId", "prj_existing"),
                    "name": "server",
                    "kind": "non_git",
                    "path": state["path"],
                }
            ]
        )
    emit([])
elif args[:2] == ["project", "create"]:
    path = args[2]
    state["project_exists"] = True
    state["path"] = path
    state["projectId"] = "prj_created"
    save()
    emit({"projectId": "prj_created", "name": "server", "kind": "non_git", "path": path})
elif args[:2] == ["workspace", "create"]:
    path = flag("--path")
    state["registered"] = True
    state["workspaceId"] = "wks_created"
    save()
    emit(
        {
            "workspaceId": "wks_created",
            "project": state.get("projectId", "prj_created"),
            "name": "paseo-explain",
            "isolation": "local",
            "cwd": path,
        }
    )
elif args[:3] == ["script", "start", "explain"]:
    state["running"] = True
    save()
    emit(entry())
elif args[:3] == ["script", "stop", "explain"]:
    if not state.get("registered"):
        not_found(flag("--cwd"))
    state["running"] = False
    save()
    emit(entry())
else:
    emit({"error": {"code": "UNKNOWN", "message": " ".join(args)}}, 1)
"""


class ResultDouble:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def write_result(self, session_dir):
        urls = json.loads((Path(session_dir) / "session.json").read_text(encoding="utf-8"))["urls"]
        self.calls.append(urls)
        if self.fail:
            raise ExplainError(3, "writer failed")
        return {"ok": True}


@contextlib.contextmanager
def result_double(double):
    import explainlib

    with mock.patch.dict(sys.modules, {"explainlib.result": double}), mock.patch.object(
        explainlib, "result", double, create=True
    ):
        yield double


class World:
    def __init__(self, *, spaced_home=False):
        self._tmp = tempfile.TemporaryDirectory(prefix="pe-test-")
        base = Path(self._tmp.name)
        self.home = base / "home"
        self.pe = base / ("pe home" if spaced_home else "pe")
        self.cfg = base / "cfg"
        self.fake_root = base / "bin"
        self.fake_root.mkdir(parents=True)
        self.fake = self.fake_root / "paseo"
        self.fake.write_text("#!" + sys.executable + "\n" + FAKE_BODY, encoding="utf-8")
        self.fake.chmod(0o755)
        self.env_overlay = {
            "HOME": str(self.home),
            "PASEO_EXPLAIN_HOME": str(self.pe),
            "XDG_CONFIG_HOME": str(self.cfg),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PASEO_EXPLAIN_PASEO_BIN": str(self.fake),
        }
        self.session = None
        self.http = None
        self.thread = None
        self.port = None

    def close(self):
        if self.http is not None:
            self.http.shutdown()
            self.http.server_close()
            self.http = None
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        self._tmp.cleanup()

    def spawn_env(self, **extra):
        env = os.environ.copy()
        env.update(self.env_overlay)
        env.update(extra)
        return env

    def server_path(self):
        return os.path.abspath(self.pe / "server")

    def sessions_path(self):
        return os.path.abspath(self.pe / "sessions")

    def explain_py(self):
        return os.path.abspath(helpers.SCRIPT)

    def write_state(self, **kwargs):
        state = {
            "registered": False,
            "project_exists": False,
            "running": False,
            "port": self.port,
            "public": None,
            "local": "http://explain.localhost",
        }
        state.update(kwargs)
        (self.fake_root / "state.json").write_text(json.dumps(state), encoding="utf-8")

    def log(self):
        path = self.fake_root / "log.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def init(self, slug="demo", html=True):
        proc = subprocess.run(
            [sys.executable, str(helpers.SCRIPT), "init", "--kind", "idea", "--slug", slug],
            capture_output=True,
            text=True,
            env=self.spawn_env(),
            timeout=30,
        )
        if proc.returncode != 0:
            raise AssertionError(f"init failed: {proc.returncode} {proc.stdout} {proc.stderr}")
        self.session = Path(json.loads(proc.stdout)["session"])
        if html:
            (self.session / "explain.html").write_text(
                '<meta name="paseo-explain" content="0.2.1">\n',
                encoding="utf-8",
            )
        return self.session

    def start_http(self):
        root = self.session.parent if self.session is not None else self.sessions_path()
        self.http = serve.make_server(root, "127.0.0.1", 0)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.http.server_address[1]
        return self.port

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(helpers.SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.spawn_env(),
            timeout=30,
        )


@contextlib.contextmanager
def world(*, spaced_home=False):
    item = World(spaced_home=spaced_home)
    try:
        yield item
    finally:
        item.close()


@contextlib.contextmanager
def applied(env):
    previous = os.environ.copy()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _show():
    from explainlib import show

    return show


class ShowTests(unittest.TestCase):
    def test_fixed_port_and_host_service_config(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(registered=True, running=True)
            cfg = item.cfg / "paseo-explain" / "config.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"serve_port": 8300, "serve_host": "0.0.0.0"}), encoding="utf-8")
            with applied(item.env_overlay), result_double(ResultDouble()):
                _show().show(item.session)
            command = (f"python3 {shlex.quote(item.explain_py())} serve --root "
                       f"{shlex.quote(item.sessions_path())} --port 8300 --host 0.0.0.0")
            self.assertEqual(
                json.loads((Path(item.server_path()) / "paseo.json").read_text(encoding="utf-8")),
                {"scripts": {"explain": {"type": "service", "command": command, "port": 8300}}},
            )

    def test_host_only_keeps_paseo_port(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(registered=True, running=True)
            cfg = item.cfg / "paseo-explain" / "config.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"serve_host": "::1"}), encoding="utf-8")
            with applied(item.env_overlay), result_double(ResultDouble()):
                _show().show(item.session)
            entry = json.loads((Path(item.server_path()) / "paseo.json").read_text(encoding="utf-8"))["scripts"]["explain"]
            self.assertEqual(entry["command"],
                             f"python3 {shlex.quote(item.explain_py())} serve --root "
                             f"{shlex.quote(item.sessions_path())} --port $PASEO_PORT --host ::1")
            self.assertNotIn("port", entry)

    def test_fixed_port_uses_default_loopback_host(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(registered=True, running=True)
            cfg = item.cfg / "paseo-explain" / "config.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"serve_port": 8300}), encoding="utf-8")
            with applied(item.env_overlay), result_double(ResultDouble()):
                _show().show(item.session)
            entry = json.loads((Path(item.server_path()) / "paseo.json").read_text(encoding="utf-8"))["scripts"]["explain"]
            self.assertEqual(entry["port"], 8300)
            self.assertTrue(entry["command"].endswith("--port 8300 --host 127.0.0.1"))

    def test_public_base_url_precedes_proxy_url(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(registered=True, running=True, public="https://proxy.example/base")
            cfg = item.cfg / "paseo-explain" / "config.json"
            cfg.parent.mkdir(parents=True)
            cfg.write_text(json.dumps({"public_base_url": "http://my-host:8300/base/"}), encoding="utf-8")
            with applied(item.env_overlay), result_double(ResultDouble()):
                result = _show().show(item.session)
            self.assertEqual(result["public_url"], "http://my-host:8300/base/demo/")

    def test_fake_shebang_and_mode(self):
        with world() as item:
            first = item.fake.read_text(encoding="utf-8").splitlines()[0]
            self.assertEqual(first, "#!" + sys.executable)
            self.assertTrue(item.fake.stat().st_mode & stat.S_IXUSR)

    def test_first_time_sequence_public_null_and_paseo_json(self):
        with world(spaced_home=True) as item:
            item.init()
            item.start_http()
            item.write_state(public=None, local="http://explain.localhost")
            double = ResultDouble()
            with applied(item.env_overlay), result_double(double):
                result = _show().show(item.session)
            server = item.server_path()
            self.assertEqual(
                item.log(),
                [
                    ["script", "ls", "--cwd", server, "--json"],
                    ["project", "ls", "--json"],
                    ["project", "create", server, "--json"],
                    [
                        "workspace",
                        "create",
                        "--isolation",
                        "local",
                        "--path",
                        server,
                        "--title",
                        "paseo-explain",
                        "--json",
                    ],
                    ["script", "ls", "--cwd", server, "--json"],
                    ["script", "start", "explain", "--cwd", server, "--json"],
                    ["script", "ls", "--cwd", server, "--json"],
                ],
            )
            command = (
                f"python3 {shlex.quote(item.explain_py())} serve --root "
                f"{shlex.quote(item.sessions_path())} --port $PASEO_PORT"
            )
            self.assertIn(" ", item.sessions_path())
            self.assertEqual(
                json.loads((Path(server) / "paseo.json").read_text(encoding="utf-8")),
                {"scripts": {"explain": {"type": "service", "command": command}}},
            )
            expected = json.dumps(
                {"scripts": {"explain": {"type": "service", "command": command}}},
                ensure_ascii=False, indent=2,
            ) + "\n"
            self.assertEqual((Path(server) / "paseo.json").read_text(encoding="utf-8"), expected)
            urls = {"local": "http://explain.localhost/demo/", "public": None}
            self.assertEqual(result["ok"], True)
            self.assertEqual(result["local_url"], urls["local"])
            self.assertIsNone(result["public_url"])
            self.assertEqual(result["port"], item.port)
            self.assertEqual(result["workspace_id"], "wks_created")
            self.assertEqual(double.calls, [urls])
            stored = json.loads((item.session / "session.json").read_text(encoding="utf-8"))["urls"]
            self.assertEqual(stored, urls)
            paseo_json = Path(server) / "paseo.json"
            stamp = paseo_json.stat().st_mtime_ns
            os.utime(paseo_json, ns=(stamp - 5_000_000_000, stamp - 5_000_000_000))
            stamped = paseo_json.stat().st_mtime_ns
            with applied(item.env_overlay), result_double(double):
                again = _show().show(item.session)
            self.assertEqual(paseo_json.stat().st_mtime_ns, stamped)
            self.assertIsNone(again["workspace_id"])
            self.assertEqual(item.log()[7], ["script", "ls", "--cwd", server, "--json"])
            self.assertEqual(len(double.calls), 2)

    def test_project_already_exists_skips_create(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(project_exists=True, path=item.server_path(), projectId="prj_existing")
            double = ResultDouble()
            with applied(item.env_overlay), result_double(double):
                result = _show().show(item.session)
            ops = [row[:2] for row in item.log()]
            self.assertEqual(
                ops,
                [
                    ["script", "ls"],
                    ["project", "ls"],
                    ["workspace", "create"],
                    ["script", "ls"],
                    ["script", "start"],
                    ["script", "ls"],
                ],
            )
            self.assertNotIn(["project", "create"], ops)
            self.assertEqual(result["workspace_id"], "wks_created")
            self.assertEqual(double.calls[0]["public"], None)

    def test_running_service_skips_start_and_public_url(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(
                registered=True,
                project_exists=True,
                path=item.server_path(),
                running=True,
                local="http://explain.localhost/",
                public="https://phone.example/base/",
            )
            double = ResultDouble()
            with applied(item.env_overlay), result_double(double):
                result = _show().show(item.session, start=True)
            self.assertEqual([row[:2] for row in item.log()], [["script", "ls"]])
            self.assertEqual(result["local_url"], "http://explain.localhost/demo/")
            self.assertEqual(result["public_url"], "https://phone.example/base/demo/")
            self.assertIsNone(result["workspace_id"])
            self.assertEqual(result["port"], item.port)
            self.assertEqual(len(double.calls), 1)
            self.assertEqual(double.calls[0]["public"], result["public_url"])

    def test_no_start_when_already_running(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(registered=True, running=True, public=None)
            double = ResultDouble()
            with applied(item.env_overlay), result_double(double):
                result = _show().show(item.session, start=False)
            self.assertEqual([row[:2] for row in item.log()], [["script", "ls"]])
            self.assertTrue(result["ok"])
            self.assertIsNone(result["workspace_id"])

    def test_writer_failure_propagates_after_urls(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(registered=True, running=True)
            double = ResultDouble(fail=True)
            with applied(item.env_overlay), result_double(double):
                with self.assertRaises(ExplainError) as caught:
                    returned = _show().show(item.session)
                    self.fail(f"show returned {returned}")
            self.assertEqual(caught.exception.code, 3)
            self.assertEqual(str(caught.exception), "writer failed")
            self.assertEqual(len(double.calls), 1)
            stored = json.loads((item.session / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["urls"]["local"], "http://explain.localhost/demo/")

    def test_nonrecoverable_error_exits_3(self):
        with world() as item:
            item.init(html=False)
            item.write_state(
                force_error={"code": "DAEMON_UNAVAILABLE", "message": "daemon down"}
            )
            proc = item.cli("show", "--session", str(item.session))
            self.assertEqual(proc.returncode, 3)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("DAEMON_UNAVAILABLE", payload["errors"][0]["message"])
            self.assertIn("daemon down", payload["errors"][0]["message"])
            self.assertEqual([row[:2] for row in item.log()], [["script", "ls"]])

    def test_page_check_404_exits_3(self):
        with world() as item:
            item.init(html=False)
            item.start_http()
            item.write_state(registered=True, running=True)
            proc = item.cli("show", "--session", str(item.session))
            self.assertEqual(proc.returncode, 3)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])
            self.assertIn("page check", payload["errors"][0]["message"])
            stored = json.loads((item.session / "session.json").read_text(encoding="utf-8"))
            self.assertIsNone(stored["urls"])
            self.assertEqual([row[:2] for row in item.log()], [["script", "ls"]])

    def test_no_start_stopped_exits_3_without_create(self):
        with world() as item:
            item.init(html=False)
            item.write_state(registered=True, running=False, project_exists=False)
            proc = item.cli("show", "--session", str(item.session), "--no-start")
            self.assertEqual(proc.returncode, 3)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])
            joined = " ".join(" ".join(row) for row in item.log())
            self.assertNotIn("start", joined)
            self.assertNotIn("create", joined)
            self.assertEqual(item.log()[0][:2], ["script", "ls"])

    def test_missing_cli_fallback(self):
        with world() as item:
            item.init(html=False)
            missing = str(item.fake_root / "missing-paseo")
            empty = item.fake_root / "empty-path"
            empty.mkdir()
            env = item.spawn_env(PASEO_EXPLAIN_PASEO_BIN=missing, PATH=str(empty))
            proc = subprocess.run(
                [sys.executable, str(helpers.SCRIPT), "show", "--session", str(item.session)],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 3)
            payload = json.loads(proc.stdout)
            self.assertFalse(payload["ok"])
            self.assertEqual(
                payload["fallback"],
                f"python3 {item.explain_py()} serve --root {item.sessions_path()} --port 8765",
            )
            self.assertNotIn("fallback", payload["errors"][0])
            with applied(env):
                self.assertIsNone(_show().paseo_bin())
                with self.assertRaises(ExplainError) as caught:
                    _show().run_paseo(["script", "ls", "--json"])
            self.assertEqual(caught.exception.code, 3)
            self.assertEqual(caught.exception.extra["fallback"], payload["fallback"])

    def test_stop_registered(self):
        with world() as item:
            item.write_state(registered=True, running=True, port=9)
            proc = item.cli("stop")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout), {"ok": True, "stopped": True})
            self.assertEqual(item.log()[0][:4], ["script", "stop", "explain", "--cwd"])
            self.assertFalse(json.loads((item.fake_root / "state.json").read_text(encoding="utf-8"))["running"])

    def test_stop_unregistered(self):
        with world() as item:
            item.write_state(registered=False, running=False)
            proc = item.cli("stop")
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(json.loads(proc.stdout), {"ok": True, "stopped": False})

    def test_stdout_error_json_is_still_returned(self):
        with world() as item:
            item.write_state(
                stdout_error=True,
                force_error={"code": "DAEMON_UNAVAILABLE", "message": "daemon down"},
            )
            with applied(item.env_overlay):
                result = _show().run_paseo(["script", "ls", "--json"])
            self.assertEqual(
                result,
                {"error": {"code": "DAEMON_UNAVAILABLE", "message": "daemon down"}},
            )

    def test_helpers_return_shapes(self):
        with world() as item:
            item.init()
            item.start_http()
            item.write_state(registered=True, running=True)
            with applied(item.env_overlay):
                show = _show()
                self.assertEqual(show.paseo_bin(), str(item.fake))
                directory = show.ensure_server_project()
                self.assertEqual(os.path.abspath(directory), item.server_path())
                entry = show.ensure_service(directory, start=True)
            self.assertEqual(entry["scriptName"], "explain")
            self.assertEqual(entry["lifecycle"], "running")
            self.assertEqual(entry["port"], item.port)
            self.assertIsNone(entry["publicProxyUrl"])
            self.assertEqual([row[:2] for row in item.log()], [["script", "ls"]])


if __name__ == "__main__":
    unittest.main()
