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
