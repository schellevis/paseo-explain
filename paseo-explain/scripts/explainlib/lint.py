"""Repository lint checks for skill text, schemas, the template, and git layout."""

import importlib
import json
import os
import re
import subprocess
from pathlib import Path

CORE_RULE = (
    "Every claim points at evidence, every quote is verbatim, "
    "and what is not known is said to be unknown."
)
MERMAID_SRC = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
MERMAID_SRI = "sha384-rbtjAdnIQE/aQJGEgXrVUlMibdfTSa4PQju4HDhN3sR2PmaKFzhEafuePsl9H/9I"

PLACEHOLDERS = {
    "{{LANG}}": 1,
    "{{TITLE}}": 1,
    "{{DATA_JSON}}": 1,
    "{{MERMAID_SRC}}": 1,
    "{{MERMAID_SRI}}": 1,
    "{{NONCE}}": 2,
}
FORBIDDEN = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    'setTimeout("',
    "setTimeout('",
    "javascript:",
)
REFERENCE_FILES = (
    "paseo-explain/references/pipeline.md",
    "paseo-explain/references/prompts.md",
    "paseo-explain/references/schema.md",
    "paseo-explain/references/display.md",
    "paseo-explain/references/integration.md",
    "paseo-explain/references/design.md",
)
R1_FILES = frozenset(
    {
        ".gitignore",
        "AGENTS.md",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_LICENSES.md",
        "paseo-explain/SKILL.md",
        "paseo-explain/LICENSE",
        "paseo-explain/assets/template.html",
        "paseo-explain/references/pipeline.md",
        "paseo-explain/references/prompts.md",
        "paseo-explain/references/schema.md",
        "paseo-explain/references/display.md",
        "paseo-explain/references/integration.md",
        "paseo-explain/references/design.md",
        "paseo-explain/references/explain.schema.json",
        "paseo-explain/references/result.schema.json",
        "paseo-explain/scripts/explain.py",
        "paseo-explain/scripts/explainlib/__init__.py",
        "paseo-explain/scripts/explainlib/common.py",
        "paseo-explain/scripts/explainlib/config.py",
        "paseo-explain/scripts/explainlib/ingest.py",
        "paseo-explain/scripts/explainlib/scan.py",
        "paseo-explain/scripts/explainlib/validate.py",
        "paseo-explain/scripts/explainlib/render.py",
        "paseo-explain/scripts/explainlib/reports.py",
        "paseo-explain/scripts/explainlib/result.py",
        "paseo-explain/scripts/explainlib/serve.py",
        "paseo-explain/scripts/explainlib/show.py",
        "paseo-explain/scripts/explainlib/lint.py",
        "paseo-explain/scripts/explainlib/leaks.py",
        "tests/helpers.py",
        "tests/test_common_config.py",
        "tests/test_ingest.py",
        "tests/test_scan.py",
        "tests/test_validate.py",
        "tests/test_render.py",
        "tests/test_reports_result.py",
        "tests/test_serve.py",
        "tests/test_show.py",
        "tests/test_lint_leaks.py",
        "tests/test_cli.py",
        "tests/fixtures/autopilot-run/00-brief.md",
        "tests/fixtures/autopilot-run/01-spec.md",
        "tests/fixtures/autopilot-run/02-spec-resolution.md",
        "tests/fixtures/autopilot-run/03-plan.md",
        "tests/fixtures/autopilot-run/04-plan-resolution.md",
        "tests/fixtures/idea.md",
        "tests/fixtures/plan-explain.json",
        "tests/fixtures/idea-explain.json",
        "tests/fixtures/factcheck.json",
        "tests/fixtures/reader.json",
    }
)
_LINK = re.compile(r"references/[A-Za-z0-9_./-]+")


def _default_repo_root() -> Path:
    script_dir = Path(__file__).resolve().parents[1]
    return script_dir.parents[1]


def _git_env() -> dict:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    return env


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        env=_git_env(),
        check=False,
    )


def _read_text(path: Path):
    try:
        data = path.read_bytes()
    except OSError as exc:
        return None, str(exc)
    try:
        return data.decode("utf-8"), None
    except UnicodeDecodeError:
        return None, "not valid UTF-8"


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _parse_frontmatter(text: str):
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---\n", 3)
    if end < 0:
        return None
    data = {}
    section = None
    for raw in text[4:end].splitlines():
        if not raw.strip():
            continue
        indented = raw[0] in " \t"
        key, sep, val = raw.strip().partition(":")
        if not sep or not key.strip():
            return None
        key = key.strip()
        val = val.strip()
        if indented:
            if not isinstance(section, dict):
                return None
            section[key] = _unquote(val)
            continue
        if val == "":
            data[key] = {}
            section = data[key]
        else:
            data[key] = _unquote(val)
            section = None
    return data


def _import_module(name: str):
    try:
        return importlib.import_module(name), None
    except Exception as exc:
        return None, f"cannot import {name}: {exc}"


def _key_set(value):
    if isinstance(value, (str, bytes)) or not isinstance(value, (set, frozenset, list, tuple)):
        return None
    try:
        return {str(item) for item in value}
    except TypeError:
        return None


def _property_names(node, found: set) -> None:
    if isinstance(node, dict):
        props = node.get("properties")
        if isinstance(props, dict):
            found.update(str(key) for key in props)
        for child in node.values():
            _property_names(child, found)
    elif isinstance(node, list):
        for child in node:
            _property_names(child, found)


def _load_json(path: Path):
    text, err = _read_text(path)
    if err:
        return None, err
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc.msg}"
    if not isinstance(data, dict):
        return None, "not valid JSON: expected an object"
    return data, None


def _tracked(repo: Path):
    top = _run_git(repo, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return None, "tracked-file check skipped: not a git work tree"
    if Path(top.stdout.strip()).resolve() != repo.resolve():
        return None, "tracked-file check skipped: git work tree is not the repository root"
    listed = _run_git(repo, "ls-files", "-s", "-z")
    if listed.returncode != 0:
        detail = listed.stderr.strip() or "git ls-files failed"
        return None, f"tracked-file check skipped: {detail}"
    entries = []
    for record in listed.stdout.split("\0"):
        if not record:
            continue
        meta, sep, path = record.partition("\t")
        if not sep:
            continue
        mode = meta.split()[0] if meta.split() else ""
        entries.append((mode, path))
    return entries, None


def lint(repo_root=None) -> dict:
    repo = Path(repo_root).resolve() if repo_root else _default_repo_root()
    errors = []
    warnings = []

    def add(path: str, message: str) -> None:
        errors.append({"path": path, "message": message})

    skill_rel = "paseo-explain/SKILL.md"
    skill_path = repo / skill_rel
    skill_text, skill_err = _read_text(skill_path)
    if skill_err:
        add(skill_rel, f"SKILL.md cannot be read: {skill_err}")
        skill_text = ""
    if len(skill_text.splitlines()) > 200:
        add(skill_rel, "SKILL.md exceeds 200 lines")
    front = _parse_frontmatter(skill_text) if skill_text else None
    if skill_text and front is None:
        add(skill_rel, "SKILL.md frontmatter is missing or not valid")
    elif front is not None:
        if front.get("name") != "paseo-explain":
            add(skill_rel, "frontmatter name must be paseo-explain")
        if "description" not in front or not isinstance(front.get("description"), str) or not front.get("description"):
            add(skill_rel, "frontmatter key missing: description")
        metadata = front.get("metadata")
        if not isinstance(metadata, dict):
            add(skill_rel, "frontmatter key missing: metadata")
            metadata = {}
        if "compatibility" not in metadata or not metadata.get("compatibility"):
            add(skill_rel, "frontmatter key missing: compatibility")
        package, import_err = _import_module("explainlib")
        version = getattr(package, "__version__", None) if package is not None else None
        if import_err or not isinstance(version, str):
            add(skill_rel, f"cannot import explainlib.__version__: {import_err or 'missing __version__'}")
        elif metadata.get("version") != version:
            add(
                skill_rel,
                f"metadata.version {metadata.get('version')!r} != explainlib.__version__ {version!r}",
            )
    if skill_text and CORE_RULE not in skill_text:
        add(skill_rel, "core rule is missing from SKILL.md")
    if skill_text:
        seen = set()
        for match in _LINK.findall(skill_text):
            rel_link = match.rstrip(".")
            if rel_link in seen:
                continue
            seen.add(rel_link)
            target = repo / "paseo-explain" / rel_link
            if not target.is_file():
                add(skill_rel, f"link does not resolve: {rel_link}")

    for rel in REFERENCE_FILES:
        path = repo / rel
        text, err = _read_text(path)
        if err:
            add(rel, f"reference file missing or unreadable: {rel} ({err})")
            continue
        title = next((line for line in text.splitlines() if line.strip()), "")
        if not title.startswith("# "):
            add(rel, f"reference file lacks a '# ' title line: {rel}")
        if rel.endswith("/pipeline.md") and CORE_RULE not in text:
            add(rel, "core rule is missing from pipeline.md")

    explain_schema, explain_err = _load_json(repo / "paseo-explain" / "references" / "explain.schema.json")
    if explain_err:
        add("paseo-explain/references/explain.schema.json", f"explain.schema.json is {explain_err}")
    result_schema, result_err = _load_json(repo / "paseo-explain" / "references" / "result.schema.json")
    if result_err:
        add("paseo-explain/references/result.schema.json", f"result.schema.json is {result_err}")

    validate_mod, validate_err = _import_module("explainlib.validate")
    if validate_err:
        add("paseo-explain/scripts/explainlib/validate.py", validate_err)
    elif explain_schema is not None:
        expected = _key_set(getattr(validate_mod, "EXPLAIN_TOP_KEYS", None))
        props = explain_schema.get("properties")
        got = set(props) if isinstance(props, dict) else None
        if expected is None:
            add("paseo-explain/scripts/explainlib/validate.py", "validate.EXPLAIN_TOP_KEYS is missing")
        elif got is None:
            add("paseo-explain/references/explain.schema.json", "explain.schema.json properties are missing")
        elif got != expected:
            add(
                "paseo-explain/references/explain.schema.json",
                "explain.schema.json properties "
                f"{sorted(got)} != validate.EXPLAIN_TOP_KEYS {sorted(expected)}",
            )

    result_mod, result_import_err = _import_module("explainlib.result")
    if result_import_err:
        add("paseo-explain/scripts/explainlib/result.py", result_import_err)
    elif result_schema is not None:
        expected = _key_set(getattr(result_mod, "RESULT_KEYS", None))
        props = result_schema.get("properties")
        got = set(props) if isinstance(props, dict) else None
        if expected is None:
            add("paseo-explain/scripts/explainlib/result.py", "result.RESULT_KEYS is missing")
        elif got is None:
            add("paseo-explain/references/result.schema.json", "result.schema.json properties are missing")
        elif got != expected:
            add(
                "paseo-explain/references/result.schema.json",
                "result.schema.json properties "
                f"{sorted(got)} != result.RESULT_KEYS {sorted(expected)}",
            )

    if explain_schema is not None:
        names = set()
        _property_names(explain_schema, names)
        schema_md_rel = "paseo-explain/references/schema.md"
        schema_md, schema_md_err = _read_text(repo / schema_md_rel)
        if schema_md_err:
            add(schema_md_rel, f"schema.md cannot be read: {schema_md_err}")
        else:
            for name in sorted(names):
                if f"`{name}`" not in schema_md:
                    add(schema_md_rel, f"schema.md does not mention `{name}` in backticks")

    template_rel = "paseo-explain/assets/template.html"
    template, template_err = _read_text(repo / template_rel)
    if template_err:
        add(template_rel, f"template cannot be read: {template_err}")
    else:
        for token, expected in PLACEHOLDERS.items():
            count = template.count(token)
            if count != expected:
                add(template_rel, f"placeholder {token} appears {count} times, expected {expected}")
        for needle in FORBIDDEN:
            if needle in template:
                add(template_rel, f"forbidden string {needle}")

    render_mod, render_err = _import_module("explainlib.render")
    if render_err:
        add("paseo-explain/scripts/explainlib/render.py", render_err)
    else:
        if getattr(render_mod, "MERMAID_SRC", None) != MERMAID_SRC:
            add("paseo-explain/scripts/explainlib/render.py", "render.MERMAID_SRC != the pinned Mermaid URL")
        if getattr(render_mod, "MERMAID_SRI", None) != MERMAID_SRI:
            add("paseo-explain/scripts/explainlib/render.py", "render.MERMAID_SRI != the pinned Mermaid hash")

    tracked, skip = _tracked(repo)
    if skip:
        warnings.append({"path": "", "message": skip})
    else:
        for mode, rel in tracked:
            full = repo / rel
            if rel not in R1_FILES:
                add(rel, f"tracked file is outside the repository layout: {rel}")
            if mode == "120000" or full.is_symlink():
                add(rel, f"tracked symlink: {rel}")
            elif mode != "100644" or (full.exists() and not full.is_symlink() and full.stat().st_mode & 0o111):
                add(rel, f"tracked file is executable: {rel}")

    return {"ok": not errors, "errors": errors, "warnings": warnings}
