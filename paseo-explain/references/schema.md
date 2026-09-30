# Explanation schema

Read this while authoring `explain.json`; the JSON schemas are documentation and `validate.py` is the runtime authority.

All top-level fields are required, and unknown keys are rejected. `explain_schema` is 1; `kind` is `plan` or `idea` and matches the session; `lang` is a language code; `title` names the page; `levels` equals the session's written levels. `lead` is an evidence-bearing headline with `text`, `evidence` and `confidence`. `facts` contains short `label`/`value` pairs with the same evidence and confidence fields. `hero` contains a `caption`, canvas `height`, `zones`, `nodes`, `edges` and walkthrough `steps`. `sections` contains 3–8 typed sections. `glossary` contains `term`/`definition` entries.

A leveled text is a string shared across levels, or an object with exactly the written-level keys (`1`, `3`, `5`, or all of `1`, `2`, `3`, `4`, `5`). Never omit a written level. For plans, `confidence` is `confirmed`, `inferred` or `unknown`. Ideas also allow `user_statement` and `assumption`. `confirmed` and `user_statement` need evidence ids from `evidence.json`; evidence ids used anywhere must exist. A `quote` has an `evidence` id and verbatim `text` from that fragment.

## Structure

A `zone` has `id`, `label`, `tone`, `x`, `y`, `w`, `h`. A hero node has `id`, `label`, optional `sub`, `tone`, optional `zone`, `x`, `y`, optional `w`, `detail`, `evidence`, `confidence`. An edge uses `from`, `to`, optional `label`, `tone`; a step uses `nodes`, `from`, `to`, `text`, `evidence`. Tone is `existing`, `new` or `external` on nodes and zones, and `existing` or `new` on edges. Node and zone ids are slugs; references must resolve.

Every section has `id`, `type`, `title`, optional `subtitle`, `evidence` and `confidence`. A `prose` section adds `body`; `change` adds `now`, `next`, `unchanged` lists whose entries have `text`, `evidence`, `confidence`, optional `quote`; `diagram` adds `mermaid`, `caption`, `alt`; `coverage` adds `requirements` and `tasks`. A requirement has `id`, `text`, `tasks` ids, `test` (`yes`, `no`, `unknown`), `evidence`, `confidence`; a task has `id`, `label`, `evidence`. A `decisions` section has `items` with `status` (`chosen` or `rejected`), `title`, `why`, `evidence`, `confidence`. A `risks` section has `items` with `kind` (`risk`, `open_question`, `assumption`), `title`, `text`, `evidence`, `confidence`. A `quiz` section has `items` with `q` and `a`. Plan-only types are `change` and `coverage`.

## Caps

| Field | Limit |
|---|---|
| `title`, section `title`, decision/risk `title` | 80 characters |
| `lead.text` | 90 words per level |
| `facts` | 0–6; `label` 24 chars, `value` 40 chars |
| `hero` | `height` 200–600; 0–4 `zones`, 2–9 `nodes`, 0–12 `edges`, 0–8 `steps` |
| Zone `label`; node `label`, `sub`, `w` | 32 chars; 28 chars; 36 chars; width 120–320 |
| Node `detail`; step `text`; hero `caption` | 80; 40; 40 words per level |
| `sections`; section `subtitle`; prose `body` | 3–8; 40; 250 words per level |
| Change lists and item `text` | 0–6 each, at least one total; 40 words per level |
| Diagram `mermaid`, `alt`, `caption` | 3000 chars, 40 nonempty lines, 25 flowchart nodes; 1–400 chars; 40 words |
| Coverage `requirements`, `tasks`; requirement `text` | 1–30; 1–40; 120 chars |
| Decision, risk, quiz `items` | 1–6, 1–8, 1–4 respectively |
| Decision `why`, risk `text`, quiz `q`/`a` | 80, 80, 40/80 words per level |
| `glossary`; `term`, `definition`; `quote.text` | 0–20; 40/240 chars; 300 chars |
| Complete `explain.json` | 200,000 UTF-8 bytes |

Canvas coordinates use 0–1000 horizontally and 0–`height` vertically. Mermaid begins with `flowchart`, `graph`, `sequenceDiagram`, `stateDiagram-v2`, `mindmap` or `timeline`. It cannot contain `<`, `click `, `%%{`, `javascript:`, `href`, `callback` or `call ` (case-insensitive). Every `evidence` reference must resolve; `quote.text` must match verbatim after CRLF-to-LF normalization. The validator warns above two `new` hero nodes or 1800 total words at the default level.

## Minimal plan example

Prepare a plan session with `init --kind plan --slug garden-example --mode quick`, `frame --audience "Garden members" --question "How are plots booked?"`, and `ingest --file plan=<a text file containing: Garden members book plots. One booking per plot. The board lists free plots.>`. The resulting first fragment is `E1`. Put this JSON in its `explain.json`, then run `validate --session`.

```json
{
  "explain_schema": 1, "kind": "plan", "lang": "en", "title": "Garden booking", "levels": [1, 3, 5],
  "lead": {"text": "Garden members book plots one at a time.", "evidence": ["E1"], "confidence": "confirmed"},
  "facts": [],
  "hero": {
    "caption": "A member checks availability before booking.", "height": 300, "zones": [],
    "nodes": [
      {"id": "member", "label": "Member", "tone": "existing", "zone": null, "x": 80, "y": 100, "detail": "A member wants a plot.", "evidence": ["E1"], "confidence": "inferred"},
      {"id": "plot", "label": "Plot", "tone": "new", "zone": null, "x": 400, "y": 100, "detail": "One booking per plot.", "evidence": ["E1"], "confidence": "confirmed"}
    ],
    "edges": [{"from": "member", "to": "plot", "tone": "new"}], "steps": []
  },
  "sections": [
    {"id": "purpose", "type": "prose", "title": "Purpose", "evidence": ["E1"], "confidence": "confirmed", "body": "Garden members book plots."},
    {"id": "change", "type": "change", "title": "The change", "evidence": ["E1"], "confidence": "confirmed", "now": [], "next": [{"text": "Only one booking is allowed per plot.", "evidence": ["E1"], "confidence": "confirmed"}], "unchanged": []},
    {"id": "question", "type": "quiz", "title": "Check yourself", "evidence": [], "confidence": "inferred", "items": [{"q": "How many bookings per plot?", "a": "One."}]}
  ],
  "glossary": []
}
```

## Minimal idea example

Prepare an idea session with `init --kind idea --slug shelf-example --mode quick`, `frame --audience "Neighbours" --question "How could tool lending work?"`, and `ingest --text-file <a text file containing: Neighbours could share tools on a shelf. A volunteer could check returns.>`. The first paragraph becomes `E1`. Put this JSON in `explain.json`, then run `validate --session`.

```json
{
  "explain_schema": 1, "kind": "idea", "lang": "en", "title": "Tool lending shelf", "levels": [1, 3, 5],
  "lead": {"text": "Neighbours could share tools on a shelf.", "evidence": ["E1"], "confidence": "user_statement"},
  "facts": [],
  "hero": {
    "caption": "A shelf links neighbours and tools.", "height": 300, "zones": [],
    "nodes": [
      {"id": "shelf", "label": "Shelf", "tone": "new", "zone": null, "x": 80, "y": 100, "detail": "The shelf holds shared tools.", "evidence": ["E1"], "confidence": "inferred"},
      {"id": "neighbour", "label": "Neighbour", "tone": "external", "zone": null, "x": 400, "y": 100, "detail": "A neighbour borrows a tool.", "evidence": ["E1"], "confidence": "inferred"}
    ],
    "edges": [{"from": "shelf", "to": "neighbour", "tone": "new"}], "steps": []
  },
  "sections": [
    {"id": "purpose", "type": "prose", "title": "Purpose", "evidence": ["E1"], "confidence": "user_statement", "body": "Neighbours could share tools on a shelf."},
    {"id": "mechanism", "type": "prose", "title": "How it works", "evidence": ["E1"], "confidence": "inferred", "body": "A volunteer could check returns."},
    {"id": "question", "type": "quiz", "title": "Check yourself", "evidence": [], "confidence": "inferred", "items": [{"q": "Where are tools kept?", "a": "On a shared shelf."}]}
  ],
  "glossary": []
}
```
