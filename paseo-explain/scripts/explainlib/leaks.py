"""OPSEC leak scanner for emails, local paths, ids, secrets, and extra terms."""

import os
import re
import subprocess
from pathlib import Path

_ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.invalid")
_WORKSPACE_SEGMENTS = {"project-a", "example"}
_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_CREDIT_FILES = (
    Path("THIRD_PARTY_LICENSES.md"),
    Path("paseo-explain/references/design.md"),
    Path("paseo-explain/assets/template.html"),
)

PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("local-path", re.compile(r"/home/[a-z]")),
    ("local-path", re.compile(r"/Users/[A-Za-z]")),
    ("local-path", re.compile(r"C:\\Users\\")),
    ("workspace-path", re.compile("/work" + "space/")),
    ("uuid", re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")),
    ("paseo-id", re.compile(r"\b(wks|prj)_[0-9a-f]{8,}\b")),
    ("secret", re.compile(r"\bsk-[A-Za-z0-9]{8,}")),
    ("secret", re.compile(r"\bghp_[A-Za-z0-9]{8,}")),
    ("secret", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("secret", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]


def _default_repo_root() -> Path:
    script_dir = Path(__file__).resolve().parents[1]
    return script_dir.parents[1]


def _git_env() -> dict:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _excerpt(line: str) -> str:
    text = line.strip()
    if len(text) > 160:
        return text[:160]
    return text


def _email_allowed(address: str) -> bool:
    domain = address.rsplit("@", 1)[-1].casefold()
    return any(domain == item or domain.endswith("." + item) for item in _ALLOWED_EMAIL_DOMAINS)


def _workspace_allowed(line: str, end: int) -> bool:
    segment = []
    for char in line[end:]:
        if not char.isalnum() and char not in "._-":
            break
        segment.append(char)
    token = "".join(segment).rstrip(".")
    return token in _WORKSPACE_SEGMENTS


def _extra_terms(terms) -> list:
    cleaned = []
    for term in terms or []:
        if not isinstance(term, str):
            continue
        text = term.strip()
        if text and not text.startswith("#"):
            cleaned.append(text)
    return cleaned


def scan_text(text, extra_terms) -> list:
    hits = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(line):
                if kind == "email" and _email_allowed(match.group(0)):
                    continue
                if kind == "workspace-path" and _workspace_allowed(line, match.end()):
                    continue
                hits.append({"line": lineno, "kind": kind, "excerpt": _excerpt(line)})
        for term in _extra_terms(extra_terms):
            folded = term.casefold()
            start = 0
            haystack = line.casefold()
            while True:
                found = haystack.find(folded, start)
                if found < 0:
                    break
                hits.append({"line": lineno, "kind": "extra", "excerpt": _excerpt(line)})
                start = found + max(len(folded), 1)
    return hits


def _read_text(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _load_extra(root: Path) -> list:
    text = _read_text(root / ".opsec-extra")
    if text is None:
        return []
    terms = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            terms.append(stripped)
    return terms


def _credited_names(root: Path) -> set:
    names = set()
    for rel in _CREDIT_FILES:
        text = _read_text(root / rel)
        if text is None:
            continue
        if rel.name == "template.html":
            comment = re.search(r"<!--(.*?)-->", text, re.DOTALL)
            text = comment.group(1) if comment else ""
        for match in _REPO_NAME.findall(text):
            names.add(match.rstrip(".").casefold())
    return names


def _copyright_exempt(path: Path, line: str) -> bool:
    if not line.lstrip().startswith("Copyright (c)"):
        return False
    return path.name == "LICENSE" or path.name == "THIRD_PARTY_LICENSES.md"


def _scan_file(path: Path, root: Path, extra_terms, display: str) -> list:
    if path.is_symlink() or path.name == ".opsec-extra":
        return []
    text = _read_text(path)
    if text is None:
        return []
    terms = [term for term in extra_terms if term.casefold() not in _credited_names(root)]
    lines = text.splitlines()
    hits = []
    for hit in scan_text(text, terms):
        line = lines[hit["line"] - 1] if 1 <= hit["line"] <= len(lines) else ""
        if hit["kind"] == "extra" and _copyright_exempt(path, line):
            continue
        hits.append({"file": display, "line": hit["line"], "kind": hit["kind"], "excerpt": hit["excerpt"]})
    return hits


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name != ".git"]
        for name in filenames:
            yield Path(dirpath) / name


def _finish(hits, errors) -> dict:
    ordered = sorted(hits, key=lambda item: (item["file"], item["line"], item["kind"], item["excerpt"]))
    report = {"ok": not ordered and not errors, "hits": ordered}
    if errors:
        report["errors"] = errors
    return report


def leaks_paths(paths) -> dict:
    hits = []
    errors = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            root = path
            extra = _load_extra(root)
            for file in _iter_files(root):
                display = file.relative_to(root).as_posix()
                hits.extend(_scan_file(file, root, extra, display))
        elif path.is_file():
            root = path.parent
            extra = _load_extra(root)
            hits.extend(_scan_file(path, root, extra, path.name))
        else:
            errors.append({"path": str(path), "message": "path not found"})
    return _finish(hits, errors)


def leaks_repo(repo_root=None) -> dict:
    repo = Path(repo_root).resolve() if repo_root else _default_repo_root()
    listed = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "-z"],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )
    if listed.returncode != 0:
        detail = listed.stderr.strip() or "not a git work tree"
        return _finish([], [{"path": str(repo), "message": detail}])
    extra = _load_extra(repo)
    hits = []
    for rel in listed.stdout.split("\0"):
        if not rel:
            continue
        hits.extend(_scan_file(repo / rel, repo, extra, rel))
    return _finish(hits, [])
