"""Tests for explain.json validation, briefs, pointers, and fixtures."""

import copy
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

sys.path.insert(0, str(helpers.REPO / "paseo-explain" / "scripts"))

from explainlib import common
from explainlib.common import ExplainError
from explainlib.validate import (
    EXPLAIN_TOP_KEYS,
    claim_leaves,
    claim_units,
    resolve_pointer,
    run_validate,
    validate_brief,
    validate_explain,
    validate_mermaid,
)

NO_EXAMPLE = "no example section; add one concrete case"
CODE_LABEL = "label starts with an internal code; lead with plain words and put the code in parentheses"
LEVEL1_PREFIX = "level 1 uses terms the glossary does not explain: "
SCHEMA_MD = helpers.REPO / "paseo-explain" / "references" / "schema.md"

PLAN_UNITS = [
    "/lead",
    "/facts/0",
    "/facts/1",
    "/hero/nodes/0",
    "/hero/nodes/1",
    "/hero/nodes/2",
    "/hero/steps/0",
    "/sections/0",
    "/sections/1/now/0",
    "/sections/1/next/0",
    "/sections/1/unchanged/0",
    "/sections/2",
    "/sections/3/requirements/0",
    "/sections/3/requirements/1",
    "/sections/3/requirements/2",
    "/sections/3/requirements/3",
    "/sections/4/items/0",
    "/sections/4/items/1",
    "/sections/5/items/0",
    "/sections/5/items/1",
    "/sections/5/items/2",
]

PLAN_LEAVES = [
    ("/lead", "/lead/text/1"),
    ("/lead", "/lead/text/3"),
    ("/lead", "/lead/text/5"),
    ("/facts/0", "/facts/0/value"),
    ("/facts/1", "/facts/1/value"),
    ("/hero/nodes/0", "/hero/nodes/0/detail/1"),
    ("/hero/nodes/0", "/hero/nodes/0/detail/3"),
    ("/hero/nodes/0", "/hero/nodes/0/detail/5"),
    ("/hero/nodes/1", "/hero/nodes/1/detail/1"),
    ("/hero/nodes/1", "/hero/nodes/1/detail/3"),
    ("/hero/nodes/1", "/hero/nodes/1/detail/5"),
    ("/hero/nodes/2", "/hero/nodes/2/detail/1"),
    ("/hero/nodes/2", "/hero/nodes/2/detail/3"),
    ("/hero/nodes/2", "/hero/nodes/2/detail/5"),
    ("/hero/steps/0", "/hero/steps/0/text/1"),
    ("/hero/steps/0", "/hero/steps/0/text/3"),
    ("/hero/steps/0", "/hero/steps/0/text/5"),
    ("/sections/0", "/sections/0/body/1"),
    ("/sections/0", "/sections/0/body/3"),
    ("/sections/0", "/sections/0/body/5"),
    ("/sections/1/now/0", "/sections/1/now/0/text/1"),
    ("/sections/1/now/0", "/sections/1/now/0/text/3"),
    ("/sections/1/now/0", "/sections/1/now/0/text/5"),
    ("/sections/1/next/0", "/sections/1/next/0/text/1"),
    ("/sections/1/next/0", "/sections/1/next/0/text/3"),
    ("/sections/1/next/0", "/sections/1/next/0/text/5"),
    ("/sections/1/unchanged/0", "/sections/1/unchanged/0/text/1"),
    ("/sections/1/unchanged/0", "/sections/1/unchanged/0/text/3"),
    ("/sections/1/unchanged/0", "/sections/1/unchanged/0/text/5"),
    ("/sections/2", "/sections/2/mermaid"),
    ("/sections/2", "/sections/2/alt"),
    ("/sections/3/requirements/0", "/sections/3/requirements/0/text"),
    ("/sections/3/requirements/1", "/sections/3/requirements/1/text"),
    ("/sections/3/requirements/2", "/sections/3/requirements/2/text"),
    ("/sections/3/requirements/3", "/sections/3/requirements/3/text"),
    ("/sections/4/items/0", "/sections/4/items/0/title"),
    ("/sections/4/items/0", "/sections/4/items/0/why/1"),
    ("/sections/4/items/0", "/sections/4/items/0/why/3"),
    ("/sections/4/items/0", "/sections/4/items/0/why/5"),
    ("/sections/4/items/1", "/sections/4/items/1/title"),
    ("/sections/4/items/1", "/sections/4/items/1/why/1"),
    ("/sections/4/items/1", "/sections/4/items/1/why/3"),
    ("/sections/4/items/1", "/sections/4/items/1/why/5"),
    ("/sections/5/items/0", "/sections/5/items/0/title"),
    ("/sections/5/items/0", "/sections/5/items/0/text/1"),
    ("/sections/5/items/0", "/sections/5/items/0/text/3"),
    ("/sections/5/items/0", "/sections/5/items/0/text/5"),
    ("/sections/5/items/1", "/sections/5/items/1/title"),
    ("/sections/5/items/1", "/sections/5/items/1/text/1"),
    ("/sections/5/items/1", "/sections/5/items/1/text/3"),
    ("/sections/5/items/1", "/sections/5/items/1/text/5"),
    ("/sections/5/items/2", "/sections/5/items/2/title"),
    ("/sections/5/items/2", "/sections/5/items/2/text/1"),
    ("/sections/5/items/2", "/sections/5/items/2/text/3"),
    ("/sections/5/items/2", "/sections/5/items/2/text/5"),
]

CORRECTION_SENTENCE = (
    "Gardeners keep one written reason so the chosen rule stays easy to explain "
    "at this level without adding extra claims."
)
CHECKED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
FORBIDDEN_MERMAID = ("<", "click ", "%%{", "javascript:", "href", "callback", "call ")


def _frame(session, env):
    code, out, err = helpers.run_cli(
        "frame",
        "--session",
        session,
        "--audience",
        "Neighbours who use the garden or the hall",
        "--question",
        "What does this change for them?",
        "--out-of-scope",
        "Payments",
        "--check-question",
        "Is there a fee?::No",
        "--orchestrator-model",
        "example/model",
        env=env,
    )
    if code != 0:
        raise AssertionError(f"frame failed: {code} {out} {err}")
    return json.loads(out)


def _init_plan(env):
    session = helpers.make_session(
        env,
        "plan",
        autopilot=str(helpers.FIXTURES / "autopilot-run"),
        doc="plan",
        levels=3,
        lang="en",
    )
    code, out, err = helpers.run_cli(
        "ingest",
        "--session",
        session,
        "--autopilot",
        helpers.FIXTURES / "autopilot-run",
        "--doc",
        "plan",
        env=env,
    )
    if code != 0:
        raise AssertionError(f"ingest failed: {code} {out} {err}")
    _frame(session, env)
    shutil.copy(helpers.FIXTURES / "plan-explain.json", session / "explain.json")
    return session


def _init_idea(env):
    session = helpers.make_session(env, "idea", slug="idea-shelf", levels=3, lang="en")
    code, out, err = helpers.run_cli(
        "ingest",
        "--session",
        session,
        "--text-file",
        helpers.FIXTURES / "idea.md",
        env=env,
    )
    if code != 0:
        raise AssertionError(f"ingest failed: {code} {out} {err}")
    _frame(session, env)
    shutil.copy(helpers.FIXTURES / "idea-explain.json", session / "explain.json")
    return session


def _leveled_objects(obj, path=""):
    found = []
    if isinstance(obj, dict) and set(obj) >= {"1", "3", "5"} and all(isinstance(obj[k], str) for k in ("1", "3", "5")):
        found.append((path, obj))
        return found
    if isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(_leveled_objects(value, f"{path}/{key}"))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            found.extend(_leveled_objects(value, f"{path}/{index}"))
    return found


class ValidateTests(unittest.TestCase):
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
        cls.plan_session = _init_plan(cls.env)
        cls.idea_session = _init_idea(cls.env)
        cls.plan = json.loads((helpers.FIXTURES / "plan-explain.json").read_text(encoding="utf-8"))
        cls.idea = json.loads((helpers.FIXTURES / "idea-explain.json").read_text(encoding="utf-8"))
        cls.plan_evidence = json.loads((cls.plan_session / "evidence.json").read_text(encoding="utf-8"))
        cls.idea_evidence = json.loads((cls.idea_session / "evidence.json").read_text(encoding="utf-8"))
        cls.plan_meta = json.loads((cls.plan_session / "session.json").read_text(encoding="utf-8"))
        cls.idea_meta = json.loads((cls.idea_session / "session.json").read_text(encoding="utf-8"))
        cls.brief = json.loads((cls.plan_session / "brief.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _errors(self, explain, evidence=None, session=None):
        return validate_explain(
            explain,
            self.plan_evidence if evidence is None else evidence,
            self.plan_meta if session is None else session,
            self.brief,
        )

    def _assert_error(self, errors, path, message):
        self.assertTrue(
            any(error["path"] == path and message in error["message"] for error in errors),
            errors,
        )

    def test_fixtures_pass_and_validate_json_stores_hashes(self):
        for session, fixture_name in (
            (self.plan_session, "plan-explain.json"),
            (self.idea_session, "idea-explain.json"),
        ):
            shutil.copy(helpers.FIXTURES / fixture_name, session / "explain.json")
            code, out, err = helpers.run_cli("validate", "--session", session, env=self.env)
            self.assertEqual(code, 0, out + err)
            printed = json.loads(out)
            stored = json.loads((session / "validate.json").read_text(encoding="utf-8"))
            self.assertEqual(printed, stored)
            self.assertTrue(stored["ok"])
            self.assertEqual(stored["errors"], [])
            hashes = common.current_hashes(session)
            self.assertEqual(stored["explain_sha256"], hashes["explain_sha256"])
            self.assertEqual(stored["evidence_sha256"], hashes["evidence_sha256"])
            self.assertEqual(stored["brief_sha256"], hashes["brief_sha256"])
            self.assertIsNotNone(stored["explain_sha256"])
            self.assertRegex(stored["checked_at"], CHECKED_AT_RE)
        plan_types = {section["type"] for section in self.plan["sections"]}
        idea_types = {section["type"] for section in self.idea["sections"]}
        self.assertEqual(
            plan_types,
            {"prose", "change", "diagram", "coverage", "decisions", "risks", "quiz"},
        )
        self.assertEqual(idea_types, {"prose", "diagram", "decisions", "risks", "quiz"})
        self.assertEqual(self.plan["lang"], "en")
        self.assertEqual(self.idea["lang"], "en")
        self.assertEqual(self.plan["levels"], [1, 3, 5])
        self.assertEqual(self.idea["levels"], [1, 3, 5])
        coverage = next(section for section in self.plan["sections"] if section["type"] == "coverage")
        self.assertTrue(any(item["tasks"] == [] for item in coverage["requirements"]))
        self.assertTrue(any(item["test"] == "no" for item in coverage["requirements"]))
        self.assertEqual(
            {node["tone"] for node in self.plan["hero"]["nodes"]},
            {"existing", "new", "external"},
        )
        self.assertIn("quote", json.dumps(self.plan))
        for document in (self.plan, self.idea):
            leveled = _leveled_objects(document)
            self.assertGreater(len(leveled), 0)
            for path, value in leveled:
                self.assertEqual(len({value["1"], value["3"], value["5"]}), 3, path)

    def test_frame_merges_and_rejects_missing_audience(self):
        code, out, err = helpers.run_cli(
            "frame",
            "--session",
            self.idea_session,
            "--out-of-scope",
            "Maps",
            "--out-of-scope",
            "Accounts",
            env=self.env,
        )
        self.assertEqual(code, 0, out + err)
        brief = json.loads(out)
        self.assertEqual(brief["out_of_scope"], ["Maps", "Accounts"])
        self.assertEqual(brief["audience"], "Neighbours who use the garden or the hall")
        self.assertEqual(brief["question"], "What does this change for them?")
        stored = json.loads((self.idea_session / "brief.json").read_text(encoding="utf-8"))
        self.assertEqual(stored, brief)
        bare = helpers.make_session(self.env, "idea", slug="bare-idea", levels=3, lang="en")
        code, out, err = helpers.run_cli("frame", "--session", bare, env=self.env)
        self.assertEqual(code, 1, out + err)
        self.assertTrue(any(error["path"] == "/audience" for error in json.loads(out)["errors"]))
        self.assertFalse((bare / "brief.json").is_file())

    def test_unknown_key(self):
        explain = copy.deepcopy(self.plan)
        explain["extra"] = True
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/extra", "unknown key extra")

    def test_word_cap(self):
        explain = copy.deepcopy(self.plan)
        explain["lead"]["text"]["3"] = " ".join(["word"] * 91)
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/lead/text/3", "exceeds 90 words")

    def test_leveled_text_keys(self):
        explain = copy.deepcopy(self.plan)
        del explain["lead"]["text"]["5"]
        explain["lead"]["text"]["2"] = "This level was not written for the session."
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/lead/text", "written levels")

    def test_bad_evidence_id(self):
        explain = copy.deepcopy(self.plan)
        explain["lead"]["evidence"] = ["E999"]
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/lead/evidence/0", "unknown evidence id E999")

    def test_non_verbatim_quote(self):
        explain = copy.deepcopy(self.plan)
        explain["sections"][1]["now"][0]["quote"]["text"] = "This sentence is not in the fragment."
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/sections/1/now/0/quote/text", "not a verbatim")

    def test_confirmed_without_evidence(self):
        explain = copy.deepcopy(self.plan)
        explain["lead"]["confidence"] = "confirmed"
        explain["lead"]["evidence"] = []
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/lead/evidence", "confirmed requires evidence")

    def test_hero_reference_and_bounds(self):
        explain = copy.deepcopy(self.plan)
        explain["hero"]["edges"][0]["to"] = "missing"
        explain["hero"]["zones"][0]["w"] = 2000
        explain["hero"]["nodes"][0]["x"] = 1500
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/hero/edges/0/to", "unknown node")
        self._assert_error(errors, "/hero/zones/0", "outside the canvas")
        self._assert_error(errors, "/hero/nodes/0/x", "expected an integer")

    def test_section_type_not_allowed_for_kind(self):
        explain = copy.deepcopy(self.idea)
        explain["sections"][0]["type"] = "change"
        errors, _warnings = self._errors(explain, evidence=self.idea_evidence, session=self.idea_meta)
        self._assert_error(errors, "/sections/0/type", "not allowed")

    def test_forbidden_mermaid_tokens(self):
        base = "flowchart LR\n  plots[Plot records] --> overlap[Overlap rule]\n"
        self.assertEqual(validate_mermaid(base), [])
        for token in FORBIDDEN_MERMAID:
            with self.subTest(token=token):
                explain = copy.deepcopy(self.plan)
                explain["sections"][2]["mermaid"] = base + token + "\n"
                errors, _warnings = self._errors(explain)
                self.assertTrue(
                    any(
                        error["path"] == "/sections/2/mermaid" and token in error["message"]
                        for error in errors
                    ),
                    errors,
                )

    def test_mermaid_node_count_cap(self):
        lines = ["flowchart LR"] + [f"n{index}[N]" for index in range(26)]
        explain = copy.deepcopy(self.plan)
        explain["sections"][2]["mermaid"] = "\n".join(lines) + "\n"
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/sections/2/mermaid", "more than 25 nodes")

    def test_size_cap(self):
        explain = copy.deepcopy(self.plan)
        explain["lead"]["text"] = "a" * 200001
        errors, _warnings = self._errors(explain)
        self.assertTrue(any("200000" in error["message"] for error in errors), errors)

    def test_missing_brief_audience(self):
        missing = {
            "explain_schema": 1,
            "question": "What changes?",
            "out_of_scope": [],
            "check_questions": [],
        }
        errors = validate_brief(missing)
        self.assertTrue(any(error["path"] == "/audience" for error in errors), errors)
        empty = dict(missing, audience="")
        errors = validate_brief(empty)
        self.assertTrue(any(error["path"] == "/audience" for error in errors), errors)
        path = self.plan_session / "brief.json"
        original = path.read_text(encoding="utf-8")
        try:
            path.write_text(json.dumps(empty), encoding="utf-8")
            code, out, err = helpers.run_cli("validate", "--session", self.plan_session, env=self.env)
            self.assertEqual(code, 1, out + err)
            self.assertTrue(any(error["path"] == "/audience" for error in json.loads(out)["errors"]))
            stored = json.loads((self.plan_session / "validate.json").read_text(encoding="utf-8"))
            self.assertFalse(stored["ok"])
            self.assertIn("brief_sha256", stored)
        finally:
            path.write_text(original, encoding="utf-8")

    def test_robustness_non_object_roots(self):
        errors, warnings = validate_explain([], self.plan_evidence, self.plan_meta, self.brief)
        self.assertEqual(warnings, [])
        self._assert_error(errors, "", "explain.json must be an object")
        self._assert_error(validate_brief([]), "", "brief.json must be an object")
        session = helpers.make_session(self.env, "idea", slug="bad-root", levels=3, lang="en")
        (session / "explain.json").write_text("[]\n", encoding="utf-8")
        (session / "brief.json").write_text("[]\n", encoding="utf-8")
        (session / "evidence.json").write_text("{}\n", encoding="utf-8")
        with self.assertRaises(ExplainError) as caught:
            run_validate(session)
        self.assertEqual(caught.exception.code, 1)
        paths = [error["path"] for error in caught.exception.errors]
        self.assertIn("", paths)
        stored = json.loads((session / "validate.json").read_text(encoding="utf-8"))
        self.assertFalse(stored["ok"])
        self.assertTrue(any("explain.json" in error["message"] for error in stored["errors"]))
        self.assertTrue(any("brief.json" in error["message"] for error in stored["errors"]))

    def test_robustness_booleans_where_integers_are_required(self):
        explain = copy.deepcopy(self.plan)
        explain["hero"]["nodes"][0]["x"] = True
        explain["levels"] = [True, 3, 5]
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/hero/nodes/0/x", "expected an integer")
        self._assert_error(errors, "/levels/0", "expected an integer")
        errors = validate_brief(
            {
                "explain_schema": True,
                "audience": "Neighbours",
                "question": "What changes?",
                "out_of_scope": [],
                "check_questions": [],
            }
        )
        self._assert_error(errors, "/explain_schema", "explain_schema must be 1")

    def test_robustness_duplicate_ids(self):
        explain = copy.deepcopy(self.plan)
        explain["hero"]["nodes"][1]["id"] = explain["hero"]["nodes"][0]["id"]
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/hero/nodes/1/id", "duplicate id")
        explain = copy.deepcopy(self.plan)
        explain["sections"][1]["id"] = explain["sections"][0]["id"]
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/sections/1/id", "duplicate id")
        explain = copy.deepcopy(self.plan)
        explain["glossary"].append({"term": "plot", "definition": "The same garden bed."})
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/glossary/2/term", "duplicate term")

    def test_claim_units_and_leaves_match_hand_list(self):
        self.assertEqual(claim_units(self.plan), PLAN_UNITS)
        self.assertEqual(claim_leaves(self.plan), PLAN_LEAVES)

    def test_resolve_pointer_escapes_indexes_and_invalid(self):
        obj = {"a/b": {"c~d": [10, {"a~/b": 7}]}, "items": ["zero", "one"]}
        self.assertEqual(resolve_pointer(obj, "/a~1b/c~0d/0"), (True, 10))
        self.assertEqual(resolve_pointer(obj, "/a~1b/c~0d/1/a~0~1b"), (True, 7))
        self.assertEqual(resolve_pointer(obj, "/items/1"), (True, "one"))
        self.assertEqual(resolve_pointer(obj, ""), (True, obj))
        self.assertEqual(resolve_pointer(obj, "/missing"), (False, None))
        self.assertEqual(resolve_pointer(obj, "/items/2"), (False, None))
        self.assertEqual(resolve_pointer(obj, "/items/01"), (False, None))
        self.assertEqual(resolve_pointer(obj, "items/0"), (False, None))
        self.assertEqual(resolve_pointer(obj, "/items/nope"), (False, None))

    def test_fixture_contract_edits_stay_valid(self):
        explain = copy.deepcopy(self.plan)
        risks = next(section for section in explain["sections"] if section["id"] == "risks")
        self.assertEqual(risks["type"], "risks")
        self.assertGreaterEqual(len(risks["items"]), 3)
        del risks["items"][1]
        del risks["items"][0]
        decisions_index = next(
            index for index, section in enumerate(explain["sections"]) if section["id"] == "decisions"
        )
        why = explain["sections"][decisions_index]["items"][0]["why"]
        self.assertIsInstance(why, dict)
        self.assertEqual(set(why), {"1", "3", "5"})
        self.assertEqual(len(CORRECTION_SENTENCE.split()), 20)
        why["3"] = CORRECTION_SENTENCE
        errors, _warnings = self._errors(explain)
        self.assertEqual(errors, [])
        leaf = f"/sections/{decisions_index}/items/0/why/3"
        self.assertIn(("/sections/4/items/0", leaf), claim_leaves(self.plan))
        idea_risks = next(section for section in self.idea["sections"] if section["id"] == "risks")
        self.assertEqual(idea_risks["type"], "risks")
        self.assertGreaterEqual(len(idea_risks["items"]), 2)

    def test_new_node_warning_is_not_an_error(self):
        explain = copy.deepcopy(self.plan)
        for node in explain["hero"]["nodes"]:
            node["tone"] = "new"
        errors, warnings = self._errors(explain)
        self.assertEqual(errors, [])
        self._assert_error(warnings, "/hero/nodes", "tone new")

    def _example(self, evidence="E3", **fields):
        section = {
            "id": "case",
            "type": "example",
            "title": "One booking",
            "evidence": [evidence],
            "confidence": "inferred",
            "situation": "A member wants a plot for the summer.",
            "now": "The member asks the board by email and waits.",
            "after": "The member sees free plots and books one at once.",
        }
        section.update(fields)
        return section

    def _with_example(self, base=None, **fields):
        explain = copy.deepcopy(self.plan if base is None else base)
        explain["sections"].insert(0, self._example(**fields))
        return explain

    def _messages(self, items, path):
        return [item["message"] for item in items if item["path"] == path]

    def test_example_section_valid(self):
        errors, warnings = self._errors(self._with_example())
        self.assertEqual(errors, [])
        self.assertNotIn(NO_EXAMPLE, self._messages(warnings, "/sections"))
        self.assertFalse(any("should be the first" in item["message"] for item in warnings), warnings)

    def test_example_missing_fields_and_caps(self):
        section = self._example()
        del section["after"]
        explain = copy.deepcopy(self.plan)
        explain["sections"].insert(0, section)
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/sections/0/after", "missing key after")
        explain = self._with_example(now=" ".join(["word"] * 61))
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/sections/0/now", "exceeds 60 words")
        explain = self._with_example(now=" ".join(["word"] * 60))
        self.assertEqual(self._errors(explain)[0], [])
        explain = self._with_example(extra="no")
        self._assert_error(self._errors(explain)[0], "/sections/0/extra", "unknown key extra")

    def test_second_example_is_error(self):
        explain = self._with_example()
        explain["sections"].insert(2, self._example(id="case-two"))
        errors, _warnings = self._errors(explain)
        self._assert_error(errors, "/sections/2", "at most one example section")
        self.assertFalse(any(error["path"] == "/sections/0" and "at most one" in error["message"] for error in errors))

    def test_example_not_first_warns(self):
        explain = copy.deepcopy(self.plan)
        explain["sections"].insert(2, self._example())
        errors, warnings = self._errors(explain)
        self.assertEqual(errors, [])
        self.assertIn("example section should be the first section", self._messages(warnings, "/sections/2"))
        self.assertNotIn(NO_EXAMPLE, self._messages(warnings, "/sections"))

    def test_fixtures_warn_no_example(self):
        for name, explain, evidence, session in (
            ("plan", self.plan, self.plan_evidence, self.plan_meta),
            ("idea", self.idea, self.idea_evidence, self.idea_meta),
        ):
            with self.subTest(name=name):
                errors, warnings = self._errors(explain, evidence=evidence, session=session)
                self.assertEqual(errors, [])
                self.assertIn(NO_EXAMPLE, self._messages(warnings, "/sections"))
                self.assertEqual([w for w in warnings if w["path"] != "/sections"], [])

    def test_example_allowed_for_all_kinds(self):
        from explainlib import validate as v

        for kind in ("plan", "idea", "codebase"):
            self.assertIn("example", v._SECTION_KINDS[kind])
        explain = self._with_example(self.idea, evidence="E5")
        errors, warnings = self._errors(explain, evidence=self.idea_evidence, session=self.idea_meta)
        self.assertEqual(errors, [])
        self.assertNotIn(NO_EXAMPLE, self._messages(warnings, "/sections"))

    def test_code_like_labels_warn(self):
        for label in ("T1 parser", "D rebuild", "T5\u2013T6 screen", "finalize_run", "cli.py", "src/app", "build()"):
            with self.subTest(label=label):
                explain = copy.deepcopy(self.plan)
                explain["hero"]["nodes"][0]["label"] = label
                errors, warnings = self._errors(explain)
                self.assertEqual(errors, [])
                self.assertEqual(self._messages(warnings, "/hero/nodes/0/label"), [CODE_LABEL])
        for label in ("CLI", "Member", "Command line (cli.py)", "DNS lookup", "A member", "I agree"):
            with self.subTest(label=label):
                explain = copy.deepcopy(self.plan)
                explain["hero"]["nodes"][0]["label"] = label
                _errors, warnings = self._errors(explain)
                self.assertEqual(self._messages(warnings, "/hero/nodes/0/label"), [])
        explain = copy.deepcopy(self.plan)
        explain["hero"]["zones"][0]["label"] = "T2 zone"
        explain["sections"][0]["title"] = "T3 importer"
        explain["title"] = "T1"
        errors, warnings = self._errors(explain)
        self.assertEqual(errors, [])
        self.assertEqual(self._messages(warnings, "/hero/zones/0/label"), [CODE_LABEL])
        self.assertEqual(self._messages(warnings, "/sections/0/title"), [CODE_LABEL])
        self.assertEqual(self._messages(warnings, "/title"), [])

    def _level1(self, text, glossary=(), title=None, levels=(1, 3, 5), lead_text=None):
        explain = copy.deepcopy(self.plan)
        explain["glossary"] = [{"term": term, "definition": "Explained."} for term in glossary]
        explain["lead"]["text"] = lead_text if lead_text is not None else {
            "1": text,
            "3": "Level three text with API and ZQX.",
            "5": "Level five text with API and ZQX.",
        }
        if title is not None:
            explain["title"] = title
        if tuple(levels) != (1, 3, 5):
            explain["levels"] = list(levels)
            session = dict(self.plan_meta, levels=list(levels))
            explain["lead"]["text"] = {str(level): text for level in levels}
            return validate_explain(explain, self.plan_evidence, session, self.brief)[1]
        return self._errors(explain)[1]

    def _terms(self, warnings):
        found = self._messages(warnings, "/glossary")
        self.assertLessEqual(len(found), 1, found)
        if not found:
            return []
        self.assertTrue(found[0].startswith(LEVEL1_PREFIX), found)
        return found[0][len(LEVEL1_PREFIX):].split(", ")

    def test_level1_terms_warning(self):
        text = "The ZQX file feeds T1 and `load_run`."
        self.assertEqual(self._terms(self._level1(text)), ["ZQX", "T1", "load_run"])
        self.assertEqual(self._terms(self._level1(text, glossary=["ZQX"])), ["T1", "load_run"])
        self.assertEqual(self._terms(self._level1(text, glossary=["ZQX", "T1", "load_run"])), [])
        self.assertEqual(self._terms(self._level1("API (application programming interface) answers.")), [])
        self.assertEqual(self._terms(self._level1("An application programming interface (API) answers.")), [])
        self.assertEqual(self._terms(self._level1("The API answers.")), ["API"])
        self.assertEqual(self._terms(self._level1("(API) answers first.")), ["API"])
        self.assertEqual(self._terms(self._level1("Plain words only.")), [])
        self.assertEqual(self._terms(self._level1("The ASNs answer.", glossary=["ASN"])), [])
        self.assertEqual(self._terms(self._level1("The ZQX file.", glossary=["ZQX file format"])), [])
        self.assertEqual(self._terms(self._level1("The API answers.", title="API gateway")), [])

    def test_level1_terms_only_when_level_one_is_written(self):
        self.assertEqual(self._terms(self._level1("The API answers.", levels=(3,))), [])
        self.assertEqual(self._terms(self._level1("The API answers.", levels=(1, 3))), ["API"])
        # An acronym present only at levels 3 and 5 does not warn.
        explain_text = {"1": "Plain words only.", "3": "The API answers.", "5": "The API answers."}
        self.assertEqual(self._terms(self._level1(None, lead_text=explain_text)), [])

    def test_level1_terms_caps_at_ten_in_first_seen_order(self):
        text = " ".join(f"T{number}" for number in range(1, 13))
        terms = self._terms(self._level1(text))
        self.assertEqual(terms, [f"T{number}" for number in range(1, 11)])

    def test_caps_loosened(self):
        def errors_for(mutate):
            explain = copy.deepcopy(self.plan)
            mutate(explain)
            return self._errors(explain)[0]

        def label(size):
            return lambda e: e["hero"]["nodes"].__getitem__(0).__setitem__("label", "L" * size)

        self.assertEqual(errors_for(label(40)), [])
        self._assert_error(errors_for(label(41)), "/hero/nodes/0/label", "40 characters")
        sixty, over = " ".join(["word"] * 60), " ".join(["word"] * 61)
        change = next(i for i, s in enumerate(self.plan["sections"]) if s["type"] == "change")

        def step(text):
            return lambda e: e["hero"]["steps"][0].__setitem__("text", text)

        def item(text):
            return lambda e: e["sections"][change]["now"][0].__setitem__("text", text)

        self.assertEqual(errors_for(step(sixty)), [])
        self._assert_error(errors_for(step(over)), "/hero/steps/0/text", "exceeds 60 words")
        self.assertEqual(errors_for(item(sixty)), [])
        self._assert_error(errors_for(item(over)), f"/sections/{change}/now/0/text", "exceeds 60 words")
        # Nine sections are allowed, ten are not.
        explain = copy.deepcopy(self.plan)
        while len(explain["sections"]) < 9:
            extra = copy.deepcopy(explain["sections"][0])
            extra["id"] = f"extra-{len(explain['sections'])}"
            explain["sections"].append(extra)
        self.assertEqual(self._errors(explain)[0], [])
        extra = copy.deepcopy(explain["sections"][0])
        extra["id"] = "extra-last"
        explain["sections"].append(extra)
        self._assert_error(self._errors(explain)[0], "/sections", "expected 3..9 items")

    def test_map_role_cap_is_sixty_words(self):
        from explainlib import validate as v

        entry = {"path": "a.py", "role": " ".join(["word"] * 60), "evidence": [], "confidence": "inferred"}
        section = {"entries": [entry]}
        self.assertEqual(v._validate_map(section, "/s", [1, 3, 5], {}, "codebase", None), [])
        entry["role"] = " ".join(["word"] * 61)
        errors = v._validate_map(section, "/s", [1, 3, 5], {}, "codebase", None)
        self._assert_error(errors, "/s/entries/0/role", "exceeds 60 words")

    def test_claim_units_and_leaves_include_example(self):
        explain = self._with_example()
        units = claim_units(explain)
        self.assertEqual(units[7], "/sections/0")
        self.assertEqual(units.count("/sections/0"), 1)
        leaves = [leaf for unit, leaf in claim_leaves(explain) if unit == "/sections/0"]
        self.assertEqual(
            leaves,
            [f"/sections/0/{field}" for field in ("situation", "now", "after")],
        )
        explain["sections"][0]["now"] = {"1": "One.", "3": "Three.", "5": "Five."}
        leaves = [leaf for unit, leaf in claim_leaves(explain) if unit == "/sections/0"]
        self.assertEqual(
            leaves,
            ["/sections/0/situation", "/sections/0/now/1", "/sections/0/now/3", "/sections/0/now/5", "/sections/0/after"],
        )

    def test_words_at_level_counts_example(self):
        from explainlib import validate as v

        base = v._words_at_level(self.plan, 3)
        added = v._words_at_level(self._with_example(), 3)
        self.assertEqual(added - base, 2 + 8 + 9 + 10)  # title, situation, now, after

    def test_apply_corrections_example_leaf(self):
        session = helpers.make_session(self.env, "idea", slug="idea-example", levels=3, lang="en")
        for args in (
            ("ingest", "--session", session, "--text-file", helpers.FIXTURES / "idea.md"),
            ("frame", "--session", session, "--audience", "Neighbours", "--question", "What changes?"),
        ):
            code, out, err = helpers.run_cli(*args, env=self.env)
            self.assertEqual(code, 0, out + err)
        explain = self._with_example(self.idea, evidence="E5")
        explain["sections"][0]["now"] = {
            "1": "A neighbour asks around for a drill.",
            "3": "A neighbour asks around the street for a drill.",
            "5": "A neighbour asks several households for a drill.",
        }
        (session / "explain.json").write_text(json.dumps(explain), encoding="utf-8")
        code, out, err = helpers.run_cli("validate", "--session", session, env=self.env)
        self.assertEqual(code, 0, out + err)
        code, out, err = helpers.run_cli("check-prepare", "--session", session, "--kind", "factcheck", env=self.env)
        self.assertEqual(code, 0, out + err)
        request = json.loads((session / "checks" / "factcheck-request.json").read_text(encoding="utf-8"))
        corrected = "A neighbour walks to the shelf and takes a drill."
        claims = []
        for _unit, leaf in claim_leaves(explain):
            fixed = leaf == "/sections/0/now/1"
            claims.append(
                {
                    "ref": leaf,
                    "claim": "The example leaf is checked against the idea text.",
                    "verdict": "corrected" if fixed else "verified",
                    "evidence": ["E5"],
                    "correction": corrected if fixed else None,
                }
            )
        report = {
            "explain_report": 1,
            "kind": "factcheck",
            "model": "codex/gpt-example",
            "explain_sha256": request["explain_sha256"],
            "claims": claims,
            "plan_checks": [],
            "summary": "Synthetic fact-check report.",
        }
        (session / "factcheck.json").write_text(json.dumps(report), encoding="utf-8")
        code, out, err = helpers.run_cli("check-report", "--session", session, "--kind", "factcheck", env=self.env)
        self.assertEqual(code, 0, out + err)
        code, out, err = helpers.run_cli("apply-corrections", "--session", session, env=self.env)
        self.assertEqual(code, 0, out + err)
        updated = json.loads((session / "explain.json").read_text(encoding="utf-8"))
        self.assertEqual(updated["sections"][0]["now"]["1"], corrected)
        self.assertEqual(updated["sections"][0]["now"]["3"], explain["sections"][0]["now"]["3"])

    def test_schema_md_plan_example_validates(self):
        self._check_schema_md_example("plan")

    def test_schema_md_idea_example_validates(self):
        self._check_schema_md_example("idea")

    def _check_schema_md_example(self, kind):
        text = SCHEMA_MD.read_text(encoding="utf-8")
        start = text.index(f"## Minimal {kind} example")
        example = json.loads(re.search(r"```json\n(.*?)\n```", text[start:], re.S).group(1))
        self.assertEqual(example["kind"], kind)
        self.assertEqual(example["sections"][0]["type"], "example")
        with tempfile.TemporaryDirectory(prefix="pe-schema-") as tmp:
            home = Path(tmp)
            env = {"HOME": str(home), "PASEO_EXPLAIN_HOME": str(home / "pe"), "XDG_CONFIG_HOME": str(home / "cfg")}
            if kind == "plan":
                source = home / "plan.txt"
                source.write_text(
                    "Garden members book plots. One booking per plot. The board lists free plots.\n", encoding="utf-8"
                )
                session = helpers.make_session(env, "plan", slug="garden-example", mode="quick")
                ingest = ("--file", f"plan={source}")
                frame = ("--audience", "Garden members", "--question", "How are plots booked?")
            else:
                source = home / "idea.txt"
                source.write_text(
                    "Neighbours could share tools on a shelf. A volunteer could check returns.\n", encoding="utf-8"
                )
                session = helpers.make_session(env, "idea", slug="shelf-example", mode="quick")
                ingest = ("--text-file", str(source))
                frame = ("--audience", "Neighbours", "--question", "How could tool lending work?")
            for args in (("frame", "--session", session, *frame), ("ingest", "--session", session, *ingest)):
                code, out, err = helpers.run_cli(*args, env=env)
                self.assertEqual(code, 0, out + err)
            (session / "explain.json").write_text(json.dumps(example), encoding="utf-8")
            code, out, err = helpers.run_cli("validate", "--session", session, env=env)
            self.assertEqual(code, 0, out + err)
            self.assertEqual(json.loads(out)["warnings"], [])

    def test_schema_matches_validator_keys(self):
        schema = json.loads(
            (helpers.REPO / "paseo-explain" / "references" / "explain.schema.json").read_text(encoding="utf-8")
        )
        self.assertIn("2020-12", schema["$schema"])
        self.assertEqual(set(schema["properties"]), set(EXPLAIN_TOP_KEYS))
        self.assertIs(schema["additionalProperties"], False)

        def walk(node):
            if isinstance(node, dict):
                if node.get("type") == "object" or "properties" in node:
                    self.assertIn("additionalProperties", node)
                    self.assertIs(node["additionalProperties"], False)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(schema)


if __name__ == "__main__":
    unittest.main()
