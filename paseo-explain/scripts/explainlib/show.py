"""Show and stop the explain page through a Paseo workspace service."""

import http.client
import importlib
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from explainlib.common import ExplainError, load_session, save_session, server_dir, sessions_root, write_json


def _explain_py() -> str:
    return os.path.abspath(Path(__file__).resolve().parents[1] / "explain.py")


def _fallback_command() -> str:
    root = os.path.abspath(sessions_root())
    return f"python3 {_explain_py()} serve --root {root} --port 8765"


def paseo_bin():
    override = os.environ.get("PASEO_EXPLAIN_PASEO_BIN")
    if override is not None:
        return override if os.path.isfile(override) else None
    return shutil.which("paseo")


def run_paseo(args):
    binary = paseo_bin()
    if not binary:
        raise ExplainError(3, "paseo CLI not found", extra={"fallback": _fallback_command()})
    try:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise ExplainError(3, "paseo command timed out") from exc
    if proc.returncode == 0:
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise ExplainError(3, "paseo returned invalid JSON") from exc
    for output in (proc.stdout, proc.stderr):
        if not output:
            continue
        try:
            return json.loads(output)
        except json.JSONDecodeError:
            pass
    detail = proc.stderr.strip() or f"paseo exited {proc.returncode}"
    raise ExplainError(3, detail)


def _server_path(directory=None) -> str:
    return os.path.abspath(directory if directory is not None else server_dir())


def _paseo_error(data):
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("code"):
            return err
    return None


def _raise_error(err):
    raise ExplainError(3, f"{err.get('code', '')}: {err.get('message', '')}")


def ensure_server_project() -> Path:
    directory = Path(_server_path())
    directory.mkdir(parents=True, exist_ok=True)
    command = (
        f"python3 {shlex.quote(_explain_py())} serve --root "
        f"{shlex.quote(os.path.abspath(sessions_root()))} --port $PASEO_PORT"
    )
    desired = {"scripts": {"explain": {"type": "service", "command": command}}}
    path = directory / "paseo.json"
    if path.is_file():
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if current == desired:
            return directory
    write_json(path, desired)
    return directory


def _explain_entry(data):
    rows = data if isinstance(data, list) else None
    if rows is None and isinstance(data, dict) and data.get("scriptName"):
        rows = [data]
    if not rows:
        raise ExplainError(3, "explain script entry missing")
    for item in rows:
        if isinstance(item, dict) and item.get("scriptName") == "explain":
            return item
    raise ExplainError(3, "explain script entry missing")


def _port_ready(port) -> bool:
    return isinstance(port, int) and not isinstance(port, bool)


def _ready(entry) -> bool:
    return entry.get("lifecycle") == "running" and _port_ready(entry.get("port"))


def _script_ls(cwd):
    return run_paseo(["script", "ls", "--cwd", cwd, "--json"])


def _project_registered(projects, cwd) -> bool:
    server_real = os.path.realpath(cwd)
    for project in projects:
        if not isinstance(project, dict):
            continue
        path = project.get("path")
        if isinstance(path, str) and os.path.realpath(path) == server_real:
            return True
    return False


def _poll_running(cwd):
    deadline = time.monotonic() + 10
    while True:
        data = _script_ls(cwd)
        err = _paseo_error(data)
        if err:
            _raise_error(err)
        entry = _explain_entry(data)
        if _ready(entry):
            return entry
        if time.monotonic() >= deadline:
            raise ExplainError(3, "explain service did not become running")
        time.sleep(0.5)


def _ensure_service(directory, *, start: bool):
    cwd = _server_path(directory)
    workspace_id = None
    data = _script_ls(cwd)
    err = _paseo_error(data)
    if err:
        if err.get("code") != "WORKSPACE_NOT_FOUND":
            _raise_error(err)
        if not start:
            _raise_error(err)
        projects = run_paseo(["project", "ls", "--json"])
        perr = _paseo_error(projects)
        if perr:
            _raise_error(perr)
        if not isinstance(projects, list):
            raise ExplainError(3, "paseo project ls returned unexpected output")
        if not _project_registered(projects, cwd):
            created = run_paseo(["project", "create", cwd, "--json"])
            cerr = _paseo_error(created)
            if cerr:
                _raise_error(cerr)
        created_ws = run_paseo(
            [
                "workspace",
                "create",
                "--isolation",
                "local",
                "--path",
                cwd,
                "--title",
                "paseo-explain",
                "--json",
            ]
        )
        werr = _paseo_error(created_ws)
        if werr:
            _raise_error(werr)
        if isinstance(created_ws, dict) and isinstance(created_ws.get("workspaceId"), str):
            workspace_id = created_ws["workspaceId"]
        data = _script_ls(cwd)
        err = _paseo_error(data)
        if err:
            _raise_error(err)
    entry = _explain_entry(data)
    if not _ready(entry):
        if not start:
            raise ExplainError(3, "explain service is not running")
        started = run_paseo(["script", "start", "explain", "--cwd", cwd, "--json"])
        serr = _paseo_error(started)
        if serr:
            _raise_error(serr)
        entry = _poll_running(cwd)
    return entry, workspace_id


def ensure_service(directory, *, start: bool) -> dict:
    entry, _workspace_id = _ensure_service(directory, start=start)
    return entry


def _check_page(port: int, slug: str) -> None:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=5)
        try:
            conn.request("GET", f"/{slug}/")
            response = conn.getresponse()
            body = response.read()
            status = response.status
        finally:
            conn.close()
    except (OSError, http.client.HTTPException, TimeoutError) as exc:
        raise ExplainError(3, f"explain page check failed: {exc}") from exc
    if status != 200 or b'name="paseo-explain"' not in body:
        raise ExplainError(3, "explain page check failed")


def _urls(entry, slug: str):
    local_base = entry.get("localProxyUrl")
    if not isinstance(local_base, str) or local_base == "":
        raise ExplainError(3, "localProxyUrl missing")
    local_url = local_base.rstrip("/") + "/" + slug + "/"
    public = entry.get("publicProxyUrl")
    if isinstance(public, str) and public != "":
        public_url = public.rstrip("/") + "/" + slug + "/"
    else:
        public_url = None
    return local_url, public_url


def show(session_dir, *, start: bool = True) -> dict:
    session_dir = os.fspath(session_dir)
    session = load_session(session_dir)
    slug = session["slug"]
    server = ensure_server_project()
    entry, workspace_id = _ensure_service(server, start=start)
    _check_page(entry["port"], slug)
    local_url, public_url = _urls(entry, slug)
    session["urls"] = {"local": local_url, "public": public_url}
    save_session(session_dir, session)
    result = importlib.import_module("explainlib.result")
    result.write_result(session_dir)
    return {
        "ok": True,
        "local_url": local_url,
        "public_url": public_url,
        "port": entry["port"],
        "workspace_id": workspace_id,
    }


def stop() -> dict:
    data = run_paseo(["script", "stop", "explain", "--cwd", _server_path(), "--json"])
    err = _paseo_error(data)
    if err:
        if err.get("code") == "WORKSPACE_NOT_FOUND":
            return {"ok": True, "stopped": False}
        _raise_error(err)
    return {"ok": True, "stopped": True}
