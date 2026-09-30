# Explanation pipeline

Read this when running an explanation from intake through checks, rendering and updates.

Every claim points at evidence, every quote is verbatim, and what is not known is said to be unknown.

## FRAME

Choose `kind=plan` for a plan, spec, pair, or autopilot input; otherwise choose `idea`. Record a one-line audience, one-sentence reader question, out-of-scope list, mode, written levels, default level and language. Unless `--unattended`, ask one question combining the audience and “which question should this explanation answer?”, offering inferred values as the default. Under `--unattended`, infer all values. For autopilot, use audience “the person who must approve this document” and question “what will this change, why this way, and what is still open?” Run `explain.py init`, then `explain.py frame --orchestrator-model <your provider/model>` with the brief fields; pass the model on every frame so check independence can be established. `--out` is an export directory, not the session location.

## GATHER

For an idea, save the user's idea text verbatim to `<session>/input/idea.md` first. Run `explain.py ingest` with the source files or text file. Read `evidence.json` and `scan.json`, including every flagged fragment. Flags do not block work; surface them in the summary and result.

## MODEL

In chat-internal reasoning, make a ledger of what this is, main units and unknowns; do not write a ledger file. Choose 3–9 concepts, each backed by an evidence id and connected to another concept. For plans, prefer requirements and acceptance criteria over design/interfaces, task text, rationale prose and titles, in that order. For ideas, mark statements `user_statement`, `assumption` or `inferred` according to their origin.

## OUTLINE

Use 3–8 sections, one claim each, omitting empty topics. For plans, prefer problem/outcome → what changes (now/next/unchanged) → worked sequence → foundations-first build order → coverage → decisions and rejected alternatives → risks/open questions → check yourself. For ideas, prefer problem → core idea → mechanism with an example → trade-offs/alternatives → assumptions/unknowns → check yourself. Do not pad.

## DRAFT and VALIDATE

Write `<session>/explain.json` using [schema.md](schema.md). Fill every written level in each leveled field, including lead and supporting details; cite ids in `evidence.json` and copy quotes verbatim. Run `explain.py validate --session S`, fix reported errors and retry no more than three times. If errors remain, stop and tell the user; `result.json` must report `failed`.

## READER-TEST, then FACT-CHECK

In deep mode, run READER-TEST and its one possible revision **before** FACT-CHECK, so the fact-check covers final text. Standard mode runs only FACT-CHECK. Quick mode and `--no-delegate` run neither and ignore old reports.

For READER-TEST, record 3–5 check questions with expected answers via `explain.py frame --check-question "Q::EXPECTED"`, then validate. `explain.py check-prepare --kind reader` writes `checks/reader.md` without quiz answers and a request containing questions only, never expected answers. Launch one reader with [the reader prompt](prompts.md); it reads only that Markdown. Put the request's `explain_sha256` and `level` literally into the prompt. Run `explain.py check-report --kind reader`, grade answers yourself against expected answers, then `explain.py grade --correct N`. Revise once if any answer is wrong, or the report lists undefined terms or hard-to-follow passages; validate after revision.

For FACT-CHECK, validate and run `explain.py check-prepare --kind factcheck`; this freezes `checks/factcheck-explain.json` and lists every claim-bearing leaf. Launch one fact-checker with [the fact-checker prompt](prompts.md), pointing it to that frozen copy. Wait for its report, then run `explain.py check-report --kind factcheck`. Apply report changes only through `explain.py apply-corrections [--remove UNIT]…`, which writes corrections verbatim and relabels unsupported units `unknown` unless you request allowed removals. Validate afterwards. Never hand-edit after the check: another edit makes it stale and requires a new fact-check for a current check label. A reconciled result honestly counts corrections applied after checking.

If a configured agent cannot run or times out, use `explain.py skip --step fact_check|reader_test --reason "<why>"`; the page and result show the omission.

## Agent selection and waiting

Choose models in this order: `--models` JSON (`{"fact-checker":"provider/model[:thinking]","reader":"provider/model[:thinking]"}`), a fitting configured Paseo profile, then `list_providers`/`list_models` discovery. Prefer a different model family from your own; the reader may be cheaper. Use the lowest adequate thinking level (fact-checker medium, reader low). Keep one fallback per role on another family. On a launch rejection, try that fallback once; do not retry the rejected model in this session. If only your family is available, continue and label the page “Checked by the same model family”.

Create agents using `create_agent` or `paseo run` in a write-capable, non-prompting mode, never plan/read-only mode. Set `notifyOnFinish: true` and labels `paseo-explain.session=<slug>` and `paseo-explain.role=<fact-checker|reader>`. Confirm activity (first turn or tool call) within 60 seconds. Poll at most every 60 seconds; check pending permissions first with `paseo permit ls --json` or the MCP equivalent. Approve only reads of the session and listed sources or writes to the assigned report path; deny other requests. Stop an agent with no new activity for 10 minutes or no report after 20 minutes, and mark the step skipped. Archive each agent as soon as its report is ingested.

## RENDER, SHOW and update

Run `explain.py render`, then follow [display.md](display.md). For updates, edit `explain.json`, validate, render and re-show. Content changes increment the round. Any edit beyond corrections applied by `apply-corrections` makes a prior fact-check stale; re-run FACT-CHECK for changed claims in standard or deep mode.

## Session files

| File | Writer | Purpose |
|---|---|---|
| `session.json` | `init`, `render`, `grade`, `skip`, `show`, `tab` | Identity, options, round, URLs and state |
| `brief.json` | `frame` | Audience, question, scope and expected reader answers |
| `input/idea.md` | explainer | Verbatim idea text |
| `evidence.json` | `ingest` | Sources, fragments, structured references |
| `scan.json` | `ingest` | Untrusted-content flags |
| `explain.json` | explainer | Authored explanation |
| `validate.json` | `validate` | Errors, warnings and three content hashes |
| `checks/factcheck-request.json` | `check-prepare` | Claim units, leaves and binding hashes |
| `checks/factcheck-explain.json` | `check-prepare` | Frozen explanation for fact-checker |
| `checks/applied.json` | `apply-corrections` | Reconciled edit record |
| `checks/reader-request.json` | `check-prepare` | Reader questions, level and binding hash |
| `checks/reader.md` | `check-prepare` | Reader-only Markdown without answers |
| `factcheck.json` | fact-checker, `check-report` | Validated fact-check report |
| `reader.json` | reader, `check-report` | Validated reader report |
| `explain.html`, `explain.md` | `render` | Page and Markdown twin |
| `result.json` | `render`, `show`, `tab`, `result` | Caller contract and check state |
