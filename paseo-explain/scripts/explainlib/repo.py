"""List the files of a repository and classify them for the manifest and repomap."""

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess

from explainlib.common import ExplainError, now_iso, write_json
from explainlib.secretscan import find_secrets, is_secret_file

FILE_LIMIT = 20000
LARGE_BYTES = 409600
SNIFF_BYTES = 8192

PRUNE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".cache",
        "dist",
        "build",
        "target",
    }
)

DOC_EXTENSIONS = frozenset({".md", ".markdown", ".rst", ".txt", ".adoc"})

LANG_BY_EXT = {
    ".py": "Python",
    ".js": "JavaScript",
    ".mjs": "JavaScript",
    ".cjs": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".rb": "Ruby",
    ".php": "PHP",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cxx": "C++",
    ".hpp": "C++",
    ".hh": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".scala": "Scala",
    ".sh": "Shell",
    ".bash": "Shell",
    ".zsh": "Shell",
    ".ps1": "PowerShell",
    ".sql": "SQL",
    ".html": "HTML",
    ".htm": "HTML",
    ".css": "CSS",
    ".scss": "CSS",
    ".sass": "CSS",
    ".less": "CSS",
    ".vue": "Vue",
    ".svelte": "Svelte",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".rst": "reStructuredText",
    ".txt": "Text",
    ".adoc": "AsciiDoc",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".toml": "TOML",
    ".ini": "INI",
    ".cfg": "INI",
    ".xml": "XML",
    ".r": "R",
    ".jl": "Julia",
    ".lua": "Lua",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".erl": "Erlang",
    ".hs": "Haskell",
    ".ml": "OCaml",
    ".tf": "Terraform",
}

LANG_BY_NAME = {
    "Makefile": "Make",
    "Dockerfile": "Dockerfile",
    "Containerfile": "Dockerfile",
    "Justfile": "Just",
}

_GIT_OPTIONS = [
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.untrackedCache=false",
]


def canon(text: str, empty: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", text.lower())).strip("-") or empty


def is_doc_file(record: dict) -> bool:
    return record["kind"] == "text" and os.path.splitext(record["path"])[1].lower() in DOC_EXTENSIONS


def code_totals(files: list) -> tuple:
    """Return (code_files, code_bytes) over text files that are not doc files."""
    code = [f for f in files if f["kind"] == "text" and not is_doc_file(f)]
    return len(code), sum(f["size"] for f in code)


def _git_env() -> dict:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _git(repo_real: str, *args: str):
    try:
        proc = subprocess.run(
            ["git", "-C", repo_real, *_GIT_OPTIONS, *args],
            capture_output=True,
            timeout=30,
            env=_git_env(),
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_listing(repo_real: str):
    if shutil.which("git") is None:
        return None
    inside = _git(repo_real, "rev-parse", "--is-inside-work-tree")
    if inside is None or inside.decode("utf-8", "replace").strip() != "true":
        return None
    listed = _git(repo_real, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listed is None:
        return None
    paths = [os.fsdecode(item) for item in listed.split(b"\0") if item]
    head = _git(repo_real, "rev-parse", "HEAD")
    head_text = head.decode("utf-8", "replace").strip() if head else ""
    return paths, (head_text or None)


def _walk_listing(repo_real: str) -> list:
    found = []
    for folder, dirs, files in os.walk(repo_real, followlinks=False):
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
        rel_dir = os.path.relpath(folder, repo_real)
        for name in files:
            rel = name if rel_dir == "." else os.path.join(rel_dir, name)
            found.append(rel.replace(os.sep, "/"))
    return found


def _inside(real: str, repo_real: str) -> bool:
    prefix = repo_real if repo_real.endswith(os.sep) else repo_real + os.sep
    return real.startswith(prefix)


def _inode(path: str):
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_dev, info.st_ino)


def _secret_inodes(repo_real: str) -> set:
    """Inodes of every regular file with a secret-file name, including files git ignores."""
    found = set()
    for folder, dirs, files in os.walk(repo_real, followlinks=False):
        dirs[:] = [d for d in dirs if d not in PRUNE_DIRS]
        for name in files:
            if not is_secret_file(name):
                continue
            try:
                info = os.lstat(os.path.join(folder, name))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.add((info.st_dev, info.st_ino))
    return found


def _lang(path: str):
    name = os.path.basename(path)
    if name in LANG_BY_NAME:
        return LANG_BY_NAME[name]
    return LANG_BY_EXT.get(os.path.splitext(name)[1].lower())


def _line_count(text: str) -> int:
    from explainlib.ingest import _line_count as count

    return count(text)


def _record(path, size, kind, lang, reason, lines=None, sha256=None) -> dict:
    return {
        "path": path,
        "size": size,
        "lines": lines,
        "sha256": sha256,
        "kind": kind,
        "lang": lang,
        "reason": reason,
    }


def _read_record(path: str, full: str, size: int, lang) -> dict:
    try:
        with open(full, "rb") as handle:
            data = handle.read()
    except OSError:
        return _record(path, size, "excluded", lang, "unreadable file")
    digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is None or b"\x00" in data[:SNIFF_BYTES]:
        return _record(path, len(data), "binary", lang, None, None, digest)
    kind = "large" if len(data) > LARGE_BYTES else "text"
    return _record(path, len(data), kind, lang, None, _line_count(text), digest)


def list_repository(repo_real: str) -> dict:
    git = False
    head = None
    paths = None
    listed = _git_listing(repo_real)
    if listed is not None:
        paths, head = listed
        git = True
    else:
        paths = _walk_listing(repo_real)
    paths = sorted({p for p in paths if os.path.lexists(os.path.join(repo_real, p))})
    if len(paths) > FILE_LIMIT:
        raise ExplainError(1, f"repository too large: {len(paths)} files (limit {FILE_LIMIT})")

    info = {}
    for rel in paths:
        full = os.path.join(repo_real, rel)
        real = os.path.realpath(full)
        info[rel] = (full, real, _inside(real, repo_real))

    by_name = set()
    for rel, (full, real, inside) in info.items():
        if inside and (is_secret_file(os.path.basename(rel)) or is_secret_file(os.path.basename(real))):
            by_name.add(rel)
    inodes = _secret_inodes(repo_real)
    for rel in by_name:
        node = _inode(info[rel][0])
        if node is not None:
            inodes.add(node)
    secret_files = set(by_name)
    for rel, (full, _real, inside) in info.items():
        if inside and rel not in secret_files and _inode(full) in inodes:
            secret_files.add(rel)

    records = []
    withheld = 0
    for rel in paths:
        full, real, inside = info[rel]
        lang = _lang(rel)
        if find_secrets(rel):
            withheld += 1
            try:
                size = os.stat(full).st_size
            except OSError:
                size = 0
            records.append(_record(f"(withheld-path-{withheld})", size, "excluded", lang, "secret-like path"))
            continue
        if not inside:
            try:
                size = os.stat(full).st_size
            except OSError:
                size = 0
            records.append(_record(rel, size, "excluded", lang, "symlink outside repository"))
            continue
        if not os.path.isfile(real):
            continue
        try:
            size = os.stat(full).st_size
        except OSError:
            continue
        if rel in secret_files:
            records.append(_record(rel, size, "excluded", lang, "secret file"))
            continue
        records.append(_read_record(rel, full, size, lang))

    return {
        "repo": {"name": os.path.basename(repo_real), "git": git, "head": head},
        "files": records,
    }


BUILD_BASENAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "requirements.txt",
        "Pipfile",
        "poetry.lock",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "build.gradle.kts",
        "Gemfile",
        "composer.json",
        "Makefile",
        "Justfile",
        "Dockerfile",
        "Containerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
        "compose.yaml",
        "tox.ini",
        "noxfile.py",
        "CMakeLists.txt",
        "deno.json",
        "bun.lockb",
        ".gitlab-ci.yml",
    }
)
ENTRYPOINT_STEMS = frozenset({"main", "__main__", "app", "server", "cli", "index", "manage", "run"})
ENTRYPOINT_DIRS = frozenset({"bin", "cmd"})

TREE_LINE_LIMIT = 300
TREE_WIDE_FILES = 2000
BUILD_LIMIT = 40
ENTRYPOINT_LIMIT = 30
EXCLUDED_LIMIT = 50

SPLIT_FILES = 150
SPLIT_LINES = 20000
REPOMAP_NAME = "repomap.json"


def build_files(listing: dict) -> list:
    """Sorted paths of all non-excluded build and run files (not truncated)."""
    found = []
    for record in listing["files"]:
        if record["kind"] == "excluded":
            continue
        path = record["path"]
        if os.path.basename(path) in BUILD_BASENAMES or path.startswith(".github/workflows/"):
            found.append(path)
    return sorted(found)


def entrypoints(listing: dict) -> list:
    """Sorted paths of non-excluded text files that look like entrypoints by name only."""
    found = []
    for record in listing["files"]:
        if record["kind"] != "text":
            continue
        path = record["path"]
        parts = path.split("/")
        stem = parts[-1].split(".")[0]
        if stem in ENTRYPOINT_STEMS or (len(parts) > 1 and parts[-2] in ENTRYPOINT_DIRS):
            found.append(path)
    return sorted(found)


def _capped(items: list, limit: int) -> list:
    if not items:
        return ["- none"]
    lines = [f"- {item}" for item in items[:limit]]
    if len(items) > limit:
        lines.append(f"- … ({len(items) - limit} more)")
    return lines


def _tree_lines(files: list) -> list:
    depth = 2 if len(files) > TREE_WIDE_FILES else 3
    totals = {}
    for record in files:
        if record["kind"] == "excluded":
            continue
        parts = record["path"].split("/")[:-1][:depth]
        lines = record["lines"] if record["kind"] in ("text", "large") else 0
        for level in range(1, len(parts) + 1):
            key = tuple(parts[:level])
            count, total = totals.get(key, (0, 0))
            totals[key] = (count + 1, total + (lines or 0))
    keys = sorted(totals)
    rows = [
        f"{'  ' * (len(key) - 1)}- {'/'.join(key)}/ ({totals[key][0]} files, {totals[key][1]} lines)"
        for key in keys[:TREE_LINE_LIMIT]
    ]
    if len(keys) > TREE_LINE_LIMIT:
        rows.append(f"- … ({len(keys) - TREE_LINE_LIMIT} more directories)")
    return rows or ["- none"]


def build_manifest(listing: dict) -> str:
    files = listing["files"]
    info = listing["repo"]
    kinds = {k: sum(1 for f in files if f["kind"] == k) for k in ("text", "large", "binary", "excluded")}
    counted = [f for f in files if f["kind"] in ("text", "large")]
    head = (info.get("head") or "")[:12] or "none"

    langs = {}
    for record in counted:
        if record["lang"]:
            count, lines = langs.get(record["lang"], (0, 0))
            langs[record["lang"]] = (count + 1, lines + (record["lines"] or 0))
    lang_rows = [
        f"- {name}: {count} files, {lines} lines"
        for name, (count, lines) in sorted(langs.items(), key=lambda item: (-item[1][1], item[0]))
    ]

    excluded = sorted(
        (f for f in files if f["kind"] == "excluded"), key=lambda f: f["path"]
    )
    out = [
        f"# Repository manifest: {info['name']}",
        "",
        "## Summary",
        f"- Files: {len(files)} (text {kinds['text']}, large {kinds['large']}, "
        f"binary {kinds['binary']}, excluded {kinds['excluded']})",
        f"- Lines in text files: {sum(f['lines'] or 0 for f in counted)}",
        f"- Git: {'yes' if info.get('git') else 'no'}; HEAD {head}",
        "",
        "## Languages",
        *(lang_rows or ["- none"]),
        "",
        "## Tree",
        *_tree_lines(files),
        "",
        "## Build and run files",
        *_capped(build_files(listing), BUILD_LIMIT),
        "",
        "## Entrypoint candidates",
        "Candidates found by name only.",
        *_capped(entrypoints(listing), ENTRYPOINT_LIMIT),
        "",
        "## Excluded files",
        *_capped([f"{f['path']} ({f['reason']})" for f in excluded], EXCLUDED_LIMIT),
    ]
    return "\n".join(out) + "\n"


def area_files_sha256(files: list) -> str:
    text = "".join(f"{f['path']}\0{f['sha256']}\n" for f in sorted(files, key=lambda f: f["path"]))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def area_state(area: dict) -> str:
    survey = area.get("survey")
    if not isinstance(survey, dict):
        return "missing"
    return "fresh" if survey.get("files_sha256") == area.get("files_sha256") else "stale"


def _area_groups(listing: dict) -> list:
    """Return [(area dict, [file records])] sorted by area id."""
    groups = {}
    for record in listing["files"]:
        if record["kind"] not in ("text", "large"):
            continue
        parts = record["path"].split("/")
        groups.setdefault(parts[0] if len(parts) > 1 else None, []).append(record)

    candidates = []  # (sort key, display paths, base id, files)
    for top, members in groups.items():
        if top is None:
            candidates.append(((0, ""), ["(root files)"], "root", members))
            continue
        lines = sum(f["lines"] or 0 for f in members)
        deeper = any(f["path"].count("/") >= 2 for f in members)
        if (len(members) > SPLIT_FILES or lines > SPLIT_LINES) and deeper:
            subs = {}
            direct = []
            for record in members:
                parts = record["path"].split("/")
                if len(parts) == 2:
                    direct.append(record)
                else:
                    subs.setdefault(parts[1], []).append(record)
            for sub, sub_members in subs.items():
                candidates.append(((1, f"{top}/{sub}/"), [f"{top}/{sub}/"], canon(f"{top}/{sub}", "area"), sub_members))
            if direct:
                candidates.append(((1, f"{top}/"), [f"{top}/ (direct files)"], canon(top, "area") + "-direct", direct))
        else:
            candidates.append(((1, f"{top}/"), [f"{top}/"], canon(top, "area"), members))

    used = set()
    result = []
    for _key, paths, base, members in sorted(candidates, key=lambda c: c[0]):
        area_id = base
        number = 2
        while area_id in used:
            area_id = f"{base}-{number}"
            number += 1
        used.add(area_id)
        area = {
            "id": area_id,
            "paths": paths,
            "files": len(members),
            "lines": sum(f["lines"] or 0 for f in members),
            "files_sha256": area_files_sha256(members),
            "state": "missing",
            "survey": None,
        }
        result.append((area, sorted(members, key=lambda f: f["path"])))
    return sorted(result, key=lambda item: item[0]["id"])


def compute_areas(listing: dict) -> list:
    return [area for area, _members in _area_groups(listing)]


def area_file_map(listing: dict) -> dict:
    """Map area id to its file records (text and large files), sorted by path."""
    return {area["id"]: members for area, members in _area_groups(listing)}


def load_repomap(session_dir):
    try:
        with open(os.path.join(os.fspath(session_dir), REPOMAP_NAME), "rb") as handle:
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_repomap(session_dir, obj: dict) -> None:
    write_json(os.path.join(os.fspath(session_dir), REPOMAP_NAME), obj)


def merge_repomap(old, listing: dict, areas: list, selection: dict) -> dict:
    carried = {}
    if isinstance(old, dict) and isinstance(old.get("areas"), list):
        for previous in old["areas"]:
            if isinstance(previous, dict) and isinstance(previous.get("survey"), dict):
                carried[previous.get("id")] = previous["survey"]
    merged = []
    for area in areas:
        item = copy.deepcopy(area)
        item["survey"] = copy.deepcopy(carried[area["id"]]) if area["id"] in carried else None
        item["state"] = area_state(item)
        merged.append(item)
    return {
        "explain_schema": 1,
        "repo": dict(listing["repo"]),
        "generated_at": now_iso(),
        "files": listing["files"],
        "areas": merged,
        "selection": selection,
    }
