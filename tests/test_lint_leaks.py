"""Tests for repository lint and the OPSEC leak scanner."""

import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LINT_SRC = REPO / "paseo-explain" / "scripts" / "explainlib" / "lint.py"
LEAKS_SRC = REPO / "paseo-explain" / "scripts" / "explainlib" / "leaks.py"
SCRIPTS = REPO / "paseo-explain" / "scripts"

CORE_RULE = (
    "Every claim points at evidence, every quote is verbatim, "
    "and what is not known is said to be unknown."
)
MERMAID_SRC = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
MERMAID_SRI = "sha384-rbtjAdnIQE/aQJGEgXrVUlMibdfTSa4PQju4HDhN3sR2PmaKFzhEafuePsl9H/9I"

sys.path.insert(0, str(SCRIPTS))

from explainlib import leaks  # noqa: E402


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o644)


def _git(repo: Path, *args: str) -> None:
    env = os.environ.copy()
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


def _parts(*parts: str) -> str:
    return "".join(parts)


def _skill() -> str:
    return (
        "---\n"
        "name: paseo-explain\n"
        "description: Explain a plan.\n"
        "metadata:\n"
        '  version: "0.1.0"\n'
        '  compatibility: "Python 3.10+."\n'
        "---\n"
        "\n"
        f"{CORE_RULE}\n"
        "\n"
        "references/pipeline.md\n"
        "references/prompts.md\n"
        "references/schema.md\n"
        "references/display.md\n"
        "references/integration.md\n"
        "references/design.md\n"
    )


def _template() -> str:
    return (
        "<!-- Adapted from acme/widget-skill. See THIRD_PARTY_LICENSES.md. -->\n"
        "<!doctype html>\n"
        '<html lang="{{LANG}}">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>{{TITLE}}</title>\n"
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        "script-src 'nonce-{{NONCE}}' https://cdn.jsdelivr.net; "
        "style-src 'unsafe-inline'; img-src data:; font-src 'none'; "
        "connect-src 'none'; base-uri 'none'; form-action 'none'\">\n"
        f'<script src="{{{{MERMAID_SRC}}}}" integrity="{{{{MERMAID_SRI}}}}"></script>\n'
        '<script type="application/json" id="pe-data">{{DATA_JSON}}</script>\n'
        "</head>\n"
        '<body><script nonce="{{NONCE}}"></script></body>\n'
        "</html>\n"
    )


def _explain_schema() -> str:
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "sections": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"heading": {"type": "string"}},
                    },
                },
            },
            "$defs": {
                "note": {"type": "object", "properties": {"note_body": {"type": "string"}}}
            },
        },
        indent=2,
    )


def _result_schema() -> str:
    return json.dumps(
        {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {"status": {"type": "string"}, "checks": {"type": "object"}},
        },
        indent=2,
    )


def _schema_md() -> str:
    return (
        "# Schema\n"
        "\n"
        "When to read: the explain.json fields.\n"
        "\n"
        "`title` `sections` `heading` `note_body`\n"
    )


def build_lint_repo(root: Path, *, git: bool = True) -> None:
    skill = root / "paseo-explain"
    _write(skill / "SKILL.md", _skill())
    _write(
        skill / "references" / "pipeline.md",
        f"# Pipeline\n\nWhen to read: the stage procedure.\n\n{CORE_RULE}\n",
    )
    for name, title in (
        ("prompts.md", "Prompts"),
        ("display.md", "Display"),
        ("integration.md", "Integration"),
        ("design.md", "Design"),
    ):
        _write(
            skill / "references" / name,
            f"# {title}\n\nWhen to read: this reference.\n",
        )
    _write(skill / "references" / "schema.md", _schema_md())
    _write(skill / "references" / "explain.schema.json", _explain_schema() + "\n")
    _write(skill / "references" / "result.schema.json", _result_schema() + "\n")
    _write(skill / "assets" / "template.html", _template())
    _write(skill / "scripts" / "explainlib" / "__init__.py", '"""Stub package."""\n__version__ = "0.1.0"\n')
    _write(
        skill / "scripts" / "explainlib" / "validate.py",
        '"""Stub validator."""\nEXPLAIN_TOP_KEYS = frozenset({"title", "sections"})\n',
    )
    _write(
        skill / "scripts" / "explainlib" / "result.py",
        '"""Stub result."""\nRESULT_KEYS = frozenset({"status", "checks"})\n',
    )
    _write(
        skill / "scripts" / "explainlib" / "render.py",
        '"""Stub renderer."""\n'
        f"MERMAID_SRC = {MERMAID_SRC!r}\n"
        f"MERMAID_SRI = {MERMAID_SRI!r}\n",
    )
    _write(skill / "scripts" / "explainlib" / "lint.py", LINT_SRC.read_text(encoding="utf-8"))
    if git:
        _git(root, "init")
        _git(root, "add", "-A")


def run_lint(root: Path, *, default_root: bool = False) -> dict:
    script = (
        "import json, os, sys\n"
        "sys.path.insert(0, os.environ['PE_SCRIPTS'])\n"
        "from explainlib.lint import lint\n"
        "root = None if os.environ.get('PE_DEFAULT') == '1' else os.environ['PE_ROOT']\n"
        "json.dump(lint(root), sys.stdout)\n"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PE_SCRIPTS"] = str(root / "paseo-explain" / "scripts")
    env["PE_ROOT"] = str(root)
    env["PE_DEFAULT"] = "1" if default_root else "0"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
    )
    if proc.returncode != 0:
        raise AssertionError(
            f"lint subprocess failed ({proc.returncode})\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )
    return json.loads(proc.stdout)


def messages(report: dict) -> list[str]:
    return [item["message"] for item in report["errors"]]


class LintTests(unittest.TestCase):
    def _repo(self, mutate=None, *, git=True):
        tmp = tempfile.TemporaryDirectory(prefix="pe-T4-att-T4-1-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        build_lint_repo(root, git=False)
        if mutate:
            mutate(root)
        if git:
            _git(root, "init")
            _git(root, "add", "-A")
        return root

    def test_clean_repository_ok(self):
        root = self._repo()
        explicit = run_lint(root)
        default = run_lint(root, default_root=True)
        for report in (explicit, default):
            self.assertTrue(report["ok"], report)
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["warnings"], [])

    def test_line_count(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "SKILL.md"
            path.write_text(path.read_text(encoding="utf-8") + ("\n" * 220), encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertFalse(report["ok"])
        self.assertTrue(any("200" in msg and "SKILL.md" in msg for msg in messages(report)))

    def test_frontmatter_name(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "SKILL.md"
            path.write_text(path.read_text(encoding="utf-8").replace("name: paseo-explain", "name: other"), encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("name" in msg for msg in messages(report)))

    def test_metadata_version_must_equal_package_version(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "SKILL.md"
            path.write_text(path.read_text(encoding="utf-8").replace('"0.1.0"', '"9.9.9"'), encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("metadata.version" in msg for msg in messages(report)))

    def test_frontmatter_missing_key(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "SKILL.md"
            text = path.read_text(encoding="utf-8").replace('  compatibility: "Python 3.10+."\n', "")
            path.write_text(text, encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("compatibility" in msg for msg in messages(report)))

    def test_core_rule_missing_from_skill(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "SKILL.md"
            path.write_text(path.read_text(encoding="utf-8").replace(CORE_RULE, "a shorter rule"), encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("core rule" in msg and "SKILL.md" in msg for msg in messages(report)))

    def test_core_rule_missing_from_pipeline(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "references" / "pipeline.md"
            path.write_text("# Pipeline\n\nWhen to read: the stage procedure.\n", encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("core rule" in msg and "pipeline.md" in msg for msg in messages(report)))

    def test_unresolved_reference_link(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "SKILL.md"
            path.write_text(path.read_text(encoding="utf-8") + "references/missing.md\n", encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("missing.md" in msg and "resolve" in msg for msg in messages(report)))

    def test_missing_reference_file(self):
        def mutate(root: Path) -> None:
            (root / "paseo-explain" / "references" / "display.md").unlink()

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("display.md" in msg for msg in messages(report)))

    def test_reference_requires_title(self):
        def mutate(root: Path) -> None:
            _write(root / "paseo-explain" / "references" / "prompts.md", "Prompts without a heading\n")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("title" in msg and "prompts.md" in msg for msg in messages(report)))

    def test_explain_schema_must_parse(self):
        def mutate(root: Path) -> None:
            _write(root / "paseo-explain" / "references" / "explain.schema.json", "{")

        report = run_lint(self._repo(mutate))
        self.assertFalse(report["ok"])
        self.assertTrue(any("explain.schema.json" in msg and "JSON" in msg for msg in messages(report)))

    def test_explain_schema_keys_match_validator(self):
        def mutate(root: Path) -> None:
            _write(
                root / "paseo-explain" / "scripts" / "explainlib" / "validate.py",
                '"""Stub validator."""\nEXPLAIN_TOP_KEYS = frozenset({"title"})\n',
            )

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("EXPLAIN_TOP_KEYS" in msg for msg in messages(report)))

    def test_result_schema_must_parse(self):
        def mutate(root: Path) -> None:
            _write(root / "paseo-explain" / "references" / "result.schema.json", "{")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("result.schema.json" in msg and "JSON" in msg for msg in messages(report)))

    def test_result_schema_keys_match_result_module(self):
        def mutate(root: Path) -> None:
            _write(
                root / "paseo-explain" / "scripts" / "explainlib" / "result.py",
                '"""Stub result."""\nRESULT_KEYS = frozenset({"status"})\n',
            )

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("RESULT_KEYS" in msg for msg in messages(report)))

    def test_missing_validate_is_an_error(self):
        def mutate(root: Path) -> None:
            (root / "paseo-explain" / "scripts" / "explainlib" / "validate.py").unlink()

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("explainlib.validate" in msg for msg in messages(report)))

    def test_missing_result_is_an_error(self):
        def mutate(root: Path) -> None:
            (root / "paseo-explain" / "scripts" / "explainlib" / "result.py").unlink()

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("explainlib.result" in msg for msg in messages(report)))

    def test_missing_render_is_an_error(self):
        def mutate(root: Path) -> None:
            (root / "paseo-explain" / "scripts" / "explainlib" / "render.py").unlink()

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("explainlib.render" in msg for msg in messages(report)))

    def test_schema_md_mentions_nested_property(self):
        def mutate(root: Path) -> None:
            text = (root / "paseo-explain" / "references" / "schema.md").read_text(encoding="utf-8")
            _write(root / "paseo-explain" / "references" / "schema.md", text.replace("`note_body`", "note_body"))

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("note_body" in msg and "schema.md" in msg for msg in messages(report)))

    def test_placeholder_counts(self):
        cases = {
            "{{LANG}}": ("{{LANG}}", 1),
            "{{TITLE}}": ("{{TITLE}}", 1),
            "{{DATA_JSON}}": ("{{DATA_JSON}}", 1),
            "{{MERMAID_SRC}}": ("{{MERMAID_SRC}}", 1),
            "{{MERMAID_SRI}}": ("{{MERMAID_SRI}}", 1),
            "{{NONCE}}": ("{{NONCE}}", 2),
        }
        for token, (label, expected) in cases.items():
            with self.subTest(token=token):
                def mutate(root: Path, token=token) -> None:
                    path = root / "paseo-explain" / "assets" / "template.html"
                    path.write_text(path.read_text(encoding="utf-8").replace(token, token + token, 1), encoding="utf-8")

                report = run_lint(self._repo(mutate))
                self.assertTrue(any(label in msg and "placeholder" in msg for msg in messages(report)))
                self.assertTrue(any(str(expected) in msg for msg in messages(report)))

    def test_forbidden_template_strings(self):
        forbidden = [
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
            'setTimeout("',
            "setTimeout('",
            "javascript:",
        ]
        for needle in forbidden:
            with self.subTest(needle=needle):
                def mutate(root: Path, needle=needle) -> None:
                    path = root / "paseo-explain" / "assets" / "template.html"
                    path.write_text(path.read_text(encoding="utf-8") + "\n" + needle + "\n", encoding="utf-8")

                report = run_lint(self._repo(mutate))
                self.assertTrue(any("forbidden" in msg and needle in msg for msg in messages(report)))

    def test_mermaid_src(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "scripts" / "explainlib" / "render.py"
            path.write_text(path.read_text(encoding="utf-8").replace(MERMAID_SRC, MERMAID_SRC + ".bad"), encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("MERMAID_SRC" in msg for msg in messages(report)))

    def test_mermaid_sri(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "scripts" / "explainlib" / "render.py"
            path.write_text(path.read_text(encoding="utf-8").replace(MERMAID_SRI, "sha384-wrong"), encoding="utf-8")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("MERMAID_SRI" in msg for msg in messages(report)))

    def test_tracked_file_outside_layout(self):
        def mutate(root: Path) -> None:
            _write(root / "NOTES-NOT-IN-R1.txt", "extra\n")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("NOTES-NOT-IN-R1.txt" in msg for msg in messages(report)))

    def test_executable_tracked_file(self):
        def mutate(root: Path) -> None:
            path = root / "paseo-explain" / "SKILL.md"
            path.chmod(0o755)

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("executable" in msg and "SKILL.md" in msg for msg in messages(report)))

    def test_tracked_symlink(self):
        def mutate(root: Path) -> None:
            target = root / "paseo-explain" / "SKILL.md"
            link = root / "README.md"
            link.symlink_to(target)

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("symlink" in msg and "README.md" in msg for msg in messages(report)))

    def test_claude_md_symlink_to_agents_md_is_allowed(self):
        def mutate(root: Path) -> None:
            _write(root / "AGENTS.md", "# Agents\n")
            (root / "CLAUDE.md").symlink_to("AGENTS.md")

        report = run_lint(self._repo(mutate))
        self.assertFalse(any("CLAUDE.md" in msg for msg in messages(report)), messages(report))

    def test_claude_md_must_point_to_agents_md(self):
        def mutate(root: Path) -> None:
            (root / "CLAUDE.md").symlink_to("README.md")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("CLAUDE.md must be a symlink to AGENTS.md" in msg for msg in messages(report)))

    def test_claude_md_regular_file_is_rejected(self):
        def mutate(root: Path) -> None:
            _write(root / "CLAUDE.md", "# copy\n")

        report = run_lint(self._repo(mutate))
        self.assertTrue(any("CLAUDE.md must be a symlink to AGENTS.md" in msg for msg in messages(report)))

    def test_outside_git_skips_tracked_file_check(self):
        root = self._repo(git=False)
        report = run_lint(root)
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["errors"], [])
        self.assertTrue(any("skipped" in item["message"] for item in report["warnings"]))


class LeakTests(unittest.TestCase):
    def test_pattern_strings_match_the_spec(self):
        got = [(kind, pattern.pattern) for kind, pattern in leaks.PATTERNS]
        self.assertEqual(
            got,
            [
                ("email", r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
                ("local-path", r"/home/[a-z]"),
                ("local-path", r"/Users/[A-Za-z]"),
                ("local-path", r"C:\\Users\\"),
                ("workspace-path", "/work" + "space/"),
                ("uuid", r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
                ("paseo-id", r"\b(wks|prj)_[0-9a-f]{8,}\b"),
                ("secret", r"\bsk-[A-Za-z0-9]{8,}"),
                ("secret", r"\bghp_[A-Za-z0-9]{8,}"),
                ("secret", r"\bAKIA[0-9A-Z]{16}\b"),
                ("secret", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
            ],
        )

    def test_each_kind_hits(self):
        samples = {
            "email": "reach me at " + _parts("person@", "real-domain.test") + " today\n",
            "local-path-home": "notes live in " + _parts("/ho", "me/a") + "/docs\n",
            "local-path-users": "notes live in " + _parts("/Us", "ers/A") + "/docs\n",
            "local-path-windows": "notes live in " + _parts("C:\\", "Users\\") + "A\\docs\n",
            "workspace-path": "tree " + _parts("/work", "space/") + "paseo-explain/src\n",
            "uuid": "id " + _parts("12345678-1234-1234-1234-", "123456789abc") + " end\n",
            "paseo-id-wks": "open " + _parts("wks_", "abcdef01") + " next\n",
            "paseo-id-prj": "open " + _parts("prj_", "1234abcd") + " next\n",
            "secret-sk": "token " + _parts("sk-", "abcdefgh") + "\n",
            "secret-ghp": "token " + _parts("ghp_", "abcdefgh") + "\n",
            "secret-akia": "token " + _parts("AKIA", "0000000000000000") + "\n",
            "secret-key": _parts("-----BEGIN ", "RSA PRIVATE KEY-----") + "\n",
        }
        expected = {
            "email": "email",
            "local-path-home": "local-path",
            "local-path-users": "local-path",
            "local-path-windows": "local-path",
            "workspace-path": "workspace-path",
            "uuid": "uuid",
            "paseo-id-wks": "paseo-id",
            "paseo-id-prj": "paseo-id",
            "secret-sk": "secret",
            "secret-ghp": "secret",
            "secret-akia": "secret",
            "secret-key": "secret",
        }
        with tempfile.TemporaryDirectory(prefix="pe-T4-att-T4-1-") as tmp:
            root = Path(tmp)
            for name, text in samples.items():
                _write(root / f"{name}.txt", text)
            report = leaks.leaks_paths([str(root)])
        self.assertFalse(report["ok"])
        found = {(hit["file"], hit["kind"]) for hit in report["hits"]}
        for name, kind in expected.items():
            self.assertIn((f"{name}.txt", kind), found)
        self.assertTrue(all(set(hit) == {"file", "line", "kind", "excerpt"} for hit in report["hits"]))

    def test_allowed_values_do_not_hit(self):
        text = "\n".join(
            [
                "noreply@example.invalid",
                "person@example.com",
                "person@example.org",
                "person@example.invalid",
                "person@sub.example.com",
                "User@Example.ORG",
                "/workspace/project-a",
                "/workspace/project-a/src",
                '"/workspace/project-a"',
                "/workspace/example",
                "/workspace/example/src",
                "see /workspace/example.",
                "ABCDEF12-1234-1234-1234-123456789ABC",
            ]
        )
        self.assertEqual(leaks.scan_text(text, []), [])
        with tempfile.TemporaryDirectory(prefix="pe-T4-att-T4-1-") as tmp:
            path = Path(tmp) / "allowed.txt"
            _write(path, text + "\n")
            report = leaks.leaks_paths([str(path)])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["hits"], [])

    def test_disallowed_near_misses_hit(self):
        text = _parts("person@", "example.net ") + _parts("/work", "space/", "project-ab ") + _parts("/work", "space/", "examples\n")
        kinds = {hit["kind"] for hit in leaks.scan_text(text, [])}
        self.assertIn("email", kinds)
        self.assertIn("workspace-path", kinds)

    def test_opsec_extra_is_case_insensitive(self):
        with tempfile.TemporaryDirectory(prefix="pe-T4-att-T4-1-") as tmp:
            root = Path(tmp)
            _write(root / ".opsec-extra", "# ignored\n\nGardenToken\n")
            _write(root / "note.txt", "see gardentoken here\n")
            report = leaks.leaks_paths([str(root)])
        extra_hits = [hit for hit in report["hits"] if hit["kind"] == "extra"]
        self.assertEqual([hit["file"] for hit in extra_hits], ["note.txt"])
        self.assertEqual(extra_hits[0]["line"], 1)

    def test_copyright_lines_and_credited_repo_names_are_exempt(self):
        with tempfile.TemporaryDirectory(prefix="pe-T4-att-T4-1-") as tmp:
            root = Path(tmp)
            _write(root / ".opsec-extra", "Unique Person\nacme/widget-skill\n")
            _write(root / "LICENSE", "Copyright (c) 2026 Unique Person\nUnique Person again\n")
            _write(
                root / "THIRD_PARTY_LICENSES.md",
                "Copyright (c) 2026 Unique Person\nAdapted from acme/widget-skill.\n",
            )
            _write(root / "paseo-explain" / "references" / "design.md", "Credit acme/widget-skill.\n")
            _write(
                root / "paseo-explain" / "assets" / "template.html",
                "<!-- acme/widget-skill -->\nacme/widget-skill in the body\n",
            )
            _write(root / "other.txt", "Unique Person and acme/widget-skill\n")
            report = leaks.leaks_paths([str(root)])
        files = {(hit["file"], hit["line"], hit["kind"]) for hit in report["hits"]}
        self.assertIn(("LICENSE", 2, "extra"), files)
        self.assertNotIn(("LICENSE", 1, "extra"), files)
        self.assertFalse(any(hit["file"] == "THIRD_PARTY_LICENSES.md" for hit in report["hits"]))
        self.assertNotIn(("paseo-explain/references/design.md", 1, "extra"), files)
        self.assertNotIn(("paseo-explain/assets/template.html", 1, "extra"), files)
        self.assertNotIn(("paseo-explain/assets/template.html", 2, "extra"), files)
        self.assertIn(("other.txt", 1, "extra"), files)

    def test_binary_and_non_utf8_are_skipped(self):
        with tempfile.TemporaryDirectory(prefix="pe-T4-att-T4-1-") as tmp:
            root = Path(tmp)
            (root / "blob.bin").write_bytes(b"\xff\xfe " + _parts("person@", "real-domain.test").encode())
            _write(root / "note.txt", _parts("person@", "real-domain.test") + "\n")
            report = leaks.leaks_paths([str(root)])
        self.assertEqual([hit["file"] for hit in report["hits"]], ["note.txt"])
        self.assertEqual(report["hits"][0]["kind"], "email")

    def test_repo_scan_uses_git_and_untracked_extra_terms(self):
        with tempfile.TemporaryDirectory(prefix="pe-T4-att-T4-1-") as tmp:
            root = Path(tmp)
            _write(root / "tracked.txt", _parts("person@", "real-domain.test") + "\nSECRETWORD\n")
            _write(root / "untracked.txt", _parts("person@", "real-domain.test") + "\n")
            _write(root / ".opsec-extra", "SecretWord\n")
            _git(root, "init")
            _git(root, "add", "tracked.txt")
            report = leaks.leaks_repo(str(root))
        files = {hit["file"] for hit in report["hits"]}
        self.assertIn("tracked.txt", files)
        self.assertNotIn("untracked.txt", files)
        self.assertNotIn(".opsec-extra", files)
        kinds = {hit["kind"] for hit in report["hits"] if hit["file"] == "tracked.txt"}
        self.assertEqual(kinds, {"email", "extra"})

    def test_leaks_repo_default_root_follows_script_path(self):
        with tempfile.TemporaryDirectory(prefix="pe-T4-att-T4-1-") as tmp:
            root = Path(tmp)
            dest = root / "paseo-explain" / "scripts" / "explainlib" / "leaks.py"
            _write(dest, LEAKS_SRC.read_text(encoding="utf-8"))
            _write(root / "paseo-explain" / "scripts" / "explainlib" / "__init__.py", '"""Stub package."""\n')
            _write(root / "tracked.txt", _parts("person@", "real-domain.test") + "\n")
            _git(root, "init")
            _git(root, "add", "tracked.txt")
            script = (
                "import json, os, sys\n"
                "sys.path.insert(0, os.environ['PE_SCRIPTS'])\n"
                "from explainlib.leaks import leaks_repo\n"
                "json.dump(leaks_repo(None), sys.stdout)\n"
            )
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            env["PE_SCRIPTS"] = str(root / "paseo-explain" / "scripts")
            proc = subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                env=env,
                cwd=str(root),
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads(proc.stdout)
        self.assertFalse(report["ok"])
        self.assertEqual(report["hits"][0]["file"], "tracked.txt")
        self.assertEqual(report["hits"][0]["kind"], "email")

    def test_scan_text_reports_line_numbers(self):
        address = _parts("person@", "real-domain.test")
        hits = leaks.scan_text("ok\n" + address + "\n", [])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["line"], 2)
        self.assertEqual(hits[0]["kind"], "email")
        self.assertIn(address, hits[0]["excerpt"])
