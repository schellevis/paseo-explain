"""Shared session paths, JSON I/O, hashes, and init."""

import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from explainlib import EXPLAIN_SCHEMA
from explainlib.config import load_config, resolve_default_level, resolve_levels, snap_level

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

LEVEL_NAMES = {
    "en": {
        1: "Simple (B1)",
        2: "Accessible",
        3: "Mixed",
        4: "Technical",
        5: "Expert",
    },
    "nl": {
        1: "Eenvoudig (B1)",
        2: "Toegankelijk",
        3: "Gemengd",
        4: "Technisch",
        5: "Expert",
    },
}

_MODES = ("quick", "standard", "deep")


class ExplainError(Exception):
    def __init__(self, code: int, errors: list | str, extra: dict | None = None):
        if isinstance(errors, str):
            errors = [{"path": "", "message": errors}]
        self.code = code
        self.errors = errors
        self.extra = extra
        message = errors[0]["message"] if errors else ""
        super().__init__(message)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path) -> str:
    with open(path, "rb") as handle:
        return sha256_bytes(handle.read())


def canonical_sha256(obj) -> str:
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def explain_home() -> Path:
    raw = os.environ.get("PASEO_EXPLAIN_HOME")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".paseo-explain"


def sessions_root() -> Path:
    return explain_home() / "sessions"


def server_dir() -> Path:
    return explain_home() / "server"


def autopilot_slug(run_dir: str, doc: str) -> str:
    real = os.path.realpath(run_dir)
    base = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", os.path.basename(real).lower())).strip("-") or "run"
    digest = hashlib.sha1(real.encode()).hexdigest()[:8]
    return f"ap-{base[:40]}-{digest}-{doc}"


def load_session(session_dir) -> dict:
    data = read_json(Path(session_dir) / "session.json")
    if not isinstance(data, dict):
        raise ExplainError(1, "session.json must be an object")
    return data


def save_session(session_dir, obj) -> None:
    write_json(Path(session_dir) / "session.json", obj)


def current_hashes(session_dir) -> dict:
    root = Path(session_dir)
    hashes = {"explain_sha256": None, "evidence_sha256": None, "brief_sha256": None}
    explain = root / "explain.json"
    evidence = root / "evidence.json"
    brief = root / "brief.json"
    if explain.is_file():
        hashes["explain_sha256"] = canonical_sha256(read_json(explain))
    if evidence.is_file():
        hashes["evidence_sha256"] = sha256_file(evidence)
    if brief.is_file():
        hashes["brief_sha256"] = canonical_sha256(read_json(brief))
    return hashes


def words(text: str) -> int:
    return len(text.split())


def _abs_path(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _remove_tree_entry(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _clear_quick_checks(session_dir: Path) -> None:
    for name in ("factcheck.json", "reader.json"):
        _remove_tree_entry(session_dir / name)
    _remove_tree_entry(session_dir / "checks")


def _blank_session(slug: str, kind: str, identity: dict, cfg: dict, mode, level, levels, lang, out) -> dict:
    written = resolve_levels(levels, cfg)
    return {
        "explain_schema": EXPLAIN_SCHEMA,
        "slug": slug,
        "kind": kind,
        "identity": identity,
        "mode": mode if mode is not None else "standard",
        "levels": written,
        "default_level": resolve_default_level(level, cfg, written),
        "lang": lang if lang is not None else cfg["values"]["lang"],
        "theme": cfg["values"]["theme"],
        "created_at": now_iso(),
        "round": 0,
        "content_sha256": None,
        "export_dir": _abs_path(out) if out else None,
        "reader_grade": None,
        "skipped": {"fact_check": None, "reader_test": None},
        "urls": None,
        "tab_opened": None,
    }


def init_session(
    *,
    kind: str | None,
    slug: str | None,
    autopilot: str | None,
    doc: str | None,
    out: str | None,
    mode: str | None,
    level: int | None,
    levels: int | None,
    lang: str | None,
) -> dict:
    if bool(slug) == bool(autopilot):
        raise ExplainError(2, "specify exactly one of slug and autopilot")
    if mode is not None and mode not in _MODES:
        raise ExplainError(1, "mode must be quick, standard, or deep")
    if levels is not None and levels not in (3, 5):
        raise ExplainError(1, "levels must be 3 or 5")
    cfg = load_config()
    if autopilot:
        if doc not in ("spec", "plan"):
            raise ExplainError(1, "doc must be spec or plan")
        slug = autopilot_slug(autopilot, doc)
        kind = "plan"
        identity = {"source": "autopilot", "run_dir": os.path.realpath(autopilot), "doc": doc}
    else:
        if kind not in ("plan", "idea"):
            raise ExplainError(1, "kind must be plan or idea")
        if SLUG_RE.fullmatch(slug) is None:
            raise ExplainError(1, "invalid slug")
        identity = {"source": "user", "kind": kind}
    if SLUG_RE.fullmatch(slug) is None:
        raise ExplainError(1, "invalid slug")
    try:
        session_dir = sessions_root() / slug
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExplainError(3, f"unwritable directory: {exc}") from exc
    existing_path = session_dir / "session.json"
    if existing_path.is_file():
        existing = load_session(session_dir)
        if existing.get("identity") != identity:
            raise ExplainError(1, "session identity mismatch")
        obj = existing
        written = obj.get("levels")
        if levels is not None:
            written = resolve_levels(levels, cfg)
            obj["levels"] = written
        if not isinstance(written, list):
            written = resolve_levels(None, cfg)
            obj["levels"] = written
        if level is not None:
            obj["default_level"] = resolve_default_level(level, cfg, written)
        elif levels is not None:
            obj["default_level"] = snap_level(obj.get("default_level", 3), written)
        switching_quick = False
        if mode is not None and mode != obj.get("mode"):
            switching_quick = mode == "quick"
            obj["mode"] = mode
        if lang is not None:
            obj["lang"] = lang
        if out is not None:
            obj["export_dir"] = _abs_path(out)
        obj["explain_schema"] = EXPLAIN_SCHEMA
        obj["slug"] = slug
        obj["kind"] = kind
        obj["identity"] = identity
        if switching_quick:
            obj["reader_grade"] = None
            _clear_quick_checks(session_dir)
        for key, value in _blank_session(slug, kind, identity, cfg, None, None, None, None, None).items():
            obj.setdefault(key, value)
        save_session(session_dir, obj)
    else:
        obj = _blank_session(slug, kind, identity, cfg, mode, level, levels, lang, out)
        if obj["mode"] == "quick":
            _clear_quick_checks(session_dir)
        save_session(session_dir, obj)
    return {
        "ok": True,
        "session": str(session_dir.resolve()),
        "slug": slug,
        "round": obj["round"],
        "default_level": obj["default_level"],
        "levels": obj["levels"],
        "mode": obj["mode"],
    }
