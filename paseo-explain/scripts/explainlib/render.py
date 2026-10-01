"""Render explain.html, the Markdown twin, and the injection payload."""

import html
import importlib
import json
import re
import secrets
import shutil
from pathlib import Path

from explainlib import __version__
from explainlib.common import (
    ExplainError,
    canonical_sha256,
    load_session,
    now_iso,
    read_json,
    save_session,
)
from explainlib.config import load_config
from explainlib.validate import run_validate

MERMAID_SRC = "https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.min.js"
MERMAID_SRI = "sha384-rbtjAdnIQE/aQJGEgXrVUlMibdfTSa4PQju4HDhN3sR2PmaKFzhEafuePsl9H/9I"
CSP_TEMPLATE = (
    "default-src 'none'; script-src 'nonce-{nonce}' https://cdn.jsdelivr.net; style-src 'unsafe-inline'; "
    "img-src data:; font-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'"
)
_PLACEHOLDER = re.compile(r"\{\{(LANG|TITLE|NONCE|DATA_JSON|MERMAID_SRC|MERMAID_SRI)\}\}")
_TEMPLATE_PATH = Path(__file__).resolve().parents[2] / "assets" / "template.html"
_VERDICTS = ("verified", "corrected", "unsupported", "unverifiable")
_COPY = {
    "en": {
        "now": "Now",
        "next": "Next",
        "unchanged": "Unchanged",
        "chosen": "Chosen",
        "rejected": "Rejected",
        "risk": "Risk",
        "open_question": "Open question",
        "assumption": "Assumption",
        "independent": "Fact-checked by {model} (another model family): {v} verified · {c} corrected",
        "corrected_suffix": "; {n} corrections applied afterwards by the explainer (not re-checked)",
        "same_family": "Checked by the same model family ({model})",
        "independence_unknown": "Fact-checked by {model}; independence unknown",
        "not_checked": "Not independently checked",
        "skip": "Fact-check skipped: {reason}",
        "reader": "Reader test: {correct}/{total} questions answered",
        "flagged": "Contains instruction-like text in the sources; treated as data",
        "depth_docs": "Based on documentation; code not read",
        "depth_code": "Based on documentation and {n} code excerpts",
    },
    "nl": {
        "now": "Nu",
        "next": "Volgende",
        "unchanged": "Ongewijzigd",
        "chosen": "Gekozen",
        "rejected": "Afgewezen",
        "risk": "Risico",
        "open_question": "Open vraag",
        "assumption": "Aanname",
        "independent": "Feitencheck door {model} (een andere modelfamilie): {v} geverifieerd · {c} gecorrigeerd",
        "corrected_suffix": "; {n} correcties daarna toegepast door de uitlegger (niet opnieuw gecontroleerd)",
        "same_family": "Gecontroleerd door dezelfde modelfamilie ({model})",
        "independence_unknown": "Feitencheck door {model}; onafhankelijkheid onbekend",
        "not_checked": "Niet onafhankelijk gecontroleerd",
        "skip": "Feitencheck overgeslagen: {reason}",
        "reader": "Lezerstest: {correct}/{total} vragen beantwoord",
        "flagged": "Bevat instructie-achtige tekst in de bronnen; behandeld als data",
        "depth_docs": "Gebaseerd op documentatie; code niet gelezen",
        "depth_code": "Gebaseerd op documentatie en {n} codefragmenten",
    },
}


def embed_json(obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        s.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def fill_template(template, values):
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], template)


def injection_payload(html):
    lit = json.dumps(html).replace("</", "<\\/").replace("<!--", "<\\!--")
    lit = lit.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return (
        "() => { const h = "
        + lit
        + "; document.open(); document.write(h); document.close(); return document.title; }"
    )


def _copy(lang):
    if isinstance(lang, str) and lang.lower().startswith("nl"):
        return _COPY["nl"]
    return _COPY["en"]


def _esc(text):
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    return text.replace("<", "&lt;").replace(">", "&gt;")


def _inline(text):
    return _esc(text).replace("\n", " ")


def _text_at(value, level):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        got = value.get(str(level))
        if isinstance(got, str):
            return got
    return ""


def _ids(obj):
    if not isinstance(obj, dict):
        return []
    evidence = obj.get("evidence")
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, str)]
    if isinstance(evidence, str):
        return [evidence]
    return []


def _item_ids(obj):
    found = _ids(obj)
    quote = obj.get("quote") if isinstance(obj, dict) else None
    if isinstance(quote, dict) and isinstance(quote.get("evidence"), str) and quote["evidence"] not in found:
        found.append(quote["evidence"])
    return found


def _collect_ids(obj, found):
    if isinstance(obj, dict):
        for item in _ids(obj):
            if item not in found:
                found.append(item)
        for value in obj.values():
            _collect_ids(value, found)
    elif isinstance(obj, list):
        for item in obj:
            _collect_ids(item, found)


def _evidence_index(evidence):
    sources = {}
    fragments = []
    if isinstance(evidence, dict):
        for src in evidence.get("sources") or []:
            if isinstance(src, dict) and isinstance(src.get("id"), str):
                display = src.get("display")
                sources[src["id"]] = display if isinstance(display, str) else ""
        raw = evidence.get("fragments")
        if isinstance(raw, list):
            fragments = raw
    index = {}
    for frag in fragments:
        if not isinstance(frag, dict) or not isinstance(frag.get("id"), str):
            continue
        text = frag.get("text") if isinstance(frag.get("text"), str) else ""
        index[frag["id"]] = {
            "anchor": frag.get("anchor") if isinstance(frag.get("anchor"), str) else "",
            "source_display": sources.get(frag.get("source"), ""),
            "line_start": frag.get("line_start"),
            "line_end": frag.get("line_end"),
            "text": text[:800],
            "flagged": bool(frag.get("flagged")),
            "withheld": frag.get("withheld") is True,
        }
    return index


def _embedded_evidence(explain, evidence):
    index = _evidence_index(evidence)
    found = []
    _collect_ids(explain, found)
    return {eid: index[eid] for eid in found if eid in index}


def _anchor_suffix(ids, index):
    bits = []
    for eid in ids or []:
        info = index.get(eid)
        if not info:
            continue
        bits.append(f"[{_esc(info['source_display'])} {_esc(info['anchor'])}]")
    if not bits:
        return ""
    return " " + " ".join(bits)


def _flagged_fragments(evidence):
    fragments = evidence.get("fragments") if isinstance(evidence, dict) else None
    if not isinstance(fragments, list):
        return []
    return [frag for frag in fragments if isinstance(frag, dict) and bool(frag.get("flagged"))]


def _flagged_meta(evidence):
    found = _flagged_fragments(evidence)
    anchors = []
    for frag in found[:10]:
        anchor = frag.get("anchor")
        anchors.append(anchor if isinstance(anchor, str) else "")
    return len(found), anchors


def _any_flagged(_explain, evidence):
    return bool(_flagged_fragments(evidence))


def _banner_lines(meta, explain, evidence):
    lang = explain.get("lang") if isinstance(explain, dict) else ""
    copy = _copy(lang)
    label = meta.get("check_label") or "not_checked"
    models = meta.get("models") if isinstance(meta.get("models"), dict) else {}
    model = models.get("fact_checker") or ""
    if not isinstance(model, str):
        model = "" if model is None else str(model)
    counts = meta.get("fact_counts") if isinstance(meta.get("fact_counts"), dict) else {}
    verified = counts.get("verified") or 0
    corrected = counts.get("corrected") or 0
    checks = meta.get("checks") if isinstance(meta.get("checks"), dict) else {}
    corrections = checks.get("fact_check_corrections") or 0
    if label in ("independent", "independent_corrected"):
        line = copy["independent"].format(model=_inline(model), v=verified, c=corrected)
        if label == "independent_corrected":
            line += copy["corrected_suffix"].format(n=corrections)
    elif label == "same_family":
        line = copy["same_family"].format(model=_inline(model))
    elif label == "independence_unknown":
        line = copy["independence_unknown"].format(model=_inline(model))
    else:
        line = copy["not_checked"]
    lines = [line]
    depth = meta.get("depth")
    if depth == "docs":
        lines.append(copy["depth_docs"])
    elif depth == "code":
        count = meta.get("code_excerpts")
        lines.append(copy["depth_code"].format(n=count if isinstance(count, int) and not isinstance(count, bool) else 0))
    skipped = meta.get("skipped") if isinstance(meta.get("skipped"), dict) else {}
    reason = skipped.get("fact_check")
    if isinstance(reason, str) and reason:
        lines.append(copy["skip"].format(reason=_inline(reason)))
    grade = meta.get("reader_grade") if isinstance(meta.get("reader_grade"), dict) else None
    if meta.get("mode") == "deep" and isinstance(grade, dict) and grade.get("total") is not None:
        lines.append(copy["reader"].format(correct=grade.get("correct"), total=grade.get("total")))
    if _any_flagged(explain, evidence):
        lines.append(copy["flagged"])
    return lines


def _paragraphs(text, ids, index):
    raw = text.strip("\n")
    if not raw:
        return ""
    parts = [_esc(part.strip("\n")) for part in re.split(r"\n[ \t]*\n", raw)]
    parts[-1] = parts[-1] + _anchor_suffix(ids, index)
    return "\n\n".join(parts)


def _fence(lang, body):
    tick = "```"
    while tick in body:
        tick += "`"
    if not body.endswith("\n"):
        body += "\n"
    return f"{tick}{lang}\n{body}{tick}"


def _change_md(section, level, index, lang):
    copy = _copy(lang)
    blocks = []
    for key, label in (("now", copy["now"]), ("next", copy["next"]), ("unchanged", copy["unchanged"])):
        lines = [f"**{label}**"]
        for item in section.get(key) or []:
            if not isinstance(item, dict):
                continue
            lines.append(
                "- " + _inline(_text_at(item.get("text"), level)) + _anchor_suffix(_item_ids(item), index)
            )
        blocks.append("\n".join(lines))
    body = "\n\n".join(blocks)
    extra = _anchor_suffix(_ids(section), index).strip()
    if extra:
        body += "\n\n" + extra
    return body


def _cell(text):
    return _esc(text).replace("|", "\\|").replace("\n", " ")


def _coverage_md(section, index):
    tasks = [item for item in (section.get("tasks") or []) if isinstance(item, dict)]
    headers = ["Requirement", *[str(item.get("id") or "") for item in tasks], "Test"]
    rows = [
        "| " + " | ".join(_cell(header) for header in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    task_ids = [item.get("id") for item in tasks]
    for req in section.get("requirements") or []:
        if not isinstance(req, dict):
            continue
        linked = set(req.get("tasks") or [])
        label = _cell(f"{req.get('id') or ''} {req.get('text') or ''}") + _anchor_suffix(_ids(req), index)
        marks = ["•" if tid in linked else "" for tid in task_ids]
        test = _cell(req.get("test") or "")
        rows.append("| " + " | ".join([label, *marks, test]) + " |")
    body = "\n".join(rows)
    extra = _anchor_suffix(_ids(section), index).strip()
    if extra:
        body += "\n\n" + extra
    return body


def _bullet_items(section, level, index, label_of):
    lines = []
    for item in section.get("items") or []:
        if not isinstance(item, dict):
            continue
        label = label_of(item)
        title = _inline(item.get("title") or "")
        body = _inline(_text_at(item.get("why") if "why" in item else item.get("text"), level))
        lines.append(f"- **{label}** {title}: {body}" + _anchor_suffix(_item_ids(item), index))
    extra = _anchor_suffix(_ids(section), index).strip()
    body = "\n".join(lines)
    if extra:
        body += ("\n\n" if body else "") + extra
    return body


def _map_md(section, level, index):
    rows = ["| Path | Role |", "| --- | --- |"]
    for entry in section.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        path = _cell(entry.get("path") or "")
        role = _cell(_text_at(entry.get("role"), level)) + _anchor_suffix(_ids(entry), index)
        rows.append(f"| `{path}` | {role} |")
    body = "\n".join(rows)
    extra = _anchor_suffix(_ids(section), index).strip()
    if extra:
        body += "\n\n" + extra
    return body


def _start_md(section, level, index):
    blocks = []
    for number, step in enumerate(section.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        chunk = (
            f"{number}. **{_inline(step.get('title') or '')}**: "
            f"{_inline(_text_at(step.get('text'), level))}" + _anchor_suffix(_ids(step), index)
        )
        command = step.get("command")
        if isinstance(command, str) and command:
            chunk += "\n\n" + _fence("", command)
        blocks.append(chunk)
    body = "\n\n".join(blocks)
    extra = _anchor_suffix(_ids(section), index).strip()
    if extra:
        body += ("\n\n" if body else "") + extra
    return body


def _quiz_md(section, level, for_reader):
    blocks = []
    for number, item in enumerate(section.get("items") or [], 1):
        if not isinstance(item, dict):
            continue
        chunk = f"{number}. {_inline(_text_at(item.get('q'), level))}"
        if not for_reader:
            chunk += "\n" + f"Answer: {_inline(_text_at(item.get('a'), level))}"
        blocks.append(chunk)
    return "\n\n".join(blocks)


def _diagram_md(section, level, index):
    caption = _inline(_text_at(section.get("caption"), level)) + _anchor_suffix(_ids(section), index)
    source = section.get("mermaid") if isinstance(section.get("mermaid"), str) else ""
    alt = section.get("alt") if isinstance(section.get("alt"), str) else ""
    return "\n\n".join(part for part in (caption, _fence("mermaid", _esc(source)), _esc(alt)) if part)


def _section_md(section, number, level, index, lang, for_reader):
    copy = _copy(lang)
    parts = [f"## {number:02d} {_inline(section.get('title') or '')}"]
    if "subtitle" in section:
        subtitle = _text_at(section.get("subtitle"), level)
        if subtitle:
            parts.append(_esc(subtitle))
    kind = section.get("type")
    if kind == "prose":
        parts.append(_paragraphs(_text_at(section.get("body"), level), _ids(section), index))
    elif kind == "change":
        parts.append(_change_md(section, level, index, lang))
    elif kind == "diagram":
        parts.append(_diagram_md(section, level, index))
    elif kind == "coverage":
        parts.append(_coverage_md(section, index))
    elif kind == "decisions":
        parts.append(
            _bullet_items(
                section,
                level,
                index,
                lambda item: copy.get(item.get("status"), _inline(item.get("status") or "")),
            )
        )
    elif kind == "risks":
        parts.append(
            _bullet_items(
                section,
                level,
                index,
                lambda item: copy.get(item.get("kind"), _inline(item.get("kind") or "")),
            )
        )
    elif kind == "map":
        parts.append(_map_md(section, level, index))
    elif kind == "start":
        parts.append(_start_md(section, level, index))
    elif kind == "quiz":
        parts.append(_quiz_md(section, level, for_reader))
    return "\n\n".join(part for part in parts if part)


def _overview(explain, level, index):
    hero = explain.get("hero") if isinstance(explain.get("hero"), dict) else {}
    parts = ["## Overview"]
    caption = _text_at(hero.get("caption"), level)
    if caption:
        parts.append(_esc(caption))
    nodes = []
    for node in hero.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nodes.append(
            f"- **{_inline(node.get('label') or '')}** ({_inline(node.get('tone') or '')}): "
            f"{_inline(_text_at(node.get('detail'), level))}"
            + _anchor_suffix(_ids(node), index)
        )
    if nodes:
        parts.append("\n".join(nodes))
    steps = []
    for number, step in enumerate(hero.get("steps") or [], 1):
        if not isinstance(step, dict):
            continue
        steps.append(
            f"{number}. {_inline(_text_at(step.get('text'), level))}" + _anchor_suffix(_ids(step), index)
        )
    if steps:
        parts.append("\n".join(steps))
    return "\n\n".join(parts)


def _glossary(explain):
    items = [item for item in (explain.get("glossary") or []) if isinstance(item, dict)]
    if not items:
        return ""
    entries = [
        f"**{_inline(item.get('term') or '')}**\n\n{_esc(item.get('definition') or '')}" for item in items
    ]
    return "## Glossary\n\n" + "\n\n".join(entries)


def render_markdown(explain, evidence, meta, level, for_reader=False) -> str:
    if not isinstance(explain, dict):
        explain = {}
    if not isinstance(meta, dict):
        meta = {}
    index = _evidence_index(evidence)
    lang = explain.get("lang") if isinstance(explain.get("lang"), str) else "en"
    parts = [f"# {_inline(explain.get('title') or '')}"]
    lead = explain.get("lead") if isinstance(explain.get("lead"), dict) else {}
    parts.append(_inline(_text_at(lead.get("text"), level)) + _anchor_suffix(_ids(lead), index))
    facts = []
    for fact in explain.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        facts.append(
            f"- **{_inline(fact.get('label') or '')}:** {_inline(fact.get('value') or '')}"
            + _anchor_suffix(_ids(fact), index)
        )
    if facts:
        parts.append("\n".join(facts))
    parts.append("\n".join("> " + line for line in _banner_lines(meta, explain, evidence)))
    parts.append(_overview(explain, level, index))
    for number, section in enumerate(explain.get("sections") or [], 1):
        if isinstance(section, dict):
            parts.append(_section_md(section, number, level, index, lang, for_reader))
    glossary = _glossary(explain)
    if glossary:
        parts.append(glossary)
    return "\n\n".join(part for part in parts if part) + "\n"


def _load_object(path, name):
    path = Path(path)
    if not path.is_file():
        raise ExplainError(1, f"{name} is missing")
    try:
        return read_json(path)
    except json.JSONDecodeError:
        raise ExplainError(1, f"{name} is not valid JSON") from None
    except UnicodeDecodeError:
        raise ExplainError(1, f"{name} is not valid UTF-8") from None
    except OSError as exc:
        raise ExplainError(3, f"cannot read {name}: {exc}") from exc


def _fact_counts(session_dir, fact_check):
    if fact_check not in ("pass", "partial"):
        return None
    path = Path(session_dir) / "factcheck.json"
    if not path.is_file():
        return None
    try:
        report = read_json(path)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError, ValueError):
        return None
    claims = report.get("claims") if isinstance(report, dict) else None
    if not isinstance(claims, list):
        return None
    counts = {name: 0 for name in _VERDICTS}
    for claim in claims:
        if isinstance(claim, dict) and claim.get("verdict") in counts:
            counts[claim["verdict"]] += 1
    return counts


def _code_excerpts(session, evidence):
    if session.get("kind") != "codebase" or not isinstance(evidence, dict):
        return None
    roles = {}
    for src in evidence.get("sources") or []:
        if isinstance(src, dict) and isinstance(src.get("id"), str):
            roles[src["id"]] = src.get("role")
    fragments = evidence.get("fragments") if isinstance(evidence.get("fragments"), list) else []
    return sum(1 for frag in fragments if isinstance(frag, dict) and roles.get(frag.get("source")) == "code")


def _compose_meta(session, explain, evidence, computed, session_dir):
    checks = computed.get("checks") if isinstance(computed, dict) and isinstance(computed.get("checks"), dict) else {}
    models = computed.get("models") if isinstance(computed, dict) and isinstance(computed.get("models"), dict) else {}
    config = load_config()
    theme = config["values"]["theme"] if config["sources"]["theme"] == "config" else None
    grade = session.get("reader_grade")
    if not isinstance(grade, dict):
        grade = None
    lang = explain.get("lang") if isinstance(explain.get("lang"), str) else session.get("lang")
    flagged_total, flagged_anchors = _flagged_meta(evidence)
    return {
        "version": __version__,
        "slug": session.get("slug"),
        "round": session.get("round"),
        "generated_at": now_iso(),
        "default_level": session.get("default_level"),
        "levels": session.get("levels"),
        "lang": lang,
        "theme": theme,
        "mode": session.get("mode"),
        "checks": checks,
        "check_label": checks.get("check_label"),
        "skipped": checks.get("skipped"),
        "models": models,
        "fact_counts": _fact_counts(session_dir, checks.get("fact_check")),
        "flagged_total": flagged_total,
        "flagged_anchors": flagged_anchors,
        "reader_grade": grade,
        "depth": session.get("depth") if isinstance(session.get("depth"), str) else None,
        "code_excerpts": _code_excerpts(session, evidence),
    }


def _render_html(explain, evidence, meta):
    try:
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExplainError(3, f"cannot read template: {exc}") from exc
    nonce = secrets.token_urlsafe(18)
    title = explain.get("title") if isinstance(explain.get("title"), str) else ""
    lang = meta.get("lang") if isinstance(meta.get("lang"), str) else ""
    payload = {
        "explain": explain,
        "evidence": _embedded_evidence(explain, evidence),
        "meta": meta,
    }
    values = {
        "LANG": lang,
        "TITLE": html.escape(title, quote=True),
        "NONCE": nonce,
        "DATA_JSON": embed_json(payload),
        "MERMAID_SRC": MERMAID_SRC,
        "MERMAID_SRI": MERMAID_SRI,
    }
    return fill_template(template, values)


def _write_text(path, text):
    path = Path(path)
    try:
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        raise ExplainError(3, f"unwritable directory: {exc}") from exc


def _result_api():
    return importlib.import_module("explainlib.result")


def render(session_dir) -> dict:
    root = Path(session_dir).resolve()
    run_validate(root)
    computed = _result_api().compute_result(str(root))
    session = load_session(root)
    explain = _load_object(root / "explain.json", "explain.json")
    evidence = _load_object(root / "evidence.json", "evidence.json")
    digest = canonical_sha256(explain)
    if session.get("content_sha256") != digest:
        session["round"] = int(session.get("round") or 0) + 1
        session["content_sha256"] = digest
        save_session(root, session)
    meta = _compose_meta(session, explain, evidence, computed, root)
    html_text = _render_html(explain, evidence, meta)
    md_text = render_markdown(explain, evidence, meta, session.get("default_level"), for_reader=False)
    html_path = root / "explain.html"
    md_path = root / "explain.md"
    _write_text(html_path, html_text)
    _write_text(md_path, md_text)
    export = session.get("export_dir")
    if isinstance(export, str) and export:
        dest = Path(export)
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(html_path, dest / "explain.html")
            shutil.copyfile(md_path, dest / "explain.md")
        except OSError as exc:
            raise ExplainError(3, f"unwritable directory: {exc}") from exc
    else:
        export = None
    _result_api().write_result(str(root))
    return {
        "ok": True,
        "html": str(html_path),
        "md": str(md_path),
        "result": str(root / "result.json"),
        "round": session.get("round"),
        "bytes": html_path.stat().st_size,
        "export": export,
    }


def markdown_command(session_dir, level: int, to: str | None):
    root = Path(session_dir).resolve()
    try:
        session = load_session(root)
    except FileNotFoundError:
        raise ExplainError(1, "session.json is missing") from None
    except json.JSONDecodeError:
        raise ExplainError(1, "session.json is not valid JSON") from None
    except UnicodeDecodeError:
        raise ExplainError(1, "session.json is not valid UTF-8") from None
    levels = session.get("levels") or []
    if level not in levels:
        raise ExplainError(1, "level is not a written level")
    explain = _load_object(root / "explain.json", "explain.json")
    evidence = _load_object(root / "evidence.json", "evidence.json")
    computed = _result_api().compute_result(str(root))
    meta = _compose_meta(session, explain, evidence, computed, root)
    text = render_markdown(explain, evidence, meta, level, for_reader=False)
    if to is None:
        return text
    dest = Path(to).resolve()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ExplainError(3, f"unwritable directory: {exc}") from exc
    _write_text(dest, text)
    return {"ok": True, "path": str(dest)}


def inject(session_dir, max_bytes: int = 400000) -> str:
    path = Path(session_dir) / "explain.html"
    if not path.is_file():
        raise ExplainError(1, "explain.html is missing")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ExplainError(3, f"cannot read explain.html: {exc}") from exc
    if len(data) > max_bytes:
        raise ExplainError(1, f"explain.html exceeds {max_bytes} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ExplainError(1, "explain.html is not valid UTF-8") from None
    return injection_payload(text)
