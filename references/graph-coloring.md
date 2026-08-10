# Graph view coloring — onav-index setup

onav-index tags every entity note with `onav/<type>` and `onav/<status>` (M3) and
sets a `type:` frontmatter field. This document is the reference for configuring
Obsidian's graph view to color nodes by type — emitted as a one-time setup note
to `<vault>/<projects_subfolder>/_setup-notes/graph-coloring.md` on the first
init (and never overwritten after).

## Recommended color mapping

| Group query | Color | Type |
| --- | --- | --- |
| `onav/FR` | Red | Functional requirements |
| `onav/AD` | Orange | Architecture decisions |
| `onav/SM` | Yellow | Success metrics |
| `onav/CAP` | Green | Capabilities |
| `onav/NFR` | Purple | Non-functional requirements |
| `onav/Epic` | Cyan | Epics |
| `onav/Story` | Blue | Stories |

Status tags (`onav/stable`, `onav/needs-update`, `onav/draft`) can drive a
second grouping layer or filtered searches.

## How to apply (Obsidian)

**Settings → Graph View → Groups** — add one group per type, entering the tag
query (e.g. `onav/FR`) and assigning the color. Obsidian applies the first
matching group, so order does not matter when the tag sets are disjoint (they
are here). The same tags drive filtered searches and Dataview queries across
the vault.

The `type:` frontmatter field is an alternative graph-grouping key if you
prefer it to tags.
