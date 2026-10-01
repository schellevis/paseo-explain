"""Read plan, spec, and idea sources into evidence.json and scan.json."""

import json
import os
import re
from pathlib import Path

from explainlib import EXPLAIN_SCHEMA
from explainlib.common import ExplainError, load_session, sha256_bytes, write_json
from explainlib import repo
from explainlib.scan import scan_fragments
from explainlib.secretscan import find_secrets

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


def _split_long(text: str, line_start: int, limit: int = 4000) -> list[tuple[str, int, int]]:
    if len(text) <= limit:
        return [(text, line_start, _line_end(text, line_start))]
    pieces = []
    remaining = text
    cursor = line_start
    while remaining:
        if len(remaining) <= limit:
            chunk = remaining
            remaining = ""
        else:
            window = remaining[:limit]
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


_DOC_ROOT_NAMES = ("claude.md", "agents.md", "gemini.md")
_DOC_ROOT_PREFIXES = ("readme", "contributing", "architecture", "design", "hacking", "development")
_AUTO_DOC_LIMIT = 20
_CODEBASE_ONLY = "codebase ingest takes --add-doc, --code and --reuse only"
_NOT_CODEBASE = "--add-doc, --code and --reuse are for codebase sessions"
_CODE_RANGE = re.compile(r"^(.*):(\d+)-(\d+)$")
_CODE_WINDOW = 120
_CODE_MIN_FRAGMENT = 40
_CODE_FRAGMENT_CHARS = 8000
_CODE_FRAGMENT_LIMIT = 300
_EVIDENCE_LIMIT = 2000000


def _load_checked_session(session_dir: Path) -> dict:
    try:
        return load_session(session_dir)
    except ExplainError:
        raise
    except FileNotFoundError as exc:
        raise ExplainError(1, "session.json is missing") from exc
    except json.JSONDecodeError as exc:
        raise ExplainError(1, "session.json is not valid json") from exc


def _root_doc_rank(path: str):
    if "/" in path:
        return None
    lower = path.lower()
    if lower in _DOC_ROOT_NAMES:
        return _DOC_ROOT_NAMES.index(lower)
    for offset, prefix in enumerate(_DOC_ROOT_PREFIXES):
        if lower.startswith(prefix):
            return len(_DOC_ROOT_NAMES) + offset
    return None


def _real(repo_real: str, path: str) -> str:
    return os.path.realpath(os.path.join(repo_real, path))


def _automatic_docs(listing: dict, repo_real: str) -> list[str]:
    root = []
    nested = []
    for record in listing["files"]:
        if record["kind"] != "text":
            continue
        path = record["path"]
        rank = _root_doc_rank(path)
        if rank is not None:
            root.append((rank, path))
        elif path.split("/")[0] in ("docs", "doc") and "/" in path and repo.is_doc_file(record):
            nested.append(path)
    ordered = [path for _rank, path in sorted(root)] + sorted(nested)
    seen = set()
    chosen = []
    for path in ordered:
        real = _real(repo_real, path)
        if real in seen:
            continue
        seen.add(real)
        chosen.append(path)
    return chosen[:_AUTO_DOC_LIMIT]


def resolve_listed(spec: str, repo_real: str, by_path: dict) -> dict:
    """Map a repo-relative or absolute path to its listed text record, or raise ExplainError(1)."""
    shown = "(withheld-path)" if find_secrets(spec) else spec
    missing = ExplainError(1, f"file not in repository listing: {shown}")
    if "\x00" in spec or "\\" in spec or ".." in spec.split("/"):
        raise missing
    if os.path.isabs(spec):
        norm = os.path.normpath(spec)
        resolved = os.path.join(os.path.realpath(os.path.dirname(norm)), os.path.basename(norm))
        prefix = repo_real.rstrip(os.sep) + os.sep
        if not resolved.startswith(prefix):
            raise missing
        rel = resolved[len(prefix) :].replace(os.sep, "/")
    else:
        rel = "/".join(part for part in spec.split("/") if part not in ("", "."))
    record = by_path.get(rel)
    if record is None:
        raise missing
    if record["kind"] == "excluded":
        raise ExplainError(1, f"file is excluded: {record['path']} ({record['reason']})")
    if record["kind"] == "large":
        raise ExplainError(1, f"file is too large: {record['path']}")
    if record["kind"] == "binary":
        raise ExplainError(1, f"file is binary: {record['path']}")
    return record


def _read_listed(repo_real: str, record: dict) -> tuple[bytes, str]:
    data = _read_required(os.path.join(repo_real, record["path"]))
    if sha256_bytes(data) != record["sha256"]:
        raise ExplainError(1, f"file changed during ingest: {record['path']}")
    return data, _decode(data, record["path"])


def parse_code_spec(spec: str) -> tuple[str, int | None, int | None]:
    """Split PATH or PATH:START-END; the range bounds against the file are checked by the caller."""
    match = _CODE_RANGE.match(spec)
    if match is None:
        return spec, None, None
    start, end = int(match.group(2)), int(match.group(3))
    if start < 1 or start > end:
        raise ExplainError(1, f"invalid code range: {spec}")
    return match.group(1), start, end


def _is_boundary(lines, i: int) -> bool:
    line = lines[i - 1].rstrip("\r\n")
    previous = lines[i - 2].strip()
    return line != "" and line[0] not in " \t" and previous == ""


def split_code(lines, start: int, end: int) -> list[tuple[int, int]]:
    """Cut the 1-based inclusive range into fragments of at most 120 lines, preferably at top-level lines."""
    ranges = []
    a = start
    while a <= end:
        if end - a + 1 <= _CODE_WINDOW:
            ranges.append((a, end))
            break
        limit = a + _CODE_WINDOW - 1
        cut = None
        for i in range(limit, a + _CODE_MIN_FRAGMENT - 1, -1):
            if _is_boundary(lines, i):
                cut = i
                break
        if cut is None:
            ranges.append((a, limit))
            a = limit + 1
        else:
            ranges.append((a, cut - 1))
            a = cut
    return ranges


def merge_ranges(ranges) -> list[tuple[int, int]]:
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _collect_code(code, repo_real: str, by_path: dict, previous=()) -> dict:
    """Map each selected file path to its merged line ranges (a whole-file spec covers everything)."""
    wanted = {}
    for path, start, end in previous:
        count = by_path[path]["lines"] or 0
        wanted.setdefault(path, []).append((start, end, start == 1 and end == count))
    for spec in code or []:
        path, start, end = parse_code_spec(spec)
        record = resolve_listed(path, repo_real, by_path)
        count = record["lines"] or 0
        if start is None:
            start, end = 1, count
        elif end > count:
            shown = "(withheld-path)" if find_secrets(spec) else spec
            raise ExplainError(1, f"code range out of bounds: {shown}")
        wanted.setdefault(record["path"], []).append((start, end, start == 1 and end == count))
    selected = {}
    for path, items in wanted.items():
        count = by_path[path]["lines"] or 0
        if any(whole for _start, _end, whole in items):
            selected[path] = [(1, count)] if count else []
        else:
            selected[path] = merge_ranges([(start, end) for start, end, _whole in items])
    return selected


def _code_fragments(path: str, source_id: str, lines, ranges, start_index: int) -> list[dict]:
    fragments = []
    number = start_index
    for range_start, range_end in ranges:
        for a, b in split_code(lines, range_start, range_end):
            text = "".join(lines[a - 1 : b])
            for chunk, line_start, line_end in _split_long(text, a, _CODE_FRAGMENT_CHARS):
                fragments.append(
                    {
                        "id": f"E{number}",
                        "source": source_id,
                        "anchor": f"{path}:L{line_start}-L{line_end}",
                        "line_start": line_start,
                        "line_end": line_end,
                        "text": chunk,
                        "flagged": False,
                        "withheld": False,
                    }
                )
                number += 1
    return fragments


def _withheld_text(fragment: dict) -> str:
    return (
        "[withheld by paseo-explain: possible secret in lines "
        f"{fragment['line_start']}-{fragment['line_end']}]\n"
    )


def _withhold_secrets(fragments) -> list:
    """Replace text of fragments whose text or anchor looks like a secret; return the scan flags."""
    flags = []
    for fragment in fragments:
        text_hit = bool(find_secrets(fragment["text"]))
        anchor_hit = bool(find_secrets(fragment["anchor"]))
        if not (text_hit or anchor_hit):
            continue
        if anchor_hit:
            fragment["anchor"] = "§(withheld)"
        fragment["withheld"] = True
        fragment["flagged"] = True
        fragment["text"] = _withheld_text(fragment)
        flags.append({"evidence": fragment["id"], "pattern": "secret", "excerpt": "(withheld)"})
    return flags


def _previous_selection(session_dir: Path):
    previous = repo.load_repomap(session_dir)
    selection = previous.get("selection") if isinstance(previous, dict) else None
    if not isinstance(selection, dict):
        return [], []
    docs = [item for item in selection.get("docs") or [] if isinstance(item, dict)]
    code = [item for item in selection.get("code") or [] if isinstance(item, dict)]
    return docs, code


def _still_same(by_path: dict, item: dict) -> bool:
    record = by_path.get(item.get("path"))
    return record is not None and record["kind"] == "text" and record["sha256"] == item.get("sha256")


def _codebase_ingest(session_dir: Path, session: dict, docs, code, reuse=False) -> dict:
    identity = session.get("identity") if isinstance(session.get("identity"), dict) else {}
    repo_real = identity.get("repo")
    if not isinstance(repo_real, str) or not os.path.isdir(repo_real):
        raise ExplainError(1, "repository directory not found")
    depth = session.get("depth") or "docs"
    if code and depth != "code":
        raise ExplainError(1, "--code requires depth code")
    listing = repo.list_repository(repo_real)
    by_path = {record["path"]: record for record in listing["files"]}

    doc_paths = _automatic_docs(listing, repo_real)
    seen = {_real(repo_real, path) for path in doc_paths}
    for spec in docs or []:
        record = resolve_listed(spec, repo_real, by_path)
        real = _real(repo_real, record["path"])
        if real not in seen:
            seen.add(real)
            doc_paths.append(record["path"])

    reused = 0
    dropped = []
    previous_code = []
    if reuse:
        old_docs, old_code = _previous_selection(session_dir)
        for item in old_docs:
            if not _still_same(by_path, item):
                dropped.append(str(item.get("path")))
                continue
            real = _real(repo_real, item["path"])
            if real not in seen:
                seen.add(real)
                doc_paths.append(item["path"])
                reused += 1
        for item in old_code:
            start, end = item.get("start"), item.get("end")
            shown = f"{item.get('path')}:{start}-{end}"
            valid = isinstance(start, int) and isinstance(end, int) and 1 <= start <= end
            if depth != "code" or not valid:
                if depth == "code":
                    dropped.append(shown)
                continue
            if not _still_same(by_path, item) or end > (by_path[item["path"]]["lines"] or 0):
                dropped.append(shown)
                continue
            reused += 1
            previous_code.append((item["path"], start, end))

    selected_code = _collect_code(code, repo_real, by_path, previous_code)

    manifest = repo.build_manifest(listing)
    manifest_bytes = manifest.encode("utf-8")
    entries = [("manifest", "(manifest)", manifest_bytes, manifest, None)]
    selection_docs = []
    for path in doc_paths:
        data, text = _read_listed(repo_real, by_path[path])
        entries.append(("doc", path, data, text, None))
        selection_docs.append({"path": path, "sha256": sha256_bytes(data)})
    selection_code = []
    for path in sorted(selected_code):
        data, text = _read_listed(repo_real, by_path[path])
        entries.append(("code", path, data, text, selected_code[path]))
        for start, end in selected_code[path]:
            selection_code.append(
                {"path": path, "start": start, "end": end, "sha256": sha256_bytes(data)}
            )

    sources = []
    fragments = []
    next_index = 1
    code_count = 0
    for number, (role, display, data, text, ranges) in enumerate(entries, start=1):
        source = {
            "id": f"S{number}",
            "role": role,
            "display": display,
            "sha256": sha256_bytes(data),
            "lines": _line_count(text),
        }
        if role == "code":
            raw_lines = [raw for _no, _content, raw in _logical_lines(_normalize(text))]
            produced = _code_fragments(display, source["id"], raw_lines, ranges, next_index)
            code_count += len(produced)
        else:
            if _has_atx(text):
                produced = fragment_blocks(split_blocks(text), source["id"], next_index)
            else:
                produced = fragment_paragraphs(text, source["id"], next_index)
            for fragment in produced:
                fragment["withheld"] = False
        next_index += len(produced)
        fragments.extend(produced)
        sources.append(source)

    if code_count > _CODE_FRAGMENT_LIMIT:
        raise ExplainError(1, f"too many code fragments: {code_count} (limit {_CODE_FRAGMENT_LIMIT})")
    secret_flags = _withhold_secrets(fragments)
    scan_doc = scan_fragments(fragments)
    for fragment in fragments:
        if fragment["withheld"]:
            fragment["flagged"] = True
    scan_doc["flags"] = secret_flags + scan_doc["flags"]
    structured = {
        "refs_present": False,
        "requirements": [],
        "tasks": [],
        "waves": [],
        "findings": [],
        "depth": depth,
        "repo": dict(listing["repo"]),
        "build_files": repo.build_files(listing),
        "entrypoints": repo.entrypoints(listing),
    }
    evidence = {
        "explain_schema": EXPLAIN_SCHEMA,
        "sources": sources,
        "fragments": fragments,
        "structured": structured,
    }
    size = len(json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8")) + 1
    if size > _EVIDENCE_LIMIT:
        raise ExplainError(1, "evidence too large")
    areas = repo.compute_areas(listing)
    repomap = repo.merge_repomap(
        repo.load_repomap(session_dir), listing, areas, {"docs": selection_docs, "code": selection_code}
    )
    write_json(session_dir / "evidence.json", evidence)
    write_json(session_dir / "scan.json", scan_doc)
    repo.write_repomap(session_dir, repomap)

    code_files, code_bytes = repo.code_totals(listing["files"])
    survey = (
        depth == "code"
        and (code_files > 60 or code_bytes > 300000)
        and any(area["state"] != "fresh" for area in repomap["areas"])
    )
    return {
        "ok": True,
        "sources": len(sources),
        "fragments": len(fragments),
        "flagged": sum(1 for fragment in fragments if fragment["flagged"]),
        "withheld": sum(1 for fragment in fragments if fragment["withheld"]),
        "depth": depth,
        "files": len(listing["files"]),
        "code_files": code_files,
        "code_bytes": code_bytes,
        "survey_recommended": survey,
        "areas": [
            {"id": area["id"], "files": area["files"], "lines": area["lines"], "state": area["state"]}
            for area in repomap["areas"]
        ],
        "reused": reused,
        "dropped": dropped,
        "structured": {"requirements": 0, "tasks": 0, "findings": 0, "refs_present": False},
    }


def ingest(
    session_dir, *, autopilot=None, doc=None, files=None, text_file=None, docs=None, code=None, reuse=False
) -> dict:
    session_dir = Path(session_dir)
    session = _load_checked_session(session_dir)
    if session.get("kind") == "codebase":
        if autopilot is not None or doc is not None or files or text_file is not None:
            raise ExplainError(2, _CODEBASE_ONLY)
        return _codebase_ingest(session_dir, session, docs, code, reuse)
    if docs or code or reuse:
        raise ExplainError(2, _NOT_CODEBASE)

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
