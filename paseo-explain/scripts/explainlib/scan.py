"""Case-insensitive scan for untrusted-content patterns in evidence fragments."""

import re

from explainlib import EXPLAIN_SCHEMA

_SPECS = [
    (
        "override",
        r"\b(ignore|disregard|forget)\b[^.\n]{0,40}\b(previous|prior|above|earlier|all|your)\b[^.\n]{0,20}\b(instructions?|rules?|prompts?)\b",
    ),
    ("override", r"\byou are now\b"),
    ("override", r"\bnew instructions?\b"),
    ("override", r"\bsystem prompt\b"),
    (
        "address-agent",
        r"\b(assistant|agent|explainer|model|ai|fact-?checker|reader)\b[,:]?\s+(you\s+must|must\s+now|should\s+now|please|ignore|do\s+not)\b",
    ),
    (
        "steer-explain",
        r"\b(mark|label|rate)\b[^.\n]{0,30}\b(verified|confirmed|correct)\b",
    ),
    ("steer-explain", r"\bskip\b[^.\n]{0,30}\b(fact-?check|verification|check)\b"),
    (
        "steer-explain",
        r"\b(say|write|state)\b[^.\n]{0,30}\b(this plan|the plan|it)\b[^.\n]{0,20}\b(is|as)\b[^.\n]{0,20}\b(safe|approved|complete|flawless)\b",
    ),
    ("exfiltrate", r"\b(fetch|curl|wget|open|visit)\b[^.\n]{0,40}https?://"),
    ("exfiltrate", r"\bsend\b[^.\n]{0,40}\b(to|via)\b[^.\n]{0,40}(https?://|webhook)"),
    ("hidden", r"<!--"),
    ("hidden", "[\u200b\u200c\u200d\u2060\ufeff]"),
    ("hidden", "[\u202a-\u202e\u2066-\u2069]"),
    ("hidden", r"[A-Za-z0-9+/]{120,}={0,2}"),
]

PATTERNS: list[tuple[str, re.Pattern]] = [
    (name, re.compile(expr, re.IGNORECASE)) for name, expr in _SPECS
]


def scan_text(text: str) -> list[dict]:
    hits = []
    for name, pattern in PATTERNS:
        for match in pattern.finditer(text or ""):
            hits.append({"pattern": name, "start": match.start(), "end": match.end()})
    return hits


def _excerpt(text: str, start: int, end: int) -> str:
    start = max(0, min(start, len(text)))
    end = max(start, min(end, len(text)))
    if end - start >= 160:
        return text[start : start + 160]
    begin = start - min(start, (160 - (end - start)) // 2)
    finish = min(len(text), begin + 160)
    begin = max(0, finish - 160)
    return text[begin:finish]


def scan_fragments(fragments) -> dict:
    flags = []
    for fragment in fragments:
        text = fragment.get("text") or ""
        hits = scan_text(text)
        fragment["flagged"] = bool(hits)
        for hit in hits:
            flags.append(
                {
                    "evidence": fragment["id"],
                    "pattern": hit["pattern"],
                    "excerpt": _excerpt(text, hit["start"], hit["end"]),
                }
            )
    return {"explain_schema": EXPLAIN_SCHEMA, "flags": flags}
