"""Local reading-level configuration."""

import json
import os
import re
from pathlib import Path

_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
_KEYS = ("reading_level", "levels", "theme", "lang")
_DEFAULTS = {
    "reading_level": 3,
    "levels": 3,
    "theme": "dark",
    "lang": "en",
}


def config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg).expanduser() / "paseo-explain" / "config.json"
    return Path.home() / ".config" / "paseo-explain" / "config.json"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid(key: str, value) -> bool:
    if key == "reading_level":
        return _is_int(value) and 1 <= value <= 5
    if key == "levels":
        return value in (3, 5) and _is_int(value)
    if key == "theme":
        return value in ("dark", "light")
    if key == "lang":
        return isinstance(value, str) and _LANG_RE.fullmatch(value) is not None
    return False


def load_config() -> dict:
    path = config_path()
    values = dict(_DEFAULTS)
    sources = {key: "default" for key in _KEYS}
    warnings: list[str] = []
    exists = path.is_file()
    raw = {}
    if exists:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            warnings.append("ignored invalid config file")
            raw = {}
        if not isinstance(raw, dict):
            warnings.append("ignored invalid config file")
            raw = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            if key not in _DEFAULTS:
                warnings.append(f"ignored unknown key: {key}")
                continue
            if not _valid(key, value):
                warnings.append(f"ignored invalid {key}: {value!r}")
                continue
            values[key] = value
            sources[key] = "config"
    return {
        "values": values,
        "sources": sources,
        "warnings": warnings,
        "path": str(path),
        "exists": exists,
    }


def resolve_levels(cli_levels: int | None, cfg: dict) -> list[int]:
    chosen = cli_levels if cli_levels is not None else cfg["values"]["levels"]
    if chosen == 5:
        return [1, 2, 3, 4, 5]
    if chosen == 3:
        return [1, 3, 5]
    raise ValueError(f"levels must be 3 or 5, not {chosen!r}")


def snap_level(level: int, levels: list[int]) -> int:
    ordered = sorted(levels)
    best = ordered[0]
    best_dist = abs(level - best)
    for candidate in ordered[1:]:
        dist = abs(level - candidate)
        if dist < best_dist:
            best = candidate
            best_dist = dist
    return best


def resolve_default_level(cli_level: int | None, cfg: dict, levels: list[int]) -> int:
    if cli_level is not None:
        raw = cli_level
    else:
        raw = cfg["values"]["reading_level"]
    return snap_level(raw, levels)


def config_report() -> dict:
    cfg = load_config()
    values = {}
    for key in _KEYS:
        values[key] = {"value": cfg["values"][key], "source": cfg["sources"][key]}
    return {
        "ok": True,
        "path": cfg["path"],
        "exists": cfg["exists"],
        "values": values,
        "warnings": cfg["warnings"],
    }
