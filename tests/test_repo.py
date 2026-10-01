import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "paseo-explain" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from explainlib import repo  # noqa: E402
from explainlib.common import ExplainError  # noqa: E402
from repo_helpers import FIXTURE_REPO, copy_fixture, make_git_repo, tree_state  # noqa: E402

HAS_GIT = shutil.which("git") is not None
TOKEN = "sk-" + "A" * 24


class TempCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pe-repo-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(os.path.realpath(self._tmp.name))

    def make(self, files, name="repo"):
        root = self.tmp / name
        root.mkdir(parents=True, exist_ok=True)
        for rel, data in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return root

    def listing(self, root):
        return repo.list_repository(os.path.realpath(root))

    def by_path(self, listing):
        return {r["path"]: r for r in listing["files"]}


class CanonTest(unittest.TestCase):
    def test_canon_rule(self):
        self.assertEqual(repo.canon("My Repo_1!", "repo"), "my-repo-1")
        self.assertEqual(repo.canon("---", "repo"), "repo")
        self.assertEqual(repo.canon("", "area"), "area")

    def test_tables(self):
        self.assertIn("node_modules", repo.PRUNE_DIRS)
        self.assertIn(".md", repo.DOC_EXTENSIONS)
        self.assertEqual(repo.LANG_BY_EXT[".tsx"], "TypeScript")


class FixtureTest(unittest.TestCase):
    def test_fixture_readme_sections(self):
        text = (FIXTURE_REPO / "README.md").read_text(encoding="utf-8")
        run = text.split("## Run")[1].split("## ")[0]
        test = text.split("## Test")[1].split("## ")[0]
        self.assertIn('python3 -m tasklist add "Buy milk"', run)
        self.assertIn("python3 -m unittest", test)

    def test_fixture_files(self):
        found = sorted(
            p.relative_to(FIXTURE_REPO).as_posix() for p in FIXTURE_REPO.rglob("*") if p.is_file()
        )
        self.assertEqual(
            found,
            [
                "AGENTS.md",
                "README.md",
                "checks/check_store.py",
                "docs/overview.md",
                "pyproject.toml",
                "src/tasklist/__init__.py",
                "src/tasklist/__main__.py",
                "src/tasklist/cli.py",
                "src/tasklist/store.py",
            ],
        )


class WalkModeTest(TempCase):
    def test_fixture_walk(self):
        root = copy_fixture(self.tmp / "fx")
        result = self.listing(root)
        self.assertEqual(result["repo"], {"name": "fx", "git": False, "head": None})
        paths = [r["path"] for r in result["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertTrue(all("\\" not in p for p in paths))
        rec = self.by_path(result)["src/tasklist/store.py"]
        self.assertEqual(rec["kind"], "text")
        self.assertEqual(rec["lang"], "Python")
        self.assertIsNone(rec["reason"])
        self.assertEqual(len(rec["sha256"]), 64)
        self.assertEqual(rec["size"], (root / "src/tasklist/store.py").stat().st_size)
        self.assertEqual(rec["lines"], len((root / "src/tasklist/store.py").read_text().splitlines()))
        self.assertEqual(set(rec), {"path", "size", "lines", "sha256", "kind", "lang", "reason"})

    def test_prune_list(self):
        files = {"keep.py": "x = 1\n"}
        for name in sorted(repo.PRUNE_DIRS):
            files[f"{name}/inner.txt"] = "hidden\n"
            files[f"sub/{name}/deep.txt"] = "hidden\n"
        result = self.listing(self.make(files))
        self.assertEqual([r["path"] for r in result["files"]], ["keep.py"])

    def test_doc_and_code_files(self):
        root = self.make(
            {
                "README.md": "# t\n",
                "notes.TXT": "n\n",
                "main.py": "print(1)\n",
                "data.json": "{}\n",
                "blob.bin": b"\x00\x01",
                "Makefile": "all:\n",
            }
        )
        files = self.listing(root)["files"]
        docs = [r["path"] for r in files if repo.is_doc_file(r)]
        self.assertEqual(docs, ["README.md", "notes.TXT"])
        count, size = repo.code_totals(files)
        self.assertEqual(count, 2)  # data.json is data, not code
        self.assertEqual(size, len("print(1)\n") + len("all:\n"))
        self.assertEqual(self.by_path({"files": files})["Makefile"]["lang"], "Make")
        self.assertEqual(self.by_path({"files": files})["notes.TXT"]["lang"], "Text")

    def test_binary_kinds(self):
        root = self.make({"nul.dat": b"abc\x00def", "latin.txt": b"caf\xe9\n", "ok.txt": "café\n"})
        recs = self.by_path(self.listing(root))
        self.assertEqual(recs["nul.dat"]["kind"], "binary")
        self.assertIsNone(recs["nul.dat"]["lines"])
        self.assertEqual(recs["latin.txt"]["kind"], "binary")
        self.assertEqual(len(recs["latin.txt"]["sha256"]), 64)
        self.assertEqual(recs["ok.txt"]["kind"], "text")
        self.assertEqual(recs["ok.txt"]["lines"], 1)

    def test_nul_after_sniff_window_is_text_when_valid(self):
        data = b"a" * 9000 + b"\x00"
        recs = self.by_path(self.listing(self.make({"late.dat": data})))
        self.assertEqual(recs["late.dat"]["kind"], "text")

    def test_large_kind(self):
        root = self.make({"edge.txt": b"a" * 409600, "big.txt": b"a" * 409601})
        recs = self.by_path(self.listing(root))
        self.assertEqual(recs["edge.txt"]["kind"], "text")
        self.assertEqual(recs["big.txt"]["kind"], "large")
        self.assertEqual(recs["big.txt"]["lines"], 1)
        self.assertEqual(recs["big.txt"]["size"], 409601)

    def test_symlink_outside_repository(self):
        outside = self.make({"outside.txt": "private notes\n"}, name="outside")
        root = self.make({"a.txt": "a\n"})
        os.symlink(outside / "outside.txt", root / "link.txt")
        rec = self.by_path(self.listing(root))["link.txt"]
        self.assertEqual(rec["kind"], "excluded")
        self.assertEqual(rec["reason"], "symlink outside repository")
        self.assertIsNone(rec["lines"])
        self.assertIsNone(rec["sha256"])

    def test_symlink_inside_repository_is_read(self):
        root = self.make({"a.txt": "a\n"})
        os.symlink("a.txt", root / "b.txt")
        os.symlink(".", root / "loop")
        recs = self.by_path(self.listing(root))
        self.assertEqual(recs["b.txt"]["kind"], "text")
        self.assertNotIn("loop", recs)

    def test_secret_file_by_name(self):
        root = self.make({".env": "A=1\n", ".env.example": "A=\n", "server.pem": "x\n", "ok.py": "1\n"})
        recs = self.by_path(self.listing(root))
        self.assertEqual(recs[".env"]["reason"], "secret file")
        self.assertEqual(recs[".env"]["kind"], "excluded")
        self.assertIsNone(recs[".env"]["sha256"])
        self.assertEqual(recs[".env"]["size"], 4)
        self.assertEqual(recs["server.pem"]["reason"], "secret file")
        self.assertEqual(recs[".env.example"]["kind"], "text")

    def test_symlink_readme_to_env(self):
        root = self.make({".env": "A=1\n"})
        os.symlink(".env", root / "README.md")
        recs = self.by_path(self.listing(root))
        self.assertEqual(recs["README.md"]["kind"], "excluded")
        self.assertEqual(recs["README.md"]["reason"], "secret file")
        self.assertIsNone(recs["README.md"]["sha256"])

    def test_hard_link_of_secret_in_walk_mode(self):
        root = self.make({".env": "A=1\n"})
        try:
            os.link(root / ".env", root / "notes.txt")
        except OSError:
            self.skipTest("hard links unavailable")
        recs = self.by_path(self.listing(root))
        self.assertEqual(recs["notes.txt"]["reason"], "secret file")

    def test_secret_like_path_placeholder(self):
        first = f"zz-{TOKEN}.txt"
        second = f"docs/{TOKEN}-b.md"
        root = self.make({first: "a\n", second: "b\n", "plain.txt": "c\n"})
        result = self.listing(root)
        recs = self.by_path(result)
        self.assertEqual(sorted(recs), ["(withheld-path-1)", "(withheld-path-2)", "plain.txt"])
        for key in ("(withheld-path-1)", "(withheld-path-2)"):
            self.assertEqual(recs[key]["reason"], "secret-like path")
            self.assertEqual(recs[key]["kind"], "excluded")
            self.assertIsNone(recs[key]["sha256"])
        # listing order is sorted order: docs/... before zz-...
        self.assertEqual(recs["(withheld-path-1)"]["size"], 2)
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_secret_like_path_takes_precedence_over_secret_file(self):
        name = f"credentials-{TOKEN}.txt"
        root = self.make({name: "a\n"})
        result = self.listing(root)
        rec = result["files"][0]
        self.assertEqual(rec["path"], "(withheld-path-1)")
        self.assertEqual(rec["reason"], "secret-like path")
        self.assertNotIn(TOKEN, json.dumps(result))

    def test_limit(self):
        root = self.tmp / "many"
        root.mkdir()
        for i in range(20001):
            (root / f"f{i}").touch()
        with self.assertRaises(ExplainError) as ctx:
            self.listing(root)
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(str(ctx.exception), "repository too large: 20001 files (limit 20000)")


@unittest.skipUnless(HAS_GIT, "git not available")
class GitModeTest(TempCase):
    def git(self, root, *args):
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", HOME=str(self.tmp), XDG_CONFIG_HOME=str(self.tmp))
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        return subprocess.run(
            ["git", "-C", str(root), *args], env=env, check=True, capture_output=True, text=True
        ).stdout.strip()

    def test_git_listing_of_fixture(self):
        root = make_git_repo(FIXTURE_REPO, self.tmp / "g")
        result = self.listing(root)
        self.assertTrue(result["repo"]["git"])
        self.assertIsNone(result["repo"]["head"])
        self.assertEqual(
            [r["path"] for r in result["files"]],
            sorted(p.relative_to(FIXTURE_REPO).as_posix() for p in FIXTURE_REPO.rglob("*") if p.is_file()),
        )

    def test_head_and_gitignore(self):
        root = make_git_repo(FIXTURE_REPO, self.tmp / "g")
        (root / ".gitignore").write_text("ignored.txt\n")
        (root / "ignored.txt").write_text("i\n")
        (root / "new.txt").write_text("n\n")
        (root / "node_modules").mkdir()
        (root / "node_modules" / "tracked.js").write_text("x\n")
        self.git(root, "add", "-A")
        self.git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "m")
        (root / "gone.txt").write_text("g\n")
        self.git(root, "add", "gone.txt")
        (root / "gone.txt").unlink()
        result = self.listing(root)
        paths = [r["path"] for r in result["files"]]
        self.assertEqual(result["repo"]["head"], self.git(root, "rev-parse", "HEAD"))
        self.assertIn("new.txt", paths)
        self.assertIn("node_modules/tracked.js", paths)
        self.assertNotIn("ignored.txt", paths)
        self.assertNotIn("gone.txt", paths)

    def test_subdirectory_of_repository(self):
        root = make_git_repo(FIXTURE_REPO, self.tmp / "g")
        result = self.listing(root / "src")
        self.assertTrue(result["repo"]["git"])
        self.assertEqual(result["repo"]["name"], "src")
        self.assertEqual(
            [r["path"] for r in result["files"]],
            ["tasklist/__init__.py", "tasklist/__main__.py", "tasklist/cli.py", "tasklist/store.py"],
        )

    def test_hard_link_of_gitignored_env(self):
        root = make_git_repo(FIXTURE_REPO, self.tmp / "g")
        (root / ".gitignore").write_text(".env\n")
        (root / ".env").write_text("A=1\n")
        try:
            os.link(root / ".env", root / "settings.txt")
        except OSError:
            self.skipTest("hard links unavailable")
        recs = self.by_path(self.listing(root))
        self.assertNotIn(".env", recs)
        self.assertEqual(recs["settings.txt"]["kind"], "excluded")
        self.assertEqual(recs["settings.txt"]["reason"], "secret file")

    def test_symlink_to_gitignored_env(self):
        root = make_git_repo(FIXTURE_REPO, self.tmp / "g")
        (root / ".gitignore").write_text(".env\n")
        (root / ".env").write_text("A=1\n")
        os.symlink(".env", root / "link.txt")
        recs = self.by_path(self.listing(root))
        self.assertEqual(recs["link.txt"]["reason"], "secret file")

    def test_tree_state_unchanged(self):
        root = make_git_repo(FIXTURE_REPO, self.tmp / "g")
        self.git(root, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "-m", "m")
        (root / "untracked.txt").write_text("u\n")
        before = tree_state(root)
        self.assertTrue(any(k.startswith(".git/") for k in before))
        self.listing(root)
        self.assertEqual(tree_state(root), before)


class FakeGitTest(TempCase):
    def fake(self, mode="ok"):
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir(exist_ok=True)
        log = self.tmp / "git.log"
        script = bin_dir / "git"
        listed = "exit 1" if mode == "fail" else r"printf 'a.txt\0b.txt\0a.txt\0'"
        script.write_text(
            "#!/bin/sh\n"
            f'LOG="{log}"\n'
            'echo "ARGV $*" >> "$LOG"\n'
            'echo "ENV GIT_OPTIONAL_LOCKS=$GIT_OPTIONAL_LOCKS GIT_CONFIG_NOSYSTEM=$GIT_CONFIG_NOSYSTEM '
            'GIT_TERMINAL_PROMPT=$GIT_TERMINAL_PROMPT GIT_DIR=${GIT_DIR-unset} GIT_WORK_TREE=${GIT_WORK_TREE-unset}" >> "$LOG"\n'
            'case "$*" in\n'
            "  *is-inside-work-tree*) echo true;;\n"
            f"  *ls-files*) {listed};;\n"
            "  *rev-parse*HEAD*) echo 0123456789abcdef0123456789abcdef01234567;;\n"
            "esac\n"
        )
        script.chmod(0o755)
        return bin_dir, log

    def run_listing(self, mode="ok"):
        bin_dir, log = self.fake(mode)
        root = self.make({"a.txt": "a\n", "b.txt": "b\n", "ignored.txt": "i\n"})
        path = f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"
        with mock.patch.dict(os.environ, {"PATH": path, "GIT_DIR": "/nonexistent", "GIT_WORK_TREE": "/nonexistent"}):
            result = self.listing(root)
        return result, log.read_text().splitlines()

    def test_hardening_options_and_environment(self):
        result, lines = self.run_listing()
        self.assertTrue(result["repo"]["git"])
        self.assertEqual(result["repo"]["head"], "0123456789abcdef0123456789abcdef01234567")
        self.assertEqual([r["path"] for r in result["files"]], ["a.txt", "b.txt"])
        argv = [line[5:].split() for line in lines if line.startswith("ARGV ")]
        self.assertEqual(len(argv), 3)
        opts = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false"]
        for call in argv:
            self.assertEqual(call[0], "-C")
            self.assertEqual(call[2:8], opts)
        self.assertEqual(argv[0][8:], ["rev-parse", "--is-inside-work-tree"])
        self.assertEqual(argv[1][8:], ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
        self.assertEqual(argv[2][8:], ["rev-parse", "HEAD"])
        envs = [line for line in lines if line.startswith("ENV ")]
        self.assertEqual(len(envs), 3)
        for line in envs:
            self.assertEqual(
                line,
                "ENV GIT_OPTIONAL_LOCKS=0 GIT_CONFIG_NOSYSTEM=1 GIT_TERMINAL_PROMPT=0 GIT_DIR=unset GIT_WORK_TREE=unset",
            )

    def test_failed_ls_files_falls_back_to_walk(self):
        result, _lines = self.run_listing("fail")
        self.assertFalse(result["repo"]["git"])
        self.assertIsNone(result["repo"]["head"])
        self.assertEqual([r["path"] for r in result["files"]], ["a.txt", "b.txt", "ignored.txt"])

    def test_no_git_on_path_uses_walk(self):
        root = self.make({"a.txt": "a\n"})
        with mock.patch.object(repo.shutil, "which", return_value=None):
            result = self.listing(root)
        self.assertFalse(result["repo"]["git"])


class ReadOnlyTest(TempCase):
    def test_walk_mode_leaves_tree_unchanged(self):
        root = copy_fixture(self.tmp / "fx")
        (root / ".env").write_text("A=1\n")
        before = tree_state(root)
        self.listing(root)
        self.assertEqual(tree_state(root), before)


if __name__ == "__main__":
    unittest.main()


def rec(path, kind="text", lines=1, size=10, sha=None, lang=None, reason=None):
    if kind == "excluded":
        lines, sha = None, None
    elif kind == "binary":
        lines = None
    if sha is None and kind != "excluded":
        sha = "h:" + path
    return {"path": path, "size": size, "lines": lines, "sha256": sha, "kind": kind, "lang": lang, "reason": reason}


def listing_of(files, git=False, head=None, name="proj"):
    return {"repo": {"name": name, "git": git, "head": head}, "files": files}


def section(text, heading):
    return text.split(f"## {heading}\n")[1].split("\n## ")[0].rstrip("\n").split("\n")


FIXTURE_MANIFEST = """# Repository manifest: tasklist

## Summary
- Files: 9 (text 9, large 0, binary 0, excluded 0)
- Lines in text files: 150
- Git: no; HEAD none

## Languages
- Python: 5 files, 102 lines
- Markdown: 3 files, 40 lines
- TOML: 1 files, 8 lines

## Tree
- checks/ (1 files, 22 lines)
- docs/ (1 files, 11 lines)
- src/ (4 files, 80 lines)
  - src/tasklist/ (4 files, 80 lines)

## Build and run files
- pyproject.toml

## Entrypoint candidates
Candidates found by name only.
- src/tasklist/__main__.py
- src/tasklist/cli.py

## Excluded files
- none
"""


class ManifestTest(TempCase):
    def test_fixture_manifest_exact(self):
        root = copy_fixture(self.tmp / "tasklist")
        self.assertEqual(repo.build_manifest(self.listing(root)), FIXTURE_MANIFEST)

    def test_manifest_is_deterministic(self):
        root = copy_fixture(self.tmp / "tasklist")
        listing = self.listing(root)
        self.assertEqual(repo.build_manifest(listing), repo.build_manifest(listing))

    def test_empty_sections_and_head(self):
        text = repo.build_manifest(listing_of([], git=True, head="0123456789abcdef0123"))
        self.assertIn("- Files: 0 (text 0, large 0, binary 0, excluded 0)", text)
        self.assertIn("- Git: yes; HEAD 0123456789ab\n", text)
        for heading in ("Languages", "Tree", "Build and run files", "Excluded files"):
            self.assertEqual(section(text, heading), ["- none"])
        self.assertEqual(section(text, "Entrypoint candidates"), ["Candidates found by name only.", "- none"])

    def test_summary_counts_and_language_order(self):
        files = [
            rec("a.py", lines=5, lang="Python"),
            rec("b.go", lines=5, lang="Go"),
            rec("big.js", kind="large", lines=100, lang="JavaScript"),
            rec("img.png", kind="binary"),
            rec("blob.bin", kind="binary", lang="Python"),
            rec("x.pem", kind="excluded", reason="secret file", lang=None),
            rec("n.txt", lines=1, lang=None),
        ]
        text = repo.build_manifest(listing_of(files))
        self.assertIn("- Files: 7 (text 3, large 1, binary 2, excluded 1)", text)
        self.assertIn("- Lines in text files: 111", text)
        self.assertEqual(
            section(text, "Languages"),
            ["- JavaScript: 1 files, 100 lines", "- Go: 1 files, 5 lines", "- Python: 1 files, 5 lines"],
        )
        self.assertEqual(section(text, "Excluded files"), ["- x.pem (secret file)"])

    def test_tree_counts_all_non_excluded_lines_text_and_large(self):
        files = [
            rec("top.py", lines=9),
            rec("a/x.py", lines=3),
            rec("a/b/y.py", kind="large", lines=7),
            rec("a/b/z.bin", kind="binary"),
            rec("a/b/s.pem", kind="excluded", reason="secret file"),
            rec("a/b/c/d/deep.py", lines=2),
        ]
        tree = section(repo.build_manifest(listing_of(files)), "Tree")
        self.assertEqual(
            tree,
            [
                "- a/ (4 files, 12 lines)",
                "  - a/b/ (3 files, 9 lines)",
                "    - a/b/c/ (1 files, 2 lines)",
            ],
        )

    def test_tree_sorted_by_path_segments(self):
        files = [rec("a-x/f.py"), rec("a/b/f.py"), rec("a/f.py")]
        tree = section(repo.build_manifest(listing_of(files)), "Tree")
        self.assertEqual([t.strip() for t in tree], ["- a/ (2 files, 2 lines)", "- a/b/ (1 files, 1 lines)", "- a-x/ (1 files, 1 lines)"])

    def test_tree_truncates_at_300_with_uncounted_line(self):
        files = [rec(f"d{i:03d}/f.py") for i in range(305)]
        tree = section(repo.build_manifest(listing_of(files)), "Tree")
        self.assertEqual(len(tree), 301)
        self.assertEqual(tree[299], "- d299/ (1 files, 1 lines)")
        self.assertEqual(tree[300], "- … (5 more directories)")

    def test_tree_exactly_300_has_no_truncation_line(self):
        files = [rec(f"d{i:03d}/f.py") for i in range(300)]
        tree = section(repo.build_manifest(listing_of(files)), "Tree")
        self.assertEqual(len(tree), 300)
        self.assertNotIn("more directories", tree[-1])

    def test_tree_depth_three_by_default(self):
        files = [rec("a/b/c/d/f.py")]
        tree = section(repo.build_manifest(listing_of(files)), "Tree")
        self.assertEqual(len(tree), 3)

    def test_tree_depth_two_above_2000_files(self):
        files = [rec(f"a/b/c/f{i}.py") for i in range(2001)]
        tree = section(repo.build_manifest(listing_of(files)), "Tree")
        self.assertEqual(tree, ["- a/ (2001 files, 2001 lines)", "  - a/b/ (2001 files, 2001 lines)"])
        files = [rec(f"a/b/c/f{i}.py") for i in range(2000)]
        self.assertEqual(len(section(repo.build_manifest(listing_of(files)), "Tree")), 3)

    def test_build_files(self):
        files = [
            rec("package.json"),
            rec("sub/Makefile"),
            rec(".github/workflows/ci.yml"),
            rec(".gitlab-ci.yml"),
            rec("docs/package.json", kind="excluded", reason="secret file"),
            rec("src/main.py"),
            rec("compose.yaml"),
            rec("tox.ini"),
        ]
        self.assertEqual(
            repo.build_files(listing_of(files)),
            [".github/workflows/ci.yml", ".gitlab-ci.yml", "compose.yaml", "package.json", "sub/Makefile", "tox.ini"],
        )

    def test_build_files_cap_40(self):
        files = [rec(f"m{i:02d}/Makefile") for i in range(43)]
        lines = section(repo.build_manifest(listing_of(files)), "Build and run files")
        self.assertEqual(len(lines), 41)
        self.assertEqual(lines[-1], "- … (3 more)")
        self.assertEqual(len(repo.build_files(listing_of(files))), 43)

    def test_entrypoints(self):
        files = [
            rec("main.py"),
            rec("src/app.test.js"),
            rec("pkg/__main__.py"),
            rec("bin/tool"),
            rec("deep/cmd/serve.go"),
            rec("cmd/x/inner.go"),
            rec("lib/helper.py"),
            rec("big/index.js", kind="large"),
            rec("img/index.png", kind="binary"),
            rec("secret/main.py", kind="excluded", reason="secret file"),
            rec("Run.sh"),
        ]
        self.assertEqual(
            repo.entrypoints(listing_of(files)),
            ["bin/tool", "deep/cmd/serve.go", "main.py", "pkg/__main__.py", "src/app.test.js"],
        )

    def test_entrypoints_cap_30(self):
        files = [rec(f"m{i:02d}/main.py") for i in range(33)]
        lines = section(repo.build_manifest(listing_of(files)), "Entrypoint candidates")
        self.assertEqual(lines[0], "Candidates found by name only.")
        self.assertEqual(len(lines), 32)
        self.assertEqual(lines[-1], "- … (3 more)")

    def test_excluded_cap_50(self):
        files = [rec(f"k{i:02d}.pem", kind="excluded", reason="secret file") for i in range(52)]
        lines = section(repo.build_manifest(listing_of(files)), "Excluded files")
        self.assertEqual(len(lines), 51)
        self.assertEqual(lines[0], "- k00.pem (secret file)")
        self.assertEqual(lines[-1], "- … (2 more)")

    def test_withheld_path_placeholder_only(self):
        files = [rec("(withheld-path-1)", kind="excluded", reason="secret-like path")]
        text = repo.build_manifest(listing_of(files))
        self.assertIn("- (withheld-path-1) (secret-like path)", text)


def area_by_id(areas):
    return {a["id"]: a for a in areas}


class AreasTest(unittest.TestCase):
    def test_top_level_grouping_root_and_sorting(self):
        files = [
            rec("README.md", lines=4),
            rec("setup.py", lines=4),
            rec("src/a.py", lines=10),
            rec("src/sub/b.py", lines=5),
            rec("Docs/x.md", lines=2),
            rec("bin.dat", kind="binary"),
            rec("s.pem", kind="excluded", reason="secret file"),
            rec("big/z.py", kind="large", lines=50),
        ]
        areas = repo.compute_areas(listing_of(files))
        # Areas hold code files only: Docs/x.md and README.md are documentation, not surveyed.
        self.assertEqual([a["id"] for a in areas], ["big", "root", "src"])
        by = area_by_id(areas)
        self.assertEqual(by["root"]["paths"], ["(root files)"])
        self.assertEqual((by["root"]["files"], by["root"]["lines"]), (1, 4))
        self.assertEqual(by["src"]["paths"], ["src/"])
        self.assertEqual((by["src"]["files"], by["src"]["lines"]), (2, 15))
        self.assertEqual(by["big"]["files"], 1)
        for a in areas:
            self.assertEqual(set(a), {"id", "paths", "files", "lines", "files_sha256", "state", "survey"})
            self.assertEqual(a["state"], "missing")
            self.assertIsNone(a["survey"])

    def test_split_above_150_files(self):
        files = [rec(f"big/direct{i}.py") for i in range(10)]
        files += [rec(f"big/one/f{i}.py") for i in range(100)]
        files += [rec(f"big/two/deep/f{i}.py") for i in range(41)]
        listing = listing_of(files + [rec("other/x.py")])
        areas = repo.compute_areas(listing)
        self.assertEqual([a["id"] for a in areas], ["big-direct", "big-one", "big-two", "other"])
        by = area_by_id(areas)
        self.assertEqual(by["big-one"]["paths"], ["big/one/"])
        self.assertEqual(by["big-direct"]["paths"], ["big/ (direct files)"])
        self.assertEqual(
            (by["big-direct"]["files"], by["big-one"]["files"], by["big-two"]["files"]), (10, 100, 41)
        )
        members = repo.area_file_map(listing)
        flat = [f["path"] for fs in members.values() for f in fs]
        self.assertEqual(sorted(flat), sorted(r["path"] for r in listing["files"]))
        self.assertEqual(len(flat), len(set(flat)))
        self.assertTrue(all(f["path"].startswith("big/two/") for f in members["big-two"]))

    def test_no_split_at_150_or_without_subdirectories(self):
        files = [rec(f"big/s{i % 3}/f{i}.py") for i in range(150)]
        self.assertEqual([a["id"] for a in repo.compute_areas(listing_of(files))], ["big"])
        files = [rec(f"flat/f{i}.py") for i in range(200)]
        areas = repo.compute_areas(listing_of(files))
        self.assertEqual([(a["id"], a["files"]) for a in areas], [("flat", 200)])

    def test_split_above_20000_lines_without_direct_area(self):
        files = [rec("big/a/x.py", lines=15000), rec("big/b/y.py", lines=5001)]
        areas = repo.compute_areas(listing_of(files))
        self.assertEqual([a["id"] for a in areas], ["big-a", "big-b"])
        files = [rec("big/a/x.py", lines=15000), rec("big/b/y.py", lines=5000)]
        self.assertEqual([a["id"] for a in repo.compute_areas(listing_of(files))], ["big"])

    def test_ids_canon_empty_and_collisions(self):
        files = [rec("My Dir/a.py"), rec("my-dir/b.py"), rec("my_dir/c.py"), rec("root/d.py"), rec("top.py"), rec("---/e.py")]
        areas = repo.compute_areas(listing_of(files))
        self.assertEqual([a["id"] for a in areas], ["area", "my-dir", "my-dir-2", "my-dir-3", "root", "root-2"])
        by = area_by_id(areas)
        self.assertEqual(by["root"]["paths"], ["(root files)"])
        self.assertEqual(by["root-2"]["paths"], ["root/"])

    def test_deterministic_across_input_order(self):
        files = [rec(f"{d}/f.py") for d in ("My Dir", "my-dir", "my_dir")]
        a = repo.compute_areas(listing_of(files))
        b = repo.compute_areas(listing_of(list(reversed(files))))
        self.assertEqual(a, b)

    def test_empty_listing(self):
        self.assertEqual(repo.compute_areas(listing_of([])), [])


class FilesShaTest(unittest.TestCase):
    def test_exact_formula(self):
        import hashlib

        files = [rec("b.py", sha="22"), rec("a.py", sha="11")]
        expected = hashlib.sha256(b"a.py\x0011\nb.py\x0022\n").hexdigest()
        self.assertEqual(repo.area_files_sha256(files), expected)
        self.assertEqual(repo.area_files_sha256(list(reversed(files))), expected)
        self.assertEqual(repo.area_files_sha256([]), hashlib.sha256(b"").hexdigest())

    def test_change_detection(self):
        base = [rec("a.py", sha="11"), rec("b.py", sha="22")]
        before = repo.compute_areas(listing_of(base))[0]["files_sha256"]
        self.assertEqual(before, repo.compute_areas(listing_of(base))[0]["files_sha256"])
        changed = [rec("a.py", sha="11"), rec("b.py", sha="23")]
        self.assertNotEqual(before, repo.compute_areas(listing_of(changed))[0]["files_sha256"])
        added = base + [rec("c.py", sha="33")]
        self.assertNotEqual(before, repo.compute_areas(listing_of(added))[0]["files_sha256"])
        renamed = [rec("a.py", sha="11"), rec("c.py", sha="22")]
        self.assertNotEqual(before, repo.compute_areas(listing_of(renamed))[0]["files_sha256"])


class AreaStateTest(unittest.TestCase):
    def test_states(self):
        self.assertEqual(repo.area_state({"files_sha256": "x", "survey": None}), "missing")
        self.assertEqual(repo.area_state({"files_sha256": "x"}), "missing")
        self.assertEqual(repo.area_state({"files_sha256": "x", "survey": {"files_sha256": "x"}}), "fresh")
        self.assertEqual(repo.area_state({"files_sha256": "x", "survey": {"files_sha256": "y"}}), "stale")


SELECTION = {"docs": [{"path": "README.md", "sha256": "ab"}], "code": []}


class RepomapTest(TempCase):
    def test_load_missing_and_invalid(self):
        self.assertIsNone(repo.load_repomap(self.tmp))
        (self.tmp / "repomap.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(repo.load_repomap(self.tmp))
        (self.tmp / "repomap.json").write_bytes(b"\xff\xfe")
        self.assertIsNone(repo.load_repomap(self.tmp))
        (self.tmp / "repomap.json").write_text("[1]", encoding="utf-8")
        self.assertIsNone(repo.load_repomap(self.tmp))

    def test_write_and_load_roundtrip_atomic(self):
        obj = {"explain_schema": 1, "areas": [], "ünï": "ü"}
        with mock.patch("os.replace", wraps=os.replace) as replace:
            repo.write_repomap(self.tmp, obj)
        self.assertEqual(replace.call_count, 1)
        src, dst = replace.call_args[0]
        self.assertEqual(os.path.dirname(os.path.abspath(src)), str(self.tmp))
        self.assertEqual(Path(dst), self.tmp / "repomap.json")
        self.assertEqual(repo.load_repomap(self.tmp), obj)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["repomap.json"])

    def test_merge_shape_and_missing_state(self):
        listing = listing_of([rec("src/a.py", lang="Python"), rec("x.pem", kind="excluded", reason="secret file")], git=True, head="abc")
        areas = repo.compute_areas(listing)
        obj = repo.merge_repomap(None, listing, areas, SELECTION)
        self.assertEqual(set(obj), {"explain_schema", "repo", "generated_at", "files", "areas", "selection"})
        self.assertEqual(obj["explain_schema"], 1)
        self.assertEqual(obj["repo"], {"name": "proj", "git": True, "head": "abc"})
        self.assertRegex(obj["generated_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(obj["files"], listing["files"])
        self.assertEqual(obj["selection"], SELECTION)
        self.assertEqual([(a["id"], a["state"], a["survey"]) for a in obj["areas"]], [("src", "missing", None)])
        self.assertEqual(areas[0]["state"], "missing")

    def test_merge_carries_surveys_by_id_and_recomputes_state(self):
        old_listing = listing_of([rec("src/a.py", sha="1"), rec("lib/b.py", sha="2"), rec("gone/c.py", sha="3")])
        old_areas = repo.compute_areas(old_listing)
        old = repo.merge_repomap(None, old_listing, old_areas, SELECTION)
        for area in old["areas"]:
            area["survey"] = {"model": "m/x", "files_sha256": area["files_sha256"], "report": {"summary": area["id"]}}
            area["state"] = "fresh"
        new_listing = listing_of([rec("src/a.py", sha="1"), rec("lib/b.py", sha="CHANGED"), rec("new/d.py", sha="4")])
        new_areas = repo.compute_areas(new_listing)
        merged = repo.merge_repomap(old, new_listing, new_areas, {"docs": [], "code": []})
        by = area_by_id(merged["areas"])
        self.assertEqual(sorted(by), ["lib", "new", "src"])
        self.assertEqual(by["src"]["state"], "fresh")
        self.assertEqual(by["src"]["survey"]["report"], {"summary": "src"})
        self.assertEqual(by["lib"]["state"], "stale")
        self.assertEqual(by["lib"]["survey"]["report"], {"summary": "lib"})
        self.assertEqual(by["new"]["state"], "missing")
        self.assertIsNone(by["new"]["survey"])
        self.assertEqual(merged["selection"], {"docs": [], "code": []})

    def test_merge_ignores_malformed_old(self):
        listing = listing_of([rec("src/a.py")])
        areas = repo.compute_areas(listing)
        for old in ({}, {"areas": "x"}, {"areas": [1, {"id": "src", "survey": "bad"}, {"survey": {}}]}):
            merged = repo.merge_repomap(old, listing, areas, SELECTION)
            self.assertEqual([(a["id"], a["state"]) for a in merged["areas"]], [("src", "missing")])


class CodeClassificationTest(unittest.TestCase):
    def test_data_files_are_not_code_areas_or_entrypoints(self):
        files = [
            rec("src/app.py", lines=10),
            rec("src/cli.py", lines=5),
            rec("data/run.log", lines=900),
            rec("data/results.json", lines=900),
            rec("data/table.csv", lines=900),
            rec("lists/hosts.txt", lines=900),
            rec("static/app.css", lines=20),
            rec("web/index.html", lines=20),
            rec("logs/cli.log", lines=20),
        ]
        listing = listing_of(files)
        self.assertEqual([a["id"] for a in repo.compute_areas(listing)], ["src", "static", "web"])
        self.assertEqual(repo.entrypoints(listing), ["src/app.py", "src/cli.py"])
        count, _size = repo.code_totals(files)
        self.assertEqual(count, 4)
        self.assertFalse(repo.is_code_file(rec("data/results.json")))
        self.assertTrue(repo.is_code_file(rec("big/z.py", kind="large"), kinds=("text", "large")))
        self.assertFalse(repo.is_code_file(rec("big/z.py", kind="large")))

