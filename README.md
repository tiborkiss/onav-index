# onav-index

**A BMad skill that turns a project's canonical artifacts into a navigable Obsidian note graph — every codified entity (`FR`, `AD`, `SM`, `CAP`, `NFR`, `Epic`, `Story`) becomes a backlinked note, navigable in both directions.**

BMad projects accumulate dense cross-references (`FR-4`, `AD-6`, `E2.5`, `SM-2`…) that are compact in prose but painful to navigate in long documents — following any one means searching several files to recover the relationship. `onav-index` generates a derived note-graph layer where each entity is a note and every relationship is a one-click wiki-link. Open `FR-4` and see, in the backlinks panel, every AD that governs it, every story that realizes it, and every SM that validates it.

The canonical files remain the **single source of truth**. The note graph is a regenerable navigation + annotation layer — never a second source.

## Features

- **7 entity types** parsed from all canonical files (PRD, architecture spine, SPEC, epics) with relationships derived bidirectionally — AD `Binds`, SM `Validates`, CAP success-criteria, story acceptance criteria, and the FR Coverage Map.
- **Three-tier freshness model**: live Dataview queries for relationship lists (always current), static + `last_reviewed` for canonical-derived definitions (drift-tracked), and a preserved `## Personal notes` section for your annotations.
- **Drift-acceptance, not drift-prevention**: notes are navigation aids, not canonical. `last_reviewed` + `source_sha` surface staleness; the dashboard shows what's drifted without forcing sync.
- **Dataview dashboard** — staleness, orphans, coverage gaps, and hotspots, rendered live. Plus a **Canvas** visual layout (epic → story → FR).
- **Personal-notes preservation** — your `## Personal notes` survive every regen (`init`, `update`, `refresh`). This is the trust foundation that makes drift-acceptance viable.
- **Turbovault-safe** — `--emit-mode manifest` outputs a write manifest for the mediating agent to apply via turbovault MCP, so LiveSync/git-managed vaults stay consistent. Direct file-IO fallback for everyone else.
- **Project-agnostic** — one installed skill serves every BMad project. Project context auto-detects from `_bmad/config.*`.

## Requirements

- **[`uv`](https://docs.astral.sh/uv/)** (required) — runs the generator and resolves its single dependency (`pyyaml`) automatically from the script's inline metadata.
- **Python 3.11+** (via `uv`).
- **[Obsidian](https://obsidian.md)** + the **[Dataview](https://github.com/blacksmithgu/obsidian-dataview)** plugin — required for the navigation value (backlinks, live queries, Canvas). Without Dataview, queries render as code blocks; the native backlinks panel still works.
- A **BMad project** with canonical artifacts (PRD, architecture spine, SPEC, `epics.md`).

## Install

`onav-index` is a self-registering BMad skill. Add it to your project's skills location, then run its setup once.

**As a git submodule** (recommended for tracked projects):

```bash
git submodule add git@github.com:tiborkiss/onav-index.git <skills-dir>/onav-index
```

**Or clone directly** into your skills folder.

Then invoke the skill with `setup` (or `configure`) to register the module and set your vault root — see [Configuration](#configuration).

## Quick start

```bash
# 1. Configure (once): sets onav_vault_root + preferences.
#    Invoke the onav-index skill with `setup`, or edit _bmad/config.yaml directly:
#      modules:
#        onav:
#          onav_vault_root: /path/to/your/vault

# 2. Index the project into the vault (105 notes from a real project):
uv run scripts/gen_index.py --project-root . --vault-root /path/to/your/vault init --force

# 3. Open <vault>/projects/<project-slug>/index.md in Obsidian.
```

If your vault is **turbovault- or LiveSync-managed**, use manifest mode so the agent applies writes via MCP (raw file writes desync those systems):

```bash
uv run scripts/gen_index.py --vault-root /path/to/vault --emit-mode manifest init --force
# → apply the resulting JSON manifest via turbovault_write_note(...)
```

## Commands

The generator (`scripts/gen_index.py`) has four subcommands. Global flags — `--project-root`, `--vault-root`, `--emit-mode {file,manifest}`, `-H/--headless` — go before the subcommand.

| Command | Purpose |
| --- | --- |
| `init` | Cold-start: emit every derivable entity note + dashboard + Canvas + MOC. `--force` overwrites an existing project dir; `--dry-run` previews. |
| `update <ID> [<ID>...]` | **The workhorse.** Targeted refresh by ID, then a backlink-crawl suggestion list (affected notes + missing-note gaps) for the agent to confirm. `--yes-all` cascades one level. |
| `refresh` | Full regenerate-all with pruning. Leftovers whose entity is gone from canonical are pruned — *unless* they carry `## Personal notes`, which are flagged, never deleted. Writes a refresh-log. |
| `doctor` | Environment check: uv, vault root, Dataview plugin, canonical files. |

## How it works

**The mediating-agent pattern.** You talk to whichever BMad agent is loaded; it runs the generator and handles the conversation (presenting the suggestion list, collecting `[All | None | IDs]` confirmations). The generator owns the deterministic work a prompt does unreliably — parsing canonical files, deriving relationships, emitting notes. Clean separation: the script handles files, the agent handles conversation.

**Three-tier freshness** aligns each content type with its natural drift behavior:

| Tier | Content | Why |
| --- | --- | --- |
| Live (Dataview) | Relationship lists (`### Referenced by`) | Re-evaluated on every open; new entities appear with no regen. |
| Static + `last_reviewed` | Canonical-derived definitions, outgoing `### References` | Drift-tracked via `source_sha`; staleness is visible. |
| Manual | `## Personal notes` | Preserved across every rewrite — the trust foundation. |

**Non-invasive.** Reads `_bmad-output/` and `_bmad/config.*`; writes only to the configured vault path. Never touches BMad internals — BMad version updates cannot break it.

## Configuration

Config keys live under `[modules.onav]` (in `_bmad/config.yaml`, or `.toml` — onav reads both). Set via the skill's `setup`/`configure` action, or edit directly.

| Key | Default | Meaning |
| --- | --- | --- |
| `onav_vault_root` | *(required)* | Absolute path to your Obsidian vault root. Emit path: `<vault>/<projects_subfolder>/<project-slug>/`. |
| `onav_projects_subfolder` | `projects` | Subfolder under the vault root for indexed BMad projects. Accepts nesting (e.g. `projects/YourOrg`) for an org-scoped layout. |
| `onav_project_slug` | *(auto-derived)* | Exact-case override for this project's leaf folder name — the default auto-derives a lowercase-kebab slug (`ToF-Tracking-WS` → `tof-tracking-ws`). A `--project-slug` CLI flag overrides per-invocation. |
| `onav_prefer_turbovault` | `true` | Prefer manifest mode / turbovault for vault writes; falls back to direct file editing. |
| `onav_stale_days` | `14` | Days after which an unreviewed note counts as stale on the dashboard. |

**Org-nested, case-preserving layout** — combine both to land at e.g. `<vault>/projects/BlendArtis/ToF-Tracking-WS/` instead of the default `<vault>/projects/tof-tracking-ws/`:

```bash
uv run scripts/gen_index.py --project-root . --vault-root /path/to/vault \
  --project-slug "ToF-Tracking-WS" --projects-subfolder "projects/BlendArtis" init --force
# or set onav_project_slug / onav_projects_subfolder in config to make it permanent.
```

Project name + canonical paths are **not** config — they auto-resolve from the host project's BMad config.

## Example entity note

```markdown
---
id: FR-4
type: FR
title: Stable bridge upstream contract
tags: [onav/FR, onav/stable]
source: _bmad-output/.../prd.md
source_anchor: FR-4
source_sha: 80a055fa7c50
status: stable
last_reviewed: '2026-08-10T05:37:41Z'
generator: onav-index (M7)
---
# FR-4 — Stable bridge upstream contract

The Bridge's upstream interface accepts only crop/calibration commands and
emits only timestamped frames, calibration data, and per-zone stats…

## Relationships

### References
- [[E1b|E1b]] — Integrated Streaming — GPDMA + SPI-slave + raw `3DMD`

### Referenced by
```dataview
LIST WITHOUT ID link(file.name, file.name) + " — " + title
FROM [[]] AND "projects/my-project"
WHERE file.name != this.file.name
SORT type ASC, file.name ASC
```
```

Opening this note in Obsidian shows every AD, SM, CAP, and Story that references `FR-4` — the exact relationship-search that was slow in prose.

## Status & scope

**v0.1.0** — initial release. Functional and validated against a real BMad project (105 entity notes across 7 types). Known deferral: **Streams** (protocol-spec wire-format transactions) — their canonical structure is irregular and earns a dedicated follow-up pass. The seven core entity types already deliver the dense cross-reference graph.

## License

[MIT](LICENSE) © Tibor Kiss
