"""Renderer, Markdown twin, and injection payload."""

import contextlib
import copy
import html
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

sys.path.insert(0, str(helpers.REPO / "paseo-explain" / "scripts"))

from explainlib.common import ExplainError, canonical_sha256

TEMPLATE = helpers.REPO / "paseo-explain" / "assets" / "template.html"
_PLACEHOLDER = re.compile(r"\{\{(LANG|TITLE|NONCE|DATA_JSON|MERMAID_SRC|MERMAID_SRI)\}\}")
_DATA_RE = re.compile(
    r'<script type="application/json" id="pe-data">(.*?)</script>',
    re.DOTALL,
)
_CSP_NONCE = re.compile(r"script-src 'nonce-([^']+)'")
_SCRIPT_NONCE = re.compile(r'<script nonce="([^"]*)">')
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
CHECKS = {
    "validate": "pass",
    "fact_check": "skipped",
    "fact_check_corrections": 0,
    "reader_test": "skipped",
    "independent": False,
    "check_label": "not_checked",
    "skipped": {"fact_check": None, "reader_test": None},
}
MODELS = {"fact_checker": None, "reader": None, "orchestrator": "example/model"}
META_KEYS = {
    "version",
    "slug",
    "round",
    "generated_at",
    "default_level",
    "levels",
    "lang",
    "theme",
    "mode",
    "checks",
    "check_label",
    "skipped",
    "models",
    "fact_counts",
    "flagged_total",
    "flagged_anchors",
    "reader_grade",
}
PLAN_HEADINGS = [
    "# Garden plot booking",
    "## Overview",
    "## 01 What the plan covers",
    "## 02 What changes",
    "## 03 Booking flow",
    "## 04 Requirements and tasks",
    "## 05 Choices",
    "## 06 Risks and open points",
    "## 07 Check your reading",
    "## Glossary",
]


class ResultDouble:
    def __init__(self, fact_check="skipped"):
        self.fact_check = fact_check
        self.compute_notes = []
        self.write_notes = []

    def compute_result(self, session_dir):
        root = Path(session_dir)
        self.compute_notes.append(
            {
                "validate": (root / "validate.json").is_file(),
                "html": (root / "explain.html").is_file(),
                "md": (root / "explain.md").is_file(),
            }
        )
        checks = dict(CHECKS)
        checks["fact_check"] = self.fact_check
        return {"status": "failed", "checks": checks, "models": MODELS}

    def write_result(self, session_dir):
        root = Path(session_dir)
        session = json.loads((root / "session.json").read_text(encoding="utf-8"))
        self.write_notes.append(
            {
                "html": (root / "explain.html").is_file(),
                "md": (root / "explain.md").is_file(),
                "round": session["round"],
                "content_sha256": session["content_sha256"],
            }
        )
        return {"ok": True}


@contextlib.contextmanager
def patched_result(double):
    import explainlib

    with mock.patch.dict(sys.modules, {"explainlib.result": double}), mock.patch.object(
        explainlib, "result", double, create=True
    ):
        yield double


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


def _anchor(evidence, eid):
    sources = {src["id"]: src["display"] for src in evidence["sources"]}
    frag = next(item for item in evidence["fragments"] if item["id"] == eid)
    return f"[{sources[frag['source']]} {frag['anchor']}]"


def _payload(page):
    found = _DATA_RE.findall(page)
    if len(found) != 1:
        raise AssertionError(f"expected one data block, found {len(found)}")
    return found[0]


def _nonces(page):
    csp = _CSP_NONCE.findall(page)
    script = _SCRIPT_NONCE.findall(page)
    return csp, script


def _assert_single_pass(test, template, filled, values):
    template_at = filled_at = 0
    for match in _PLACEHOLDER.finditer(template):
        gap = template[template_at : match.start()]
        test.assertEqual(filled[filled_at : filled_at + len(gap)], gap)
        filled_at += len(gap)
        replacement = values[match.group(1)]
        test.assertEqual(filled[filled_at : filled_at + len(replacement)], replacement)
        filled_at += len(replacement)
        template_at = match.end()
    test.assertEqual(filled[filled_at:], template[template_at:])


def _write_factcheck(session, verdicts):
    report = {
        "explain_report": 1,
        "kind": "factcheck",
        "model": "other/model",
        "explain_sha256": "abc",
        "claims": [
            {
                "ref": "/lead/text/3",
                "claim": "claim",
                "verdict": verdict,
                "evidence": [],
                "correction": None,
            }
            for verdict in verdicts
        ],
        "plan_checks": [],
        "summary": "counted",
    }
    (Path(session) / "factcheck.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _undo_literal(payload):
    prefix = "() => { const h = "
    suffix = "; document.open(); document.write(h); document.close(); return document.title; }"
    if not payload.startswith(prefix) or not payload.endswith(suffix):
        raise AssertionError(payload[:80])
    literal = payload[len(prefix) : -len(suffix)]
    return json.loads(literal.replace("<\\/", "</").replace("<\\!--", "<!--"))


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="pe-test-")
        root = Path(cls._tmp.name)
        cls.env = {
            "HOME": str(root / "home"),
            "PASEO_EXPLAIN_HOME": str(root / "pe"),
            "XDG_CONFIG_HOME": str(root / "cfg"),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        cls._saved = {key: os.environ.get(key) for key in cls.env}
        os.environ.update(cls.env)
        cls.plan_session = _init_plan(cls.env)
        cls.idea_session = _init_idea(cls.env)
        cls.plan = json.loads((helpers.FIXTURES / "plan-explain.json").read_text(encoding="utf-8"))
        cls.idea = json.loads((helpers.FIXTURES / "idea-explain.json").read_text(encoding="utf-8"))
        cls.plan_evidence = json.loads((cls.plan_session / "evidence.json").read_text(encoding="utf-8"))
        cls.idea_evidence = json.loads((cls.idea_session / "evidence.json").read_text(encoding="utf-8"))
        cls.template = TEMPLATE.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        for key, value in cls._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        cls._tmp.cleanup()

    def clone(self, src, name):
        dest = Path(self._tmp.name) / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        return dest

    def test_embed_json_escapes_and_roundtrips(self):
        from explainlib import render

        obj = {"lead": "</script>&<\u2028\u2029>", "keep": "{{TITLE}}"}
        encoded = render.embed_json(obj)
        self.assertNotIn("<", encoded)
        self.assertNotIn(">", encoded)
        self.assertNotIn("&", encoded)
        self.assertNotIn("\u2028", encoded)
        self.assertNotIn("\u2029", encoded)
        self.assertIn("\\u003c", encoded)
        self.assertIn("\\u003e", encoded)
        self.assertIn("\\u0026", encoded)
        self.assertEqual(json.loads(encoded), obj)

    def test_fill_template_is_single_pass(self):
        from explainlib import render

        template = "L {{LANG}} T {{TITLE}} D {{DATA_JSON}} N {{NONCE}} S {{MERMAID_SRC}} H {{MERMAID_SRI}} N2 {{NONCE}}"
        values = {
            "LANG": "en",
            "TITLE": "x {{DATA_JSON}}",
            "DATA_JSON": "y {{TITLE}} {{NONCE}}",
            "NONCE": "n-1",
            "MERMAID_SRC": render.MERMAID_SRC,
            "MERMAID_SRI": render.MERMAID_SRI,
        }
        filled = render.fill_template(template, values)
        self.assertEqual(
            filled,
            "L en T x {{DATA_JSON}} D y {{TITLE}} {{NONCE}} N n-1 S "
            + render.MERMAID_SRC
            + " H "
            + render.MERMAID_SRI
            + " N2 n-1",
        )
        _assert_single_pass(self, template, filled, values)

    def test_injection_payload_roundtrip(self):
        from explainlib import render

        page = "<html><!--\u2028--></script>\u2029</html>"
        payload = render.injection_payload(page)
        self.assertTrue(payload.startswith("() => {"))
        self.assertNotIn("</script", payload)
        self.assertNotIn("<!--", payload)
        self.assertNotIn("\u2028", payload)
        self.assertNotIn("\u2029", payload)
        self.assertIn("document.write", payload)
        self.assertEqual(_undo_literal(payload), page)

    def test_template_placeholders_and_forbidden_strings(self):
        from explainlib import render

        counts = {name: 0 for name in ("LANG", "TITLE", "DATA_JSON", "MERMAID_SRC", "MERMAID_SRI", "NONCE")}
        for match in _PLACEHOLDER.finditer(self.template):
            counts[match.group(1)] += 1
        self.assertEqual(counts["LANG"], 1)
        self.assertEqual(counts["TITLE"], 1)
        self.assertEqual(counts["DATA_JSON"], 1)
        self.assertEqual(counts["MERMAID_SRC"], 1)
        self.assertEqual(counts["MERMAID_SRI"], 1)
        self.assertEqual(counts["NONCE"], 2)
        for needle in FORBIDDEN:
            self.assertNotIn(needle, self.template)
        self.assertEqual(
            render.MERMAID_SRC,
            "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js",
        )
        self.assertEqual(
            render.MERMAID_SRI,
            "sha384-rbtjAdnIQE/aQJGEgXrVUlMibdfTSa4PQju4HDhN3sR2PmaKFzhEafuePsl9H/9I",
        )
        self.assertIn("{nonce}", render.CSP_TEMPLATE)

    def test_markdown_twin_structure(self):
        from explainlib import render

        evidence = self.plan_evidence
        meta = {
            "check_label": "not_checked",
            "models": MODELS,
            "checks": CHECKS,
            "skipped": CHECKS["skipped"],
            "mode": "standard",
            "fact_counts": None,
            "reader_grade": None,
        }
        md = render.render_markdown(self.plan, evidence, meta, 3)
        headings = [line for line in md.splitlines() if line.startswith("#")]
        self.assertEqual(headings, PLAN_HEADINGS)
        lead = (
            "Neighbours reserve one plot for a season, and the plan keeps two active "
            "bookings from overlapping."
        )
        self.assertIn(lead + " " + _anchor(evidence, "E1"), md)
        self.assertIn("- **Plots:** Name, size, season " + _anchor(evidence, "E5"), md)
        self.assertIn("> Not independently checked", md)
        self.assertIn(
            "- **Plot records** (existing): A plot record stores its name, size, and season label before anyone books it.",
            md,
        )
        self.assertIn(
            "1. The gardener picks a plot, and the overlap rule accepts only one active booking. "
            + _anchor(evidence, "E6"),
            md,
        )
        self.assertIn("**Now**", md)
        self.assertIn("**Next**", md)
        self.assertIn("**Unchanged**", md)
        mermaid = self.plan["sections"][2]["mermaid"].replace("<", "&lt;").replace(">", "&gt;")
        self.assertIn("```mermaid\n" + mermaid + "```", md)
        self.assertIn(self.plan["sections"][2]["alt"], md)
        self.assertIn("| Requirement | T1 | T2 | T3 | T4 | T5 | Test |", md)
        self.assertIn("R3 A full plot keeps a waitlist in arrival order.", md)
        self.assertIn("| no |", md)
        self.assertIn("- **Chosen** Keep plots on paper at the hall:", md)
        self.assertIn("- **Rejected** Make the waitlist mandatory:", md)
        self.assertIn("- **Risk** The public board has no task:", md)
        self.assertIn("- **Open question** Who updates the paper calendar:", md)
        self.assertIn("- **Assumption** One garden only:", md)
        self.assertRegex(
            md,
            r"1\. May two active bookings exist for the same plot and season\?\n"
            r"Answer: No\. The overlap rule refuses the second active booking\.",
        )
        self.assertIn("## Glossary", md)
        self.assertIn("**Plot**", md)
        self.assertIn("One garden bed that a neighbour can book for a season.", md)
        self.assertNotIn("<", md)
        level_one = render.render_markdown(self.plan, evidence, meta, 1)
        self.assertIn("Neighbours book one garden plot for a season.", level_one)
        self.assertNotIn(lead, level_one)

    def test_markdown_escapes_angles_reader_and_banner(self):
        from explainlib import render

        explain = copy.deepcopy(self.plan)
        explain["lead"]["text"] = "see a <tag> and a > mark"
        md = render.render_markdown(
            explain,
            self.plan_evidence,
            {"check_label": "not_checked", "mode": "standard", "models": {}, "checks": {}, "skipped": {}},
            3,
        )
        self.assertNotIn("<", md)
        self.assertIn("see a &lt;tag&gt; and a &gt; mark", md)
        reader = render.render_markdown(
            self.plan,
            self.plan_evidence,
            {"check_label": "not_checked", "mode": "standard", "models": {}, "checks": {}, "skipped": {}},
            3,
            for_reader=True,
        )
        self.assertNotIn("Answer:", reader)
        self.assertIn("May two active bookings exist for the same plot and season?", reader)
        banner = {
            "check_label": "independent_corrected",
            "models": {"fact_checker": "codex/gpt-example", "reader": None, "orchestrator": "example/model"},
            "fact_counts": {"verified": 4, "corrected": 1, "unsupported": 2, "unverifiable": 0},
            "checks": {"fact_check_corrections": 3, "check_label": "independent_corrected", "skipped": {}},
            "skipped": {"fact_check": "no second model", "reader_test": None},
            "mode": "deep",
            "reader_grade": {"correct": 2, "total": 3, "report_sha256": "abc"},
        }
        checked = render.render_markdown(self.plan, self.plan_evidence, banner, 3)
        self.assertIn(
            "> Fact-checked by codex/gpt-example (another model family): 4 verified · 1 corrected; "
            "3 corrections applied afterwards by the explainer (not re-checked)",
            checked,
        )
        self.assertIn("> Fact-check skipped: no second model", checked)
        self.assertIn("> Reader test: 2/3 questions answered", checked)
        dutch = copy.deepcopy(self.plan)
        dutch["lang"] = "nl"
        dutch_md = render.render_markdown(
            dutch,
            self.plan_evidence,
            {"check_label": "not_checked", "mode": "standard", "models": {}, "checks": {}, "skipped": {}},
            3,
        )
        self.assertIn("> Niet onafhankelijk gecontroleerd", dutch_md)
        flagged = copy.deepcopy(self.plan_evidence)
        for frag in flagged["fragments"]:
            if frag["id"] == "E1":
                frag["flagged"] = True
        flagged_md = render.render_markdown(
            self.plan,
            flagged,
            {"check_label": "not_checked", "mode": "standard", "models": {}, "checks": {}, "skipped": {}},
            3,
        )
        self.assertIn("> Contains instruction-like text in the sources; treated as data", flagged_md)
        idea_md = render.render_markdown(
            self.idea,
            self.idea_evidence,
            {"check_label": "not_checked", "mode": "standard", "models": {}, "checks": {}, "skipped": {}},
            3,
        )
        self.assertNotIn("<", idea_md)

    def test_render_escapes_payload_nonce_round_and_export(self):
        from explainlib import render

        session = self.clone(self.plan_session, "xss")
        explain = json.loads((session / "explain.json").read_text(encoding="utf-8"))
        attack = '</script><img src=x onerror=alert(1)> <!-- \u2028 & {{TITLE}} {{NONCE}}'
        explain["title"] = 'A <B> & "C"'
        explain["lead"]["text"] = {key: attack for key in explain["lead"]["text"]}
        (session / "explain.json").write_text(
            json.dumps(explain, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        export = Path(self._tmp.name) / "export-xss"
        stored = json.loads((session / "session.json").read_text(encoding="utf-8"))
        stored["export_dir"] = str(export)
        (session / "session.json").write_text(
            json.dumps(stored, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        double = ResultDouble()
        with patched_result(double):
            first = render.render(session)
            first_page = (session / "explain.html").read_text(encoding="utf-8")
            second = render.render(session)
        page = (session / "explain.html").read_text(encoding="utf-8")
        self.assertEqual(page.count('<script type="application/json" id="pe-data">'), 1)
        self.assertNotIn("</script><img", page)
        self.assertEqual(page.count("<!--"), self.template.count("<!--"))
        self.assertEqual(page.count("</script>"), self.template.count("</script>"))
        encoded = _payload(page)
        self.assertNotIn("</script", encoded)
        self.assertNotIn("<", encoded)
        restored = json.loads(encoded)
        self.assertEqual(restored["explain"]["lead"]["text"]["3"], attack)
        self.assertEqual(restored["explain"]["title"], 'A <B> & "C"')
        self.assertIn("{{TITLE}}", encoded)
        self.assertIn("{{NONCE}}", encoded)
        csp, scripts = _nonces(page)
        self.assertEqual(len(csp), 1)
        self.assertEqual(csp, scripts)
        nonce = csp[0]
        self.assertIn(render.CSP_TEMPLATE.format(nonce=nonce), page)
        self.assertIn(f'<script nonce="{nonce}">', page)
        values = {
            "LANG": "en",
            "TITLE": html.escape('A <B> & "C"', quote=True),
            "NONCE": nonce,
            "DATA_JSON": encoded,
            "MERMAID_SRC": render.MERMAID_SRC,
            "MERMAID_SRI": render.MERMAID_SRI,
        }
        _assert_single_pass(self, self.template, page, values)
        self.assertIn("<title>A &lt;B&gt; &amp; &quot;C&quot;</title>", page)
        meta = restored["meta"]
        self.assertEqual(set(meta), META_KEYS)
        self.assertNotIn("status", meta)
        self.assertEqual(meta["version"], "0.1.0")
        self.assertEqual(meta["checks"], CHECKS)
        self.assertEqual(meta["check_label"], "not_checked")
        self.assertEqual(meta["skipped"], CHECKS["skipped"])
        self.assertEqual(meta["models"], MODELS)
        self.assertIsNone(meta["fact_counts"])
        self.assertIsNone(meta["reader_grade"])
        self.assertIsNone(meta["theme"])
        self.assertEqual(meta["lang"], "en")
        self.assertEqual(meta["default_level"], 3)
        self.assertEqual(meta["levels"], [1, 3, 5])
        self.assertEqual(meta["mode"], "standard")
        self.assertEqual(meta["slug"], stored["slug"])
        session_now = json.loads((session / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["round"], 1)
        self.assertEqual(session_now["round"], 1)
        self.assertEqual(session_now["content_sha256"], canonical_sha256(explain))
        self.assertEqual(first["round"], 1)
        self.assertEqual(second["round"], 1)
        self.assertNotEqual(_nonces(first_page)[0][0], _nonces(page)[0][0])
        self.assertEqual(json.loads(_payload(page))["meta"]["round"], 1)
        self.assertEqual((export / "explain.html").read_bytes(), (session / "explain.html").read_bytes())
        self.assertEqual((export / "explain.md").read_bytes(), (session / "explain.md").read_bytes())
        self.assertEqual(first["export"], str(export))
        self.assertEqual(first["bytes"], (session / "explain.html").stat().st_size)
        self.assertTrue(first["html"].endswith("explain.html"))
        self.assertTrue(Path(first["html"]).is_file())
        self.assertNotIn("<", (session / "explain.md").read_text(encoding="utf-8"))
        evidence_block = restored["evidence"]
        self.assertIn("E1", evidence_block)
        sample = evidence_block["E1"]
        self.assertEqual(
            set(sample),
            {"anchor", "source_display", "line_start", "line_end", "text", "flagged"},
        )
        self.assertLessEqual(len(sample["text"]), 800)
        self.assertEqual(len(double.write_notes), 2)
        self.assertTrue(all(note["html"] and note["md"] for note in double.write_notes))

    def test_only_valid_configured_theme_is_embedded(self):
        from explainlib import render

        with tempfile.TemporaryDirectory(prefix="pe-test-theme-") as config_home:
            config_dir = Path(config_home) / "paseo-explain"
            config_dir.mkdir()
            for name, value, expected in (("light", "light", "light"), ("invalid", "blue", None)):
                with self.subTest(name=name):
                    session = self.clone(self.plan_session, "configured-theme-" + name)
                    (config_dir / "config.json").write_text(
                        json.dumps({"theme": value}) + "\n", encoding="utf-8"
                    )
                    with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home}), patched_result(ResultDouble()):
                        render.render(session)
                    page = (session / "explain.html").read_text(encoding="utf-8")
                    self.assertEqual(json.loads(_payload(page))["meta"]["theme"], expected)

    def test_flagged_fragments_and_fact_counts(self):
        from explainlib import render

        idea = self.clone(self.idea_session, "idea-flags")
        plan = self.clone(self.plan_session, "plan-flags")
        with patched_result(ResultDouble()):
            render.render(idea)
            render.render(plan)
        idea_page = json.loads(_payload((idea / "explain.html").read_text(encoding="utf-8")))
        plan_page = json.loads(_payload((plan / "explain.html").read_text(encoding="utf-8")))
        evidence = json.loads((idea / "evidence.json").read_text(encoding="utf-8"))
        flagged = [frag for frag in evidence["fragments"] if frag.get("flagged")]
        self.assertEqual([frag["anchor"] for frag in flagged], ["¶3"])
        self.assertEqual(idea_page["meta"]["flagged_total"], 1)
        self.assertEqual(idea_page["meta"]["flagged_anchors"], ["¶3"])
        self.assertNotIn(flagged[0]["id"], idea_page["evidence"])
        self.assertIsNone(idea_page["meta"]["fact_counts"])
        self.assertIn(
            "> Contains instruction-like text in the sources; treated as data",
            (idea / "explain.md").read_text(encoding="utf-8"),
        )
        self.assertEqual(plan_page["meta"]["flagged_total"], 0)
        self.assertEqual(plan_page["meta"]["flagged_anchors"], [])
        self.assertNotIn(
            "instruction-like",
            (plan / "explain.md").read_text(encoding="utf-8"),
        )

        skipped = self.clone(self.plan_session, "skipped-facts")
        verdicts = ["verified", "verified", "corrected", "unsupported", "unverifiable", "other"]
        _write_factcheck(skipped, verdicts)
        with patched_result(ResultDouble("skipped")):
            render.render(skipped)
        skipped_meta = json.loads(_payload((skipped / "explain.html").read_text(encoding="utf-8")))["meta"]
        self.assertIsNone(skipped_meta["fact_counts"])

        stale = self.clone(self.plan_session, "stale-facts")
        _write_factcheck(stale, verdicts)
        with patched_result(ResultDouble("stale")):
            render.render(stale)
        stale_meta = json.loads(_payload((stale / "explain.html").read_text(encoding="utf-8")))["meta"]
        self.assertIsNone(stale_meta["fact_counts"])

        counted = self.clone(self.plan_session, "counted-facts")
        _write_factcheck(counted, verdicts)
        with patched_result(ResultDouble("partial")):
            render.render(counted)
        counted_meta = json.loads(_payload((counted / "explain.html").read_text(encoding="utf-8")))["meta"]
        self.assertEqual(
            counted_meta["fact_counts"],
            {"verified": 2, "corrected": 1, "unsupported": 1, "unverifiable": 1},
        )

        many = self.clone(self.plan_session, "many-flags")
        stored = json.loads((many / "evidence.json").read_text(encoding="utf-8"))
        base = stored["fragments"][0]
        extras = []
        for index in range(12):
            frag = copy.deepcopy(base)
            frag["id"] = f"Z{index}"
            frag["anchor"] = f"¶{index}"
            frag["flagged"] = True
            extras.append(frag)
        stored["fragments"] = extras + stored["fragments"]
        (many / "evidence.json").write_text(
            json.dumps(stored, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with patched_result(ResultDouble()):
            render.render(many)
        many_meta = json.loads(_payload((many / "explain.html").read_text(encoding="utf-8")))["meta"]
        self.assertEqual(many_meta["flagged_total"], 12)
        self.assertEqual(many_meta["flagged_anchors"], [f"¶{index}" for index in range(10)])

    def test_round_increments_only_when_content_changes(self):
        from explainlib import render

        session = self.clone(self.plan_session, "rounds")
        explain = json.loads((session / "explain.json").read_text(encoding="utf-8"))
        double = ResultDouble()
        with patched_result(double):
            render.render(session)
            self.assertFalse(double.compute_notes[0]["html"])
            self.assertTrue(double.compute_notes[0]["validate"])
            self.assertEqual(len(double.write_notes), 1)
            self.assertEqual(double.write_notes[0]["round"], 1)
            self.assertEqual(double.write_notes[0]["content_sha256"], canonical_sha256(explain))
            self.assertTrue(double.write_notes[0]["html"])
            self.assertTrue(double.write_notes[0]["md"])
            render.render(session)
            stored = json.loads((session / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["round"], 1)
            page = (session / "explain.html").read_text(encoding="utf-8")
            self.assertEqual(json.loads(_payload(page))["meta"]["round"], 1)
            explain["lead"]["text"]["3"] += " Today."
            (session / "explain.json").write_text(
                json.dumps(explain, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            third = render.render(session)
        stored = json.loads((session / "session.json").read_text(encoding="utf-8"))
        page = (session / "explain.html").read_text(encoding="utf-8")
        self.assertEqual(stored["round"], 2)
        self.assertEqual(stored["content_sha256"], canonical_sha256(explain))
        self.assertEqual(json.loads(_payload(page))["meta"]["round"], 2)
        self.assertEqual(third["round"], 2)
        self.assertEqual(len(double.write_notes), 3)
        self.assertEqual(len(double.compute_notes), 3)
        self.assertIsNone(third["export"])

    def test_failed_validation_does_not_write_or_call_result(self):
        from explainlib import render

        session = self.clone(self.plan_session, "invalid")
        explain = json.loads((session / "explain.json").read_text(encoding="utf-8"))
        explain["title"] = ""
        (session / "explain.json").write_text(json.dumps(explain) + "\n", encoding="utf-8")
        double = ResultDouble()
        with patched_result(double):
            with self.assertRaises(ExplainError) as caught:
                render.render(session)
        self.assertEqual(caught.exception.code, 1)
        self.assertFalse((session / "explain.html").exists())
        self.assertFalse((session / "explain.md").exists())
        self.assertEqual(double.compute_notes, [])
        self.assertEqual(double.write_notes, [])

    def test_inject_and_markdown_command(self):
        from explainlib import render

        session = self.clone(self.plan_session, "commands")
        double = ResultDouble()
        with patched_result(double):
            render.render(session)
            payload = render.inject(session)
            text = render.markdown_command(session, 1, None)
            dest = Path(self._tmp.name) / "twin.md"
            written = render.markdown_command(session, 3, str(dest))
            with self.assertRaises(ExplainError) as caught:
                render.markdown_command(session, 2, None)
            with self.assertRaises(ExplainError) as limited:
                render.inject(session, max_bytes=10)
        page = (session / "explain.html").read_text(encoding="utf-8")
        self.assertTrue(payload.startswith("() => {"))
        self.assertNotIn("</script", payload)
        self.assertNotIn("<!--", payload)
        self.assertEqual(_undo_literal(payload), page)
        self.assertIn("Neighbours book one garden plot for a season.", text)
        self.assertIsInstance(text, str)
        self.assertEqual(written, {"ok": True, "path": str(dest.resolve())})
        self.assertEqual(dest.read_text(encoding="utf-8"), render.render_markdown(
            json.loads((session / "explain.json").read_text(encoding="utf-8")),
            json.loads((session / "evidence.json").read_text(encoding="utf-8")),
            json.loads(_payload(page))["meta"],
            3,
        ))
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(limited.exception.code, 1)
        bare = self.clone(self.plan_session, "no-html")
        with self.assertRaises(ExplainError) as missing:
            render.inject(bare)
        self.assertEqual(missing.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
