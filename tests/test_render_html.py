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


def run_render(out_dir, *args):
    cmd = [sys.executable, str(RENDER), "--out-dir", str(out_dir)] + list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
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
                                      "registered_via": "direct", "evidence": "VERIFIED"},
                                     {"name": "orphan-script.py", "is_symlink": False, "target": None,
                                      "registered_via": "none", "evidence": "INFERRED"}],
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


# --- instrument readout (A2/AM-1) ---
def test_instrument_readout_renders_exactly_the_gauge_specs(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "gauges"; out_dir.mkdir()
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
    out_dir = tmp_path / "fgauge"; out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "friction" in text.lower()
    assert 'data-gauge="friction_total"' in text


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
    assert set(p1) == {"overview", "coverage", "weight", "friction", "hygiene"}
    assert p1["coverage"].startswith("| ")                  # markdown table
    assert "|" in p1["coverage"]


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
def test_join_decisions_ambiguous_heats_all_matches():
    node_index = {"a.md": ["always_loaded:rules/a.md", "on_demand:skills/x/rules/a.md"]}
    records = [{"date": "2026-07-01", "component": "rules/a.md + write-guard.py"}]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15")
    assert heat["always_loaded:rules/a.md"] == 1
    assert heat["on_demand:skills/x/rules/a.md"] == 1
    assert extra["segments_ambiguous"] == 1


def test_join_decisions_temporal_cutoff_excludes_future_records():
    node_index = {"a.md": ["always_loaded:rules/a.md"]}
    records = [{"date": "2026-08-01", "component": "rules/a.md"}]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15")
    assert heat == {}


def test_join_metrics_recovery_join_phases_and_agents():
    node_index = {
        "coding-team": ["on_demand:coding-team"],
        "execution.md": ["on_demand:skills/coding-team/phases/execution.md"],
        "ct-implementer.md": ["on_demand:skills/coding-team/agents/ct-implementer.md"],
    }
    records = [{"date": "2026-07-01", "phases_used": ["execute"],
                "agents_dispatched": {"builder": 2}, "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, node_index, "2026-07-15")
    assert heat["on_demand:coding-team"] == 1
    assert heat["on_demand:skills/coding-team/phases/execution.md"] == 1
    assert heat["on_demand:skills/coding-team/agents/ct-implementer.md"] == 1
    assert extra["records_eligible"] == 1


def test_join_metrics_clean_run_not_eligible():
    node_index = {"coding-team": ["on_demand:coding-team"]}
    records = [{"date": "2026-07-01", "rework_iterations": 0, "audit_rounds": 1, "findings_total": 0}]
    heat, joined, extra = rh.join_metrics(records, node_index, "2026-07-15")
    assert heat == {}
    assert extra["records_eligible"] == 0


def test_aggregate_codex_by_mode_and_verdict():
    records = [{"mode": "plan", "verdict": "REVISE", "ts": "2026-07-01T00:00:00Z", "round": 2},
               {"mode": "plan", "verdict": "SHIP", "ts": "2026-07-02T00:00:00Z"}]
    agg = rh.aggregate_codex(records, "2026-07-15")
    assert agg["runs"] == 2
    assert agg["by_mode"] == {"plan": 2}
    assert agg["by_verdict"] == {"REVISE": 1, "SHIP": 1}
    assert agg["max_revise_round"] == 2


def test_extract_basename_normalizer():
    assert rh.extract_basename("hooks/write-guard.py:check_phase5") == "write-guard.py"
    assert rh.extract_basename("write-guard.py --flag") == "write-guard.py"
    assert rh.extract_basename("coding-team") == "coding-team"


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
    assert 'class="cell-rect heatable fh1"' in text
    assert 'class="friction-badge"' in text


def test_friction_overlay_css_dims_unheated_cells_and_marks_toggle_pressed(tmp_path):
    """The friction toggle must have an UNMISTAKABLE visual effect (demo-blocker
    fix): unheated cells dim while the overlay is on, heated cells stay at full
    opacity with a bold stroke, and the toggle button itself gets a distinct
    pressed look — not just the generic aria-pressed border-color rule."""
    doc = _minimal_doc()
    out_dir = tmp_path / "friction_visibility"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "body.friction-on .heatable:not(.fh1):not(.fh2):not(.fh3):not(.fh4){opacity:0.25}" in text
    assert "body.friction-on .fh1,body.friction-on .fh2,body.friction-on .fh3,body.friction-on .fh4{opacity:1}" in text
    assert "stroke-width:4" in text
    assert '#friction-toggle[aria-pressed="true"]{background:var(--sem-empty)' in text


def test_friction_view_has_four_stream_cards(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "fcards"; out_dir.mkdir()
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
    out_dir = tmp_path / "ftotal"; out_dir.mkdir()
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
    out_dir = tmp_path / "fcomp"; out_dir.mkdir()
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
    assert 'class="friction-components"' in text          # the per-component table
    import re
    rows = re.findall(r'<tr[^>]*class="friction-component-row"[^>]*>(.*?)</tr>', text, re.S)
    assert len(rows) >= 2
    # sorted order: alpha row precedes zeta row
    a_idx = next(i for i, r in enumerate(rows) if "alpha.md" in r)
    z_idx = next(i for i, r in enumerate(rows) if "zeta.md" in r)
    assert a_idx < z_idx
    # alpha joined twice -> count 2; zeta once -> count 1
    assert ">2<" in rows[a_idx] and ">1<" in rows[z_idx]


def test_weight_view_has_treemap_and_ladder_both_prerendered(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "weight"; out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
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
    out_dir = tmp_path / "wheat"; out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    # decisions heats an always-loaded node AND coding-team (on-demand)
    decisions = out_dir / "d.jsonl"
    decisions.write_text(
        json.dumps({"date": "2026-07-01", "component": "rules/a.md"}) + "\n"
        + json.dumps({"date": "2026-07-01", "component": "coding-team"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions))
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
    out_dir = tmp_path / "tmopac"; out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
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
    assert "@media (prefers-color-scheme: light)" in text
    assert "--sem-covered" in text and "--sem-thin" in text and "--sem-empty" in text
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
    out_dir = tmp_path / "cov"; out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "evidence": "V", "note": "n"}],
        "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert text.count('class="matrix-cell') == 36
    assert text.count('class="inspector-panel"') == 36
    assert 'data-cell-id="Afford-context"' in text
    # preselect Constrain-memory: verdict token sits BETWEEN matrix-cell and sel
    # (impl class order is fixed: `matrix-cell verdict-<v> sel`, then data-cell-id).
    # Constrain-memory has no synth cell -> verdict "empty", so the class is exact:
    assert 'class="matrix-cell verdict-empty sel" data-cell-id="Constrain-memory"' in text
    # empty cells get dashed+hatch verdict class
    assert "verdict-empty" in text


def test_coverage_verdict_fill_classes(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "cov2"; out_dir.mkdir()
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
    assert re.search(r'class="matrix-cell verdict-covered[ "]', text)
    assert re.search(r'class="matrix-cell verdict-thin[ "]', text)


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


# ============================================================= 6. IA pivot: 5 views + switcher
def test_five_views_present_not_six_tabs(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "views"; out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    for vid in ("view-overview", "view-coverage", "view-weight", "view-friction", "view-hygiene"):
        assert f'id="{vid}"' in text
    p = _ExternalRefParser(); p.feed(text)
    view_btns = text.count('class="view-btn"')
    assert view_btns == 5
    # no leftover 6-tab panel ids
    assert 'id="panel-6"' not in text
    # progressive enhancement (finding #2): NO view is server-hidden — with JS off,
    # every view is visible/scrollable; the static script collapses to Overview on load.
    # Parse each of the 5 view <section> start tags; assert `hidden` absent regardless of
    # attribute order (round2: a `hidden class="view"` ordering must also fail).
    import re
    view_tags = re.findall(r'<section[^>]*\bclass="view"[^>]*>', text)
    assert len(view_tags) == 5
    for tag in view_tags:
        assert "hidden" not in tag


def test_exactly_one_executable_script(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "onescript"; out_dir.mkdir()
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
    out_dir = tmp_path / "copy"; out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    for vid in ("overview", "coverage", "weight", "friction", "hygiene"):
        assert f'<script type="application/json" id="copy-{vid}">' in text
        assert f'data-copy-target="copy-{vid}"' in text


def test_keyboard_activation_wired_for_button_cells(tmp_path):
    """WCAG 2.2 AA: role=button cells (mini-grid + matrix) must be keyboard-operable.
    The static script wires a keydown handler to [data-goto] and .matrix-cell."""
    doc = _minimal_doc()
    out_dir = tmp_path / "kbd"; out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "addEventListener('keydown'" in text
    assert "[data-goto], .matrix-cell" in text
    assert "e.preventDefault()" in text          # Space must not scroll


# ============================================================= 7. Overview digest + hero + nav
def test_overview_default_view_and_mini_grid_nav(tmp_path):
    doc = _minimal_doc()
    out_dir = tmp_path / "ov"; out_dir.mkdir()
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
    assert 'class="mini-grid"' in text
    # mini-cell navigates to coverage with a target cell id
    assert 'data-goto="view-coverage"' in text
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
    out_dir = tmp_path / "ov2"; out_dir.mkdir()
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
    assert target.read_text(encoding="utf-8") != "ORIGINAL CONTENT SHOULD SURVIVE"
