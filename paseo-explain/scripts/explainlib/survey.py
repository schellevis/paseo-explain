"""Surveyor requests and reports for one area of a repository."""

import json
import os
import re
from pathlib import Path

from explainlib import REPORT_SCHEMA
from explainlib.common import (
    ExplainError,
    load_session,
    now_iso,
    read_json,
    sha256_bytes,
    write_json,
)
from explainlib.repo import (
    area_file_map,
    area_state,
    load_repomap,
    write_repomap,
)
from explainlib.scan import scan_text
from explainlib.secretscan import find_secrets_in

SURVEYS_DIR = "surveys"
_AREA_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_REQUIRED = ("explain_report", "kind", "model", "area", "files_sha256", "summary", "components")
_OPTIONAL = ("flows", "entrypoints", "ranges")
_COMPONENT_KEYS = ("name", "paths", "role")
_FLOW_KEYS = ("from", "to", "text")
_ENTRYPOINT_KEYS = ("path", "line", "text")
_RANGE_KEYS = ("path", "start", "end", "why")


def _error(path, message):
    return {"path": path, "message": message}


def _ptr(base, token):
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    return f"{base}/{escaped}"


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _session(session_dir):
    try:
        return load_session(session_dir)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None


def _surveys(session_dir) -> Path:
    return Path(os.path.abspath(os.fspath(session_dir))) / SURVEYS_DIR


def _check_area_id(area) -> None:
    if not isinstance(area, str) or not _AREA_RE.match(area):
        raise ExplainError(1, "unknown area")


def _under_surveys(session_dir, path) -> bool:
    """True when the entry itself (not a symlink target) lies in <session>/surveys/."""
    folder = os.path.realpath(os.path.dirname(os.path.abspath(os.fspath(path))))
    return folder == os.path.realpath(os.fspath(_surveys(session_dir)))


def _discard(session_dir, path) -> None:
    if _under_surveys(session_dir, path):
        try:
            os.unlink(os.fspath(path))
        except OSError:
            pass


def prepare(session_dir, area) -> dict:
    session = _session(session_dir)
    if session.get("kind") != "codebase":
        raise ExplainError(1, "survey needs a codebase session")
    if session.get("depth") != "code":
        raise ExplainError(1, "survey needs depth code")
    repomap = load_repomap(session_dir)
    if repomap is None:
        raise ExplainError(1, "repomap.json is missing; run ingest")
    _check_area_id(area)
    entry = next((a for a in repomap.get("areas", []) if isinstance(a, dict) and a.get("id") == area), None)
    if entry is None:
        raise ExplainError(1, "unknown area")
    members = area_file_map({"files": repomap.get("files", [])}).get(area, [])
    files = [
        {"path": f["path"], "lines": f["lines"], "sha256": f["sha256"]}
        for f in members
        if f.get("kind") == "text"
    ]
    surveys = _surveys(session_dir)
    request_path = surveys / f"{area}-request.json"
    report_path = surveys / f"{area}.json"
    request = {
        "explain_report": REPORT_SCHEMA,
        "kind": "survey",
        "area": area,
        "root": session.get("identity", {}).get("repo"),
        "files": files,
        "files_sha256": entry.get("files_sha256"),
        "created_at": now_iso(),
    }
    write_json(request_path, request)
    if report_path.is_symlink() or report_path.is_file():
        report_path.unlink()
    return {"ok": True, "request": str(request_path), "report": str(report_path), "files": len(files)}


def _text(value, pointer, limit, errors) -> None:
    if not isinstance(value, str):
        errors.append(_error(pointer, "expected a string"))
    elif not value:
        errors.append(_error(pointer, "must not be empty"))
    elif len(value) > limit:
        errors.append(_error(pointer, f"longer than {limit} characters"))


def _keys(obj, pointer, required, errors, optional=()) -> None:
    for key in required:
        if key not in obj:
            errors.append(_error(pointer, f"missing key {key}"))
    for key in obj:
        if key not in required and key not in optional:
            errors.append(_error(pointer, f"unknown key in {pointer or '/'}"))


def _items(report, key, limit, errors, minimum=0):
    """Return the list under key (checked for type and size) or None."""
    value = report.get(key, [])
    pointer = "/" + key
    if not isinstance(value, list):
        errors.append(_error(pointer, "expected a list"))
        return None
    if len(value) < minimum:
        errors.append(_error(pointer, f"needs at least {minimum} item" + ("s" if minimum > 1 else "")))
    if len(value) > limit:
        errors.append(_error(pointer, f"more than {limit} items"))
    return value


def _file_path(value, pointer, lines_by_path, errors):
    """Check a request file path; return its line count or None."""
    if not isinstance(value, str) or value not in lines_by_path:
        errors.append(_error(pointer, "not a file of the request" if isinstance(value, str) else "expected a string"))
        return None
    return lines_by_path[value]


def validate_survey(report, request) -> list:
    errors = []
    if not isinstance(report, dict):
        return [_error("", "expected an object")]
    files = request.get("files", []) if isinstance(request, dict) else []
    lines_by_path = {f["path"]: f.get("lines") or 0 for f in files if isinstance(f, dict)}
    _keys(report, "", _REQUIRED, errors, _OPTIONAL)

    if "explain_report" in report and report["explain_report"] != REPORT_SCHEMA:
        errors.append(_error("/explain_report", f"must be {REPORT_SCHEMA}"))
    if "kind" in report and report["kind"] != "survey":
        errors.append(_error("/kind", "must be survey"))
    if "area" in report and report["area"] != request.get("area"):
        errors.append(_error("/area", "does not match the request"))
    if "files_sha256" in report and report["files_sha256"] != request.get("files_sha256"):
        errors.append(_error("/files_sha256", "does not match the request"))
    if "model" in report:
        _text(report["model"], "/model", 80, errors)
    if "summary" in report:
        _text(report["summary"], "/summary", 1200, errors)

    prefixes = [p for p in lines_by_path]
    if "components" in report:
        components = _items(report, "components", 30, errors, minimum=1)
        for i, comp in enumerate(components or []):
            base = _ptr("/components", i)
            if not isinstance(comp, dict):
                errors.append(_error(base, "expected an object"))
                continue
            _keys(comp, base, _COMPONENT_KEYS, errors)
            if "name" in comp:
                _text(comp["name"], base + "/name", 60, errors)
            if "role" in comp:
                _text(comp["role"], base + "/role", 400, errors)
            if "paths" in comp:
                paths = comp["paths"]
                pointer = base + "/paths"
                if not isinstance(paths, list):
                    errors.append(_error(pointer, "expected a list"))
                    continue
                if not 1 <= len(paths) <= 20:
                    errors.append(_error(pointer, "needs 1 to 20 items"))
                for j, entry in enumerate(paths):
                    here = _ptr(pointer, j)
                    if not isinstance(entry, str) or not entry:
                        errors.append(_error(here, "expected a non-empty string"))
                    elif entry in lines_by_path:
                        continue
                    elif not (entry.endswith("/") and any(p.startswith(entry) for p in prefixes)):
                        errors.append(_error(here, "not a file or directory of the request"))

    for key, names, limit, texts in (
        ("flows", _FLOW_KEYS, 20, {"from": 60, "to": 60, "text": 300}),
    ):
        if key in report:
            for i, item in enumerate(_items(report, key, limit, errors) or []):
                base = _ptr("/" + key, i)
                if not isinstance(item, dict):
                    errors.append(_error(base, "expected an object"))
                    continue
                _keys(item, base, names, errors)
                for name, cap in texts.items():
                    if name in item:
                        _text(item[name], base + "/" + name, cap, errors)

    if "entrypoints" in report:
        for i, item in enumerate(_items(report, "entrypoints", 10, errors) or []):
            base = _ptr("/entrypoints", i)
            if not isinstance(item, dict):
                errors.append(_error(base, "expected an object"))
                continue
            _keys(item, base, _ENTRYPOINT_KEYS, errors)
            total = _file_path(item["path"], base + "/path", lines_by_path, errors) if "path" in item else None
            if "line" in item:
                line = item["line"]
                if not _is_int(line) or line < 1 or (total is not None and line > total):
                    errors.append(_error(base + "/line", "not a line of the file"))
            if "text" in item:
                _text(item["text"], base + "/text", 200, errors)

    if "ranges" in report:
        for i, item in enumerate(_items(report, "ranges", 40, errors) or []):
            base = _ptr("/ranges", i)
            if not isinstance(item, dict):
                errors.append(_error(base, "expected an object"))
                continue
            _keys(item, base, _RANGE_KEYS, errors)
            total = _file_path(item["path"], base + "/path", lines_by_path, errors) if "path" in item else None
            start, end = item.get("start"), item.get("end")
            for name, value in (("start", start), ("end", end)):
                if name in item and (not _is_int(value) or value < 1 or (total is not None and value > total)):
                    errors.append(_error(base + "/" + name, "not a line of the file"))
            if _is_int(start) and _is_int(end):
                if start > end:
                    errors.append(_error(base, "start is after end"))
                elif end - start + 1 > 120:
                    errors.append(_error(base, "range is longer than 120 lines"))
            if "why" in item:
                _text(item["why"], base + "/why", 200, errors)
    return errors


def _strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            yield from _strings(item)
    elif isinstance(obj, dict):
        for value in obj.values():
            yield from _strings(value)


def record(session_dir, area, file=None) -> dict:
    session = _session(session_dir)
    if session.get("kind") != "codebase":
        raise ExplainError(1, "survey needs a codebase session")
    _check_area_id(area)
    surveys = _surveys(session_dir)
    default = surveys / f"{area}.json"
    source = Path(os.path.abspath(file)) if file else default

    # (1) the request
    request_path = surveys / f"{area}-request.json"
    if not request_path.is_file():
        raise ExplainError(1, "survey request is missing; run survey-prepare")
    try:
        request = read_json(request_path)
    except (ValueError, OSError):
        request = None
    if not isinstance(request, dict) or not isinstance(request.get("files"), list):
        raise ExplainError(1, "survey request is not valid; run survey-prepare")

    # (2) the report file
    try:
        data = source.read_bytes()
    except OSError:
        raise ExplainError(1, "survey report is missing") from None
    try:
        report = json.loads(data.decode("utf-8"))
    except ValueError:
        _discard(session_dir, source)
        raise ExplainError(1, "survey report is not valid json") from None

    # (3) secrets
    if find_secrets_in(report):
        _discard(session_dir, source)
        raise ExplainError(1, "survey report contains a possible secret")

    # (4) format
    errors = validate_survey(report, request)
    if errors:
        _discard(session_dir, source)
        raise ExplainError(1, errors)

    # (5) freshness
    repomap = load_repomap(session_dir)
    entry = None
    if repomap is not None:
        entry = next((a for a in repomap.get("areas", []) if isinstance(a, dict) and a.get("id") == area), None)
    if entry is None or entry.get("files_sha256") != request.get("files_sha256"):
        _discard(session_dir, source)
        raise ExplainError(1, "survey is stale: area files changed")

    # (6) store
    if os.path.realpath(source) != os.path.realpath(default):
        tmp = default.with_name(f".{default.name}.{os.getpid()}.tmp")
        default.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, default)
    flags = sum(len(scan_text(text)) for text in _strings(report))
    entry["survey"] = {
        "model": report["model"],
        "files_sha256": report["files_sha256"],
        "report_sha256": sha256_bytes(data),
        "stored_at": now_iso(),
        "flags": flags,
        "report": report,
    }
    entry["state"] = area_state(entry)
    write_repomap(session_dir, repomap)
    return {
        "ok": True,
        "area": area,
        "state": entry["state"],
        "components": len(report["components"]),
        "ranges": len(report.get("ranges", [])),
        "flags": flags,
    }
