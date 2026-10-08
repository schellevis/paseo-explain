"""Tests for check requests, reports, corrections, and result.json."""

import copy
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import helpers

import sys

sys.path.insert(0, str(helpers.REPO / "paseo-explain" / "scripts"))

from explainlib.common import canonical_sha256, current_hashes, sha256_file, write_json
from explainlib.result import RESULT_KEYS, compute_result, model_family
from explainlib.reports import factcheck_state, reader_state
from explainlib.validate import claim_leaves, claim_units

CORRECTION_SENTENCE = (
    "Gardeners keep one written reason so the chosen rule stays easy to explain "
    "at this level without adding extra claims."
)
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
QUESTIONS = (
    ("Is there a fee?", "No"),
    ("Where is the calendar?", "At the hall"),
    ("How many gardens?", "One"),
)


def _section_index(explain, section_id):
    return next(index for index, section in enumerate(explain["sections"]) if section["id"] == section_id)


def _cli(env, *args):
    code, out, err = helpers.run_cli(*args, env=env)
    return code, out, err


def _frame(session, env, orchestrator="example/model", questions=QUESTIONS):
    args = [
        "frame",
        "--session",
        session,
        "--audience",
        "Neighbours who use the garden or the hall",
        "--question",
        "What does this change for them?",
        "--out-of-scope",
        "Payments",
    ]
    for question, expected in questions:
        args.extend(["--check-question", f"{question}::{expected}"])
    if orchestrator is not None:
        args.extend(["--orchestrator-model", orchestrator])
    code, out, err = _cli(env, *args)
    if code != 0:
        raise AssertionError(f"frame failed: {code} {out} {err}")


def _init_plan(env, **init_opts):
    session = helpers.make_session(
        env,
        "plan",
        autopilot=str(helpers.FIXTURES / "autopilot-run"),
        doc="plan",
        levels=3,
        lang="en",
        **init_opts,
    )
    code, out, err = _cli(
        env,
        "ingest",
        "--session",
        session,
        "--autopilot",
        helpers.FIXTURES / "autopilot-run",
        "--doc",
        "plan",
    )
    if code != 0:
        raise AssertionError(f"ingest failed: {code} {out} {err}")
    _frame(session, env)
    shutil.copy(helpers.FIXTURES / "plan-explain.json", session / "explain.json")
    code, out, err = _cli(env, "validate", "--session", session)
    if code != 0:
        raise AssertionError(f"validate failed: {code} {out} {err}")
    return session


def _explain():
    return json.loads((helpers.FIXTURES / "plan-explain.json").read_text(encoding="utf-8"))


def _correction_leaf(explain):
    index = _section_index(explain, "decisions")
    return f"/sections/{index}/items/0/why/3"


def _risk_units(explain):
    index = _section_index(explain, "risks")
    return [f"/sections/{index}/items/0", f"/sections/{index}/items/1"]


def _claims(explain, overrides):
    claims = []
    for _unit, leaf in claim_leaves(explain):
        verdict, correction, text = overrides.get(
            leaf, ("verified", None, "This leaf matches the evidence.")
        )
        claims.append(
            {
                "ref": leaf,
                "claim": text,
                "verdict": verdict,
                "evidence": ["E1"],
                "correction": correction,
            }
        )
    return claims


def _fact_body(explain, overrides=None, model="codex/gpt-example"):
    return {
        "explain_report": 1,
        "kind": "factcheck",
        "model": model,
        "explain_sha256": "REPLACE",
        "claims": _claims(explain, overrides or {}),
        "plan_checks": [],
        "summary": "Synthetic fact-check report.",
    }


class ReportResultTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="pe-test-")
        root = Path(cls._tmp.name)
        cls.env = {
            "HOME": str(root),
            "PASEO_EXPLAIN_HOME": str(root / "pe"),
            "XDG_CONFIG_HOME": str(root / "cfg"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        cls.session = _init_plan(cls.env)
        cls.backup = root / "backup"
        shutil.copytree(cls.session, cls.backup)
        cls.explain = _explain()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def setUp(self):
        for child in self.session.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in self.backup.iterdir():
            dest = self.session / child.name
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)

    def _prepare(self, kind):
        code, out, err = _cli(self.env, "check-prepare", "--session", self.session, "--kind", kind)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def _request(self, kind):
        return json.loads((self.session / "checks" / f"{kind}-request.json").read_text(encoding="utf-8"))

    def _bind(self, kind, body):
        request = self._request(kind)
        body = copy.deepcopy(body)
        body["explain_sha256"] = request["explain_sha256"]
        write_json(self.session / f"{kind}.json", body)
        return body

    def _check(self, kind, path=None):
        args = ["check-report", "--session", self.session, "--kind", kind]
        if path is not None:
            args.extend(["--file", path])
        code, out, err = _cli(self.env, *args)
        return code, out, err

    def _install_fact(self, overrides=None, **extra):
        self._prepare("factcheck")
        body = _fact_body(self.explain, overrides)
        body.update(extra)
        self._bind("factcheck", body)
        code, out, err = self._check("factcheck")
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def _touch_html(self):
        (self.session / "explain.html").write_text("page", encoding="utf-8")

    def _errors(self, out):
        return json.loads(out)["errors"]

    def test_fixtures_follow_the_contract(self):
        fact = json.loads((helpers.FIXTURES / "factcheck.json").read_text(encoding="utf-8"))
        reader = json.loads((helpers.FIXTURES / "reader.json").read_text(encoding="utf-8"))
        self.assertEqual(fact["explain_sha256"], "REPLACE")
        self.assertEqual(fact["model"], "codex/gpt-example")
        self.assertEqual(reader["explain_sha256"], "REPLACE")
        self.assertEqual(reader["level"], 3)
        self.assertEqual(reader["model"], "cursor/reader-example")
        self.assertEqual(len(reader["answers"]), 3)
        leaves = [leaf for _unit, leaf in claim_leaves(self.explain)]
        self.assertEqual([claim["ref"] for claim in fact["claims"]], leaves)
        correction = _correction_leaf(self.explain)
        corrected = [claim for claim in fact["claims"] if claim["verdict"] == "corrected"]
        unsupported = [claim for claim in fact["claims"] if claim["verdict"] == "unsupported"]
        self.assertEqual(len(corrected), 1)
        self.assertEqual(corrected[0]["ref"], correction)
        self.assertEqual(corrected[0]["correction"], CORRECTION_SENTENCE)
        self.assertEqual(len(CORRECTION_SENTENCE.split()), 20)
        risk_titles = [f"{unit}/title" for unit in _risk_units(self.explain)]
        self.assertEqual([claim["ref"] for claim in unsupported], risk_titles)
        self.assertTrue(all(claim["verdict"] == "verified" for claim in fact["claims"] if claim not in corrected and claim not in unsupported))

    def test_prepare_requests_and_reader_markdown(self):
        printed = self._prepare("factcheck")
        request = self._request("factcheck")
        self.assertEqual(printed["units"], len(claim_units(self.explain)))
        self.assertEqual(request["units"], claim_units(self.explain))
        self.assertEqual(request["leaves"], [leaf for _unit, leaf in claim_leaves(self.explain)])
        self.assertEqual(request["kind"], "factcheck")
        self.assertRegex(request["created_at"], ISO_RE)
        hashes = current_hashes(self.session)
        self.assertEqual(request["explain_sha256"], hashes["explain_sha256"])
        self.assertEqual(request["evidence_sha256"], hashes["evidence_sha256"])
        self.assertEqual(request["brief_sha256"], hashes["brief_sha256"])
        frozen = Path(printed["input"]).read_bytes()
        self.assertEqual(frozen, (self.session / "explain.json").read_bytes())
        (self.session / "factcheck.json").write_text("{}", encoding="utf-8")
        write_json(self.session / "checks" / "applied.json", {"explain_report": 1})
        self._prepare("factcheck")
        self.assertFalse((self.session / "factcheck.json").exists())
        self.assertFalse((self.session / "checks" / "applied.json").exists())
        code, out, err = _cli(self.env, "check-prepare", "--session", self.session, "--kind", "reader")
        self.assertEqual(code, 0, out + err)
        reader_req = self._request("reader")
        brief = json.loads((self.session / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(reader_req["questions"], [item["q"] for item in brief["check_questions"]])
        self.assertNotIn("expected", json.dumps(reader_req))
        self.assertEqual(reader_req["questions_sha256"], canonical_sha256(brief["check_questions"]))
        self.assertEqual(reader_req["level"], 3)
        self.assertEqual(json.loads(out)["units"], 3)
        markdown = (self.session / "checks" / "reader.md").read_text(encoding="utf-8")
        self.assertNotIn("Answer:", markdown)
        self.assertIn("?", markdown)
        (self.session / "validate.json").unlink()
        code, out, err = _cli(self.env, "check-prepare", "--session", self.session, "--kind", "factcheck")
        self.assertEqual(code, 1, out + err)
        self.assertIn("validate.json is missing", out)

    def test_fixture_reports_pass_after_hash_substitution(self):
        self._prepare("factcheck")
        digest = self._request("factcheck")["explain_sha256"]
        raw = (helpers.FIXTURES / "factcheck.json").read_text(encoding="utf-8").replace("REPLACE", digest)
        (self.session / "factcheck.json").write_text(raw, encoding="utf-8")
        code, out, err = self._check("factcheck")
        self.assertEqual(code, 0, out + err)
        counts = json.loads(out)["counts"]
        self.assertEqual(counts["corrected"], 1)
        self.assertEqual(counts["unsupported"], 2)
        self.assertEqual(counts["leaves"], counts["covered"])
        self._prepare("reader")
        digest = self._request("reader")["explain_sha256"]
        raw = (helpers.FIXTURES / "reader.json").read_text(encoding="utf-8").replace("REPLACE", digest)
        (self.session / "reader.json").write_text(raw, encoding="utf-8")
        code, out, err = self._check("reader")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["counts"]["answers"], 3)
        reader = json.loads((self.session / "reader.json").read_text(encoding="utf-8"))
        risks = _section_index(self.explain, "risks")
        reader["hard_to_follow"] = [{"ref": f"/sections/{risks}", "why": "The risk list is dense."}]
        write_json(self.session / "reader.json", reader)
        code, out, err = self._check("reader")
        self.assertEqual(code, 0, out + err)
        reader["hard_to_follow"] = [{"ref": "/missing", "why": "Nowhere in the page."}]
        write_json(self.session / "reader-bad.json", reader)
        code, out, err = self._check("reader", self.session / "reader-bad.json")
        self.assertEqual(code, 1, out + err)
        self.assertTrue(any("does not resolve" in error["message"] for error in self._errors(out)))

    def test_report_rejects_bad_facts_and_roots(self):
        self._prepare("factcheck")
        digest = self._request("factcheck")["explain_sha256"]
        base = json.loads((helpers.FIXTURES / "factcheck.json").read_text(encoding="utf-8"))
        base["explain_sha256"] = digest
        leaves = self._request("factcheck")["leaves"]
        correction = _correction_leaf(self.explain)
        units = _risk_units(self.explain)
        decisions = _section_index(self.explain, "decisions")

        def fail(body):
            path = self.session / "bad-fact.json"
            write_json(path, body)
            code, out, err = self._check("factcheck", path)
            self.assertEqual(code, 1, out + err)
            self.assertEqual((self.session / "factcheck.json").exists(), False)
            return self._errors(out)

        wrong = copy.deepcopy(base)
        wrong["explain_sha256"] = "0" * 64
        self.assertTrue(any(error["path"] == "/explain_sha256" for error in fail(wrong)))
        unit_ref = copy.deepcopy(base)
        unit_ref["claims"][0]["ref"] = units[0]
        self.assertTrue(any("not a leaf" in error["message"] for error in fail(unit_ref)))
        parent = copy.deepcopy(base)
        parent["claims"][0]["ref"] = f"/sections/{decisions}/items/0/why"
        self.assertTrue(any("not a leaf" in error["message"] for error in fail(parent)))
        missing = copy.deepcopy(base)
        missing["claims"][0]["ref"] = "/nope"
        self.assertTrue(any("not a leaf" in error["message"] for error in fail(missing)))
        one_level = copy.deepcopy(base)
        one_level["claims"] = [claim for claim in one_level["claims"] if not claim["ref"].endswith(("/1", "/5"))]
        uncovered = fail(one_level)
        self.assertTrue(any(error["message"] == "uncovered leaf" and error["path"].endswith("/1") for error in uncovered))
        self.assertIn(leaves[0], [error["path"] for error in uncovered])
        no_correction = copy.deepcopy(base)
        for claim in no_correction["claims"]:
            if claim["ref"] == correction:
                claim["correction"] = None
        self.assertTrue(any("correction is required" in error["message"] for error in fail(no_correction)))
        duplicate = copy.deepcopy(base)
        extra = copy.deepcopy(next(claim for claim in duplicate["claims"] if claim["ref"] == correction))
        duplicate["claims"].append(extra)
        self.assertTrue(any(error["message"] == "duplicate correction" for error in fail(duplicate)))
        too_long = copy.deepcopy(base)
        for claim in too_long["claims"]:
            if claim["ref"] == correction:
                claim["correction"] = " ".join(["word"] * 81)
        self.assertTrue(any("exceeds 80 words" in error["message"] for error in fail(too_long)))
        unknown = copy.deepcopy(base)
        unknown["extra"] = True
        self.assertTrue(any(error["path"] == "/extra" and "unknown key" in error["message"] for error in fail(unknown)))
        for root in ([], "nope", True):
            path = self.session / "bad-root.json"
            path.write_text(json.dumps(root), encoding="utf-8")
            code, out, err = self._check("factcheck", path)
            self.assertEqual(code, 1, out + err)
            self.assertTrue(any(error["path"] == "" and "expected an object" in error["message"] for error in self._errors(out)))
        self._prepare("reader")
        reader = json.loads((helpers.FIXTURES / "reader.json").read_text(encoding="utf-8"))
        reader["explain_sha256"] = self._request("reader")["explain_sha256"]
        reader["level"] = True
        path = self.session / "bad-reader.json"
        write_json(path, reader)
        code, out, err = self._check("reader", path)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(any(error["path"] == "/level" and "integer" in error["message"] for error in self._errors(out)))
        path.write_text(json.dumps(["reader"]), encoding="utf-8")
        code, out, err = self._check("reader", path)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(any(error["path"] == "" for error in self._errors(out)))

    def test_apply_corrections_relabel_remove_and_guards(self):
        self._prepare("factcheck")
        digest = self._request("factcheck")["explain_sha256"]
        raw = (helpers.FIXTURES / "factcheck.json").read_text(encoding="utf-8").replace("REPLACE", digest)
        (self.session / "factcheck.json").write_text(raw, encoding="utf-8")
        self._check("factcheck")
        code, out, err = _cli(self.env, "apply-corrections", "--session", self.session, "--remove", "/facts/0")
        self.assertEqual(code, 1, out + err)
        self.assertTrue(any("no unsupported claim" in error["message"] for error in self._errors(out)))
        prose = next(index for index, section in enumerate(self.explain["sections"]) if section["type"] == "prose")
        prose_unit = f"/sections/{prose}"
        body_leaf = next(leaf for unit, leaf in claim_leaves(self.explain) if unit == prose_unit)
        self._install_fact({body_leaf: ("unsupported", None, "The prose is not supported.")})
        before = (self.session / "explain.json").read_bytes()
        code, out, err = _cli(self.env, "apply-corrections", "--session", self.session, "--remove", prose_unit)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(any("not a list item" in error["message"] for error in self._errors(out)))
        self.assertEqual((self.session / "explain.json").read_bytes(), before)
        self.setUp()
        self._prepare("factcheck")
        (self.session / "factcheck.json").write_text(
            (helpers.FIXTURES / "factcheck.json").read_text(encoding="utf-8").replace(
                "REPLACE", self._request("factcheck")["explain_sha256"]
            ),
            encoding="utf-8",
        )
        self._check("factcheck")
        code, out, err = _cli(self.env, "apply-corrections", "--session", self.session)
        self.assertEqual(code, 0, out + err)
        printed = json.loads(out)
        self.assertEqual(printed, {"ok": True, "corrected": 1, "relabelled": 2, "removed": 0})
        updated = json.loads((self.session / "explain.json").read_text(encoding="utf-8"))
        decisions = _section_index(updated, "decisions")
        self.assertEqual(updated["sections"][decisions]["items"][0]["why"]["3"], CORRECTION_SENTENCE)
        risks = _section_index(updated, "risks")
        self.assertEqual(updated["sections"][risks]["items"][0]["confidence"], "unknown")
        self.assertEqual(updated["sections"][risks]["items"][1]["confidence"], "unknown")
        state = factcheck_state(self.session)
        self.assertEqual(state["state"], "reconciled")
        self.assertEqual(state["corrections"], 3)
        code, out, err = _cli(self.env, "validate", "--session", self.session)
        self.assertEqual(code, 0, out + err)
        result = compute_result(self.session)
        self.assertEqual(result["checks"]["fact_check_corrections"], 3)
        title_refs = {f"checked:{unit}/title" for unit in _risk_units(self.explain)}
        self.assertEqual(
            {item["ref"] for item in result["findings"] if item["kind"] == "unsupported_claim"},
            title_refs,
        )
        self.setUp()
        units = _risk_units(self.explain)
        self._prepare("factcheck")
        (self.session / "factcheck.json").write_text(
            (helpers.FIXTURES / "factcheck.json").read_text(encoding="utf-8").replace(
                "REPLACE", self._request("factcheck")["explain_sha256"]
            ),
            encoding="utf-8",
        )
        self._check("factcheck")
        code, out, err = _cli(
            self.env,
            "apply-corrections",
            "--session",
            self.session,
            "--remove",
            units[0],
            "--remove",
            units[1],
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(json.loads(out)["removed"], 2)
        applied = json.loads((self.session / "checks" / "applied.json").read_text(encoding="utf-8"))
        self.assertEqual(applied["removed"], [units[1], units[0]])
        self.assertEqual(applied["corrected"], [_correction_leaf(self.explain)])
        self.assertEqual(factcheck_state(self.session)["corrections"], 3)
        _cli(self.env, "validate", "--session", self.session)
        removed_findings = compute_result(self.session)
        self.assertFalse(any(item["kind"] == "unsupported_claim" for item in removed_findings["findings"]))
        updated = json.loads((self.session / "explain.json").read_text(encoding="utf-8"))
        self.assertEqual(len(next(section for section in updated["sections"] if section["id"] == "risks")["items"]), 1)
        code, out, err = _cli(self.env, "apply-corrections", "--session", self.session)
        self.assertEqual(code, 1, out + err)
        self.assertIn("corrections already applied", out)
        updated["sections"][_section_index(updated, "decisions")]["items"][0]["why"]["3"] = "Edited inside the corrected unit now."
        write_json(self.session / "explain.json", updated)
        self.assertEqual(factcheck_state(self.session)["state"], "stale")

    def test_apply_leaves_explain_unchanged_when_invalid(self):
        decisions = _section_index(self.explain, "decisions")
        unit = f"/sections/{decisions}/items/0"
        leaf = next(item for owner, item in claim_leaves(self.explain) if owner == unit)
        self._install_fact({leaf: ("unsupported", None, "This chosen decision is not supported.")})
        before = sha256_file(self.session / "explain.json")
        code, out, err = _cli(self.env, "apply-corrections", "--session", self.session, "--remove", unit)
        self.assertEqual(code, 1, out + err)
        self.assertEqual(sha256_file(self.session / "explain.json"), before)
        self.assertFalse((self.session / "checks" / "applied.json").exists())

    def test_factcheck_state_transitions(self):
        self._install_fact()
        self.assertEqual(factcheck_state(self.session)["state"], "current")
        code, out, err = _cli(
            self.env,
            "ingest",
            "--session",
            self.session,
            "--autopilot",
            helpers.FIXTURES / "autopilot-run",
            "--doc",
            "plan",
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(factcheck_state(self.session)["state"], "current")
        _frame(self.session, self.env, questions=(("Is there a fee?", "Yes"),) + QUESTIONS[1:])
        self.assertEqual(factcheck_state(self.session)["state"], "stale")
        self.setUp()
        changed = Path(self._tmp.name) / "changed-run"
        shutil.copytree(helpers.FIXTURES / "autopilot-run", changed)
        spec = changed / "01-spec.md"
        spec.write_text(spec.read_text(encoding="utf-8") + "\nA further public note.\n", encoding="utf-8")
        self._install_fact()
        code, out, err = _cli(self.env, "ingest", "--session", self.session, "--autopilot", changed, "--doc", "plan")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(factcheck_state(self.session)["state"], "stale")
        self.setUp()
        self._install_fact()
        code, out, err = _cli(
            self.env,
            "init",
            "--autopilot",
            helpers.FIXTURES / "autopilot-run",
            "--doc",
            "plan",
            "--mode",
            "quick",
        )
        self.assertEqual(code, 0, out + err)
        self.assertFalse((self.session / "factcheck.json").exists())
        self.assertFalse((self.session / "checks").exists())
        self.assertEqual(factcheck_state(self.session)["state"], "none")
        code, out, err = _cli(
            self.env,
            "init",
            "--autopilot",
            helpers.FIXTURES / "autopilot-run",
            "--doc",
            "plan",
            "--mode",
            "standard",
        )
        self.assertEqual(code, 0, out + err)
        self.assertEqual(factcheck_state(self.session)["state"], "none")

    def test_reader_state_grade_and_skip(self):
        self._prepare("reader")
        digest = self._request("reader")["explain_sha256"]
        raw = (helpers.FIXTURES / "reader.json").read_text(encoding="utf-8").replace("REPLACE", digest)
        (self.session / "reader.json").write_text(raw, encoding="utf-8")
        code, out, err = self._check("reader")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(reader_state(self.session)["state"], "stale")
        code, out, err = _cli(self.env, "grade", "--session", self.session, "--correct", "3")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(reader_state(self.session)["state"], "counted")
        grade = json.loads((self.session / "session.json").read_text(encoding="utf-8"))["reader_grade"]
        self.assertEqual(grade["correct"], 3)
        self.assertEqual(grade["total"], 3)
        self.assertEqual(grade["report_sha256"], sha256_file(self.session / "reader.json"))
        code, out, err = _cli(self.env, "grade", "--session", self.session, "--correct", "4")
        self.assertEqual(code, 1, out + err)
        self.assertIn("out of range", out)
        explain = json.loads((self.session / "explain.json").read_text(encoding="utf-8"))
        explain["title"] = "Garden plot booking revised"
        write_json(self.session / "explain.json", explain)
        self.assertEqual(reader_state(self.session)["state"], "counted")
        body = json.loads((self.session / "reader.json").read_text(encoding="utf-8"))
        body["summary"] = "A second reading without a new grade."
        other = self.session / "reader-next.json"
        write_json(other, body)
        code, out, err = self._check("reader", other)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(reader_state(self.session)["state"], "stale")
        code, out, err = _cli(self.env, "grade", "--session", self.session, "--correct", "3")
        self.assertEqual(code, 0, out + err)
        _frame(
            self.session,
            self.env,
            questions=(
                ("Is a fee charged?", "No"),
                ("Where is the calendar?", "At the hall"),
                ("How many gardens?", "One"),
            ),
        )
        self.assertEqual(reader_state(self.session)["state"], "stale")
        code, out, err = _cli(self.env, "validate", "--session", self.session)
        self.assertEqual(code, 0, out + err)
        self._prepare("reader")
        session = json.loads((self.session / "session.json").read_text(encoding="utf-8"))
        self.assertIsNone(session["reader_grade"])
        self.assertFalse((self.session / "reader.json").exists())
        self.assertEqual(reader_state(self.session)["state"], "none")
        code, out, err = _cli(self.env, "skip", "--session", self.session, "--step", "fact_check", "--reason", "no second model")
        self.assertEqual(code, 0, out + err)
        code, out, err = _cli(self.env, "skip", "--session", self.session, "--step", "reader_test", "--reason", "x" * 200)
        self.assertEqual(code, 0, out + err)
        code, out, err = _cli(self.env, "skip", "--session", self.session, "--step", "reader_test", "--reason", "x" * 201)
        self.assertEqual(code, 1, out + err)
        self._prepare("factcheck")
        self._bind("factcheck", _fact_body(self.explain))
        code, out, err = self._check("factcheck")
        self.assertEqual(code, 0, out + err)
        skipped = json.loads((self.session / "session.json").read_text(encoding="utf-8"))["skipped"]
        self.assertIsNone(skipped["fact_check"])
        self.assertEqual(skipped["reader_test"], "x" * 200)
        (self.session / "reader.json").unlink(missing_ok=True)
        code, out, err = _cli(self.env, "grade", "--session", self.session, "--correct", "1")
        self.assertEqual(code, 1, out + err)
        self.assertIn("reader report is missing", out)

    def test_result_labels_findings_and_export(self):
        self.assertEqual(model_family("codex/gpt-6-astra"), "gpt")
        self.assertEqual(model_family("cursor/grok-4.7"), "grok")
        self.assertEqual(model_family("claude/claude-opus-5-5"), "claude")
        self.assertEqual(model_family("mistral-vibe/glm-5-3"), "glm")
        self.assertEqual(model_family("openai/gpt-5:<thinking>"), "gpt")
        quick = helpers.make_session(self.env, "idea", slug="quick-idea", mode="quick", levels=3, lang="en")
        code, out, err = _cli(self.env, "ingest", "--session", quick, "--text-file", helpers.FIXTURES / "idea.md")
        self.assertEqual(code, 0, out + err)
        _frame(quick, self.env, questions=QUESTIONS[:1])
        shutil.copy(helpers.FIXTURES / "idea-explain.json", quick / "explain.json")
        code, out, err = _cli(self.env, "validate", "--session", quick)
        self.assertEqual(code, 0, out + err)
        (quick / "explain.html").write_text("page", encoding="utf-8")
        quick_result = compute_result(quick)
        self.assertEqual(quick_result["status"], "ok")
        self.assertEqual(quick_result["checks"]["check_label"], "not_checked")
        self.assertEqual(quick_result["checks"]["fact_check"], "skipped")
        self.assertTrue(any(item["kind"] == "flagged_content" for item in quick_result["findings"]))
        evidence = json.loads((quick / "evidence.json").read_text(encoding="utf-8"))
        flagged = [frag["id"] for frag in evidence["fragments"] if frag.get("flagged") is True]
        self.assertTrue(flagged)
        self.assertTrue(any(item["kind"] == "flagged_content" and item["ref"] in flagged for item in quick_result["findings"]))
        self._touch_html()
        code, out, err = _cli(self.env, "skip", "--session", self.session, "--step", "fact_check", "--reason", "no second model")
        self.assertEqual(code, 0, out + err)
        skipped = compute_result(self.session)
        self.assertEqual(skipped["checks"]["skipped"]["fact_check"], "no second model")
        self.assertEqual(skipped["checks"]["fact_check"], "skipped")
        self.assertEqual(skipped["status"], "partial")
        coverage = next(section for section in self.explain["sections"] if section["id"] == "coverage")
        empty_ids = [item["id"] for item in coverage["requirements"] if item["tasks"] == []]
        untested = [item["id"] for item in coverage["requirements"] if item["test"] == "no"]
        linked = {task_id for item in coverage["requirements"] for task_id in item["tasks"]}
        unlinked = [item["id"] for item in coverage["tasks"] if item["id"] not in linked]
        kinds = skipped["findings"]
        for ref in empty_ids:
            self.assertTrue(any(item["kind"] == "requirement_without_task" and item["ref"] == ref for item in kinds))
        for ref in untested:
            self.assertTrue(any(item["kind"] == "requirement_without_test" and item["ref"] == ref for item in kinds))
        for ref in unlinked:
            self.assertTrue(any(item["kind"] == "task_without_requirement" and item["ref"] == ref for item in kinds))
        risks = _section_index(self.explain, "risks")
        open_item = next(
            index for index, item in enumerate(self.explain["sections"][risks]["items"]) if item["kind"] == "open_question"
        )
        self.assertTrue(
            any(
                item["kind"] == "open_question"
                and item["ref"] == f"/sections/{risks}/items/{open_item}"
                and item["text"] == self.explain["sections"][risks]["items"][open_item]["title"]
                for item in kinds
            )
        )
        evidence = json.loads((self.session / "evidence.json").read_text(encoding="utf-8"))
        evidence["structured"]["requirements"].append({"id": "R9", "title": "Extra requirement", "evidence": "E1"})
        write_json(self.session / "evidence.json", evidence)
        with_refs = compute_result(self.session)
        self.assertTrue(any(item["kind"] == "requirement_without_task" and item["ref"] == "R9" for item in with_refs["findings"]))
        evidence["structured"]["refs_present"] = False
        write_json(self.session / "evidence.json", evidence)
        without_refs = compute_result(self.session)
        self.assertFalse(any(item["ref"] == "R9" for item in without_refs["findings"]))
        self.assertTrue(any(item["kind"] == "requirement_without_task" and item["ref"] == empty_ids[0] for item in without_refs["findings"]))
        self.setUp()
        brief = json.loads((self.session / "brief.json").read_text(encoding="utf-8"))
        brief.pop("orchestrator_model", None)
        write_json(self.session / "brief.json", brief)
        _cli(self.env, "validate", "--session", self.session)
        self._install_fact()
        unknown = compute_result(self.session)
        self.assertEqual(unknown["checks"]["check_label"], "independence_unknown")
        self.assertIs(unknown["checks"]["independent"], False)
        self.setUp()
        _frame(self.session, self.env, orchestrator="acme/gpt-demo")
        _cli(self.env, "validate", "--session", self.session)
        self._install_fact()
        same = compute_result(self.session)
        self.assertEqual(same["checks"]["check_label"], "same_family")
        self.setUp()
        self._install_fact()
        explain = json.loads((self.session / "explain.json").read_text(encoding="utf-8"))
        explain["facts"][0]["value"] = "Paper hall"
        write_json(self.session / "explain.json", explain)
        code, out, err = _cli(self.env, "validate", "--session", self.session)
        self.assertEqual(code, 0, out + err)
        self._touch_html()
        stale = compute_result(self.session)
        self.assertEqual(stale["checks"]["fact_check"], "stale")
        self.assertEqual(stale["checks"]["check_label"], "not_checked")
        self.assertEqual(stale["status"], "partial")
        self.setUp()
        leaf = _correction_leaf(self.explain)
        unit = _risk_units(self.explain)[0]
        title = f"{unit}/title"
        self._install_fact(
            {
                leaf: ("corrected", CORRECTION_SENTENCE, "The written reason at this level is replaced."),
                title: ("unsupported", None, "This risk item is not supported by the evidence."),
            }
        )
        code, out, err = _cli(self.env, "apply-corrections", "--session", self.session, "--remove", unit)
        self.assertEqual(code, 0, out + err)
        _cli(self.env, "validate", "--session", self.session)
        self._touch_html()
        reconciled = compute_result(self.session)
        self.assertEqual(reconciled["checks"]["fact_check"], "pass")
        self.assertEqual(reconciled["checks"]["fact_check_corrections"], 2)
        self.assertEqual(reconciled["checks"]["check_label"], "independent_corrected")
        self.assertIs(reconciled["checks"]["independent"], True)
        self.assertEqual(reconciled["status"], "ok")
        self.assertEqual(set(reconciled), set(RESULT_KEYS))
        code, out, err = _cli(
            self.env,
            "init",
            "--autopilot",
            helpers.FIXTURES / "autopilot-run",
            "--doc",
            "plan",
            "--mode",
            "deep",
        )
        self.assertEqual(code, 0, out + err)
        shutil.copy(self.backup / "explain.json", self.session / "explain.json")
        shutil.copy(self.backup / "evidence.json", self.session / "evidence.json")
        shutil.copy(self.backup / "brief.json", self.session / "brief.json")
        code, out, err = _cli(self.env, "validate", "--session", self.session)
        self.assertEqual(code, 0, out + err)
        self._install_fact()
        self._prepare("reader")
        digest = self._request("reader")["explain_sha256"]
        (self.session / "reader.json").write_text(
            (helpers.FIXTURES / "reader.json").read_text(encoding="utf-8").replace("REPLACE", digest),
            encoding="utf-8",
        )
        code, out, err = self._check("reader")
        self.assertEqual(code, 0, out + err)
        code, out, err = _cli(self.env, "grade", "--session", self.session, "--correct", "3")
        self.assertEqual(code, 0, out + err)
        self._touch_html()
        deep = compute_result(self.session)
        self.assertEqual(deep["mode"], "deep")
        self.assertEqual(deep["checks"]["reader_test"], "pass")
        self.assertEqual(deep["checks"]["fact_check"], "pass")
        self.assertEqual(deep["status"], "ok")
        code, out, err = _cli(self.env, "grade", "--session", self.session, "--correct", "2")
        self.assertEqual(code, 0, out + err)
        partial = compute_result(self.session)
        self.assertEqual(partial["checks"]["reader_test"], "partial")
        self.assertEqual(partial["status"], "partial")
        export = Path(self._tmp.name) / "export-copy"
        code, out, err = _cli(
            self.env,
            "init",
            "--autopilot",
            helpers.FIXTURES / "autopilot-run",
            "--doc",
            "plan",
            "--out",
            export,
            "--mode",
            "standard",
        )
        self.assertEqual(code, 0, out + err)
        shutil.copy(self.backup / "explain.json", self.session / "explain.json")
        code, out, err = _cli(self.env, "validate", "--session", self.session)
        self.assertEqual(code, 0, out + err)
        code, out, err = _cli(self.env, "render", "--session", self.session)
        self.assertEqual(code, 0, out + err)
        for name in ("explain.html", "explain.md", "result.json"):
            self.assertEqual((self.session / name).read_bytes(), (export / name).read_bytes())
        code, out, err = _cli(self.env, "result", "--session", self.session)
        self.assertEqual(code, 0, out + err)
        stored = json.loads((self.session / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(out), stored)
        self.assertEqual((export / "result.json").read_bytes(), (self.session / "result.json").read_bytes())
        self.assertRegex(stored["generated_at"], ISO_RE)
        code, out, err = _cli(self.env, "tab", "--session", self.session, "--opened", "true")
        self.assertEqual(code, 0, out + err)
        self.assertEqual((export / "result.json").read_bytes(), (self.session / "result.json").read_bytes())
        self.assertIs(json.loads(out)["tab_opened"], True)
        again = compute_result(self.session)
        self.assertEqual({key: value for key, value in again.items() if key != "generated_at"}, {key: value for key, value in json.loads((self.session / "result.json").read_text(encoding="utf-8")).items() if key != "generated_at"})

    def test_result_schema_matches_keys(self):
        schema = json.loads(
            (helpers.REPO / "paseo-explain" / "references" / "result.schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("2020-12", schema["$schema"])
        self.assertEqual(set(schema["properties"]), set(RESULT_KEYS))
        self.assertIs(schema["additionalProperties"], False)

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object" or "properties" in node:
                    self.assertIs(node["additionalProperties"], False)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(schema)


class ReaderWithoutQuestionsTests(unittest.TestCase):
    """The reader test works in standard mode with no check questions."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="pe-test-")
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.env = {
            "HOME": str(root),
            "PASEO_EXPLAIN_HOME": str(root / "pe"),
            "XDG_CONFIG_HOME": str(root / "cfg"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def _session(self, mode, questions=()):
        session = helpers.make_session(
            self.env,
            "plan",
            autopilot=str(helpers.FIXTURES / "autopilot-run"),
            doc="plan",
            levels=3,
            lang="en",
            mode=mode,
        )
        code, out, err = _cli(
            self.env, "ingest", "--session", session, "--autopilot", helpers.FIXTURES / "autopilot-run", "--doc", "plan"
        )
        self.assertEqual(code, 0, out + err)
        _frame(session, self.env, questions=questions)
        shutil.copy(helpers.FIXTURES / "plan-explain.json", session / "explain.json")
        code, out, err = _cli(self.env, "validate", "--session", session)
        self.assertEqual(code, 0, out + err)
        return session

    def _prepare(self, session, expect=0):
        code, out, err = _cli(self.env, "check-prepare", "--session", session, "--kind", "reader")
        self.assertEqual(code, expect, out + err)
        return out

    def _reader_report(self, session, answers=(), terms=(), hard=()):
        request = json.loads((session / "checks" / "reader-request.json").read_text(encoding="utf-8"))
        body = {
            "explain_report": 1,
            "kind": "reader",
            "model": "cursor/reader-example",
            "explain_sha256": request["explain_sha256"],
            "level": request["level"],
            "answers": list(answers),
            "undefined_terms": list(terms),
            "hard_to_follow": list(hard),
            "summary": "Synthetic reader report.",
        }
        write_json(session / "reader.json", body)
        code, out, err = _cli(self.env, "check-report", "--session", session, "--kind", "reader")
        self.assertEqual(code, 0, out + err)

    def _result(self, session):
        code, out, err = _cli(self.env, "result", "--session", session)
        self.assertEqual(code, 0, out + err)
        return json.loads(out)

    def _grade(self, session, correct, expect=0):
        code, out, err = _cli(self.env, "grade", "--session", session, "--correct", str(correct))
        self.assertEqual(code, expect, out + err)
        return out

    def _render(self, session):
        code, out, err = _cli(self.env, "render", "--session", session)
        self.assertEqual(code, 0, out + err)

    def _standard_flow(self, terms=()):
        session = self._session("standard")
        self._render(session)
        self.assertEqual(self._result(session)["status"], "partial")
        out = self._prepare(session)
        self.assertEqual(json.loads(out)["units"], 0)
        request = json.loads((session / "checks" / "reader-request.json").read_text(encoding="utf-8"))
        self.assertEqual(request["questions"], [])
        self.assertEqual(request["questions_sha256"], canonical_sha256([]))
        self._reader_report(session, terms=terms)
        self._grade(session, 0)
        stored = json.loads((session / "session.json").read_text(encoding="utf-8"))["reader_grade"]
        self.assertEqual(stored, {"correct": 0, "total": 0, "report_sha256": sha256_file(session / "reader.json")})
        self._render(session)
        return session

    def test_standard_reader_without_questions(self):
        session = self._standard_flow()
        result = self._result(session)
        self.assertEqual(result["checks"]["reader_test"], "pass")
        self.assertEqual(result["status"], "partial")

    def test_standard_reader_terms_make_partial(self):
        session = self._standard_flow(terms=["ZQX"])
        self.assertEqual(self._result(session)["checks"]["reader_test"], "partial")

    def test_grade_zero_questions_range(self):
        session = self._session("standard")
        self._prepare(session)
        self._reader_report(session)
        out = self._grade(session, 1, expect=1)
        self.assertIn("correct is out of range", out)

    def test_deep_reader_still_requires_questions(self):
        session = self._session("deep")
        out = self._prepare(session, expect=1)
        self.assertIn("reader check requires 1 to 5 check questions", out)

    def _stale_cycle(self, mode, questions, correct):
        session = self._session(mode, questions)
        self._prepare(session)
        answers = [{"q": index, "answer": "An answer."} for index in range(len(questions))]
        self._reader_report(session, answers=answers)
        self._grade(session, correct)
        self.assertEqual(reader_state(session)["state"], "counted")
        body = json.loads((session / "reader.json").read_text(encoding="utf-8"))
        body["summary"] = "A second reading."
        write_json(session / "reader.json", body)
        self.assertEqual(reader_state(session)["state"], "stale")
        self._grade(session, correct)
        self.assertEqual(reader_state(session)["state"], "counted")
        _frame(session, self.env, questions=tuple(questions) + (("Is there a deposit?", "No"),))
        self.assertEqual(reader_state(session)["state"], "stale")
        code, out, err = _cli(self.env, "validate", "--session", session)
        self.assertEqual(code, 0, out + err)
        self._prepare(session)
        answers = [{"q": index, "answer": "An answer."} for index in range(len(questions) + 1)]
        self._reader_report(session, answers=answers)
        self._grade(session, correct)
        self.assertEqual(reader_state(session)["state"], "counted")

    def test_reader_state_stale_after_edits(self):
        self._stale_cycle("standard", (), 0)
        self._stale_cycle("deep", QUESTIONS[:2], 2)

    def test_quick_mode_unchanged(self):
        session = self._session("quick")
        self.assertEqual(reader_state(session)["state"], "none")


if __name__ == "__main__":
    unittest.main()


class PromptReportFormatTests(unittest.TestCase):
    """The report formats pasted into agent prompts match the validators."""

    def _blocks(self):
        import re
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "paseo-explain" / "references" / "prompts.md").read_text(encoding="utf-8")
        section = text.split("## Report formats", 1)[1]
        found = re.findall(r"### (\w+)\n\n```json\n(.*?)\n```", section, re.S)
        return {name: json.loads(body) for name, body in found}

    def test_every_prompt_has_a_report_format_placeholder(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parents[1] / "paseo-explain" / "references" / "prompts.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("{REPORT_FORMAT}\n```"), 3)

    def test_format_keys_match_validators(self):
        from explainlib import survey

        blocks = self._blocks()
        self.assertEqual(set(blocks), {"factcheck", "reader", "survey"})
        fc = blocks["factcheck"]
        self.assertEqual(set(fc), {"explain_report", "kind", "model", "explain_sha256", "claims", "summary", "plan_checks"})
        self.assertEqual(set(fc["claims"][0]), {"ref", "claim", "verdict", "evidence", "correction"})
        self.assertEqual(set(fc["plan_checks"][0]), {"kind", "ref", "text"})
        rd = blocks["reader"]
        self.assertEqual(set(rd), {"explain_report", "kind", "model", "explain_sha256", "level", "answers", "undefined_terms", "hard_to_follow", "summary"})
        self.assertEqual(set(rd["answers"][0]), {"q", "answer"})
        self.assertEqual(set(rd["hard_to_follow"][0]), {"ref", "why"})
        sv = blocks["survey"]
        self.assertEqual(set(sv), set(survey._REQUIRED) | set(survey._OPTIONAL))
        self.assertEqual(set(sv["components"][0]), set(survey._COMPONENT_KEYS))
        self.assertEqual(set(sv["flows"][0]), set(survey._FLOW_KEYS))
        self.assertEqual(set(sv["entrypoints"][0]), set(survey._ENTRYPOINT_KEYS))
        self.assertEqual(set(sv["ranges"][0]), set(survey._RANGE_KEYS))
