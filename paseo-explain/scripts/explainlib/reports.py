"""Fact-check and reader requests, report checks, and corrections."""

import shutil
from pathlib import Path

from explainlib import REPORT_SCHEMA
from explainlib.common import (
    ExplainError,
    canonical_sha256,
    current_hashes,
    load_session,
    now_iso,
    read_json,
    save_session,
    sha256_file,
    words,
    write_json,
)
from explainlib.repo import load_repomap
from explainlib.secretscan import find_secrets_in
from explainlib.validate import (
    claim_leaves,
    claim_units,
    resolve_pointer,
    validate_explain,
    validate_mermaid,
)

_VERDICTS = ("verified", "corrected", "unsupported", "unverifiable")
_PLAN_CHECK_KINDS = (
    "requirement_without_task",
    "task_without_requirement",
    "contradiction",
)
_SKIP_STEPS = {"factcheck": "fact_check", "reader": "reader_test"}
_LIST_COLUMNS = ("now", "next", "unchanged", "requirements", "items", "entries", "steps")
_GATE_NAMES = {"factcheck": "factcheck report", "reader": "reader report"}


def _error(path, message):
    return {"path": path, "message": message}


def _ptr(base, token):
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    if base == "":
        return "/" + escaped
    return base + "/" + escaped


def _unlink(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _read_object(path: Path, label: str):
    import json

    if not path.is_file():
        raise ExplainError(1, f"{label} is missing")
    try:
        data = read_json(path)
    except json.JSONDecodeError:
        raise ExplainError(1, f"{label} is not valid JSON") from None
    except UnicodeDecodeError:
        raise ExplainError(1, f"{label} is not valid UTF-8") from None
    except OSError as exc:
        raise ExplainError(3, f"cannot read {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExplainError(1, _error("", "expected an object"))
    return data


def _known_evidence(evidence) -> set:
    found = set()
    if not isinstance(evidence, dict):
        return found
    fragments = evidence.get("fragments")
    if not isinstance(fragments, list):
        return found
    for fragment in fragments:
        if isinstance(fragment, dict) and isinstance(fragment.get("id"), str):
            found.add(fragment["id"])
    return found


def _check_keys(obj, path, required, optional=()):
    errors = []
    allowed = set(required) | set(optional)
    for key in obj:
        if key not in allowed:
            errors.append(_error(_ptr(path, key), f"unknown key {key}"))
    for key in required:
        if key not in obj:
            errors.append(_error(_ptr(path, key), f"missing key {key}"))
    return errors


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _leaf_limits(pointer: str):
    parts = pointer.split("/")[1:]
    if not parts:
        return None
    if parts[0] == "lead":
        return ("words", 90)
    if parts[0] == "facts":
        return ("chars", 0, 40)
    if parts[0] == "hero" and len(parts) > 1 and parts[1] == "nodes":
        return ("words", 80)
    if parts[0] == "hero" and len(parts) > 1 and parts[1] == "steps":
        return ("words", 40)
    if parts[0] != "sections":
        return None
    if "entries" in parts:
        return ("chars", 1, 120) if parts[-1] == "path" else ("words", 40)
    if "steps" in parts:
        if parts[-1] == "title":
            return ("chars", 1, 80)
        if parts[-1] == "command":
            return ("chars", 1, 200)
        return ("words", 60)
    if "body" in parts:
        return ("words", 250)
    if "mermaid" in parts:
        return ("mermaid",)
    if "alt" in parts:
        return ("chars", 1, 400)
    if "requirements" in parts:
        return ("chars", 0, 120)
    if "why" in parts:
        return ("words", 80)
    if "title" in parts and "items" in parts:
        return ("chars", 0, 80)
    if any(name in parts for name in ("now", "next", "unchanged")):
        return ("words", 40)
    if "items" in parts and "text" in parts:
        return ("words", 80)
    return None


def _correction_errors(path, leaf, text):
    limits = _leaf_limits(leaf)
    if not isinstance(text, str):
        return [_error(path, "expected a string")]
    if limits is None:
        if text == "":
            return [_error(path, "correction must be a non-empty string")]
        return []
    kind = limits[0]
    if kind == "words":
        if text == "":
            return [_error(path, "leveled text must be a non-empty string")]
        if words(text) > limits[1]:
            return [_error(path, f"exceeds {limits[1]} words")]
        return []
    if kind == "chars":
        lo, hi = limits[1], limits[2]
        if len(text) < lo or len(text) > hi:
            return [_error(path, f"expected a string of {lo}..{hi} characters")]
        return []
    return [_error(path, message) for message in validate_mermaid(text)]


def _check_model(value, path):
    if not isinstance(value, str) or value == "" or len(value) > 80:
        return [_error(path, "expected a model string of 1..80 characters")]
    return []


def _check_summary(value, path):
    if not isinstance(value, str) or len(value) > 600:
        return [_error(path, "expected a string of at most 600 characters")]
    return []


def check_factcheck(report, request, frozen_explain, evidence) -> list:
    del frozen_explain
    if not isinstance(report, dict):
        return [_error("", "expected an object")]
    if not isinstance(request, dict):
        return [_error("", "fact-check request is missing")]
    errors = _check_keys(
        report,
        "",
        ("explain_report", "kind", "model", "explain_sha256", "claims", "summary"),
        ("plan_checks",),
    )
    if report.get("explain_report") != REPORT_SCHEMA or isinstance(report.get("explain_report"), bool):
        errors.append(_error("/explain_report", "expected 1"))
    if report.get("kind") != "factcheck":
        errors.append(_error("/kind", "expected factcheck"))
    if "model" in report:
        errors.extend(_check_model(report.get("model"), "/model"))
    if "summary" in report:
        errors.extend(_check_summary(report.get("summary"), "/summary"))
    expected_hash = request.get("explain_sha256")
    if "explain_sha256" in report and report.get("explain_sha256") != expected_hash:
        errors.append(_error("/explain_sha256", "does not match the request"))
    leaves = request.get("leaves") if isinstance(request.get("leaves"), list) else []
    leaf_set = set(leaves)
    claims = report.get("claims")
    covered = set()
    corrected = set()
    if "claims" in report:
        if not isinstance(claims, list):
            errors.append(_error("/claims", "expected a list"))
            claims = []
        elif len(claims) < 1 or len(claims) > 600:
            errors.append(_error("/claims", "expected 1..600 items"))
        known = _known_evidence(evidence)
        for index, claim in enumerate(claims):
            claim_path = f"/claims/{index}"
            if not isinstance(claim, dict):
                errors.append(_error(claim_path, "expected an object"))
                continue
            errors.extend(
                _check_keys(claim, claim_path, ("ref", "claim", "verdict", "evidence", "correction"))
            )
            ref = claim.get("ref")
            if "ref" in claim:
                if not isinstance(ref, str) or ref not in leaf_set:
                    errors.append(_error(_ptr(claim_path, "ref"), "ref is not a leaf"))
                else:
                    covered.add(ref)
            if "claim" in claim:
                text = claim.get("claim")
                if not isinstance(text, str) or len(text) > 300:
                    errors.append(_error(_ptr(claim_path, "claim"), "expected a string of at most 300 characters"))
            verdict = claim.get("verdict")
            if "verdict" in claim and verdict not in _VERDICTS:
                errors.append(_error(_ptr(claim_path, "verdict"), "verdict is not allowed"))
            if "evidence" in claim:
                ev = claim.get("evidence")
                ev_path = _ptr(claim_path, "evidence")
                if not isinstance(ev, list):
                    errors.append(_error(ev_path, "expected a list"))
                else:
                    for ev_index, ev_id in enumerate(ev):
                        if not isinstance(ev_id, str):
                            errors.append(_error(_ptr(ev_path, ev_index), "expected a string"))
                        elif ev_id not in known:
                            errors.append(_error(_ptr(ev_path, ev_index), f"unknown evidence id {ev_id}"))
            if "correction" in claim and verdict in _VERDICTS:
                correction = claim.get("correction")
                corr_path = _ptr(claim_path, "correction")
                if verdict == "corrected":
                    if correction is None:
                        errors.append(_error(corr_path, "correction is required"))
                    elif isinstance(ref, str) and ref in corrected:
                        errors.append(_error(claim_path, "duplicate correction"))
                    else:
                        if isinstance(ref, str):
                            corrected.add(ref)
                        errors.extend(_correction_errors(corr_path, ref if isinstance(ref, str) else "", correction))
                elif correction is not None:
                    errors.append(_error(corr_path, "correction must be null"))
    if isinstance(claims, list):
        for leaf in leaves:
            if leaf not in covered:
                errors.append(_error(leaf if isinstance(leaf, str) else "", "uncovered leaf"))
    plan_checks = report.get("plan_checks", [])
    if "plan_checks" in report or plan_checks:
        if not isinstance(plan_checks, list):
            errors.append(_error("/plan_checks", "expected a list"))
        elif len(plan_checks) > 50:
            errors.append(_error("/plan_checks", "expected at most 50 items"))
        else:
            for index, item in enumerate(plan_checks):
                item_path = f"/plan_checks/{index}"
                if not isinstance(item, dict):
                    errors.append(_error(item_path, "expected an object"))
                    continue
                errors.extend(_check_keys(item, item_path, ("kind", "ref", "text")))
                if "kind" in item and item.get("kind") not in _PLAN_CHECK_KINDS:
                    errors.append(_error(_ptr(item_path, "kind"), "kind is not allowed"))
                if "ref" in item and (not isinstance(item.get("ref"), str) or item.get("ref") == "" or len(item.get("ref")) > 80):
                    errors.append(_error(_ptr(item_path, "ref"), "expected a string of 1..80 characters"))
                if "text" in item and (not isinstance(item.get("text"), str) or len(item.get("text")) > 300):
                    errors.append(_error(_ptr(item_path, "text"), "expected a string of at most 300 characters"))
    return errors


def check_reader(report, request, explain) -> list:
    if not isinstance(report, dict):
        return [_error("", "expected an object")]
    if not isinstance(request, dict):
        return [_error("", "reader request is missing")]
    errors = _check_keys(
        report,
        "",
        (
            "explain_report",
            "kind",
            "model",
            "explain_sha256",
            "level",
            "answers",
            "undefined_terms",
            "hard_to_follow",
            "summary",
        ),
    )
    if report.get("explain_report") != REPORT_SCHEMA or isinstance(report.get("explain_report"), bool):
        errors.append(_error("/explain_report", "expected 1"))
    if report.get("kind") != "reader":
        errors.append(_error("/kind", "expected reader"))
    if "model" in report:
        errors.extend(_check_model(report.get("model"), "/model"))
    if "summary" in report:
        errors.extend(_check_summary(report.get("summary"), "/summary"))
    if "explain_sha256" in report and report.get("explain_sha256") != request.get("explain_sha256"):
        errors.append(_error("/explain_sha256", "does not match the request"))
    level = report.get("level")
    if "level" in report:
        if isinstance(level, bool) or not isinstance(level, int):
            errors.append(_error("/level", "expected an integer"))
        elif level != request.get("level"):
            errors.append(_error("/level", "does not match the request"))
    questions = request.get("questions") if isinstance(request.get("questions"), list) else []
    answers = report.get("answers")
    if "answers" in report:
        if not isinstance(answers, list):
            errors.append(_error("/answers", "expected a list"))
        elif len(answers) != len(questions):
            errors.append(_error("/answers", "expected one answer per question"))
        else:
            seen = set()
            for index, answer in enumerate(answers):
                answer_path = f"/answers/{index}"
                if not isinstance(answer, dict):
                    errors.append(_error(answer_path, "expected an object"))
                    continue
                errors.extend(_check_keys(answer, answer_path, ("q", "answer")))
                q = answer.get("q")
                if "q" in answer:
                    if not _is_int(q):
                        errors.append(_error(_ptr(answer_path, "q"), "expected an integer"))
                    elif q in seen or q < 0 or q >= len(questions):
                        errors.append(_error(_ptr(answer_path, "q"), "duplicate or unknown question index"))
                    else:
                        seen.add(q)
                if "answer" in answer:
                    text = answer.get("answer")
                    if not isinstance(text, str) or len(text) > 600:
                        errors.append(_error(_ptr(answer_path, "answer"), "expected a string of at most 600 characters"))
            for q in range(len(questions)):
                if q not in seen and len(answers) == len(questions):
                    errors.append(_error("/answers", f"missing answer for question {q}"))
    terms = report.get("undefined_terms")
    if "undefined_terms" in report:
        if not isinstance(terms, list):
            errors.append(_error("/undefined_terms", "expected a list"))
        elif len(terms) > 30:
            errors.append(_error("/undefined_terms", "expected at most 30 items"))
        else:
            for index, term in enumerate(terms):
                if not isinstance(term, str) or len(term) > 60:
                    errors.append(_error(f"/undefined_terms/{index}", "expected a string of at most 60 characters"))
    hard = report.get("hard_to_follow")
    if "hard_to_follow" in report:
        if not isinstance(hard, list):
            errors.append(_error("/hard_to_follow", "expected a list"))
        else:
            for index, item in enumerate(hard):
                item_path = f"/hard_to_follow/{index}"
                if not isinstance(item, dict):
                    errors.append(_error(item_path, "expected an object"))
                    continue
                errors.extend(_check_keys(item, item_path, ("ref", "why")))
                ref = item.get("ref")
                if "ref" in item:
                    ok, _value = resolve_pointer(explain if isinstance(explain, dict) else {}, ref)
                    if not isinstance(ref, str) or not ref.startswith("/") or not ok:
                        errors.append(_error(_ptr(item_path, "ref"), "does not resolve"))
                if "why" in item:
                    why = item.get("why")
                    if not isinstance(why, str) or len(why) > 300:
                        errors.append(_error(_ptr(item_path, "why"), "expected a string of at most 300 characters"))
    return errors


def counts(kind, report, request) -> dict:
    report = report if isinstance(report, dict) else {}
    request = request if isinstance(request, dict) else {}
    if kind == "factcheck":
        totals = {name: 0 for name in _VERDICTS}
        covered = set()
        claims = report.get("claims") if isinstance(report.get("claims"), list) else []
        leaves = request.get("leaves") if isinstance(request.get("leaves"), list) else []
        leaf_set = set(leaves)
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            verdict = claim.get("verdict")
            if verdict in totals:
                totals[verdict] += 1
            ref = claim.get("ref")
            if isinstance(ref, str) and ref in leaf_set:
                covered.add(ref)
        totals["leaves"] = len(leaves)
        totals["covered"] = len(covered)
        return totals
    answers = report.get("answers") if isinstance(report.get("answers"), list) else []
    terms = report.get("undefined_terms") if isinstance(report.get("undefined_terms"), list) else []
    hard = report.get("hard_to_follow") if isinstance(report.get("hard_to_follow"), list) else []
    return {
        "answers": len(answers),
        "undefined_terms": len(terms),
        "hard_to_follow": len(hard),
    }


def _validation_problem(session_dir: Path):
    path = session_dir / "validate.json"
    if not path.is_file():
        return "validate.json is missing"
    try:
        document = read_json(path)
    except (OSError, UnicodeDecodeError, ValueError):
        return "validate.json is not valid JSON"
    if not isinstance(document, dict) or document.get("ok") is not True:
        return "validation did not pass"
    current = current_hashes(session_dir)
    for key in ("explain_sha256", "evidence_sha256", "brief_sha256"):
        if document.get(key) != current[key]:
            return "validation is not current"
    return None


def _require_current_validation(session_dir: Path) -> None:
    problem = _validation_problem(session_dir)
    if problem:
        raise ExplainError(1, problem)


def prepare(session_dir, kind: str) -> dict:
    if kind not in _SKIP_STEPS:
        raise ExplainError(1, "kind must be factcheck or reader")
    root = Path(session_dir)
    try:
        session = load_session(root)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None
    _require_current_validation(root)
    explain = _read_object(root / "explain.json", "explain.json")
    evidence = _read_object(root / "evidence.json", "evidence.json")
    brief = _read_object(root / "brief.json", "brief.json")
    hashes = current_hashes(root)
    checks = root / "checks"
    try:
        checks.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExplainError(3, f"unwritable directory: {exc}") from exc
    created = now_iso()
    if kind == "factcheck":
        units = claim_units(explain)
        leaves = [leaf for _unit, leaf in claim_leaves(explain)]
        request = {
            "explain_report": REPORT_SCHEMA,
            "kind": "factcheck",
            "explain_sha256": hashes["explain_sha256"],
            "evidence_sha256": hashes["evidence_sha256"],
            "brief_sha256": hashes["brief_sha256"],
            "units": units,
            "leaves": leaves,
            "created_at": created,
        }
        request_path = checks / "factcheck-request.json"
        frozen_path = checks / "factcheck-explain.json"
        try:
            write_json(request_path, request)
            shutil.copyfile(root / "explain.json", frozen_path)
        except OSError as exc:
            raise ExplainError(3, f"unwritable directory: {exc}") from exc
        _unlink(root / "factcheck.json")
        _unlink(checks / "applied.json")
        return {
            "ok": True,
            "request": str(request_path.resolve()),
            "input": str(frozen_path.resolve()),
            "units": len(units),
        }
    questions = brief.get("check_questions")
    if not isinstance(questions, list):
        questions = []
    minimum = 0 if session.get("mode") == "standard" else 1
    if not minimum <= len(questions) <= 5:
        raise ExplainError(1, "reader check requires 1 to 5 check questions")
    texts = []
    for item in questions:
        if not isinstance(item, dict) or not isinstance(item.get("q"), str):
            raise ExplainError(1, "reader check requires 1 to 5 check questions")
        texts.append(item["q"])
    request = {
        "explain_report": REPORT_SCHEMA,
        "kind": "reader",
        "explain_sha256": hashes["explain_sha256"],
        "questions_sha256": canonical_sha256(questions),
        "level": session.get("default_level"),
        "questions": texts,
        "created_at": created,
    }
    request_path = checks / "reader-request.json"
    reader_md = checks / "reader.md"
    from explainlib.render import render_markdown

    meta = {
        "check_label": "not_checked",
        "mode": session.get("mode"),
        "models": {},
        "checks": {},
        "skipped": session.get("skipped") if isinstance(session.get("skipped"), dict) else {},
    }
    markdown = render_markdown(explain, evidence, meta, session.get("default_level"), for_reader=True)
    try:
        write_json(request_path, request)
        reader_md.write_text(markdown, encoding="utf-8")
    except OSError as exc:
        raise ExplainError(3, f"unwritable directory: {exc}") from exc
    _unlink(root / "reader.json")
    session["reader_grade"] = None
    save_session(root, session)
    return {
        "ok": True,
        "request": str(request_path.resolve()),
        "input": str(reader_md.resolve()),
        "units": len(texts),
    }


def _load_report_file(path: Path):
    import json

    if not path.is_file():
        raise ExplainError(1, f"{path.name} is missing")
    try:
        return read_json(path)
    except json.JSONDecodeError:
        raise ExplainError(1, f"{path.name} is not valid JSON") from None
    except UnicodeDecodeError:
        raise ExplainError(1, f"{path.name} is not valid UTF-8") from None
    except OSError as exc:
        raise ExplainError(3, f"cannot read {path.name}: {exc}") from exc


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def check_report(session_dir, kind: str, file: str | None) -> dict:
    if kind not in _SKIP_STEPS:
        raise ExplainError(1, "kind must be factcheck or reader")
    root = Path(session_dir)
    try:
        session = load_session(root)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None
    dest = root / f"{kind}.json"
    source = Path(file) if file else dest
    report = _load_report_file(source)
    if session.get("kind") == "codebase" and find_secrets_in(report):
        if _inside(root, source):
            _unlink(source)
        raise ExplainError(1, f"{_GATE_NAMES[kind]} contains a possible secret")
    if kind == "factcheck":
        request = _load_report_file(root / "checks" / "factcheck-request.json")
        frozen = _load_report_file(root / "checks" / "factcheck-explain.json")
        evidence = _load_report_file(root / "evidence.json")
        errors = check_factcheck(report, request, frozen, evidence)
    else:
        request = _load_report_file(root / "checks" / "reader-request.json")
        explain_path = root / "explain.json"
        explain = _load_report_file(explain_path) if explain_path.is_file() else {}
        errors = check_reader(report, request, explain)
    if errors:
        raise ExplainError(1, errors)
    if source.resolve() != dest.resolve():
        try:
            write_json(dest, report)
        except OSError as exc:
            raise ExplainError(3, f"unwritable directory: {exc}") from exc
    step = _SKIP_STEPS[kind]
    skipped = session.get("skipped")
    if not isinstance(skipped, dict):
        skipped = {"fact_check": None, "reader_test": None}
    skipped[step] = None
    session["skipped"] = skipped
    save_session(root, session)
    return {"ok": True, "kind": kind, "counts": counts(kind, report, request)}


def _is_list_item(pointer: str) -> bool:
    parts = pointer.split("/")[1:]
    if len(parts) == 2 and parts[0] == "facts" and parts[1].isdigit():
        return True
    if (
        len(parts) == 4
        and parts[0] == "sections"
        and parts[1].isdigit()
        and parts[2] in _LIST_COLUMNS
        and parts[3].isdigit()
    ):
        return True
    return False


def _split_index(pointer: str):
    parts = pointer.split("/")[1:]
    parent = "/" + "/".join(parts[:-1]) if len(parts) > 1 else ""
    return parent, int(parts[-1])


def _set_pointer(obj, pointer: str, value) -> None:
    parts = pointer.split("/")[1:]
    current = obj
    for part in parts[:-1]:
        token = part.replace("~1", "/").replace("~0", "~")
        current = current[int(token)] if isinstance(current, list) else current[token]
    token = parts[-1].replace("~1", "/").replace("~0", "~")
    if isinstance(current, list):
        current[int(token)] = value
    else:
        current[token] = value


def _delete_pointer(obj, pointer: str) -> None:
    parent, index = _split_index(pointer)
    ok, container = resolve_pointer(obj, parent)
    if ok and isinstance(container, list) and index < len(container):
        del container[index]


def _hashes_match(request, current) -> bool:
    if not isinstance(request, dict):
        return False
    return (
        request.get("explain_sha256") == current.get("explain_sha256")
        and request.get("evidence_sha256") == current.get("evidence_sha256")
        and request.get("brief_sha256") == current.get("brief_sha256")
    )


def apply_corrections(session_dir, remove: list) -> dict:
    root = Path(session_dir)
    try:
        load_session(root)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None
    applied_path = root / "checks" / "applied.json"
    if applied_path.is_file():
        raise ExplainError(1, "corrections already applied")
    request = _load_report_file(root / "checks" / "factcheck-request.json")
    report = _load_report_file(root / "factcheck.json")
    frozen = _load_report_file(root / "checks" / "factcheck-explain.json")
    evidence = _load_report_file(root / "evidence.json")
    explain = _load_report_file(root / "explain.json")
    brief = _load_report_file(root / "brief.json")
    session = load_session(root)
    current = current_hashes(root)
    if not _hashes_match(request, current):
        raise ExplainError(1, "fact-check request does not match the current hashes")
    errors = check_factcheck(report, request, frozen, evidence)
    if errors:
        raise ExplainError(1, errors)
    claims = report.get("claims") if isinstance(report.get("claims"), list) else []
    leaf_units = {leaf: unit for unit, leaf in claim_leaves(explain if isinstance(explain, dict) else {})}
    unsupported = []
    corrected = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        ref = claim.get("ref")
        if claim.get("verdict") == "corrected" and isinstance(ref, str):
            corrected.append(ref)
        elif claim.get("verdict") == "unsupported" and isinstance(ref, str):
            unit = leaf_units.get(ref)
            if unit and unit not in unsupported:
                unsupported.append(unit)
    remove = list(remove or [])
    remove_errors = []
    seen_remove = set()
    for unit in remove:
        if unit in seen_remove:
            remove_errors.append(_error(unit, "duplicate remove target"))
        seen_remove.add(unit)
        if unit not in unsupported:
            remove_errors.append(_error(unit, "remove target has no unsupported claim"))
        elif not _is_list_item(unit):
            remove_errors.append(_error(unit, "remove target is not a list item"))
    if remove_errors:
        raise ExplainError(1, remove_errors)
    import copy

    updated = copy.deepcopy(explain)
    for claim in claims:
        if isinstance(claim, dict) and claim.get("verdict") == "corrected":
            _set_pointer(updated, claim["ref"], claim["correction"])
    removed = []
    grouped = {}
    order = []
    for unit in unsupported:
        if unit not in seen_remove:
            continue
        parent, index = _split_index(unit)
        if parent not in grouped:
            grouped[parent] = []
            order.append(parent)
        grouped[parent].append((index, unit))
    for parent in order:
        for _index, unit in sorted(grouped[parent], key=lambda item: item[0], reverse=True):
            removed.append(unit)
            _delete_pointer(updated, unit)
    relabelled = []
    for unit in unsupported:
        if unit in seen_remove:
            continue
        ok, target = resolve_pointer(updated, unit)
        if ok and isinstance(target, dict):
            target["confidence"] = "unknown"
            relabelled.append(unit)
    explain_errors, _warnings = validate_explain(
        updated,
        evidence if isinstance(evidence, dict) else {},
        session,
        brief if isinstance(brief, dict) else {},
        repomap=load_repomap(root) if session.get("kind") == "codebase" else None,
    )
    if explain_errors:
        raise ExplainError(1, explain_errors)
    result_hash = canonical_sha256(updated)
    record = {
        "explain_report": REPORT_SCHEMA,
        "request_explain_sha256": request.get("explain_sha256"),
        "result_explain_sha256": result_hash,
        "corrected": corrected,
        "relabelled": relabelled,
        "removed": removed,
        "applied_at": now_iso(),
    }
    try:
        write_json(root / "explain.json", updated)
        write_json(applied_path, record)
    except OSError as exc:
        raise ExplainError(3, f"unwritable directory: {exc}") from exc
    return {
        "ok": True,
        "corrected": len(corrected),
        "relabelled": len(relabelled),
        "removed": len(removed),
    }


def _parse_optional(path: Path):
    import json

    if not path.is_file():
        return None
    try:
        data = read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    return data


def _report_is_valid(kind, session_dir: Path) -> bool:
    root = Path(session_dir)
    report = _parse_optional(root / f"{kind}.json")
    request = _parse_optional(root / "checks" / f"{kind}-request.json")
    if not isinstance(report, dict) or not isinstance(request, dict):
        return False
    if kind == "factcheck":
        frozen = _parse_optional(root / "checks" / "factcheck-explain.json")
        evidence = _parse_optional(root / "evidence.json")
        return not check_factcheck(report, request, frozen, evidence if isinstance(evidence, dict) else {})
    explain = _parse_optional(root / "explain.json")
    return not check_reader(report, request, explain if isinstance(explain, dict) else {})


def factcheck_state(session_dir) -> dict:
    root = Path(session_dir)
    try:
        session = load_session(root)
    except (ExplainError, OSError, ValueError):
        session = {}
    report = _parse_optional(root / "factcheck.json")
    applied = _parse_optional(root / "checks" / "applied.json")
    if not isinstance(report, dict):
        report = None
    if not isinstance(applied, dict):
        applied = None
    empty = {"state": "none", "corrections": 0, "report": report, "applied": applied}
    if session.get("mode") == "quick" or not _report_is_valid("factcheck", root):
        return empty
    request = _parse_optional(root / "checks" / "factcheck-request.json")
    current = current_hashes(root)
    if (
        not isinstance(request, dict)
        or request.get("evidence_sha256") != current.get("evidence_sha256")
        or request.get("brief_sha256") != current.get("brief_sha256")
    ):
        return {"state": "stale", "corrections": 0, "report": report, "applied": applied}
    if current.get("explain_sha256") == request.get("explain_sha256"):
        return {"state": "current", "corrections": 0, "report": report, "applied": applied}
    if (
        isinstance(applied, dict)
        and applied.get("result_explain_sha256") == current.get("explain_sha256")
    ):
        corrected = applied.get("corrected") if isinstance(applied.get("corrected"), list) else []
        relabelled = applied.get("relabelled") if isinstance(applied.get("relabelled"), list) else []
        removed = applied.get("removed") if isinstance(applied.get("removed"), list) else []
        return {
            "state": "reconciled",
            "corrections": len(corrected) + len(relabelled) + len(removed),
            "report": report,
            "applied": applied,
        }
    return {"state": "stale", "corrections": 0, "report": report, "applied": applied}


def reader_state(session_dir) -> dict:
    root = Path(session_dir)
    try:
        session = load_session(root)
    except (ExplainError, OSError, ValueError):
        session = {}
    report = _parse_optional(root / "reader.json")
    if not isinstance(report, dict):
        report = None
    grade = session.get("reader_grade") if isinstance(session.get("reader_grade"), dict) else None
    empty = {"state": "none", "grade": grade, "report": report}
    if session.get("mode") == "quick" or not _report_is_valid("reader", root):
        return {"state": "none", "grade": grade, "report": report}
    request = _parse_optional(root / "checks" / "reader-request.json")
    brief = _parse_optional(root / "brief.json")
    questions = brief.get("check_questions") if isinstance(brief, dict) else None
    current_q = canonical_sha256(questions if isinstance(questions, list) else [])
    report_hash = None
    report_path = root / "reader.json"
    if report_path.is_file():
        try:
            report_hash = sha256_file(report_path)
        except OSError:
            report_hash = None
    grade_hash = grade.get("report_sha256") if isinstance(grade, dict) else None
    if (
        not isinstance(request, dict)
        or current_q != request.get("questions_sha256")
        or not isinstance(grade, dict)
        or grade_hash != report_hash
    ):
        return {"state": "stale", "grade": grade, "report": report}
    return {"state": "counted", "grade": grade, "report": report}


def skip(session_dir, step: str, reason: str) -> dict:
    if step not in ("fact_check", "reader_test"):
        raise ExplainError(1, "step must be fact_check or reader_test")
    if not isinstance(reason, str) or len(reason) > 200:
        raise ExplainError(1, "reason must be at most 200 characters")
    root = Path(session_dir)
    try:
        session = load_session(root)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None
    skipped = session.get("skipped")
    if not isinstance(skipped, dict):
        skipped = {"fact_check": None, "reader_test": None}
    skipped[step] = reason
    session["skipped"] = skipped
    save_session(root, session)
    return {"ok": True, "step": step, "reason": reason}


def grade(session_dir, correct: int) -> dict:
    if not _is_int(correct):
        raise ExplainError(1, "correct must be an integer")
    root = Path(session_dir)
    try:
        session = load_session(root)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None
    standard = session.get("mode") == "standard"
    brief_path = root / "brief.json"
    if brief_path.is_file():
        brief = _load_report_file(brief_path)
        questions = brief.get("check_questions") if isinstance(brief, dict) else None
    else:
        questions = None
    if not isinstance(questions, list):
        questions = []
    if not questions and not standard:
        raise ExplainError(1, "brief has no check questions")
    report_path = root / "reader.json"
    if not report_path.is_file():
        raise ExplainError(1, "reader report is missing")
    request_path = root / "checks" / "reader-request.json"
    if request_path.is_file():
        request = _load_report_file(request_path)
        report = _load_report_file(report_path)
        explain = _load_report_file(root / "explain.json") if (root / "explain.json").is_file() else {}
        errors = check_reader(report, request, explain if isinstance(explain, dict) else {})
        if errors:
            raise ExplainError(1, errors)
    total = len(questions)
    if correct < 0 or correct > total:
        raise ExplainError(1, "correct is out of range")
    digest = sha256_file(report_path)
    stored = {"correct": correct, "total": total, "report_sha256": digest}
    session["reader_grade"] = stored
    save_session(root, session)
    return {"ok": True, "correct": correct, "total": total, "report_sha256": digest}
