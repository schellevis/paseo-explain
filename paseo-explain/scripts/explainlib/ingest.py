"""Read plan, spec, and idea sources into evidence.json and scan.json."""

import json
import os
import re
from pathlib import Path

from explainlib import EXPLAIN_SCHEMA
from explainlib.common import ExplainError, load_session, sha256_bytes, write_json
from explainlib.scan import scan_fragments

_ATX = re.compile(r"^(#{1,4})[ \t]+(.*)$")
_REQ = re.compile(r"^###\s+(R\d+)\.?\s+(.+?)\s*$")
_TASK = re.compile(r"^###\s+Task\s+([A-Za-z0-9._-]+):\s+(.+?)\s*$")
_FINDING_HEAD = re.compile(r"^##\s+Finding decisions\s*$")
_WAVE = re.compile(r"^\s*-\s+wave\s*:\s*(.*)$", re.IGNORECASE)
_DEPS = re.compile(r"^\s*-\s+dependencies\s*:\s*(.*)$", re.IGNORECASE)
_OWNED = re.compile(r"^\s*-\s+owned\s+files\s*:\s*(.*)$", re.IGNORECASE)
_OUTCOME = re.compile(r"^\s*-\s+outcome\s*:\s*(.*)$", re.IGNORECASE)
_REASON = re.compile(r"^\s*-\s+reason\s*:\s*(.*)$", re.IGNORECASE)
_NONE = re.compile(r"(?i)^none\b")
_REF = re.compile(r"\bR\d+\b")
_PAREN = re.compile(r"\s*\([^)]*\)\s*$")

_AUTOPILOT = [
    ("00-brief.md", "brief", False),
    ("01-spec.md", "spec", True),
    ("02-spec-resolution.md", "resolution", False),
]
_AUTOPILOT_PLAN = [
    ("03-plan.md", "plan", True),
    ("04-plan-resolution.md", "resolution", False),
]


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def _logical_lines(norm: str) -> list[tuple[int, str, str]]:
    if norm == "":
        return []
    parts = norm.split("\n")
    trailing = norm.endswith("\n")
    if trailing:
        parts = parts[:-1]
    lines = []
    for index, content in enumerate(parts):
        is_last = index == len(parts) - 1
        raw = content if is_last and not trailing else content + "\n"
        lines.append((index + 1, content, raw))
    return lines


def _preamble(lines: list[tuple[int, str, str]]) -> dict:
    return {
        "anchor": "§(preamble)",
        "line_start": lines[0][0],
        "line_end": lines[-1][0],
        "text": "".join(item[2] for item in lines),
        "level": 0,
        "heading": "",
    }


def split_blocks(text: str) -> list[dict]:
    lines = _logical_lines(_normalize(text))
    heading_at = [index for index, (_no, content, _raw) in enumerate(lines) if _ATX.match(content)]
    if not heading_at:
        if not lines:
            return []
        return [_preamble(lines)]
    blocks = []
    if heading_at[0] > 0:
        blocks.append(_preamble(lines[: heading_at[0]]))
    for pos, start in enumerate(heading_at):
        end = heading_at[pos + 1] if pos + 1 < len(heading_at) else len(lines)
        chunk = lines[start:end]
        content = chunk[0][1]
        match = _ATX.match(content)
        blocks.append(
            {
                "anchor": f"§{match.group(2).strip()}",
                "line_start": chunk[0][0],
                "line_end": chunk[-1][0],
                "text": "".join(item[2] for item in chunk),
                "level": len(match.group(1)),
                "heading": content,
            }
        )
    return blocks


def _line_end(chunk: str, line_start: int) -> int:
    if chunk == "":
        return line_start
    newlines = chunk.count("\n")
    if chunk.endswith("\n"):
        return line_start + newlines - 1
    return line_start + newlines


def _split_long(text: str, line_start: int) -> list[tuple[str, int, int]]:
    if len(text) <= 4000:
        return [(text, line_start, _line_end(text, line_start))]
    pieces = []
    remaining = text
    cursor = line_start
    while remaining:
        if len(remaining) <= 4000:
            chunk = remaining
            remaining = ""
        else:
            window = remaining[:4000]
            newline = window.rfind("\n")
            if newline >= 0:
                chunk = window[: newline + 1]
            else:
                chunk = window
            remaining = remaining[len(chunk) :]
        if chunk == "":
            break
        end = _line_end(chunk, cursor)
        pieces.append((chunk, cursor, end))
        cursor = end + 1 if chunk.endswith("\n") else end
    return pieces


def fragment_blocks(blocks, source_id, start_index) -> list[dict]:
    fragments = []
    number = start_index
    for block in blocks:
        parts = _split_long(block["text"], block["line_start"])
        for index, (chunk, line_start, line_end) in enumerate(parts):
            anchor = block["anchor"] if index == 0 else f"{block['anchor']} (part {index + 1})"
            fragments.append(
                {
                    "id": f"E{number}",
                    "source": source_id,
                    "anchor": anchor,
                    "line_start": line_start,
                    "line_end": line_end,
                    "text": chunk,
                    "flagged": False,
                }
            )
            number += 1
    return fragments


def fragment_paragraphs(text, source_id, start_index) -> list[dict]:
    paragraphs = []
    current = []
    for line_no, content, raw in _logical_lines(_normalize(text)):
        if content.strip() == "":
            if current:
                paragraphs.append(current)
                current = []
        else:
            current.append((line_no, raw))
    if current:
        paragraphs.append(current)
    fragments = []
    number = start_index
    for index, paragraph in enumerate(paragraphs, start=1):
        fragments.append(
            {
                "id": f"E{number}",
                "source": source_id,
                "anchor": f"¶{index}",
                "line_start": paragraph[0][0],
                "line_end": paragraph[-1][0],
                "text": "".join(raw for _line_no, raw in paragraph),
                "flagged": False,
            }
        )
        number += 1
    return fragments


def _parse_list(value: str) -> list[str]:
    raw = value.strip()
    if _NONE.match(raw):
        return []
    items = []
    for part in re.split(r"[,;]", raw):
        item = _PAREN.sub("", part.replace("`", "").strip()).strip()
        if item:
            items.append(item)
    return items


def _parse_wave(value: str):
    raw = value.strip()
    if re.fullmatch(r"[0-9]+", raw):
        return int(raw)
    return None


def _first_bullet(pattern, text: str):
    for line in text.split("\n"):
        match = pattern.match(line)
        if match:
            return match.group(1)
    return None


def _evidence_ids(blocks, fragments) -> list[str]:
    ids = []
    offset = 0
    for block in blocks:
        count = len(_split_long(block["text"], block["line_start"]))
        group = fragments[offset : offset + count]
        offset += count
        ids.append(group[0]["id"] if group else "")
    return ids


def _finding_doc(display: str) -> str:
    name = os.path.basename(display)
    if name.startswith("02-"):
        return "spec"
    if name.startswith("04-"):
        return "plan"
    return "other"


def _refs(text: str) -> list[str]:
    found = []
    seen = set()
    for ref in _REF.findall(text):
        if ref not in seen:
            seen.add(ref)
            found.append(ref)
    return found


def extract_structured(sources_with_blocks) -> dict:
    requirements = []
    tasks = []
    findings = []
    for source, blocks, fragments in sources_with_blocks:
        evids = _evidence_ids(blocks, fragments)
        role = source.get("role")
        if role == "spec":
            for block, evid in zip(blocks, evids):
                match = _REQ.match(block["heading"])
                if match:
                    requirements.append(
                        {"id": match.group(1), "title": match.group(2).strip(), "evidence": evid}
                    )
        elif role == "plan":
            for block, evid in zip(blocks, evids):
                match = _TASK.match(block["heading"])
                if not match:
                    continue
                wave_raw = _first_bullet(_WAVE, block["text"])
                deps_raw = _first_bullet(_DEPS, block["text"])
                owned_raw = _first_bullet(_OWNED, block["text"])
                tasks.append(
                    {
                        "id": match.group(1),
                        "title": match.group(2).strip(),
                        "wave": None if wave_raw is None else _parse_wave(wave_raw),
                        "dependencies": [] if deps_raw is None else _parse_list(deps_raw),
                        "owned_files": [] if owned_raw is None else _parse_list(owned_raw),
                        "requirement_refs": _refs(block["text"]),
                        "evidence": evid,
                    }
                )
        elif role == "resolution":
            in_region = False
            for block, evid in zip(blocks, evids):
                level = block["level"]
                heading = block["heading"]
                if level == 2 and _FINDING_HEAD.match(heading):
                    in_region = True
                    continue
                if in_region and 0 < level <= 2:
                    in_region = False
                    continue
                if not (in_region and level == 3):
                    continue
                match = _ATX.match(heading)
                outcome = _first_bullet(_OUTCOME, block["text"])
                reason = _first_bullet(_REASON, block["text"])
                findings.append(
                    {
                        "id": match.group(2).strip() if match else heading,
                        "doc": _finding_doc(source.get("display", "")),
                        "outcome": "" if outcome is None else outcome.strip(),
                        "reason": "" if reason is None else reason.strip(),
                        "evidence": evid,
                    }
                )
    grouped = {}
    for task in tasks:
        wave = task["wave"]
        if wave is None:
            continue
        grouped.setdefault(wave, []).append(task["id"])
    return {
        "refs_present": any(task["requirement_refs"] for task in tasks),
        "requirements": requirements,
        "tasks": tasks,
        "waves": [grouped[wave] for wave in sorted(grouped)],
        "findings": findings,
    }


def _read_bytes(path) -> bytes:
    with open(path, "rb") as handle:
        return handle.read()


def _read_required(path) -> bytes:
    try:
        return _read_bytes(path)
    except FileNotFoundError as exc:
        raise ExplainError(1, f"missing {os.path.basename(os.fspath(path))}") from exc
    except OSError as exc:
        raise ExplainError(1, f"unreadable source: {os.path.basename(os.fspath(path))}") from exc


def _read_optional(path):
    try:
        return _read_bytes(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ExplainError(1, f"unreadable source: {os.path.basename(os.fspath(path))}") from exc


def _decode(data: bytes, display: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExplainError(1, f"source is not utf-8: {display}") from exc


def _has_atx(text: str) -> bool:
    return any(_ATX.match(content) for _no, content, _raw in _logical_lines(_normalize(text)))


def _line_count(text: str) -> int:
    return len(_logical_lines(_normalize(text)))


def _load_entries(autopilot, doc, files, text_file):
    if text_file is not None:
        display = os.path.basename(os.fspath(text_file))
        return [("idea", display, _read_required(text_file))], True
    if autopilot is not None:
        if doc not in ("spec", "plan"):
            raise ExplainError(1, "doc must be spec or plan")
        root = Path(autopilot)
        names = list(_AUTOPILOT)
        if doc == "plan":
            names.extend(_AUTOPILOT_PLAN)
        loaded = []
        for name, role, required in names:
            path = root / name
            data = _read_required(path) if required else _read_optional(path)
            if data is not None:
                loaded.append((role, name, data))
        return loaded, False
    if files:
        loaded = []
        for role, path in files:
            display = os.path.basename(os.fspath(path))
            loaded.append((role, display, _read_required(path)))
        return loaded, False
    raise ExplainError(1, "ingest requires a source")


def ingest(session_dir, *, autopilot=None, doc=None, files=None, text_file=None) -> dict:
    session_dir = Path(session_dir)
    try:
        load_session(session_dir)
    except ExplainError:
        raise
    except FileNotFoundError as exc:
        raise ExplainError(1, "session.json is missing") from exc
    except json.JSONDecodeError as exc:
        raise ExplainError(1, "session.json is not valid json") from exc

    loaded, paragraph_mode = _load_entries(autopilot, doc, files, text_file)
    sources = []
    bundles = []
    fragments = []
    next_index = 1
    for number, (role, display, data) in enumerate(loaded, start=1):
        text = _decode(data, display)
        source = {
            "id": f"S{number}",
            "role": role,
            "display": display,
            "sha256": sha256_bytes(data),
            "lines": _line_count(text),
        }
        if paragraph_mode and not _has_atx(text):
            blocks = []
            produced = fragment_paragraphs(text, source["id"], next_index)
        else:
            blocks = split_blocks(text)
            produced = fragment_blocks(blocks, source["id"], next_index)
        next_index += len(produced)
        fragments.extend(produced)
        sources.append(source)
        bundles.append((source, blocks, produced))

    structured = extract_structured(bundles)
    scan_doc = scan_fragments(fragments)
    evidence = {
        "explain_schema": EXPLAIN_SCHEMA,
        "sources": sources,
        "fragments": fragments,
        "structured": structured,
    }
    write_json(session_dir / "evidence.json", evidence)
    write_json(session_dir / "scan.json", scan_doc)
    return {
        "ok": True,
        "sources": len(sources),
        "fragments": len(fragments),
        "flagged": sum(1 for fragment in fragments if fragment["flagged"]),
        "structured": {
            "requirements": len(structured["requirements"]),
            "tasks": len(structured["tasks"]),
            "findings": len(structured["findings"]),
            "refs_present": structured["refs_present"],
        },
    }
