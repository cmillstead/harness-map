"""Tests for render_html.py per docs/plans/2026-07-15-harness-map-html-viz-design.md §5.
Real fixtures only (no mocks — the renderer is pure stdlib). Reuses `run_collector`
(test_collector.py:21) and `fake_harness` (conftest.py:13)."""
import importlib.util
import json
import os
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from test_collector import run_collector

RENDER = Path(__file__).resolve().parents[1] / "render_html.py"
REAL_SAMPLE = Path("/Users/cevin/Documents/obsidian-vault/AI/output/harness-map-2026-07-15.json")

_spec = importlib.util.spec_from_file_location("harness_map_render_html", RENDER)
rh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rh)


# --------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session", autouse=True)
def _fake_home_for_render_html_tests(tmp_path_factory):
    """§9-R D hermeticity guarantee: every test in this module runs under a fake $HOME,
    so a test that forgets to pass explicit `--*-file` fixture paths structurally
    CANNOT read the author's real telemetry streams."""
    fake_home = tmp_path_factory.mktemp("fake_home")
    original = os.environ.get("HOME")
    os.environ["HOME"] = str(fake_home)
    yield fake_home
    if original is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = original


def run_render(out_dir, *args, env=None, extra=None):
    cmd = [sys.executable, str(RENDER), "--out-dir", str(out_dir)] + list(args) + list(extra or [])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)
    return proc


class _ExternalRefParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.external = []
        self.on_handlers = []
        self.style_attrs = []
        self.tag_counts = {}

    def handle_starttag(self, tag, attrs):
        self.tag_counts[tag] = self.tag_counts.get(tag, 0) + 1
        for k, v in attrs:
            if k in ("src", "href", "srcset", "xlink:href") and v and v.startswith(("http://", "https://")):
                self.external.append((tag, k, v))
            if k.startswith("on"):
                self.on_handlers.append((tag, k))
            if k == "style":
                self.style_attrs.append((tag, v))


def _minimal_doc(extra_files=None, extra_promotion="", tokens_a=100, tokens_b=50):
    files = [
        {"path": "CLAUDE.md", "category": "claude_md", "words": 40, "lines": 5,
         "tokens_est": tokens_a, "evidence": "VERIFIED"},
        {"path": "rules/a.md", "category": "rule", "words": 20, "lines": 3,
         "tokens_est": tokens_b, "evidence": "VERIFIED"},
    ]
    if extra_files:
        files.extend(extra_files)
    doc = {
        "schema_version": 1,
        "generated_at": "2026-07-15T00:00:00+00:00",
        "root": "/fake/root",
        "headline": {
            "always_loaded_words": 60, "always_loaded_tokens_est": tokens_a + tokens_b,
            "always_loaded_file_count": len(files), "duplicate_pair_count": 1,
            "unchecked_binary_count": 0, "instruction_files_over_200": 0,
            "orphan_registration_count": 1, "orphan_script_count": 1,
        },
        "always_loaded": {
            "files": files, "conditional_variants": [], "skill_descriptions": [],
            "agent_descriptions": [], "totals": {"words": 60, "tokens_est": tokens_a + tokens_b,
                                                   "file_count": len(files)},
        },
        "on_demand": {
            "skills": [{"name": "coding-team", "lines": 50, "words": 300, "has_test": True,
                        "evidence": "VERIFIED"}],
            "skill_internal_bodies": [
                {"skill": "coding-team", "path": "skills/coding-team/phases/execution.md",
                 "kind": "phase", "lines": 10, "words": 80, "evidence": "VERIFIED"},
                {"skill": "coding-team", "path": "skills/coding-team/agents/ct-implementer.md",
                 "kind": "agent", "lines": 10, "words": 90, "evidence": "VERIFIED"},
            ],
            "memory_bodies": [{"path": "projects/x/memory/feedback_note.md", "project_slug": "x",
                                "lines": 5, "words": 40, "evidence": "VERIFIED"}],
        },
        "enforcement": {
            "hooks": {
                "registered": [{"command": "python3 ~/.claude/hooks/write-guard.py",
                                 "script": "hooks/write-guard.py", "exists": True,
                                 "registered_via": "direct", "registration_evidence": "VERIFIED",
                                 "target_evidence": "VERIFIED"}],
                "orphan_registrations": [{"script": "hooks/missing.py", "target_status": "missing",
                                           "registration_evidence": "VERIFIED"}],
                "scripts_on_disk": [{"name": "write-guard.py", "is_symlink": False, "target": None,
                                      "registered_via": "direct", "evidence": "VERIFIED",
                                      "description": "guards writes"},
                                     {"name": "orphan-script.py", "is_symlink": False, "target": None,
                                      "registered_via": "none", "evidence": "INFERRED",
                                      "description": ""}],
                "orphan_scripts": [{"name": "orphan-script.py", "evidence": "INFERRED"}],
            },
            "permissions": {"allow_count": 1, "deny_count": 1, "ask_count": 0, "evidence": "VERIFIED"},
        },
        "config": {"env_keys": ["FAKE_TOKEN"], "env_key_count": 1, "model": "opus", "cleanup_period_days": 1,
                    "sandbox": True, "enabled_plugins": [], "plugin_count": 0, "marketplaces": [],
                    "marketplace_count": 0, "installed_plugins": [], "installed_plugin_count": 0,
                    "evidence": "VERIFIED"},
        "instruction_length_flags": [],
        "duplication": {"shingle_k": 8, "metric": "containment", "threshold": 0.6,
                         "pairs": [{"a": "rules/a.md", "b": "rules/b.md", "score": 0.9,
                                    "shared_sample": "shared words here", "evidence": "INFERRED"}]},
        "phantom_refs": [{"source": "rules/a.md", "ref": "nope.md", "kind": "path",
                           "resolved": False, "evidence": "VERIFIED"}],
        "promotion_candidates": [{"source": "rules/a.md", "pattern": "NEVER",
                                   "excerpt": extra_promotion or "NEVER do the thing",
                                   "hook_covered": False, "evidence": "INFERRED"}],
        "test_coverage": {"hooks": [], "skills": [], "summary": {"hooks_with_test": 0, "hooks_total": 0,
                                                                   "skills_with_test": 0, "skills_total": 0}},
        "inaccessible": [], "blind_spots": ["a blind spot note"], "errors": [],
    }
    return doc


def _write_sidecar(out_dir, date, doc):
    (Path(out_dir) / f"harness-map-{date}.json").write_text(json.dumps(doc))


# ============================================================= 1. real-data smoke render
def test_real_data_smoke_render(tmp_path):
    if not REAL_SAMPLE.is_file():
        pytest.skip("real sample not present on this machine")
    out_dir = tmp_path / "real"
    out_dir.mkdir()
    (out_dir / REAL_SAMPLE.name).write_text(REAL_SAMPLE.read_text())
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    out_file = out_dir / "harness-map-2026-07-15.html"
    assert out_file.is_file()
    text = out_file.read_text(encoding="utf-8")

    p = _ExternalRefParser()
    p.feed(text)
    assert p.external == [], f"external resource refs found: {p.external}"

    doc = json.loads((out_dir / REAL_SAMPLE.name).read_text())
    for key, _, _ in rh.HEADLINE_KEYS:
        assert str(doc["headline"][key]) in text, f"headline value for {key} missing from HTML"


def test_real_data_smoke_render_with_friction_streams_present(tmp_path):
    """The actual demo invocation: friction streams default to real paths under
    (fake) $HOME. With no files there, every stream must degrade to 'absent', never crash."""
    if not REAL_SAMPLE.is_file():
        pytest.skip("real sample not present on this machine")
    out_dir = tmp_path / "real2"
    out_dir.mkdir()
    (out_dir / REAL_SAMPLE.name).write_text(REAL_SAMPLE.read_text())
    proc = run_render(out_dir, "--date", "2026-07-15")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "absent" in text


# ============================================================= 2. transform correctness
def test_build_contextweight_model_groups_and_cells():
    doc = _minimal_doc()
    model = rh.build_contextweight_model(doc)
    cats = {c["category"] for c in model["always"]["groups"]}
    assert cats == {"claude_md", "rule"}
    cells = model["always"]["cells"]
    assert {c["path"] for c in cells} == {"CLAUDE.md", "rules/a.md"}
    node_keys = {c["node_key"] for c in cells}
    assert node_keys == {"always_loaded:CLAUDE.md", "always_loaded:rules/a.md"}


def test_build_contextweight_model_on_demand_groups():
    doc = _minimal_doc()
    model = rh.build_contextweight_model(doc)
    od_groups = {c["category"] for c in model["on_demand"]["groups"]}
    assert od_groups == {"skill", "phase", "agent", "memory"}
    od_node_keys = {c["node_key"] for c in model["on_demand"]["cells"]}
    assert "on_demand:coding-team" in od_node_keys
    assert "on_demand:skills/coding-team/phases/execution.md" in od_node_keys


def test_build_bipartite_model_direct_edges_and_orphans():
    doc = _minimal_doc()
    model = rh.build_bipartite_model(doc)
    assert len(model["left"]) == 1
    assert model["left"][0]["node_key"] == "hook:write-guard.py"
    assert len(model["left_orphans"]) == 1
    assert model["left_orphans"][0]["script"] == "hooks/missing.py"
    right_names = {n["name"]: n["registered_via"] for n in model["right"]}
    assert right_names == {"write-guard.py": "direct", "orphan-script.py": "none"}
    assert len(model["edges"]) == 1
    assert model["edges"][0] == {"from": "hook:write-guard.py", "to": "hook:write-guard.py"}
    assert model["orphan_script_count"] == 1


def test_build_bipartite_model_propagates_script_description():
    doc = _minimal_doc()
    doc["enforcement"]["hooks"]["scripts_on_disk"][0]["description"] = "guards writes"
    model = rh.build_bipartite_model(doc)          # takes the WHOLE doc (see L199 test)
    assert any(n.get("description") == "guards writes" for n in model["right"])
    # a sidecar WITHOUT the field still builds (reader-tolerant .get -> "")
    doc2 = _minimal_doc()
    for s in doc2["enforcement"]["hooks"]["scripts_on_disk"]:
        s.pop("description", None)
    m2 = rh.build_bipartite_model(doc2)
    assert all(n.get("description") == "" for n in m2["right"])


def test_build_trend_model_multi_sidecar_series():
    doc1 = _minimal_doc(tokens_a=100, tokens_b=50)
    doc2 = _minimal_doc(tokens_a=200, tokens_b=50)
    model = rh.build_trend_model([("2026-07-14", doc1), ("2026-07-15", doc2)])
    assert model["dates"] == ["2026-07-14", "2026-07-15"]
    assert model["first_run"] is False
    tokens_series = next(s for s in model["series"] if s["key"] == "always_loaded_tokens_est")
    assert tokens_series["values"] == [150, 250]


def test_build_trend_model_single_sidecar_first_run():
    doc = _minimal_doc()
    model = rh.build_trend_model([("2026-07-15", doc)])
    assert model["first_run"] is True


def test_build_dupweb_model_nodes_edges_phantom_refs():
    doc = _minimal_doc()
    model = rh.build_dupweb_model(doc)
    assert {n["path"] for n in model["nodes"]} == {"rules/a.md", "rules/b.md"}
    assert len(model["edges"]) == 1
    assert model["edges"][0]["a"] == "always_loaded:rules/a.md"
    assert model["edges"][0]["b"] == "always_loaded:rules/b.md"
    assert len(model["phantom_refs"]) == 1


def test_build_civc_model_exact_36_cell_key_set():
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered"},
        {"verb": "Evolve", "surface": "observability", "verdict": "thin"},
    ], "drag_candidates": []}
    model = rh.build_civc_model(synth)
    assert model["available"] is True
    key_set = {(c["verb"], c["surface"]) for c in model["cells"]}
    expected = {(v, s) for v in rh.VERBS for s in rh.SURFACES}
    assert key_set == expected
    assert len(model["cells"]) == 36
    afford_context = next(c for c in model["cells"] if c["verb"] == "Afford" and c["surface"] == "context")
    assert afford_context["verdict"] == "covered"
    empty_cell = next(c for c in model["cells"] if c["verb"] == "Constrain" and c["surface"] == "tools")
    assert empty_cell["verdict"] == "empty"


def test_build_civc_model_absent_synthesis_empty_state():
    model = rh.build_civc_model(None)
    assert model == {"available": False, "cells": []}


def test_build_dragcandidate_model_sorted_by_n():
    synth = {"drag_candidates": [{"n": 2, "surface": "memory", "evidence": "V", "outcome": "keep",
                                   "what_must_survive": "", "risk_if_wrong": ""},
                                  {"n": 1, "surface": "context", "evidence": "I", "outcome": "probation",
                                   "what_must_survive": "", "risk_if_wrong": ""}]}
    model = rh.build_dragcandidate_model(synth)
    assert [r["n"] for r in model["rows"]] == [1, 2]


def test_build_dragcandidate_model_absent_synthesis_empty_state():
    model = rh.build_dragcandidate_model(None)
    assert model == {"available": False, "rows": []}


def test_drag_outcome_labels_known_and_fallback():
    assert rh._drag_outcome_label("probation").startswith("Demotion candidate")
    assert rh._drag_outcome_label("give it one home").startswith("Consolidate")
    assert rh._drag_outcome_label("weird-freeform") == "Recommended: weird-freeform"  # catch-all
    assert rh._drag_outcome_label("") == "No recommendation recorded"


def test_build_dragcandidate_brief_is_pure_and_inlines_fields():
    row = {"n": 1, "surface": "memory", "evidence": "churny",
           "outcome": "probation", "what_must_survive": "the rule text",
           "risk_if_wrong": "lose the guard"}
    a = rh.build_dragcandidate_brief(row)
    assert a == rh.build_dragcandidate_brief(dict(row))
    assert "the rule text" in a and "lose the guard" in a and "churny" in a
    assert "Demotion candidate" in a


# --- severity bands ---
def test_gauge_band_thresholds():
    assert rh._gauge_band("duplicate_pair_count", 0) == ("CLEAN", "good")
    assert rh._gauge_band("duplicate_pair_count", 2) == ("SOME", "warn")
    assert rh._gauge_band("duplicate_pair_count", 9) == ("MANY", "bad")
    assert rh._gauge_band("phantom_ref_count", 0) == ("CLEAN", "good")
    assert rh._gauge_band("phantom_ref_count", 1) == ("BROKEN", "bad")
    assert rh._gauge_band("instruction_files_over_200", 0) == ("COMPLIANT", "good")


def test_gauge_band_unknown_key_is_neutral():
    assert rh._gauge_band("always_loaded_file_count", 42) == ("", "neutral")


# --- friction total (drives the AM-1 gauge) ---
def test_friction_total_counts_joined_records_plus_codex_runs():
    joined = {"n1": [{}, {}], "n2": [{}]}          # 3 joined records
    codex = {"runs": 4, "by_mode": {}, "by_verdict": {}, "max_revise_round": 0}
    assert rh.friction_total(joined, codex) == 7


def test_friction_total_empty_is_zero():
    assert rh.friction_total({}, {"runs": 0}) == 0


def test_friction_contributions_reconcile_to_total():
    # multi-attribution metrics (one record -> two joined keys), aggregate-only, codex, decisions
    joined = {"on_demand:skills/coding-team/phases/execution.md": [{"a": 1}, {"a": 2}],
              "always_loaded:rules/a.md": [{"d": 1}]}
    footer = [{"stream": "metrics", "records_aggregate_only": 4}]
    codex_aggregate = {"runs": 7}
    contribs = rh._friction_contributions(joined, footer, codex_aggregate)
    total = rh.friction_total(joined, codex_aggregate, rh._metrics_aggregate_only(footer))
    assert sum(n for _, n in contribs) == total          # provably reconciles
    # and the empty case doesn't crash / still reconciles to 0
    assert sum(n for _, n in rh._friction_contributions({}, [], {"runs": 0})) == 0


# --- instrument readout (A2/AM-1) ---
def test_instrument_readout_renders_exactly_the_gauge_specs(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "gauges"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'class="gauges"' in text
    # Assert the ACTUAL gauge set (GAUGE_SPECS = 5 headline keys + phantom + friction),
    # not all 8 HEADLINE_KEYS — the old "iterate HEADLINE_KEYS" check was vacuous because
    # 3 headline keys (orphans x2, unchecked_binary) are NOT gauges (they live in Hygiene).
    gauge_keys = [key for _, key, _ in rh.GAUGE_SPECS]
    assert set(gauge_keys) == {"always_loaded_words", "always_loaded_tokens_est",
                               "always_loaded_file_count", "instruction_files_over_200",
                               "duplicate_pair_count", "phantom_ref_count", "friction_total"}
    for key in gauge_keys:
        assert f'data-gauge="{key}"' in text          # each intended gauge renders
    # the three non-gauge headline keys must NOT appear as gauge cards
    for dropped in ("orphan_registration_count", "orphan_script_count", "unchecked_binary_count"):
        assert f'data-gauge="{dropped}"' not in text
    assert 'class="gauge gauge-' in text
    # the 5 headline-sourced gauge VALUES still render (regression of old headline display)
    for key in ("always_loaded_words", "always_loaded_tokens_est", "always_loaded_file_count",
                "instruction_files_over_200", "duplicate_pair_count"):
        assert str(doc["headline"][key]) in text


def test_friction_gauge_reflects_joined_records(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "fgauge"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "friction" in text.lower()
    assert 'data-gauge="friction_total"' in text


def test_gauge_drilldown_accordion_count_friction_and_contributors(tmp_path):
    import re

    doc = _minimal_doc()
    out_dir = tmp_path / "gdrill"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # every gauge is now an accordion button controlling a drawer panel
    assert 'class="gauge gauge-' in text                       # regression: class preserved
    assert 'aria-expanded="false"' in text
    assert 'aria-controls="gdrawer-duplicate_pair_count"' in text
    assert '<div class="gauge-drawer">' in text
    assert 'id="gdrawer-duplicate_pair_count"' in text and "hidden" in text
    # count gauge drill lists the real underlying items (the dup pair a<->b)
    dup_panel = re.search(r'id="gdrawer-duplicate_pair_count"[^>]*>(.*?)</div>', text, re.S).group(1)
    assert "gauge-drill" in dup_panel and "rules/a.md" in dup_panel
    # aggregate gauge uses the DISTINCT contributors class + label, never a fake full list
    words_panel = re.search(r'id="gdrawer-always_loaded_words"[^>]*>(.*?)</div>', text, re.S).group(1)
    assert "gauge-contributors" in words_panel
    assert "gauge-drill\"" not in words_panel  # no count-drill class on an aggregate gauge
    assert "top contributors" in words_panel.lower()
    # friction gauge drill is a per-stream breakdown, not a flat list
    fr_panel = re.search(r'id="gdrawer-friction_total"[^>]*>(.*?)</div>', text, re.S).group(1)
    assert "gauge-drill" in fr_panel
    # the accordion listener is wired in the single executable script
    assert "button.gauge[aria-controls]" in rh.STATIC_SCRIPT


def test_gauge_drilldown_words_gauge_shows_word_count_not_tokens(tmp_path):
    """QA finding 2 — the `always_loaded_words` gauge drill must show each contributor's
    WORD count, not its token estimate. `_minimal_doc` gives CLAUDE.md distinct
    words=40/tokens_est=100, so the two gauges must render different numbers for the
    SAME file."""
    import re

    doc = _minimal_doc()
    out_dir = tmp_path / "gdrill-words"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    words_panel = re.search(r'id="gdrawer-always_loaded_words"[^>]*>(.*?)</div>', text, re.S).group(1)
    claude_md_line = re.search(r'<code>CLAUDE\.md</code>[^<]*', words_panel).group(0)
    assert "40" in claude_md_line          # words, not the tokens_est=100
    assert "100" not in claude_md_line
    # symmetry: the tokens_est drill still shows the token estimate, unchanged
    tokens_panel = re.search(r'id="gdrawer-always_loaded_tokens_est"[^>]*>(.*?)</div>', text, re.S).group(1)
    claude_md_tokens_line = re.search(r'<code>CLAUDE\.md</code>[^<]*', tokens_panel).group(0)
    assert "100" in claude_md_tokens_line


# --- overview digest model ---
def test_build_overview_model_enumerates_gaps_weight_and_drag():
    civc = {"available": True, "cells": [
        {"verb": "Constrain", "surface": "memory", "verdict": "empty"},
        {"verb": "Afford", "surface": "context", "verdict": "covered"},
    ]}
    ctx = {"always": {"cells": [
        {"path": "CLAUDE.md", "size": 900, "node_key": "always_loaded:CLAUDE.md"},
        {"path": "rules/a.md", "size": 100, "node_key": "always_loaded:rules/a.md"},
    ]}}
    drag = {"available": True, "rows": [{"n": 1, "surface": "memory", "outcome": "keep"}]}
    headline = {"instruction_files_over_200": 2, "duplicate_pair_count": 1}
    m = rh.build_overview_model({"civc": civc, "context_weight": ctx, "drag": drag},
                                headline, phantom_ref_count=3, friction_total_value=5)
    assert ("Constrain", "memory") in m["roadmap_gaps"]
    assert ("Afford", "context") not in m["roadmap_gaps"]
    assert m["weight_tax"][0]["path"] == "CLAUDE.md"       # top by size
    assert m["hygiene"] == {"over_cap": 2, "dup_pairs": 1, "phantom_refs": 3}
    assert m["friction"]["count"] == 5
    assert m["drag_candidates"][0]["n"] == 1


# --- copy payloads (pure function) ---
def test_build_copy_payloads_are_pure_and_have_all_views():
    doc = _minimal_doc()
    # reuse the same models render_html builds; construct minimal ones inline
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered"}], "drag_candidates": []}
    models = {
        "context_weight": rh.build_contextweight_model(doc),
        "bipartite": rh.build_bipartite_model(doc),
        "trend": rh.build_trend_model([("2026-07-15", doc)]),
        "dupweb": rh.build_dupweb_model(doc),
        "civc": rh.build_civc_model(synth),
        "drag": rh.build_dragcandidate_model(synth),
    }
    friction = ({}, {}, [], {"runs": 0, "by_mode": {}, "by_verdict": {}, "max_revise_round": 0})
    p1 = rh.build_copy_payloads("2026-07-15", models, friction, doc)
    p2 = rh.build_copy_payloads("2026-07-15", models, friction, doc)
    assert p1 == p2                                          # deterministic
    assert set(p1) == {"overview", "weight", "friction", "hygiene"}
    # Task B-t2 tab merge: the coverage markdown table is folded into "overview"
    assert "| verb" in p1["overview"]                        # markdown table
    assert "|" in p1["overview"]


def test_squarify_geometry_fills_bounding_box():
    items = [{"size": 30, "id": "a"}, {"size": 20, "id": "b"}, {"size": 10, "id": "c"}]
    cells = rh.squarify(items, 0.0, 0.0, 100.0, 60.0)
    assert len(cells) == 3
    total_area = sum(float(c["w"]) * float(c["h"]) for c in cells)
    assert abs(total_area - 6000.0) < 1.0


def test_squarify_excludes_non_positive_sizes():
    items = [{"size": 10, "id": "a"}, {"size": 0, "id": "b"}, {"size": -5, "id": "c"}]
    cells = rh.squarify(items, 0.0, 0.0, 100.0, 60.0)
    assert len(cells) == 1


# ============================================================= friction join transforms
def test_join_decisions_path_ref_heats_exactly_one_node_no_basename_siblings():
    """(a) A single telemetry record naming ONE path heats exactly that node — a
    same-basename sibling elsewhere in the tree must NOT be heated (the subtree-smear
    bug §C1 fixes)."""
    node_index = {"a.md": ["always_loaded:rules/a.md", "on_demand:skills/x/rules/a.md"]}
    records = [{"date": "2026-07-01", "component": "rules/a.md"}]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15")
    assert heat == {"always_loaded:rules/a.md": 1}
    assert extra["segments_joined"] == 1
    assert extra["segments_ambiguous"] == 0
    assert extra["segments_unmatched"] == 0


def test_join_decisions_bare_name_ambiguous_heats_none():
    """(b) A bare-name ref matching >1 node heats NONE and increments
    segments_ambiguous — basename fan-out no longer heats every match."""
    node_index = {"a.md": ["always_loaded:rules/a.md", "on_demand:skills/x/rules/a.md"]}
    records = [{"date": "2026-07-01", "component": "a.md"}]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15")
    assert heat == {}
    assert joined == {}
    assert extra["segments_ambiguous"] == 1
    assert extra["segments_joined"] == 0


def test_join_decisions_path_ref_zero_matches_is_unmatched_not_ambiguous():
    """(c) A path-bearing ref resolving to zero rendered nodes counts as unmatched —
    neither ambiguous nor heated."""
    node_index = {"a.md": ["always_loaded:rules/a.md"]}
    records = [{"date": "2026-07-01", "component": "rules/nope.md"}]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15")
    assert heat == {}
    assert extra["segments_ambiguous"] == 0
    assert extra["segments_unmatched"] == 1


def test_join_decisions_harness_absolute_path_resolves_lexically_against_root():
    """A harness-absolute path (as the collector could emit) resolves against
    doc['root'] via a string prefix-strip — never filesystem resolve()/realpath."""
    node_index = {"a.md": ["always_loaded:rules/a.md"]}
    records = [{"date": "2026-07-01", "component": "/fake/root/rules/a.md"}]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15", root="/fake/root")
    assert heat == {"always_loaded:rules/a.md": 1}


def test_join_decisions_temporal_cutoff_excludes_future_records():
    node_index = {"a.md": ["always_loaded:rules/a.md"]}
    records = [{"date": "2026-08-01", "component": "rules/a.md"}]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15")
    assert heat == {}


def test_join_interventions_bare_name_ambiguous_heats_none():
    node_index = {"a.md": ["always_loaded:rules/a.md", "on_demand:skills/x/rules/a.md"]}
    records = [{"date": "2026-07-01", "memory_file": "a.md"}]
    heat, joined, extra = rh.join_interventions(records, node_index, "2026-07-15")
    assert heat == {}
    assert extra["segments_ambiguous"] == 1
    assert extra["segments_joined"] == 0


def test_join_interventions_path_ref_heats_exactly_one_node():
    node_index = {"a.md": ["always_loaded:rules/a.md", "on_demand:skills/x/rules/a.md"]}
    records = [{"date": "2026-07-01", "memory_file": "skills/x/rules/a.md"}]
    heat, joined, extra = rh.join_interventions(records, node_index, "2026-07-15")
    assert heat == {"on_demand:skills/x/rules/a.md": 1}
    assert extra["segments_joined"] == 1


def test_join_metrics_recovery_join_phases_and_agents():
    node_index = {
        "execution.md": ["on_demand:skills/coding-team/phases/execution.md"],
        "ct-implementer.md": ["on_demand:skills/coding-team/agents/ct-implementer.md"],
    }
    records = [{"date": "2026-07-01", "phases_used": ["execute"],
                "agents_dispatched": {"builder": 2}, "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, node_index, "2026-07-15")
    assert heat == {"on_demand:skills/coding-team/phases/execution.md": 1,
                     "on_demand:skills/coding-team/agents/ct-implementer.md": 1}
    assert extra["records_eligible"] == 1
    assert extra["records_aggregate_only"] == 0


def test_join_metrics_no_blanket_coding_team_heat():
    """(§C1 change 1) The blanket 'coding-team' base-node heat is GONE — an eligible
    record with no resolvable phase/agent alias must NOT reattach to the skill node."""
    node_index = {"coding-team": ["on_demand:coding-team"]}
    records = [{"date": "2026-07-01", "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, node_index, "2026-07-15")
    assert heat == {}
    assert joined == {}


def test_join_metrics_unattributed_record_counts_aggregate_only_and_feeds_friction_total():
    """(d) A metrics-eligible record with no phase/agent alias increments
    records_aggregate_only AND that count is folded into friction_total, so the
    eligible-but-unattributed signal isn't silently dropped."""
    records = [{"date": "2026-07-01", "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, {}, "2026-07-15")
    assert extra["records_aggregate_only"] == 1
    assert rh.friction_total({}, {"runs": 0}, extra["records_aggregate_only"]) == 1


def test_join_metrics_alias_resolves_exact_node_not_basename_sibling():
    """(e) The phase alias resolves to the exact skills/coding-team/phases/<file> node,
    never a same-basename sibling elsewhere in the tree."""
    node_index = {
        "execution.md": ["on_demand:skills/coding-team/phases/execution.md",
                          "on_demand:skills/other-skill/phases/execution.md"],
    }
    records = [{"date": "2026-07-01", "phases_used": ["execute"], "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, node_index, "2026-07-15")
    assert heat == {"on_demand:skills/coding-team/phases/execution.md": 1}
    assert extra["records_aggregate_only"] == 0


def test_join_metrics_clean_run_not_eligible():
    node_index = {"coding-team": ["on_demand:coding-team"]}
    records = [{"date": "2026-07-01", "rework_iterations": 0, "audit_rounds": 1, "findings_total": 0}]
    heat, joined, extra = rh.join_metrics(records, node_index, "2026-07-15")
    assert heat == {}
    assert extra["records_eligible"] == 0


# ================================================== C3: composed overlay no-smear proofs
def test_build_friction_overlay_single_path_event_heats_exactly_one_node(tmp_path):
    """Integration proof (through the COMPOSED `build_friction_overlay` entry point, not
    just the unit-level `join_decisions`) that a single path-bearing decisions event heats
    exactly the node it references and never a same-basename sibling elsewhere in the tree
    — the subtree-smear bug §C1 fixed at the join level must also hold end to end."""
    node_index = {"a.md": ["always_loaded:rules/a.md", "on_demand:skills/x/rules/a.md"]}
    decisions_file = tmp_path / "decisions.jsonl"
    decisions_file.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")

    heat, joined, footer, codex = rh.build_friction_overlay(
        {"root": ""}, {"decisions": decisions_file}, node_index, "2026-07-15", set())

    assert heat == {"always_loaded:rules/a.md": 1}
    assert "on_demand:skills/x/rules/a.md" not in heat
    assert set(joined) == {"always_loaded:rules/a.md"}


def test_build_friction_overlay_metrics_stream_no_subtree_smear(tmp_path):
    """Integration proof that multiple eligible metrics records naming DIFFERENT
    phase/agent components each heat only their own node, an unattributed eligible
    record does not reattach to the coding-team base node, and no single node absorbs
    the whole set — the 21-node coding-team-subtree flood regression this overlay was
    built to prevent."""
    node_index = {
        "execution.md": ["on_demand:skills/coding-team/phases/execution.md"],
        "ct-implementer.md": ["on_demand:skills/coding-team/agents/ct-implementer.md"],
        "coding-team": ["on_demand:coding-team"],
    }
    metrics_file = tmp_path / "metrics.jsonl"
    metrics_file.write_text(
        json.dumps({"date": "2026-07-01", "phases_used": ["execute"], "rework_iterations": 1}) + "\n"
        + json.dumps({"date": "2026-07-02", "agents_dispatched": {"builder": 2},
                       "rework_iterations": 1}) + "\n"
        + json.dumps({"date": "2026-07-03", "rework_iterations": 1}) + "\n"
    )

    heat, joined, footer, codex = rh.build_friction_overlay(
        {"root": ""}, {"metrics": metrics_file}, node_index, "2026-07-15", set())

    assert heat["on_demand:skills/coding-team/phases/execution.md"] == 1
    assert heat["on_demand:skills/coding-team/agents/ct-implementer.md"] == 1
    assert "on_demand:coding-team" not in heat
    assert len(heat) == 2
    assert max(heat.values()) == 1


def test_aggregate_codex_by_mode_and_verdict():
    records = [{"mode": "plan", "verdict": "REVISE", "ts": "2026-07-01T00:00:00Z", "round": 2},
               {"mode": "plan", "verdict": "SHIP", "ts": "2026-07-02T00:00:00Z"}]
    agg = rh.aggregate_codex(records, "2026-07-15")
    assert agg["runs"] == 2
    assert agg["by_mode"] == {"plan": 2}
    assert agg["by_verdict"] == {"REVISE": 1, "SHIP": 1}
    assert agg["max_revise_round"] == 2


def test_normalize_ref_token():
    assert rh._normalize_ref_token("hooks/write-guard.py:check_phase5") == "hooks/write-guard.py"
    assert rh._normalize_ref_token("write-guard.py --flag") == "write-guard.py"
    assert rh._normalize_ref_token("coding-team") == "coding-team"


# ============================================================= 3. escaping / security
XSS_PAYLOADS = ['</script><img onerror=alert(1)>', '"><script>alert(1)</script>', "</style>${7*7}"]


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_shared_sample_injection_is_escaped(tmp_path, payload):
    doc = _minimal_doc()
    doc["duplication"]["pairs"][0]["shared_sample"] = payload
    out_dir = tmp_path / "xss1"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert payload not in text
    assert "&lt;" in text or "&quot;" in text or "&amp;" in text
    p = _ExternalRefParser()
    p.feed(text)
    assert p.tag_counts.get("style", 0) == 1
    # A8: JSON data islands are inert `<script type="application/json">` tags — only
    # the ONE executable script (no type attr) counts toward the CSP script-hash budget.
    import re
    exe_scripts = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe_scripts) == 1


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_hook_command_injection_is_escaped(tmp_path, payload):
    doc = _minimal_doc()
    doc["enforcement"]["hooks"]["registered"][0]["command"] = payload
    out_dir = tmp_path / "xss2"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert payload not in text
    p = _ExternalRefParser()
    p.feed(text)
    assert p.tag_counts.get("style", 0) == 1
    import re
    exe_scripts = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe_scripts) == 1


def test_scripts_on_disk_shows_description_and_empty_fallback(tmp_path):
    doc = _minimal_doc()
    doc["enforcement"]["hooks"]["scripts_on_disk"][0]["description"] = "guards writes"
    doc["enforcement"]["hooks"]["scripts_on_disk"][1]["description"] = ""
    out_dir = tmp_path / "scriptdesc"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "guards writes" in text
    assert "no description" in text


def test_script_description_hostile_html_renders_escaped(tmp_path):
    """B3(6): a description containing HTML must render ESCAPED — proves render-time
    escaping end-to-end (the collector-only injection-string test does not)."""
    doc = _minimal_doc()
    doc["enforcement"]["hooks"]["scripts_on_disk"][0]["description"] = '<script>alert(1)</script>'
    out_dir = tmp_path / "hostile"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in text        # never emitted raw
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text  # emitted escaped


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_promotion_excerpt_injection_is_escaped(tmp_path, payload):
    doc = _minimal_doc(extra_promotion=payload)
    out_dir = tmp_path / "xss3"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # promotion_candidates are not directly rendered by any current tab body — but the
    # excerpt lives in phantom_refs' sibling data; this asserts render doesn't choke on it
    # and no unescaped payload leaks anywhere in the byte stream.
    assert payload not in text


def test_env_values_never_read_and_env_keys_render(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "envtest"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    env = dict(os.environ)
    env["FAKE_SECRET_ENV"] = "s3cr3t-should-never-appear-anywhere"
    proc = subprocess.run([sys.executable, str(RENDER), "--out-dir", str(out_dir),
                            "--date", "2026-07-15", "--no-friction"],
                           capture_output=True, text=True, timeout=30, env=env)
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "s3cr3t-should-never-appear-anywhere" not in text


def test_no_on_handlers_and_no_style_attributes(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "csp_attrs"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    p = _ExternalRefParser()
    p.feed(text)
    assert p.on_handlers == []
    assert p.style_attrs == []


def test_ladder_bars_get_cursor_pointer_affordance():
    """QA finding 3 — the click-to-act handler binds to `svg [data-node-key]`, which
    includes `.ladder-bar` (see `bar_cls` in render_html.py), but only `.cell-rect` got
    `cursor:pointer`. A ladder bar is clickable but must also LOOK clickable."""
    import re
    assert re.search(r"svg \.cell-rect[^{]*,\s*svg \.ladder-bar[^{]*\{[^}]*cursor:pointer[^}]*\}",
                      rh.STATIC_STYLE) is not None


def test_csp_hashes_match_recomputed_static_blocks(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "csp_hash"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    m = re.search(r"style-src 'sha256-([^']+)'; script-src 'sha256-([^']+)'", text)
    assert m is not None
    assert m.group(1) == rh._csp_hash(rh.STATIC_STYLE)
    assert m.group(2) == rh._csp_hash(rh.STATIC_SCRIPT)


def test_csp_has_connect_src_self_not_none(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "csp_connect"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "connect-src 'self'" in text
    assert "connect-src 'none'" not in text


def test_eventsource_listener_feature_detected_and_guarded():
    # progressive enhancement: gated on window.EventSource, wrapped so file:// is a no-op
    assert "EventSource" in rh.STATIC_SCRIPT
    assert "/events" in rh.STATIC_SCRIPT
    # no inline handler / style injection introduced (CSP model preserved)
    assert "onerror=" not in rh.STATIC_SCRIPT  # uses addEventListener('error', ...)


def test_eventsource_gated_on_http_origin():
    # FIX 3 (Codex P2): a file:// static artifact must NOT open EventSource — on file://
    # construction succeeds, the request fails, and EventSource AUTO-RECONNECTS on error,
    # so every one-shot report opened in a browser hammers file:///events. The fix gates
    # construction on an http(s) origin. Browser runtime behavior isn't unit-testable in
    # pytest, so asserting the protocol gate guards `new EventSource` is the right proxy.
    script = rh.STATIC_SCRIPT
    assert "location.protocol" in script
    assert "'http:'" in script and "'https:'" in script
    idx_gate = script.index("location.protocol")
    idx_es = script.index("new EventSource")
    assert idx_gate < idx_es, "the protocol gate must precede EventSource construction"


def test_generation_meta_and_sync_reload_guard(tmp_path):
    # FIX 4 (Codex P2): the page carries the build generation it was rendered from (a
    # <meta>, CSP-safe — no inline script), and the client reloads only when the server
    # reports a HIGHER generation on (re)connect. Equal generations never reload (no loop
    # on a fresh page); a higher server generation after a missed-during-disconnect refresh
    # triggers the catch-up reload.
    assert 'meta[name="hm-generation"]' in rh.STATIC_SCRIPT
    assert "serverGen > pageGen" in rh.STATIC_SCRIPT
    doc = _minimal_doc()
    out_dir = tmp_path / "gen"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    ctx = rh.render_from_out_dir(out_dir, date="2026-07-15", no_friction=True, generation=7)
    assert '<meta name="hm-generation" content="7">' in ctx.html_text
    # one-shot report path (no generation) emits NO generation meta -> stays deterministic
    # (the static script still references the meta by name in its querySelector, so assert on
    # the emitted <meta> TAG, not the bare substring).
    ctx0 = rh.render_from_out_dir(out_dir, date="2026-07-15", no_friction=True)
    assert '<meta name="hm-generation"' not in ctx0.html_text


def test_eventsource_gated_on_generation_marker(tmp_path):
    # FIX 1 (Codex r2): the http(s) protocol check ALONE is insufficient. A STATIC one-shot
    # artifact served over plain HTTP (e.g. `python -m http.server`) has NO hm-generation meta
    # yet, under a protocol-only gate, would still construct EventSource('/events') — whose
    # missing endpoint AUTO-RECONNECTS indefinitely (request churn) and could even reload off an
    # unrelated `/events` on that host. The fix gates construction on the serve-ONLY marker: the
    # `<meta name="hm-generation">` that only serve.py's live render emits (a one-shot/file://
    # render passes generation=None -> no meta). Browser runtime isn't unit-testable in pytest,
    # so assert (a) STATIC_SCRIPT keys construction off the marker's PRESENCE (genMeta) + an
    # integer pageGen, reading the marker BEFORE `new EventSource`; and (b) the meta-emission
    # side (the serve-only signal itself) via render_from_out_dir: a render WITH generation
    # emits the marker, one WITHOUT emits none — so a static artifact's gate stays closed.
    script = rh.STATIC_SCRIPT
    assert 'meta[name="hm-generation"]' in script
    idx_marker = script.index('meta[name="hm-generation"]')
    idx_es = script.index("new EventSource")
    assert idx_marker < idx_es, "the hm-generation marker must be read before EventSource construction"
    # the authoritative serve-mode gate: construction is guarded on the marker's PRESENCE
    # (genMeta) AND an integer pageGen (a non-integer content parses to NaN -> gate stays shut)
    assert "genMeta &&" in script, "EventSource construction must be gated on the hm-generation marker presence"
    assert "isNaN(pageGen)" in script, "the gate must require pageGen to parse as an integer"
    # (b) meta-emission side, testable via render_from_out_dir(generation=...)
    doc = _minimal_doc()
    out_dir = tmp_path / "marker_gate"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    ctx_live = rh.render_from_out_dir(out_dir, date="2026-07-15", no_friction=True, generation=3)
    assert '<meta name="hm-generation" content="3">' in ctx_live.html_text
    ctx_static = rh.render_from_out_dir(out_dir, date="2026-07-15", no_friction=True)
    assert '<meta name="hm-generation"' not in ctx_static.html_text, \
        "a one-shot/static render (generation=None) must emit NO marker -> its EventSource gate stays closed"


# ============================================================= 4. determinism
def test_self_run_twice_byte_identical(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "det1"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc1 = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc1.returncode == 0, proc1.stderr
    first_bytes = (out_dir / "harness-map-2026-07-15.html").read_bytes()
    proc2 = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc2.returncode == 0, proc2.stderr
    second_bytes = (out_dir / "harness-map-2026-07-15.html").read_bytes()
    assert first_bytes == second_bytes


def test_cross_pythonhashseed_byte_identical(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "det2"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    env1 = dict(os.environ)
    env1["PYTHONHASHSEED"] = "1"
    env2 = dict(os.environ)
    env2["PYTHONHASHSEED"] = "2"
    proc1 = subprocess.run([sys.executable, str(RENDER), "--out-dir", str(out_dir),
                             "--date", "2026-07-15", "--no-friction"],
                            capture_output=True, text=True, timeout=30, env=env1)
    assert proc1.returncode == 0, proc1.stderr
    bytes1 = (out_dir / "harness-map-2026-07-15.html").read_bytes()
    proc2 = subprocess.run([sys.executable, str(RENDER), "--out-dir", str(out_dir),
                             "--date", "2026-07-15", "--no-friction"],
                            capture_output=True, text=True, timeout=30, env=env2)
    assert proc2.returncode == 0, proc2.stderr
    bytes2 = (out_dir / "harness-map-2026-07-15.html").read_bytes()
    assert bytes1 == bytes2


def test_home_defaults_resolved_at_call_time(tmp_path):
    """§9-R D: defaults resolve through $HOME at call time, never frozen at import."""
    fake_home_2 = tmp_path / "another_home"
    fake_home_2.mkdir()
    (fake_home_2 / ".claude").mkdir()
    (fake_home_2 / ".claude" / "harness-decisions.jsonl").write_text(
        json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    doc = _minimal_doc()
    out_dir = tmp_path / "callres"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    original_home = os.environ.get("HOME")
    os.environ["HOME"] = str(fake_home_2)
    try:
        rc = rh.main(["--out-dir", str(out_dir), "--date", "2026-07-15"])
    finally:
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home
    assert rc == 0
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "decisions: loaded" in text


def test_render_from_out_dir_matches_cli_bytes(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "helper"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    # in-memory helper (no-friction so no $HOME telemetry dependence)
    streams = {"decisions": None, "metrics": None, "interventions": None, "codex": None}
    ctx = rh.render_from_out_dir(
        out_dir, date="2026-07-15", streams=streams, no_friction=True)
    assert ctx.date == "2026-07-15"
    assert ctx.doc["root"] == doc["root"]
    assert ctx.html_bytes == ctx.html_text.encode("utf-8", "backslashreplace")
    assert ctx.models and ctx.node_index is not None   # shared state carried for the cheap path
    # CLI path must produce the identical bytes
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    on_disk = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert ctx.html_text == on_disk


def test_render_from_out_dir_raises_on_missing_sidecar(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(rh.RenderError):
        rh.render_from_out_dir(empty, date=None, streams=None, no_friction=True)


def test_default_streams_keys_and_paths():
    # §9-R D: resolved through the (fake, session-scoped) $HOME at CALL time, matching
    # main()'s own default-path construction exactly (serve.py's _build_streams delegates
    # to this same helper, so the two can never drift).
    streams = rh.default_streams()
    assert set(streams) == {"decisions", "metrics", "codex", "interventions"}
    home = Path(os.environ["HOME"])
    assert streams["decisions"] == home / ".claude" / "harness-decisions.jsonl"
    assert streams["metrics"] == home / ".claude" / "harness-metrics.jsonl"
    assert streams["codex"] == home / ".claude" / "harness-codex.jsonl"
    assert streams["interventions"] is None


# ============================================================= 5. degradation / edge matrix
def test_missing_out_dir_is_fatal(tmp_path):
    proc = run_render(tmp_path / "does-not-exist", "--no-friction")
    assert proc.returncode != 0


def test_date_no_match_is_fatal(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "nomatch"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-01-01", "--no-friction")
    assert proc.returncode != 0


def test_synthesis_absent_renders_graceful_empty_state(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "nosynth"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Coverage Matrix unavailable" in text
    assert "drag-candidate table unavailable" in text


def test_synthesis_absent_empty_states_are_loud_and_actionable(tmp_path):
    """A3: the quiet 'synthesis sidecar not found' text must be replaced with a
    message that names the exact missing file and the fix — both the Coverage
    Matrix empty-state (mini-grid + inspector body) and the drag-candidate table
    empty-state, when no `harness-synthesis-<date>.json` sidecar is present."""
    doc = _minimal_doc()
    out_dir = tmp_path / "nosynth_loud"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # Coverage Matrix empty-state (mini-grid on Overview + inspector body on Coverage)
    assert text.count("harness-synthesis-2026-07-15.json") >= 3  # mini-grid + civc_body + drag_body
    assert "re-run" in text.lower() and "step b" in text.lower()
    assert "synthesis sidecar not found" not in text  # old quiet phrasing is gone
    # drag-candidate table empty-state carries the same actionable pointer
    assert "drag-candidate table unavailable" in text


def test_synthesis_present_renders_populated_matrix_and_drag_table(tmp_path):
    """Sibling of the loud-empty-state test above: when a valid synthesis sidecar
    IS present, neither empty-state message renders — the real Coverage Matrix
    (36 matrix-cells) and drag-candidate table render instead. Fixture is the A2
    synthesis-template.json skeleton (36 civc cells + 1 drag candidate)."""
    template_path = Path(__file__).resolve().parents[1] / "synthesis-template.json"
    synth = json.loads(template_path.read_text(encoding="utf-8"))
    synth.setdefault("schema_version", 1)  # template is a fill-in-the-blank skeleton;
    # load_sidecar requires schema_version (§6 structural TYPE validation) to accept it.
    doc = _minimal_doc()
    out_dir = tmp_path / "synth_present"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # populated Coverage Matrix: 36 matrix-cells, no empty-state pointer text
    assert text.count('class="cell matrix-cell') == 36
    assert "Coverage Matrix unavailable" not in text
    # populated drag-candidate table: the one template row renders, not the empty-state
    assert "drag-candidate table unavailable" not in text
    assert '<td>1</td><td>context</td>' in text


def test_corrupt_sidecar_excluded_from_trend_and_listed_in_skipped(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "corrupt"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", doc)
    (out_dir / "harness-map-2026-07-13.json").write_text("{not valid json")
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "2026-07-13" in text  # listed in the provenance footer's skipped-sidecars section


def test_corrupt_sidecar_at_explicit_date_is_fatal(tmp_path):
    out_dir = tmp_path / "corruptexplicit"
    out_dir.mkdir()
    (out_dir / "harness-map-2026-07-15.json").write_text("{not valid json")
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode != 0


def test_empty_arrays_render_empty_state_not_crash(tmp_path):
    doc = _minimal_doc()
    doc["duplication"]["pairs"] = []
    doc["phantom_refs"] = []
    out_dir = tmp_path / "empties"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "no duplicate pairs above threshold" in text
    assert "no phantom refs" in text


def test_friction_stream_absent_footer_note(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "absentstream"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "interventions: absent" in text


def test_friction_panel_renders_english_sentences_not_raw_json(tmp_path):
    """Demo-readability follow-up: the per-stream row must lead with a human sentence,
    with the raw counter dict demoted to a collapsed <details> secondary."""
    doc = _minimal_doc()
    out_dir = tmp_path / "friction_english"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions_file = out_dir / "decisions.jsonl"
    decisions_file.write_text(
        json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n"
        + json.dumps({"date": "2026-07-02", "component": "no-match-here.md"}) + "\n"
    )
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "component references matched to map components" in text
    assert '<details class="friction-row-detail"><summary>raw counters</summary>' in text
    assert "a data join, not a judgment" in text


def test_friction_absent_stream_renders_plain_sentence(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "friction_absent"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Interventions — stream not provided." in text


def test_metrics_stream_renders_attribution_sentence(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "friction_metrics"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    metrics_file = out_dir / "metrics.jsonl"
    metrics_file.write_text(
        json.dumps({"date": "2026-07-01", "rework_iterations": 1, "phases_used": ["execute"],
                     "agents_dispatched": {"builder": 1}}) + "\n"
    )
    proc = run_render(out_dir, "--date", "2026-07-15", "--metrics-file", str(metrics_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "eligible pipeline records attributed to phase/agent components" in text


def test_codex_aggregate_renders_english_sentence(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "friction_codex"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    codex_file = out_dir / "codex.jsonl"
    codex_file.write_text(
        json.dumps({"ts": "2026-07-01T00:00:00Z", "mode": "plan", "verdict": "APPROVED"}) + "\n"
        + json.dumps({"ts": "2026-07-02T00:00:00Z", "mode": "diff", "verdict": "REVISE", "round": 2}) + "\n"
    )
    proc = run_render(out_dir, "--date", "2026-07-15", "--codex-file", str(codex_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "2 Codex reviews" in text
    assert "on plans" in text
    assert "on diffs" in text
    assert "approved" in text
    assert "needed revision" in text
    assert "revise round" in text
    assert '"runs":' not in text


def test_friction_overlay_legend_and_heat_classes_render(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "friction_heat"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions_file = out_dir / "decisions.jsonl"
    decisions_file.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="friction-legend"' in text
    assert "most-active" in text
    # C2: a LONE heated node is the only distinct heat value present, so the rank/quantile
    # bucketing (which replaced the old raw-count `min(heat_n, 4)` threshold) resolves it
    # to the top bucket (rank/k == 1.0) rather than the old count-literal fh1.
    assert 'class="cell-rect heatable fh4"' in text
    assert 'class="friction-badge"' in text


# ============================================================= 5b. C2 rank/quantile heat buckets
def test_heat_bucket_map_empty_heat_no_crash():
    """C2 determinism guard: zero heated nodes must not call max()/min()/a quantile on
    an empty sequence — `_heat_bucket_map({})` returns an empty map instead."""
    assert rh._heat_bucket_map({}) == {}


def test_heat_bucket_map_single_distinct_value_defined_bucket():
    """C2 determinism guard: one distinct heat value (however many nodes share it) must
    not divide-by-zero or crash. Defined behavior: the sole value has nothing to rank
    against, so rank/k == 1.0 resolves it to the top bucket."""
    heat = {"always_loaded:a.md": 3, "always_loaded:b.md": 3}
    assert rh._heat_bucket_map(heat) == {3: 4}


def test_heat_bucket_map_fh4_is_minority_on_realistic_distribution():
    """C2: the top bucket (fh4) must be a genuine minority slice of the heated set on a
    realistic multi-heat distribution — a raw-count threshold (pre-C2: any heat>=4 was
    fh4) would flood it instead."""
    heat = {}
    for i in range(6):
        heat[f"always_loaded:low{i}.md"] = 1
    for i in range(3):
        heat[f"always_loaded:mid{i}.md"] = 2
    for i in range(2):
        heat[f"always_loaded:high{i}.md"] = 3
    heat["always_loaded:hottest.md"] = 10
    bucket_map = rh._heat_bucket_map(heat)
    buckets = [bucket_map[v] for v in heat.values()]
    fh4_count = buckets.count(4)
    assert fh4_count == 1
    assert fh4_count < len(buckets) / 2
    assert bucket_map[10] == 4      # the single highest value is the top bucket
    assert bucket_map[1] == 1       # the lowest value is the lightest bucket
    assert bucket_map[1] < bucket_map[2] < bucket_map[3] < bucket_map[10]   # rank order preserved


def test_heat_bucket_ties_render_identical_fh_class():
    """C2: two different node keys with the identical heat VALUE must render the
    identical fhN class — bucket is a pure function of value, never of node identity
    or dict/set iteration order."""
    tree = {"cells": [
        {"path": "a.md", "node_key": "always_loaded:a.md", "size": 10,
         "x": "0.00", "y": "0.00", "w": "80.00", "h": "40.00", "fill": "#000"},
        {"path": "b.md", "node_key": "always_loaded:b.md", "size": 10,
         "x": "0.00", "y": "40.00", "w": "80.00", "h": "40.00", "fill": "#000"},
    ], "canvas_w": 100.0, "canvas_h": 100.0}
    heat = {"always_loaded:a.md": 4, "always_loaded:b.md": 4, "always_loaded:c.md": 1}
    svg = rh._render_treemap_svg(tree, heat, "t")
    import re
    classes = re.findall(r'class="cell-rect heatable (fh\d)"', svg)
    assert len(classes) == 2
    assert classes[0] == classes[1]


def test_render_treemap_and_ladder_share_same_bucket_for_same_heat_value():
    """C2: the treemap and the ladder must apply the SAME value->bucket mapping — not
    two independently-computed bucketings that could diverge."""
    tree = {"cells": [{"path": "a.md", "node_key": "always_loaded:a.md", "size": 10,
                        "x": "0.00", "y": "0.00", "w": "80.00", "h": "40.00", "fill": "#000"}],
            "canvas_w": 100.0, "canvas_h": 100.0}
    heat = {"always_loaded:a.md": 2, "always_loaded:b.md": 9}
    expected_bucket = rh._heat_bucket_map(heat)[2]
    treemap_svg = rh._render_treemap_svg(tree, heat, "t")
    ladder_svg = rh._render_ladder_svg(tree, heat, "l")
    assert f'class="cell-rect heatable fh{expected_bucket}"' in treemap_svg
    assert f'class="ladder-bar heatable fh{expected_bucket}"' in ladder_svg


def test_weight_view_bucket_map_shared_across_always_and_ondemand_panels(tmp_path):
    """C2: `_render_weight_view` computes ONE bucket_map from the full heat dict and
    passes it to all four render sites (treemap/ladder x always/on-demand) — a node in
    the always-loaded panel and a node in the on-demand panel with the SAME heat value
    must get the SAME fhN class, proving one shared mapping rather than each panel
    independently re-deriving (and potentially diverging on) its own thresholds."""
    doc = _minimal_doc()
    out_dir = tmp_path / "shared_bucket"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    lines = (
        [json.dumps({"date": "2026-07-01", "component": "rules/a.md"})] * 2      # always-loaded, heat=2
        + [json.dumps({"date": "2026-07-01", "component": "coding-team"})] * 2   # on-demand, same heat=2
        + [json.dumps({"date": "2026-07-01", "component": "CLAUDE.md"})] * 9     # forces heat=2 out of the top bucket
    )
    decisions.write_text("\n".join(lines) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    always_bucket = re.search(
        r'class="cell-rect heatable (fh\d)" data-node-key="always_loaded:rules/a\.md"', text)
    ondemand_bucket = re.search(
        r'class="cell-rect heatable (fh\d)" data-node-key="on_demand:coding-team"', text)
    assert always_bucket and ondemand_bucket
    assert always_bucket.group(1) == ondemand_bucket.group(1)
    assert always_bucket.group(1) != "fh4"      # the two heat=2 nodes must not be in the top bucket


def test_friction_overlay_css_dims_unheated_cells_and_marks_toggle_pressed(tmp_path):
    """The friction toggle must have an UNMISTAKABLE visual effect (demo-blocker
    fix): unheated cells dim while the overlay is on, heated cells get a
    GRADUATED intensity ramp (fh1 "light" strictly < fh2 < fh3 < fh4
    "most-active", each visually distinct — B-t3 restyle: the old rule flattened
    every heated tier to opacity:1, so "some" friction looked identical to
    "most-active"), and the toggle button itself gets a distinct pressed look —
    not just the generic aria-pressed border-color rule."""
    doc = _minimal_doc()
    out_dir = tmp_path / "friction_visibility"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "body.friction-on .heatable:not(.fh1):not(.fh2):not(.fh3):not(.fh4){opacity:0.25}" in text
    import re

    def _fh_rule_value(n, prop):
        m = re.search(rf'body\.friction-on \.fh{n}\{{[^}}]*{prop}:([\d.]+)[;}}]', text)
        assert m is not None, f"fh{n} rule missing a {prop} declaration"
        return float(m.group(1))

    opacities = [_fh_rule_value(n, "opacity") for n in (1, 2, 3, 4)]
    assert opacities == sorted(opacities) and len(set(opacities)) == 4, (
        f"friction heat opacity must be strictly graduated fh1<fh2<fh3<fh4, got {opacities}")
    assert opacities[-1] == 1.0   # most-active bucket (fh4) stays at full opacity
    stroke_widths = [_fh_rule_value(n, "stroke-width") for n in (1, 2, 3, 4)]
    assert stroke_widths == sorted(stroke_widths) and len(set(stroke_widths)) == 4
    assert "stroke-width:4" in text
    assert '#friction-toggle[aria-pressed="true"]{background:var(--crit)' in text


def test_friction_view_has_four_stream_cards(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "fcards"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert text.count('class="stream-card"') == 4          # one card per telemetry stream
    for title in ("Decisions", "Review metrics", "Interventions", "Codex reviews"):
        assert title in text                                # STREAM_LABELS titles
    assert "d.jsonl" in text                                # loaded stream's source filename
    assert "What matched" in text                           # existing join table still below


def test_friction_view_total_matches_gauge(tmp_path):
    """DECISION 6 + finding #3: parse the ACTUAL gauge value AND the Friction-view header
    total and assert they are the SAME, non-zero number. Asserting only that the
    `data-gauge` attribute exists is a false-green — a gauge rendering 0 would pass."""
    doc = _minimal_doc()
    out_dir = tmp_path / "ftotal"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    # two decisions on DISTINCT always-loaded components -> >=2 joined records (unambiguous, >1)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(
        json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n"
        + json.dumps({"date": "2026-07-02", "component": "CLAUDE.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    # gauge value = the .v inside the friction_total gauge card (order fixed by _render_gauge)
    gm = re.search(r'data-gauge="friction_total"[^>]*>\s*<div class="v">(\d+)</div>', text)
    assert gm is not None, "friction_total gauge card missing its .v value"
    gauge_val = int(gm.group(1))
    hm = re.search(r'Friction events:\s*(\d+)', text)
    assert hm is not None
    header_val = int(hm.group(1))
    # the DECISION 6 invariant: gauge and view header render ONE identical value ...
    assert gauge_val == header_val
    # ... and it is provably non-zero (both injected decisions join) — kills the 0 false-green
    assert gauge_val >= 2


def test_friction_view_has_per_component_join_table(tmp_path):
    """A6 + finding #1: the per-COMPONENT join table (which node keys got heated, and how
    many friction records each) must render — not only the per-stream 'What matched' table.
    round2 #7: inject two components in REVERSE lexical order and assert the rendered rows
    come out sorted (proves sorted(joined.items()), not insertion order).
    NOTE (grounding deviation from the plan literal): the plan's fixture text referenced
    "rules/zeta.md"/"rules/alpha.md" without registering them as always-loaded files, so
    they would never resolve through `node_index` (join_decisions matches on basenames
    already present in `build_node_index(models)`, §1.3). Registering both as
    `extra_files` here so they actually join, per the "adapt fixtures to real shapes"
    grounding instruction."""
    doc = _minimal_doc(extra_files=[
        {"path": "rules/zeta.md", "category": "rule", "words": 10, "lines": 2,
         "tokens_est": 10, "evidence": "VERIFIED"},
        {"path": "rules/alpha.md", "category": "rule", "words": 10, "lines": 2,
         "tokens_est": 10, "evidence": "VERIFIED"},
    ])
    out_dir = tmp_path / "fcomp"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    # z-component FIRST, a-component SECOND (twice) -> node keys join for both; sorted output
    # must place the a-key row before the z-key row regardless of insertion order.
    decisions.write_text(
        json.dumps({"date": "2026-07-01", "component": "rules/zeta.md"}) + "\n"
        + json.dumps({"date": "2026-07-02", "component": "rules/alpha.md"}) + "\n"
        + json.dumps({"date": "2026-07-03", "component": "rules/alpha.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'class="friction-components sortable"' in text  # the per-component table
    import re
    rows = re.findall(r'<tr[^>]*class="friction-component-row"[^>]*>(.*?)</tr>', text, re.S)
    assert len(rows) >= 2
    # sorted order: alpha row precedes zeta row
    a_idx = next(i for i, r in enumerate(rows) if "alpha.md" in r)
    z_idx = next(i for i, r in enumerate(rows) if "zeta.md" in r)
    assert a_idx < z_idx
    # alpha joined twice -> count 2; zeta once -> count 1
    assert ">2<" in rows[a_idx] and ">1<" in rows[z_idx]


def test_component_friction_table_is_sortable_with_deterministic_initial_order(tmp_path):
    """NOTE (grounding deviation from the plan literal, same as
    test_friction_view_has_per_component_join_table above): the plan's fixture text
    referenced "rules/zeta.md"/"rules/alpha.md" without registering them as
    always-loaded files, so they would never resolve through `node_index`
    (join_decisions matches on basenames already present in `build_node_index(models)`).
    Registering both as `extra_files` here so they actually join."""
    import re

    doc = _minimal_doc(extra_files=[
        {"path": "rules/zeta.md", "category": "rule", "words": 10, "lines": 2,
         "tokens_est": 10, "evidence": "VERIFIED"},
        {"path": "rules/alpha.md", "category": "rule", "words": 10, "lines": 2,
         "tokens_est": 10, "evidence": "VERIFIED"},
    ])
    out_dir = tmp_path / "sortable"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(
        json.dumps({"date": "2026-07-01", "component": "rules/zeta.md"}) + "\n"
        + json.dumps({"date": "2026-07-02", "component": "rules/alpha.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # sortable scaffolding present
    assert 'class="friction-components sortable"' in text
    assert "<thead>" in text and "<tbody>" in text
    assert text.count('aria-sort="none"') >= 2
    assert 'data-sort-col="0" data-sort-type="text"' in text
    assert 'data-sort-col="1" data-sort-type="num"' in text
    # server order is still the deterministic initial state (alpha before zeta)
    body = re.search(r"<tbody>(.*?)</tbody>", text, re.S).group(1)
    assert body.index("alpha.md") < body.index("zeta.md")
    # rows carry data-node-key for the treemap click-to-jump (Task 6) and keep the class
    assert 'class="friction-component-row" data-node-key="' in text
    # the generic sort listener is wired in the single executable script, and updates
    # the ▲/▼/↕ indicator (design promised a directional glyph, not a static ↕)
    assert "table.sortable" in rh.STATIC_SCRIPT
    assert "aria-sort" in rh.STATIC_SCRIPT
    assert ".sort-ind" in rh.STATIC_SCRIPT
    assert "▲" in rh.STATIC_SCRIPT and "▼" in rh.STATIC_SCRIPT
    assert 'class="sort-ind"' in text          # initial indicator markup present


def test_weight_view_has_treemap_and_ladder_both_prerendered(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "weight"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="weight-mode"' in text
    assert 'data-mode="treemap"' in text and 'data-mode="ladder"' in text
    assert 'aria-pressed="true"' in text            # treemap default pressed
    assert 'class="treemap-panel"' in text and 'class="ladder-panel"' in text
    assert "real per-turn tax" in text               # A5 "story" note present (finding #6)
    assert 'id="treemap-always"' in text and 'id="treemap-ondemand"' in text
    # ladder is SVG bars (no style= for width). Bars always carry `heatable` (structural);
    # fhN is added only when heated, so match the class PREFIX (finding #4: an exact
    # 'class="ladder-bar"' with trailing quote can never match 'ladder-bar heatable ...').
    assert 'class="ladder-bar heatable' in text


def test_weight_heat_lands_on_both_always_and_on_demand(tmp_path):
    """Finding #4: prove heat reaches the always-loaded AND the on-demand treemap
    separately — a global `count('heatable fh') >= 2` can be satisfied by ONE node
    duplicated across a single panel's treemap+ladder. Scope to each named panel and
    require a real data-node-key on the heated cell."""
    doc = _minimal_doc()
    out_dir = tmp_path / "wheat"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    # decisions heats an always-loaded node AND coding-team (on-demand)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(
        json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n"
        + json.dumps({"date": "2026-07-01", "component": "coding-team"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    # slice out the always-loaded and on-demand treemap panels by their dom ids
    always = re.search(r'id="treemap-always".*?(?=id="treemap-ondemand"|class="ladder-panel")', text, re.S)
    ondemand = re.search(r'id="treemap-ondemand".*?(?=class="ladder-panel"|</section>)', text, re.S)
    assert always and ondemand
    # each panel independently carries a heated, node-keyed cell. Real keys are
    # `always_loaded:...a.md` and `on_demand:coding-team` (round2 #1) -> match by substring.
    assert re.search(r'heatable fh\d"[^>]*data-node-key="[^"]*a\.md"', always.group(0)) \
        or re.search(r'data-node-key="[^"]*a\.md"[^>]*heatable fh', always.group(0))
    assert re.search(r'heatable fh\d"[^>]*data-node-key="[^"]*coding-team"', ondemand.group(0)) \
        or re.search(r'data-node-key="[^"]*coding-team"[^>]*heatable fh', ondemand.group(0))
    # AM-3: a HEATED, node-keyed LADDER bar must also carry fhN (not just structural heatable)
    ladder = re.search(r'class="ladder-panel".*?</section>', text, re.S)
    assert ladder and re.search(r'ladder-bar heatable fh\d"[^>]*data-node-key="[^"]*a\.md"',
                                ladder.group(0))
    assert 'id="friction-toggle"' in text            # overlay control local to weight view


def test_treemap_label_threshold_constants_are_approved_values():
    """A5 + finding #6 (round2): pin the label auto-hide threshold to the approved ~58x30 —
    exposing it as module constants makes the test threshold-sensitive (a 56x18 constant
    FAILS here), instead of the earlier vacuous 'a fill-opacity exists' check."""
    assert rh.TREEMAP_LABEL_MIN_W >= 58
    assert rh.TREEMAP_LABEL_MIN_H >= 30


def test_treemap_uses_value_scaled_opacity(tmp_path):
    """A5 + finding #6 (round2): opacity must be VALUE-SCALED — assert the treemap emits at
    least TWO DISTINCT fill-opacity values (a constant opacity would collapse to one), via
    an SVG attribute (never style=). Fixture guarantees size variance among always-loaded
    cells. If _minimal_doc lacks size variance, inject two cells of clearly different size."""
    doc = _minimal_doc()
    out_dir = tmp_path / "tmopac"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    opacities = set(re.findall(r'<rect[^>]*\bfill-opacity="([0-9.]+)"', text))
    assert len(opacities) >= 2, f"opacity not value-scaled (distinct values: {opacities})"
    assert 'style=' not in text                      # opacity via attribute, never inline style


def test_design_tokens_define_both_themes_and_semantic_trio(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "tokens"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "@media (prefers-color-scheme: dark)" in text
    assert "--good" in text and "--warn" in text and "--crit" in text
    assert "tabular-nums" in text
    assert "ui-monospace" in text


def test_civc_notes_and_legend_render(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "civc_notes"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "note": "context note here"},
    ], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Coverage scale" in text
    assert "<summary>note</summary>context note here</details>" in text


def test_civc_renamed_to_coverage_matrix_in_display_text(tmp_path):
    """The stale 4-letter acronym must not appear anywhere a human reads the page —
    only the JSON schema key (`civc`) and internal identifiers (build_civc_model,
    the `civc-legend` CSS class) may keep the old name (§Change 2)."""
    doc = _minimal_doc()
    out_dir = tmp_path / "civc_rename"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "note": "context note here"},
    ], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Coverage Matrix" in text
    assert "six verbs (what the harness does to behavior)" in text
    assert "six surfaces (what it" in text
    assert "CIVC" not in text.upper().replace("CIVC-LEGEND", "")


@pytest.mark.parametrize("payload", XSS_PAYLOADS)
def test_civc_note_injection_is_escaped(tmp_path, payload):
    doc = _minimal_doc()
    out_dir = tmp_path / "civc_xss"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "note": payload},
    ], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert payload not in text


def test_coverage_matrix_cells_clickable_and_inspectors_prerendered(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "cov"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "evidence": "V", "note": "n"}],
        "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert text.count('class="cell matrix-cell') == 36
    assert text.count('class="inspector-panel"') == 36
    assert 'data-cell-id="Afford-context"' in text
    # preselect Constrain-memory: verdict token sits BETWEEN matrix-cell and sel
    # (impl class order is fixed: `cell matrix-cell verdict-<v> sel`, then data-cell-id).
    # Constrain-memory has no synth cell -> verdict "empty", so the class is exact:
    assert 'class="cell matrix-cell verdict-empty sel" data-cell-id="Constrain-memory"' in text
    # empty cells get dashed+hatch verdict class
    assert "verdict-empty" in text


def test_coverage_verdict_fill_classes(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "cov2"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered"},
        {"verb": "Evolve", "surface": "observability", "verdict": "thin"}], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # finding #8: assert the NEW combined matrix-cell class (bare "verdict-covered" already
    # exists pre-rework -> a false-green). The reworked Coverage view fills the CELL itself.
    import re
    assert re.search(r'class="cell matrix-cell verdict-covered[ "]', text)
    assert re.search(r'class="cell matrix-cell verdict-thin[ "]', text)


def test_friction_stream_malformed_lines_skip_and_count(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "malformed"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions_file = out_dir / "decisions.jsonl"
    decisions_file.write_text(
        json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n"
        + "not valid json\n"
    )
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert '"records_invalid":1' in text.replace(" ", "") or "records_invalid" in text


# ---------------------------------------------- read_jsonl robustness (Codex challenge F2/F3/F7)
def test_read_jsonl_over_cap_single_line_rejected_without_full_read(tmp_path):
    # FIX 2: a single line far larger than max_bytes must be REJECTED (never fully allocated),
    # so a multi-GB line cannot hang/OOM the renderer. Use a small cap + a 5000-byte line
    # (never GBs) — the same overflow codepath, cheap to exercise.
    stream = tmp_path / "huge.jsonl"
    stream.write_text(json.dumps({"payload": "z" * 5000}) + "\n")

    records, malformed, nonblank = rh.read_jsonl(stream, max_bytes=1000)

    assert records == []                # the over-cap line was never parsed
    assert malformed == 1               # the rejected overflow tail is counted once
    assert nonblank == 0                # no complete line fit under the cap


def test_read_jsonl_over_cap_keeps_complete_lines_rejects_overflow_tail(tmp_path):
    # FIX 2: complete lines fully inside the byte budget still parse; only the past-cap
    # overflow tail is rejected (never parsed mid-token).
    stream = tmp_path / "mixed.jsonl"
    stream.write_text(
        json.dumps({"a": 1}) + "\n"
        + json.dumps({"big": "z" * 5000}) + "\n"
        + json.dumps({"c": 3}) + "\n"
    )

    records, malformed, nonblank = rh.read_jsonl(stream, max_bytes=1000)

    assert records == [{"a": 1}]        # the one complete line under the cap parsed
    assert malformed == 1               # the overflow tail (huge line + trailing line) rejected once
    assert nonblank == 1


def test_read_jsonl_under_cap_returns_all_records(tmp_path):
    # FIX 2: the normal under-cap path is unchanged — every record returned, zero malformed,
    # read through the O_NONBLOCK regular-file open.
    stream = tmp_path / "ok.jsonl"
    stream.write_text(json.dumps({"a": 1}) + "\n" + json.dumps({"b": 2}) + "\n")

    records, malformed, nonblank = rh.read_jsonl(stream)

    assert records == [{"a": 1}, {"b": 2}]
    assert malformed == 0
    assert nonblank == 2


def test_read_jsonl_regular_file_reads_through_nonblock_open(tmp_path):
    # FIX 7: the O_RDONLY|O_NONBLOCK open + S_ISREG fstat accepts a normal regular file and
    # reads its full contents (the guard only rejects non-regular targets like FIFO/device).
    stream = tmp_path / "regular.jsonl"
    stream.write_text(json.dumps({"regular": True}) + "\n")

    records, malformed, nonblank = rh.read_jsonl(stream)

    assert records == [{"regular": True}]
    assert malformed == 0
    assert nonblank == 1


def test_read_jsonl_pathological_bignum_skipped_not_raised(tmp_path):
    # FIX 3: a bare integer past Python's int-string conversion limit raises ValueError
    # (NOT json.JSONDecodeError) inside json.loads; the broadened except must skip+count it
    # as malformed so no exception escapes, and a following valid line must still parse.
    stream = tmp_path / "bignum.jsonl"
    stream.write_text("1" * 5000 + "\n" + json.dumps({"ok": True}) + "\n")

    records, malformed, nonblank = rh.read_jsonl(stream)

    assert records == [{"ok": True}]    # the valid line after the poison line still parsed
    assert malformed == 1               # the pathological number counted as malformed
    assert nonblank == 2                # both physical lines counted as non-blank


# ============================================================= 6. IA pivot: 5 views + switcher
def test_four_views_present_not_six_tabs(tmp_path):
    """Task B-t2 tab merge: the former standalone Coverage tab was folded into
    Overview (its full matrix + inspector now render inside `view-overview`) — 4
    views remain, and no separate `view-coverage` section/tab exists."""
    doc = _minimal_doc()
    out_dir = tmp_path / "views"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    for vid in ("view-overview", "view-weight", "view-friction", "view-hygiene"):
        assert f'id="{vid}"' in text
    assert 'id="view-coverage"' not in text
    assert 'id="view-btn-coverage"' not in text
    p = _ExternalRefParser()
    p.feed(text)
    view_btns = text.count('class="view-btn"')
    assert view_btns == 4
    # no leftover 6-tab panel ids
    assert 'id="panel-6"' not in text
    # progressive enhancement (finding #2): NO view is server-hidden — with JS off,
    # every view is visible/scrollable; the static script collapses to Overview on load.
    # Parse each of the 4 view <section> start tags; assert `hidden` absent regardless of
    # attribute order (round2: a `hidden class="view"` ordering must also fail).
    import re
    view_tags = re.findall(r'<section[^>]*\bclass="view"[^>]*>', text)
    assert len(view_tags) == 4
    for tag in view_tags:
        assert "hidden" not in tag


def test_exactly_one_executable_script(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "onescript"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # exactly one executable script (data islands are type=application/json)
    import re
    exe = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe) == 1


def test_copy_buttons_and_islands_present(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "copy"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    for vid in ("overview", "weight", "friction", "hygiene"):
        assert f'<script type="application/json" id="copy-{vid}">' in text
        assert f'data-copy-target="copy-{vid}"' in text
    assert 'id="copy-coverage"' not in text


def test_copy_disclosure_reveals_exact_payload_before_copy(tmp_path):
    """item 2: transparency before copy — a native <details> reveals the exact
    markdown payload in a scrollable <pre>, gated behind an explicit inner copy
    button. No auto-copy; the existing generic .copy-btn handler is untouched."""
    doc = _minimal_doc()
    out_dir = tmp_path / "copyprev"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert '<details class="copy-preview">' in text
    assert "Copy view as markdown" in text          # per-view label says what it grabs
    assert '<pre class="copy-preview-body">' in text
    # the preview shows the SAME markdown the island holds (esc_html'd) — a known token
    import re
    assert "Coverage" in re.search(r'<pre class="copy-preview-body">(.*?)</pre>', text, re.S).group(1)
    # copy is still gated behind a .copy-btn with data-copy-target (no auto-copy, handler unchanged)
    assert 'data-copy-target="copy-overview"' in text


def test_brief_control_wraps_brief_in_copy_preview(tmp_path):
    doc = _minimal_doc()   # has one dup pair -> one brief
    out_dir = tmp_path / "briefprev"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # the dup brief island still exists AND is now revealed by a copy-preview
    assert 'id="brief-dup-0"' in text
    assert 'data-copy-target="brief-dup-0"' in text
    assert text.count('<details class="copy-preview">') >= 2   # per-view + at least one brief


def test_summary_in_focus_visible_rule():
    assert "summary:focus-visible" in rh.STATIC_STYLE


def test_keyboard_activation_wired_for_button_cells(tmp_path):
    """WCAG 2.2 AA: role=button coverage matrix cells must be keyboard-operable.
    The static script wires a keydown handler to .matrix-cell."""
    doc = _minimal_doc()
    out_dir = tmp_path / "kbd"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "addEventListener('keydown'" in text
    assert "querySelectorAll('.matrix-cell')" in text
    assert "e.preventDefault()" in text          # Space must not scroll


# ============================================================= 7. Overview digest + hero + nav
def test_overview_default_view_has_full_matrix_and_no_friction_heat(tmp_path):
    """Task B-t2 tab merge: Overview's main area is the FULL 36-cell Coverage Matrix
    (no separate mini-grid strip any more) plus the friction hero + digest sidebar."""
    doc = _minimal_doc()
    out_dir = tmp_path / "ov"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered"}],
        "drag_candidates": [{"n": 1, "surface": "memory", "evidence": "e", "outcome": "keep",
                             "what_must_survive": "", "risk_if_wrong": ""}]}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # overview visible by default (not hidden)
    assert '<section id="view-overview" class="view"' in text
    # the mini-grid strip is gone — the full 36-cell matrix is the "glance" now
    assert 'class="mini-grid"' not in text
    assert text.count('class="cell matrix-cell') == 36
    assert 'class="hero-friction' in text
    # RESOLVED DECISION 1 + finding #4 NEGATIVE contract: the Overview view must carry NO
    # friction heat — slice the whole view-overview <section> and assert no heat markers.
    import re
    ov = re.search(r'<section id="view-overview".*?</section>', text, re.S)
    assert ov is not None
    ov_html = ov.group(0)
    assert "heatable" not in ov_html
    assert re.search(r'\bfh\d\b', ov_html) is None
    assert "data-node-key" not in ov_html


def test_overview_digest_lists_roadmap_gaps_and_drag(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "ov2"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [], "drag_candidates": [
        {"n": 1, "surface": "memory", "evidence": "e", "outcome": "probation",
         "what_must_survive": "", "risk_if_wrong": ""}]}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Needs attention" in text
    # finding #8: scope to the Overview section — a global "probation" match could be
    # satisfied by Friction's (moved) drag table even if the Overview digest omitted it.
    import re
    ov = re.search(r'<section id="view-overview".*?</section>', text, re.S)
    assert ov is not None and "probation" in ov.group(0)   # drag candidate in the Overview digest


def test_drag_candidates_render_meaning_and_unique_island_ids(tmp_path):
    import re
    template_path = Path(__file__).resolve().parents[1] / "synthesis-template.json"
    synth = json.loads(template_path.read_text(encoding="utf-8"))
    synth.setdefault("schema_version", 1)
    doc = _minimal_doc()
    out_dir = tmp_path / "drag"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "drag candidate is a component" in text.lower()          # definition line
    assert "Must survive:" in text and "Risk if wrong:" in text      # labeled fields
    # globally-unique island ids per site (first-match getElementById safety)
    assert 'id="brief-drag-0"' in text and 'id="brief-dragov-0"' in text
    # overview section stays free of data-node-key (RESOLVED DECISION 1)
    ov = re.search(r'<section id="view-overview".*?</section>', text, re.S).group(0)
    assert "data-node-key" not in ov


# ================================================================== contract layer (real collector)
def test_contract_layer_real_collector_output_renders(tmp_path, fake_harness):
    collector_doc = run_collector(fake_harness)
    out_dir = tmp_path / "contract"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", collector_doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / "harness-map-2026-07-15.html").is_file()


def test_write_html_safely_refuses_inside_harness_root(tmp_path):
    fake_root = tmp_path / "fakeclaude"
    fake_root.mkdir()
    out_dir = fake_root / "reports"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(fake_root)
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode != 0
    assert not (out_dir / "harness-map-2026-07-15.html").exists()


def test_hard_link_target_regression(tmp_path):
    """A pre-existing HTML target that is a hard link to a file inside the doc's
    `root` must not be truncated by the write (Codex F1)."""
    fake_root = tmp_path / "harnessroot"
    fake_root.mkdir()
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(tmp_path / "unrelated-root")
    (tmp_path / "unrelated-root").mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)

    inside_file = fake_root / "shared.html"
    inside_file.write_text("ORIGINAL CONTENT SHOULD SURVIVE")
    target = out_dir / "harness-map-2026-07-15.html"
    os.link(inside_file, target)

    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    assert inside_file.read_text() == "ORIGINAL CONTENT SHOULD SURVIVE"


# ============================================================= 9. Hygiene fold + provenance footer
def test_hygiene_view_folds_dup_phantom_trend_and_wiring(tmp_path):
    doc = _minimal_doc()
    # inject explicit hygiene inputs so the A6 presentation contracts are provable
    doc["instruction_length_flags"] = [
        {"path": "skills/review/SKILL.md", "lines": 1467, "threshold": 200, "evidence": "VERIFIED"},   # > 600 -> critical pill
        {"path": "agents/ct-implementer.md", "lines": 206, "threshold": 200, "evidence": "VERIFIED"},  # over cap, not critical
    ]
    doc["headline"] = dict(doc.get("headline", {}), unchecked_binary_count=3)
    # inject one dup pair with a known overlap so "% shared" is a checkable value.
    # field names match the REAL dup-model input schema (build_dupweb_model reads
    # doc["duplication"]["pairs"] with a/b/score/shared_sample, and doc["phantom_refs"]
    # top-level, not nested under "duplication" — see collector.py/build_dupweb_model).
    doc["duplication"] = {"shingle_k": 8, "metric": "containment", "threshold": 0.6,
                           "pairs": [{"a": "rules/x.md", "b": "rules/y.md", "score": 0.9,
                                      "shared_sample": "sample", "evidence": "INFERRED"}]}
    doc["phantom_refs"] = []
    out_dir = tmp_path / "hyg"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", doc)   # 2 sidecars -> trend table
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    hyg = re.search(r'<section id="view-hygiene".*?</section>', text, re.S)
    assert hyg is not None
    hyg_html = hyg.group(0)
    assert "Wiring integrity" in hyg_html             # bipartite folded in
    assert "write-guard.py" in hyg_html               # registered hook surfaced
    assert "orphan" in hyg_html.lower()
    assert "Trend" in hyg_html                         # trend table folded in
    # A6 length flags (round2 #4): the CRITICAL pill sits on the 1467-line row specifically.
    crit_row = re.search(r'<tr[^>]*>(?:(?!</tr>).)*?1467(?:(?!</tr>).)*?</tr>', hyg_html, re.S)
    assert crit_row and "critical" in crit_row.group(0).lower()
    # the 206-line row is over-cap but NOT critical
    over_row = re.search(r'<tr[^>]*>(?:(?!</tr>).)*?206(?:(?!</tr>).)*?</tr>', hyg_html, re.S)
    assert over_row and "critical" not in over_row.group(0).lower()
    # A6 dup presentation (round2 #4): "<a> ⇄ <b>" + 90% shared on the SAME dup row.
    # (scoped to the <tr> like crit_row/over_row above — the plan's Step-1 snippet
    # for this assertion truncated the <tr>...</tr> anchors; restored here so the
    # match actually spans the whole row instead of stopping at the arrow glyph.)
    dup_row = re.search(r'<tr[^>]*>(?:(?!</tr>).)*?⇄(?:(?!</tr>).)*?</tr>', hyg_html, re.S)
    assert dup_row and "%" in dup_row.group(0) and "90" in dup_row.group(0)
    # finding #5a (round2 #4): unchecked_binary_count in a DEDICATED hygiene element,
    # not merely present because the folded Trend table happens to include it.
    unchecked = re.search(r'class="hygiene-unchecked"[^>]*>[^<]*3', hyg_html)
    assert unchecked is not None
    # Task 3's contract still holds: unchecked binaries no longer a gauge.
    assert 'data-gauge="unchecked_binary_count"' not in text


def test_provenance_footer_keeps_warning_visible(tmp_path):
    import re
    doc = _minimal_doc()
    doc["inaccessible"] = [{"path": "secret.md", "reason": "denied"}]
    out_dir = tmp_path / "prov"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="provenance"' in text
    assert "warning" in text.lower()
    assert "secret.md" in text                        # detail in <details>
    # the warning count is inline (not hidden behind <details>) — must appear
    # BEFORE the <details> element, not only inside it.
    footer = re.search(r'<footer[^>]*id="provenance"[^>]*>.*?</footer>', text, re.S).group(0)
    before_details = footer.split("<details", 1)[0]
    assert "warning" in before_details.lower()


# ============================================================= 8. copy payload content + IA determinism (Task 10, A8/A9)
def test_copy_payload_overview_includes_coverage_markdown_table(tmp_path):
    """Task B-t2 tab merge: the coverage markdown table is now folded into the
    single "overview" copy island (there is no standalone "coverage" island)."""
    doc = _minimal_doc()
    out_dir = tmp_path / "cpmd"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered"}], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    assert 'id="copy-coverage"' not in text
    m = re.search(r'<script type="application/json" id="copy-overview">(.*?)</script>', text, re.S)
    assert m is not None
    payload = json.loads(m.group(1))          # island stores a JSON string
    assert "| verb" in payload
    assert "covered" in payload


def test_all_four_copy_payloads_present_and_nonempty(tmp_path):
    """Finding #9: every view's copy island must carry real, non-empty markdown — not just
    Coverage. Empty/malformed Overview/Weight/Friction/Hygiene payloads must fail here."""
    doc = _minimal_doc()
    out_dir = tmp_path / "cpall"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered"}], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    # round2 #5: assert VIEW-SPECIFIC content per island (a generic stub must fail), and
    # require exactly 4 islands (a set() would mask duplicate ids).
    islands = re.findall(r'<script type="application/json" id="copy-(\w+)">', text)
    assert len(islands) == 4 and sorted(islands) == \
        ["friction", "hygiene", "overview", "weight"]

    def payload(vid):
        m = re.search(rf'<script type="application/json" id="copy-{vid}">(.*?)</script>', text, re.S)
        assert m is not None, f"copy-{vid} island missing"
        p = json.loads(m.group(1))                       # each island is a JSON string
        assert isinstance(p, str) and p.strip(), f"copy-{vid} payload empty"
        return p
    # each view's payload carries a marker unique to that view's builder output
    op = payload("overview")
    assert "harness-map" in op
    assert "| verb" in op                 # Coverage Matrix table folded in (tab merge)
    wp = payload("weight")
    assert "tokens" in wp or "always-loaded" in wp.lower()
    fp = payload("friction")
    assert "codex" in fp.lower()          # _codex_sentence always appended
    hp = payload("hygiene")
    assert ("hygiene" in hp.lower() or "dup" in hp.lower()
                                      or "phantom" in hp.lower())


def test_copy_islands_are_inert_not_executable(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "inert"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    exe = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe) == 1
    # finding #8: a zero-island output would ALSO pass the count above — so require the 4
    # inert islands to be present. This ties the "one executable script" contract to the
    # islands actually existing.
    islands = re.findall(r'<script type="application/json" id="copy-(\w+)">', text)
    assert set(islands) == {"overview", "weight", "friction", "hygiene"}


def test_full_ia_determinism_byte_identical(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "detfull"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "note": "n"}],
        "drag_candidates": [{"n": 1, "surface": "memory", "evidence": "e", "outcome": "keep",
                             "what_must_survive": "", "risk_if_wrong": ""}]}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    a = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert a.returncode == 0, a.stderr
    b1 = (out_dir / "harness-map-2026-07-15.html").read_bytes()
    b = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert b.returncode == 0, b.stderr
    b2 = (out_dir / "harness-map-2026-07-15.html").read_bytes()
    assert b1 == b2
    # finding #8: prove this fixture actually exercises the NEW IA (else it false-greens
    # against pre-rework code, which is already deterministic).
    assert b'class="gauges"' in b1 and b'id="view-overview"' in b1
    assert b'class="cell matrix-cell' in b1   # merged Coverage Matrix renders inside Overview
    assert b'<script type="application/json" id="copy-overview">' in b1


def test_full_ia_determinism_cross_pythonhashseed(tmp_path):
    """Finding #10: run the ENRICHED fixture (synthesis + friction, exercising every new
    helper) under TWO different PYTHONHASHSEED values — the same-seed test above cannot
    catch dict/set-ordering nondeterminism; the pre-existing cross-seed fixture had no
    synthesis or friction."""
    doc = _minimal_doc()
    out_dir = tmp_path / "detseed"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "note": "n"}],
        "drag_candidates": [{"n": 1, "surface": "memory", "evidence": "e", "outcome": "keep",
                             "what_must_survive": "", "risk_if_wrong": ""}]}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    outs = []
    for seed in ("0", "1"):
        p = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions),
                       env={**os.environ, "PYTHONHASHSEED": seed})
        # round2 #6: assert the render SUCCEEDED before reading — else a failed 2nd render
        # leaves the 1st render's file in place and the byte compare false-passes.
        assert p.returncode == 0, p.stderr
        outs.append((out_dir / "harness-map-2026-07-15.html").read_bytes())
    assert outs[0] == outs[1]


def test_csp_hashes_cover_the_emitted_blocks(tmp_path):
    """Finding #7: recompute the sha256 of the ACTUAL emitted <style> and executable
    <script> block bytes and assert each matches the CSP meta hash — comparing the CSP
    values to the module constants (rh.STATIC_STYLE / rh.STATIC_SCRIPT) would miss any
    per-render interpolation into the emitted block. Also assert the executable script
    bytes are identical across two materially-different inputs (proves it stays static)."""
    import re
    import hashlib
    import base64

    def emit(out_dir, **kw):
        proc = run_render(out_dir, "--date", "2026-07-15", **kw)
        assert proc.returncode == 0, proc.stderr
        return (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    d1 = tmp_path / "csp1"
    d1.mkdir()
    _write_sidecar(d1, "2026-07-15", _minimal_doc())
    text = emit(d1, extra=["--no-friction"])
    # extract the CSP script/style sha256 tokens and the emitted blocks
    meta = re.search(r"script-src 'sha256-([A-Za-z0-9+/=]+)'", text)
    style_meta = re.search(r"style-src 'sha256-([A-Za-z0-9+/=]+)'", text)
    assert meta and style_meta
    exe = re.search(r'<script(?![^>]*application/json)[^>]*>(.*?)</script>', text, re.S)
    sty = re.search(r'<style>(.*?)</style>', text, re.S)
    assert exe and sty
    got_script = base64.b64encode(hashlib.sha256(exe.group(1).encode()).digest()).decode()
    got_style = base64.b64encode(hashlib.sha256(sty.group(1).encode()).digest()).decode()
    assert got_script == meta.group(1)
    assert got_style == style_meta.group(1)
    # executable script bytes are input-invariant (fully static)
    d2 = tmp_path / "csp2"
    d2.mkdir()
    _write_sidecar(d2, "2026-07-15", _minimal_doc())
    dec = d2 / "d.jsonl"
    dec.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    text2 = emit(d2, extra=["--decisions-file", str(dec)])
    exe2 = re.search(r'<script(?![^>]*application/json)[^>]*>(.*?)</script>', text2, re.S)
    assert exe.group(1) == exe2.group(1)


# ============================================================= 10. Codex+QA gate follow-ups
def test_build_civc_model_rejects_unallowlisted_verdict():
    """FIX 1 (P1): a crafted synthesis `verdict` must not pass through unallowlisted —
    only {"covered","thin","empty"} survive; anything else normalizes to "empty"."""
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered fh1 heatable"},
        {"verb": "Evolve", "surface": "observability", "verdict": "thin"},
    ], "drag_candidates": []}
    model = rh.build_civc_model(synth)
    afford = next(c for c in model["cells"] if c["verb"] == "Afford" and c["surface"] == "context")
    assert afford["verdict"] == "empty"
    evolve = next(c for c in model["cells"] if c["verb"] == "Evolve" and c["surface"] == "observability")
    assert evolve["verdict"] == "thin"


def test_civc_verdict_injection_normalized_in_overview_and_coverage(tmp_path):
    """FIX 1: full-render proof the class-injection is neutralized — the merged
    Overview view's matrix-cell must carry NO `fh1`/`heatable` token, and must render
    as `verdict-empty` (normalized), never the raw crafted string."""
    doc = _minimal_doc()
    out_dir = tmp_path / "civc_verdict_xss"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered fh1 heatable"},
    ], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    ov = re.search(r'<section id="view-overview".*?</section>', text, re.S)
    assert ov is not None
    assert "fh1" not in ov.group(0) and "heatable" not in ov.group(0)
    assert re.search(r'class="cell matrix-cell verdict-empty[ "]', ov.group(0))


def test_render_survives_lone_surrogate_path_without_crash(tmp_path):
    """FIX 2 (P1): a sidecar path carrying a lone UTF-16 surrogate must never crash the
    final UTF-8 write — the renderer must always emit a complete, valid-UTF-8 file."""
    doc = _minimal_doc(extra_files=[
        {"path": "bad\ud800.md", "category": "rule", "words": 1, "lines": 1,
         "tokens_est": 5, "evidence": "VERIFIED"},
    ])
    out_dir = tmp_path / "surrogate"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    out_file = out_dir / "harness-map-2026-07-15.html"
    file_bytes = out_file.read_bytes()
    text = file_bytes.decode("utf-8")   # must not raise UnicodeDecodeError
    assert "bad" in text


def test_treemap_badge_renders_even_when_label_suppressed():
    """FIX 3 (P2): the friction legend claims "color is never the only signal" — that
    claim is only true if EVERY heated cell shows a join-count badge, even a cell whose
    text label is suppressed for being below TREEMAP_LABEL_MIN_W/H."""
    tree = {"cells": [{"path": "tiny.md", "node_key": "always_loaded:tiny.md",
                        "x": "0.00", "y": "0.00", "w": "10.00", "h": "10.00", "fill": "#000"}],
            "canvas_w": 20.0, "canvas_h": 20.0}
    heat = {"always_loaded:tiny.md": 3}
    svg = rh._render_treemap_svg(tree, heat, "t")
    assert 'class="cell-label"' not in svg     # confirms this cell IS below the label threshold
    assert 'class="friction-badge">3</text>' in svg


def test_ladder_badge_renders_on_heated_bar():
    """Companion to the treemap FIX 3 test above (Codex round-2 P2, ladder residual):
    the friction legend claims "every heated cell also shows a join-count badge" —
    that claim was only true for treemap cells. A heated LADDER bar must also carry
    a VISIBLE `friction-badge` text element with the same join count the hover
    `<title>` already carries, not only a hover-only title."""
    tree = {"cells": [{"path": "a.md", "node_key": "always_loaded:a.md",
                        "size": 10, "fill": "#000"}],
            "canvas_w": 20.0, "canvas_h": 20.0}
    heat = {"always_loaded:a.md": 3}
    svg = rh._render_ladder_svg(tree, heat, "l")
    assert 'class="ladder-bar heatable fh' in svg
    assert 'class="friction-badge">3</text>' in svg


def test_length_critical_node_keys_reuses_hygiene_threshold_not_a_new_one():
    """B-t3 follow-up: `_length_critical_node_keys` must be the SAME >600-line cut
    the Hygiene tab's `critical` pill already uses (`LENGTH_CRITICAL_LINES`), and
    v1 is CRITICAL-only — a merely `over`-cap file (<=600 lines) must NOT appear.
    Returns a `{node_key: lines}` dict (not a bare set) — the `lines` value feeds
    the treemap/ladder `<title>` reason text (operator-caught follow-up: a ring
    with no explanation in the ONLY hover text read as a contradiction)."""
    doc = _minimal_doc()
    doc["instruction_length_flags"] = [
        {"path": "skills/coding-team/SKILL.md", "lines": 700, "threshold": 200, "evidence": "VERIFIED"},
        {"path": "rules/a.md", "lines": 210, "threshold": 200, "evidence": "VERIFIED"},
    ]
    keys = rh._length_critical_node_keys(doc)
    assert keys == {"on_demand:coding-team": 700}   # the 210-line "over" file is excluded


def test_treemap_and_ladder_render_length_crit_ring_on_critical_cell_only(tmp_path):
    """B-t3 follow-up: the length-criticality outline must land on the SPECIFIC
    cell hygiene's own length-flag table classifies `critical` (here `coding-team`'s
    SKILL.md, injected at 700 lines) and NOT on an unrelated on-demand cell (the
    `ct-implementer.md` agent body) — proves the signal is per-file, not a blanket
    always-on decoration. Also asserts the marker + legend render, and that the ring
    element itself never carries `heatable` (so the friction-dim rule can't touch it,
    per the hard invariant)."""
    doc = _minimal_doc()
    doc["instruction_length_flags"] = [
        {"path": "skills/coding-team/SKILL.md", "lines": 700, "threshold": 200, "evidence": "VERIFIED"},
    ]
    out_dir = tmp_path / "lencrit"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    weight = re.search(r'<section id="view-weight".*?</section>', text, re.S).group(0)
    # the critical skill's cell carries the ring, is never `heatable`, and gets a marker
    crit_ring = re.search(
        r'<rect[^>]*class="length-crit-ring"[^>]*data-node-key="on_demand:coding-team"[^>]*/>', weight)
    assert crit_ring is not None
    assert "heatable" not in crit_ring.group(0)
    assert re.search(r'<circle[^>]*class="length-crit-marker"[^>]*/>', weight)
    # an unrelated on-demand cell (the agent body) must NOT get the ring
    non_crit_ring = re.search(
        r'class="length-crit-ring"[^>]*data-node-key="on_demand:skills/coding-team/agents/ct-implementer\.md"',
        weight)
    assert non_crit_ring is None
    # exactly two rings total: one in the treemap panel, one in the ladder panel,
    # both for the same critical cell (never duplicated onto other cells)
    assert weight.count('class="length-crit-ring"') == 2
    assert 'id="length-crit-legend"' in weight
    assert "600 lines" in weight


def test_length_crit_ring_is_warn_not_crit_and_title_explains_reason(tmp_path):
    """B-t3 fix (operator-caught conflation): the ring/marker/legend-swatch must
    use `--warn` (amber), NOT `--crit` (red) — red is friction heat's own fh1-4
    ramp, and a crit-colored ring on a zero-friction tile read as a false friction
    claim. Separately, the ring's ONLY companion signal is the cell's hover
    `<title>` — for the critical cell it must explain WHY (line count), and a
    DIFFERENT, non-critical cell's title must be completely unchanged (no
    dangling "critically oversized" text on a cell that isn't)."""
    doc = _minimal_doc()
    doc["instruction_length_flags"] = [
        {"path": "skills/coding-team/SKILL.md", "lines": 700, "threshold": 200, "evidence": "VERIFIED"},
    ]
    out_dir = tmp_path / "lencrit_warn"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re

    # color decoupled from friction-red, in the CSS itself
    assert ".length-crit-ring{fill:none;stroke:var(--warn)" in text
    assert ".length-crit-marker{fill:var(--warn)" in text
    assert ".legend-swatch.length-crit-swatch{background:transparent;border:2px solid var(--warn)" in text
    assert ".length-crit-ring{fill:none;stroke:var(--crit)" not in text
    assert ".length-crit-marker{fill:var(--crit)" not in text

    # the critical cell's title explains the reason with its actual line count, and
    # explicitly decouples the amber ring from churn (item 6)
    assert "size: 700 lines" in text
    crit_title = re.search(
        r'data-node-key="on_demand:coding-team">'
        r'<title>([^<]*)</title>', text)
    assert crit_title is not None
    assert "size: 700 lines" in crit_title.group(1)
    assert "amber ring = oversize, NOT churn" in crit_title.group(1)

    # a non-critical cell's title is unchanged — no dangling reason text
    non_crit_title = re.search(
        r'data-node-key="on_demand:skills/coding-team/agents/ct-implementer\.md">'
        r'<title>([^<]*)</title>', text)
    assert non_crit_title is not None
    assert "size:" not in non_crit_title.group(1)
    assert "amber ring" not in non_crit_title.group(1)
    assert non_crit_title.group(1).endswith("churn: none recorded")


def test_cell_title_decouples_ring_from_churn_and_click_jumps(tmp_path):
    doc = _minimal_doc(extra_files=[
        {"path": "skills/coding-team/agents/ct-implementer.md", "category": "skill",
         "words": 4000, "lines": 700, "tokens_est": 5000, "evidence": "VERIFIED"}])
    doc["instruction_length_flags"] = [{"path": "skills/coding-team/agents/ct-implementer.md",
                                        "lines": 700, "threshold": 200, "evidence": "VERIFIED"}]
    out_dir = tmp_path / "celltitle"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # a zero-friction tile reads "churn: none recorded", never "(friction: 0)" contradiction
    assert "churn: none recorded" in text
    assert "(friction: 0" not in text
    # a critically-oversized tile's title explains the amber ring is size, NOT churn
    assert "amber ring = oversize, NOT churn" in text
    assert "700 lines" in text
    # click-to-act block wired: activates Friction tab + highlights the component row
    assert "svg [data-node-key]" in rh.STATIC_SCRIPT
    assert "activate('view-friction')" in rh.STATIC_SCRIPT
    # B4: the row lookup compares getAttribute — it must NOT interpolate the key into a
    # selector string (a POSIX path with a quote would throw DOMException)
    assert "getAttribute('data-node-key') === key" in rh.STATIC_SCRIPT
    assert '[data-node-key="' + "'" not in rh.STATIC_SCRIPT  # no interpolated-value selector


def test_click_target_key_with_selector_significant_char_round_trips(tmp_path):
    # a node_key containing a '"' (legal in a POSIX path) must emit as an escaped attribute
    # on BOTH the treemap cell and the component row — the iterate-compare handler then
    # matches them at runtime without a selector-injection crash.
    doc = _minimal_doc(extra_files=[
        {"path": 'rules/a"b.md', "category": "rule", "words": 10, "lines": 2,
         "tokens_est": 30, "evidence": "VERIFIED"}])
    out_dir = tmp_path / "quotedkey"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": 'rules/a"b.md'}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # attribute is HTML-escaped (&quot;) — raw unescaped quote inside the attr value never appears
    assert 'data-node-key="always_loaded:rules/a&quot;b.md"' in text


def test_length_crit_ring_absent_when_no_length_flags(tmp_path):
    """The length-crit legend + ring must not render at all when nothing is
    length-critical — no dead UI for a signal with nothing to show."""
    doc = _minimal_doc()
    doc["instruction_length_flags"] = []
    out_dir = tmp_path / "nolencrit"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="length-crit-legend"' not in text
    assert 'class="length-crit-ring"' not in text


def test_trend_delta_bad_direction_when_metric_increases():
    """FIX 4 (P3): _trend_delta must consult the series' own polarity, not render every
    change identically. `always_loaded_words` is polarity="up" (growth is the BAD
    direction) — an increase must resolve to the "bad" semantic."""
    trend = {"first_run": False, "series": [
        {"key": "always_loaded_words", "label": "Always-loaded words", "polarity": "up",
         "values": [100, 150]},
    ]}
    assert rh._trend_delta(trend, "always_loaded_words") == ("▲ 50", "bad")


def test_trend_delta_good_direction_when_bad_polarity_metric_decreases():
    trend = {"first_run": False, "series": [
        {"key": "always_loaded_words", "label": "Always-loaded words", "polarity": "up",
         "values": [150, 100]},
    ]}
    assert rh._trend_delta(trend, "always_loaded_words") == ("▼ 50", "good")


def test_trend_delta_neutral_for_none_polarity():
    trend = {"first_run": False, "series": [
        {"key": "always_loaded_file_count", "label": "x", "polarity": "none", "values": [5, 6]},
    ]}
    assert rh._trend_delta(trend, "always_loaded_file_count") == ("▲ 1", "neutral")


def test_trend_delta_none_on_first_run():
    assert rh._trend_delta({"first_run": True, "series": []}, "always_loaded_words") is None


def test_trend_delta_renders_polarity_aware_class_in_gauge_card(tmp_path):
    """FIX 8: a 2-sidecar render must exercise the `first_run=False` delta path — the
    tokens gauge card must show the arrow+magnitude AND (FIX 4) the bad-direction
    semantic class, since growth in always-loaded tokens is the BAD direction."""
    doc1 = _minimal_doc(tokens_a=100, tokens_b=50)
    doc2 = _minimal_doc(tokens_a=200, tokens_b=50)
    out_dir = tmp_path / "trenddelta"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", doc1)
    _write_sidecar(out_dir, "2026-07-15", doc2)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    gauge = re.search(r'data-gauge="always_loaded_tokens_est".*?</div>\s*</div>', text, re.S)
    assert gauge is not None
    assert 'class="delta delta-bad"' in gauge.group(0)
    assert "▲" in gauge.group(0) and "100" in gauge.group(0)


def test_data_goto_cross_view_nav_removed_after_tab_merge():
    """Task B-t2 tab merge: `data-goto` only ever existed to drive the Overview
    mini-grid's "jump to Coverage tab" click — dropping the mini-grid (folded into
    the merged matrix) removes its only consumer, so the handler and the attribute
    must both be fully gone from the static script (no dead selector left behind)."""
    assert "data-goto" not in rh.STATIC_SCRIPT
    # the ArrowLeft/ArrowRight tablist nav still moves focus between view-btns
    assert ".focus()" in rh.STATIC_SCRIPT


def test_view_sections_are_focusable_targets(tmp_path):
    """`.focus()` (used by the ArrowLeft/ArrowRight tablist nav) only works if the
    target view is itself focusable — every view section must carry `tabindex="-1"`
    as a programmatic-focus target."""
    doc = _minimal_doc()
    out_dir = tmp_path / "focusable"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    for vid in ("view-overview", "view-weight", "view-friction", "view-hygiene"):
        tag = re.search(rf'<section id="{vid}"[^>]*>', text)
        assert tag is not None
        assert 'tabindex="-1"' in tag.group(0)


def test_view_tablist_arrow_key_navigation_wired(tmp_path):
    """FIX 6 (P3, WCAG APG tablist pattern): ArrowLeft/ArrowRight must traverse the
    `.view-btn` group, not just click/Enter/Space."""
    doc = _minimal_doc()
    out_dir = tmp_path / "arrownav"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "ArrowRight" in text and "ArrowLeft" in text
    assert 'role="tablist"' in text


def test_friction_toggle_hidden_under_no_friction_shown_when_enabled(tmp_path):
    """FIX 7 (P3): `#friction-toggle` + `#friction-legend` must not render under
    `--no-friction` (there is nothing for the toggle to reveal); a friction-ON render
    (via `--decisions-file`) must still carry them — Task 6's heat tests rely on this."""
    doc = _minimal_doc()
    out_dir = tmp_path / "toggle_gate"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc_off = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc_off.returncode == 0, proc_off.stderr
    text_off = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="friction-toggle"' not in text_off
    assert 'id="friction-legend"' not in text_off

    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    proc_on = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc_on.returncode == 0, proc_on.stderr
    text_on = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="friction-toggle"' in text_on
    assert 'id="friction-legend"' in text_on


def test_friction_toggle_hidden_when_zero_nodes_heated(tmp_path):
    """(f) §C1 change 4 (Codex-caught): friction-enabled but ALL telemetry is
    unmatched -> zero heated nodes. The toggle + legend must NOT render (an active
    toggle over an all-dim treemap with nothing highlighted is worse than no toggle);
    a plain 'no node-attributed friction' note renders instead."""
    doc = _minimal_doc()
    out_dir = tmp_path / "zero_heat"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    # bare-name ref matching nothing in this doc -> unmatched, zero heat
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "no-match-here.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="friction-toggle"' not in text
    assert 'id="friction-legend"' not in text
    assert "no node-attributed friction" in text


# ============================================================= 6. B3/D6 action-launcher briefs
def test_consolidation_brief_is_deterministic_and_coding_team_ready():
    pair = {"a": "rules/a.md", "b": "rules/b.md", "score": 0.9, "shared_sample": "shared words"}
    md1 = rh.build_consolidation_brief(pair)
    md2 = rh.build_consolidation_brief(pair)
    assert md1 == md2
    assert md1.startswith("#") or "## " in md1
    assert "rules/a.md" in md1 and "rules/b.md" in md1
    assert "/coding-team" in md1


def test_refactor_brief_deterministic():
    flag = {"path": "skills/x/SKILL.md", "lines": 240}
    assert rh.build_refactor_brief(flag) == rh.build_refactor_brief(flag)
    md = rh.build_refactor_brief(flag)
    assert "240" in md
    assert "skills/x/SKILL.md" in md
    assert "/coding-team" in md


def test_gap_stub_brief_deterministic():
    md1 = rh.build_gap_stub_brief("Constrain", "memory")
    md2 = rh.build_gap_stub_brief("Constrain", "memory")
    assert md1 == md2
    assert md1.startswith("#") or "## " in md1
    assert "Constrain" in md1 and "memory" in md1
    assert "/coding-team" in md1


def test_build_phantom_ref_brief_is_pure_and_kind_aware():
    ref = {"source": "rules/a.md", "ref": "nope.md", "kind": "path", "resolved": False}
    a = rh.build_phantom_ref_brief(ref)
    b = rh.build_phantom_ref_brief(dict(ref))
    assert a == b                                  # pure: same input, same bytes
    assert "rules/a.md" in a and "nope.md" in a
    assert "Broken path" in a                      # path+unresolved guidance
    ext = rh.build_phantom_ref_brief({"source": "s", "ref": "http://x", "kind": "external",
                                      "resolved": False})
    assert "External ref" in ext
    unknown = rh.build_phantom_ref_brief({"source": "s", "ref": "r", "kind": "mystery",
                                          "resolved": False})
    assert "Verify the target exists" in unknown   # catch-all


def test_phantom_table_has_guidance_column_and_brief(tmp_path):
    doc = _minimal_doc()   # one phantom ref
    out_dir = tmp_path / "phantom"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "phantom ref is a pointer" in text.lower()    # definition line
    assert "<th>What to do</th>" in text                  # new column
    assert 'id="brief-phantom-0"' in text                # per-row brief island
    assert 'data-copy-target="brief-phantom-0"' in text


def test_brief_islands_and_buttons_render_for_findings(tmp_path):
    doc = _minimal_doc()   # has 1 dup pair + phantom ref
    doc["instruction_length_flags"] = [{"path": "skills/x/SKILL.md", "lines": 240, "evidence": "VERIFIED"}]
    out_dir = tmp_path / "briefs"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered"},
    ], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")

    # dup pair (index 0) -> brief-dup-0
    assert 'data-copy-target="brief-dup-0"' in text
    assert '<script type="application/json" id="brief-dup-0">' in text

    # over-cap flag (index 0 in the sorted length-flag table) -> brief-overcap-0
    assert 'data-copy-target="brief-overcap-0"' in text
    assert '<script type="application/json" id="brief-overcap-0">' in text

    # 35 of the 36 Coverage Matrix cells are empty (only Afford x context is covered)
    # -> at least one gap-stub island/button, indexed brief-gap-0
    assert 'data-copy-target="brief-gap-0"' in text
    assert '<script type="application/json" id="brief-gap-0">' in text

    # still no inline handlers / style attrs (CSP model intact)
    p = _ExternalRefParser()
    p.feed(text)
    assert p.on_handlers == []
    assert p.style_attrs == []
    # STATIC_SCRIPT stays the ONE executable <script> — every brief island is inert
    import re
    exe_scripts = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe_scripts) == 1


def test_no_brief_islands_or_buttons_when_no_actionable_findings(tmp_path):
    doc = _minimal_doc()
    doc["duplication"]["pairs"] = []
    doc["instruction_length_flags"] = []
    out_dir = tmp_path / "no_briefs"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    # no synthesis sidecar written -> Coverage Matrix unavailable, zero cells, zero gap briefs
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "brief-dup-" not in text
    assert "brief-overcap-" not in text
    assert "brief-gap-" not in text


def test_brief_payload_is_the_pure_builder_markdown(tmp_path):
    """The island payload is JSON-encoded so it's inert inside the script tag —
    decoding it must yield exactly what the pure builder produces for the same input."""
    doc = _minimal_doc()
    out_dir = tmp_path / "brief_payload"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    m = re.search(r'<script type="application/json" id="brief-dup-0">(.*?)</script>', text, re.S)
    assert m is not None
    payload = json.loads(m.group(1))
    expected = rh.build_consolidation_brief(doc["duplication"]["pairs"][0])
    assert payload == expected


def test_static_script_byte_unchanged_by_action_launcher_briefs():
    """B3/D6 spec: the brief buttons reuse the EXISTING `.copy-btn` machinery — the
    generic click handler already reads `data-copy-target` for ANY `.copy-btn`, so
    STATIC_SCRIPT itself carries no brief-specific code and its hash never moves."""
    assert "brief-" not in rh.STATIC_SCRIPT
    assert "build_consolidation_brief" not in rh.STATIC_SCRIPT


def test_dupweb_renders_when_duplication_is_null(tmp_path):
    """T6 audit FIX 1: a hand-edited/corrupted sidecar with `"duplication": null`
    (structurally valid top-level dict + schema_version, malformed nested value) must
    not crash the whole render. `_render_hygiene_view` mirrors the existing
    `build_dupweb_model` guard (`doc.get("duplication", {}) or {}`) rather than a bare
    `doc.get("duplication", {}).get("pairs", [])`, which raises AttributeError when the
    key is present with value `None` (the `.get` default only covers a MISSING key)."""
    doc = _minimal_doc()
    doc["duplication"] = None
    out_dir = tmp_path / "null_duplication"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    assert "AttributeError" not in proc.stderr
    assert "Traceback" not in proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "no duplicate pairs above threshold" in text


# ============================================================= 8. maximal fixture + determinism net
def _maximal_doc_and_synth():
    """Task 8 fixture: `_minimal_doc()` extended to light up every new T1-T7 surface in
    ONE render. The critical (>600-line) length flag lands on
    `skills/coding-team/agents/ct-implementer.md`, whose node already exists in the base
    fixture's `on_demand.skill_internal_bodies` (node_key
    `on_demand:skills/coding-team/agents/ct-implementer.md`) — no `extra_files` needed to
    create the cell, only the flag to ring it. Friction decisions below name
    `rules/a.md` (a DIFFERENT, always-loaded component) so `joined` is non-empty while
    the critical node stays at zero recorded churn — the amber/friction decouple."""
    doc = _minimal_doc()
    doc["instruction_length_flags"] = [
        {"path": "skills/coding-team/agents/ct-implementer.md", "lines": 700,
         "threshold": 200, "evidence": "VERIFIED"},
    ]
    doc["duplication"]["pairs"].append(
        {"a": "rules/c.md", "b": "rules/d.md", "score": 0.75,
         "shared_sample": "more shared words here too", "evidence": "INFERRED"})
    doc["phantom_refs"].append(
        {"source": "rules/a.md", "ref": "https://example.com/missing", "kind": "external",
         "resolved": False, "evidence": "VERIFIED"})
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "note": "n"},
        {"verb": "Constrain", "surface": "memory", "verdict": "empty", "note": ""},
    ], "drag_candidates": [
        {"n": 1, "surface": "memory", "evidence": "churny", "outcome": "probation",
         "what_must_survive": "the rule text", "risk_if_wrong": "lose the guard"},
        {"n": 2, "surface": "context", "evidence": "stale",
         "outcome": "consider folding into onboarding",
         "what_must_survive": "the walkthrough", "risk_if_wrong": "new users lose orientation"},
    ]}
    return doc, synth


def test_maximal_fixture_preserves_all_hard_invariants(tmp_path):
    import re
    import base64
    import hashlib

    doc, synth = _maximal_doc_and_synth()
    out_dir = tmp_path / "maximal"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    p = _ExternalRefParser()
    p.feed(text)
    assert p.on_handlers == []            # zero inline on* handlers
    assert p.style_attrs == []            # zero inline style=
    assert p.external == []               # no external resource refs
    assert p.tag_counts.get("style") == 1                       # exactly one <style>
    exe = re.findall(r'<script(?![^>]*application/json)[^>]*>', text)
    assert len(exe) == 1                                        # exactly one executable <script>
    # CSP hashes cover the ACTUAL emitted blocks
    sty = re.search(r'<style>(.*?)</style>', text, re.S).group(1)
    exe_body = re.search(r'<script(?![^>]*application/json)[^>]*>(.*?)</script>', text, re.S).group(1)
    style_meta = re.search(r"style-src 'sha256-([A-Za-z0-9+/=]+)'", text).group(1)
    script_meta = re.search(r"script-src 'sha256-([A-Za-z0-9+/=]+)'", text).group(1)
    assert base64.b64encode(hashlib.sha256(sty.encode()).digest()).decode() == style_meta
    assert base64.b64encode(hashlib.sha256(exe_body.encode()).digest()).decode() == script_meta
    # every new surface is present
    assert "gauge-drawer" in text and "copy-preview" in text and "What to do" in text
    assert 'class="friction-components sortable"' in text
    assert 'id="brief-drag-0"' in text and 'id="brief-dragov-0"' in text
    assert "amber ring = oversize, NOT churn" in text
    # NON-REGRESSION — DIRECT assertions (not merely "present"):
    # (a) indigo/violet treemap palette (render_html.py:330 always-loaded var(--accent);
    #     :367 on-demand var(--accent-2)) — both hues emitted on real cells
    assert 'fill="var(--accent)"' in text
    assert 'fill="var(--accent-2)"' in text
    # (b) amber length-crit decouple: ring is --warn, never friction-red --crit
    assert ".length-crit-ring{fill:none;stroke:var(--warn)" in text
    assert ".length-crit-ring{fill:none;stroke:var(--crit)" not in text
    # (c) a critically-oversized ZERO-friction node stays amber AND its tooltip reads
    #     "churn: none recorded" (the amber/friction decouple). ANCHOR to the fixture's
    #     KNOWN critical node_key — matching "first zero-friction title" would grab an
    #     ordinary non-critical cell and false-fail. The length-crit cell-rect emits
    #     `data-node-key="KEY"><title>...`; the length-crit RING is self-closing (no
    #     <title>), so this regex only hits the titled cell.
    crit_key = "on_demand:skills/coding-team/agents/ct-implementer.md"  # the fixture's critical node
    m = re.search(r'data-node-key="' + re.escape(crit_key) + r'"><title>([^<]*)</title>', text)
    assert m is not None
    assert "churn: none recorded" in m.group(1)
    assert "amber ring = oversize, NOT churn" in m.group(1)


def test_fit_label_truncates_and_drops_when_no_room():
    # DIRECT _fit_label (render_html.py:1514) regression — the collision fix must hold
    assert rh._fit_label("x", 400) == "x"                        # short label fits verbatim
    long = rh._fit_label("a-very-long-basename-that-overflows.md", 60)
    assert long.endswith("…") and len(long) < len("a-very-long-basename-that-overflows.md")
    assert rh._fit_label("anything", 4) == ""                    # no room -> "" (caller omits <text>)


def test_maximal_fixture_is_byte_identical_across_pythonhashseed(tmp_path):
    """Step 3: extends the determinism net to the FULL maximal fixture (mirrors
    test_full_ia_determinism_cross_pythonhashseed's structure) — the same-seed
    determinism tests elsewhere cannot catch dict/set-ordering nondeterminism, and no
    prior determinism test carries every T1-T7 surface (length-crit ring, 2 dup pairs,
    2 phantom refs incl. external kind, 2 drag candidates, friction join) at once."""
    doc, synth = _maximal_doc_and_synth()
    out_dir = tmp_path / "maximal_det"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    outs = []
    for seed in ("0", "1"):
        p = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions),
                       env={**os.environ, "PYTHONHASHSEED": seed})
        # assert the render SUCCEEDED before reading — else a failed 2nd render leaves the
        # 1st render's file in place and the byte compare false-passes.
        assert p.returncode == 0, p.stderr
        outs.append((out_dir / "harness-map-2026-07-15.html").read_bytes())
    assert outs[0] == outs[1]
