#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Tests for gen_index.py — M2 full ID-extraction contract.

Covers every canonical reader (FR/SM from prd, AD from spine, CAP from spec,
NFR/Epic/Story from epics), the FR Coverage Map targeted parse, the master ID
tokenizer (Story-before-Epic ordering, letter suffixes, cross-cutting SM-Cn),
and bidirectional relationship derivation (outgoing references + inverted
incoming, with coverage-map edges folded into FR outgoing).

Run via uv against the stdlib unittest runner (no pytest dependency):
    uv run --with pyyaml python3 scripts/tests/test_gen_index.py
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_GEN = Path(__file__).resolve().parent.parent / "gen_index.py"
_spec = importlib.util.spec_from_file_location("gen_index", _GEN)
gen_index = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
sys.modules["gen_index"] = gen_index
_spec.loader.exec_module(gen_index)

# ---------------------------------------------------------------------------
# Fixtures — compact canonical shapes validated against the real ToF corpus.
# ---------------------------------------------------------------------------

FIXTURE_PRD = textwrap.dedent(
    """\
    # Project PRD

    ## 4 Requirements

    #### FR-1: Sensor boot and address assignment

    The Bridge can boot two attached sensors and assign each a unique I3C
    address. Validates FR-2 indirectly during boot validation.

    **Consequences (testable):**
    - Both sensors reach addressable state without manual intervention.

    #### FR-8b: Geometric calibration data via normal capture

    First-attempt geometric calibration rides on the normal capture path. The
    overlay shares FR-8b's collateral-necessity framing (self-reference check).

    ## 7. Success Metrics

    *Each SM cross-references the FR(s) it validates.*

    - **SM-1**: Sustained per-sensor frame rate — ≥ 60 FPS. Validates FR-1, FR-8b.
    - **SM-C1**: Bridge firmware polish — over-investing is misdirected. Counterbalances SM-1.
    """
)

FIXTURE_SPINE = textwrap.dedent(
    """\
    # Architecture Spine

    ### AD-1 — The bridge contract is a hex port

    - **Binds:** all (FR-1, SM-1; the contract every layer rests on).
    - **Prevents:** sensor detail leaking upstream.
    - **Rule:** The bridge upstream surface is exactly and only crop commands in and frames out. The surface is versioned. Refer to AD-2 for the SPI side.
    """
)

FIXTURE_SPEC = textwrap.dedent(
    """\
    # SPEC

    ## Capabilities

    - **CAP-1 — Bridge dual-sensor management**
      - **intent:** A bridge brings its two sensors to addressable state and sustains a cropped stream.
      - **success:** Both sensors addressable; ≥60 FPS; no cross-stream interruption. (FR-1, FR-8b; SM-1)

    ## Constraints
    """
)

FIXTURE_EPICS = textwrap.dedent(
    """\
    # Epics

    ## Requirements Inventory

    ### FR Coverage Map

    Every FR homed to exactly one epic:

    - FR-1 → E1a (sensor boot — absorbed)
    - FR-8b → E2 (geometric-cal capture)

    ## Epic List

    ### Epic E1a: Foundation — Skeletons

    Tibor takes over the bridge firmware and stands up skeletons. Bootstraps the acceptance harness. Lowest-risk epic.
    **FRs covered:** FR-1
    **Needs:** none

    ### Epic E2: Port Driver

    The N6 production port driver producing the 3DMD stream. Subscribe surface validated by the probe.
    **FRs covered:** FR-8b
    **Needs:** E1a (skeletons)

    ## Epic E1a: Foundation — Skeletons

    ### Story E1a.1: Absorb bridge-firmware

    As Tibor (engineer),
    I want the existing firmware absorbed into BMAD,
    So that FR-1 boot is formalized. Validates against Story E1a.2 downstream.

    ### Story E1a.2: N6 Zephyr skeleton

    As Tibor (engineer),
    I want the N6 Zephyr workspace initialized,
    So that the SPI test-master has a home.

    ## Epic E2: Port Driver

    ### Story E2.1: Production SPI port driver (AD-1)

    As Tibor (engineer),
    I want the one port driver across all SPI bridges,
    So that FR-8b geometric capture has a path. Depends on Story E1a.1.

    - **NFR-1 (Observability):** Every layer exposes state to diagnose any FR failure. Per-zone stats (FR-1) + CAPTURE are the surfaces.
    """
)


def _seed_project(project_root: Path) -> None:
    """Write the fixture canonical tree a BMad project resolves."""
    pa = project_root / "_bmad-output" / "planning-artifacts"
    (pa / "prds" / "p").mkdir(parents=True)
    (pa / "prds" / "p" / "prd.md").write_text(FIXTURE_PRD, encoding="utf-8")
    (pa / "architecture" / "v1").mkdir(parents=True)
    (pa / "architecture" / "v1" / "ARCHITECTURE-SPINE.md").write_text(FIXTURE_SPINE, encoding="utf-8")
    (pa / "epics.md").write_text(FIXTURE_EPICS, encoding="utf-8")
    specs = project_root / "_bmad-output" / "specs" / "s"
    specs.mkdir(parents=True)
    (specs / "SPEC.md").write_text(FIXTURE_SPEC, encoding="utf-8")
    (project_root / "_bmad").mkdir(parents=True, exist_ok=True)
    (project_root / "_bmad" / "config.toml").write_text(
        '[core]\nproject_name = "Fixture"\n[modules.bmm]\nplanning_artifacts = "'
        + str(project_root) + '/_bmad-output/planning-artifacts"\n',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


class TestTokenizer(unittest.TestCase):
    def test_story_before_epic_no_double_count(self) -> None:
        # "E1a.1" must register as Story only, not also as Epic E1a.
        refs = gen_index.extract_all_refs("see Story E1a.1 and Epic E1a", own_id="X")
        self.assertIn("E1a.1", refs)
        self.assertIn("E1a", refs)
        # No duplicate E1a from inside E1a.1.
        self.assertEqual(refs.count("E1a"), 1)
        self.assertEqual(refs.count("E1a.1"), 1)

    def test_letter_suffix_and_cross_cutting(self) -> None:
        refs = gen_index.extract_all_refs("FR-8b and SM-C1 plus SM-2", own_id="X")
        self.assertIn("FR-8b", refs)
        self.assertIn("SM-C1", refs)
        self.assertIn("SM-2", refs)

    def test_self_excluded_and_deduped(self) -> None:
        refs = gen_index.extract_all_refs("FR-1 cites FR-1 and FR-2 and FR-2", own_id="FR-1")
        self.assertEqual(refs, ["FR-2"])


# ---------------------------------------------------------------------------
# BMad config layer precedence (4-layer resolver: base < custom, team < personal)
# ---------------------------------------------------------------------------


class TestConfigLayerPrecedence(unittest.TestCase):
    def test_custom_user_wins_over_base_and_custom_team(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            (root / "_bmad" / "config.toml").write_text(
                (root / "_bmad" / "config.toml").read_text(encoding="utf-8")
                + '\n[modules.onav]\nonav_vault_root = "/base"\n',
                encoding="utf-8",
            )
            custom_dir = root / "_bmad" / "custom"
            custom_dir.mkdir()
            (custom_dir / "config.toml").write_text(
                '[modules.onav]\nonav_vault_root = "/team-custom"\n', encoding="utf-8"
            )
            (custom_dir / "config.user.toml").write_text(
                '[modules.onav]\nonav_vault_root = "/personal-custom"\n', encoding="utf-8"
            )
            ctx = gen_index.resolve_context(root, None)
            self.assertEqual(str(ctx.vault_root), "/personal-custom")

    def test_custom_dir_is_optional(self) -> None:
        # No _bmad/custom/ at all — must not error, falls through to base config.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            ctx = gen_index.resolve_context(root, str(Path(tmp) / "vault"))
            self.assertEqual(ctx.vault_root, (Path(tmp) / "vault").resolve())


# ---------------------------------------------------------------------------
# Project slug override (exact-case, org-nested layouts)
# ---------------------------------------------------------------------------


class TestProjectSlugOverride(unittest.TestCase):
    def test_default_slug_is_lowercase_kebab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            ctx = gen_index.resolve_context(root, str(Path(tmp) / "vault"))
            self.assertEqual(ctx.project_slug, "fixture")

    def test_cli_override_preserves_exact_case(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            ctx = gen_index.resolve_context(
                root, str(Path(tmp) / "vault"), project_slug_override="ToF-Tracking-WS"
            )
            self.assertEqual(ctx.project_slug, "ToF-Tracking-WS")

    def test_config_override_used_when_no_cli_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            (root / "_bmad" / "config.user.toml").write_text(
                '[modules.onav]\nonav_project_slug = "ToF-Tracking-WS"\n', encoding="utf-8"
            )
            ctx = gen_index.resolve_context(root, str(Path(tmp) / "vault"))
            self.assertEqual(ctx.project_slug, "ToF-Tracking-WS")

    def test_cli_override_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            (root / "_bmad" / "config.user.toml").write_text(
                '[modules.onav]\nonav_project_slug = "from-config"\n', encoding="utf-8"
            )
            ctx = gen_index.resolve_context(
                root, str(Path(tmp) / "vault"), project_slug_override="from-cli"
            )
            self.assertEqual(ctx.project_slug, "from-cli")

    def test_nested_projects_subfolder_combines_with_slug_for_org_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            (root / "_bmad" / "config.user.toml").write_text(
                '[modules.onav]\nonav_projects_subfolder = "projects/BlendArtis"\n', encoding="utf-8"
            )
            ctx = gen_index.resolve_context(
                root, str(Path(tmp) / "vault"), project_slug_override="ToF-Tracking-WS"
            )
            self.assertEqual(
                ctx.project_dir, Path(tmp) / "vault" / "projects" / "BlendArtis" / "ToF-Tracking-WS"
            )

    def test_cli_projects_subfolder_override_wins_over_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            (root / "_bmad" / "config.user.toml").write_text(
                '[modules.onav]\nonav_projects_subfolder = "from-config"\n', encoding="utf-8"
            )
            ctx = gen_index.resolve_context(
                root,
                str(Path(tmp) / "vault"),
                project_slug_override="ToF-Tracking-WS",
                projects_subfolder_override="projects/BlendArtis",
            )
            self.assertEqual(
                ctx.project_dir, Path(tmp) / "vault" / "projects" / "BlendArtis" / "ToF-Tracking-WS"
            )

    def test_path_traversal_in_override_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            with self.assertRaises(SystemExit):
                gen_index.resolve_context(
                    root, str(Path(tmp) / "vault"), project_slug_override="../../etc"
                )


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


class TestReaders(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        self.root = Path(self.tmp) / "proj"
        _seed_project(self.root)
        self.prd = self.root / "_bmad-output" / "planning-artifacts" / "prds" / "p" / "prd.md"
        self.spine = self.root / "_bmad-output" / "planning-artifacts" / "architecture" / "v1" / "ARCHITECTURE-SPINE.md"
        self.spec = self.root / "_bmad-output" / "specs" / "s" / "SPEC.md"
        self.epics = self.root / "_bmad-output" / "planning-artifacts" / "epics.md"

    def test_fr_reader_letter_suffix_and_refs(self) -> None:
        frs = {e.id: e for e in gen_index.read_prd_frs(self.prd, self.root)}
        self.assertIn("FR-1", frs)
        self.assertIn("FR-8b", frs)
        self.assertIn("unique I3C address", frs["FR-1"].definition)
        # FR-8b self-reference excluded.
        self.assertNotIn("FR-8b", frs["FR-8b"].references)

    def test_sm_reader_primary_and_cross_cutting(self) -> None:
        sms = {e.id: e for e in gen_index.read_prd_sms(self.prd, self.root)}
        self.assertIn("SM-1", sms)
        self.assertIn("SM-C1", sms)
        # SM-1 validates FR-1 and FR-8b.
        self.assertIn("FR-1", sms["SM-1"].references)
        self.assertIn("FR-8b", sms["SM-1"].references)
        # SM-C1 counterbalances SM-1 (cross-cutting reference).
        self.assertIn("SM-1", sms["SM-C1"].references)

    def test_ad_reader_binds_field(self) -> None:
        ads = {e.id: e for e in gen_index.read_spine_ads(self.spine, self.root)}
        self.assertIn("AD-1", ads)
        self.assertEqual(ads["AD-1"].title, "The bridge contract is a hex port")
        # Binds carries FR-1 and SM-1; Rule mentions AD-2.
        self.assertIn("FR-1", ads["AD-1"].references)
        self.assertIn("SM-1", ads["AD-1"].references)
        self.assertIn("AD-2", ads["AD-1"].references)

    def test_cap_reader_intent_and_success_refs(self) -> None:
        caps = {e.id: e for e in gen_index.read_spec_caps(self.spec, self.root)}
        self.assertIn("CAP-1", caps)
        self.assertEqual(caps["CAP-1"].title, "Bridge dual-sensor management")
        self.assertIn("addressable state", caps["CAP-1"].definition)  # intent text
        # success parens carry FR + SM refs.
        self.assertIn("FR-1", caps["CAP-1"].references)
        self.assertIn("FR-8b", caps["CAP-1"].references)
        self.assertIn("SM-1", caps["CAP-1"].references)

    def test_nfr_reader_from_epics(self) -> None:
        nfrs = {e.id: e for e in gen_index.read_epics_nfrs(self.epics, self.root)}
        self.assertIn("NFR-1", nfrs)
        self.assertEqual(nfrs["NFR-1"].title, "Observability")
        self.assertIn("FR-1", nfrs["NFR-1"].references)

    def test_epics_reader_coverage_map_and_dedupe(self) -> None:
        epics, stories, coverage = gen_index.read_epics(self.epics, self.root)
        epic_ids = [e.id for e in epics]
        # Deduped: E1a and E2 each appear twice in the fixture (list + content).
        self.assertEqual(sorted(epic_ids), ["E1a", "E2"])
        # Coverage map edges.
        self.assertIn(("FR-1", "E1a"), coverage)
        self.assertIn(("FR-8b", "E2"), coverage)
        # Epic E2 references its FR + Needs E1a.
        e2 = next(e for e in epics if e.id == "E2")
        self.assertIn("FR-8b", e2.references)
        self.assertIn("E1a", e2.references)

    def test_story_reader_user_story_and_cross_refs(self) -> None:
        _epics, stories, _cov = gen_index.read_epics(self.epics, self.root)
        s = {e.id: e for e in stories}
        self.assertIn("E1a.1", s)
        self.assertIn("E2.1", s)
        # E2.1 cites AD-1 and depends on Story E1a.1.
        self.assertIn("AD-1", s["E2.1"].references)
        self.assertIn("E1a.1", s["E2.1"].references)
        # E1a.1 definition came from the 'I want' clause.
        self.assertIn("firmware absorbed", s["E1a.1"].definition)


# ---------------------------------------------------------------------------
# Relationship graph
# ---------------------------------------------------------------------------


class TestRelationshipGraph(unittest.TestCase):
    def test_coverage_map_folds_into_fr_outgoing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            ctx = gen_index.resolve_context(root, str(Path(tmp) / "vault"))
            entities, _shas = gen_index._collect_entities(ctx)
            fr1 = next(e for e in entities if e.id == "FR-1")
            # Coverage map said FR-1 -> E1a: the epic is now in FR-1's outgoing.
            self.assertIn("E1a", fr1.references)

    def test_bidirectional_incoming(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            ctx = gen_index.resolve_context(root, str(Path(tmp) / "vault"))
            entities, _shas = gen_index._collect_entities(ctx)
            incoming = gen_index.build_incoming(entities)
            # AD-1 binds FR-1 -> FR-1's incoming includes AD-1.
            self.assertIn("AD-1", incoming["FR-1"])
            # SM-1 validates FR-1 -> FR-1's incoming includes SM-1.
            self.assertIn("SM-1", incoming["FR-1"])
            # CAP-1 success includes FR-1 -> FR-1's incoming includes CAP-1.
            self.assertIn("CAP-1", incoming["FR-1"])


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


class TestEmission(unittest.TestCase):
    def test_note_emits_outgoing_static_and_incoming_dataview(self) -> None:
        ent = gen_index.Entity(
            id="FR-1", type="FR", title="Sensor boot",
            definition="The Bridge boots two sensors.",
            source_path="prd.md", source_anchor="FR-1",
            references=["FR-2", "E1a"],
        )
        title_by_id = {"FR-2": "Independent crop-window configuration", "E1a": "Foundation"}
        with tempfile.TemporaryDirectory() as tmp:
            note = gen_index.emit_entity_note(
                ent, Path(tmp) / "FR", source_sha="abc", project_subpath="projects/proj",
                title_by_id=title_by_id,
            )
            text = note.read_text(encoding="utf-8")
            # Tier 2: outgoing references are path-qualified wiki-links with
            # explicit alias: [[projects/proj/FR/FR-2|FR-2]] — path qualification
            # prevents cross-project resolution when multiple projects share the
            # same entity IDs. The alias keeps "ID" as display text.
            self.assertIn("## Relationships", text)
            self.assertIn("### References", text)
            self.assertIn("[[projects/proj/FR/FR-2|FR-2]] — Independent crop-window configuration", text)
            self.assertIn("[[projects/proj/Epic/E1a|E1a]] — Foundation", text)
            # Tier 1: incoming is a live Dataview LIST, scoped to the project,
            # self-aliased via link() for the same plugin-immunity reason —
            # kept deliberately simple (no Type/Title columns): both a
            # concatenated LIST expression and a TABLE with those columns
            # failed to render in practice, so this is the reliable fallback.
            self.assertIn("```dataview", text)
            self.assertIn('FROM [[]] AND "projects/proj"', text)
            self.assertIn("LIST WITHOUT ID link(file.name, file.name)", text)
            self.assertNotIn("Title", text)
            # No static incoming list remains (replaced by Dataview).
            self.assertNotIn("- [[AD-1", text)

    def test_reference_without_known_title_stays_bare(self) -> None:
        # A missing-note gap (referenced ID with no title yet known) must not
        # crash or render a bogus suffix — just the path-qualified link, no title.
        ent = gen_index.Entity(
            id="FR-1", type="FR", title="Sensor boot", definition="d",
            source_path="prd.md", source_anchor="FR-1", references=["FR-99"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            note = gen_index.emit_entity_note(
                ent, Path(tmp) / "FR", source_sha="abc", project_subpath="projects/proj",
                title_by_id={},
            )
            text = note.read_text(encoding="utf-8")
            # Path-qualified + aliased, no title suffix.
            self.assertIn("- [[projects/proj/FR/FR-99|FR-99]]\n", text)
            self.assertNotIn("[[projects/proj/FR/FR-99|FR-99]] —", text)

    def test_note_tag_taxonomy_in_frontmatter(self) -> None:
        ent = gen_index.Entity(
            id="AD-1", type="AD", title="Bridge contract",
            definition="The bridge contract is a hex port.",
            source_path="spine.md", source_anchor="AD-1", references=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            note = gen_index.emit_entity_note(
                ent, Path(tmp) / "AD", source_sha="abc", project_subpath="projects/p",
                title_by_id={},
            )
            text = note.read_text(encoding="utf-8")
            self.assertIn("onav/AD", text)  # type tag
            self.assertIn("onav/stable", text)  # status tag

    def test_note_with_no_outgoing_still_shows_backlinks_query(self) -> None:
        ent = gen_index.Entity(
            id="FR-8b", type="FR", title="Geometric cal",
            definition="Calibration via normal capture.",
            source_path="prd.md", source_anchor="FR-8b", references=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            note = gen_index.emit_entity_note(
                ent, Path(tmp) / "FR", source_sha="abc", project_subpath="projects/p",
                title_by_id={},
            )
            text = note.read_text(encoding="utf-8")
            # Orphan-friendly: still surfaces a live Referenced by query.
            self.assertIn("## Referenced by", text)
            self.assertIn("```dataview", text)


# ---------------------------------------------------------------------------
# Personal notes preservation (M4 trust foundation)
# ---------------------------------------------------------------------------


class TestPersonalNotesPreservation(unittest.TestCase):
    def _emit(self, ent, tmp, project_subpath="projects/p"):
        return gen_index.emit_entity_note(
            ent, Path(tmp) / ent.type, source_sha="abc", project_subpath=project_subpath,
            title_by_id={},
        )

    def test_personal_notes_survive_regen(self) -> None:
        ent = gen_index.Entity(
            id="FR-1", type="FR", title="Sensor boot",
            definition="Boots two sensors.",
            source_path="prd.md", source_anchor="FR-1", references=["FR-2"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            note = self._emit(ent, tmp)
            # User adds personal notes.
            note.write_text(
                note.read_text(encoding="utf-8") + "\n## Personal notes\n\nMy hard-won insight.\n",
                encoding="utf-8",
            )
            # Regenerate (definition/ref change simulated).
            ent.definition = "Boots two sensors, revised."
            note2 = self._emit(ent, tmp)
            text = note2.read_text(encoding="utf-8")
            self.assertIn("## Personal notes", text)
            self.assertIn("My hard-won insight.", text)
            # The regen change landed too.
            self.assertIn("Boots two sensors, revised.", text)

    def test_preservation_is_universal_across_repeated_regens(self) -> None:
        ent = gen_index.Entity(
            id="AD-1", type="AD", title="Bridge contract",
            definition="Hex port.",
            source_path="spine.md", source_anchor="AD-1", references=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            self._emit(ent, tmp)
            p = Path(tmp) / "AD" / "AD-1.md"
            p.write_text(p.read_text(encoding="utf-8") + "\n## Personal notes\n\nnote A\n", encoding="utf-8")
            # Regen three times; the note must survive all of them.
            for _ in range(3):
                self._emit(ent, tmp)
            self.assertIn("note A", p.read_text(encoding="utf-8"))

    def test_no_personal_notes_no_artifacts(self) -> None:
        ent = gen_index.Entity(
            id="SM-1", type="SM", title="Frame rate",
            definition="60 FPS.",
            source_path="prd.md", source_anchor="SM-1", references=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            note = self._emit(ent, tmp)
            text = note.read_text(encoding="utf-8")
            # No Personal notes SECTION (heading at line start). The generated
            # comment documents the heading name, so a bare substring check
            # would false-positive on the comment — match the heading as a line.
            self.assertIsNone(re.search(r"^## Personal notes\s*$", text, re.MULTILINE))


# ---------------------------------------------------------------------------
# update — the workhorse (M4)
# ---------------------------------------------------------------------------


class TestUpdate(unittest.TestCase):
    def test_update_regenerates_canvas_with_full_graph_not_just_changed_entity(self) -> None:
        # Regression: the canvas must always show the full epic/story/FR graph,
        # even though `update` only rewrites the requested entity's own note.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            canvas_path = vault / "projects" / "fixture" / "structure.canvas"
            before_ids = {n["id"] for n in json.loads(canvas_path.read_text(encoding="utf-8"))["nodes"]}

            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "update", "FR-1"])
            after_ids = {n["id"] for n in json.loads(canvas_path.read_text(encoding="utf-8"))["nodes"]}

            self.assertEqual(before_ids, after_ids)
            self.assertIn("E2.1", after_ids)  # a node untouched by this update

    def test_update_refreshes_and_reports_backlinks_and_preserves_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            proj = vault / "projects" / "fixture"

            # Inject personal notes into FR-1 before updating.
            fr1 = proj / "FR" / "FR-1.md"
            fr1.write_text(
                fr1.read_text(encoding="utf-8") + "\n## Personal notes\n\nkeep me\n",
                encoding="utf-8",
            )

            rc = gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "update", "FR-1"])
            self.assertEqual(rc, 0)
            # Personal notes survived.
            self.assertIn("keep me", fr1.read_text(encoding="utf-8"))
            self.assertIn("## Personal notes", fr1.read_text(encoding="utf-8"))

    def test_update_unknown_id_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            rc = gen_index.main(
                ["--project-root", str(root), "--vault-root", str(vault), "update", "FR-999"]
            )
            self.assertEqual(rc, 1)

    def test_update_yes_all_cascades_affected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            # FR-1 is bound by AD-1, validated by SM-1, covered by CAP-1.
            # Capture pre-update last_reviewed of AD-1 to prove it was rewritten.
            ad1 = vault / "projects" / "fixture" / "AD" / "AD-1.md"
            before = ad1.read_text(encoding="utf-8")
            import time
            time.sleep(1.1)  # ensure last_reviewed timestamp advances
            gen_index.main(
                ["--project-root", str(root), "--vault-root", str(vault), "update", "FR-1", "--yes-all"]
            )
            after = ad1.read_text(encoding="utf-8")
            # AD-1 was cascaded-refreshed: its last_reviewed advanced.
            self.assertNotEqual(
                _frontmatter_value(before, "last_reviewed"),
                _frontmatter_value(after, "last_reviewed"),
            )


def _frontmatter_value(note_text: str, key: str) -> str:
    m = re.search(rf"^{re.escape(key)}:\s*'?(.+?)'?\s*$", note_text, re.MULTILINE)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Dashboard + Canvas (M6)
# ---------------------------------------------------------------------------


class TestDashboard(unittest.TestCase):
    def test_dashboard_has_dataview_blocks_and_no_static_entity_links(self) -> None:
        """Orphan integrity: the dashboard must use only Dataview for entity
        references. A static [[entity]] link would register as an inlink and
        break orphan detection. Coverage-gap IDs are shown as code spans."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            dash = (vault / "projects" / "fixture" / "index.md").read_text(encoding="utf-8")
            # Dataview queries present.
            self.assertIn("```dataview", dash)
            self.assertIn("length(file.inlinks)", dash)  # orphan + hotspot queries
            self.assertIn('FROM "projects/fixture"', dash)  # scoped to project
            # No static entity wiki-links anywhere in the dashboard.
            self.assertFalse(re.search(r"\[\[(FR-|AD-|SM-|CAP-|NFR-|E\d)", dash))

    def test_coverage_gaps_computed_from_canonical(self) -> None:
        ents = [
            gen_index.Entity("FR-1", "FR", "t", "d", "p", "FR-1", references=[]),
            gen_index.Entity("FR-2", "FR", "t", "d", "p", "FR-2", references=[]),
            gen_index.Entity("E1", "Epic", "t", "d", "p", "E1", references=["FR-1"]),  # realizes FR-1
            gen_index.Entity("AD-1", "AD", "t", "d", "p", "AD-1", references=["FR-1"]),  # binds FR-1
            gen_index.Entity("AD-2", "AD", "t", "d", "p", "AD-2", references=[]),  # binds nothing
        ]
        gaps = gen_index._compute_coverage_gaps(ents)
        self.assertEqual(gaps["uncovered_frs"], ["FR-2"])  # no Epic/Story references it
        self.assertEqual(gaps["unbinding_ads"], ["AD-2"])  # references no FR/SM


class TestCanvas(unittest.TestCase):
    def test_canvas_nodes_are_readable_text_labels_not_dense_file_embeds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            canvas_path = vault / "projects" / "fixture" / "structure.canvas"
            self.assertTrue(canvas_path.exists())
            canvas = json.loads(canvas_path.read_text(encoding="utf-8"))
            by_id = {n["id"]: n for n in canvas["nodes"]}
            # Known fixture IDs across all three columns.
            for nid in ("E1a", "E2", "E1a.1", "E2.1", "FR-1", "FR-8b"):
                self.assertIn(nid, by_id)
                node = by_id[nid]
                # Text node with an aliased wikilink (clickable, navigates to the
                # real note) rather than a dense type:"file" live embed.
                self.assertEqual(node["type"], "text")
                self.assertTrue(node["text"].startswith(f"[[{nid}|{nid} — "))
                self.assertNotIn("file", node)
                # Boxes are big enough that the label is visible without zooming.
                self.assertGreaterEqual(node["width"], 300)
                self.assertGreaterEqual(node["height"], 80)
            # Edges carry the 'belongs to' (story->epic) and/or 'realizes' labels.
            labels = {e.get("label") for e in canvas["edges"]}
            self.assertIn("belongs to", labels)


class TestGraphColoringNote(unittest.TestCase):
    def test_note_emitted_once_then_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            note = vault / "projects" / "_setup-notes" / "graph-coloring.md"
            self.assertTrue(note.exists())
            first = note.read_text(encoding="utf-8")
            # Second run must NOT overwrite (user may have edited it).
            user_edit = "\nMY CUSTOM NOTE\n"
            note.write_text(first + user_edit, encoding="utf-8")
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            self.assertIn(user_edit.strip(), note.read_text(encoding="utf-8"))


class TestVaultRootMOC(unittest.TestCase):
    def test_mixed_case_slug_upsert_does_not_duplicate_on_repeat(self) -> None:
        # Regression: the exclude-match must be case-insensitive on BOTH sides.
        # A mixed-case slug (e.g. from --project-slug) previously never matched
        # its own prior entry, so repeated runs appended a new line every time.
        existing = ""
        for _ in range(3):
            existing = gen_index._render_vault_root_moc(
                existing, "ToF-Tracking-WS", "ToF-Tracking-WS", {"FR": 17}
            )
        entry_lines = [ln for ln in existing.splitlines() if ln.strip().startswith("- [[")]
        self.assertEqual(len(entry_lines), 1)

    def test_other_projects_preserved_across_upsert(self) -> None:
        existing = gen_index._render_vault_root_moc("", "project-a", "Project A", {"FR": 1})
        existing = gen_index._render_vault_root_moc(existing, "project-b", "Project B", {"FR": 2})
        # Re-upsert project-a (e.g. a refresh) must not drop project-b's entry.
        existing = gen_index._render_vault_root_moc(existing, "project-a", "Project A", {"FR": 5})
        self.assertIn("project-a/index", existing)
        self.assertIn("project-b/index", existing)
        entry_lines = [ln for ln in existing.splitlines() if ln.strip().startswith("- [[")]
        self.assertEqual(len(entry_lines), 2)


# ---------------------------------------------------------------------------
# refresh — regenerate-all with pruning (M5)
# ---------------------------------------------------------------------------


class TestRefresh(unittest.TestCase):
    def _init(self, root: Path, vault: Path) -> None:
        gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])

    def test_refresh_prunes_unannotated_leftover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            self._init(root, vault)
            proj = vault / "projects" / "fixture"
            # Inject an un-annotated leftover (entity not in canonical).
            stale = proj / "FR" / "FR-999.md"
            stale.write_text("---\nid: FR-999\ntype: FR\n---\n# FR-999\ngone\n", encoding="utf-8")
            self.assertTrue(stale.exists())

            rc = gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "refresh"])
            self.assertEqual(rc, 0)
            # Pruned: the leftover is gone.
            self.assertFalse(stale.exists())

    def test_refresh_keeps_annotated_leftover_and_flags_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            self._init(root, vault)
            proj = vault / "projects" / "fixture"
            annotated = proj / "FR" / "FR-888.md"
            annotated.write_text(
                "---\nid: FR-888\ntype: FR\n---\n# FR-888\ngone\n\n## Personal notes\n\nkeep me\n",
                encoding="utf-8",
            )

            rc = gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "refresh"])
            self.assertEqual(rc, 0)
            # Trust foundation holds even for leftovers: annotated note survives.
            self.assertTrue(annotated.exists())
            self.assertIn("keep me", annotated.read_text(encoding="utf-8"))
            # A refresh-log was written.
            logs = list(proj.glob("_refresh-log-*.md"))
            self.assertEqual(len(logs), 1)
            log_text = logs[0].read_text(encoding="utf-8")
            self.assertIn("FR-888", log_text)  # flagged for manual review
            self.assertIn("Kept", log_text)

    def test_refresh_preserves_personal_notes_of_existing_entities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            self._init(root, vault)
            proj = vault / "projects" / "fixture"
            ad1 = proj / "AD" / "AD-1.md"
            ad1.write_text(
                ad1.read_text(encoding="utf-8") + "\n## Personal notes\n\nsurvives refresh\n",
                encoding="utf-8",
            )
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "refresh"])
            self.assertIn("survives refresh", ad1.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# End-to-end CLI
# ---------------------------------------------------------------------------


class TestInitEndToEnd(unittest.TestCase):
    def test_init_all_types_emitted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            rc = gen_index.main(
                ["--project-root", str(root), "--vault-root", str(vault), "init", "--force"]
            )
            self.assertEqual(rc, 0)
            proj = vault / "projects" / "fixture"
            # All seven types present.
            for typ, nid in [("FR", "FR-1"), ("SM", "SM-C1"), ("AD", "AD-1"),
                             ("CAP", "CAP-1"), ("NFR", "NFR-1"), ("Epic", "E1a"),
                             ("Story", "E2.1")]:
                self.assertTrue((proj / typ / (nid + ".md")).exists(), f"missing {typ}/{nid}.md")
            # MOC + vault-root MOC.
            self.assertTrue((proj / "index.md").exists())
            self.assertIn("fixture", (vault / "projects" / "index.md").read_text(encoding="utf-8"))

    def test_init_without_vault_root_errors_clearly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            with self.assertRaises(SystemExit) as cm:
                gen_index.main(["--project-root", str(root), "init", "--force"])
            self.assertIn("vault root", str(cm.exception).lower())


# ---------------------------------------------------------------------------
# Manifest mode + doctor (M7)
# ---------------------------------------------------------------------------


class TestManifestMode(unittest.TestCase):
    def test_manifest_init_writes_nothing_but_outputs_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            # Capture stdout JSON.
            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = gen_index.main(
                    ["--project-root", str(root), "--vault-root", str(vault),
                     "--emit-mode", "manifest", "init", "--force"]
                )
            self.assertEqual(rc, 0)
            manifest = json.loads(buf.getvalue())
            self.assertEqual(manifest["emit_mode"], "manifest")
            self.assertGreater(manifest["summary"]["write_count"], 0)
            # Kinds present.
            kinds = {w["kind"] for w in manifest["writes"]}
            self.assertIn("note", kinds)
            self.assertIn("dashboard", kinds)
            self.assertIn("canvas", kinds)
            self.assertIn("vault_root_moc", kinds)
            # The vault directory must NOT have been created (manifest writes nothing).
            self.assertFalse(vault.exists())

    def test_manifest_preserves_personal_notes_in_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            # Seed via file mode, then add personal notes.
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            fr1 = vault / "projects" / "fixture" / "FR" / "FR-1.md"
            fr1.write_text(
                fr1.read_text(encoding="utf-8") + "\n## Personal notes\n\nsurvives in manifest\n",
                encoding="utf-8",
            )
            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                gen_index.main(
                    ["--project-root", str(root), "--vault-root", str(vault),
                     "--emit-mode", "manifest", "update", "FR-1"]
                )
            manifest = json.loads(buf.getvalue())
            fr1_write = next(w for w in manifest["writes"] if w.get("id") == "FR-1")
            self.assertTrue(fr1_write["has_personal_notes"])
            self.assertIn("survives in manifest", fr1_write["content"])

    def test_manifest_update_carries_only_changed_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            gen_index.main(["--project-root", str(root), "--vault-root", str(vault), "init", "--force"])
            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                gen_index.main(
                    ["--project-root", str(root), "--vault-root", str(vault),
                     "--emit-mode", "manifest", "update", "FR-1"]
                )
            manifest = json.loads(buf.getvalue())
            note_writes = [w for w in manifest["writes"] if w["kind"] == "note"]
            # Only FR-1 (the changed note), not every entity.
            self.assertEqual([w["id"] for w in note_writes], ["FR-1"])


class TestDoctor(unittest.TestCase):
    def test_doctor_reports_checks_and_uv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "proj"
            _seed_project(root)
            vault = Path(tmp) / "vault"
            import io
            import contextlib

            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = gen_index.main(
                    ["--project-root", str(root), "--vault-root", str(vault), "doctor"]
                )
            self.assertEqual(rc, 0)
            report = json.loads(buf.getvalue())
            self.assertEqual(report["status"], "ok")
            check_names = {c["check"] for c in report["checks"]}
            self.assertIn("uv on PATH", check_names)
            self.assertIn("Dataview plugin", check_names)
            self.assertIn("canonical: PRD", check_names)


if __name__ == "__main__":
    unittest.main()
