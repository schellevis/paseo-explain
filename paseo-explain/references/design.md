# Design and writing

Read this while choosing the story, diagram and language of an explanation.

Aim for density “4/10: enough to be complete, not so dense it needs a guide”. Put the concrete outcome first and keep each section to one claim. The lead states the problem or the outcome the reader will see, not the order of work. The first section after the lead is the `example` section: one concrete case, told as situation, today and afterwards, before any internal mechanism. The hero has at most nine nodes; “one or two accent (`new`) nodes carry the story; use `new` only for what the plan adds”. Distinguish existing, new and external zones and nodes. Use no more than one diagram per section. Every caption states the claim of its figure.

For Mermaid, use `flowchart`, `graph`, `sequenceDiagram`, `stateDiagram-v2`, `mindmap` or `timeline`. Always quote node labels as `id["label"]`. Never include `click`, `%%{init}` or `<`. Stay within 25 nodes and 40 nonempty lines; use top-down layout for narrow content. Provide an `alt` text equivalent, since diagrams may fall back to source text offline or on parse failure.

At level 1, Simple (B1), use sentences of at most 15 words, no unexplained jargon, and a concrete example first. At level 2, Accessible, keep short sentences and define necessary terms. At level 3, Mixed, connect the example to the main mechanism and decision. At level 4, Technical, name interfaces and constraints precisely while retaining signposts. At level 5, Expert, use precise terms, no analogies, and dense but traceable detail. Write the same supported claims at every written level; change explanation depth, not certainty.

The page uses a dark default with a light toggle, system fonts, evidence chips and a source side panel. Colour should identify existing, new, external, risk and open-question meaning without being the only cue. Keep labels short and supply text equivalents. The visual reference's palette, shape and depth tokens were adapted from `konraddzbik/architecture-diagram-skill` (MIT, Copyright (c) 2026 Konrad Dzbik). Density and accent rules credit `cathrynlavery/diagram-design` (MIT, Copyright (c) 2025 Cathryn Lavery); the validated-spec rendering and fact-check ideas credit `nicobailon/visual-explainer` (MIT, Copyright (c) 2025 Nico Bailon). The latter two are idea credits, with no copied code; see [third-party licences](../../THIRD_PARTY_LICENSES.md).

For a codebase page, the hero shows the main parts and one flow through them, using only the tones `existing` and `external`: a codebase describes what exists, so nothing is `new`. `validate` warns “codebase hero uses tone new” when a hero node, zone or edge has tone `new`. Prefer the `map` section for parts and the `start` section for how to run and test.

## Plain words

- Write details, steps, items and captions as full sentences with a subject and a verb, never as comma lists of nouns.
- Never use an internal code (task id, codename, function or file name) as the main name of a thing. Say what it does in plain words and put the code in parentheses at most. `validate` warns when a hero label, zone label or section title starts with a code.
- Every term a level-1 reader would not know goes in the glossary or is explained in the sentence itself. `validate` warns about unexplained acronyms and codes at level 1.
- Bad: node label `T2 store`, detail `Lock, retry, audit.` Good: label `Saves the booking (T2)`, detail `The booking is saved once; a second save for the same plot is refused.`
