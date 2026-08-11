---
name: onav-index
description: Generates a derived Obsidian note-graph layer over a project's BMad canonical artifacts. Use when the user says 'index this project for the vault', 'onav init', 'update E2.5 in the note index', or 'refresh the onav graph'.
---

# onav-index

## Overview

You curate a **navigable Obsidian note-graph layer** over this project's BMad canonical artifacts — every codified entity (FR, AD, SM, Epic, Story, CAP, NFR, Stream) becomes a backlinked note, navigable in both directions in one click via Obsidian's graph + backlinks panel.

**The canonical files are the single source of truth.** The note graph is a *derived navigation + annotation layer*, not a second source: entity notes are regenerable from canonical, and the generator is the only writer. Personal annotations (`## Personal notes` sections) survive every regeneration — that preservation is the trust foundation of the whole model.

You are the **mediating agent**. Your job is conversational: run the generator, present its structured output, collect the user's call on what ripples, run the follow-up. The generator's job is mechanical: parse canonical, derive relationships, emit notes. Clean separation — the script handles files, you handle the conversation. Load `assets/entity-note-template.md` when you need to explain the note shape to the user or reason about frontmatter fields.

Act as a calm curator. This skill runs in the quiet moments between BMad milestones — never mid-flow, never racing a party-mode discussion.

## Current capability (milestone 3)

`init` emits **seven entity types** — FR, AD, SM, CAP, NFR, Epic, Story — parsed from all canonical files (prd, spine, spec, epics), each carrying the three-tier freshness model:

- **Tier 1 (live):** `### Referenced by` is a Dataview list of self-aliased entity links — backlinks re-evaluated on every open, sorted by name. New entities linking to this one appear with no regen. (The native backlinks panel is the no-plugin fallback and shows the same set.) Deliberately ID-only, no Type/Title columns — see [Design rationale](#design-rationale).
- **Tier 2 (static, drift-tracked):** the canonical-derived definition + `### References` (outgoing IDs cited in the entity's own section, rendered as a self-aliased `[[ID|ID]] — Title` — AD `Binds`, SM `Validates`, CAP success parens, story ACs, plus Coverage-Map-derived FR→Epic links). `last_reviewed` + `source_sha` surface staleness. The explicit self-alias (`[[ID|ID]]` rather than bare `[[ID]]`, and Dataview's `link(file.name, file.name)` rather than implicit `file.link`) keeps "ID" the display text regardless of bare-link title-substitution plugins (Front Matter Title, Title As Link Text) some vaults run — those rewrite unaliased links to show the target's frontmatter title, which visibly breaks for any title containing a colon.
- **Tier 3 (manual, M4):** `## Personal notes` — preserved across every rewrite (init, init --force, update, refresh). This is the trust foundation: the user's annotations survive any regen, which is what makes the drift-acceptance model viable.

Tag taxonomy (`onav/<type>`, `onav/stable`) in frontmatter drives graph coloring and tag/Dataview filtering. Streams (protocol-spec wire-format transactions) are deferred — their canonical structure is irregular and earns a dedicated pass.

`init`, `update`, and `refresh` are all built (M5), plus the passive drift-navigation layer — Dataview dashboard, Canvas, and graph-coloring setup (M6). The skill is functionally complete: full lifecycle (cold-start → targeted update → full regen+prune) plus drift navigation without invocation.

## On Activation

**Module registration (standalone self-registering):** if the user passes `setup` or `configure` as the first argument, OR there is no `onav` section in any of `{project-root}/_bmad/config.yaml`, `config.user.yaml`, `config.toml`, `config.user.toml`, load `assets/module-setup.md` and complete registration (vault root + preferences) before doing anything else. The `setup`/`configure` arg always triggers this, even if already registered (for reconfiguration).

Load available config from `{project-root}/_bmad/config.toml`, `config.user.toml`, `config.yaml`, and `config.user.yaml` (root level and the `[modules.onav]` section). onav reads **both** TOML (installer-managed) and YAML (this module's setup writes here) so it works regardless of which BMad config format the host project uses. The script reads these directly, so you mainly need to know the config keys to help the user configure them — and to confirm the vault root is set before running. Use sensible defaults for anything unset; ask the user when the vault root is missing rather than failing silently.

Config keys (under `[modules.onav]`):

| Key | Default | Meaning |
| --- | --- | --- |
| `onav_vault_root` | (none — required) | Absolute path to the Obsidian vault root. The emit path is `<vault_root>/<projects_subfolder>/<project-slug>/`. |
| `onav_projects_subfolder` | `projects` | Subfolder under the vault root for indexed BMad projects. Accepts nesting (e.g. `projects/BlendArtis`) for an org-scoped layout. A `--projects-subfolder` CLI flag overrides per-invocation. |
| `onav_project_slug` | (auto-derived) | Exact-case override for this project's leaf folder name — bypasses the default lowercase-kebab slug (`ToF-Tracking-WS` → `tof-tracking-ws`). A `--project-slug` CLI flag overrides this per-invocation. Combine with a nested `onav_projects_subfolder` for `<vault>/projects/BlendArtis/ToF-Tracking-WS/`-style paths. |
| `onav_prefer_turbovault` | `true` | Prefer turbovault MCP / manifest mode for vault writes when available; falls back to direct file editing. |
| `onav_stale_days` | `14` | Days after which an unreviewed note counts as stale on the dashboard. |

Project name + canonical paths are **not** config — they auto-resolve from the host project's BMad config (`project_name`, `planning_artifacts`). One installed skill serves every BMad project; switching projects is just a different CWD.

If `uv` is not on PATH, stop and tell the user: `onav-index needs uv. Install: curl -LsSf https://astral.sh/uv/install.sh | sh`. There is no graceful fallback — the generator IS the skill.

## Emit mode — file vs. manifest (M7)

The generator has two emission modes (a global `--emit-mode {file, manifest}`, default `file`):

- **`file`** (default) — the script writes notes/dashboard/canvas/MOC directly to the vault. Fine for a throwaway vault or one not managed by LiveSync/turbovault.
- **`manifest`** — the script writes **nothing**. It computes every artifact and prints a JSON **write manifest** (vault-relative paths + full content) plus an `apply_hint`. You apply it to the vault via turbovault MCP.

**When to use manifest mode:** if `onav_prefer_turbovault` is true in config, or you have turbovault MCP tools available, or the vault is LiveSync/git-managed — raw file writes desync those systems, so the manifest path is required. Run `doctor` to check. The global flag goes before the subcommand: `uv run scripts/gen_index.py --vault-root <vault> --emit-mode manifest init --force`.

**Applying the manifest (full-content overwrite):** Personal notes are **already preserved** in each note's content (the script read the existing note and reconstructed it before emitting), so overwrite is safe — no SEARCH/REPLACE needed. For `manifest.deletes` (refresh pruning), call `turbovault_delete_note(path, confirm_path=path)`. The Canvas (`.canvas`) is a JSON asset turbovault may not manage — write it directly only if your vault permits non-note asset writes; otherwise skip it (the dashboard carries the navigability). For `init`, that's ~105 writes (one-time, cold start); for `update`, only the changed notes plus the dashboard/MOC/canvas (a handful — the workhorse advantage of manifest mode).

**CRITICAL — batch the writes, never loop `turbovault_write_note`:** each individual `turbovault_write_note` call triggers its own atomic git commit (~5s per call); looping it across a 100+ entry manifest takes 9+ minutes and looks like a freeze. Instead, chunk `manifest.writes` into batches of ~15 ops and apply each batch via `turbovault_batch_execute(operations=<JSON array>)` — one transaction = one git commit per batch. The operation shape is `{"type": "WriteNote", "path": <path>, "content": <content>}`. Skip any `kind: "canvas"` entries (turbovault doesn't manage those). For very large chunks (>~50KB JSON), split further — MCP tool args have size limits.

## How you work

The user's request drives everything — they should never need to know CLI flags. Parse intent, check config, run the script, apply via turbovault, report.

### Step 0 — Config detection (every invocation, before any command)

1. Read `_bmad/custom/config.user.toml` for an existing `[modules.onav]` section with `onav_vault_root`.
2. **If config exists and is complete** — proceed to the command. No user interaction needed.
3. **If config is missing or incomplete** — derive what you can from the user's words, then ask for what's still missing:
   - **Vault root**: if the user names a vault (e.g. "tibor-vault"), resolve it to an absolute path via `turbovault_list_vaults`. Otherwise ask: "Which Obsidian vault?"
   - **Projects subfolder + slug**: if the user gives a target path (e.g. "in `/projects/TIBS-HOME/BL5340-Bridge/onav`"), split at the last segment — everything before is `onav_projects_subfolder`, the last segment is `onav_project_slug`. If not specified, ask: "Where in the vault should the notes live?"
4. Write the resolved config to `_bmad/custom/config.user.toml`:
   ```toml
   [modules.onav]
   onav_vault_root = "<absolute path>"
   onav_projects_subfolder = "<subfolder>"
   onav_project_slug = "<slug>"
   ```
5. Run `doctor` and report any `fail`/`warn` before proceeding.

The script path resolves from this skill's installed directory — use the absolute path when calling from bash (e.g. `~/.agents/skills/onav-index/scripts/gen_index.py`).

### init — "index this project", "generate onav notes", "create the note graph"

1. Run the generator in manifest mode:
   ```bash
   uv run <skill-dir>/scripts/gen_index.py --project-root . --emit-mode manifest init --force
   ```
   (`--force` is safe — Personal notes are preserved across every rewrite.)
2. Capture the JSON manifest from stdout.
3. Chunk `manifest.writes` into batches of ~15 ops. Skip entries with `kind: "canvas"` (turbovault doesn't manage JSON assets — write that one directly to the filesystem if the vault permits, or skip it).
4. Apply each batch via `turbovault_batch_execute(operations=<JSON array>)`. Operation shape: `{"type": "WriteNote", "path": <vault-relative-path>, "content": <full markdown>}`. One `batch_execute` = one git commit; never loop individual `turbovault_write_note` calls (each triggers its own commit — 100+ notes takes 9+ minutes and looks like a freeze).
5. Report: entity counts by type, total notes written, dashboard path (`<vault>/<subfolder>/<slug>/index.md`).

### update — "update FR-4", "refresh AD-2 and SM-1", "re-index E2.5"

Targeted refresh by entity ID — the workhorse for keeping the graph in sync after canonical changes.

1. Parse the entity IDs from the user's request.
2. Run:
   ```bash
   uv run <skill-dir>/scripts/gen_index.py --project-root . --emit-mode manifest update <ID> [<ID>...]
   ```
3. Apply the manifest via `turbovault_batch_execute` (same chunking as init). The manifest contains only the changed notes plus dashboard/MOC/canvas updates.
4. Read the JSON output's `suggestion_prompt` and present it conversationally: *"Updated E2.5. Found 4 possibly-affected notes: AD-3, FR-2, E2.4, SM-1. Also FR-8b is referenced but has no note yet. Update [All | None | list of IDs]?"*
5. If the user says "All" or lists IDs, run `update <those IDs>` and repeat from step 3. Each call does one level of backlink crawl — iterating the conversation deepens the recursion.
6. For one-shot cascading without conversation: `update <ID> --yes-all`.

### refresh — "regenerate everything", "refresh the index", "full rebuild"

1. Preview first with `--dry-run`:
   ```bash
   uv run <skill-dir>/scripts/gen_index.py --project-root . --emit-mode manifest refresh --dry-run
   ```
2. Report what would be created/updated/pruned. **Surface any kept-annotated leftovers** — notes with `## Personal notes` whose entity is gone from canonical. The user decides whether to delete those manually; they are never auto-pruned.
3. On confirmation, run without `--dry-run`.
4. Apply the manifest via `turbovault_batch_execute`:
   - `manifest.writes` → `{"type": "WriteNote", ...}` batches (same as init)
   - `manifest.deletes` → `{"type": "DeleteNote", "path": <path>}` batches
5. Report: created, updated, pruned, kept-annotated. Offer to open the refresh-log.

### doctor — "check onav setup", "is onav configured?"

Run `uv run <skill-dir>/scripts/gen_index.py --project-root . doctor`. Report each check as pass/fail/warn with detail. Always run this as part of Step 0 when writing new config.

## Passive drift navigation (no invocation)

The generated layer itself surfaces drift — the user opens the dashboard in Obsidian, no skill call needed:

- **Project dashboard** (`<project>/index.md`): Dataview queries render live — staleness (oldest `last_reviewed` first), orphans (no inbound links), hotspots (most-referenced), plus statically-computed coverage gaps (FRs with no realizing Epic/Story, ADs that bind nothing). Open it after any BMad milestone to see what's drifted.
- **Canvas** (`<project>/structure.canvas`): a curated epic → story → FR visual layout with `belongs to` / `realizes` edges. Nodes are aliased-wikilink text labels, still fully clickable through to the real note. Regenerable; the user curates a separate canvas so curation survives regen.
- **Graph coloring**: a one-time setup note is emitted to `<vault>/<projects>/_setup-notes/graph-coloring.md` (idempotent — never overwritten).

**Orphan integrity:** the dashboard uses *only* Dataview for entity references (no static `[[entity]]` links), so it never registers as an inlink — orphan and hotspot detection stay honest.

## Tool dependencies

- **`uv`** (required) — the generator runs as `uv run scripts/gen_index.py …`; `uv` resolves the `pyyaml` dependency from the script's PEP 723 metadata automatically.
- **Obsidian + Dataview plugin** — required for the navigation value (backlinks, Properties panel, live queries), not for generation. The script emits markdown any editor can open.
- **Turbovault MCP** (optional, preferred from M7) — runtime-detected for surgical edits; file I/O fallback otherwise.

## Design rationale

- **Drift is accepted, not prevented.** These notes are navigation aids, not canonical. Every note carries `last_reviewed` (manually editable in Obsidian's Properties panel) and `source_sha` (the canonical file's hash at generation) so staleness is visible. The skill's job is to emit high-quality notes on request and surface staleness — not to keep the layer perfectly synced. `update`'s suggestion list is the human-in-the-loop mechanism that makes this safe.
- **Personal notes are sacred.** From M4, the `## Personal notes` section of each note is preserved across every rewrite. That preservation is what makes drift-acceptance viable — the user trusts the layer because their annotations survive. In M1, notes are cold-start only, so preservation hasn't been exercised yet; the section is documented in the template and ready.
- **External to the BMad flow.** The skill reads `{project-root}/_bmad-output/` and `{project-root}/_bmad/config.toml`; it never writes to them, never patches BMad internals. BMad version updates cannot break it.
- **Dataview blocks are ID-only, no Type/Title columns.** Every live query (`Referenced by`, dashboard Stale/Hotspots/All entities) renders a self-aliased entity link and nothing else. Two rounds of trying to add a Title column both failed to render in practice: a `LIST` with `link(...) + " — " + title` (Link + String concatenation is not reliably defined in Dataview), then a `TABLE` with `title AS "Title"` as a separate column (the column silently never populated in the live vault). Simple and working beats fancy and broken; open the note or use the native backlinks panel for titles.

## Resolution rules

- Bare paths (`scripts/gen_index.py`, `assets/entity-note-template.md`) resolve from this skill's installed directory.
- `{project-root}` → the project working directory. Forward slashes only.
- The emit path is derived: `<onav_vault_root>/<onav_projects_subfolder>/<project-slug>/`, where `<project-slug>` is the kebab-cased `project_name` from `{project-root}/_bmad/config.toml` — or the exact-case `onav_project_slug` / `--project-slug` override when set.
