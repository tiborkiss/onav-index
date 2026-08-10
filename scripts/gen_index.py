#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["pyyaml>=6.0"]
# ///
"""gen_index — the deterministic half of onav-index.

onav-index generates a derived Obsidian note-graph layer over a project's BMad
canonical artifacts. The mediating agent handles conversation; this script owns
the mechanical work a prompt does unreliably: parsing canonical files, deriving
relationships, and emitting notes with preserved frontmatter.

MILESTONE 2 (full ID-extraction contract): reads all canonical files and emits
seven entity types — FR, AD, SM, CAP, NFR, Epic, Story — with bidirectionally
derived wiki-link relationships. The architecture proven in M1 (FR vertical
slice) now scales horizontally. Streams (protocol-spec wire-format transactions)
are deferred: their canonical structure is irregular and earns a dedicated pass.

Subcommands:
  init                Cold-start: emit all derivable entity notes from canonical.
  update <ID> [...]   Targeted refresh + backlink crawl + suggestion list. (M4)
  refresh             Full regenerate-all with pruning. (M5)

Config resolution (first wins):
  --vault-root flag
    > [modules.onav] onav_vault_root in _bmad/config.user.toml
    > [modules.onav] onav_vault_root in _bmad/config.toml
    > error (the mediating agent asks the user)
Project + canonical paths auto-detected from _bmad/config.toml. The canonical
files remain the single source of truth; this script only reads them and writes
to the configured vault path.

ID-extraction contract (what each reader parses, validated against the ToF corpus):
  FR      prd.md         #### FR-n[b]: title          + body scan
  SM      prd.md §7      - **SM-[C]n**: text          + "Validates FR-x" in line
  AD      spine          ### AD-n — title              + **Binds:**/**Rule:** bullets
  CAP     spec           - **CAP-n — title**           + **intent:**/**success:** bullets
  NFR     epics.md       - **NFR-n (Name):** desc      + inline FR refs
  Epic    epics.md       ### Epic E1a: title (list)    + **FRs covered:**/**Needs:**
  Story   epics.md       ### Story E1a.1: title        + body scan (ACs, user story)
  FR→Epic epics.md       - FR-n → E1b (Coverage Map)   targeted parse (non-entity table)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GENERATOR_TAG = "onav-index (M7)"

# Heading / bullet patterns per entity type. Separators accept em-dash (—),
# en-dash (–), or hyphen (-) to survive minor canonical-format drift.
SEP = r"[—–-]"
FR_HEADING_RE = re.compile(rf"^#{{1,6}}\s+FR-(?P<id>\d+[a-z]?)\s*[:{SEP[1]}]\s*(?P<title>.+?)\s*$")
AD_HEADING_RE = re.compile(rf"^#{{1,6}}\s+AD-(?P<id>\d+)\s*{SEP}\s*(?P<title>.+?)\s*$")
EPIC_HEADING_RE = re.compile(r"^#{1,6}\s+Epic\s+(?P<id>E\d+[a-z]?)\s*:\s*(?P<title>.+?)\s*$")
STORY_HEADING_RE = re.compile(r"^#{1,6}\s+Story\s+(?P<id>E\d+[a-z]?\.\d+)\s*:\s*(?P<title>.+?)\s*$")
SM_BULLET_RE = re.compile(r"^-\s+\*\*SM-(?P<id>C?\d+)\*\*\s*:\s*(?P<text>.+?)\s*$")
CAP_HEADING_RE = re.compile(rf"^-\s+\*\*CAP-(?P<id>\d+)\s*{SEP}\s*(?P<title>.+?)\*\*\s*$")
NFR_BULLET_RE = re.compile(r"^-\s+\*\*NFR-(?P<id>\d+)\s*\((?P<name>[^)]+)\):\*\*\s*(?P<text>.+?)\s*$")
HEADING_RE = re.compile(r"^#{1,6}\s")
BOLD_SUBSECTION_RE = re.compile(r"^\*\*.+\*\*\s*[:{-}]")
COVERAGE_MAP_RE = re.compile(r"^-\s+FR-(?P<fr>\d+[a-z]?)\s*→\s*(?P<epic>E\d+[a-z]?)")

# Master ID tokenizer. Alternatives are ordered so the most specific (Story)
# wins before Epic; the Epic negative lookahead is belt-and-suspenders so a
# bare "E1a" doesn't also match inside "E1a.1" text that Story already consumed.
TOKEN_RE = re.compile(
    r"(?P<Story>E\d+[a-z]?\.\d+)"
    r"|(?P<FR>FR-\d+[a-z]?)"
    r"|(?P<AD>AD-\d+)"
    r"|(?P<SM>SM-C?\d+)"
    r"|(?P<CAP>CAP-\d+)"
    r"|(?P<NFR>NFR-\d+)"
    r"|(?P<Epic>E\d+[a-z]?(?!\.\d))"
)


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    """A single codified entity extracted from a canonical file."""

    id: str  # e.g. "FR-8b", "SM-C1", "E1a.1" — includes the type prefix
    type: str  # e.g. "FR", "AD", "Story"
    title: str
    definition: str
    source_path: str  # canonical file path (project-relative when possible)
    source_anchor: str  # the ID, for a path#anchor pointer
    references: list[str] = field(default_factory=list)  # outgoing IDs cited in raw_section
    raw_section: str = ""  # full section text, preserved for later milestone use


@dataclass
class ProjectContext:
    """Resolved project + vault context for one invocation."""

    project_name: str
    project_slug: str
    project_root: Path
    vault_root: Path
    projects_subfolder: str
    planning_artifacts: Path
    stale_days: int
    prefer_turbovault: bool

    @property
    def project_dir(self) -> Path:
        return self.vault_root / self.projects_subfolder / self.project_slug

    @property
    def vault_projects_root(self) -> Path:
        return self.vault_root / self.projects_subfolder


# ---------------------------------------------------------------------------
# Shared extraction helpers
# ---------------------------------------------------------------------------


def extract_all_refs(text: str, own_id: str) -> list[str]:
    """Every entity ID cited in ``text`` — deduped, order of first appearance, self excluded.

    This is the uniform relationship detector: each reader scans its own section
    text and the result becomes the entity's outgoing edges. It captures the
    structured relationship fields (AD Binds, SM Validates, CAP success parens,
    Story ACs) because those are text too — no per-type semantic parsing needed.
    """
    seen: dict[str, None] = {}
    for m in TOKEN_RE.finditer(text):
        ref = m.group(0)
        if ref != own_id and ref not in seen:
            seen[ref] = None
    return list(seen)


def _first_sentence(text: str, max_len: int = 220) -> str:
    """Distill a short definition from longer canonical text (best-effort)."""
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    boundary = re.search(r"[.!?](?=\s+[A-Z(])", text)
    if boundary and boundary.start() <= max_len:
        return text[: boundary.start() + 1].strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(" ", 1)[0].strip()
    return cut + "…"


def _section_body(lines: list[str], start: int, headings_at_or_above: int = 0) -> str:
    """Lines after a heading until the next heading (any depth) or EOF."""
    body: list[str] = []
    for line in lines[start + 1 :]:
        if HEADING_RE.match(line):
            break
        body.append(line)
    return "\n".join(body).strip()


def _rel_to_project(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def _read_bmad_config(project_root: Path) -> dict:
    """Read BMad config across all four resolver layers, deep-merged, later wins.

    Matches BMad's actual convention: ``config.toml`` (installer base, team) <
    ``config.user.toml`` (installer base, personal) < ``custom/config.toml``
    (team override, committed, "never touched by the installer") <
    ``custom/config.user.toml`` (personal override, gitignored, "wins over both
    base config and team overrides"). Each layer is tried as both TOML
    (installer / custom convention) and YAML (this module's own standalone
    setup via merge-config.py), so onav's settings are found wherever they were
    written. custom/config.user.toml is the durable home for a personal path
    like onav_vault_root — it survives every `bmad install` re-run.
    """
    merged: dict = {}
    layers = (
        ("_bmad/config.toml", "toml"),
        ("_bmad/config.yaml", "yaml"),
        ("_bmad/config.user.toml", "toml"),
        ("_bmad/config.user.yaml", "yaml"),
        ("_bmad/custom/config.toml", "toml"),
        ("_bmad/custom/config.yaml", "yaml"),
        ("_bmad/custom/config.user.toml", "toml"),
        ("_bmad/custom/config.user.yaml", "yaml"),
    )
    for name, fmt in layers:
        path = project_root / name
        if not path.exists():
            continue
        if fmt == "toml":
            _merge_toml_subset(merged, path.read_text(encoding="utf-8"))
        else:
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue
            if isinstance(data, dict):
                _deep_merge(merged, data)
    return merged


def _deep_merge(into: dict, src: dict) -> None:
    """Recursively merge ``src`` into ``into`` (src wins on leaf conflicts)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(into.get(k), dict):
            _deep_merge(into[k], v)
        else:
            into[k] = v


def _merge_toml_subset(into: dict, text: str) -> None:
    current: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = [seg.strip() for seg in line[1:-1].split(".")]
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if "#" in value and not (value.startswith('"') or value.startswith("'")):
            value = value.split("#", 1)[0].strip()
        parsed = _parse_toml_value(value)
        cursor = into
        for seg in current:
            cursor = cursor.setdefault(seg, {})
        cursor[key] = parsed


def _parse_toml_value(value: str):
    if (value.startswith('"') and value.endswith('"')) or (
        value.startswith("'") and value.endswith("'")
    ):
        return value[1:-1]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        return value


def _expand_root(value: str, project_root: Path) -> Path:
    if "{project-root}" in value:
        value = value.replace("{project-root}", str(project_root))
    return Path(value)


def resolve_context(
    project_root: Path,
    vault_root_override: str | None,
    project_slug_override: str | None = None,
    projects_subfolder_override: str | None = None,
) -> ProjectContext:
    project_root = project_root.resolve()
    config = _read_bmad_config(project_root)

    core = config.get("core", {})
    project_name = core.get("project_name", project_root.name)

    bmm = config.get("modules", {}).get("bmm", {})
    planning_artifacts = _expand_root(
        bmm.get("planning_artifacts", str(project_root / "_bmad-output" / "planning-artifacts")),
        project_root,
    )

    onav = config.get("modules", {}).get("onav", {})

    vault_root_str = vault_root_override or onav.get("onav_vault_root")
    if not vault_root_str:
        raise SystemExit(
            "onav-index: no vault root configured. Pass --vault-root PATH, or set "
            "'onav_vault_root' under [modules.onav] in {project-root}/_bmad/custom/config.user.toml"
            " (personal, gitignored, durable across installer re-runs — or config.user.yaml/.toml),"
            " or run onav-index with `setup`."
        )
    vault_root = Path(vault_root_str).expanduser().resolve()

    # Project slug: exact-case override (CLI flag > onav_project_slug config) takes
    # precedence over the auto-derived lowercase-kebab slug. Lets a project land at
    # e.g. <vault>/<subfolder>/BlendArtis/ToF-Tracking-WS/ instead of the default
    # <vault>/projects/tof-tracking-ws/ — combine with onav_projects_subfolder
    # (which already accepts nested paths like "projects/BlendArtis") for org nesting.
    slug_raw = project_slug_override or onav.get("onav_project_slug")
    project_slug = _sanitize_slug_override(slug_raw) if slug_raw else _slugify(project_name)

    return ProjectContext(
        project_name=project_name,
        project_slug=project_slug,
        project_root=project_root,
        vault_root=vault_root,
        projects_subfolder=projects_subfolder_override or onav.get("onav_projects_subfolder", "projects"),
        planning_artifacts=planning_artifacts,
        stale_days=int(onav.get("onav_stale_days", 14)),
        prefer_turbovault=bool(onav.get("onav_prefer_turbovault", True)),
    )


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "project"


def _sanitize_slug_override(raw: str) -> str:
    """Accept an exact-case project slug override, guarding only against path
    traversal (no '..' segments, no leading '/'). Case and hyphenation are
    preserved verbatim — this is a trusted local override, not derived text."""
    cleaned = raw.strip().strip("/")
    if not cleaned or ".." in cleaned.split("/"):
        raise SystemExit(f"onav-index: invalid project slug override: {raw!r}")
    return cleaned


# ---------------------------------------------------------------------------
# Canonical locators
# ---------------------------------------------------------------------------


def find_prd(planning_artifacts: Path) -> Path | None:
    candidates = sorted(
        (planning_artifacts / "prds").glob("*/prd.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def find_spine(planning_artifacts: Path) -> Path | None:
    arch = planning_artifacts / "architecture"
    if not arch.exists():
        return None
    candidates = sorted(arch.glob("*/ARCHITECTURE-SPINE.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def find_spec(project_root: Path) -> Path | None:
    spec_dir = project_root / "_bmad-output" / "specs"
    if not spec_dir.exists():
        return None
    candidates = sorted(spec_dir.glob("*/SPEC.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def find_protocol_spec(project_root: Path) -> Path | None:
    p = project_root / "docs" / "protocol-spec.md"
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# Canonical readers
# ---------------------------------------------------------------------------


def read_prd_frs(prd_path: Path | None, project_root: Path) -> list[Entity]:
    """FR entities from prd.md (#### FR-n[b]: title)."""
    if prd_path is None:
        return []
    source_path = _rel_to_project(prd_path, project_root)
    lines = prd_path.read_text(encoding="utf-8").splitlines()
    bounds: list[tuple[str, str, int]] = []
    for idx, line in enumerate(lines):
        m = FR_HEADING_RE.match(line)
        if m:
            bounds.append((m.group("id"), m.group("title").strip(), idx))

    entities: list[Entity] = []
    for i, (fr_num, title, start) in enumerate(bounds):
        end = bounds[i + 1][2] if i + 1 < len(bounds) else len(lines)
        body_lines = lines[start + 1 : end]
        body = "\n".join(body_lines).strip()
        definition = _extract_fr_definition(body_lines)
        entities.append(
            Entity(
                id=f"FR-{fr_num}",
                type="FR",
                title=title,
                definition=definition,
                source_path=source_path,
                source_anchor=f"FR-{fr_num}",
                references=extract_all_refs(body, f"FR-{fr_num}"),
                raw_section=body,
            )
        )
    return entities


def _extract_fr_definition(body_lines: list[str]) -> str:
    para: list[str] = []
    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            if para:
                break
            continue
        if BOLD_SUBSECTION_RE.match(stripped) or HEADING_RE.match(stripped):
            break
        para.append(stripped)
    return " ".join(para).strip()


def read_prd_sms(prd_path: Path | None, project_root: Path) -> list[Entity]:
    """SM entities from prd.md §7 bullets (- **SM-[C]n**: text. Validates FR-x)."""
    if prd_path is None:
        return []
    source_path = _rel_to_project(prd_path, project_root)
    entities: list[Entity] = []
    for line in prd_path.read_text(encoding="utf-8").splitlines():
        m = SM_BULLET_RE.match(line.strip())
        if not m:
            continue
        sm_id = f"SM-{m.group('id')}"
        text = m.group("text")
        title = text.split("—")[0].split("–")[0].split(" - ")[0].strip()
        title = title or sm_id
        entities.append(
            Entity(
                id=sm_id,
                type="SM",
                title=title,
                definition=text,
                source_path=source_path,
                source_anchor=sm_id,
                references=extract_all_refs(line, sm_id),
                raw_section=line,
            )
        )
    return entities


def read_spine_ads(spine_path: Path | None, project_root: Path) -> list[Entity]:
    """AD entities from ARCHITECTURE-SPINE.md (### AD-n — title + Binds/Rule)."""
    if spine_path is None:
        return []
    source_path = _rel_to_project(spine_path, project_root)
    lines = spine_path.read_text(encoding="utf-8").splitlines()
    bounds: list[tuple[str, str, int]] = []
    for idx, line in enumerate(lines):
        m = AD_HEADING_RE.match(line)
        if m:
            bounds.append((m.group("id"), m.group("title").strip(), idx))

    entities: list[Entity] = []
    for i, (ad_num, title, start) in enumerate(bounds):
        end = bounds[i + 1][2] if i + 1 < len(bounds) else len(lines)
        body_lines = lines[start + 1 : end]
        body = "\n".join(body_lines).strip()
        rule_text = _extract_bold_field(body_lines, "Rule")
        definition = _first_sentence(rule_text) if rule_text else title
        entities.append(
            Entity(
                id=f"AD-{ad_num}",
                type="AD",
                title=title,
                definition=definition,
                source_path=source_path,
                source_anchor=f"AD-{ad_num}",
                references=extract_all_refs(body, f"AD-{ad_num}"),
                raw_section=body,
            )
        )
    return entities


def _extract_bold_field(body_lines: list[str], field_name: str) -> str:
    """Text following a ``- **<field_name>:**`` bullet (e.g. Rule, Binds)."""
    pat = re.compile(rf"^-\s+\*\*{re.escape(field_name)}:\s*\*\*\s*(.+)$")
    for line in body_lines:
        m = pat.match(line.strip())
        if m:
            return m.group(1).strip()
    return ""


def read_spec_caps(spec_path: Path | None, project_root: Path) -> list[Entity]:
    """CAP entities from SPEC.md (- **CAP-n — title** + intent/success bullets)."""
    if spec_path is None:
        return []
    source_path = _rel_to_project(spec_path, project_root)
    lines = spec_path.read_text(encoding="utf-8").splitlines()
    entities: list[Entity] = []
    i = 0
    while i < len(lines):
        m = CAP_HEADING_RE.match(lines[i].strip())
        if m:
            cap_id = f"CAP-{m.group('id')}"
            title = m.group("title").strip()
            block: list[str] = []
            j = i + 1
            while j < len(lines) and (lines[j].startswith("  ") or lines[j].strip() == ""):
                block.append(lines[j])
                j += 1
            block_text = "\n".join(block).strip()
            intent = _extract_subbullet(block, "intent")
            definition = intent or title
            entities.append(
                Entity(
                    id=cap_id,
                    type="CAP",
                    title=title,
                    definition=definition,
                    source_path=source_path,
                    source_anchor=cap_id,
                    references=extract_all_refs(block_text, cap_id),
                    raw_section=block_text,
                )
            )
            i = j
            continue
        i += 1
    return entities


def _extract_subbullet(block_lines: list[str], field_name: str) -> str:
    pat = re.compile(rf"^\s*-\s+\*\*{re.escape(field_name)}:\s*\*\*\s*(.+)$")
    for line in block_lines:
        m = pat.match(line)
        if m:
            return m.group(1).strip()
    return ""


def read_epics(epics_path: Path | None, project_root: Path) -> tuple[list[Entity], list[Entity], list[tuple[str, str]]]:
    """Epic + Story entities from epics.md, plus FR→Epic edges from the Coverage Map.

    Returns (epics, stories, coverage_edges) where coverage_edges is the targeted
    parse of the FR Coverage Map — a mapping table that lives outside any
    entity's own section, so it can't be captured by section-text scanning.
    """
    if epics_path is None:
        return [], [], []
    source_path = _rel_to_project(epics_path, project_root)
    lines = epics_path.read_text(encoding="utf-8").splitlines()

    epics: list[Entity] = []
    stories: list[Entity] = []
    coverage_edges: list[tuple[str, str]] = []

    # Coverage Map: - FR-n → E1a (anywhere; the table is under Requirements Inventory).
    for line in lines:
        m = COVERAGE_MAP_RE.match(line.strip())
        if m:
            coverage_edges.append((f"FR-{m.group('fr')}", m.group("epic")))

    # Epic entities: ### Epic E1a: title (the Epic List entries carry FRs covered / Needs).
    epic_bounds: list[tuple[str, str, int]] = []
    story_bounds: list[tuple[str, str, int]] = []
    for idx, line in enumerate(lines):
        em = EPIC_HEADING_RE.match(line)
        if em:
            epic_bounds.append((em.group("id"), em.group("title").strip(), idx))
            continue
        sm = STORY_HEADING_RE.match(line)
        if sm:
            story_bounds.append((sm.group("id"), sm.group("title").strip(), idx))

    for i, (eid, title, start) in enumerate(epic_bounds):
        end = _next_bound(epic_bounds, i, story_bounds, lines)
        body_lines = lines[start + 1 : end]
        body = "\n".join(body_lines).strip()
        definition = _first_sentence(body)
        scan_text = title + "\n" + body
        epics.append(
            Entity(
                id=eid,
                type="Epic",
                title=title,
                definition=definition,
                source_path=source_path,
                source_anchor=eid,
                references=extract_all_refs(scan_text, eid),
                raw_section=body,
            )
        )

    # Dedupe epics by ID: the Epic List (### Epic E1a:) and the content section
    # (## Epic E1a:) both match the heading regex. Keep the first occurrence —
    # the List entry carries the 'FRs covered' / 'Needs' relationship data.
    seen_ids: set[str] = set()
    deduped_epics: list[Entity] = []
    for ent in epics:
        if ent.id in seen_ids:
            continue
        seen_ids.add(ent.id)
        deduped_epics.append(ent)
    epics = deduped_epics

    for i, (sid, title, start) in enumerate(story_bounds):
        end = _next_bound([], 0, story_bounds, lines, this_index=i)
        # Bound by the next story OR next epic heading after this point.
        next_epic = next((e[2] for e in epic_bounds if e[2] > start), len(lines))
        end = min(end, next_epic)
        body_lines = lines[start + 1 : end]
        body = "\n".join(body_lines).strip()
        definition = _extract_story_definition(body)
        scan_text = title + "\n" + body
        stories.append(
            Entity(
                id=sid,
                type="Story",
                title=title,
                definition=definition,
                source_path=source_path,
                source_anchor=sid,
                references=extract_all_refs(scan_text, sid),
                raw_section=body,
            )
        )

    return epics, stories, coverage_edges


def _next_bound(_epic_bounds, _ei, story_bounds, lines, this_index=None) -> int:
    """Index of the next story heading after the i-th story, or EOF."""
    if this_index is not None:
        if this_index + 1 < len(story_bounds):
            return story_bounds[this_index + 1][2]
        return len(lines)
    # Fallback (unused for epics — epics use a simpler next-in-list below).
    return len(lines)


def _extract_story_definition(body: str) -> str:
    """The 'I want ...' clause of a user story, else the first sentence."""
    m = re.search(r"\bI want\s+(.+?)(?:\n|,\s*so that)", body, re.IGNORECASE | re.DOTALL)
    if m:
        return _first_sentence(m.group(1).strip())
    return _first_sentence(body)


def read_epics_nfrs(epics_path: Path | None, project_root: Path) -> list[Entity]:
    """NFR entities from epics.md (- **NFR-n (Name):** desc). NFRs live in epics, not the PRD."""
    if epics_path is None:
        return []
    source_path = _rel_to_project(epics_path, project_root)
    entities: list[Entity] = []
    for line in epics_path.read_text(encoding="utf-8").splitlines():
        m = NFR_BULLET_RE.match(line.strip())
        if not m:
            continue
        nfr_id = f"NFR-{m.group('id')}"
        name = m.group("name").strip()
        text = m.group("text")
        entities.append(
            Entity(
                id=nfr_id,
                type="NFR",
                title=name,
                definition=text,
                source_path=source_path,
                source_anchor=nfr_id,
                references=extract_all_refs(line, nfr_id),
                raw_section=line,
            )
        )
    return entities


def read_protocol_streams(_protocol_path: Path | None, _project_root: Path) -> list[Entity]:
    """Stream entities (protocol-spec wire-format transactions). DEFERRED.

    The protocol-spec structures streams irregularly: `stream.*` transactions
    appear as `### n.n` subsections while `cal.*` opcodes are prose bullets.
    A reliable, uniform Stream reader earns a dedicated pass; the seven core
    entity types above already deliver the dense BMad cross-reference graph.
    """
    return []


# ---------------------------------------------------------------------------
# Relationship graph
# ---------------------------------------------------------------------------


def apply_coverage_map(entities: list[Entity], coverage_edges: list[tuple[str, str]]) -> None:
    """Fold the Coverage Map's FR→Epic edges into the FR entities' outgoing refs.

    The Coverage Map is a mapping table outside any entity's own section, so
    section-text scanning can't see it. This adds the epic to each FR's
    references (FR→Epic direction); the Epic→FR direction already comes from
    each Epic's own 'FRs covered:' text via extract_all_refs.
    """
    by_id = {e.id: e for e in entities}
    for fr_id, epic_id in coverage_edges:
        if fr_id in by_id and epic_id not in by_id[fr_id].references:
            by_id[fr_id].references.append(epic_id)


def build_incoming(entities: list[Entity]) -> dict[str, set[str]]:
    """Invert every entity's outgoing references into a target→sources map."""
    incoming: dict[str, set[str]] = defaultdict(set)
    for ent in entities:
        for ref in ent.references:
            incoming[ref].add(ent.id)
    return incoming


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


PERSONAL_NOTES_HEADING_RE = re.compile(r"^##\s+Personal\s+notes\s*$", re.IGNORECASE | re.MULTILINE)


def _extract_personal_notes(note_path: Path) -> str:
    """Read the user's ``## Personal notes`` section from an existing note.

    Sentinel strategy: the ``## Personal notes`` heading (case-insensitive)
    through EOF. This is the trust foundation of the drift-acceptance model —
    whatever the user wrote there survives every regen. Returns the heading +
    body (trailing newline), or "" if the note or section is absent.
    """
    if not note_path.exists():
        return ""
    text = note_path.read_text(encoding="utf-8")
    m = PERSONAL_NOTES_HEADING_RE.search(text)
    if not m:
        return ""
    return text[m.start():].rstrip() + "\n"


def _render_entity_note(
    entity: Entity, note_path: Path, source_sha: str, project_subpath: str, title_by_id: dict[str, str]
) -> tuple[str, bool]:
    """Pure render: returns (note_content, has_personal_notes). Reads the
    existing note at ``note_path`` for Personal-notes preservation (reading is
    safe even in a turbovault-managed vault) but does NOT write. The write
    happens in emit_entity_note (file mode) or is left to the agent applying
    the manifest (manifest mode, via turbovault MCP).

    ``title_by_id`` (id -> title, built from ALL current entities) lets each
    reference render as ``[[FR-4]] — Stable bridge upstream contract`` instead
    of a bare ID — the ID stays the literal link target (unambiguous in raw
    markdown), the title is plain text appended on the same line. A reference
    to an ID with no known title (a missing-note gap) renders as a bare link.
    """
    # M4 trust foundation: read the existing note's Personal notes BEFORE
    # regenerating, then re-append verbatim. Universal across init/update/refresh.
    personal_notes = _extract_personal_notes(note_path)

    status = "stable"
    frontmatter = {
        "id": entity.id,
        "type": entity.type,
        "title": entity.title,
        "tags": [f"onav/{entity.type}", f"onav/{status}"],
        "source": entity.source_path,
        "source_anchor": entity.source_anchor,
        "source_sha": source_sha,
        "status": status,
        "last_reviewed": _now_iso(),
        "generator": GENERATOR_TAG,
    }

    outgoing = list(entity.references)

    parts: list[str] = []
    parts.append("<!-- onav-index generated note. Canonical files are the source")
    parts.append(" of truth; this note is a regenerable navigation layer. The")
    parts.append(" 'Referenced by' list is a LIVE Dataview query — install the")
    parts.append(" Dataview plugin for it to render (Obsidian's backlinks panel")
    parts.append(" is the no-plugin fallback). Add personal annotations under a")
    parts.append(" '## Personal notes' heading — preserved across every rewrite. -->")
    parts.append("")
    parts.append("---")
    parts.append(yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip())
    parts.append("---")
    parts.append("")
    parts.append(f"# {entity.id} — {entity.title}")
    parts.append("")
    if entity.definition:
        parts.append(entity.definition)
        parts.append("")

    # Relationships section: outgoing static, incoming live (Dataview).
    dataview_block = _referenced_by_query(project_subpath)
    if outgoing:
        parts.append("## Relationships")
        parts.append("")
        parts.append("### References")
        parts.append("")
        for ref in outgoing:
            ref_title = title_by_id.get(ref)
            parts.append(f"- [[{ref}]] — {ref_title}" if ref_title else f"- [[{ref}]]")
        parts.append("")
        parts.append("### Referenced by")
        parts.append("")
        parts.append(dataview_block)
        parts.append("")
    else:
        # No outgoing refs, but the entity may still be referenced by others —
        # surface the live backlinks query so the note is never a dead end.
        parts.append("## Referenced by")
        parts.append("")
        parts.append(dataview_block)
        parts.append("")

    if personal_notes:
        # Re-append the preserved Personal notes block verbatim. This is the
        # trust foundation: the user's annotations survive every regen.
        parts.append(personal_notes.rstrip())

    return "\n".join(parts) + "\n", bool(personal_notes)


def emit_entity_note(
    entity: Entity, type_dir: Path, source_sha: str, project_subpath: str, title_by_id: dict[str, str]
) -> Path:
    """File mode: render one entity note and write it. Returns the note path.

    Three-tier freshness: Tier 1 (live Dataview 'Referenced by'), Tier 2 (static
    definition + 'References', drift-tracked), Tier 3 ('## Personal notes',
    preserved). Manifest mode (M7) calls _render_entity_note directly and leaves
    the write to the agent via turbovault MCP, so this is the file-IO path only.
    """
    type_dir.mkdir(parents=True, exist_ok=True)
    note_path = type_dir / f"{entity.id}.md"
    content, _ = _render_entity_note(entity, note_path, source_sha, project_subpath, title_by_id)
    note_path.write_text(content, encoding="utf-8")
    return note_path


def _referenced_by_query(project_subpath: str) -> str:
    """Live Dataview backlinks query, scoped to this project (sealed silos).

    Renders each backlink as ``ID — Title`` (via ``LIST " — " + title``),
    matching the static References section's format. Sorted by type then name
    rather than grouped, trading the M3 typed-grouping for simplicity and
    title-visibility — grouping + per-row titles needs Dataview's row-array
    syntax, which adds fragility for a display-only concern.
    """
    return (
        "```dataview\n"
        'LIST " — " + title\n'
        f'FROM [[]] AND "{project_subpath}"\n'
        "WHERE file.name != this.file.name\n"
        "SORT type ASC, file.name ASC\n"
        "```"
    )


def _compute_coverage_gaps(entities: list[Entity]) -> dict[str, list[str]]:
    """Deterministic coverage gaps from canonical (recomputed each run).

    - uncovered_frs: FRs no Epic or Story references (no realizer).
    - unbinding_ads: ADs that reference no FR or SM (bind nothing).
    These are canonical-derived facts (tier 2), so static is correct — unlike
    orphans/hotspots, which depend on the live link graph and live in Dataview.
    """
    by_id = {e.id: e for e in entities}
    incoming = build_incoming(entities)
    uncovered_frs = [
        e.id
        for e in entities
        if e.type == "FR"
        and not any(by_id[s].type in ("Epic", "Story") for s in incoming.get(e.id, set()) if s in by_id)
    ]
    unbinding_ads = [
        e.id
        for e in entities
        if e.type == "AD" and not any(r.startswith(("FR-", "SM-")) for r in e.references)
    ]
    return {"uncovered_frs": _natural_sort_ids(uncovered_frs), "unbinding_ads": _natural_sort_ids(unbinding_ads)}


def _natural_sort_ids(ids: list[str]) -> list[str]:
    return sorted(ids, key=lambda i: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", i)])


def _render_project_index(
    entities_by_type: dict[str, list[Entity]],
    project_name: str,
    project_subpath: str,
) -> str:
    """Pure render of the Dataview dashboard content (no file write).

    Uses ONLY Dataview for entity references so the dashboard creates no static
    wiki-links — orphan/hotspot detection stays honest. Canonical facts (counts,
    coverage gaps) stay static; graph/time-derived facts are live Dataview.
    """
    all_entities = [e for ents in entities_by_type.values() for e in ents]
    gaps = _compute_coverage_gaps(all_entities)
    total = len(all_entities)
    breakdown = " · ".join(f"{k}: {len(v)}" for k, v in sorted(entities_by_type.items()) if v)
    folder = project_subpath

    p: list[str] = []
    p.append(f"# {project_name} — onav dashboard")
    p.append("")
    p.append("<!-- onav-index generated dashboard (M6). Dataview queries render live;")
    p.append(" static sections recompute on each init/update/refresh. The dashboard")
    p.append(" uses only Dataview for entity links so it doesn't pollute orphan/hotspot")
    p.append(" detection. Install the Dataview plugin for queries to render. -->")
    p.append("")
    p.append("## Overview")
    p.append("")
    p.append(f"**{total} entities** — {breakdown}")
    p.append("")
    p.append("## Drift")
    p.append("")
    p.append("### Stale — oldest `last_reviewed` first")
    p.append("")
    p.append("```dataview")
    p.append("TABLE WITHOUT ID file.link AS \"Entity\", type AS \"Type\", last_reviewed AS \"Last reviewed\"")
    p.append(f"FROM \"{folder}\"")
    p.append("WHERE type != null")
    p.append("SORT last_reviewed ASC")
    p.append("LIMIT 20")
    p.append("```")
    p.append("")
    p.append("### Orphans — no inbound links")
    p.append("")
    p.append("```dataview")
    p.append("LIST")
    p.append(f"FROM \"{folder}\"")
    p.append("WHERE type != null AND length(file.inlinks) = 0")
    p.append("SORT file.name ASC")
    p.append("```")
    p.append("")
    p.append("### Hotspots — most-referenced")
    p.append("")
    p.append("```dataview")
    p.append("TABLE WITHOUT ID file.link AS \"Entity\", type AS \"Type\", length(file.inlinks) AS \"Inbound\"")
    p.append(f"FROM \"{folder}\"")
    p.append("WHERE type != null AND length(file.inlinks) > 0")
    p.append("SORT length(file.inlinks) DESC")
    p.append("LIMIT 15")
    p.append("```")
    p.append("")
    p.append("## Coverage gaps")
    p.append("")
    p.append("<!-- Static — recomputed from canonical on each run. -->")
    p.append("")
    p.append(f"- **FRs with no realizing Epic/Story:** {_fmt_id_list(gaps['uncovered_frs'])}")
    p.append(f"- **ADs that bind nothing:** {_fmt_id_list(gaps['unbinding_ads'])}")
    p.append("")
    p.append("## All entities")
    p.append("")
    p.append("```dataview")
    p.append("TABLE WITHOUT ID file.link AS \"Entity\", title AS \"Title\"")
    p.append(f"FROM \"{folder}\"")
    p.append("WHERE type != null")
    p.append("SORT type ASC, file.name ASC")
    p.append("```")
    p.append("")
    return "\n".join(p)


def emit_project_index(
    project_dir: Path,
    entities_by_type: dict[str, list[Entity]],
    project_name: str,
    project_subpath: str,
) -> Path:
    """File mode: render the dashboard and write <project_dir>/index.md."""
    project_dir.mkdir(parents=True, exist_ok=True)
    index_path = project_dir / "index.md"
    index_path.write_text(
        _render_project_index(entities_by_type, project_name, project_subpath), encoding="utf-8"
    )
    return index_path


def _fmt_id_list(ids: list[str]) -> str:
    # IDs are shown as code spans, NOT wiki-links, so the dashboard stays
    # link-free and orphan detection remains honest. The All-entities Dataview
    # table provides the clickable navigation.
    return ", ".join(f"`{i}`" for i in ids) or "none"


# Canvas node colors (Obsidian Canvas "color" field values).
_CANVAS_COLORS = {"Epic": "6", "Story": "4", "FR": "5"}  # purple, green, cyan


def _render_canvas(entities: list[Entity], project_subpath: str) -> str | None:
    """Pure render of the Canvas JSON (epic → story → FR layout). Returns None
    if there are no epic/story/FR entities to lay out.

    Nodes are ``type: "text"`` containing an aliased wikilink (``[[E6.5|E6.5 —
    title]]``) rather than ``type: "file"`` embeds. A file-embed renders the
    ENTIRE note (frontmatter, definition, Relationships, Dataview block) inside
    a small box — dense and mostly unreadable without zooming. A text node
    shows exactly the short label, stays fully clickable/navigable to the real
    note (Canvas references don't register as Dataview/backlink inlinks either
    way, so this trades nothing on the orphan-detection front), and needs a
    smaller box to be fully readable.
    """
    epics = sorted([e for e in entities if e.type == "Epic"], key=lambda e: e.id)
    stories = sorted([e for e in entities if e.type == "Story"], key=lambda e: e.id)
    frs = sorted([e for e in entities if e.type == "FR"], key=lambda e: e.id)
    if not (epics or stories or frs):
        return None

    node_w, node_h, row_h = 340, 100, 130
    epic_x, story_x, fr_x = 0, 520, 1040

    nodes: list[dict] = []
    by_node: dict[str, dict] = {}

    def _add(ent: Entity, x: int, idx: int) -> None:
        label = f"{ent.id} — {ent.title}".replace("|", "-").replace("]]", ")")
        node = {
            "id": ent.id,
            "type": "text",
            "text": f"[[{ent.id}|{label}]]",
            "x": x,
            "y": idx * row_h,
            "width": node_w,
            "height": node_h,
            "color": _CANVAS_COLORS.get(ent.type),
        }
        nodes.append(node)
        by_node[ent.id] = node

    for i, e in enumerate(epics):
        _add(e, epic_x, i)
    for i, s in enumerate(stories):
        _add(s, story_x, i)
    for i, f in enumerate(frs):
        _add(f, fr_x, i)

    edges: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _edge(src: str, dst: str, label: str) -> None:
        if src == dst or src not in by_node or dst not in by_node:
            return
        key = (src, dst)
        if key in seen:
            return
        seen.add(key)
        edges.append({"id": f"{src}->{dst}", "fromNode": src, "toNode": dst, "label": label})

    # Story -> parent Epic (derived from the story ID prefix: E1a.1 -> E1a).
    for s in stories:
        parent = re.match(r"(E\d+[a-z]?)\.", s.id)
        if parent:
            _edge(s.id, parent.group(1), "belongs to")
    # Story / Epic -> FRs they reference.
    for ent in stories + epics:
        for ref in ent.references:
            if ref.startswith("FR-"):
                _edge(ent.id, ref, "realizes")

    return json.dumps({"nodes": nodes, "edges": edges}, indent=2)


def emit_canvas(
    project_dir: Path,
    entities: list[Entity],
    project_subpath: str,
) -> Path | None:
    """File mode: render the Canvas and write <project_dir>/structure.canvas.

    The generated canvas is regenerable; the user curates a separate canvas
    (e.g. ``my-view.canvas``) so curation survives regen. Returns None if there
    is nothing to lay out.
    """
    content = _render_canvas(entities, project_subpath)
    if content is None:
        return None
    project_dir.mkdir(parents=True, exist_ok=True)
    canvas_path = project_dir / "structure.canvas"
    canvas_path.write_text(content, encoding="utf-8")
    return canvas_path


_GRAPH_COLORING_MD = """# Graph view coloring — one-time setup

onav-index tags every entity note with `onav/<type>` (and `onav/<status>`).
Configure Obsidian's graph view to color nodes by tag so the structure is
visible at a glance:

**Settings → Graph View → Groups** — add one group per type:

| Group query | Suggested color | Type |
| --- | --- | --- |
| `onav/FR` | Red | Functional requirements |
| `onav/AD` | Orange | Architecture decisions |
| `onav/SM` | Yellow | Success metrics |
| `onav/CAP` | Green | Capabilities |
| `onav/NFR` | Purple | Non-functional requirements |
| `onav/Epic` | Cyan | Epics |
| `onav/Story` | Blue | Stories |

The same tags drive filtered searches and Dataview queries. The `type:`
frontmatter field is an alternative key for graph grouping if you prefer it
over tags.
"""


def emit_graph_coloring_note(vault_projects_root: Path) -> Path | None:
    """Emit the one-time graph-coloring setup note, if not already present.

    Idempotent — never overwrites the user's edited copy. Content lives in
    _GRAPH_COLORING_MD so the manifest emitter reuses the same source.
    """
    setup_dir = vault_projects_root / "_setup-notes"
    note_path = setup_dir / "graph-coloring.md"
    if note_path.exists():
        return None
    setup_dir.mkdir(parents=True, exist_ok=True)
    note_path.write_text(_GRAPH_COLORING_MD, encoding="utf-8")
    return note_path


def _natural_sort(ents: list[Entity]) -> list[Entity]:
    """Sort entities so FR-2 precedes FR-10 and E1a.2 precedes E1a.10."""
    def key(e: Entity) -> list:
        return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", e.id)]
    return sorted(ents, key=key)


def _render_vault_root_moc(
    existing: str,
    project_slug: str,
    project_name: str,
    stats: dict[str, int],
) -> str:
    """Pure render of the vault-root MOC. Upserts this project's entry into the
    existing content (preserves other projects' entries + manual notes)."""
    header_lines = [
        "# Indexed BMad projects",
        "",
        "<!-- onav-index vault-root MOC. Auto-updated; safe to add manual notes below. -->",
        "",
    ]
    total = sum(stats.values())
    breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(stats.items()) if v)
    entry = f"- [[{project_slug}/index|{project_name}]] — {total} entities ({breakdown})"
    prefix = f"- [[{project_slug}/".lower()
    other_lines = [
        ln
        for ln in existing.splitlines()
        if ln.strip().startswith("- [[")
        and not ln.strip().lower().startswith(prefix)
    ]
    body = header_lines + other_lines + [entry, ""]
    return "\n".join(body)


def emit_vault_root_moc(
    vault_projects_root: Path,
    project_slug: str,
    project_name: str,
    stats: dict[str, int],
) -> Path:
    """File mode: render the vault-root MOC (upsert) and write it."""
    vault_projects_root.mkdir(parents=True, exist_ok=True)
    moc_path = vault_projects_root / "index.md"
    existing = moc_path.read_text(encoding="utf-8") if moc_path.exists() else ""
    moc_path.write_text(
        _render_vault_root_moc(existing, project_slug, project_name, stats), encoding="utf-8"
    )
    return moc_path


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def _collect_entities(ctx: ProjectContext) -> tuple[list[Entity], dict[str, str]]:
    """Run every canonical reader. Returns all entities + a source-sha map."""
    prd_path = find_prd(ctx.planning_artifacts)
    spine_path = find_spine(ctx.planning_artifacts)
    spec_path = find_spec(ctx.project_root)
    epics_path = ctx.planning_artifacts / "epics.md"
    if not epics_path.exists():
        epics_path = None  # type: ignore[assignment]
    protocol_path = find_protocol_spec(ctx.project_root)

    frs = read_prd_frs(prd_path, ctx.project_root)
    sms = read_prd_sms(prd_path, ctx.project_root)
    ads = read_spine_ads(spine_path, ctx.project_root)
    caps = read_spec_caps(spec_path, ctx.project_root)
    nfrs = read_epics_nfrs(epics_path, ctx.project_root)
    epics, stories, coverage_edges = read_epics(epics_path, ctx.project_root)
    _streams = read_protocol_streams(protocol_path, ctx.project_root)  # deferred

    entities = frs + sms + ads + caps + nfrs + epics + stories
    apply_coverage_map(entities, coverage_edges)

    source_shas: dict[str, str] = {}
    for canonical in (prd_path, spine_path, spec_path, epics_path, protocol_path):
        if canonical and Path(canonical).exists():
            source_shas[_rel_to_project(Path(canonical), ctx.project_root)] = _file_sha256(Path(canonical))
    return entities, source_shas


def _build_manifest(
    ctx: ProjectContext,
    write_entities: list[Entity],
    all_entities: list[Entity],
    source_shas: dict[str, str],
    by_type: dict[str, list[Entity]],
    stats: dict[str, int],
    project_subpath: str,
    title_by_id: dict[str, str],
    *,
    deletes: list[str] | None = None,
    refresh_log_content: str | None = None,
) -> dict:
    """Build a write manifest (no file writes) for the agent to apply via
    turbovault MCP (or any vault-aware writer). Personal notes are already
    preserved in each note's content — the agent applies full-content overwrites.
    ``write_entities`` are the notes actually written (subset for update); the
    Canvas always renders from ``all_entities`` (the full current graph).
    """
    writes: list[dict] = []
    notes_with_personal = 0
    for ent in sorted(write_entities, key=lambda e: (e.type, e.id)):
        note_path = ctx.project_dir / ent.type / f"{ent.id}.md"
        sha = source_shas.get(ent.source_path, "") or _sha_for_source(ent.source_path, ctx.project_root)
        content, has_pn = _render_entity_note(ent, note_path, sha, project_subpath, title_by_id)
        if has_pn:
            notes_with_personal += 1
        writes.append({
            "path": f"{project_subpath}/{ent.type}/{ent.id}.md",
            "kind": "note",
            "id": ent.id,
            "has_personal_notes": has_pn,
            "content": content,
        })
    # Dashboard
    writes.append({
        "path": f"{project_subpath}/index.md",
        "kind": "dashboard",
        "content": _render_project_index(by_type, ctx.project_name, project_subpath),
    })
    # Canvas (JSON asset) — always the full graph, not just write_entities.
    canvas = _render_canvas(all_entities, project_subpath)
    if canvas is not None:
        writes.append({
            "path": f"{project_subpath}/structure.canvas",
            "kind": "canvas",
            "content": canvas,
            "note": "JSON asset (not markdown). turbovault manages notes; write .canvas directly if your vault permits non-note asset writes, else skip — the dashboard carries the navigability.",
        })
    # Vault-root MOC (upsert; read existing for preservation of other projects)
    moc_path = ctx.vault_projects_root / "index.md"
    existing = moc_path.read_text(encoding="utf-8") if moc_path.exists() else ""
    writes.append({
        "path": f"{ctx.projects_subfolder}/index.md",
        "kind": "vault_root_moc",
        "content": _render_vault_root_moc(existing, ctx.project_slug, ctx.project_name, stats),
    })
    # Graph-coloring setup note (only if not already present)
    gc_rel = f"{ctx.projects_subfolder}/_setup-notes/graph-coloring.md"
    if not (ctx.vault_projects_root / "_setup-notes" / "graph-coloring.md").exists():
        writes.append({"path": gc_rel, "kind": "graph_coloring", "content": _GRAPH_COLORING_MD})
    # Refresh log (refresh only)
    if refresh_log_content is not None:
        ts = _now_iso().replace(":", "-")
        writes.append({
            "path": f"{project_subpath}/_refresh-log-{ts}.md",
            "kind": "refresh_log",
            "content": refresh_log_content,
        })

    return {
        "emit_mode": "manifest",
        "vault_root": str(ctx.vault_root),
        "project_subpath": project_subpath,
        "project_name": ctx.project_name,
        "writes": writes,
        "deletes": deletes or [],
        "summary": {
            "entity_types": stats,
            "total_entities": sum(stats.values()),
            "write_count": len(writes),
            "delete_count": len(deletes or []),
            "notes_with_personal_notes": notes_with_personal,
        },
        "apply_hint": (
            "Apply each write via turbovault_write_note(path, content, mode='overwrite'). "
            "Apply each delete via turbovault_delete_note(path, confirm_path=path). "
            "Personal notes are already preserved in each note's content — overwrite is safe."
        ),
    }


def _emit_all(
    ctx: ProjectContext,
    args: argparse.Namespace,
    write_entities: list[Entity],
    all_entities: list[Entity],
    source_shas: dict[str, str],
    by_type: dict[str, list[Entity]],
    stats: dict[str, int],
    *,
    refresh_log_content: str | None = None,
    delete_paths: list[str] | None = None,
) -> dict | None:
    """Emit all artifacts. Returns None in file mode (written to disk) or the
    manifest dict in manifest mode (caller prints it). Shared by init/update/
    refresh so the manifest stays consistent across subcommands.

    ``write_entities`` are the notes actually (re)written — the full set for
    init/refresh, but only the changed subset for update. ``all_entities`` is
    always the complete current graph: used for the title lookup (a changed
    note may reference entities outside the written subset) and for the Canvas
    (which must always show the full epic/story/FR graph, not just what an
    `update` call happened to touch).
    """
    project_subpath = f"{ctx.projects_subfolder}/{ctx.project_slug}"
    title_by_id = {e.id: e.title for e in all_entities}
    if args.emit_mode == "manifest":
        return _build_manifest(
            ctx, write_entities, all_entities, source_shas, by_type, stats, project_subpath, title_by_id,
            deletes=delete_paths, refresh_log_content=refresh_log_content,
        )
    # File mode
    for ent in write_entities:
        sha = source_shas.get(ent.source_path, "") or _sha_for_source(ent.source_path, ctx.project_root)
        emit_entity_note(ent, ctx.project_dir / ent.type, sha, project_subpath, title_by_id)
    emit_project_index(ctx.project_dir, by_type, ctx.project_name, project_subpath)
    emit_canvas(ctx.project_dir, all_entities, project_subpath)
    emit_vault_root_moc(ctx.vault_projects_root, ctx.project_slug, ctx.project_name, stats)
    emit_graph_coloring_note(ctx.vault_projects_root)
    if refresh_log_content is not None:
        log_path = ctx.project_dir / f"_refresh-log-{_now_iso().replace(':', '-')}.md"
        log_path.write_text(refresh_log_content, encoding="utf-8")
    return None


def cmd_init(args: argparse.Namespace) -> int:
    ctx = resolve_context(Path(args.project_root), args.vault_root, args.project_slug, args.projects_subfolder)

    if ctx.project_dir.exists() and any(ctx.project_dir.iterdir()):
        msg = (
            f"onav-index: project dir exists and is non-empty: {ctx.project_dir}\n"
            "  init on an existing project regenerates every note from canonical."
            " Personal notes ('## Personal notes' sections) are preserved."
        )
        if not args.headless and not args.force:
            print(msg + "\n  Re-run with --force (or -H) to proceed.", file=sys.stderr)
            return 1
        print(msg, file=sys.stderr)

    entities, source_shas = _collect_entities(ctx)

    if not entities:
        print(
            "onav-index: no entities derived. Check that canonical files exist "
            f"under {ctx.planning_artifacts}.",
            file=sys.stderr,
        )
        return 1

    # build_incoming() is reserved for the M6 dashboard (orphan/hotspot analytics).
    # Per-note incoming is a live Dataview query as of M3, so it isn't emitted statically.
    project_subpath = f"{ctx.projects_subfolder}/{ctx.project_slug}"

    by_type: dict[str, list[Entity]] = defaultdict(list)
    for ent in entities:
        by_type[ent.type].append(ent)
    stats = {k: len(v) for k, v in by_type.items()}

    if args.dry_run:
        result = {
            "status": "ok",
            "subcommand": "init",
            "dry_run": True,
            "project": ctx.project_name,
            "entities": stats,
            "total": sum(stats.values()),
        }
        print(json.dumps(result, indent=2))
        return 0

    manifest = _emit_all(ctx, args, entities, entities, source_shas, by_type, stats)
    if manifest is not None:
        manifest["subcommand"] = "init"
        print(json.dumps(manifest, indent=2))
        return 0

    result = {
        "status": "ok",
        "subcommand": "init",
        "project": ctx.project_name,
        "slug": ctx.project_slug,
        "project_dir": str(ctx.project_dir),
        "entities": stats,
        "total": sum(stats.values()),
        "notes_written": sum(stats.values()),
    }
    print(json.dumps(result, indent=2))
    return 0


def _sha_for_source(source_rel: str, project_root: Path) -> str:
    p = project_root / source_rel
    return _file_sha256(p) if p.exists() else ""


def cmd_update(args: argparse.Namespace) -> int:
    """Targeted refresh — the workhorse. Refreshes the named entities from
    canonical (preserving Personal notes), then emits a suggestion list for the
    mediating agent: possibly-affected notes (direct backlinks of what changed)
    and missing-note gaps (newly-referenced IDs with no note yet). The agent
    renders the suggestion and runs the follow-up on [All | None | IDs].
    """
    ctx = resolve_context(Path(args.project_root), args.vault_root, args.project_slug, args.projects_subfolder)
    entities, source_shas = _collect_entities(ctx)
    by_id = {e.id: e for e in entities}

    requested: list[str] = args.ids
    found = [i for i in requested if i in by_id]
    missing_requested = [i for i in requested if i not in by_id]

    if not found:
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": "none of the requested IDs were found in canonical files.",
                    "requested": requested,
                    "missing": missing_requested,
                    "hint": "run `init` to index this project first.",
                },
                indent=2,
            )
        )
        return 1

    project_subpath = f"{ctx.projects_subfolder}/{ctx.project_slug}"

    # 1. Backlink crawl (computed from the full canonical graph, always).
    incoming = build_incoming(entities)
    affected: set[str] = set()
    for eid in found:
        affected |= incoming.get(eid, set())
    affected -= set(found)

    # 2. Missing-note gaps.
    all_ids = set(by_id)
    gaps: set[str] = set()
    for eid in found:
        for ref in by_id[eid].references:
            if ref not in all_ids:
                gaps.add(ref)

    # 3. Decide what to refresh: requested, plus the affected set if --yes-all.
    to_refresh = list(found)
    cascaded: list[str] = []
    if args.yes_all and affected:
        cascaded = sorted(affected)
        to_refresh += cascaded

    by_type: dict[str, list[Entity]] = defaultdict(list)
    for ent in entities:
        by_type[ent.type].append(ent)
    stats = {k: len(v) for k, v in by_type.items()}

    if args.dry_run:
        print(json.dumps({
            "status": "ok", "subcommand": "update", "dry_run": True,
            "would_update": to_refresh, "affected": sorted(affected),
            "gaps": sorted(gaps),
            "suggestion_prompt": _suggestion_text(found, affected, gaps),
        }, indent=2))
        return 0

    # 4. Emit. Manifest mode carries only the CHANGED notes + the full dashboard/
    #    MOC/canvas; file mode writes the same set directly.
    changed_entities = [by_id[eid] for eid in to_refresh if eid in by_id]
    manifest = _emit_all(ctx, args, changed_entities, entities, source_shas, by_type, stats)
    if manifest is not None:
        manifest.update({
            "subcommand": "update",
            "updated": found,
            "cascaded": cascaded,
            "affected": sorted(affected),
            "gaps": sorted(gaps),
            "missing_requested": missing_requested,
            "suggestion_prompt": _suggestion_text(found, affected, gaps),
        })
        print(json.dumps(manifest, indent=2))
        return 0

    result = {
        "status": "ok",
        "subcommand": "update",
        "updated": found,
        "cascaded": cascaded,
        "missing_requested": missing_requested,
        "affected": sorted(affected),
        "gaps": sorted(gaps),
        "suggestion_prompt": _suggestion_text(found, affected, gaps),
    }
    print(json.dumps(result, indent=2))
    return 0


def _suggestion_text(updated: list[str], affected: set[str], gaps: set[str]) -> str:
    """Render the conversational suggestion the mediating agent presents."""
    bits = [f"Updated {', '.join(updated)}."]
    if affected:
        bits.append(
            f"Found {len(affected)} possibly-affected note(s) from the backlink crawl: "
            f"{', '.join(sorted(affected))}."
        )
    if gaps:
        verb = "is" if len(gaps) == 1 else "are"
        bits.append(
            f"Also {', '.join(sorted(gaps))} {verb} referenced but ha{'s' if len(gaps) == 1 else 've'} no note yet."
        )
    if affected:
        bits.append("Update [All | None | list of IDs]?")
    elif gaps:
        bits.append("These gaps need canonical definitions before they can be indexed (or run `init`).")
    return " ".join(bits)


def cmd_refresh(args: argparse.Namespace) -> int:
    """Full regenerate-all with pruning. Heavy, infrequent.

    Regenerates every entity note from canonical (Personal notes preserved),
    prunes leftover notes whose entity no longer exists in canonical, and
    writes a refresh-log summary. The trust foundation holds even here: a
    leftover WITH Personal notes is never deleted — it is flagged in the log
    for manual review. Only un-annotated generated cruft is pruned.
    """
    ctx = resolve_context(Path(args.project_root), args.vault_root, args.project_slug, args.projects_subfolder)
    entities, source_shas = _collect_entities(ctx)

    if not entities:
        print(
            json.dumps(
                {
                    "status": "error",
                    "message": f"no entities derived. Check canonical files under {ctx.planning_artifacts}.",
                },
                indent=2,
            )
        )
        return 1

    project_subpath = f"{ctx.projects_subfolder}/{ctx.project_slug}"
    current_ids = {e.id for e in entities}

    # Snapshot existing note IDs (per type subdir) to distinguish created vs updated.
    existing_before: set[str] = set()
    if ctx.project_dir.exists():
        for type_dir in ctx.project_dir.iterdir():
            if type_dir.is_dir():
                existing_before.update(note.stem for note in type_dir.glob("*.md"))

    # 1. Regenerate all (Personal notes preserved in emit_entity_note).
    by_type: dict[str, list[Entity]] = defaultdict(list)
    created: list[str] = []
    updated: list[str] = []
    for ent in sorted(entities, key=lambda e: (e.type, e.id)):
        by_type[ent.type].append(ent)
        (created if ent.id not in existing_before else updated).append(ent.id)

    # 2. Prune leftovers (entity gone from canonical). Trust foundation: never
    #    delete a note that carries Personal notes — flag it instead.
    pruned: list[str] = []
    pruned_paths: list[str] = []  # vault-relative, for manifest deletes
    kept_annotated: list[str] = []
    if ctx.project_dir.exists():
        for type_dir in sorted(ctx.project_dir.iterdir()):
            if not type_dir.is_dir():
                continue
            for note in sorted(type_dir.glob("*.md")):
                if note.stem in current_ids:
                    continue
                if _extract_personal_notes(note):
                    kept_annotated.append(note.stem)
                else:
                    pruned.append(note.stem)
                    try:
                        pruned_paths.append(str(note.relative_to(ctx.vault_root)))
                    except ValueError:
                        pruned_paths.append(str(note))
                    if not args.dry_run and args.emit_mode == "file":
                        note.unlink()
        # Remove now-empty type directories (file mode only; cosmetic).
        if not args.dry_run and args.emit_mode == "file":
            for type_dir in sorted(ctx.project_dir.iterdir()):
                if type_dir.is_dir() and not any(type_dir.iterdir()):
                    type_dir.rmdir()

    # 3. Emit. Manifest mode carries the deletes + refresh-log; file mode writes.
    stats = {k: len(v) for k, v in by_type.items()}
    refresh_log_content = _refresh_log_md(
        ctx.project_name, created, updated, pruned, kept_annotated, stats
    )

    if args.dry_run:
        print(json.dumps({
            "status": "ok", "subcommand": "refresh", "dry_run": True,
            "would_create": len(created), "would_update": len(updated),
            "would_prune": pruned, "would_keep_annotated": kept_annotated,
        }, indent=2))
        return 0

    manifest = _emit_all(
        ctx, args, entities, entities, source_shas, by_type, stats,
        refresh_log_content=refresh_log_content, delete_paths=pruned_paths,
    )
    log_path = None
    if manifest is not None:
        manifest.update({
            "subcommand": "refresh",
            "counts": {
                "created": len(created), "updated": len(updated),
                "pruned": len(pruned), "kept_annotated": len(kept_annotated),
                "total_entities": sum(stats.values()),
            },
            "pruned": pruned,
            "kept_annotated": kept_annotated,
        })
        print(json.dumps(manifest, indent=2))
        return 0

    # File mode: refresh-log was written by _emit_all; report its path.
    log_path = ctx.project_dir / f"_refresh-log-{_now_iso().replace(':', '-')}.md"

    result = {
        "status": "ok",
        "subcommand": "refresh",
        "project": ctx.project_name,
        "counts": {
            "created": len(created),
            "updated": len(updated),
            "pruned": len(pruned),
            "kept_annotated": len(kept_annotated),
            "total_entities": sum(stats.values()),
        },
        "pruned": pruned,
        "kept_annotated": kept_annotated,
        "refresh_log": str(log_path) if log_path else None,
    }
    print(json.dumps(result, indent=2))
    return 0


def _refresh_log_md(
    project_name: str,
    created: list[str],
    updated: list[str],
    pruned: list[str],
    kept_annotated: list[str],
    stats: dict[str, int],
) -> str:
    """Markdown summary of one refresh run, written into the project folder."""
    lines: list[str] = []
    lines.append(f"# Refresh log — {project_name}")
    lines.append("")
    lines.append(f"Generated: {_now_iso()}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- **Created:** {len(created)}")
    lines.append(f"- **Updated:** {len(updated)}")
    lines.append(f"- **Pruned** (no Personal notes, entity removed from canonical): {len(pruned)}")
    lines.append(f"- **Kept** (has Personal notes — review manually): {len(kept_annotated)}")
    lines.append(f"- **Total entities:** {sum(stats.values())}")
    lines.append("")
    if pruned:
        lines.append("## Pruned")
        lines.append("")
        for eid in pruned:
            lines.append(f"- `{eid}`")
        lines.append("")
    if kept_annotated:
        lines.append("## Kept (has Personal notes — not pruned)")
        lines.append("")
        lines.append("These notes' entities no longer exist in canonical, but the notes carry")
        lines.append("Personal annotations. Review and delete manually if appropriate.")
        lines.append("")
        for eid in kept_annotated:
            lines.append(f"- `{eid}`")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gen_index.py",
        description="onav-index generator — derived Obsidian note-graph layer over BMad canonical artifacts.",
    )
    parser.add_argument("--project-root", default=".", help="Project root containing _bmad/config.toml.")
    parser.add_argument("--vault-root", default=None, help="Obsidian vault root (overrides config).")
    parser.add_argument(
        "--project-slug",
        default=None,
        help="Exact-case override for the project's leaf folder name under the vault "
        "(overrides onav_project_slug config and the default lowercase-kebab slug). "
        "Combine with onav_projects_subfolder (accepts nested paths, e.g. "
        "'projects/BlendArtis') for an org-nested, case-preserving layout.",
    )
    parser.add_argument(
        "--projects-subfolder",
        default=None,
        help="Override onav_projects_subfolder for this invocation (accepts nested paths, "
        "e.g. 'projects/BlendArtis').",
    )
    parser.add_argument(
        "--emit-mode",
        choices=("file", "manifest"),
        default="file",
        help="file (default) writes directly; manifest outputs a JSON write manifest for the agent "
        "to apply via turbovault MCP (required for turbovault/LiveSync-managed vaults).",
    )
    parser.add_argument("-H", "--headless", action="store_true", help="Non-interactive; use sensible defaults.")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_init = sub.add_parser("init", help="Cold-start: emit all derivable entity notes.")
    p_init.add_argument("--force", action="store_true", help="Overwrite an existing project dir without prompting.")
    p_init.add_argument("--dry-run", action="store_true", help="Report what would be written; write nothing.")
    p_init.set_defaults(func=cmd_init)

    p_update = sub.add_parser("update", help="Targeted refresh by entity ID — the workhorse.")
    p_update.add_argument("ids", nargs="+", help="Entity IDs, e.g. FR-4 E2.5.")
    p_update.add_argument("--yes-all", action="store_true", help="Also refresh the affected (backlink) set — one-level cascade.")
    p_update.add_argument("--dry-run", action="store_true", help="Report what would happen; write nothing.")
    p_update.set_defaults(func=cmd_update)

    p_refresh = sub.add_parser("refresh", help="Full regenerate-all with pruning.")
    p_refresh.add_argument("--dry-run", action="store_true", help="Preview regen + prune decisions; write/delete nothing.")
    p_refresh.set_defaults(func=cmd_refresh)

    p_doctor = sub.add_parser("doctor", help="Check the environment (uv, vault root, Dataview plugin, canonical files).")
    p_doctor.set_defaults(func=cmd_doctor)

    return parser


def cmd_doctor(args: argparse.Namespace) -> int:
    """Environment checks — the M7 setup extensions delivered as a runtime doctor.

    Verifies uv, the configured vault root exists, the projects subfolder, the
    Dataview plugin (warning, not fatal), and that canonical files resolve. The
    mediating agent runs this on first setup or when something looks off.
    """
    checks: list[dict] = []

    # uv
    import shutil

    uv_ok = shutil.which("uv") is not None
    checks.append({
        "check": "uv on PATH",
        "status": "ok" if uv_ok else "fail",
        "detail": "required — install: curl -LsSf https://astral.sh/uv/install.sh | sh" if not uv_ok else "found",
    })

    # Resolve context (may raise on missing vault root — catch and report).
    try:
        ctx = resolve_context(Path(args.project_root), args.vault_root, args.project_slug, args.projects_subfolder)
    except SystemExit as e:
        checks.append({"check": "vault root configured", "status": "fail", "detail": str(e)})
        print(json.dumps({"status": "fail", "checks": checks}, indent=2))
        return 1

    checks.append({"check": "vault root", "status": "ok" if ctx.vault_root.exists() else "warn",
                   "detail": str(ctx.vault_root)})
    checks.append({"check": "projects subfolder", "status": "ok" if ctx.vault_projects_root.exists() else "warn",
                   "detail": f"{ctx.vault_projects_root} (created on first init)"})
    dataview = ctx.vault_root / ".obsidian" / "plugins" / "dataview"
    checks.append({"check": "Dataview plugin", "status": "ok" if dataview.exists() else "warn",
                   "detail": "queries render as code blocks without it (backlinks panel still works)"})

    # Canonical files
    prd = find_prd(ctx.planning_artifacts)
    spine = find_spine(ctx.planning_artifacts)
    epics = ctx.planning_artifacts / "epics.md"
    spec = find_spec(ctx.project_root)
    for name, p in (("PRD", prd), ("ARCHITECTURE-SPINE", spine), ("epics.md", epics if epics.exists() else None), ("SPEC", spec)):
        checks.append({"check": f"canonical: {name}", "status": "ok" if p else "warn",
                       "detail": str(p) if p else "not found — entity type derived from it will be empty"})

    # turbovault preference (informational)
    checks.append({"check": "onav_prefer_turbovault", "status": "ok",
                   "detail": f"{'true — use --emit-mode manifest + turbovault MCP for vault writes' if ctx.prefer_turbovault else 'false — file mode is fine'}"})

    overall = "ok" if all(c["status"] != "fail" for c in checks) else "fail"
    print(json.dumps({"status": overall, "checks": checks}, indent=2))
    return 0 if overall == "ok" else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
