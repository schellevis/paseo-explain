"""Tests for evidence ingest, block splitting, and structured extraction."""

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

sys.path.insert(0, str(helpers.REPO / "paseo-explain" / "scripts"))

from explainlib import common
from explainlib.ingest import (
    extract_structured,
    fragment_blocks,
    fragment_paragraphs,
    ingest,
    split_blocks,
)

RUN = helpers.FIXTURES / "autopilot-run"

EXPECTED_TASKS = [
    {
        "id": "T1",
        "title": "booking data model",
        "wave": 1,
        "dependencies": [],
        "owned_files": ["app/models/plot.py", "app/models/booking.py"],
        "requirement_refs": ["R1", "R2"],
    },
    {
        "id": "T2",
        "title": "booking overlap rule",
        "wave": 1,
        "dependencies": ["T1"],
        "owned_files": ["app/services/booking.py"],
        "requirement_refs": ["R2"],
    },
    {
        "id": "T3",
        "title": "waitlist order",
        "wave": 2,
        "dependencies": ["T1"],
        "owned_files": ["app/services/waitlist.py"],
        "requirement_refs": ["R3"],
    },
    {
        "id": "T4",
        "title": "overlap checks",
        "wave": 2,
        "dependencies": ["T1", "T2"],
        "owned_files": ["app/services/overlap.py"],
        "requirement_refs": ["R1", "R2"],
    },
    {
        "id": "T5",
        "title": "season calendar view",
        "wave": 3,
        "dependencies": ["T3", "T4"],
        "owned_files": ["app/views/calendar.py"],
        "requirement_refs": ["R2"],
    },
]


def tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
        digest.update(str(path.stat().st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _line_count(data: bytes) -> int:
    text = data.decode("utf-8").replace("\r\n", "\n")
    if text == "":
        return 0
    parts = text.split("\n")
    if text.endswith("\n"):
        parts = parts[:-1]
    return len(parts)


class SplitTests(unittest.TestCase):
    def test_anchors_preamble_empty_block_and_lines(self):
        sample = "Intro\n\n# Section\n\nbody\n## Empty\n### Child\ntext\n"
        blocks = split_blocks(sample)
        self.assertEqual(
            [(b["anchor"], b["line_start"], b["line_end"], b["level"], b["heading"], b["text"]) for b in blocks],
            [
                ("§(preamble)", 1, 2, 0, "", "Intro\n\n"),
                ("§Section", 3, 5, 1, "# Section", "# Section\n\nbody\n"),
                ("§Empty", 6, 6, 2, "## Empty", "## Empty\n"),
                ("§Child", 7, 8, 3, "### Child", "### Child\ntext\n"),
            ],
        )
        frags = fragment_blocks(blocks, "S1", 1)
        self.assertEqual(frags[2]["text"], "## Empty\n")
        self.assertEqual(frags[2]["anchor"], "§Empty")
        self.assertEqual("".join(b["text"] for b in blocks), sample)

    def test_preamble_omitted_when_file_starts_with_heading(self):
        blocks = split_blocks("# Section\nbody\n")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["anchor"], "§Section")
        self.assertEqual(split_blocks(""), [])

    def test_crlf_normalised(self):
        self.assertEqual(
            split_blocks("Intro\r\n\r\n# Section\r\n"),
            split_blocks("Intro\n\n# Section\n"),
        )

    def test_only_hash_levels_one_to_four_split(self):
        sample = "# Title\n#### Deep\n##### stay\n## Next\n"
        blocks = split_blocks(sample)
        self.assertEqual([b["anchor"] for b in blocks], ["§Title", "§Deep", "§Next"])
        self.assertIn("##### stay\n", blocks[1]["text"])
        self.assertEqual("".join(b["text"] for b in blocks), sample)

    def test_four_thousand_character_split_and_long_line(self):
        exact = "x" * 4000
        one = fragment_blocks(split_blocks(exact), "S1", 1)
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0]["anchor"], "§(preamble)")
        self.assertEqual(one[0]["text"], exact)

        over = "y" * 4001
        parts = fragment_blocks(split_blocks(over), "S9", 4)
        self.assertEqual([f["id"] for f in parts], ["E4", "E5"])
        self.assertEqual(parts[0]["source"], "S9")
        self.assertEqual(parts[0]["text"], "y" * 4000)
        self.assertEqual(parts[1]["text"], "y")
        self.assertEqual(parts[0]["anchor"], "§(preamble)")
        self.assertEqual(parts[1]["anchor"], "§(preamble) (part 2)")
        self.assertEqual((parts[0]["line_start"], parts[0]["line_end"]), (1, 1))
        self.assertEqual((parts[1]["line_start"], parts[1]["line_end"]), (1, 1))
        self.assertFalse(parts[0]["flagged"])

        packed = (("m" * 50) + "\n") * 100
        packed_frags = fragment_blocks(split_blocks(packed), "S1", 1)
        self.assertGreaterEqual(len(packed_frags), 2)
        self.assertTrue(all(len(f["text"]) <= 4000 for f in packed_frags))
        self.assertTrue(packed_frags[0]["text"].endswith("\n"))
        self.assertEqual("".join(f["text"] for f in packed_frags), packed)
        self.assertEqual(packed_frags[1]["anchor"], "§(preamble) (part 2)")
        self.assertEqual(packed_frags[1]["line_start"], packed_frags[0]["line_end"] + 1)

        long = "hi\n" + ("Z" * 5000) + "\n"
        long_frags = fragment_blocks(split_blocks(long), "S1", 1)
        self.assertEqual(long_frags[0]["text"], "hi\n")
        self.assertEqual((long_frags[0]["line_start"], long_frags[0]["line_end"]), (1, 1))
        self.assertEqual(long_frags[1]["line_start"], 2)
        self.assertEqual(long_frags[1]["line_end"], 2)
        self.assertEqual(long_frags[1]["anchor"], "§(preamble) (part 2)")
        self.assertTrue(all(len(f["text"]) <= 4000 for f in long_frags))
        self.assertEqual("".join(f["text"] for f in long_frags), long)

    def test_paragraph_anchors(self):
        frags = fragment_paragraphs("Alpha line\n\nBeta\n", "S1", 3)
        self.assertEqual(frags[0]["id"], "E3")
        self.assertEqual(frags[0]["anchor"], "¶1")
        self.assertEqual(frags[0]["text"], "Alpha line\n")
        self.assertEqual((frags[0]["line_start"], frags[0]["line_end"]), (1, 1))
        self.assertEqual(frags[1]["anchor"], "¶2")
        self.assertEqual(frags[1]["text"], "Beta\n")
        self.assertEqual((frags[1]["line_start"], frags[1]["line_end"]), (3, 3))

        more = fragment_paragraphs("Alpha\nstill\n   \n\nBeta", "S2", 1)
        self.assertEqual(len(more), 2)
        self.assertEqual(more[0]["text"], "Alpha\nstill\n")
        self.assertEqual((more[0]["line_start"], more[0]["line_end"]), (1, 2))
        self.assertEqual(more[1]["text"], "Beta")
        self.assertEqual(more[1]["line_start"], 5)
        self.assertEqual(more[1]["source"], "S2")


class ExtractTests(unittest.TestCase):
    def test_lists_wave_order_refs_and_none(self):
        plan = (
            "# Implementation plan: notes\n\n"
            "### Task T3: third\n"
            "- WAVE: 2\n"
            "- Dependencies: none for now\n"
            "- Owned files: none\n"
            "- Acceptance criteria: see R2 before R1 and R2 again\n\n"
            "### Task T1: first\n"
            "- Wave: 1\n"
            "- Dependencies: `T9` (draft); T8\n"
            "- Owned Files: `app/a.py` (new), `app/b.py`\n"
            "- Acceptance criteria: covers R8\n\n"
            "### Task T2: second\n"
            "- Wave: soon\n"
            "- Dependencies: T1\n"
            "- Owned files: `app/c.py`(new)\n"
            "- Acceptance criteria: write the paper log\n"
        )
        blocks = split_blocks(plan)
        frags = fragment_blocks(blocks, "S1", 1)
        structured = extract_structured([({"id": "S1", "role": "plan", "display": "03-plan.md"}, blocks, frags)])
        self.assertEqual(structured["requirements"], [])
        self.assertEqual(structured["findings"], [])
        self.assertEqual(
            [
                (
                    task["id"],
                    task["title"],
                    task["wave"],
                    task["dependencies"],
                    task["owned_files"],
                    task["requirement_refs"],
                )
                for task in structured["tasks"]
            ],
            [
                ("T3", "third", 2, [], [], ["R2", "R1"]),
                ("T1", "first", 1, ["T9", "T8"], ["app/a.py", "app/b.py"], ["R8"]),
                ("T2", "second", None, ["T1"], ["app/c.py"], []),
            ],
        )
        self.assertEqual(structured["waves"], [["T1"], ["T3"]])
        self.assertTrue(structured["refs_present"])
        self.assertEqual(structured["tasks"][0]["evidence"], frags[1]["id"])

    def test_requirement_heading_optional_dot(self):
        spec = "### R6 Heading text\nbody\n### R7. Dotted\n"
        blocks = split_blocks(spec)
        frags = fragment_blocks(blocks, "S1", 1)
        structured = extract_structured([({"id": "S1", "role": "spec", "display": "01-spec.md"}, blocks, frags)])
        self.assertEqual(
            [(item["id"], item["title"]) for item in structured["requirements"]],
            [("R6", "Heading text"), ("R7", "Dotted")],
        )
        self.assertFalse(structured["refs_present"])

    def test_findings_region_and_other_doc(self):
        text = (
            "# Review resolution: notes\n\n"
            "### SKIP-ME\n"
            "- Outcome: rejected\n"
            "- Reason: outside\n\n"
            "## Finding decisions\n\n"
            "### F-1\n"
            "- Outcome: accepted\n"
            "- Reason: keep the paper log\n\n"
            "### F-2\n\n"
            "## Later notes\n\n"
            "### SKIP-LATER\n"
            "- Outcome: deferred\n"
            "- Reason: after\n"
        )
        blocks = split_blocks(text)
        frags = fragment_blocks(blocks, "S4", 1)
        structured = extract_structured(
            [({"id": "S4", "role": "resolution", "display": "notes.md"}, blocks, frags)]
        )
        self.assertEqual(
            [(item["id"], item["doc"], item["outcome"], item["reason"]) for item in structured["findings"]],
            [("F-1", "other", "accepted", "keep the paper log"), ("F-2", "other", "", "")],
        )
        self.assertIn("### F-1", next(f["text"] for f in frags if f["id"] == structured["findings"][0]["evidence"]))


class IngestCliTests(unittest.TestCase):
    def _ingest(self, env, session, *args):
        return helpers.run_cli("ingest", "--session", str(session), *map(str, args), env=env)

    def test_fixture_plan_and_spec_and_unchanged_tree(self):
        before = tree_hash(RUN)
        with helpers.temp_home() as env:
            session = helpers.make_session(env, "plan", autopilot=str(RUN), doc="plan")
            code, out, err = self._ingest(env, session, "--autopilot", RUN, "--doc", "plan")
            self.assertEqual(err, "")
            self.assertEqual(code, 0, out)
            payload = json.loads(out)
            evidence = json.loads((session / "evidence.json").read_text(encoding="utf-8"))
            scanned = json.loads((session / "scan.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["ok"], True)
            self.assertEqual(payload["sources"], 5)
            self.assertEqual(payload["fragments"], len(evidence["fragments"]))
            self.assertEqual(payload["flagged"], 0)
            self.assertEqual(
                payload["structured"],
                {"requirements": 4, "tasks": 5, "findings": 3, "refs_present": True},
            )
            self.assertEqual(evidence["explain_schema"], 1)
            self.assertEqual(scanned["explain_schema"], 1)
            self.assertEqual(scanned["flags"], [])
            names = ["00-brief.md", "01-spec.md", "02-spec-resolution.md", "03-plan.md", "04-plan-resolution.md"]
            roles = ["brief", "spec", "resolution", "plan", "resolution"]
            self.assertEqual([(s["display"], s["role"]) for s in evidence["sources"]], list(zip(names, roles)))
            self.assertEqual([s["id"] for s in evidence["sources"]], [f"S{i}" for i in range(1, 6)])
            self.assertEqual([f["id"] for f in evidence["fragments"]], [f"E{i}" for i in range(1, len(evidence["fragments"]) + 1)])
            self.assertTrue(all(isinstance(f["flagged"], bool) and f["flagged"] is False for f in evidence["fragments"]))
            for source, name in zip(evidence["sources"], names):
                raw = (RUN / name).read_bytes()
                self.assertEqual(source["display"], name)
                self.assertNotIn("/", source["display"])
                self.assertEqual(source["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(source["lines"], _line_count(raw))
                owned = "".join(f["text"] for f in evidence["fragments"] if f["source"] == source["id"])
                self.assertEqual(owned, raw.decode("utf-8").replace("\r\n", "\n"))
            blob = (session / "evidence.json").read_text(encoding="utf-8")
            self.assertNotIn(str(RUN), blob)
            reqs = [(item["id"], item["title"]) for item in evidence["structured"]["requirements"]]
            self.assertEqual(
                reqs,
                [("R1", "Plot records"), ("R2", "Overlap rules"), ("R3", "Waitlist"), ("R4", "Public calendar")],
            )
            tasks = evidence["structured"]["tasks"]
            self.assertEqual(
                [{k: task[k] for k in ("id", "title", "wave", "dependencies", "owned_files", "requirement_refs")} for task in tasks],
                EXPECTED_TASKS,
            )
            self.assertEqual(evidence["structured"]["waves"], [["T1", "T2"], ["T3", "T4"], ["T5"]])
            self.assertTrue(evidence["structured"]["refs_present"])
            plan_text = (RUN / "03-plan.md").read_text(encoding="utf-8")
            self.assertIn(
                "- Waves: wave 1: T5; wave 2: T1, T2; wave 3: T3, T4 (deferred docs excluded)",
                plan_text,
            )
            plan_blocks = split_blocks(plan_text)
            t3 = next(block for block in plan_blocks if block["heading"] == "### Task T3: waitlist order")
            t4 = next(block for block in plan_blocks if block["heading"] == "### Task T4: overlap checks")
            self.assertNotIn("test", t3["text"].lower())
            self.assertGreater(len(t4["text"]), 4000)
            refs = {ref for task in tasks for ref in task["requirement_refs"]}
            self.assertNotIn("R4", refs)
            self.assertIn("R3", refs)
            task4 = next(task for task in tasks if task["id"] == "T4")
            t4_frags = [
                f
                for f in evidence["fragments"]
                if f["anchor"] == "§Task T4: overlap checks" or f["anchor"].startswith("§Task T4: overlap checks (part ")
            ]
            self.assertGreaterEqual(len(t4_frags), 2)
            self.assertEqual(task4["evidence"], t4_frags[0]["id"])
            self.assertNotIn("R2", t4_frags[0]["text"])
            self.assertIn("Also covers R2.", "".join(f["text"] for f in t4_frags))
            self.assertEqual("".join(f["text"] for f in t4_frags), t4["text"])
            self.assertTrue(all(len(f["text"]) <= 4000 for f in t4_frags))
            findings = evidence["structured"]["findings"]
            self.assertEqual(
                [(item["id"], item["doc"], item["outcome"], item["reason"]) for item in findings],
                [
                    ("SR-B1", "spec", "accepted", "plots stay on paper at the hall"),
                    ("SR-B2", "spec", "accepted", "the waitlist is optional"),
                    ("PR-B1", "plan", "accepted", "the calendar stays read only"),
                ],
            )
            for item in findings:
                frag = next(f for f in evidence["fragments"] if f["id"] == item["evidence"])
                self.assertIn(f"### {item['id']}", frag["text"])
            self.assertEqual(sorted(p.name for p in session.iterdir()), ["evidence.json", "scan.json", "session.json"])

            code, out, err = self._ingest(env, session, "--autopilot", RUN, "--doc", "spec")
            self.assertEqual(code, 0, out)
            spec_payload = json.loads(out)
            spec_evidence = json.loads((session / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(
                [(s["display"], s["role"]) for s in spec_evidence["sources"]],
                [("00-brief.md", "brief"), ("01-spec.md", "spec"), ("02-spec-resolution.md", "resolution")],
            )
            self.assertEqual(spec_payload["structured"]["tasks"], 0)
            self.assertEqual(spec_payload["structured"]["requirements"], 4)
            self.assertEqual(spec_payload["structured"]["findings"], 2)
            self.assertFalse(spec_payload["structured"]["refs_present"])
            self.assertEqual([item["doc"] for item in spec_evidence["structured"]["findings"]], ["spec", "spec"])

            plan_again = helpers.make_session(env, "plan", slug="garden-again")
            code, _out, err = self._ingest(env, plan_again, "--autopilot", RUN, "--doc", "plan")
            self.assertEqual(code, 0, err)
            evidence_bytes = (plan_again / "evidence.json").read_bytes()
            scan_bytes = (plan_again / "scan.json").read_bytes()
            code, _out, err = self._ingest(env, plan_again, "--autopilot", RUN, "--doc", "plan")
            self.assertEqual(code, 0, err)
            self.assertEqual((plan_again / "evidence.json").read_bytes(), evidence_bytes)
            self.assertEqual((plan_again / "scan.json").read_bytes(), scan_bytes)
        self.assertEqual(tree_hash(RUN), before)

    def test_missing_sources_exit_1(self):
        with helpers.temp_home() as env, tempfile.TemporaryDirectory(prefix="pe-T2-att-T2-1-") as tmp:
            run = Path(tmp) / "run"
            shutil.copytree(RUN, run)
            (run / "01-spec.md").unlink()
            session = helpers.make_session(env, "plan", slug="missing-spec")
            code, out, err = self._ingest(env, session, "--autopilot", run, "--doc", "spec")
            self.assertEqual(code, 1, err)
            body = json.loads(out)
            self.assertFalse(body["ok"])
            self.assertIn("01-spec.md", body["errors"][0]["message"])
            self.assertFalse((session / "evidence.json").exists())

            shutil.copytree(RUN, run, dirs_exist_ok=True)
            (run / "03-plan.md").unlink()
            code, out, err = self._ingest(env, session, "--autopilot", run, "--doc", "plan")
            self.assertEqual(code, 1, err)
            self.assertIn("03-plan.md", json.loads(out)["errors"][0]["message"])

    def test_optional_autopilot_files_and_reads_only(self):
        with helpers.temp_home() as env, tempfile.TemporaryDirectory(prefix="pe-T2-att-T2-1-") as tmp:
            run = Path(tmp) / "run"
            shutil.copytree(RUN, run)
            (run / "run.json").write_text('{"secret":"do-not-read"}\n', encoding="utf-8")
            for name in ("00-brief.md", "02-spec-resolution.md", "04-plan-resolution.md"):
                (run / name).unlink()
            before = tree_hash(run)
            session = helpers.make_session(env, "plan", autopilot=str(run), doc="plan")
            opened = []
            real_open = open

            def tracking(path, mode="r", *args, **kwargs):
                full = os.path.abspath(path)
                root = os.path.abspath(run)
                if full == root or full.startswith(root + os.sep):
                    self.assertEqual(mode, "rb", path)
                    opened.append(os.path.basename(full))
                return real_open(path, mode, *args, **kwargs)

            with mock.patch("builtins.open", side_effect=tracking), mock.patch(
                "explainlib.ingest.write_json", wraps=common.write_json
            ) as spy:
                result = ingest(session, autopilot=str(run), doc="plan")
            self.assertEqual(result["structured"]["requirements"], 4)
            self.assertEqual(result["structured"]["tasks"], 5)
            self.assertEqual(result["structured"]["findings"], 0)
            self.assertTrue(result["structured"]["refs_present"])
            self.assertEqual(
                opened,
                [
                    "00-brief.md",
                    "01-spec.md",
                    "02-spec-resolution.md",
                    "03-plan.md",
                    "04-plan-resolution.md",
                ],
            )
            self.assertNotIn("run.json", opened)
            self.assertEqual([Path(call.args[0]).name for call in spy.call_args_list], ["evidence.json", "scan.json"])
            evidence = json.loads((session / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual([(s["display"], s["role"]) for s in evidence["sources"]], [("01-spec.md", "spec"), ("03-plan.md", "plan")])
            self.assertEqual(tree_hash(run), before)
            self.assertEqual((run / "run.json").read_text(encoding="utf-8"), '{"secret":"do-not-read"}\n')

    def test_non_utf8_exits_1(self):
        with helpers.temp_home() as env, tempfile.TemporaryDirectory(prefix="pe-T2-att-T2-1-") as tmp:
            path = Path(tmp) / "bad.md"
            path.write_bytes(b"\xff\xfe not text\n")
            session = helpers.make_session(env, "plan", slug="bad-bytes")
            code, out, err = self._ingest(env, session, "--file", f"other={path}")
            self.assertEqual(code, 1, err)
            self.assertIn("utf-8", json.loads(out)["errors"][0]["message"])
            self.assertFalse((session / "evidence.json").exists())

    def test_display_names_are_basenames(self):
        with helpers.temp_home() as env, tempfile.TemporaryDirectory(prefix="pe-T2-att-T2-1-") as tmp:
            nested = Path(tmp) / "deep" / "dir"
            nested.mkdir(parents=True)
            spec = nested / "01-spec.md"
            spec.write_bytes((RUN / "01-spec.md").read_bytes())
            crlf = nested / "notes.md"
            crlf.write_bytes(b"# Title\r\n\r\nbody\r\n")
            session = helpers.make_session(env, "plan", slug="basename")
            code, out, err = self._ingest(env, session, "--file", f"spec={spec}", "--file", f"other={crlf}")
            self.assertEqual(code, 0, err + out)
            evidence = json.loads((session / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual([(s["display"], s["role"]) for s in evidence["sources"]], [("01-spec.md", "spec"), ("notes.md", "other")])
            self.assertEqual(evidence["sources"][1]["lines"], 3)
            self.assertEqual(evidence["sources"][1]["sha256"], hashlib.sha256(crlf.read_bytes()).hexdigest())
            self.assertEqual(evidence["fragments"][-1]["text"], "# Title\n\nbody\n")
            blob = (session / "evidence.json").read_text(encoding="utf-8")
            self.assertNotIn(str(nested), blob)
            self.assertEqual(json.loads(out)["structured"]["requirements"], 4)

    def test_text_file_paragraph_and_markdown_modes(self):
        with helpers.temp_home() as env, tempfile.TemporaryDirectory(prefix="pe-T2-att-T2-1-") as tmp:
            paras = Path(tmp) / "paras.md"
            paras.write_text("First paragraph here.\n\nSecond paragraph here.\n", encoding="utf-8")
            headed = Path(tmp) / "headed.md"
            headed.write_text("# Shelf notes\n\nBorrow a ladder.\n", encoding="utf-8")
            session = helpers.make_session(env, "idea", slug="modes")
            code, out, err = self._ingest(env, session, "--text-file", paras)
            self.assertEqual(code, 0, err + out)
            evidence = json.loads((session / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual([f["anchor"] for f in evidence["fragments"]], ["¶1", "¶2"])
            self.assertEqual(evidence["sources"][0]["role"], "idea")
            self.assertEqual(evidence["sources"][0]["display"], "paras.md")
            code, out, err = self._ingest(env, session, "--text-file", headed)
            self.assertEqual(code, 0, err + out)
            evidence = json.loads((session / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["fragments"][0]["anchor"], "§Shelf notes")
            self.assertFalse(any(f["anchor"].startswith("¶") for f in evidence["fragments"]))

    def test_plan_without_requirement_refs(self):
        with helpers.temp_home() as env, tempfile.TemporaryDirectory(prefix="pe-T2-att-T2-1-") as tmp:
            path = Path(tmp) / "03-plan.md"
            path.write_text(
                "# Implementation plan: notes\n\n"
                "### Task T9: paper log\n"
                "- Wave: 1\n"
                "- Dependencies: none\n"
                "- Owned files: `app/notes.py`\n"
                "- Acceptance criteria: write the paper log\n",
                encoding="utf-8",
            )
            session = helpers.make_session(env, "plan", slug="no-refs")
            code, out, err = self._ingest(env, session, "--file", f"plan={path}")
            self.assertEqual(code, 0, err + out)
            payload = json.loads(out)
            self.assertFalse(payload["structured"]["refs_present"])
            evidence = json.loads((session / "evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["structured"]["tasks"][0]["owned_files"], ["app/notes.py"])
            self.assertEqual(evidence["structured"]["waves"], [["T9"]])
            self.assertEqual(evidence["structured"]["tasks"][0]["requirement_refs"], [])
