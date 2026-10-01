"""Validate explain.json and brief.json, and frame a session brief."""

import json
import re
from pathlib import Path

from explainlib import EXPLAIN_SCHEMA
from explainlib.repo import load_repomap
from explainlib.secretscan import find_secrets_in
from explainlib.common import (
    ExplainError,
    SLUG_RE,
    canonical_sha256,
    load_session,
    now_iso,
    read_json,
    sha256_file,
    words,
    write_json,
)

EXPLAIN_TOP_KEYS = frozenset(
    {
        "explain_schema",
        "kind",
        "lang",
        "title",
        "levels",
        "lead",
        "facts",
        "hero",
        "sections",
        "glossary",
    }
)

_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
_MERMAID_TYPES = (
    "flowchart",
    "graph",
    "sequenceDiagram",
    "stateDiagram-v2",
    "mindmap",
    "timeline",
)
_MERMAID_FORBIDDEN = ("<", "click ", "%%{", "javascript:", "href", "callback", "call ")
_NODE_DEF_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*[\[\(\{>]", re.MULTILINE)
_NODE_ARROW_RE = re.compile(r"-->\s*([A-Za-z][A-Za-z0-9_]*)")
_PLAN_CONFIDENCE = frozenset({"confirmed", "inferred", "unknown"})
_IDEA_CONFIDENCE = _PLAN_CONFIDENCE | frozenset({"user_statement", "assumption"})
_CODEBASE_CONFIDENCE = _PLAN_CONFIDENCE | frozenset({"documented"})
_SECTION_KINDS = {
    "plan": frozenset({"prose", "change", "diagram", "coverage", "decisions", "risks", "quiz"}),
    "idea": frozenset({"prose", "diagram", "decisions", "risks", "quiz"}),
    "codebase": frozenset({"prose", "diagram", "decisions", "risks", "quiz", "map", "start"}),
}
_MAX_EXPLAIN_BYTES = 200_000


def _error(path, message):
    return {"path": path, "message": message}


def _ptr(base, token):
    escaped = str(token).replace("~", "~0").replace("/", "~1")
    if base == "":
        return "/" + escaped
    return base + "/" + escaped


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


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


def _check_str(value, path, maximum, minimum=0):
    if not isinstance(value, str):
        return [_error(path, "expected a string")]
    if len(value) < minimum or len(value) > maximum:
        return [_error(path, f"expected a string of {minimum}..{maximum} characters")]
    return []


def _check_int(value, path, lo, hi):
    if not _is_int(value):
        return [_error(path, "expected an integer")]
    if value < lo or value > hi:
        return [_error(path, f"expected an integer from {lo} to {hi}")]
    return []


def _check_slug(value, path):
    if not isinstance(value, str) or SLUG_RE.fullmatch(value) is None:
        return [_error(path, "expected a slug")]
    return []


def _as_list(value, path, lo, hi):
    if not isinstance(value, list):
        return None, [_error(path, "expected a list")]
    errors = []
    if len(value) < lo or len(value) > hi:
        errors.append(_error(path, f"expected {lo}..{hi} items"))
    return value, errors


def _check_leveled(value, path, levels, max_words):
    expected = {str(level) for level in levels} if isinstance(levels, list) else set()
    if isinstance(value, str):
        if value == "":
            return [_error(path, "leveled text must be a non-empty string")]
        if words(value) > max_words:
            return [_error(path, f"exceeds {max_words} words")]
        return []
    if not isinstance(value, dict):
        return [_error(path, "leveled text must be a string or an object")]
    errors = []
    if set(value) != expected:
        errors.append(_error(path, "leveled text keys must equal the written levels"))
    for key, item in value.items():
        item_path = _ptr(path, key)
        if not isinstance(item, str) or item == "":
            errors.append(_error(item_path, "leveled text must be a non-empty string"))
        elif words(item) > max_words:
            errors.append(_error(item_path, f"exceeds {max_words} words"))
    return errors


class _Fragments(dict):
    """Fragment id -> text, plus the source role of each id and the session kind."""

    def __init__(self):
        super().__init__()
        self.roles = {}
        self.codebase = False


def _fragment_texts(evidence):
    if not isinstance(evidence, dict):
        return None
    fragments = evidence.get("fragments")
    texts = _Fragments()
    if not isinstance(fragments, list):
        return texts
    source_roles = {}
    sources = evidence.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict) and isinstance(source.get("id"), str):
                source_roles[source["id"]] = source.get("role")
    for fragment in fragments:
        if not isinstance(fragment, dict):
            continue
        frag_id = fragment.get("id")
        text = fragment.get("text")
        if isinstance(frag_id, str) and isinstance(text, str) and frag_id not in texts:
            texts[frag_id] = text.replace("\r\n", "\n")
            texts.roles[frag_id] = source_roles.get(fragment.get("source"))
    return texts


def _check_evidence(value, path, known, confidence):
    if not isinstance(value, list):
        return [_error(path, "expected a list")]
    errors = []
    for index, item in enumerate(value):
        item_path = _ptr(path, index)
        if not isinstance(item, str):
            errors.append(_error(item_path, "expected a string"))
        elif item not in known:
            errors.append(_error(item_path, f"unknown evidence id {item}"))
    if confidence in ("confirmed", "user_statement") and len(value) == 0 and not any(
        error["path"] == path and "expected a list" in error["message"] for error in errors
    ):
        errors.append(_error(path, f"{confidence} requires evidence"))
    if getattr(known, "codebase", False) and confidence in ("confirmed", "documented"):
        roles = {known.roles.get(item) for item in value if isinstance(item, str) and item in known}
        if confidence == "documented" and "doc" not in roles:
            errors.append(_error(path, "documented requires doc evidence"))
        elif confidence == "confirmed" and roles and not roles & {"code", "manifest"}:
            errors.append(_error(path, "confirmed needs code or manifest evidence; use documented"))
    return errors


def _check_confidence(value, path, kind):
    allowed = {"idea": _IDEA_CONFIDENCE, "codebase": _CODEBASE_CONFIDENCE}.get(kind, _PLAN_CONFIDENCE)
    if not isinstance(value, str) or value not in allowed:
        return [_error(path, "confidence is not allowed for this kind")]
    return []


def _check_quote(value, path, known):
    if not isinstance(value, dict):
        return [_error(path, "expected an object")]
    errors = _check_keys(value, path, ("evidence", "text"))
    evidence_id = value.get("evidence")
    text = value.get("text")
    if "evidence" in value:
        if not isinstance(evidence_id, str):
            errors.append(_error(_ptr(path, "evidence"), "expected a string"))
        elif evidence_id not in known:
            errors.append(_error(_ptr(path, "evidence"), f"unknown evidence id {evidence_id}"))
    if "text" in value:
        errors.extend(_check_str(text, _ptr(path, "text"), 300))
    if isinstance(evidence_id, str) and evidence_id in known and isinstance(text, str) and len(text) <= 300:
        fragment = known[evidence_id]
        if text.replace("\r\n", "\n") not in fragment:
            errors.append(_error(_ptr(path, "text"), "quote is not a verbatim substring of the evidence fragment"))
    return errors


def validate_mermaid(src) -> list[str]:
    if not isinstance(src, str):
        return ["expected a string"]
    messages = []
    if len(src) > 3000:
        messages.append("mermaid exceeds 3000 characters")
    nonempty = [line for line in src.splitlines() if line.strip()]
    if len(nonempty) > 40:
        messages.append("mermaid exceeds 40 non-empty lines")
    if not nonempty:
        messages.append("mermaid is empty")
    else:
        first = nonempty[0].lstrip()
        if not any(first.startswith(kind) for kind in _MERMAID_TYPES):
            messages.append("mermaid must start with a supported diagram type")
        elif first.startswith("flowchart") or first.startswith("graph"):
            identifiers = set(_NODE_DEF_RE.findall(src))
            identifiers.update(_NODE_ARROW_RE.findall(src))
            if len(identifiers) > 25:
                messages.append("mermaid has more than 25 nodes")
    lowered = src.lower()
    for token in _MERMAID_FORBIDDEN:
        if token.lower() in lowered:
            messages.append(f"mermaid contains forbidden token {token}")
    return messages


def _level_text(value, level):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        item = value.get(str(level))
        if isinstance(item, str):
            return item
    return ""


def _add_words(total, value, level):
    return total + words(_level_text(value, level))


def _words_at_level(explain, level):
    total = 0
    lead = explain.get("lead")
    if isinstance(lead, dict):
        total = _add_words(total, lead.get("text"), level)
    facts = explain.get("facts")
    if isinstance(facts, list):
        for fact in facts:
            if isinstance(fact, dict):
                for key in ("label", "value"):
                    if isinstance(fact.get(key), str):
                        total += words(fact[key])
    hero = explain.get("hero")
    if isinstance(hero, dict):
        total = _add_words(total, hero.get("caption"), level)
        for key in ("zones", "nodes", "edges", "steps"):
            items = hero.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                if key == "zones":
                    if isinstance(item.get("label"), str):
                        total += words(item["label"])
                elif key == "nodes":
                    for field in ("label", "sub"):
                        if isinstance(item.get(field), str):
                            total += words(item[field])
                    total = _add_words(total, item.get("detail"), level)
                elif key == "edges":
                    if isinstance(item.get("label"), str):
                        total += words(item["label"])
                else:
                    total = _add_words(total, item.get("text"), level)
    sections = explain.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if not isinstance(section, dict):
                continue
            if isinstance(section.get("title"), str):
                total += words(section["title"])
            total = _add_words(total, section.get("subtitle"), level)
            kind = section.get("type")
            if kind == "prose":
                total = _add_words(total, section.get("body"), level)
            elif kind == "change":
                for column in ("now", "next", "unchanged"):
                    items = section.get(column)
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                total = _add_words(total, item.get("text"), level)
            elif kind == "diagram":
                total = _add_words(total, section.get("caption"), level)
                if isinstance(section.get("alt"), str):
                    total += words(section["alt"])
            elif kind == "coverage":
                for req in section.get("requirements") or []:
                    if isinstance(req, dict) and isinstance(req.get("text"), str):
                        total += words(req["text"])
                for task in section.get("tasks") or []:
                    if isinstance(task, dict) and isinstance(task.get("label"), str):
                        total += words(task["label"])
            elif kind == "decisions":
                for item in section.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    if isinstance(item.get("title"), str):
                        total += words(item["title"])
                    total = _add_words(total, item.get("why"), level)
            elif kind == "risks":
                for item in section.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    if isinstance(item.get("title"), str):
                        total += words(item["title"])
                    total = _add_words(total, item.get("text"), level)
            elif kind == "map":
                for item in section.get("entries") or []:
                    if isinstance(item, dict):
                        total = _add_words(total, item.get("role"), level)
            elif kind == "start":
                for item in section.get("steps") or []:
                    if not isinstance(item, dict):
                        continue
                    if isinstance(item.get("title"), str):
                        total += words(item["title"])
                    total = _add_words(total, item.get("text"), level)
            elif kind == "quiz":
                for item in section.get("items") or []:
                    if not isinstance(item, dict):
                        continue
                    total = _add_words(total, item.get("q"), level)
                    total = _add_words(total, item.get("a"), level)
    glossary = explain.get("glossary")
    if isinstance(glossary, list):
        for item in glossary:
            if isinstance(item, dict):
                for key in ("term", "definition"):
                    if isinstance(item.get(key), str):
                        total += words(item[key])
    return total


def validate_brief(brief) -> list[dict]:
    if not isinstance(brief, dict):
        return [_error("", "brief.json must be an object")]
    errors = _check_keys(
        brief,
        "",
        ("explain_schema", "audience", "question", "out_of_scope", "check_questions"),
        ("orchestrator_model",),
    )
    if "explain_schema" in brief and not (
        _is_int(brief.get("explain_schema")) and brief.get("explain_schema") == EXPLAIN_SCHEMA
    ):
        errors.append(_error("/explain_schema", "explain_schema must be 1"))
    if "audience" in brief:
        errors.extend(_check_str(brief.get("audience"), "/audience", 120, 1))
    if "question" in brief:
        errors.extend(_check_str(brief.get("question"), "/question", 240, 1))
    if "out_of_scope" in brief:
        items, list_errors = _as_list(brief.get("out_of_scope"), "/out_of_scope", 0, 10)
        errors.extend(list_errors)
        if items is not None:
            for index, item in enumerate(items):
                errors.extend(_check_str(item, _ptr("/out_of_scope", index), 160))
    if "check_questions" in brief:
        questions, question_errors = _as_list(brief.get("check_questions"), "/check_questions", 0, 5)
        errors.extend(question_errors)
        if questions is not None:
            for index, item in enumerate(questions):
                item_path = _ptr("/check_questions", index)
                if not isinstance(item, dict):
                    errors.append(_error(item_path, "expected an object"))
                    continue
                errors.extend(_check_keys(item, item_path, ("q", "expected")))
                if "q" in item:
                    errors.extend(_check_str(item.get("q"), _ptr(item_path, "q"), 240))
                if "expected" in item:
                    errors.extend(_check_str(item.get("expected"), _ptr(item_path, "expected"), 400))
    if "orchestrator_model" in brief:
        errors.extend(_check_str(brief.get("orchestrator_model"), "/orchestrator_model", 80))
    return errors


def _validate_change_item(item, path, kind, levels, known):
    if not isinstance(item, dict):
        return [_error(path, "expected an object")]
    errors = _check_keys(item, path, ("text", "evidence", "confidence"), ("quote",))
    if "text" in item:
        errors.extend(_check_leveled(item.get("text"), _ptr(path, "text"), levels, 40))
    confidence = item.get("confidence")
    if "confidence" in item:
        errors.extend(_check_confidence(confidence, _ptr(path, "confidence"), kind))
    if "evidence" in item:
        errors.extend(
            _check_evidence(
                item.get("evidence"),
                _ptr(path, "evidence"),
                known,
                confidence if isinstance(confidence, str) else None,
            )
        )
    if "quote" in item:
        errors.extend(_check_quote(item.get("quote"), _ptr(path, "quote"), known))
    return errors


def _map_path_ok(value, repo_files):
    if not isinstance(value, str) or value == "" or value.startswith("/"):
        return False
    if ".." in value.split("/"):
        return False
    name = value.rstrip("/")
    if name == "":
        return False
    prefix = name + "/"
    if value.endswith("/"):
        return any(item.startswith(prefix) for item in repo_files)
    return name in repo_files or any(item.startswith(prefix) for item in repo_files)


def _validate_map(section, path, levels, known, kind, repo_files):
    errors = []
    entries, list_errors = _as_list(section.get("entries"), _ptr(path, "entries"), 1, 24)
    errors.extend(list_errors)
    seen = set()
    for index, entry in enumerate(entries or []):
        entry_path = _ptr(_ptr(path, "entries"), index)
        if not isinstance(entry, dict):
            errors.append(_error(entry_path, "expected an object"))
            continue
        errors.extend(_check_keys(entry, entry_path, ("path", "role", "evidence", "confidence")))
        if "path" in entry:
            value = entry.get("path")
            path_path = _ptr(entry_path, "path")
            errors.extend(_check_str(value, path_path, 120, 1))
            if isinstance(value, str) and 1 <= len(value) <= 120:
                if repo_files is not None and not _map_path_ok(value, repo_files):
                    errors.append(_error(path_path, "path is not a listed file or directory of the repository"))
                elif repo_files is None and (value.startswith("/") or ".." in value.split("/")):
                    errors.append(_error(path_path, "path is not a listed file or directory of the repository"))
                key = value.rstrip("/")
                if key in seen:
                    errors.append(_error(path_path, "duplicate path"))
                seen.add(key)
        if "role" in entry:
            errors.extend(_check_leveled(entry.get("role"), _ptr(entry_path, "role"), levels, 40))
        confidence = entry.get("confidence")
        if "confidence" in entry:
            errors.extend(_check_confidence(confidence, _ptr(entry_path, "confidence"), kind))
        if "evidence" in entry:
            errors.extend(
                _check_evidence(
                    entry.get("evidence"),
                    _ptr(entry_path, "evidence"),
                    known,
                    confidence if isinstance(confidence, str) else None,
                )
            )
    return errors


def _validate_start(section, path, levels, known, kind):
    errors = []
    steps, list_errors = _as_list(section.get("steps"), _ptr(path, "steps"), 1, 8)
    errors.extend(list_errors)
    for index, step in enumerate(steps or []):
        step_path = _ptr(_ptr(path, "steps"), index)
        if not isinstance(step, dict):
            errors.append(_error(step_path, "expected an object"))
            continue
        errors.extend(_check_keys(step, step_path, ("title", "text", "evidence", "confidence"), ("command",)))
        if "title" in step:
            errors.extend(_check_str(step.get("title"), _ptr(step_path, "title"), 80, 1))
        if "text" in step:
            errors.extend(_check_leveled(step.get("text"), _ptr(step_path, "text"), levels, 60))
        confidence = step.get("confidence")
        if "confidence" in step:
            errors.extend(_check_confidence(confidence, _ptr(step_path, "confidence"), kind))
        evidence_ids = step.get("evidence")
        if "evidence" in step:
            errors.extend(
                _check_evidence(
                    evidence_ids,
                    _ptr(step_path, "evidence"),
                    known,
                    confidence if isinstance(confidence, str) else None,
                )
            )
        if "command" in step:
            command = step.get("command")
            command_path = _ptr(step_path, "command")
            command_errors = _check_str(command, command_path, 200, 1)
            errors.extend(command_errors)
            if not command_errors:
                needle = command.replace("\r\n", "\n")
                ids = evidence_ids if isinstance(evidence_ids, list) else []
                if not any(isinstance(item, str) and item in known and needle in known[item] for item in ids):
                    errors.append(_error(command_path, "command is not verbatim in its evidence"))
    return errors


def _validate_section(section, path, kind, levels, known, seen_ids, repo_files=None):
    if not isinstance(section, dict):
        return [_error(path, "expected an object")]
    section_type = section.get("type")
    allowed_types = _SECTION_KINDS.get(kind, frozenset())
    type_specific = {
        "prose": ("body",),
        "change": ("now", "next", "unchanged"),
        "diagram": ("mermaid", "caption", "alt"),
        "coverage": ("requirements", "tasks"),
        "decisions": ("items",),
        "risks": ("items",),
        "quiz": ("items",),
        "map": ("entries",),
        "start": ("steps",),
    }
    required = ["id", "type", "title", "evidence", "confidence"]
    optional = ["subtitle"]
    if isinstance(section_type, str) and section_type in type_specific:
        required.extend(type_specific[section_type])
    errors = _check_keys(section, path, required, optional)
    if "id" in section:
        errors.extend(_check_slug(section.get("id"), _ptr(path, "id")))
        if isinstance(section.get("id"), str):
            if section["id"] in seen_ids:
                errors.append(_error(_ptr(path, "id"), f"duplicate id {section['id']}"))
            else:
                seen_ids.add(section["id"])
    if "type" in section:
        if not isinstance(section_type, str) or section_type not in allowed_types:
            errors.append(_error(_ptr(path, "type"), "section type is not allowed for this kind"))
    if "title" in section:
        errors.extend(_check_str(section.get("title"), _ptr(path, "title"), 80))
    if "subtitle" in section:
        errors.extend(_check_leveled(section.get("subtitle"), _ptr(path, "subtitle"), levels, 40))
    confidence = section.get("confidence")
    if "confidence" in section:
        errors.extend(_check_confidence(confidence, _ptr(path, "confidence"), kind))
    if "evidence" in section:
        errors.extend(
            _check_evidence(
                section.get("evidence"),
                _ptr(path, "evidence"),
                known,
                confidence if isinstance(confidence, str) else None,
            )
        )
    if section_type == "prose" and "body" in section:
        errors.extend(_check_leveled(section.get("body"), _ptr(path, "body"), levels, 250))
    elif section_type == "change":
        total = 0
        present = 0
        for column in ("now", "next", "unchanged"):
            if column not in section:
                continue
            present += 1
            items, list_errors = _as_list(section.get(column), _ptr(path, column), 0, 6)
            errors.extend(list_errors)
            if items is None:
                continue
            total += len(items)
            for index, item in enumerate(items):
                errors.extend(_validate_change_item(item, _ptr(_ptr(path, column), index), kind, levels, known))
        if present == 3 and total < 1:
            errors.append(_error(path, "change section needs at least one item"))
    elif section_type == "diagram":
        if "mermaid" in section:
            mermaid = section.get("mermaid")
            mermaid_path = _ptr(path, "mermaid")
            if not isinstance(mermaid, str):
                errors.append(_error(mermaid_path, "expected a string"))
            else:
                for message in validate_mermaid(mermaid):
                    errors.append(_error(mermaid_path, message))
        if "caption" in section:
            errors.extend(_check_leveled(section.get("caption"), _ptr(path, "caption"), levels, 40))
        if "alt" in section:
            errors.extend(_check_str(section.get("alt"), _ptr(path, "alt"), 400, 1))
    elif section_type == "coverage":
        task_ids = set()
        tasks = None
        if "tasks" in section:
            tasks, list_errors = _as_list(section.get("tasks"), _ptr(path, "tasks"), 1, 40)
            errors.extend(list_errors)
            if tasks is not None:
                for index, task in enumerate(tasks):
                    task_path = _ptr(_ptr(path, "tasks"), index)
                    if not isinstance(task, dict):
                        errors.append(_error(task_path, "expected an object"))
                        continue
                    errors.extend(_check_keys(task, task_path, ("id", "label", "evidence")))
                    if "id" in task:
                        errors.extend(_check_str(task.get("id"), _ptr(task_path, "id"), 12, 1))
                        if isinstance(task.get("id"), str):
                            task_ids.add(task["id"])
                    if "label" in task:
                        errors.extend(_check_str(task.get("label"), _ptr(task_path, "label"), 60))
                    if "evidence" in task:
                        errors.extend(_check_evidence(task.get("evidence"), _ptr(task_path, "evidence"), known, None))
        if "requirements" in section:
            reqs, list_errors = _as_list(section.get("requirements"), _ptr(path, "requirements"), 1, 30)
            errors.extend(list_errors)
            if reqs is not None:
                for index, req in enumerate(reqs):
                    req_path = _ptr(_ptr(path, "requirements"), index)
                    if not isinstance(req, dict):
                        errors.append(_error(req_path, "expected an object"))
                        continue
                    errors.extend(_check_keys(req, req_path, ("id", "text", "tasks", "test", "evidence", "confidence")))
                    if "id" in req:
                        errors.extend(_check_str(req.get("id"), _ptr(req_path, "id"), 12, 1))
                    if "text" in req:
                        errors.extend(_check_str(req.get("text"), _ptr(req_path, "text"), 120))
                    if "test" in req and req.get("test") not in ("yes", "no", "unknown"):
                        errors.append(_error(_ptr(req_path, "test"), "test must be yes, no, or unknown"))
                    req_confidence = req.get("confidence")
                    if "confidence" in req:
                        errors.extend(_check_confidence(req_confidence, _ptr(req_path, "confidence"), kind))
                    if "evidence" in req:
                        errors.extend(
                            _check_evidence(
                                req.get("evidence"),
                                _ptr(req_path, "evidence"),
                                known,
                                req_confidence if isinstance(req_confidence, str) else None,
                            )
                        )
                    if "tasks" in req:
                        links = req.get("tasks")
                        if not isinstance(links, list):
                            errors.append(_error(_ptr(req_path, "tasks"), "expected a list"))
                        elif tasks is not None:
                            for link_index, link in enumerate(links):
                                link_path = _ptr(_ptr(req_path, "tasks"), link_index)
                                if not isinstance(link, str):
                                    errors.append(_error(link_path, "expected a string"))
                                elif link not in task_ids:
                                    errors.append(_error(link_path, f"unknown task id {link}"))
    elif section_type == "decisions" and "items" in section:
        items, list_errors = _as_list(section.get("items"), _ptr(path, "items"), 1, 6)
        errors.extend(list_errors)
        chosen = 0
        if items is not None:
            for index, item in enumerate(items):
                item_path = _ptr(_ptr(path, "items"), index)
                if not isinstance(item, dict):
                    errors.append(_error(item_path, "expected an object"))
                    continue
                errors.extend(_check_keys(item, item_path, ("status", "title", "why", "evidence", "confidence")))
                if "status" in item:
                    if item.get("status") == "chosen":
                        chosen += 1
                    elif item.get("status") != "rejected":
                        errors.append(_error(_ptr(item_path, "status"), "status must be chosen or rejected"))
                if "title" in item:
                    errors.extend(_check_str(item.get("title"), _ptr(item_path, "title"), 80))
                if "why" in item:
                    errors.extend(_check_leveled(item.get("why"), _ptr(item_path, "why"), levels, 80))
                item_confidence = item.get("confidence")
                if "confidence" in item:
                    errors.extend(_check_confidence(item_confidence, _ptr(item_path, "confidence"), kind))
                if "evidence" in item:
                    errors.extend(
                        _check_evidence(
                            item.get("evidence"),
                            _ptr(item_path, "evidence"),
                            known,
                            item_confidence if isinstance(item_confidence, str) else None,
                        )
                    )
            if items and chosen < 1:
                errors.append(_error(_ptr(path, "items"), "decisions need at least one chosen item"))
    elif section_type == "risks" and "items" in section:
        items, list_errors = _as_list(section.get("items"), _ptr(path, "items"), 1, 8)
        errors.extend(list_errors)
        if items is not None:
            for index, item in enumerate(items):
                item_path = _ptr(_ptr(path, "items"), index)
                if not isinstance(item, dict):
                    errors.append(_error(item_path, "expected an object"))
                    continue
                errors.extend(_check_keys(item, item_path, ("kind", "title", "text", "evidence", "confidence")))
                if "kind" in item and item.get("kind") not in ("risk", "open_question", "assumption"):
                    errors.append(_error(_ptr(item_path, "kind"), "kind must be risk, open_question, or assumption"))
                if "title" in item:
                    errors.extend(_check_str(item.get("title"), _ptr(item_path, "title"), 80))
                if "text" in item:
                    errors.extend(_check_leveled(item.get("text"), _ptr(item_path, "text"), levels, 80))
                item_confidence = item.get("confidence")
                if "confidence" in item:
                    errors.extend(_check_confidence(item_confidence, _ptr(item_path, "confidence"), kind))
                if "evidence" in item:
                    errors.extend(
                        _check_evidence(
                            item.get("evidence"),
                            _ptr(item_path, "evidence"),
                            known,
                            item_confidence if isinstance(item_confidence, str) else None,
                        )
                    )
    elif section_type == "map" and "entries" in section:
        errors.extend(_validate_map(section, path, levels, known, kind, repo_files))
    elif section_type == "start" and "steps" in section:
        errors.extend(_validate_start(section, path, levels, known, kind))
    elif section_type == "quiz" and "items" in section:
        items, list_errors = _as_list(section.get("items"), _ptr(path, "items"), 1, 4)
        errors.extend(list_errors)
        if items is not None:
            for index, item in enumerate(items):
                item_path = _ptr(_ptr(path, "items"), index)
                if not isinstance(item, dict):
                    errors.append(_error(item_path, "expected an object"))
                    continue
                errors.extend(_check_keys(item, item_path, ("q", "a")))
                if "q" in item:
                    errors.extend(_check_leveled(item.get("q"), _ptr(item_path, "q"), levels, 40))
                if "a" in item:
                    errors.extend(_check_leveled(item.get("a"), _ptr(item_path, "a"), levels, 80))
    return errors


def _strings_contain(obj, needle) -> bool:
    if isinstance(obj, str):
        return needle in obj
    if isinstance(obj, dict):
        return any(_strings_contain(key, needle) or _strings_contain(value, needle) for key, value in obj.items())
    if isinstance(obj, (list, tuple)):
        return any(_strings_contain(item, needle) for item in obj)
    return False


def secret_gate(explain, session) -> list[dict]:
    """Error list for codebase sessions when explain.json holds a possible secret."""
    if isinstance(session, dict) and session.get("kind") == "codebase" and find_secrets_in(explain):
        return [_error("", "explain.json contains a possible secret")]
    return []


def _repo_paths(repomap):
    files = repomap.get("files") if isinstance(repomap, dict) else None
    paths = set()
    if isinstance(files, list):
        for record in files:
            if isinstance(record, dict) and isinstance(record.get("path"), str) and record.get("kind") != "excluded":
                paths.add(record["path"])
    return paths


def _codebase_session_errors(session, evidence, repomap):
    errors = []
    if repomap is None:
        errors.append(_error("/", "repomap.json is missing; run ingest"))
    structured = evidence.get("structured") if isinstance(evidence, dict) else None
    depth = structured.get("depth") if isinstance(structured, dict) else None
    if depth != session.get("depth"):
        errors.append(_error("/", "evidence depth does not match session depth; run ingest"))
    return errors


def _hero_uses_new(hero) -> bool:
    if not isinstance(hero, dict):
        return False
    for key in ("zones", "nodes", "edges"):
        items = hero.get(key)
        if isinstance(items, list) and any(isinstance(item, dict) and item.get("tone") == "new" for item in items):
            return True
    return False


def validate_explain(explain, evidence, session, brief, repomap=None) -> tuple[list[dict], list[dict]]:
    """Return ``(errors, warnings)`` for an explanation document.

    ``brief`` is accepted for callers that already loaded ``brief.json``; brief
    rules themselves are ``validate_brief``. ``repomap`` is the parsed
    ``repomap.json``; codebase sessions require it.
    """
    del brief
    if not isinstance(explain, dict):
        return [_error("", "explain.json must be an object")], []
    errors = []
    warnings = []
    try:
        encoded = json.dumps(explain, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        encoded = b""
    if len(encoded) > _MAX_EXPLAIN_BYTES:
        errors.append(_error("", "explain.json exceeds 200000 bytes"))
    if not isinstance(session, dict):
        errors.append(_error("", "session.json must be an object"))
        session = {}
    known = _fragment_texts(evidence)
    if known is None:
        errors.append(_error("", "evidence.json must be an object"))
        known = _Fragments()
    is_codebase = session.get("kind") == "codebase"
    known.codebase = is_codebase
    repo_files = None
    if is_codebase:
        errors.extend(_codebase_session_errors(session, evidence, repomap))
        if repomap is not None:
            repo_files = _repo_paths(repomap)
        identity = session.get("identity")
        repo_root = identity.get("repo") if isinstance(identity, dict) else None
        if isinstance(repo_root, str) and len(repo_root) > 1 and _strings_contain(explain, repo_root):
            errors.append(_error("", "explanation contains the local repository path"))
    errors.extend(_check_keys(explain, "", tuple(sorted(EXPLAIN_TOP_KEYS))))
    levels = explain.get("levels")
    kind = explain.get("kind")
    if "explain_schema" in explain and (
        not _is_int(explain.get("explain_schema")) or explain.get("explain_schema") != EXPLAIN_SCHEMA
    ):
        errors.append(_error("/explain_schema", "explain_schema must be 1"))
    if "kind" in explain:
        if kind not in ("plan", "idea", "codebase"):
            errors.append(_error("/kind", "kind must be plan, idea, or codebase"))
        elif session.get("kind") != kind:
            errors.append(_error("/kind", "kind must equal session.json.kind"))
    if "lang" in explain:
        lang = explain.get("lang")
        if not isinstance(lang, str) or _LANG_RE.fullmatch(lang) is None:
            errors.append(_error("/lang", "lang must be a BCP 47-ish language code"))
    if "title" in explain:
        errors.extend(_check_str(explain.get("title"), "/title", 80, 1))
    written_levels = session.get("levels") if isinstance(session.get("levels"), list) else None
    if "levels" in explain:
        if not isinstance(levels, list) or any(not _is_int(item) for item in levels):
            errors.append(_error("/levels", "expected a list of integers"))
            if isinstance(levels, list):
                for index, item in enumerate(levels):
                    if not _is_int(item):
                        errors.append(_error(_ptr("/levels", index), "expected an integer"))
            levels = []
        elif levels != written_levels:
            errors.append(_error("/levels", "levels must equal session.json.levels"))
    if not isinstance(levels, list):
        levels = []
    if "lead" in explain:
        lead = explain.get("lead")
        lead_path = "/lead"
        if not isinstance(lead, dict):
            errors.append(_error(lead_path, "expected an object"))
        else:
            errors.extend(_check_keys(lead, lead_path, ("text", "evidence", "confidence")))
            if "text" in lead:
                errors.extend(_check_leveled(lead.get("text"), _ptr(lead_path, "text"), levels, 90))
            confidence = lead.get("confidence")
            if "confidence" in lead:
                errors.extend(_check_confidence(confidence, _ptr(lead_path, "confidence"), kind if isinstance(kind, str) else ""))
            if "evidence" in lead:
                errors.extend(
                    _check_evidence(
                        lead.get("evidence"),
                        _ptr(lead_path, "evidence"),
                        known,
                        confidence if isinstance(confidence, str) else None,
                    )
                )
    if "facts" in explain:
        facts, list_errors = _as_list(explain.get("facts"), "/facts", 0, 6)
        errors.extend(list_errors)
        if facts is not None:
            for index, fact in enumerate(facts):
                fact_path = _ptr("/facts", index)
                if not isinstance(fact, dict):
                    errors.append(_error(fact_path, "expected an object"))
                    continue
                errors.extend(_check_keys(fact, fact_path, ("label", "value", "evidence", "confidence")))
                if "label" in fact:
                    errors.extend(_check_str(fact.get("label"), _ptr(fact_path, "label"), 24))
                if "value" in fact:
                    errors.extend(_check_str(fact.get("value"), _ptr(fact_path, "value"), 40))
                confidence = fact.get("confidence")
                if "confidence" in fact:
                    errors.extend(_check_confidence(confidence, _ptr(fact_path, "confidence"), kind if isinstance(kind, str) else ""))
                if "evidence" in fact:
                    errors.extend(
                        _check_evidence(
                            fact.get("evidence"),
                            _ptr(fact_path, "evidence"),
                            known,
                            confidence if isinstance(confidence, str) else None,
                        )
                    )
    if "hero" in explain:
        errors.extend(_validate_hero(explain.get("hero"), kind if isinstance(kind, str) else "", levels, known))
    if "sections" in explain:
        sections, list_errors = _as_list(explain.get("sections"), "/sections", 3, 8)
        errors.extend(list_errors)
        seen_ids = set()
        if sections is not None:
            for index, section in enumerate(sections):
                errors.extend(
                    _validate_section(
                        section,
                        _ptr("/sections", index),
                        kind if isinstance(kind, str) else "",
                        levels,
                        known,
                        seen_ids,
                        repo_files,
                    )
                )
    if "glossary" in explain:
        glossary, list_errors = _as_list(explain.get("glossary"), "/glossary", 0, 20)
        errors.extend(list_errors)
        seen_terms = set()
        if glossary is not None:
            for index, item in enumerate(glossary):
                item_path = _ptr("/glossary", index)
                if not isinstance(item, dict):
                    errors.append(_error(item_path, "expected an object"))
                    continue
                errors.extend(_check_keys(item, item_path, ("term", "definition")))
                if "term" in item:
                    errors.extend(_check_str(item.get("term"), _ptr(item_path, "term"), 40))
                    if isinstance(item.get("term"), str):
                        folded = item["term"].casefold()
                        if folded in seen_terms:
                            errors.append(_error(_ptr(item_path, "term"), f"duplicate term {item['term']}"))
                        else:
                            seen_terms.add(folded)
                if "definition" in item:
                    errors.extend(_check_str(item.get("definition"), _ptr(item_path, "definition"), 240))
    new_nodes = 0
    hero = explain.get("hero")
    if isinstance(hero, dict) and isinstance(hero.get("nodes"), list):
        for node in hero["nodes"]:
            if isinstance(node, dict) and node.get("tone") == "new":
                new_nodes += 1
    if new_nodes > 2:
        warnings.append(_error("/hero/nodes", "more than 2 hero nodes have tone new"))
    if is_codebase and _hero_uses_new(hero):
        warnings.append(_error("/hero", "codebase hero uses tone new"))
    default_level = session.get("default_level")
    if _is_int(default_level) and _words_at_level(explain, default_level) > 1800:
        warnings.append(_error("", "total words at the default level exceed 1800"))
    return errors, warnings


def _validate_hero(hero, kind, levels, known):
    if not isinstance(hero, dict):
        return [_error("/hero", "expected an object")]
    errors = _check_keys(hero, "/hero", ("caption", "height", "zones", "nodes", "edges", "steps"))
    height = hero.get("height")
    if "height" in hero:
        errors.extend(_check_int(height, "/hero/height", 200, 600))
    if not _is_int(height):
        height = None
    if "caption" in hero:
        errors.extend(_check_leveled(hero.get("caption"), "/hero/caption", levels, 40))
    zone_ids = set()
    if "zones" in hero:
        zones, list_errors = _as_list(hero.get("zones"), "/hero/zones", 0, 4)
        errors.extend(list_errors)
        if zones is not None:
            for index, zone in enumerate(zones):
                zone_path = _ptr("/hero/zones", index)
                if not isinstance(zone, dict):
                    errors.append(_error(zone_path, "expected an object"))
                    continue
                errors.extend(_check_keys(zone, zone_path, ("id", "label", "tone", "x", "y", "w", "h")))
                if "id" in zone:
                    errors.extend(_check_slug(zone.get("id"), _ptr(zone_path, "id")))
                    if isinstance(zone.get("id"), str) and SLUG_RE.fullmatch(zone["id"]):
                        zone_ids.add(zone["id"])
                if "label" in zone:
                    errors.extend(_check_str(zone.get("label"), _ptr(zone_path, "label"), 32))
                if "tone" in zone and zone.get("tone") not in ("existing", "new", "external"):
                    errors.append(_error(_ptr(zone_path, "tone"), "tone must be existing, new, or external"))
                coords = {}
                for key in ("x", "y", "w", "h"):
                    if key in zone:
                        if not _is_int(zone.get(key)):
                            errors.append(_error(_ptr(zone_path, key), "expected an integer"))
                        else:
                            coords[key] = zone[key]
                if height is not None and all(key in coords for key in ("x", "y", "w", "h")):
                    if (
                        coords["x"] < 0
                        or coords["y"] < 0
                        or coords["w"] < 0
                        or coords["h"] < 0
                        or coords["x"] + coords["w"] > 1000
                        or coords["y"] + coords["h"] > height
                    ):
                        errors.append(_error(zone_path, "zone rectangle is outside the canvas"))
    node_ids = set()
    if "nodes" in hero:
        nodes, list_errors = _as_list(hero.get("nodes"), "/hero/nodes", 2, 9)
        errors.extend(list_errors)
        if nodes is not None:
            for index, node in enumerate(nodes):
                node_path = _ptr("/hero/nodes", index)
                if not isinstance(node, dict):
                    errors.append(_error(node_path, "expected an object"))
                    continue
                errors.extend(
                    _check_keys(
                        node,
                        node_path,
                        ("id", "label", "tone", "zone", "x", "y", "detail", "evidence", "confidence"),
                        ("sub", "w"),
                    )
                )
                if "id" in node:
                    errors.extend(_check_slug(node.get("id"), _ptr(node_path, "id")))
                    if isinstance(node.get("id"), str):
                        if node["id"] in node_ids:
                            errors.append(_error(_ptr(node_path, "id"), f"duplicate id {node['id']}"))
                        else:
                            node_ids.add(node["id"])
                if "label" in node:
                    errors.extend(_check_str(node.get("label"), _ptr(node_path, "label"), 28))
                if "sub" in node:
                    errors.extend(_check_str(node.get("sub"), _ptr(node_path, "sub"), 36))
                if "tone" in node and node.get("tone") not in ("existing", "new", "external"):
                    errors.append(_error(_ptr(node_path, "tone"), "tone must be existing, new, or external"))
                if "zone" in node:
                    zone = node.get("zone")
                    if zone is not None and (not isinstance(zone, str) or zone not in zone_ids):
                        errors.append(_error(_ptr(node_path, "zone"), "unknown zone"))
                if "x" in node:
                    errors.extend(_check_int(node.get("x"), _ptr(node_path, "x"), 0, 1000))
                if "y" in node and height is not None:
                    errors.extend(_check_int(node.get("y"), _ptr(node_path, "y"), 0, height))
                elif "y" in node and height is None:
                    if not _is_int(node.get("y")):
                        errors.append(_error(_ptr(node_path, "y"), "expected an integer"))
                if "w" in node:
                    errors.extend(_check_int(node.get("w"), _ptr(node_path, "w"), 120, 320))
                if "detail" in node:
                    errors.extend(_check_leveled(node.get("detail"), _ptr(node_path, "detail"), levels, 80))
                confidence = node.get("confidence")
                if "confidence" in node:
                    errors.extend(_check_confidence(confidence, _ptr(node_path, "confidence"), kind))
                if "evidence" in node:
                    errors.extend(
                        _check_evidence(
                            node.get("evidence"),
                            _ptr(node_path, "evidence"),
                            known,
                            confidence if isinstance(confidence, str) else None,
                        )
                    )
    if "edges" in hero:
        edges, list_errors = _as_list(hero.get("edges"), "/hero/edges", 0, 12)
        errors.extend(list_errors)
        if edges is not None:
            for index, edge in enumerate(edges):
                edge_path = _ptr("/hero/edges", index)
                if not isinstance(edge, dict):
                    errors.append(_error(edge_path, "expected an object"))
                    continue
                errors.extend(_check_keys(edge, edge_path, ("from", "to", "tone"), ("label",)))
                for end in ("from", "to"):
                    if end in edge:
                        ref = edge.get(end)
                        if not isinstance(ref, str) or ref not in node_ids:
                            errors.append(_error(_ptr(edge_path, end), "unknown node"))
                if isinstance(edge.get("from"), str) and edge.get("from") == edge.get("to"):
                    errors.append(_error(edge_path, "edge from and to must differ"))
                if "label" in edge:
                    errors.extend(_check_str(edge.get("label"), _ptr(edge_path, "label"), 20))
                if "tone" in edge and edge.get("tone") not in ("existing", "new"):
                    errors.append(_error(_ptr(edge_path, "tone"), "tone must be existing or new"))
    if "steps" in hero:
        steps, list_errors = _as_list(hero.get("steps"), "/hero/steps", 0, 8)
        errors.extend(list_errors)
        if steps is not None:
            for index, step in enumerate(steps):
                step_path = _ptr("/hero/steps", index)
                if not isinstance(step, dict):
                    errors.append(_error(step_path, "expected an object"))
                    continue
                errors.extend(_check_keys(step, step_path, ("nodes", "from", "to", "text", "evidence")))
                if "nodes" in step:
                    refs = step.get("nodes")
                    if not isinstance(refs, list):
                        errors.append(_error(_ptr(step_path, "nodes"), "expected a list"))
                    else:
                        if len(refs) < 1 or len(refs) > 4:
                            errors.append(_error(_ptr(step_path, "nodes"), "expected 1..4 items"))
                        for ref_index, ref in enumerate(refs):
                            ref_path = _ptr(_ptr(step_path, "nodes"), ref_index)
                            if not isinstance(ref, str) or ref not in node_ids:
                                errors.append(_error(ref_path, "unknown node"))
                from_set = "from" in step and step.get("from") is not None
                to_set = "to" in step and step.get("to") is not None
                if from_set != to_set:
                    errors.append(_error(step_path, "step from and to must both be set or both be null"))
                for end in ("from", "to"):
                    if end in step and step.get(end) is not None:
                        ref = step.get(end)
                        if not isinstance(ref, str) or ref not in node_ids:
                            errors.append(_error(_ptr(step_path, end), "unknown node"))
                if "text" in step:
                    errors.extend(_check_leveled(step.get("text"), _ptr(step_path, "text"), levels, 40))
                if "evidence" in step:
                    errors.extend(_check_evidence(step.get("evidence"), _ptr(step_path, "evidence"), known, None))
    return errors


def resolve_pointer(obj, pointer) -> tuple[bool, object]:
    if not isinstance(pointer, str):
        return False, None
    if pointer == "":
        return True, obj
    if not pointer.startswith("/"):
        return False, None
    current = obj
    for raw in pointer.split("/")[1:]:
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if token not in current:
                return False, None
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                return False, None
            index = int(token)
            if index >= len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def _level_leaves(unit, field, value):
    base = f"{unit}/{field}"
    if isinstance(value, str):
        return [(unit, base)]
    if not isinstance(value, dict):
        return []
    numeric = []
    other = []
    for key in value:
        if isinstance(key, str) and key.isdigit():
            numeric.append(int(key))
        else:
            other.append(str(key))
    numeric.sort()
    leaves = [(unit, f"{base}/{number}") for number in numeric]
    leaves.extend((unit, f"{base}/{key}") for key in other)
    return leaves


def _append_fields(leaves, unit, obj, fields):
    if not isinstance(obj, dict):
        return
    for field in fields:
        if field in obj:
            leaves.extend(_level_leaves(unit, field, obj[field]))


def claim_units(explain) -> list[str]:
    if not isinstance(explain, dict):
        return []
    units = ["/lead"]
    facts = explain.get("facts")
    if isinstance(facts, list):
        units.extend(f"/facts/{index}" for index in range(len(facts)))
    hero = explain.get("hero") if isinstance(explain.get("hero"), dict) else {}
    nodes = hero.get("nodes") if isinstance(hero.get("nodes"), list) else []
    steps = hero.get("steps") if isinstance(hero.get("steps"), list) else []
    units.extend(f"/hero/nodes/{index}" for index in range(len(nodes)))
    units.extend(f"/hero/steps/{index}" for index in range(len(steps)))
    sections = explain.get("sections") if isinstance(explain.get("sections"), list) else []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        section_type = section.get("type")
        base = f"/sections/{index}"
        if section_type in ("prose", "diagram"):
            units.append(base)
        elif section_type == "change":
            for column in ("now", "next", "unchanged"):
                items = section.get(column)
                if isinstance(items, list):
                    units.extend(f"{base}/{column}/{item_index}" for item_index in range(len(items)))
        elif section_type == "coverage":
            reqs = section.get("requirements")
            if isinstance(reqs, list):
                units.extend(f"{base}/requirements/{item_index}" for item_index in range(len(reqs)))
        elif section_type in ("decisions", "risks"):
            items = section.get("items")
            if isinstance(items, list):
                units.extend(f"{base}/items/{item_index}" for item_index in range(len(items)))
        elif section_type in ("map", "start"):
            column = "entries" if section_type == "map" else "steps"
            items = section.get(column)
            if isinstance(items, list):
                units.extend(f"{base}/{column}/{item_index}" for item_index in range(len(items)))
    return units


def claim_leaves(explain) -> list[tuple[str, str]]:
    if not isinstance(explain, dict):
        return []
    leaves = []
    lead = explain.get("lead")
    _append_fields(leaves, "/lead", lead if isinstance(lead, dict) else {}, ("text",))
    facts = explain.get("facts")
    if isinstance(facts, list):
        for index, fact in enumerate(facts):
            _append_fields(leaves, f"/facts/{index}", fact, ("value",))
    hero = explain.get("hero") if isinstance(explain.get("hero"), dict) else {}
    nodes = hero.get("nodes") if isinstance(hero.get("nodes"), list) else []
    for index, node in enumerate(nodes):
        _append_fields(leaves, f"/hero/nodes/{index}", node, ("detail",))
    steps = hero.get("steps") if isinstance(hero.get("steps"), list) else []
    for index, step in enumerate(steps):
        _append_fields(leaves, f"/hero/steps/{index}", step, ("text",))
    sections = explain.get("sections") if isinstance(explain.get("sections"), list) else []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        section_type = section.get("type")
        base = f"/sections/{index}"
        if section_type == "prose":
            _append_fields(leaves, base, section, ("body",))
        elif section_type == "diagram":
            _append_fields(leaves, base, section, ("mermaid", "alt"))
        elif section_type == "change":
            for column in ("now", "next", "unchanged"):
                items = section.get(column)
                if not isinstance(items, list):
                    continue
                for item_index, item in enumerate(items):
                    _append_fields(leaves, f"{base}/{column}/{item_index}", item, ("text",))
        elif section_type == "coverage":
            reqs = section.get("requirements")
            if isinstance(reqs, list):
                for item_index, req in enumerate(reqs):
                    _append_fields(leaves, f"{base}/requirements/{item_index}", req, ("text",))
        elif section_type == "decisions":
            items = section.get("items")
            if isinstance(items, list):
                for item_index, item in enumerate(items):
                    _append_fields(leaves, f"{base}/items/{item_index}", item, ("title", "why"))
        elif section_type == "risks":
            items = section.get("items")
            if isinstance(items, list):
                for item_index, item in enumerate(items):
                    _append_fields(leaves, f"{base}/items/{item_index}", item, ("title", "text"))
        elif section_type == "map":
            items = section.get("entries")
            if isinstance(items, list):
                for item_index, item in enumerate(items):
                    _append_fields(leaves, f"{base}/entries/{item_index}", item, ("path", "role"))
        elif section_type == "start":
            items = section.get("steps")
            if isinstance(items, list):
                for item_index, item in enumerate(items):
                    _append_fields(leaves, f"{base}/steps/{item_index}", item, ("title", "text", "command"))
    return leaves


def _blank_brief():
    return {
        "explain_schema": EXPLAIN_SCHEMA,
        "audience": "",
        "question": "",
        "out_of_scope": [],
        "check_questions": [],
    }


def frame(
    session_dir,
    *,
    audience=None,
    question=None,
    out_of_scope=None,
    check_questions=None,
    orchestrator_model=None,
) -> dict:
    root = Path(session_dir)
    if not (root / "session.json").is_file():
        raise ExplainError(1, "session.json is missing")
    path = root / "brief.json"
    if path.is_file():
        try:
            brief = read_json(path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ExplainError(1, "brief.json is not valid JSON") from exc
        if not isinstance(brief, dict):
            raise ExplainError(1, "brief.json must be an object")
    else:
        brief = _blank_brief()
    if audience is not None:
        brief["audience"] = audience
    if question is not None:
        brief["question"] = question
    if out_of_scope is not None:
        brief["out_of_scope"] = list(out_of_scope)
    if check_questions is not None:
        brief["check_questions"] = list(check_questions)
    if orchestrator_model is not None:
        brief["orchestrator_model"] = orchestrator_model
    errors = validate_brief(brief)
    if errors:
        raise ExplainError(1, errors)
    write_json(path, brief)
    return brief


def _safe_hashes(session_dir):
    root = Path(session_dir)
    hashes = {"explain_sha256": None, "evidence_sha256": None, "brief_sha256": None}
    explain = root / "explain.json"
    evidence = root / "evidence.json"
    brief = root / "brief.json"
    if explain.is_file():
        try:
            hashes["explain_sha256"] = canonical_sha256(read_json(explain))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            pass
    if evidence.is_file():
        try:
            hashes["evidence_sha256"] = sha256_file(evidence)
        except OSError:
            pass
    if brief.is_file():
        try:
            hashes["brief_sha256"] = canonical_sha256(read_json(brief))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
            pass
    return hashes


def _load_json_file(path):
    if not path.is_file():
        return None, f"{path.name} is missing"
    try:
        return read_json(path), None
    except json.JSONDecodeError:
        return None, f"{path.name} is not valid JSON"
    except UnicodeDecodeError:
        return None, f"{path.name} is not valid UTF-8"
    except OSError as exc:
        return None, f"{path.name} cannot be read: {exc}"


def run_validate(session_dir) -> dict:
    root = Path(session_dir)
    errors = []
    warnings = []
    try:
        session = load_session(root)
    except ExplainError as exc:
        errors.extend(exc.errors)
        session = {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        errors.append(_error("", "session.json is missing or invalid"))
        session = {}
    explain, explain_problem = _load_json_file(root / "explain.json")
    evidence, evidence_problem = _load_json_file(root / "evidence.json")
    brief, brief_problem = _load_json_file(root / "brief.json")
    explain_path = root / "explain.json"
    if explain_path.is_file() and explain_path.stat().st_size > _MAX_EXPLAIN_BYTES:
        errors.append(_error("", "explain.json exceeds 200000 bytes"))
    if explain_problem:
        errors.append(_error("", explain_problem))
    if evidence_problem:
        errors.append(_error("", evidence_problem))
    if brief_problem:
        errors.append(_error("", brief_problem))
    if brief is not None:
        errors.extend(validate_brief(brief))
    if explain is not None:
        session_dict = session if isinstance(session, dict) else {}
        gate = secret_gate(explain, session_dict)
        if gate:
            errors = list(gate)
        else:
            repomap = load_repomap(root) if session_dict.get("kind") == "codebase" else None
            explain_errors, explain_warnings = validate_explain(
                explain,
                evidence if isinstance(evidence, dict) else {},
                session_dict,
                brief if isinstance(brief, dict) else {},
                repomap=repomap,
            )
            errors.extend(explain_errors)
            warnings.extend(explain_warnings)
    hashes = _safe_hashes(root)
    document = {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "explain_sha256": hashes["explain_sha256"],
        "evidence_sha256": hashes["evidence_sha256"],
        "brief_sha256": hashes["brief_sha256"],
        "checked_at": now_iso(),
    }
    try:
        write_json(root / "validate.json", document)
    except OSError as exc:
        raise ExplainError(3, f"unwritable directory: {exc}") from exc
    if errors:
        raise ExplainError(1, errors)
    return document
