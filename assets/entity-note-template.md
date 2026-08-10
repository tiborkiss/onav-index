---
id: <TYPE>-<n>
type: <TYPE>
title: <title from canonical heading>
tags: [onav/<TYPE>, onav/stable]
source: <project-relative path to the canonical file>
source_anchor: <the entity ID — for a path#anchor pointer>
source_sha: <first 12 hex of sha256 of the canonical file — drift detection>
status: stable
last_reviewed: <ISO 8601 UTC — the staleness indicator; manually editable>
generator: onav-index (M3)
---

<!-- onav-index generated note. Canonical files are the source of truth;
this note is a regenerable navigation layer. The 'Referenced by' list is a
LIVE Dataview query — install the Dataview plugin for it to render
(Obsidian's backlinks panel is the no-plugin fallback). Add personal
annotations under a '## Personal notes' heading — preserved from M4 onward. -->

# <ID> — <title>

<definition: the distilled canonical summary (tier 2 — static, drift-tracked
via source_sha + last_reviewed)>

## Relationships

### References

- [[<other-entity-ID>]] — <other entity's title>   (outgoing — IDs cited in
                          THIS entity's canonical section. The ID stays the
                          literal link target; the title is plain text
                          appended on the same line, looked up from the full
                          current entity set. A reference to an ID with no
                          known title — a missing-note gap — renders bare.
                          Static, because it is canonical-derived and only
                          changes on regen.)

### Referenced by

```dataview
LIST " — " + title
FROM [[]] AND "projects/<slug>"
WHERE file.name != this.file.name
SORT type ASC, file.name ASC
```

<!-- The Dataview query above is tier 1 — LIVE: every entity whose static
References list links to this note appears here as "ID — Title", sorted by
type then name, with no regen needed. Renders as a code block without the
Dataview plugin; the native backlinks panel is the universal fallback.

Sections below arrive in later milestones:

## Personal notes            (M4 — preserved across EVERY rewrite: init, init
                             --force, update, refresh. This is the trust
                             foundation of the drift-acceptance model — the
                             emitter reads this section before regenerating and
                             re-appends it verbatim. Edit freely.)
-->


## About this template

This document is the **contract** for the entity-note shape — the single
description of what `gen_index.py` emits. It serves three purposes:

1. **M3 reference (current).** The generator renders notes matching the shape
   above: frontmatter with `tags` taxonomy, the definition, static `### References`
   (tier 2), and a live Dataview `### Referenced by` query (tier 1). The
   string-formatting in `gen_index.py::emit_entity_note` is the source of truth.

2. **M3 target.** When entity-note rendering grows rich (Dataview queries,
   tag taxonomy, preserved `## Personal notes`), this template becomes the
   Jinja2 source the emitter renders against. At that point the string
   formatting in `gen_index.py::emit_entity_note` is replaced by
   `jinja2.Template(...).render(...)`, and this file is loaded at runtime.

3. **Three-tier freshness contract.** The template encodes the drift model:
   - **Live (Dataview, M3)** — relationship lists that change as the graph
     grows; re-evaluated on every note open, no regen needed.
   - **Static + `last_reviewed` (M1+)** — the canonical-derived definition;
     drift-tracked via the `source_sha` + `last_reviewed` fields.
   - **Manual (M4)** — the `## Personal notes` section; never overwritten.

## Field semantics

| Field | Purpose | Drift behavior |
| --- | --- | --- |
| `source` + `source_anchor` | Path pointer to the canonical ground truth. Works whether or not canonical lives in the vault. | Stable. |
| `source_sha` | First 12 hex of the canonical file's sha256 at generation time. Compare to the file's current sha to detect drift. | Updated only on regen. |
| `last_reviewed` | ISO 8601 UTC timestamp of the last regen *or* manual review bump. **The central staleness indicator** — manually editable in Obsidian's Properties panel. | User-editable; drives the dashboard's staleness view (M6). |
| `status` | `stable` / `draft` / `needs-update`. Set to `stable` on regen; the user flips to `needs-update` during use. | Manual after regen. |
| `generator` | Which onav milestone produced this note. Useful when re-reading notes emitted by an older generator version. | Updated per regen. |
