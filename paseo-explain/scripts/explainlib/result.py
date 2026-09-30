"""result.json contract: checks, findings, and export copies."""

import re
import shutil
from pathlib import Path

from explainlib import EXPLAIN_CONTRACT, __version__
from explainlib.common import ExplainError, current_hashes, load_session, now_iso, read_json, save_session, write_json
from explainlib.reports import factcheck_state, reader_state
from explainlib.validate import claim_leaves

RESULT_KEYS = frozenset(
    {
        "explain_contract",
        "version",
        "status",
        "kind",
        "doc",
        "slug",
        "round",
        "mode",
        "levels",
        "default_level",
        "lang",
        "urls",
        "tab_opened",
        "paths",
        "checks",
        "models",
        "findings",
        "generated_at",
    }
)

_THINKING = ":<thinking>"


def model_family(s: str) -> str:
    if not isinstance(s, str):
        return ""
    part = s.rsplit("/", 1)[-1]
    if part.endswith(_THINKING):
        part = part[: -len(_THINKING)]
    part = part.lower()
    match = re.match(r"[a-z]+", part)
    return match.group(0) if match else ""


def _parse(path: Path):
    if not path.is_file():
        return None
    try:
        return read_json(path)
    except (OSError, UnicodeDecodeError, ValueError, TypeError):
        return None


def _validate_status(session_dir: Path) -> str:
    document = _parse(session_dir / "validate.json")
    if not isinstance(document, dict) or document.get("ok") is not True:
        return "fail"
    current = current_hashes(session_dir)
    for key in ("explain_sha256", "evidence_sha256", "brief_sha256"):
        if document.get(key) != current.get(key):
            return "stale"
    return "pass"


def _unit_for_leaf(explain, leaf: str):
    if not isinstance(explain, dict):
        return None
    for unit, candidate in claim_leaves(explain):
        if candidate == leaf:
            return unit
    return None


def _add_finding(findings, seen, kind, ref, text):
    if not isinstance(ref, str):
        ref = ""
    if not isinstance(text, str):
        text = ""
    key = (kind, ref)
    if key in seen:
        return
    seen.add(key)
    findings.append({"kind": kind, "ref": ref, "text": text})


def _findings(session_dir: Path, session, fact_state) -> list:
    findings = []
    seen = set()
    explain = _parse(session_dir / "explain.json")
    evidence = _parse(session_dir / "evidence.json")
    sections = explain.get("sections") if isinstance(explain, dict) and isinstance(explain.get("sections"), list) else []
    coverage = [section for section in sections if isinstance(section, dict) and section.get("type") == "coverage"]
    for section in coverage:
        requirements = section.get("requirements") if isinstance(section.get("requirements"), list) else []
        for req in requirements:
            if not isinstance(req, dict):
                continue
            tasks = req.get("tasks")
            if not isinstance(tasks, list) or len(tasks) == 0:
                ref = req.get("id") if isinstance(req.get("id"), str) else ""
                text = req.get("text") if isinstance(req.get("text"), str) else ""
                _add_finding(findings, seen, "requirement_without_task", ref, text)
    for section in coverage:
        requirements = section.get("requirements") if isinstance(section.get("requirements"), list) else []
        tasks = section.get("tasks") if isinstance(section.get("tasks"), list) else []
        linked = set()
        for req in requirements:
            if isinstance(req, dict) and isinstance(req.get("tasks"), list):
                for task_id in req["tasks"]:
                    if isinstance(task_id, str):
                        linked.add(task_id)
        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id")
            if isinstance(task_id, str) and task_id not in linked:
                label = task.get("label") if isinstance(task.get("label"), str) else ""
                _add_finding(findings, seen, "task_without_requirement", task_id, label)
    for section in coverage:
        requirements = section.get("requirements") if isinstance(section.get("requirements"), list) else []
        for req in requirements:
            if isinstance(req, dict) and req.get("test") == "no":
                ref = req.get("id") if isinstance(req.get("id"), str) else ""
                text = req.get("text") if isinstance(req.get("text"), str) else ""
                _add_finding(findings, seen, "requirement_without_test", ref, text)
    identity = session.get("identity") if isinstance(session.get("identity"), dict) else {}
    structured = evidence.get("structured") if isinstance(evidence, dict) and isinstance(evidence.get("structured"), dict) else {}
    if identity.get("source") == "autopilot" and structured.get("refs_present") is True:
        referenced = set()
        for task in structured.get("tasks") if isinstance(structured.get("tasks"), list) else []:
            if not isinstance(task, dict):
                continue
            refs = task.get("requirement_refs")
            if isinstance(refs, list):
                for ref in refs:
                    if isinstance(ref, str):
                        referenced.add(ref)
        requirements = structured.get("requirements") if isinstance(structured.get("requirements"), list) else []
        for req in requirements:
            if not isinstance(req, dict):
                continue
            ref = req.get("id")
            if isinstance(ref, str) and ref not in referenced:
                title = req.get("title") if isinstance(req.get("title"), str) else ""
                _add_finding(findings, seen, "requirement_without_task", ref, title)
    if fact_state.get("state") in ("current", "reconciled"):
        report = fact_state.get("report") if isinstance(fact_state.get("report"), dict) else {}
        applied = fact_state.get("applied") if isinstance(fact_state.get("applied"), dict) else {}
        removed = set(applied.get("removed")) if fact_state.get("state") == "reconciled" and isinstance(applied.get("removed"), list) else set()
        frozen = _parse(session_dir / "checks" / "factcheck-explain.json")
        claims = report.get("claims") if isinstance(report.get("claims"), list) else []
        for claim in claims:
            if not isinstance(claim, dict) or claim.get("verdict") != "unsupported":
                continue
            leaf = claim.get("ref")
            if not isinstance(leaf, str):
                continue
            unit = _unit_for_leaf(frozen if isinstance(frozen, dict) else explain, leaf)
            if unit in removed:
                continue
            text = claim.get("claim") if isinstance(claim.get("claim"), str) else ""
            _add_finding(findings, seen, "unsupported_claim", f"checked:{leaf}", text)
        plan_checks = report.get("plan_checks") if isinstance(report.get("plan_checks"), list) else []
        for item in plan_checks:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            ref = item.get("ref") if isinstance(item.get("ref"), str) else ""
            text = item.get("text") if isinstance(item.get("text"), str) else ""
            if kind in ("requirement_without_task", "task_without_requirement", "contradiction"):
                _add_finding(findings, seen, kind, ref, text)
    for section_index, section in enumerate(sections):
        if not isinstance(section, dict) or section.get("type") != "risks":
            continue
        items = section.get("items") if isinstance(section.get("items"), list) else []
        for item_index, item in enumerate(items):
            if isinstance(item, dict) and item.get("kind") == "open_question":
                title = item.get("title") if isinstance(item.get("title"), str) else ""
                _add_finding(findings, seen, "open_question", f"/sections/{section_index}/items/{item_index}", title)
    fragments = evidence.get("fragments") if isinstance(evidence, dict) and isinstance(evidence.get("fragments"), list) else []
    for fragment in fragments:
        if isinstance(fragment, dict) and fragment.get("flagged") is True:
            ref = fragment.get("id") if isinstance(fragment.get("id"), str) else ""
            anchor = fragment.get("anchor") if isinstance(fragment.get("anchor"), str) else ""
            _add_finding(findings, seen, "flagged_content", ref, anchor)
    return findings


def _check_values(session_dir: Path, session, fact_state, reader):
    fact = fact_state.get("state")
    report = fact_state.get("report") if isinstance(fact_state.get("report"), dict) else {}
    claims = report.get("claims") if isinstance(report.get("claims"), list) else []
    verdicts = [claim.get("verdict") for claim in claims if isinstance(claim, dict)]
    if fact == "current":
        fact_check = "pass" if verdicts and all(verdict == "verified" for verdict in verdicts) else "partial"
    elif fact == "reconciled":
        fact_check = "partial" if any(verdict == "unverifiable" for verdict in verdicts) else "pass"
    elif fact == "stale":
        fact_check = "stale"
    else:
        fact_check = "skipped"
    corrections = fact_state.get("corrections") if fact == "reconciled" else 0
    if not isinstance(corrections, int) or isinstance(corrections, bool):
        corrections = 0
    reader_report = reader.get("report") if isinstance(reader.get("report"), dict) else {}
    grade = reader.get("grade") if isinstance(reader.get("grade"), dict) else None
    if reader.get("state") == "counted" and isinstance(grade, dict):
        terms = reader_report.get("undefined_terms") if isinstance(reader_report.get("undefined_terms"), list) else []
        hard = reader_report.get("hard_to_follow") if isinstance(reader_report.get("hard_to_follow"), list) else []
        if grade.get("correct") == grade.get("total") and not terms and not hard:
            reader_test = "pass"
        else:
            reader_test = "partial"
    elif reader.get("state") == "stale":
        reader_test = "stale"
    else:
        reader_test = "skipped"
    brief = _parse(session_dir / "brief.json")
    orchestrator = brief.get("orchestrator_model") if isinstance(brief, dict) else None
    present = isinstance(orchestrator, str) and orchestrator != ""
    fact_model = report.get("model") if isinstance(report.get("model"), str) else ""
    same_family = model_family(fact_model) == model_family(orchestrator if present else "")
    counts = fact_check in ("pass", "partial")
    independent = counts and present and not same_family
    if fact_check in ("skipped", "stale"):
        label = "not_checked"
    elif not present:
        label = "independence_unknown"
    elif same_family:
        label = "same_family"
    elif independent and corrections > 0:
        label = "independent_corrected"
    else:
        label = "independent"
    skipped = session.get("skipped") if isinstance(session.get("skipped"), dict) else {}
    return {
        "validate": _validate_status(session_dir),
        "fact_check": fact_check,
        "fact_check_corrections": corrections,
        "reader_test": reader_test,
        "independent": independent,
        "check_label": label,
        "skipped": {
            "fact_check": skipped.get("fact_check") if isinstance(skipped.get("fact_check"), str) else None,
            "reader_test": skipped.get("reader_test") if isinstance(skipped.get("reader_test"), str) else None,
        },
    }


def _models(session_dir: Path):
    fact = _parse(session_dir / "factcheck.json")
    reader = _parse(session_dir / "reader.json")
    brief = _parse(session_dir / "brief.json")
    def pick(document):
        if isinstance(document, dict) and isinstance(document.get("model"), str):
            return document["model"]
        return None
    orchestrator = None
    if isinstance(brief, dict) and isinstance(brief.get("orchestrator_model"), str) and brief.get("orchestrator_model"):
        orchestrator = brief["orchestrator_model"]
    return {"fact_checker": pick(fact), "reader": pick(reader), "orchestrator": orchestrator}


def compute_result(session_dir) -> dict:
    root = Path(session_dir)
    try:
        session = load_session(root)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None
    fact_state = factcheck_state(root)
    reader = reader_state(root)
    checks = _check_values(root, session, fact_state, reader)
    identity = session.get("identity") if isinstance(session.get("identity"), dict) else {}
    doc = identity.get("doc") if identity.get("source") == "autopilot" and identity.get("doc") in ("spec", "plan") else None
    urls = session.get("urls") if isinstance(session.get("urls"), dict) else {}
    html = root / "explain.html"
    md = root / "explain.md"
    export = session.get("export_dir") if isinstance(session.get("export_dir"), str) and session.get("export_dir") else None
    opened = session.get("tab_opened")
    if not isinstance(opened, bool):
        opened = None
    mode = session.get("mode")
    status_failed = checks["validate"] != "pass" or not html.is_file()
    if status_failed:
        status = "failed"
    elif (mode in ("standard", "deep") and checks["fact_check"] != "pass") or (
        mode == "deep" and checks["reader_test"] != "pass"
    ):
        status = "partial"
    else:
        status = "ok"
    levels = session.get("levels") if isinstance(session.get("levels"), list) else []
    default_level = session.get("default_level") if isinstance(session.get("default_level"), int) and not isinstance(session.get("default_level"), bool) else None
    round_no = session.get("round") if isinstance(session.get("round"), int) and not isinstance(session.get("round"), bool) else 0
    return {
        "explain_contract": EXPLAIN_CONTRACT,
        "version": __version__,
        "status": status,
        "kind": session.get("kind") if session.get("kind") in ("plan", "idea") else session.get("kind"),
        "doc": doc,
        "slug": session.get("slug") if isinstance(session.get("slug"), str) else "",
        "round": round_no,
        "mode": mode if mode in ("quick", "standard", "deep") else mode,
        "levels": levels,
        "default_level": default_level,
        "lang": session.get("lang") if isinstance(session.get("lang"), str) else "",
        "urls": {
            "local": urls.get("local") if isinstance(urls.get("local"), str) else None,
            "public": urls.get("public") if isinstance(urls.get("public"), str) else None,
        },
        "tab_opened": opened,
        "paths": {
            "session": str(root.resolve()),
            "html": str(html.resolve()) if html.is_file() else None,
            "md": str(md.resolve()) if md.is_file() else None,
            "export": export,
        },
        "checks": checks,
        "models": _models(root),
        "findings": _findings(root, session, fact_state),
        "generated_at": now_iso(),
    }


def write_result(session_dir) -> dict:
    root = Path(session_dir)
    document = compute_result(root)
    path = root / "result.json"
    try:
        write_json(path, document)
    except OSError as exc:
        raise ExplainError(3, f"unwritable directory: {exc}") from exc
    export = document["paths"]["export"]
    if isinstance(export, str) and export:
        dest = Path(export)
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest / "result.json")
        except OSError as exc:
            raise ExplainError(3, f"unwritable directory: {exc}") from exc
    return document


def set_tab(session_dir, opened: bool) -> dict:
    if not isinstance(opened, bool):
        raise ExplainError(1, "opened must be a boolean")
    root = Path(session_dir)
    try:
        session = load_session(root)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None
    session["tab_opened"] = opened
    save_session(root, session)
    return write_result(root)
