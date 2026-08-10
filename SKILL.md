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

- **Tier 1 (live):** `### Referenced by` is a Dataview table (Entity/Type/Title columns) — backlinks sorted by type, re-evaluated on every open, the entity self-aliased so it displays as its ID. New entities linking to this one appear with no regen. (The native backlinks panel is the no-plugin fallback.)
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

**Applying the manifest (full-content overwrite):** for each entry in `manifest.writes`, call `turbovault_write_note(path, content, mode=overwrite)`. Personal notes are **already preserved** in each note's content (the script read the existing note and reconstructed it before emitting), so overwrite is safe — no SEARCH/REPLACE needed. For `manifest.deletes` (refresh pruning), call `turbovault_delete_note(path, confirm_path=path)`. The Canvas (`.canvas`) is a JSON asset turbovault may not manage — write it directly only if your vault permits non-note asset writes; otherwise skip it (the dashboard carries the navigability). For `init`, that's ~105 writes (one-time, cold start); for `update`, only the changed notes plus the dashboard/MOC/canvas (a handful — the workhorse advantage of manifest mode).

## How you work

For every invocation, run the generator and let its JSON stdout drive what you say next. Don't reconstruct what the script already reports.

### init — cold-start a project in the vault

Run `uv run scripts/gen_index.py --project-root {project-root} --vault-root <vault> init`. The script emits every derivable entity note, writes the project `index.md`, and updates the vault-root MOC. On a non-empty existing project dir it refuses without `--force` — surface that and confirm before passing `--force`. **Personal notes (`## Personal notes` sections) are preserved across every rewrite**, including `init --force`, so re-initializing an existing project is safe.

Present the result as the project's new map: how many entities, where they live, and the vault path to open. Offer to open the dashboard `index.md`.

### update — targeted refresh with a suggestion list (the workhorse)

Run `uv run scripts/gen_index.py update <ID> [<ID>...]`. The script re-reads canonical, rewrites the named note(s) (preserving Personal notes, bumping `last_reviewed`), and prints a **suggestion list** as structured JSON plus a `suggestion_prompt` string ready to read to the user. Two parts:

- **affected** — direct backlinks of what you just refreshed (other notes that cite it and may want refreshing too).
- **gaps** — IDs the refreshed entity now references that have no note yet (new canonical content not yet indexed).

Render the `suggestion_prompt` conversationally: *"Updated E2.5. Found 4 possibly-affected notes: AD-3, FR-2, E2.4, SM-1. Update [All | None | list of IDs]?"* Collect the answer and run the follow-up: `update <those IDs>` for "All"/list, or stop for "None". Each call does one level of crawl — iterating the conversation is how the recursion deepens. For headless one-shot cascading, `update <ID> --yes-all` refreshes the affected set in the same call. The dashboard + vault-root MOCs are auto-updated as the final step of every update.

### refresh — full regenerate-all with pruning

Run `uv run scripts/gen_index.py refresh`. Heavy and infrequent. Regenerates every note from canonical (Personal notes preserved), then prunes leftover notes whose entity no longer exists in canonical, and writes a refresh-log to `<project>/_refresh-log-<timestamp>.md`.

**The trust foundation holds even in pruning:** a leftover that carries `## Personal notes` is *never* deleted — it is flagged in the refresh-log for manual review. Only un-annotated generated cruft is pruned. Use `--dry-run` to preview the prune decisions (what would be pruned vs. kept) before committing.

Present the result as a before/after: how many created, updated, pruned, and kept-annotated. If there are kept-annotated leftovers, surface them — the user decides whether to delete those manually. Offer to open the refresh-log.

## Passive drift navigation (no invocation)

The generated layer itself surfaces drift — the user opens the dashboard in Obsidian, no skill call needed:

- **Project dashboard** (`<project>/index.md`): Dataview queries render live — staleness (oldest `last_reviewed` first), orphans (no inbound links), hotspots (most-referenced), a full entity table, plus statically-computed coverage gaps (FRs with no realizing Epic/Story, ADs that bind nothing). Open it after any BMad milestone to see what's drifted.
- **Canvas** (`<project>/structure.canvas`): a curated epic → story → FR visual layout with `belongs to` / `realizes` edges. Nodes are aliased-wikilink text labels (`ID — Title`), not live file embeds — readable at a glance in a normal-sized box, still fully clickable through to the real note. Regenerable; the user curates a separate canvas so curation survives regen.
- **Graph coloring**: a one-time setup note is emitted to `<vault>/<projects>/_setup-notes/graph-coloring.md` (idempotent — never overwritten). See `references/graph-coloring.md` for the color mapping.

**Orphan integrity:** the dashboard uses *only* Dataview for entity references (no static `[[entity]]` links), so it never registers as an inlink — orphan and hotspot detection stay honest. Coverage-gap IDs are shown as code spans, not links, for the same reason.

### doctor — environment check

Run `uv run scripts/gen_index.py --vault-root <vault> doctor` on first setup or when something looks off. It checks uv on PATH, the vault root exists, the projects subfolder, the Dataview plugin (warn, not fatal), that each canonical file resolves, and the `onav_prefer_turbovault` setting. Report any `fail`/`warn` to the user with the detail.

## Tool dependencies

- **`uv`** (required) — the generator runs as `uv run scripts/gen_index.py …`; `uv` resolves the `pyyaml` dependency from the script's PEP 723 metadata automatically.
- **Obsidian + Dataview plugin** — required for the navigation value (backlinks, Properties panel, live queries), not for generation. The script emits markdown any editor can open.
- **Turbovault MCP** (optional, preferred from M7) — runtime-detected for surgical edits; file I/O fallback otherwise.

## Design rationale

- **Drift is accepted, not prevented.** These notes are navigation aids, not canonical. Every note carries `last_reviewed` (manually editable in Obsidian's Properties panel) and `source_sha` (the canonical file's hash at generation) so staleness is visible. The skill's job is to emit high-quality notes on request and surface staleness — not to keep the layer perfectly synced. `update`'s suggestion list is the human-in-the-loop mechanism that makes this safe.
- **Personal notes are sacred.** From M4, the `## Personal notes` section of each note is preserved across every rewrite. That preservation is what makes drift-acceptance viable — the user trusts the layer because their annotations survive. In M1, notes are cold-start only, so preservation hasn't been exercised yet; the section is documented in the template and ready.
- **External to the BMad flow.** The skill reads `{project-root}/_bmad-output/` and `{project-root}/_bmad/config.toml`; it never writes to them, never patches BMad internals. BMad version updates cannot break it.

## Resolution rules

- Bare paths (`scripts/gen_index.py`, `assets/entity-note-template.md`) resolve from this skill's installed directory.
- `{project-root}` → the project working directory. Forward slashes only.
- The emit path is derived: `<onav_vault_root>/<onav_projects_subfolder>/<project-slug>/`, where `<project-slug>` is the kebab-cased `project_name` from `{project-root}/_bmad/config.toml` — or the exact-case `onav_project_slug` / `--project-slug` override when set.
