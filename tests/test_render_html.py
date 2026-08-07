"""Tests for render_html.py per docs/plans/2026-07-15-harness-map-html-viz-design.md §5.
Real fixtures only (no mocks — the renderer is pure stdlib). Reuses `run_collector`
(test_collector.py:21) and `fake_harness` (conftest.py:13)."""
import hashlib
import importlib.util
import json
import os
import pwd
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path

import pytest

from test_collector import (_build_two_tier_maximal_fixture, _collector, _SECRET_SENTINELS,
                            run_collector)

RENDER = Path(__file__).resolve().parents[1] / "render_html.py"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "report-template.md"
# Optional real-data smoke fixture. Set HARNESS_MAP_REAL_SAMPLE to a collector sidecar
# JSON to enable the two real-data smoke tests; they skip when it is unset or missing.
# No absolute literal here on purpose -- this repo is public (see
# test_no_absolute_home_literal_in_runtime_modules).
_real_sample_env = os.environ.get("HARNESS_MAP_REAL_SAMPLE", "")
REAL_SAMPLE = Path(_real_sample_env) if _real_sample_env else Path("/nonexistent/harness-map-real-sample.json")

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


def _slug(path):
    """Independent re-derivation of the CC per-project memory dir name, mirroring
    test_collector.py::_active_slug's precedent. Deliberately a SECOND implementation:
    importing render_html's own helper would make every slug assertion circular.
    # Changing this rule requires a spec change (S6 §4.1)."""
    return re.sub(r"[/.]", "-", os.path.abspath(str(path)))


def _default_streams_under_home(home, root=None):
    """`default_streams()` evaluated in a SUBPROCESS with its own $HOME, returned as
    {name: str-or-None}.

    Why a subprocess: the module-level $HOME fixture is SESSION-SCOPED and SHARED. A test
    that created ~/.claude/projects/<slug>/memory/ inside it would make
    test_default_streams_keys_and_paths see the directory and break through test-ordering
    pollution. Its own $HOME is the only hermetic form. env is {**os.environ, "HOME": ...}
    — a bare {"HOME": ...} would strip PATH/PYTHONHASHSEED from the child."""
    code = (
        "import importlib.util, json, sys\n"
        "spec = importlib.util.spec_from_file_location('rh', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "root = sys.argv[2] or None\n"
        "out = {k: (None if v is None else str(v)) for k, v in m.default_streams(root).items()}\n"
        "sys.stdout.write(json.dumps(out))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code, str(RENDER), "" if root is None else str(root)],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "HOME": str(home)},
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


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


def test_find_sidecars_ignores_impossible_calendar_dates(tmp_path):
    # F8 (TRK-051): SIDECAR_RE is structural only (\d{4}-\d{2}-\d{2}), so each of these
    # filenames matches it despite naming no real calendar date -- including a Feb 29 in
    # 2026, which is NOT a leap year. None may be returned.
    for bad_date in ("2026-02-31", "2026-13-01", "2026-00-10", "2026-02-29"):
        _write_sidecar(tmp_path, bad_date, {"schema_version": 1})
    assert rh.find_sidecars(tmp_path) == []

def test_find_sidecars_keeps_real_dates_alongside_impossible_ones(tmp_path):
    # A real prior sidecar's discovery is unaffected by an impossible sibling sitting
    # right next to it -- including a Feb 29 in 2024, which IS a leap year and real.
    _write_sidecar(tmp_path, "2026-02-10", {"schema_version": 1})
    _write_sidecar(tmp_path, "2026-02-31", {"schema_version": 1})    # impossible, adjacent
    _write_sidecar(tmp_path, "2024-02-29", {"schema_version": 1})    # real leap day
    found = rh.find_sidecars(tmp_path)
    assert [d for d, _ in found] == ["2024-02-29", "2026-02-10"]     # ascending; bogus excluded

def test_find_sidecars_impossible_newest_sort_does_not_win(tmp_path):
    # The ordering case: find_sidecars' own contract is "sorted ascending by date", so its
    # LAST entry is whatever a caller relying on "the latest sidecar" would pick.
    # "2026-02-31" sorts lexically newer than every real February date and must not win
    # that slot.
    _write_sidecar(tmp_path, "2026-02-10", {"schema_version": 1})
    _write_sidecar(tmp_path, "2026-02-20", {"schema_version": 1})
    _write_sidecar(tmp_path, "2026-02-31", {"schema_version": 1})    # sorts newest, impossible
    found = rh.find_sidecars(tmp_path)
    assert found[-1][0] == "2026-02-20"    # the true newest REAL date, not the bogus one


# S6c Task 3: the sentinel a caller passes to DROP a comparability marker from a
# `_trend_doc`. `None` cannot serve — `definitions=None` already means "the standard
# comparable marker set", and `project_root=None` is a REAL scope value the collector
# emits (a null scope is a distinct scope, never "same as whatever ran last"). Asking for
# the markerless path is therefore always explicit.
_NO_MARKERS = object()


def _trend_doc(scope_root="/fake/root", project_root=None, compose=False,
               definitions=None, promotion=1, memory_bodies=1, phantom=(1, 1),
               hooks=(0, 20), skills=(0, 20), **kw):
    """A `_minimal_doc` carrying the S6c comparability markers AND variable derived
    values. Returns a PLAIN DICT the caller may freely mutate before writing it.

    Two independent reasons a bare `_minimal_doc` is unusable for a verdict fixture:

    1. It is MARKERLESS -- no `collection_scope`, no `metric_definitions` -- which
       correctly reads as UNKNOWN -> `not comparable` (S6 §6.5a / §8.1). A test built on
       it asserts `improving` and gets `not comparable`, for a reason that has nothing
       to do with the classifier.
    2. It FIXES every derived value: promotion_candidates at 1, memory_bodies at 1,
       phantom_refs at 1, and test_coverage.summary all-zero -- so BOTH ratio series
       drop every point (`total` of 0 => point dropped) and the four count series are
       flat by construction. A trend fixture built on it cannot move.

    THE CONTRACT IN ONE LINE: `_trend_doc()` with no arguments produces a point that is
    comparable on all three axes and MEASURABLE on all six derived series. Every refusal
    a test wants is requested explicitly:

      * markerless scope      -> `scope_root=_NO_MARKERS`
      * markerless definitions-> `definitions=_NO_MARKERS`
      * a scope transition    -> a different `scope_root`/`project_root`/`compose`
      * a dropped ratio point -> `hooks=(0, 0)` / `skills=(0, 0)` (total 0 => no ratio)

    `hooks`/`skills` therefore default to a NON-ZERO denominator. A zero one would
    reproduce limitation 2 inside the helper written to remove it: both ratio series
    would drop every point, `_trend_doc()` would yield four drawable derived series
    instead of six, and the tempting repair is a pinned literal 4.

    `**kw` forwards to `_minimal_doc` (extra_files, extra_promotion, tokens_a, tokens_b).
    Owned by Task 3; used by Tasks 4, 5, 7, 8 and 13."""
    doc = _minimal_doc(**kw)
    if scope_root is not _NO_MARKERS:
        doc["collection_scope"] = {"root": scope_root, "project_root": project_root,
                                   "compose": compose}
    if definitions is _NO_MARKERS:
        doc.pop("metric_definitions", None)
    else:
        doc["metric_definitions"] = dict(_collector.METRIC_DEFINITIONS
                                         if definitions is None else definitions)
    # Axis 3: every metric fully measured. `complete` is the collector's own word for it
    # (collector._metric_quality), not a synonym invented here.
    doc["metric_quality"] = {k: "complete" for k in sorted(_collector.METRIC_DEFINITIONS)}
    doc["promotion_candidates"] = [
        {"source": f"rules/p{i}.md", "pattern": "NEVER", "excerpt": "NEVER do the thing",
         "hook_covered": False, "evidence": "INFERRED"}
        for i in range(promotion)]
    doc["on_demand"]["memory_bodies"] = [
        {"path": f"projects/x/memory/note{i}.md", "project_slug": "x", "lines": 5,
         "words": 40, "evidence": "VERIFIED"}
        for i in range(memory_bodies)]
    total_refs, confirmed_refs = phantom
    doc["phantom_refs"] = [
        {"source": f"rules/r{i}.md", "ref": f"ghost{i}.md", "kind": "path",
         "resolved": False if i < confirmed_refs else None,
         "evidence": "VERIFIED" if i < confirmed_refs else "INFERRED"}
        for i in range(total_refs)]
    doc["test_coverage"]["summary"] = {
        "hooks_with_test": hooks[0], "hooks_total": hooks[1],
        "skills_with_test": skills[0], "skills_total": skills[1]}
    return doc


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


def test_trend_delta_nonnumeric_headline_value_does_not_crash(tmp_path):
    """A17: a corrupt/hostile OLDER sidecar carrying a non-numeric value on a
    gauge-linked headline key (e.g. `always_loaded_words`) must not crash the whole
    render — the `cur > prev` comparison in `_trend_delta` used to raise
    TypeError('>' not supported between instances of 'str' and 'int'). It must
    degrade to no-delta for that gauge. (The corrupt value lives on the OLDER
    sidecar, not the selected/current one, so this isolates the `_trend_delta`
    comparison guard from the unrelated `_gauge_band` current-value path. That path
    USED TO crash on a non-numeric value too, before Control 2 -- it now returns
    `("", "neutral")` for `_gauge_band("always_loaded_words", "corrupt")` instead
    of raising, so this test's isolation is no longer covering a live crash there,
    just a separate code path.)"""
    doc1 = _minimal_doc()
    doc1["headline"]["always_loaded_words"] = "</script>corrupt"  # hostile, non-numeric
    doc2 = _minimal_doc()
    out_dir = tmp_path / "a17"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", doc1)
    _write_sidecar(out_dir, "2026-07-15", doc2)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "</script>corrupt" not in text  # never emitted raw

    trend_model = rh.build_trend_model([("2026-07-14", doc1), ("2026-07-15", doc2)])
    assert rh._trend_delta(trend_model, "always_loaded_words") is None


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


def test_gauge_drilldown_corrupt_words_renders_int_zero_not_float(tmp_path):
    """T2 spec-review LOW: a row with a VALID `tokens_est` but a CORRUPT `words` value
    survives `_tokens_treemap`'s `unrenderable` gate (which only keys on `tokens_est`)
    and must still present its gated-but-unusable `words` as the integer "0", not the
    float "0.0" -- the exact "100 tokens -> 100.0 tokens" symptom `_gated_size` exists
    to prevent, surviving on its least-travelled (rejection) branch."""
    import re

    extra_files = [{"path": "rules/corrupt-words.md", "category": "rule", "words": "not-a-number",
                    "lines": 3, "tokens_est": 77, "evidence": "VERIFIED"}]
    doc = _minimal_doc(extra_files=extra_files)
    out_dir = tmp_path / "gdrill-corrupt-words"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    words_panel = re.search(r'id="gdrawer-always_loaded_words"[^>]*>(.*?)</div>', text, re.S).group(1)
    corrupt_line = re.search(r'<code>rules/corrupt-words\.md</code>[^<]*', words_panel).group(0)
    assert corrupt_line.endswith(" — 0")
    assert "0.0" not in corrupt_line


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


def test_join_decisions_date_provenance_counters_on_own_return():
    """QA P3 — the date-provenance counters (`_accumulate_date` / `_new_date_counters`,
    shared by all three joined streams) are asserted only on `join_interventions`'s
    return; `test_join_decisions_temporal_cutoff_excludes_future_records` above checks
    only `heat == {}`, which a resolver bug could also produce with the wrong counter
    values. Assert `join_decisions`'s OWN counters directly, so a future decisions-only
    specialization that bypasses the shared helper cannot silently lose them."""
    node_index = {"a.md": ["always_loaded:rules/a.md"]}
    records = [
        {"date": "2026-08-01", "component": "rules/a.md"},   # future — skipped
        {"date": "2026-07-01", "component": "rules/a.md"},   # dated, in the past
    ]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15")
    assert extra["records_skipped_future"] == 1
    assert extra["records_dated_as_of"] == 1


def test_new_date_counters_ordered_key_set_is_pinned():
    """TRK-027 — `_new_date_counters`'s docstring declares the insertion ORDER
    load-bearing ("so the rendered raw counters are byte-deterministic across
    PYTHONHASHSEED"), and `_accumulate_date` separately declares the five NAMES
    load-bearing. A bare set-equality assertion (`set(...) == {...}`) would let a silent
    reordering through and pins neither contract — this asserts the ORDERED LIST, which
    covers both in one assertion: a renamed key changes the list's contents, and a
    reordered key changes the list's sequence, either of which fails this test.
    # Changing this value requires a spec change (S6 §4.3, finding #12)."""
    assert list(rh._new_date_counters().keys()) == [
        "records_dated_as_of",
        "records_undated",
        "records_invalid_date",
        "records_conflicting_date",
        "records_skipped_future",
    ]


def test_join_decisions_undated_invalid_and_conflicting_counters():
    """QA P3 — mirrors `test_interventions_counts_undated_invalid_and_conflicting`
    (T2.2/T2.3/T2.4) for the decisions join, whose date-provenance counters had no
    direct coverage at all before this test."""
    node_index = {"a.md": ["always_loaded:rules/a.md"]}
    records = [
        {"component": "rules/a.md"},                                       # undated
        {"date": "2026-13-45", "component": "rules/a.md"},                 # invalid calendar
        {"date": "2026-07-01", "timestamp": "2026-07-14T00:00:00",
         "component": "rules/a.md"},                                       # conflicting
    ]
    heat, joined, extra = rh.join_decisions(records, node_index, "2026-07-15")
    assert extra["records_undated"] == 1
    assert extra["records_invalid_date"] == 1
    assert extra["records_conflicting_date"] == 1


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


def test_join_metrics_date_provenance_counters_on_own_return():
    """QA P3 — the date-provenance counters are asserted only on `join_interventions`'s
    return; `join_metrics` had no direct coverage at all. `_accumulate_date` runs BEFORE
    the eligibility check (line order in `join_metrics`), so an ineligible record still
    contributes to date provenance -- covered here via the eligible/future-skip pair."""
    records = [
        {"date": "2026-08-01", "rework_iterations": 1},   # future — skipped
        {"date": "2026-07-01", "rework_iterations": 1},   # dated, in the past
    ]
    heat, joined, extra = rh.join_metrics(records, {}, "2026-07-15")
    assert extra["records_skipped_future"] == 1
    assert extra["records_dated_as_of"] == 1


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


def test_join_metrics_bad_phases_shape_counted_agent_heat_still_joins():
    """Post-exec Codex finding, S6a: `phases_used` present but not a list (the real witness
    at `harness-metrics.jsonl:33` carried an int) is silently skipped by the phase join --
    `records_invalid_shape` discloses that loss instead of letting it vanish. The agents
    half, being well-formed, still attributes normally: one malformed field must not
    suppress the other field's real heat."""
    node_index = {
        "execution.md": ["on_demand:skills/coding-team/phases/execution.md"],
        "ct-implementer.md": ["on_demand:skills/coding-team/agents/ct-implementer.md"],
    }
    records = [{"date": "2026-07-01", "phases_used": 6,
                "agents_dispatched": {"builder": 2}, "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, node_index, "2026-07-15")
    assert heat == {"on_demand:skills/coding-team/agents/ct-implementer.md": 1}
    assert extra["records_invalid_shape"] == 1


def test_join_metrics_bad_agents_shape_counted():
    """`agents_dispatched` present but not a dict is the mirror-image contract violation."""
    node_index = {"execution.md": ["on_demand:skills/coding-team/phases/execution.md"]}
    records = [{"date": "2026-07-01", "phases_used": ["execute"],
                "agents_dispatched": ["builder"], "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, node_index, "2026-07-15")
    assert heat == {"on_demand:skills/coding-team/phases/execution.md": 1}
    assert extra["records_invalid_shape"] == 1


def test_join_metrics_both_fields_malformed_counted_once():
    """A record must count once toward `records_invalid_shape` even when BOTH attribution
    fields are malformed -- the disclosure is "how many records lost attribution", not "how
    many fields were malformed"."""
    records = [{"date": "2026-07-01", "phases_used": 6,
                "agents_dispatched": "builder", "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, {}, "2026-07-15")
    assert heat == {}
    assert extra["records_invalid_shape"] == 1


def test_join_metrics_absent_attribution_fields_not_counted_as_invalid_shape():
    """An ABSENT field is a legitimate older record shape, never a contract violation --
    without this test the counter could silently become a "records missing optional
    fields" tally instead of a shape-violation tally."""
    records = [{"date": "2026-07-01", "rework_iterations": 1}]
    heat, joined, extra = rh.join_metrics(records, {}, "2026-07-15")
    assert extra["records_invalid_shape"] == 0
    assert extra["records_aggregate_only"] == 1


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


def _css_decls(stylesheet, selector):
    """Return the declaration block for an exact `selector` from a CSS string, or ''."""
    marker = selector + "{"
    i = stylesheet.find(marker)
    if i == -1:
        return ""
    return stylesheet[i + len(marker):stylesheet.index("}", i)]


def test_gauge_button_content_is_top_anchored(tmp_path):
    """Rejects the pre-TRK-021 behavior: a drill-enabled gauge renders as a bare <button>,
    and a <button> inherits the UA stylesheet's vertically CENTERED anonymous content box.
    `.gauges` is display:flex with the default align-items:stretch, so every card is as tall
    as the tallest -- which made a short drill button float its value and label at the
    vertical middle while its taller siblings started at the top. `text-align:left` on
    button.gauge fixed the horizontal axis only. The button must therefore declare its own
    column flex box anchored with justify-content:flex-start, and `.gauge-chev` must use
    align-self -- `float` is inert on a flex item and would drop the chevron to the left."""
    doc = _minimal_doc()
    out_dir = tmp_path / "gauge_align"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # Guard: the fixture must actually produce a <button> gauge, or the rule is untested.
    assert '<button class="gauge' in text
    decls = _css_decls(rh.STATIC_STYLE, "button.gauge")
    assert "display:flex" in decls
    assert "flex-direction:column" in decls
    assert "justify-content:flex-start" in decls
    chev = _css_decls(rh.STATIC_STYLE, ".gauge-chev")
    assert "align-self:flex-end" in chev
    assert "float" not in chev


def test_hook_cards_share_one_spaced_list_style(tmp_path):
    """Rejects two pre-TRK-021 behaviors. (1) The three bipartite hook cards emitted bare
    unclassed <ul>s, so every entry inherited UA defaults and the "Scripts on disk" card --
    where each <li> stacks a name, a badge and a description -- rendered as an unreadable
    wall. (2) The naive fix, `.card ul`, would also hit `.digest-group ul` and
    `.tier-dark-callout ul`, which tie on specificity and win only by source order.
    Also pins that `.hook-list li` sets no padding: it out-ranks `.badge`, and a padding
    declaration here would silently flatten every orphan-registration pill."""
    doc = _minimal_doc()
    out_dir = tmp_path / "hook_lists"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    for heading in ("Registered hooks (settings.json)",
                    "Orphan registrations",
                    "Scripts on disk (registration/reachability status)"):
        assert f"<h2>{heading}</h2><ul class=\"hook-list\">" in text
    li = _css_decls(rh.STATIC_STYLE, ".hook-list li")
    assert "margin:" in li and "line-height:" in li
    assert "padding" not in li
    assert _css_decls(rh.STATIC_STYLE, ".hook-list") != ""


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


def test_metrics_sentence_discloses_shape_malformed_records(tmp_path):
    """The disclosure clause names the record that lost attribution to a bad field shape,
    not just the field type -- the shape of the real witness at
    `harness-metrics.jsonl:33` (`"phases_used": 6`, an int where the contract wants a
    list)."""
    text = _one_stream_render(tmp_path, "shapebad", "--metrics-file", [
        {"date": "2026-07-01", "rework_iterations": 1, "phases_used": 6,
         "agents_dispatched": {"builder": 1}},
    ])
    assert "1 of 1 records malformed (phase/agent attribution incomplete)" in text


def test_metrics_sentence_omits_malformed_clause_when_all_well_formed(tmp_path):
    """The clause is conditional, not always-on boilerplate: an all-well-formed stream
    must render neither the counter nor the words."""
    text = _one_stream_render(tmp_path, "shapegood", "--metrics-file", [
        {"date": "2026-07-01", "rework_iterations": 1, "phases_used": ["execute"],
         "agents_dispatched": {"builder": 1}},
    ])
    assert "records malformed" not in text


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


def test_sortable_numeric_column_strips_lower_bound_marker_before_parsing(tmp_path):
    """Post-exec Codex finding #1 (S6a). Task 5's `_lb` renders a truncated numeric cell as
    "≥N" (`_render_component_friction_table`'s "Friction records" column, e.g. "≥12").
    The embedded sorter's numeric key was `parseFloat(raw)` -- `parseFloat("≥12")` is `NaN`,
    and the existing `isNaN -> -Infinity` fallback (kept below for a genuinely non-numeric
    cell, e.g. a not-measured placeholder) then sorted EVERY row of a truncated report's
    column into the same bucket, silently breaking the sort on the one report where a
    reader needs it most.

    STDLIB-ONLY LIMITATION (binding rule 9: no JS engine in this suite). This test cannot
    execute the sorter and observe row order -- it can only assert the emitted JS source
    strips the "≥" marker before `parseFloat`. It does NOT prove a browser actually
    reorders truncated rows correctly; a typo inside the regex, or a JS engine whose
    `String.replace` behaves unexpectedly, would still pass this test. That gap is real and
    is not covered anywhere else in this suite."""
    assert "parseFloat(raw.replace(/^≥/, ''))" in rh.STATIC_SCRIPT
    # the non-numeric fallback must still catch a genuinely non-numeric cell -- the strip
    # must not broaden into "any cell sorts last"
    assert "if (type === 'num' && isNaN(key)) { key = -Infinity; }" in rh.STATIC_SCRIPT


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


def test_coverage_cell_note_is_open_at_first_paint(tmp_path):
    """Rejects the pre-TRK-021 behavior where a cell note rendered inside a CLOSED <details>:
    the operator had to click the cell and then click again to read the one sentence the
    synthesis wrote about it. `open` makes it visible at first paint. Deliberately keeps the
    <details> wrapper -- tests/test_render_html.py:1855 pins the closing bytes and removing
    the wrapper would edit an existing assertion (CLAUDE.md rule 7)."""
    doc = _minimal_doc()
    out_dir = tmp_path / "cell_note_open"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    synth = {"schema_version": 1, "civc": [
        {"verb": "Afford", "surface": "context", "verdict": "covered", "note": "context note here"},
    ], "drag_candidates": []}
    (out_dir / "harness-synthesis-2026-07-15.json").write_text(json.dumps(synth))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert '<details class="cell-note" open><summary>note</summary>context note here</details>' in text
    # The legend must not still claim the note is hidden behind a toggle.
    assert "expose it via a details toggle" not in text


def test_copy_brief_disclosure_stays_collapsed(tmp_path):
    """Rejects a blanket `open` sweep: the copy-brief disclosure (_render_copy_disclosure /
    .copy-preview) must stay CLOSED. Its body is a full markdown brief, and opening every one
    of them would bury the inspector's actual content under raw payload text."""
    doc = _minimal_doc()
    out_dir = tmp_path / "brief_closed"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert '<details class="copy-preview">' in text
    assert '<details class="copy-preview" open' not in text
    assert '<details open class="copy-preview"' not in text


def test_expand_all_is_a_toggle_with_visible_pressed_state(tmp_path):
    """Rejects the pre-TRK-021 handler, which was wired but gave zero feedback: it set
    `v.hidden = false` on every view and stopped. Nothing scrolled, the button state never
    changed, the tab bar still advertised one aria-selected tab while four panels were open,
    and there was no second click to undo it. The control must carry aria-pressed, the
    handler must branch on it, and activate() must clear both the pressed state and the
    body class so choosing a tab exits the mode instead of leaving it stale."""
    doc = _minimal_doc()
    out_dir = tmp_path / "expand_toggle"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="expand-all" type="button" aria-pressed="false"' in text
    js = rh.STATIC_SCRIPT
    assert "expand.getAttribute('aria-pressed') === 'true'" in js
    assert "expand.setAttribute('aria-pressed', 'true')" in js
    assert "classList.add('expand-all-on')" in js
    assert "classList.remove('expand-all-on')" in js
    # activate() owns the cleanup, so a tab click cannot leave a stale pressed state.
    activate_src = js[js.index("function activate(id){"):js.index("vbtns.forEach(function(b){ b.addEventListener")]
    assert "aria-pressed', 'false'" in activate_src
    assert "classList.remove('expand-all-on')" in activate_src
    # The lookup must precede activate(), not depend on `var` hoisting.
    assert js.index("var expand = document.getElementById('expand-all')") < js.index("function activate(id){")


def test_each_view_gets_a_heading_before_its_section(tmp_path):
    """Rejects expanding into an undifferentiated wall: with the tab bar deselected, four
    stacked panels carried no visible label at all, so the user could not see that anything
    had happened. Each heading must sit OUTSIDE its <section>, immediately before it, so the
    existing `<section id="view-...">.*?</section>` regression regexes are unaffected."""
    doc = _minimal_doc()
    out_dir = tmp_path / "view_headings"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    for vid, label in rh.VIEWS:
        heading = f'<h2 class="view-heading" data-for="{vid}">{label}</h2>'
        assert heading in text
        assert text.index(heading) < text.index(f'<section id="{vid}"')


def test_view_headings_are_hidden_until_expanded(tmp_path):
    """Rejects the inverse defect of the test above: four always-visible headings would
    duplicate the tab label on every ordinary single-view page. They appear only under
    `body.expand-all-on` -- and under @media print, so the control's own name stays true
    for a reader who prints without clicking it."""
    doc = _minimal_doc()
    out_dir = tmp_path / "heading_hidden"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    css = rh.STATIC_STYLE
    assert "display:none" in _css_decls(css, ".view-heading")
    assert "display:block" in _css_decls(css, "body.expand-all-on .view-heading")
    assert "@media print{" in css and ".view[hidden]{display:block}" in css


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


def test_write_html_safely_accepts_list_guard_roots_and_rejects_second_root(tmp_path):
    """P1-B (render_html half): `write_html_safely`'s `guard_roots` param must accept
    MULTIPLE roots (a list) and reject a target resolving inside ANY of them -- not
    just the first. Direct unit-level call, bypassing main()'s own write path entirely,
    so this pins write_html_safely's OWN independent multi-root guarantee."""
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    bad_target = proj / "leak" / "harness-map-2026-07-15.html"
    with pytest.raises(rh.RenderError):
        rh.write_html_safely(bad_target, "<html></html>", [operator_root, proj])
    assert not bad_target.exists()


def test_write_html_safely_still_rejects_single_operator_root(tmp_path):
    """Regression pin: a bare single root (str/Path, not a list) must still work --
    every non-compose call site passes exactly this shape."""
    operator_root = tmp_path / "fakeclaude"
    operator_root.mkdir()
    bad_target = operator_root / "reports" / "harness-map-2026-07-15.html"
    with pytest.raises(rh.RenderError):
        rh.write_html_safely(bad_target, "<html></html>", operator_root)
    assert not bad_target.exists()


def test_write_html_safely_writes_through_when_outside_every_guard_root(tmp_path):
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    target = out_dir / "harness-map-2026-07-15.html"
    rh.write_html_safely(target, "<html>ok</html>", [operator_root, proj])
    assert target.read_text(encoding="utf-8") == "<html>ok</html>"


def test_write_html_safely_rejects_symlinked_out_dir_retargeted_into_project_root(tmp_path):
    """The realistic production shape (T8 P1-B): `--out-dir` is a symlink, safe at some
    earlier check, later retargeted to point inside a guarded root -- the actual write
    target is only known by re-resolving at write time, which write_html_safely now
    always does."""
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    safe_dir = tmp_path / "safe_out"
    safe_dir.mkdir()
    out_link = tmp_path / "out_link"
    out_link.symlink_to(safe_dir)
    target = out_link / "harness-map-2026-07-15.html"
    # retarget AFTER the symlink was created safely, mirroring a swapped --out-dir
    out_link.unlink()
    out_link.symlink_to(proj)
    with pytest.raises(rh.RenderError):
        rh.write_html_safely(target, "<html></html>", [operator_root, proj])
    assert not (proj / "harness-map-2026-07-15.html").exists()


# NAME IS STALE, BEHAVIOR IS NOT. `write_html_safely` no longer calls `mkstemp`; it delegates
# to `collector.write_text_contained`, and `tempfile` is not imported in `render_html.py` at all.
# The re-check this test pins still happens -- it just guards the fd-pinned open rather than a
# `mkstemp` call. The name and docstring below are left verbatim on purpose: renaming them would
# be the first net deletion in this file against `main`, and that clean numstat is load-bearing
# evidence that no pre-existing assertion was touched. Read "mkstemp" here as "the write primitive".
def test_write_html_safely_recheck_immediately_before_mkstemp_closes_toctou_window(
        tmp_path, monkeypatch):  # mock-ok: interposes on real fs symlink timing, not a faked dependency
    """P3 hardening (parity with collector.main's own pre-mkstemp re-check, Codex
    challenge): validate_write_target's PARENT DIRECTORY itself -- not the write target's
    own name -- gets swapped into a symlink pointing at a guard root, in the window
    AFTER the top-of-function validation resolved `out_path` to a concrete (symlink-free)
    path but BEFORE `mkstemp` opens it. `out_path` itself never changes; re-resolving the
    SAME already-resolved path a second time is what must catch a parent component that
    turned into a symlink in that window. Interposes on the real `validate_write_target`
    call COUNT to flip a REAL filesystem symlink between call 1 (safe) and call 2
    (unsafe), deterministically forcing the exact interleaving a genuine race would only
    sometimes hit -- every call still goes through the real guard logic, no return value
    or side effect is faked."""
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    safe_dir = tmp_path / "safe_out"
    safe_dir.mkdir()
    target = safe_dir / "harness-map-2026-07-15.html"

    collector_mod = rh._get_sibling_collector()
    real_validate = collector_mod.validate_write_target
    calls = {"n": 0}

    def _swap_parent_dir_after_first_call(raw_path, roots, input_paths=()):
        calls["n"] += 1
        result = real_validate(raw_path, roots, input_paths)
        if calls["n"] == 1:
            # `safe_dir` is empty (nothing written through it yet) -- swap it itself
            # into a symlink pointing INTO the guarded project root, simulating a
            # parent-directory retarget landing exactly in the validate-to-mkstemp gap.
            safe_dir.rmdir()
            safe_dir.symlink_to(proj)
        return result

    monkeypatch.setattr(collector_mod, "validate_write_target", _swap_parent_dir_after_first_call)  # mock-ok: interposes on real fs symlink timing, not a faked dependency
    with pytest.raises(rh.RenderError):
        rh.write_html_safely(target, "<html></html>", [operator_root, proj])
    assert calls["n"] == 2, "both the top-of-function and the pre-mkstemp re-check must run"
    assert not any(proj.glob("*.html")), \
        "a parent-dir symlink swapped in between validate and mkstemp must never be written through"


def test_write_html_safely_preserves_hardlinked_inode(tmp_path):
    guard = tmp_path / "guarded"
    guard.mkdir()
    protected = guard / "protected.md"
    protected.write_text("ORIGINAL", encoding="utf-8")
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    target = out_dir / "r.html"
    os.link(protected, target)

    rh.write_html_safely(target, "<html></html>", [guard])

    assert protected.read_text(encoding="utf-8") == "ORIGINAL"
    assert target.read_text(encoding="utf-8") == "<html></html>"


def test_write_html_safely_routes_through_the_shared_helper():
    """Structural: the renderer must not carry its own write mechanics. Pairs with
    Task 7's parity guard — this one names the specific call."""
    import inspect
    source = inspect.getsource(rh.write_html_safely)
    assert "write_text_contained" in source
    assert "mkstemp" not in source


def test_write_html_safely_still_backslashreplaces_lone_surrogates(tmp_path):
    """The encoding error mode is NOT security logic and must survive the refactor: a
    lone UTF-16 surrogate must still produce a COMPLETE file, never abort the write."""
    guard = tmp_path / "guarded"
    guard.mkdir()
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    target = out_dir / "r.html"

    rh.write_html_safely(target, "before \ud800 after", [guard])

    written = target.read_text(encoding="utf-8")
    assert "before" in written and "after" in written
    assert "\\ud800" in written


def test_write_html_safely_still_raises_render_error_inside_guard_root(tmp_path):
    guard = tmp_path / "guarded"
    (guard / "nested").mkdir(parents=True)
    with pytest.raises(rh.RenderError):
        rh.write_html_safely(guard / "nested" / "r.html", "<html></html>", [guard])
    assert not (guard / "nested" / "r.html").exists()


def test_write_html_safely_falls_back_when_dir_fd_unsupported(
        tmp_path, monkeypatch):  # mock-ok: interposes on real fs capability detection, not a faked dependency
    monkeypatch.setattr(os, "supports_dir_fd", frozenset())
    guard = tmp_path / "guarded"
    guard.mkdir()
    out_dir = tmp_path / "reports"
    out_dir.mkdir()
    target = out_dir / "r.html"

    rh.write_html_safely(target, "<html></html>", [guard])

    assert target.read_text(encoding="utf-8") == "<html></html>"
    assert list(out_dir.glob("*.tmp")) == []


def test_render_out_path_symlinked_into_project_root_rejected_at_write_time(tmp_path):
    """P1-B sink 3 (render_html.main): the html OUTPUT PATH itself resolving into the
    composed project root via a pre-existing symlink must be rejected -- write_html_safely
    is main()'s write-time guard, independent of whatever earlier check ran."""
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(operator_root)
    doc["inspected_roots"] = {"operator": str(operator_root.resolve()),
                              "project_containment": str(proj.resolve()),
                              "project_harness": str((proj / ".claude").resolve())}
    _write_sidecar(out_dir, "2026-07-15", doc)
    leak_target = proj / "leak3.html"
    (out_dir / "harness-map-2026-07-15.html").symlink_to(leak_target)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode != 0
    assert not leak_target.exists()


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


# ============================================================= S2.M5 trend sparklines
def test_sparkline_renders_with_three_sidecars(tmp_path):
    """≥3 dated sidecars (SPEC_4 §3 gate): a sparkline appears for each of the 8
    headline series."""
    out_dir = tmp_path / "spark3"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-13", _minimal_doc(tokens_a=100, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-14", _minimal_doc(tokens_a=150, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc(tokens_a=200, tokens_b=50))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert text.count('class="sparkline"') == len(rh.HEADLINE_KEYS)


def test_sparkline_windows_to_last_ten(tmp_path):
    """12 dated sidecars -> one series' polyline has EXACTLY 10 coordinate pairs.
    Changing N=10 requires a spec change (SPEC_4 §3)."""
    out_dir = tmp_path / "spark12"
    out_dir.mkdir()
    for i in range(12):
        _write_sidecar(out_dir, f"2026-07-{i + 1:02d}", _minimal_doc(tokens_a=100 + i, tokens_b=50))
    proc = run_render(out_dir, "--date", "2026-07-12", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-12.html").read_text(encoding="utf-8")
    import re
    m = re.search(r'id="spark-always_loaded_tokens_est"[^>]*>.*?<polyline points="([^"]*)"', text, re.S)
    assert m is not None
    assert len(m.group(1).split(" ")) == rh.SPARKLINE_WINDOW == 10


def test_sparkline_window_pins_the_LAST_ten_values_not_the_first(tmp_path):
    """Codex #9: the sibling count-only assertion cannot distinguish
    values[-10:] from values[:10] -- ten evenly-spaced ascending points normalize to
    identical polyline geometry. The min/max/cur stats carry the ABSOLUTE values.

    Fixture: 12 sidecars, tokens_a = 100+i, tokens_b = 50 -> always_loaded_tokens_est
    series [150..161].  last-10 = [152..161];  first-10 (the bug) = [150..159].
    Both strings measured against live code."""
    out_dir = tmp_path / "spark12values"
    out_dir.mkdir()
    for i in range(12):
        _write_sidecar(out_dir, f"2026-07-{i + 1:02d}",
                       _minimal_doc(tokens_a=100 + i, tokens_b=50))
    proc = run_render(out_dir, "--date", "2026-07-12", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-12.html").read_text(encoding="utf-8")
    m = re.search(
        r'id="spark-always_loaded_tokens_est".*?</svg>'
        r'<span class="sparkline-stats">(.*?)</span>', text, re.S)
    assert m is not None
    assert m.group(1) == "min 152.00 · max 161.00 · cur 161.00"
    assert "min 150.00" not in m.group(1)   # the values[:10] bug shape


def test_sparkline_flat_series_renders_at_the_bottom_not_mid_height():
    """Codex #8: pins the ACTUAL geometry the corrected comment now describes."""
    svg = rh._sparkline_svg("spark-x", [5.0, 5.0, 5.0])
    m = re.search(r'points="([^"]*)"', svg)
    assert m is not None
    assert all(p.split(",")[1] == "22.00" for p in m.group(1).split(" "))


def test_sparkline_absent_below_three_sidecars(tmp_path):
    """Exactly 2 dated sidecars: no sparkline renders, but the existing Metric×dates
    table presentation is unchanged and still present."""
    out_dir = tmp_path / "spark2"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", _minimal_doc(tokens_a=100, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc(tokens_a=200, tokens_b=50))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'class="sparkline"' not in text
    assert "<th>Metric</th>" in text
    assert "Always-loaded tokens (est)" in text


def test_sparkline_dates_and_values_are_escaped(tmp_path):
    """A hostile date/headline-value pair must never land unescaped in the sparkline
    presentation — the sidecar filename regex constrains dates, so we inject the
    hostile string via a headline value instead (same class of hostile-payload as
    the XSS_PAYLOADS convention, tests/test_render_html.py:694)."""
    payload = '</script><img onerror=alert(1)>'
    doc1 = _minimal_doc(tokens_a=100, tokens_b=50)
    doc2 = _minimal_doc(tokens_a=150, tokens_b=50)
    doc3 = _minimal_doc(tokens_a=200, tokens_b=50)
    # `orphan_registration_count` is a headline series NOT read by `_trend_delta`'s
    # gauge-card path (GAUGE_SPECS only wires 5 of the 8 headline keys there) — this
    # isolates the assertion to the sparkline's own escaping/robustness, not a
    # pre-existing `_trend_delta` numeric-comparison gap on a gauge-linked key
    # (out-of-scope for S2.M5 — reported separately).
    doc3["headline"]["orphan_registration_count"] = payload
    out_dir = tmp_path / "sparkxss"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-13", doc1)
    _write_sidecar(out_dir, "2026-07-14", doc2)
    _write_sidecar(out_dir, "2026-07-15", doc3)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert payload not in text
    assert "&lt;" in text or "&quot;" in text or "&amp;" in text
    # the corrupted series' own sparkline is skipped (non-numeric guard), but the
    # other 7 headline series still render sparklines normally.
    assert text.count('class="sparkline"') == len(rh.HEADLINE_KEYS) - 1


# ======================================= pre-flight exit gate: trend-series eligibility
def _crash_envelope_doc(root="/fake/root"):
    """The EXACT artifact `collector.main()` writes to `--out` when `build_document`
    raises: `_empty_document`'s all-zero headline plus the crash marker in `errors[]`.
    Built by calling the collector's OWN producer so the fixture cannot drift from it."""
    from test_collector import _collector
    doc = _collector._empty_document(Path(root))
    doc["errors"].append(f"{_collector._CRASH_ERROR_PREFIX}RuntimeError('boom')")
    return doc


def test_crash_marker_prefix_matches_the_collector_producer():
    """The renderer's eligibility test is a string contract with a DIFFERENT module.
    Pin both ends: if the collector ever rewords its crash marker, this fails here
    rather than silently re-admitting crash envelopes to the trend series."""
    from test_collector import _collector
    assert rh.CRASH_ERROR_PREFIX == _collector._CRASH_ERROR_PREFIX
    doc = _crash_envelope_doc()
    assert any(e.startswith(rh.CRASH_ERROR_PREFIX) for e in doc["errors"])
    assert all(value == 0 for value in doc["headline"].values())


def _profile_rejection_envelope_doc(root="/fake/root"):
    """The EXACT artifact `collector.main()` writes to `--out` when the resolved
    `--profile` fails validation: `_empty_document`'s all-zero headline plus the
    profile-rejection marker in `errors[]`. Built by calling the collector's OWN
    producer so the fixture cannot drift from it -- same shape as _crash_envelope_doc,
    a different marker (F3)."""
    from test_collector import _collector
    doc = _collector._empty_document(Path(root))
    doc["errors"].append(f"{_collector._PROFILE_ERROR_PREFIX}profiles/foo.json: bad key")
    return doc


def test_profile_rejection_envelope_is_not_a_measured_run():
    # F3, renderer home. LIVE defect, not theoretical: main()'s --out write block runs
    # regardless of profile_error, so a profile-rejection envelope reaches disk as an
    # ordinary dated sidecar. Pre-fix _run_was_measured returned True for it, so
    # _empty_document's eight fabricated zeros rendered as LEAN / CLEAN -- a confident
    # all-clear for a run that inventoried nothing -- and joined the trend series as a
    # GREEN "improving" latest point. Same defect select_current was repaired for on the
    # crash-envelope path.
    doc = _profile_rejection_envelope_doc()
    assert rh._run_was_measured(doc) is False


def test_profile_rejection_envelope_excluded_from_trend_series():
    # Trend home, mirrors test_trend_excludes_a_crashed_collector_run with the OTHER
    # unmeasured-run marker. Eight fabricated zeros as the latest point render a GREEN
    # "improving" delta for every polarity=="up" metric -- inflation in the reassuring
    # direction, which is the failure the A17 numeric guard exists to prevent.
    good_first = _minimal_doc(tokens_a=100, tokens_b=50)     # 150
    good_second = _minimal_doc(tokens_a=200, tokens_b=50)    # 250
    model = rh.build_trend_model([("2026-07-13", good_first), ("2026-07-14", good_second),
                                  ("2026-07-15", _profile_rejection_envelope_doc())])
    assert model["dates"] == ["2026-07-13", "2026-07-14"]
    tokens = next(s for s in model["series"] if s["key"] == "always_loaded_tokens_est")
    assert tokens["values"] == [150, 250]
    assert rh._trend_delta(model, "always_loaded_tokens_est") == ("▲ 100", "bad")


def test_trend_excludes_a_crashed_collector_run(tmp_path):
    """A crash envelope carries eight headline ZEROS that were never measured. Joined to
    the series as the LATEST point they read as a collapse to zero, and for every
    polarity=="up" metric that renders a GREEN "improving" delta -- the reassuring-
    direction inflation the A17 guard exists to stop, arriving through a value
    `finite_number` happily accepts. Pre-fix this returned ('▼ 250', 'good')."""
    out_dir = tmp_path / "crashtrend"
    out_dir.mkdir()
    good_first = _minimal_doc(tokens_a=100, tokens_b=50)     # 150
    good_second = _minimal_doc(tokens_a=200, tokens_b=50)    # 250
    _write_sidecar(out_dir, "2026-07-13", good_first)
    _write_sidecar(out_dir, "2026-07-14", good_second)
    _write_sidecar(out_dir, "2026-07-15", _crash_envelope_doc())
    model = rh.build_trend_model([("2026-07-13", good_first), ("2026-07-14", good_second),
                                  ("2026-07-15", _crash_envelope_doc())])
    assert model["dates"] == ["2026-07-13", "2026-07-14"]
    tokens = next(s for s in model["series"] if s["key"] == "always_loaded_tokens_est")
    assert tokens["values"] == [150, 250]
    assert rh._trend_delta(model, "always_loaded_tokens_est") == ("▲ 100", "bad")


def test_render_omits_a_crashed_run_from_the_trend_table(tmp_path):
    """End to end through the real CLI: the crash envelope passes `load_sidecar`
    (dict + schema_version) and matches `root`, so it reached `dated_docs` and got a
    column of measured-looking zeros in the operator's trend table."""
    out_dir = tmp_path / "crashrender"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-13", _minimal_doc(tokens_a=100, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-14", _minimal_doc(tokens_a=200, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-15", _crash_envelope_doc())
    proc = run_render(out_dir, "--date", "2026-07-14", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-14.html").read_text(encoding="utf-8")
    m = re.search(r'<th>Metric</th>(.*?)</tr>', text, re.S)
    assert m is not None
    assert "2026-07-13" in m.group(1) and "2026-07-14" in m.group(1)
    assert "2026-07-15" not in m.group(1)


def test_trend_keeps_a_run_whose_errors_are_benign(tmp_path):
    """The eligibility rule is CRASH-MARKER-ONLY, deliberately. `errors[]` also carries
    benign per-surface warnings (a failed glob, an unparseable settings.json) emitted by
    runs that measured everything else fine -- disqualifying those would silently drop
    most real runs from the series."""
    warned = _minimal_doc(tokens_a=200, tokens_b=50)
    warned["errors"] = ["rules glob failed for /x: [Errno 13] Permission denied"]
    model = rh.build_trend_model([("2026-07-14", _minimal_doc(tokens_a=100, tokens_b=50)),
                                  ("2026-07-15", warned)])
    assert model["dates"] == ["2026-07-14", "2026-07-15"]
    assert rh._trend_delta(model, "always_loaded_tokens_est") == ("▲ 100", "bad")


def test_trend_drops_a_missing_headline_key_instead_of_zeroing_it(tmp_path):
    """A MISSING headline key means "not measured", never 0. A fabricated zero is a
    measurement the collector never made, and it drives both the sparkline geometry
    and the delta verdict."""
    partial = _minimal_doc(tokens_a=200, tokens_b=50)
    partial["headline"].pop("duplicate_pair_count")
    model = rh.build_trend_model([("2026-07-13", _minimal_doc(tokens_a=100, tokens_b=50)),
                                  ("2026-07-14", partial),
                                  ("2026-07-15", _minimal_doc(tokens_a=300, tokens_b=50))])
    dup = next(s for s in model["series"] if s["key"] == "duplicate_pair_count")
    assert model["dates"] == ["2026-07-13", "2026-07-14", "2026-07-15"]
    assert dup["values"] == [1, None, 1]      # aligned with dates, hole preserved
    assert dup["points"] == [1, 1]            # measured points only
    assert rh._trend_delta(model, "duplicate_pair_count") == ("= 0", "neutral")
    tokens = next(s for s in model["series"] if s["key"] == "always_loaded_tokens_est")
    assert tokens["points"] == [150, 250, 350]


def test_trend_table_renders_a_missing_point_as_not_measured(tmp_path):
    """The operator-facing half: the dropped point must read as absent, never as 0."""
    out_dir = tmp_path / "missingkey"
    out_dir.mkdir()
    partial = _minimal_doc(tokens_a=200, tokens_b=50)
    partial["headline"].pop("duplicate_pair_count")
    _write_sidecar(out_dir, "2026-07-13", _minimal_doc(tokens_a=100, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-14", partial)
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc(tokens_a=300, tokens_b=50))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    m = re.search(r'<tr><td>Duplicate pairs</td>(.*?)</tr>', text, re.S)
    assert m is not None
    assert rh.TREND_NOT_MEASURED_TEXT in m.group(1)


# --------------------------------------------------------- S6c Task 2: derived trend keys
def test_derived_trend_keys_cover_the_six_and_exclude_unchecked_binaries():
    """Fork (3), corrected: 14 declared / 14 RENDERED. `unchecked_binary_count` is
    excluded from the DERIVED table only (S6 §6.8 item 1) -- it stays in HEADLINE_KEYS
    and the legacy table still renders it, because three shipped assertions count
    sparklines against len(HEADLINE_KEYS). Its polarity is `none`, so the legacy table
    renders it `no direction` -- honest for a metric that is permanently 0 and
    explicitly never inspected, where a DERIVED trend row would have manufactured a
    false-clean reading.
    # Changing this set requires a spec change (S6 §6.6)."""
    assert [k for k, _, _ in rh.DERIVED_TREND_KEYS] == [
        "promotion_candidate_count", "memory_body_count", "phantom_ref_count",
        "phantom_confirmed_count", "hooks_with_test_ratio", "skills_with_test_ratio"]


def test_headline_polarity_is_derived_from_headline_keys_never_retyped():
    """One home, enforced (S6 §6.5). A second hand-written copy of the eight headline
    polarities is the two-homes defect A3 already records."""
    assert rh._HEADLINE_POLARITY == {k: p for k, _, p in rh.HEADLINE_KEYS}


def test_phantom_confirmed_counts_only_strictly_false():
    """`resolved is False`, an IDENTITY check -- never truthiness. `resolved: null` is
    the INFERRED/unverifiable state; counting it as a confirmed miss is the exact
    wrong-evidence-label defect D4 exists to fix."""
    # rows: resolved False, False, None, True -> confirmed == 2
    doc = {"phantom_refs": [
        {"source": "a.md", "ref": "x", "kind": "path", "resolved": False, "evidence": "VERIFIED"},
        {"source": "b.md", "ref": "y", "kind": "path", "resolved": False, "evidence": "VERIFIED"},
        {"source": "c.md", "ref": "z", "kind": "path", "resolved": None, "evidence": "INFERRED"},
        {"source": "d.md", "ref": "w", "kind": "path", "resolved": True, "evidence": "VERIFIED"},
    ]}
    assert rh._derived_phantom_ref_count(doc) == 4
    assert rh._derived_phantom_confirmed_count(doc) == 2


def test_ratio_series_carries_value_total_and_ratio_never_a_bare_float():
    """A bare float loses the denominator, and a shrinking denominator manufactures a
    fake improvement: delete a hook -> 16/20 -> coverage 'improves' with zero tests
    written."""
    doc = _minimal_doc()
    doc["test_coverage"]["summary"] = {"hooks_with_test": 15, "hooks_total": 20,
                                        "skills_with_test": 4, "skills_total": 5}
    model = rh.build_derived_trend_model([("2026-07-15", doc)])
    hooks = next(s for s in model["series"] if s["key"] == "hooks_with_test_ratio")
    expected = {"value": 15, "total": 20, "ratio": 0.75}
    assert hooks["values"] == [expected]
    assert hooks["points"] == [expected]
    skills = next(s for s in model["series"] if s["key"] == "skills_with_test_ratio")
    assert skills["values"] == [{"value": 4, "total": 5, "ratio": 0.8}]


def test_ratio_point_is_dropped_when_total_is_zero_or_absent():
    """total 0/absent -> ratio None -> the point is DROPPED, never 0.0. A fabricated
    0.0 is a measurement that did not happen."""
    zero_total = _minimal_doc()
    zero_total["test_coverage"]["summary"] = {"hooks_with_test": 0, "hooks_total": 0,
                                                "skills_with_test": 0, "skills_total": 0}
    absent_total = _minimal_doc()
    absent_total["test_coverage"]["summary"] = {"hooks_with_test": 3,
                                                  "skills_with_test": 0, "skills_total": 0}
    model = rh.build_derived_trend_model([("2026-07-14", zero_total), ("2026-07-15", absent_total)])
    hooks = next(s for s in model["series"] if s["key"] == "hooks_with_test_ratio")
    assert hooks["points"] == []
    assert hooks["values"] == [{"value": 0, "total": 0, "ratio": None},
                                {"value": 3, "total": None, "ratio": None}]


def test_derived_extractor_degrades_to_none_on_misshaped_sidecar():
    """Every extractor is TOTAL: a missing key, a null, a string, and a non-list all
    degrade to None (=> 'not measured'), never raise and never default to 0."""
    # list-shaped: promotion_candidate_count
    assert rh._derived_promotion_candidate_count({}) is None                    # missing key
    assert rh._derived_promotion_candidate_count({"promotion_candidates": None}) is None
    assert rh._derived_promotion_candidate_count({"promotion_candidates": "x"}) is None
    assert rh._derived_promotion_candidate_count({"promotion_candidates": {"a": 1}}) is None

    # nested list-shaped: memory_body_count
    assert rh._derived_memory_body_count({}) is None
    assert rh._derived_memory_body_count({"on_demand": None}) is None
    assert rh._derived_memory_body_count({"on_demand": "x"}) is None
    assert rh._derived_memory_body_count({"on_demand": {"memory_bodies": None}}) is None
    assert rh._derived_memory_body_count({"on_demand": {"memory_bodies": "x"}}) is None

    # list-shaped: phantom_ref_count / phantom_confirmed_count
    for extractor in (rh._derived_phantom_ref_count, rh._derived_phantom_confirmed_count):
        assert extractor({}) is None
        assert extractor({"phantom_refs": None}) is None
        assert extractor({"phantom_refs": "x"}) is None
        assert extractor({"phantom_refs": 0}) is None

    # dict-shaped container: the two ratio extractors
    for extractor in (rh._derived_hooks_test_ratio, rh._derived_skills_test_ratio):
        assert extractor({}) is None
        assert extractor({"test_coverage": None}) is None
        assert extractor({"test_coverage": "x"}) is None
        assert extractor({"test_coverage": {"summary": None}}) is None
        assert extractor({"test_coverage": {"summary": "x"}}) is None
        assert extractor({"test_coverage": {"summary": []}}) is None


def test_headline_trend_model_is_unchanged_by_the_derived_builder():
    """build_trend_model and HEADLINE_KEYS are untouched contracts (S6 §6.6)."""
    doc1 = _minimal_doc(tokens_a=100, tokens_b=50)
    doc2 = _minimal_doc(tokens_a=200, tokens_b=50)
    dated = [("2026-07-14", doc1), ("2026-07-15", doc2)]
    headline_model = rh.build_trend_model(dated)
    rh.build_derived_trend_model(dated)  # runs alongside; must not disturb the headline model
    headline_model_again = rh.build_trend_model(dated)
    assert headline_model == headline_model_again
    assert len(headline_model["series"]) == len(rh.HEADLINE_KEYS)


def test_trend_delta_serves_the_derived_model_unchanged():
    """LOAD-BEARING for Task 3, not a nice-to-have. `_trend_delta` must work on BOTH
    models so polarity->direction has ONE home; if the derived model's shape diverges,
    the classifier is forced to re-derive good/bad and the mapping lands in two homes --
    the A3 defect this plan cites against itself. Same `series` list, same
    key/polarity/values/points fields, so `_series_points` and `_trend_delta` both work
    on either model."""
    body = {"path": "a.md", "project_slug": "x", "lines": 1, "words": 1, "evidence": "VERIFIED"}
    doc1 = _minimal_doc()
    doc1["on_demand"]["memory_bodies"] = [body] * 92
    doc2 = _minimal_doc()
    doc2["on_demand"]["memory_bodies"] = [body] * 117
    dated_docs = [("2026-07-14", doc1), ("2026-07-15", doc2)]
    model = rh.build_derived_trend_model(dated_docs)
    assert rh._trend_delta(model, "memory_body_count") is not None


def test_sparkline_gate_counts_measured_points_not_sidecar_count(tmp_path):
    """SPARKLINE_MIN_POINTS is a floor on REAL points. With three sidecars but only two
    measurements of one metric, that metric's sparkline must not render -- drawing a
    two-point line under a >=3 gate would smuggle the dropped point back as geometry."""
    out_dir = tmp_path / "sparkgate"
    out_dir.mkdir()
    partial = _minimal_doc(tokens_a=200, tokens_b=50)
    partial["headline"].pop("duplicate_pair_count")
    _write_sidecar(out_dir, "2026-07-13", _minimal_doc(tokens_a=100, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-14", partial)
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc(tokens_a=300, tokens_b=50))
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert text.count('class="sparkline"') == len(rh.HEADLINE_KEYS) - 1
    assert 'id="spark-duplicate_pair_count"' not in text


# ===================================== pre-flight exit gate: one `resolved` policy
def test_phantom_label_and_guidance_agree_on_a_hostile_resolved_value():
    """`_resolved_label` is identity-based (True/False/anything-else); `_phantom_guidance`
    branched on TRUTHINESS. For a non-bool truthy value out of an untrusted sidecar the
    Resolved column printed "unverifiable" while the guidance printed "Resolved at
    collection time -- no action needed": two contradictory statements about one row."""
    provenance = "Resolved at collection time — listed for provenance; no action needed."
    for hostile in ("probably", 1, [1], {"a": 1}, 0.5):
        assert rh._resolved_label(hostile) == "unverifiable", hostile
        assert rh._phantom_guidance("path", hostile) != provenance, hostile
        assert rh._phantom_guidance("slash_command", hostile) == \
            rh._PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE, hostile
    # the three in-contract states keep their existing meanings
    assert rh._resolved_label(True) == "yes"
    assert rh._phantom_guidance("path", True) == provenance
    assert rh._resolved_label(False) == "no"
    assert rh._phantom_guidance("path", False) == rh._PHANTOM_GUIDANCE["path"]
    assert rh._resolved_label(None) == "unverifiable"
    assert rh._phantom_guidance("slash_command", None) == rh._PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE


def test_render_row_never_pairs_unverifiable_with_no_action_needed(tmp_path):
    """The same disagreement observed where the operator sees it: one table row."""
    doc = _minimal_doc()
    doc["phantom_refs"] = [{"source": "rules/a.md", "ref": "ghost.md", "kind": "path",
                            "resolved": "probably", "evidence": "VERIFIED"}]
    out_dir = tmp_path / "hostileresolved"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    m = re.search(r'<tr><td>rules/a\.md</td><td>ghost\.md</td>(.*?)</tr>', text, re.S)
    assert m is not None
    row = m.group(1)
    assert "unverifiable" in row
    assert "no action needed" not in row


# ============================================================= S6b / D4 §7.4: phantom-table grouping
# The distinction the operator must read instantly is "we checked and it's missing" vs
# "there was never anything to check". A group boundary is read before any text is, which
# is why this is a row GROUP and not a fifth column.
_D4_MIXED_ROWS = [
    {"source": "a.md", "ref": "scripts/deploy.sh", "kind": "path",
     "resolved": False, "evidence": "VERIFIED"},
    {"source": "a.md", "ref": "<repo>/docs/x-<slug>.md", "kind": "template",
     "resolved": None, "evidence": "INFERRED"},
    {"source": "a.md", "ref": "/usr/bin/tool.sh", "kind": "external",
     "resolved": None, "evidence": "INFERRED"},
    {"source": "a.md", "ref": "/gone", "kind": "slash_command",
     "resolved": None, "evidence": "INFERRED"},
]
# FOUR rows: one per group-relevant kind. No `refspec` row -- that kind is deferred to
# S6c (DEVIATION 5) and the collector never emits it, so a fixture carrying one would
# assert grouping behavior for a kind that does not exist.


def test_build_phantom_groups_returns_three_groups_in_fixed_order():
    groups = rh.build_phantom_groups(_D4_MIXED_ROWS)
    assert [g[0] for g in groups] == ["verified_missing", "not_a_path", "unverifiable"]


def test_build_phantom_groups_partitions_by_semantics_not_kind_name():
    groups = dict((g[0], g[2]) for g in rh.build_phantom_groups(_D4_MIXED_ROWS))
    assert [r["ref"] for r in groups["verified_missing"]] == ["scripts/deploy.sh"]
    assert [r["ref"] for r in groups["not_a_path"]] == ["<repo>/docs/x-<slug>.md"]
    assert [r["ref"] for r in groups["unverifiable"]] == ["/usr/bin/tool.sh", "/gone"]


def test_build_phantom_groups_is_pure_and_preserves_input_order():
    a = rh.build_phantom_groups(_D4_MIXED_ROWS)
    b = rh.build_phantom_groups([dict(r) for r in _D4_MIXED_ROWS])
    assert [(g[0], g[1], [r["ref"] for r in g[2]]) for g in a] == \
           [(g[0], g[1], [r["ref"] for r in g[2]]) for g in b]


def test_phantom_group_counts_are_derived_not_literal():
    """Requirement 20: the round-1 design text hardcoded `2/6/1` and "8 of 10", both
    arithmetically wrong against its own after-table. Two different row sets must produce
    two different rendered counts."""
    small = rh.build_phantom_groups(_D4_MIXED_ROWS[:2])
    full = rh.build_phantom_groups(_D4_MIXED_ROWS)
    assert [len(g[2]) for g in small] == [1, 1, 0]
    assert [len(g[2]) for g in full] == [1, 1, 2]


def test_phantom_group_counts_reconcile_to_the_row_total():
    """MANDATORY (§7.4, requirement 21) and a NAMED S6b exit gate. A design that states
    group counts as prose drifts the next time a kind is added — that is precisely how
    this defect arose.

    TWO invariants:
      * N1 + N2 + N3 == len(rows) -- ships exactly as requirement 21 specifies.
      * the drawer numerator == N2. Requirement 21 as written said `N2 + N3`; Codex P2-4
        dropped N3, because `unverifiable` (external + slash_command) MAY resolve outside
        the scanned root and so does not earn a count whose name asserts certainty.

    The expected value is derived here by a DIFFERENT route than the function uses -- a
    direct kind filter over `rows`, versus the function's walk of the grouping -- so this
    is not a tautology, and it will FAIL rather than silently agree if a kind is ever
    added to `not_a_path` without meeting that group's never-resolvable-by-construction
    bar (see `_phantom_never_resolvable_count`). That failure is the point: it is the
    guardrail S6c trips if it returns `refspec` to the group without revisiting the
    count."""
    for rows in ([], _D4_MIXED_ROWS[:1], _D4_MIXED_ROWS[:3], _D4_MIXED_ROWS,
                 _D4_MIXED_ROWS + [{"source": "a.md", "ref": "x", "kind": "mystery",
                                    "resolved": "weird", "evidence": "INFERRED"}]):
        groups = rh.build_phantom_groups(rows)
        counts = [len(g[2]) for g in groups]
        assert sum(counts) == len(rows), (rows, counts)
        assert rh._phantom_never_resolvable_count(rows) == counts[1], (rows, counts)
        expected_never = sum(1 for r in rows if r.get("kind") == "template")
        assert rh._phantom_never_resolvable_count(rows) == expected_never, (rows, counts)


def test_phantom_status_word_for_new_kinds_is_not_a_path():
    """Requirement 23: `no` answers "does it exist", and these were never asked."""
    assert rh._phantom_status_word("template", None) == "not a path"
    assert rh._phantom_status_word("external", None) != "not a path"
    assert rh._phantom_status_word("path", False) == "no"
    assert rh._phantom_status_word("external", None) == "unverifiable"
    assert rh._phantom_status_word("path", True) == "yes"


def test_unknown_phantom_kind_lands_in_unverifiable_never_dropped():
    rows = [{"source": "a.md", "ref": "x", "kind": "mystery",
             "resolved": None, "evidence": "INFERRED"}]
    groups = dict((g[0], g[2]) for g in rh.build_phantom_groups(rows))
    assert groups["unverifiable"] == rows


def test_hostile_resolved_false_on_a_template_row_still_groups_as_not_a_path():
    """A shape classification can never carry a confirmed negative, even from a hostile
    sidecar. The kind check precedes the resolved check for exactly this reason."""
    rows = [{"source": "a.md", "ref": "docs/{x}.md", "kind": "template",
             "resolved": False, "evidence": "VERIFIED"}]
    groups = dict((g[0], g[2]) for g in rh.build_phantom_groups(rows))
    assert groups["not_a_path"] == rows and groups["verified_missing"] == []


def test_phantom_drawer_line_is_derived_from_the_row_set():
    """Requirement 24: both figures derived. Two different docs, two different sentences.

    Both the 2-row slice and the full 4-row set contain exactly ONE template, so the
    numerator is 1 in both and only the DENOMINATOR moves. That is the stronger fixture:
    a numerator that tracked total rows, or the `unverifiable` group, would read 1-of-2
    and 3-of-4 and pass a laxer assertion."""
    two = {"phantom_refs": _D4_MIXED_ROWS[:2]}
    four = {"phantom_refs": _D4_MIXED_ROWS}
    html_two = rh._gauge_drill_html("phantom_ref_count", {}, two, [], [], {})
    html_four = rh._gauge_drill_html("phantom_ref_count", {}, four, [], [], {})
    assert "1 of 2 rows were never resolvable paths." in html_two
    assert "1 of 4 rows were never resolvable paths." in html_four


def test_phantom_drawer_discloses_that_line_ranges_are_not_validated():
    """Requirement 12: line-range validity is UNKNOWN and must never be implied as
    checked. Stated in the drawer AND (collector side) in blind_spots."""
    html = rh._gauge_drill_html("phantom_ref_count", {}, {"phantom_refs": _D4_MIXED_ROWS},
                                [], [], {})
    assert "Line ranges in citations" in html and "are not validated" in html


def test_rendered_phantom_table_carries_three_group_headers(tmp_path):
    """End-to-end through run_render, not a unit assertion on the model — a membership
    test would pass even if the renderer stopped emitting groups."""
    doc = _minimal_doc()
    doc["phantom_refs"] = [dict(r) for r in _D4_MIXED_ROWS]
    out_dir = tmp_path / "phgroups"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    html = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Verified missing — the target was looked for and is not there (1)" in html
    assert ("Not a path — a template or glob; nothing was ever resolvable, so nothing "
            "was checked (1)") in html
    assert "Unverifiable — the target space extends outside the scanned root (2)" in html


def test_not_a_path_group_hatch_shares_the_pattern_but_not_the_severity_fill(tmp_path):
    """Requirement 22, BOTH halves, and neither half is tautological.

    (a) The PATTERN is shared with `.matrix .cell.verdict-empty` -- 135deg, 4px/8px
        stops -- because that is what carries §7.4's meaning: "the instrument did not
        produce a reading here". Asserted on the NEW selector, not on a bare
        `"repeating-linear-gradient(135deg" in html`, which would pass on the
        pre-existing rule alone and prove nothing about this change.

    (b) The FILL is deliberately NOT `var(--crit-bg)`. Fill carries severity and these
        rows have none -- their own guidance column reads "No action needed", and §7.3
        states D4's purpose as the dashboard "stop[ping] painting red over rows the
        collector never verified". Matching the critical fill would reintroduce at the
        table layer the alarm D4 removes at the gauge layer.

    What each assertion actually catches -- stated per-assertion, because "they all fail
    on revert" is not true of all three and an overstated guarantee is worse than none:
      * the selector+gradient assertion fails if the new CSS rule is DELETED or its
        pattern/fill is changed in any way;
      * the `not in` assertion fails if the fill is changed specifically to
        `var(--crit-bg)` -- the one regression it exists to block. It passes vacuously if
        the rule is deleted, which is fine: the first assertion covers deletion;
      * the reference-rule assertion fails if `.matrix .cell.verdict-empty` loses the
        135deg/4px/8px pattern. It is change-invariant with respect to THIS rule by
        design -- its job is to catch the shared pattern drifting out from under us, so
        that "shared" stops being an unchecked claim.

    Changing this requires a spec change (S6 §7.4)."""
    doc = _minimal_doc()
    doc["phantom_refs"] = [dict(r) for r in _D4_MIXED_ROWS]
    out_dir = tmp_path / "phhatch"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    html = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert '<tbody class="phantom-group phantom-group-not_a_path">' in html
    rule = ".phantom-group-not_a_path td,.phantom-group-not_a_path th{background-image:"
    gradient = ("repeating-linear-gradient(135deg,var(--surface-2) 0,var(--surface-2) 4px,"
                "transparent 4px,transparent 8px)")
    # (a) shared pattern, on the new selector
    assert f"{rule}{gradient}}}" in html
    # (a) the reference rule still carries the same 135deg/4px/8px pattern, so "shared"
    # is asserted against the thing it is shared WITH, not merely restated
    assert ("repeating-linear-gradient(135deg,var(--crit-bg) 0,var(--crit-bg) 4px,"
            "transparent 4px,transparent 8px)") in html
    # (b) the new rule must NOT adopt the severity fill
    assert f"{rule}repeating-linear-gradient(135deg,var(--crit-bg)" not in html


def test_grouped_phantom_data_rows_keep_a_bare_tr_opening_tag(tmp_path):
    """RULE-7 GUARD, and the reason the group carrier is a <tbody>. An existing test at
    tests/test_render_html.py:3173 regex-matches a BARE `<tr><td>` on a row that groups
    as `unverifiable`. This asserts the property that test depends on, at the layer this
    change controls, so a future refactor that moves the class onto the <tr> fails HERE
    with a clear reason instead of failing an unrelated-looking hostile-value test."""
    doc = _minimal_doc()
    doc["phantom_refs"] = [dict(r) for r in _D4_MIXED_ROWS]
    out_dir = tmp_path / "phtr"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    html = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert re.search(r'<tr><td>a\.md</td><td>scripts/deploy\.sh</td>', html), \
        "data-row <tr> gained an attribute — binding rule 7: this breaks the bare-<tr> regex " \
        "in test_render_row_never_pairs_unverifiable_with_no_action_needed"
    assert 'class="phantom-row' not in html, \
        "the group class belongs on the <tbody>, never on a data-row <tr>"


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


# S2.M4: retired slash-command detection (phantom_refs kind=slash_command; SPEC_4 §2).
def test_build_phantom_ref_brief_slash_command_guidance_is_specific():
    ref = {"source": "rules/a.md", "ref": "/gone-command", "kind": "slash_command", "resolved": False}
    brief = rh.build_phantom_ref_brief(ref)
    assert "Retired slash command" in brief        # slash_command-specific guidance
    assert "Verify the target exists" not in brief  # not the default catch-all


# S2 gate fix (R2/F1): the resolved-is-None guidance branch.
def test_phantom_guidance_for_unverifiable_slash_command():
    """resolved=null (the new collector shape) must NOT fall through to the legacy
    resolved=false text, which tells the operator a valid /simplify reference points at
    'a command that no longer exists' -- a factually false instruction (GP#15)."""
    brief = rh.build_phantom_ref_brief(
        {"source": "rules/defensive-simplify-guard.md", "ref": "/simplify",
         "kind": "slash_command", "resolved": None})
    assert "No home for this command under the scanned root" in brief
    assert "BUILT-INS" in brief
    assert "no longer exists" not in brief
    assert "Verify the target exists" not in brief   # not the catch-all either

def test_legacy_resolved_false_slash_command_guidance_is_preserved():
    """Old sidecars still carry resolved=false; that rendering is unchanged."""
    brief = rh.build_phantom_ref_brief(
        {"source": "s", "ref": "/x", "kind": "slash_command", "resolved": False})
    assert "Retired slash command" in brief

def test_phantom_table_renders_unverifiable_not_a_bare_none(tmp_path):
    """M3: `resolved` renders verbatim in the Resolved cell, so the new null shape would
    print a literal Python "None" to the operator."""
    doc = _minimal_doc()
    doc["phantom_refs"] = [{"source": "rules/a.md", "ref": "/simplify",
                            "kind": "slash_command", "resolved": None,
                            "evidence": "INFERRED"}]
    out_dir = tmp_path / "ph"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "<td>unverifiable</td>" in text
    assert "<td>None</td>" not in text

def test_unverifiable_brief_finding_is_not_a_confident_failure():
    """R2-F6(a): resolved=null must not produce 'does not resolve to a real target'."""
    brief = rh.build_phantom_ref_brief(
        {"source": "rules/defensive-simplify-guard.md", "ref": "/simplify",
         "kind": "slash_command", "resolved": None})
    assert "does not resolve to a real target" not in brief
    assert "could not verify" in brief

def test_gauge_does_not_paint_unverifiable_rows_broken(tmp_path):
    """R2-F6(c): a doc whose ONLY phantom row is resolved=null must not render BROKEN
    ('BROKEN' is unique to phantom_ref_count in GAUGE_BANDS -- verified -- so the
    absence assertion is precise). The row itself must still be visible."""
    doc = _minimal_doc()
    doc["phantom_refs"] = [{"source": "rules/a.md", "ref": "/simplify",
                            "kind": "slash_command", "resolved": None,
                            "evidence": "INFERRED"}]
    out_dir = tmp_path / "ph2"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "BROKEN" not in text
    assert "(unverifiable)" in text          # the drill marker -- visible, not vanished
    # R4-2: scope to the phantom CARD and require the band SURVIVES the display/band
    # split. Passing the formatted string as the band input would make finite_number
    # reject it -> ("", "neutral") -> no CLEAN -- and unscoped assertions can't see that.
    card = text.split('data-gauge="phantom_ref_count"', 1)[1][:400]
    assert "1 (0 confirmed)" in card         # R3-2: displayed count stays the total
    assert "CLEAN" in card                   # the band is derived from band_value=0
    # QA exit gate (HIGH 1): the Overview digest bands the SAME metric and must not
    # contradict the header gauge. Scope to the Hygiene group's phantom row -- `_sev_dot`
    # emits only the SEMANTIC class, never a band LABEL, so the page-wide "BROKEN"
    # assertion above is structurally blind to a red dot cast over unverified rows.
    hygiene_group = text.split("<h3>Hygiene</h3>", 1)[1].split("</ul>", 1)[0]
    phantom_row = next(li for li in hygiene_group.split("<li>") if "Phantom refs" in li)
    assert "sev-good" in phantom_row         # banded on the CONFIRMED count (0), not the total
    assert "sev-bad" not in phantom_row
    assert "Phantom refs: 1" in phantom_row  # ...while the DISPLAYED number stays the total


# S6b / D4 renderer half. `_phantom_guidance` falls through to
# _PHANTOM_GUIDANCE_DEFAULT ("Verify the target exists or remove the pointer"), which
# instructs the operator to DELETE a correct stencil. The new kind needs its own entry.
def test_phantom_guidance_template_entry_is_present_and_verbatim():
    assert rh._PHANTOM_GUIDANCE["template"] == (
        "Template/placeholder reference — the token names a SHAPE (`<...>`, `{...}`, a "
        "glob, or a YYYY-MM-DD stencil), not a file, so there is nothing to resolve. No "
        "action needed unless the placeholder itself is wrong.")
    assert rh._phantom_guidance("template", None) == rh._PHANTOM_GUIDANCE["template"]


def test_phantom_guidance_has_no_refspec_entry(fake_harness):
    """DEVIATION 5: the `refspec` kind is deferred to S6c and this stage must not ship a
    guidance string for a kind the collector never emits. A dead entry would be a dark
    feature -- and worse, a reviewer or an S6c implementer could read its presence as
    evidence the arm shipped.

    S6c adds BOTH the kind and its guidance, together. Changing this requires a spec
    change (S6 §7.2)."""
    assert "refspec" not in rh._PHANTOM_GUIDANCE
    # and an unknown kind still lands on the catch-all rather than anything bespoke
    assert rh._phantom_guidance("refspec", None) == rh._PHANTOM_GUIDANCE_DEFAULT


def test_phantom_guidance_legacy_entries_are_preserved_byte_for_byte():
    """Additive only: the four pre-existing kinds and the catch-all default must be
    unchanged, and the slash-command unverifiable override must still win."""
    for kind in ("path", "external", "env_flag", "slash_command"):
        assert kind in rh._PHANTOM_GUIDANCE
    assert rh._PHANTOM_GUIDANCE["path"].startswith("Broken path —")
    assert rh._PHANTOM_GUIDANCE["external"].startswith("External ref —")
    assert rh._PHANTOM_GUIDANCE["env_flag"].startswith("Env-flag ref —")
    assert rh._PHANTOM_GUIDANCE_DEFAULT == "Verify the target exists or remove the pointer."
    assert rh._phantom_guidance("slash_command", None) == \
        rh._PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE
    assert rh._phantom_guidance("mystery", None) == rh._PHANTOM_GUIDANCE_DEFAULT


# T3.1: `_phantom_guidance` must be TOTAL, mirroring `_resolved_state` above it.
# `_PHANTOM_GUIDANCE.get(kind, ...)` hashes `kind`, and `kind` arrives straight from
# sidecar JSON (`r.get("kind", "")` / `build_phantom_ref_brief`) -- a stale, corrupted,
# or hand-crafted sidecar can carry `"kind": []` or `"kind": {}`, both valid JSON, both
# unhashable. Per `_tokens_treemap`'s documented invariant (render_html.py:488-494), one
# malformed row must degrade to the catch-all guidance, not kill the whole render.
def test_phantom_guidance_unhashable_kind_returns_default_without_raising():
    assert rh._phantom_guidance([], False) == rh._PHANTOM_GUIDANCE_DEFAULT
    assert rh._phantom_guidance({}, False) == rh._PHANTOM_GUIDANCE_DEFAULT
    assert rh._phantom_guidance([], None) == rh._PHANTOM_GUIDANCE_DEFAULT
    assert rh._phantom_guidance({}, True) == \
        "Resolved at collection time — listed for provenance; no action needed."


def test_phantom_guidance_other_hostile_kind_types_still_hit_default():
    """Non-string, HASHABLE kinds already fell through to the catch-all before this fix
    (they never raised) -- pin that they still do, unchanged."""
    assert rh._phantom_guidance(b"path", False) == rh._PHANTOM_GUIDANCE_DEFAULT
    assert rh._phantom_guidance(3, False) == rh._PHANTOM_GUIDANCE_DEFAULT
    assert rh._phantom_guidance(None, False) == rh._PHANTOM_GUIDANCE_DEFAULT


def test_phantom_guidance_totality_fix_is_a_no_op_for_every_known_kind():
    """The fix must be a pure no-op for every input that previously worked: every real
    kind still routes to its own entry, the slash_command/resolved=None override still
    wins, the resolved=True provenance branch is untouched, and an unknown STRING kind
    still lands on the catch-all."""
    provenance = "Resolved at collection time — listed for provenance; no action needed."
    assert rh._phantom_guidance("template", False) == rh._PHANTOM_GUIDANCE["template"]
    assert rh._phantom_guidance("path", False) == rh._PHANTOM_GUIDANCE["path"]
    assert rh._phantom_guidance("external", False) == rh._PHANTOM_GUIDANCE["external"]
    assert rh._phantom_guidance("env_flag", False) == rh._PHANTOM_GUIDANCE["env_flag"]
    assert rh._phantom_guidance("slash_command", None) == \
        rh._PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE
    assert rh._phantom_guidance("slash_command", False) == \
        rh._PHANTOM_GUIDANCE["slash_command"]
    assert rh._phantom_guidance("mystery", False) == rh._PHANTOM_GUIDANCE_DEFAULT
    assert rh._phantom_guidance("path", True) == provenance
    assert rh._phantom_guidance([], True) == provenance   # resolved=True short-circuits
                                                            # before kind is ever looked up


# ============================================================= S6b QA P1: build_phantom_ref_brief
# was not kind-aware for `template`. Before this fix, `template` (resolved=None) fell
# into the generic out-of-root branch: the Finding claimed "the resolution space extends
# outside the scanned root" (false -- a stencil has no resolution space), and the Action
# manufactured `/coding-team` work on a non-issue while promising the row would
# disappear (false -- a correct template still renders as a template row).
def test_build_phantom_ref_brief_is_kind_aware_for_template():
    ref = {"source": "CLAUDE.md", "ref": "<repo>/docs/handoff/YYYY-MM-DD-<slug>.md",
           "kind": "template", "resolved": None}
    brief = rh.build_phantom_ref_brief(ref)
    assert "extends outside the scanned root" not in brief
    assert "Route this through" not in brief
    assert "gone" not in brief
    assert "No action needed unless the placeholder" in brief  # correct template guidance
    assert brief == rh.build_phantom_ref_brief(dict(ref))      # pure


def test_build_phantom_ref_brief_template_regression_other_kinds_unchanged():
    """The template branch must not change any pre-existing kind's Finding/Action --
    same fixtures as test_build_phantom_ref_brief_is_pure_and_kind_aware."""
    path_brief = rh.build_phantom_ref_brief(
        {"source": "rules/a.md", "ref": "nope.md", "kind": "path", "resolved": False})
    assert "does not resolve to a real target" in path_brief
    assert "Route this through `/coding-team`" in path_brief
    slash_brief = rh.build_phantom_ref_brief(
        {"source": "rules/a.md", "ref": "/gone", "kind": "slash_command", "resolved": None})
    assert "could not verify" in slash_brief
    assert "Route this through `/coding-team`" in slash_brief


# ============================================================= S6b QA P3: build_phantom_ref_brief
# was not kind-aware for env_flag's unverifiable state (resolved=None). Before this fix, the
# generic branch's Finding claimed "the resolution space extends outside the scanned root" --
# false for env_flag, whose real cause (an unreadable hook file INSIDE `--root`) is already
# named, correctly, by `_PHANTOM_GUIDANCE_ENV_FLAG_UNVERIFIABLE` in the very next section of
# the same document. The Finding and the "What to do" section contradicted each other by name.
def test_build_phantom_ref_brief_env_flag_unverifiable_names_the_real_cause():
    ref = {"source": "rules/a.md", "ref": "MY_GUARD_FLAG", "kind": "env_flag",
           "resolved": None}
    brief = rh.build_phantom_ref_brief(ref)
    # The Finding must not claim a resolution space outside the scanned root.
    assert "extends outside the scanned root" not in brief
    # The Action must not promise the row disappears on re-run...
    assert "confirm the phantom ref is gone" not in brief
    # ...and must not instruct removing the reference as the remedy.
    assert "correct or remove the reference" not in brief
    # It must state the real cause and point at the real blocker instead.
    assert "hook file inside the scanned root could not be read" in brief
    assert "Inaccessible card" in brief
    assert brief == rh.build_phantom_ref_brief(dict(ref))   # pure


def test_build_phantom_ref_brief_env_flag_resolved_false_unchanged():
    """The confirmed-negative case (complete hooks corpus) must be byte-unchanged by the
    P3 fix -- routing through `/coding-team` on a genuine confirmed-missing target is
    correct there, unlike the unverifiable case above."""
    ref = {"source": "rules/a.md", "ref": "MY_GUARD_FLAG", "kind": "env_flag",
           "resolved": False}
    brief = rh.build_phantom_ref_brief(ref)
    assert "does not resolve to a real target" in brief
    assert ("Route this through `/coding-team`: correct or remove the reference in "
            "`rules/a.md`, then re-run `/harness-map` to confirm the phantom ref is "
            "gone.") in brief


# ============================================================= S6b QA P2: env_flag unverifiable
# guidance. `_PHANTOM_GROUP_ORDER`'s "unverifiable" header text ("the target space extends
# outside the scanned root") does not hold for env_flag: resolved=null there means a hook
# file INSIDE --root could not be read (_hooks_body_corpus, collector.py:3327), not that
# the target space extends outside the root. The header stays (pinned verbatim by
# test_rendered_phantom_table_carries_three_group_headers; correcting it would LOOSEN a
# same-execution assertion, which A27 does not permit) -- the row-level guidance cell
# carries the real reason instead.
def test_phantom_guidance_env_flag_unverifiable_names_the_real_cause():
    text = rh._phantom_guidance("env_flag", None)
    assert text == rh._PHANTOM_GUIDANCE_ENV_FLAG_UNVERIFIABLE
    assert "hook file inside the scanned root" in text
    assert "could not be read" in text
    assert "Inaccessible card" in text
    assert text != rh._PHANTOM_GUIDANCE["env_flag"]   # not the legacy confirmed-negative text
    assert "extends outside the scanned root" not in text
    assert "wire the flag" not in text   # no confident action for an unconfirmed state


def test_phantom_guidance_env_flag_resolved_false_still_legacy():
    """The confirmed-negative case (complete hooks corpus) is untouched."""
    assert rh._phantom_guidance("env_flag", False) == rh._PHANTOM_GUIDANCE["env_flag"]


# ============================================================= S6b QA: seam matrix
# One row's meaning is rendered on FOUR surfaces (group header text, status word,
# guidance cell, downloadable brief), and until now nothing compared them -- the class of
# defect both P1 and P2 are instances of. `_D4_SEAM_SURFACES` is a dict, not four inline
# calls, so a fifth surface added later is obviously meant to join `readings` below.
_D4_SEAM_SURFACES = {
    "group": lambda row: rh._phantom_group_key(row),
    "status_word": lambda row: rh._phantom_status_word(row["kind"], row["resolved"]),
    "guidance": lambda row: rh._phantom_guidance(row["kind"], row["resolved"]),
    "brief": lambda row: rh.build_phantom_ref_brief(row),
}

# S6b QA P3: which kinds' resolution space GENUINELY extends outside the scanned root --
# a CC built-in, a plugin command, or a file the walk cannot see (Codex P2-4, docstring at
# `build_phantom_groups`). `template` has no target at all; `env_flag`'s unverifiable state
# means an unreadable file INSIDE `--root`, not one beyond it. Neither may claim the
# outside-root text truthfully -- this is the set the P3 seam check below tests against.
_D4_KINDS_WITH_OUT_OF_ROOT_SPACE = frozenset({"external", "slash_command"})

# Every (kind, resolved) pair the collector can actually emit (check_phantom_refs,
# collector.py:3420-3667), plus the legacy slash_command resolved=False shape old
# sidecars still carry (S2 gate fix predates the resolved=null shape).
_D4_SEAM_MATRIX = [
    {"kind": "path", "resolved": False},          # collector.py:3642
    {"kind": "template", "resolved": None},        # collector.py:3640
    {"kind": "external", "resolved": None},        # collector.py:3520 / 3540 / 3610
    {"kind": "slash_command", "resolved": None},   # collector.py:3515 (current shape)
    {"kind": "slash_command", "resolved": False},  # legacy sidecar shape
    {"kind": "env_flag", "resolved": False},       # collector.py:3665, complete hooks corpus
    {"kind": "env_flag", "resolved": None},        # collector.py:3665, incomplete hooks corpus
]
# Excluded: kind="template", resolved=True. The collector never emits this combination
# (template rows are always resolved=None -- collector.py:3640). `_phantom_guidance`
# checks `resolved is True` BEFORE `kind` (deliberately -- a confirmed-provenance read
# cannot be overridden by a shape kind), while `_phantom_group_key` checks `kind` BEFORE
# `resolved` (deliberately -- a shape classification can never carry a confirmed negative;
# mirror case at test_hostile_resolved_false_on_a_template_row_still_groups_as_not_a_path).
# Those two deliberate, opposite precedence orders mean this one combination disagrees BY
# DESIGN: group="not_a_path" but guidance="Resolved at collection time... no action
# needed", not the template text -- verified directly by execution, not assumed. A row the
# collector cannot produce is excluded from a matrix that checks agreement on rows it does.
def test_template_resolved_true_precedence_is_the_documented_non_issue_not_a_defect():
    """Pins the reason the (template, True) combination is excluded from
    _D4_SEAM_MATRIX above -- a real assertion, not a comment asserting itself."""
    assert rh._phantom_group_key({"kind": "template", "resolved": True}) == "not_a_path"
    assert rh._phantom_guidance("template", True) == \
        "Resolved at collection time — listed for provenance; no action needed."


def test_seam_group_status_guidance_and_brief_agree_across_the_matrix():
    """For every (kind, resolved) pair the collector can emit, the four operator-facing
    surfaces must not contradict each other:
      * no row whose guidance says there is nothing to do may carry brief text
        instructing a /coding-team fix-and-remove routing;
      * no row grouped "not_a_path" (never had a target) may receive brief text telling
        the operator to fix or remove a target;
      * the status word and the group must not disagree about which rows are "not a
        path"."""
    for pair in _D4_SEAM_MATRIX:
        row = {"source": "s", "ref": "r", **pair}
        readings = {name: fn(row) for name, fn in _D4_SEAM_SURFACES.items()}
        if "no action needed" in readings["guidance"].lower():
            assert "Route this through" not in readings["brief"], pair
        if readings["group"] == "not_a_path":
            assert "does not resolve to a real target" not in readings["brief"], pair
            assert "Route this through" not in readings["brief"], pair
        assert (readings["status_word"] == "not a path") == \
            (readings["group"] == "not_a_path"), pair
        # S6b QA P3, additive: none of the three checks above caught the P3 defect --
        # they compare surfaces to EACH OTHER, never a surface's factual claim to the
        # row's kind. The env_flag-unverifiable Finding said "extends outside the scanned
        # root" while sitting beside guidance that named an in-root cause, and nothing
        # above notices a claim that is simply untrue for this row.
        claims_outside_root = "extends outside the scanned root" in readings["brief"]
        if not (pair["kind"] in _D4_KINDS_WITH_OUT_OF_ROOT_SPACE
                and pair.get("resolved") is None):
            # Every other row in the matrix: the claim must be ABSENT, because for those
            # rows it is either false (template, env_flag-unverifiable) or moot (resolved
            # is not None, so the brief takes a different branch entirely).
            assert not claims_outside_root, pair
        # A structural self-contradiction check, generalisable to any future kind or
        # surface without enumerating them: no brief may assert the resolution space
        # extends outside the scanned root while its OWN guidance section, rendered into
        # the same document, says the opposite. This is the shape the P3 defect actually
        # had -- one section claiming outside-root, the very next section naming an
        # in-root cause -- and it catches a fifth surface's version of the same mistake
        # without anyone having to add it to a kind list.
        if claims_outside_root:
            assert "not beyond it" not in readings["brief"], pair
            assert "is IN this root" not in readings["brief"], pair


def test_phantom_table_renders_when_kind_is_unhashable(tmp_path):
    """End-to-end: a malformed sidecar row (`kind` is a list) must render the row with
    the catch-all guidance-derived brief path intact, not exit the whole render with
    'fatal: could not render ...'."""
    doc = _minimal_doc()
    doc["phantom_refs"] = [{"source": "rules/a.md", "ref": "ghost.md", "kind": [],
                            "resolved": False, "evidence": "VERIFIED"}]
    out_dir = tmp_path / "unhashable_kind"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "fatal: could not render" not in text
    m = re.search(r'<tr><td>rules/a\.md</td><td>ghost\.md</td>(.*?)</tr>', text, re.S)
    assert m is not None


def test_never_resolvable_rows_do_not_paint_the_phantom_gauge_broken():
    """Requirement 17, re-verified AFTER reclassification: `_phantom_counts` returns
    (total, confirmed) and the CLEAN/BROKEN band keys off `confirmed` (resolved is False
    rows only). Template rows carry resolved=None, so they must count toward DISPLAY and
    not toward the band. The `external` row is included so the assertion is not carried
    by a single kind."""
    doc = {"phantom_refs": [
        {"source": "a.md", "ref": "docs/{x}.md", "kind": "template",
         "resolved": None, "evidence": "INFERRED"},
        {"source": "a.md", "ref": "/usr/bin/tool.sh", "kind": "external",
         "resolved": None, "evidence": "INFERRED"},
    ]}
    total, confirmed = rh._phantom_counts(doc)
    assert (total, confirmed) == (2, 0)


def test_phantom_counts_honours_kind_first_precedence_for_hostile_template_row():
    """S6b.M7: `_phantom_group_key`'s own docstring asserts kind is checked BEFORE
    `resolved` so a hostile/corrupt sidecar row `{"kind": "template", "resolved":
    false}` can never carry a confirmed negative -- but `_phantom_counts` counted it
    as `confirmed` anyway, contradicting the group it lands in (`not_a_path`) on the
    same page. The collector never emits this combination (template rows are always
    resolved=None); this is defence-in-depth against a corrupt/hand-crafted sidecar."""
    doc = {"phantom_refs": [
        {"source": "a.md", "ref": "docs/{x}.md", "kind": "template",
         "resolved": False, "evidence": "VERIFIED"},
    ]}
    total, confirmed = rh._phantom_counts(doc)
    assert (total, confirmed) == (1, 0)


def test_phantom_counts_still_counts_a_real_path_row_as_confirmed():
    """No-behaviour-change pin: an ordinary `kind="path", resolved=False` row -- the
    collector's real confirmed-missing shape -- must still count."""
    doc = {"phantom_refs": [
        {"source": "a.md", "ref": "scripts/deploy.sh", "kind": "path",
         "resolved": False, "evidence": "VERIFIED"},
    ]}
    total, confirmed = rh._phantom_counts(doc)
    assert (total, confirmed) == (1, 1)


def test_slash_command_guidance_discloses_the_absolute_path_ambiguity():
    """The replacement for the rejected §7.2 finding-#14 inversion (orchestrator ruling
    2026-08-01). `/tmp` gets a command-flavored label the collector cannot justify; the
    honest fix is to SAY SO, the remedy §7.2 proposed for `origin/main` (requirements
    13 and 18)."""
    text = rh._PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE
    assert "lexically indistinguishable from an absolute filesystem path" in text
    assert "cannot tell the two apart" in text


def test_slash_command_guidance_append_preserved_every_existing_pin():
    """Rule 7 is satisfied by APPENDING. Covers every pin in the plan's Task 3 Step 6
    table -- the single source of truth for that set -- at the two layers they live on:

      * rows 1-8, the constant-level pins, checked through `build_phantom_ref_brief` --
        two required substrings still present, three forbidden phrases still absent;
      * rows 9-10, the PAGE-level negatives, checked directly against the constant. The
        pre-existing tests at :3350 and :3374 already assert them end-to-end through a
        rendered page and must pass unmodified; these two lines assert the same property
        AT THE SOURCE so a violation fails naming the guidance string, instead of
        surfacing two tests away as a complaint about a table cell or a gauge."""
    brief = rh.build_phantom_ref_brief(
        {"source": "rules/a.md", "ref": "/tmp", "kind": "slash_command", "resolved": None})
    assert "No home for this command under the scanned root" in brief
    assert "BUILT-INS" in brief
    assert "no longer exists" not in brief
    assert "Verify the target exists" not in brief
    assert "does not resolve to a real target" not in brief
    assert "lexically indistinguishable from an absolute filesystem path" in brief
    # The two page-level forbidden strings, checked at the source so a failure names the
    # cause rather than surfacing as an unrelated-looking page assertion two tests away.
    assert "<td>None</td>" not in rh._PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE
    assert "BROKEN" not in rh._PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE


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


# ============================================================= 11. Project-tier targeting (T6)
# T4's exact output shape (`_resolve_tier_composition`, collector.py) as a STATIC dict
# literal — never a live collector run, so this module stays insulated from T5 (which
# lands `tier_composition`'s sibling settings/hooks/MCP fields concurrently). One
# collision per shadow surface exercises every branch: skill (operator wins -> project
# marked "dark"), agent (project wins -> "override"), plus a project-only command "add"
# and a project rule "add" on the union surface.
TIER_COMPOSITION_FIXTURE = {
    "nodes": [
        {"surface": "skill", "name": "review", "tier": "operator", "path": "skills/review/SKILL.md",
         "status": "effective", "shadowed_by": None},
        {"surface": "skill", "name": "review", "tier": "project", "path": ".claude/skills/review/SKILL.md",
         "status": "shadowed", "shadowed_by": {"tier": "operator", "path": "skills/review/SKILL.md"}},
        {"surface": "command", "name": "deploy", "tier": "project", "path": ".claude/commands/deploy.md",
         "status": "effective", "shadowed_by": None},
        {"surface": "agent", "name": "auditor", "tier": "operator", "path": "agents/auditor.md",
         "status": "shadowed", "shadowed_by": {"tier": "project", "path": ".claude/agents/auditor.md"}},
        {"surface": "agent", "name": "auditor", "tier": "project", "path": ".claude/agents/auditor.md",
         "status": "effective", "shadowed_by": None},
        {"surface": "rule", "name": "x", "tier": "project", "path": ".claude/rules/x.md",
         "status": "effective", "shadowed_by": None},
    ],
    "surfaces": {
        "skill": {"merge": "shadow", "winner_tier": "operator", "adds": 0, "overrides": 0, "dark": 1},
        "command": {"merge": "shadow", "winner_tier": "operator", "adds": 1, "overrides": 0, "dark": 0},
        "agent": {"merge": "shadow", "winner_tier": "project", "adds": 0, "overrides": 1, "dark": 0},
        "rule": {"merge": "union", "winner_tier": None, "adds": 1, "overrides": 0, "dark": 0},
    },
    "participating_surfaces": ["agent", "command", "rule", "skill"],
}


def test_tier_summary_band_renders_rollup_per_surface_and_dark_callout(tmp_path):
    doc = _minimal_doc()
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    out_dir = tmp_path / "tier_summary"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="tier-summary"' in text
    # rollup: adds = command(1) + rule(1) = 2; overrides = agent(1) = 1; dark = skill(1) = 1
    assert "project adds 2 / overrides 1 / 1 dark" in text
    # per-surface breakdown names every participating surface
    for surface in ("skill", "command", "agent", "rule"):
        assert f'<span class="tier-surface">{surface}</span>' in text
    # dark-skill callout: the shadowed project "review" skill, flagged as never-runs
    assert "Dark project skills &amp; commands" in text
    assert "skill:review" in text
    assert "defined but never runs" in text
    assert "shadowed by operator skills/review/SKILL.md" in text
    # confined to the Overview view (not duplicated elsewhere)
    import re
    ov = re.search(r'<section id="view-overview".*?</section>', text, re.S)
    assert ov is not None and 'id="tier-summary"' in ov.group(0)


# Finding D (P3): the "Dark project skills" callout also lists dark COMMANDS (both
# skills and commands resolve operator-wins, so both can appear under it) — a fixture
# whose only dark entry is a colliding project command must not render under a heading
# that claims "skills" only.
COMMAND_ONLY_DARK_FIXTURE = {
    "nodes": [
        {"surface": "command", "name": "deploy", "tier": "operator", "path": "commands/deploy.md",
         "status": "effective", "shadowed_by": None},
        {"surface": "command", "name": "deploy", "tier": "project", "path": ".claude/commands/deploy.md",
         "status": "shadowed", "shadowed_by": {"tier": "operator", "path": "commands/deploy.md"}},
    ],
    "surfaces": {
        "command": {"merge": "shadow", "winner_tier": "operator", "adds": 0, "overrides": 0, "dark": 1},
    },
    "participating_surfaces": ["command"],
}


def test_dark_callout_heading_covers_dark_commands_not_just_skills(tmp_path):
    doc = _minimal_doc()
    doc["tier_composition"] = COMMAND_ONLY_DARK_FIXTURE
    out_dir = tmp_path / "tier_summary_dark_command"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # the only dark entry is a command — the heading must not claim "skills" only
    assert "Dark project skills &amp; commands" in text
    assert "Dark project skills</h3>" not in text
    assert "command:deploy" in text


def test_tier_summary_band_absent_without_tier_composition(tmp_path):
    """Back-compat (C15, T6-owned for render): an old-shape sidecar with no
    `tier_composition` key at all must render with zero errors and simply omit the
    band — never a KeyError, never a stray empty card."""
    doc = _minimal_doc()
    assert "tier_composition" not in doc
    out_dir = tmp_path / "tier_summary_absent"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="tier-summary"' not in text
    assert "project adds" not in text


def test_treemap_cells_wrapped_in_tier_bearing_group():
    tree = {"cells": [
        {"path": "op.md", "node_key": "always_loaded:op.md", "size": 10, "tier": "operator",
         "x": "0.00", "y": "0.00", "w": "80.00", "h": "40.00", "fill": "#000"},
        {"path": "proj.md", "node_key": "always_loaded:proj.md", "size": 10, "tier": "project",
         "x": "0.00", "y": "40.00", "w": "80.00", "h": "40.00", "fill": "#000"},
    ], "canvas_w": 100.0, "canvas_h": 100.0}
    svg = rh._render_treemap_svg(tree, {}, "t")
    assert '<g class="tier-node tier-operator"><rect' in svg
    assert '<g class="tier-node tier-project"><rect' in svg
    import re
    op_cell = re.search(r'<g class="tier-node tier-operator">.*?data-node-key="always_loaded:op\.md".*?</g>',
                         svg, re.S)
    proj_cell = re.search(r'<g class="tier-node tier-project">.*?data-node-key="always_loaded:proj\.md".*?</g>',
                           svg, re.S)
    assert op_cell is not None and proj_cell is not None


def test_ladder_rows_wrapped_in_tier_bearing_group():
    tree = {"cells": [
        {"path": "op.md", "node_key": "always_loaded:op.md", "size": 10, "tier": "operator",
         "x": "0.00", "y": "0.00", "w": "80.00", "h": "40.00", "fill": "#000"},
        {"path": "proj.md", "node_key": "always_loaded:proj.md", "size": 5, "tier": "project",
         "x": "0.00", "y": "40.00", "w": "80.00", "h": "40.00", "fill": "#000"},
    ], "canvas_w": 100.0, "canvas_h": 100.0}
    svg = rh._render_ladder_svg(tree, {}, "l")
    import re
    op_row = re.search(r'<g class="tier-node tier-operator">.*?data-node-key="always_loaded:op\.md".*?</g>',
                        svg, re.S)
    proj_row = re.search(r'<g class="tier-node tier-project">.*?data-node-key="always_loaded:proj\.md".*?</g>',
                          svg, re.S)
    assert op_row is not None and proj_row is not None


def test_treemap_cell_defaults_to_operator_when_tier_key_absent():
    """C15 back-compat: a cell dict with no `tier` key at all (the pre-T6 / non-compose
    shape) must default to operator, never crash on a missing key."""
    tree = {"cells": [
        {"path": "a.md", "node_key": "always_loaded:a.md", "size": 10,
         "x": "0.00", "y": "0.00", "w": "80.00", "h": "40.00", "fill": "#000"},
    ], "canvas_w": 100.0, "canvas_h": 100.0}
    svg = rh._render_treemap_svg(tree, {}, "t")
    assert '<g class="tier-node tier-operator"><rect' in svg
    assert "tier-project" not in svg


@pytest.mark.parametrize("payload", ["evil", '"><script>alert(1)</script>', "operator--><g>x</g>"])
def test_tier_value_normalizes_adversarial_input_to_operator(payload):
    """Untrusted project-tier data (T3's threat model) must never place a raw `tier`
    string into a CSS class attribute — only the two known enum members are ever
    emitted; anything else (including an injection attempt) defaults to operator."""
    tree = {"cells": [
        {"path": "a.md", "node_key": "always_loaded:a.md", "size": 10, "tier": payload,
         "x": "0.00", "y": "0.00", "w": "80.00", "h": "40.00", "fill": "#000"},
    ], "canvas_w": 100.0, "canvas_h": 100.0}
    svg = rh._render_treemap_svg(tree, {}, "t")
    assert payload not in svg
    assert '<g class="tier-node tier-operator"><rect' in svg


def test_length_flags_table_rows_tagged_with_tier_and_project_badge(tmp_path):
    doc = _minimal_doc()
    doc["instruction_length_flags"] = [
        {"path": ".claude/skills/wide/SKILL.md", "lines": 250, "threshold": 200,
         "evidence": "VERIFIED", "tier": "project"},
        {"path": "skills/wide2/SKILL.md", "lines": 240, "threshold": 200, "evidence": "VERIFIED"},
    ]
    out_dir = tmp_path / "tier_flags"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    hyg = re.search(r'<section id="view-hygiene".*?</section>', text, re.S).group(0)
    proj_row = re.search(r'<tr class="tier-node tier-project">.*?</tr>', hyg, re.S)
    assert proj_row is not None
    assert '.claude/skills/wide/SKILL.md' in proj_row.group(0)
    assert '<span class="badge tier-project">project</span>' in proj_row.group(0)
    op_row = re.search(r'<tr class="tier-node tier-operator">.*?</tr>', hyg, re.S)
    assert op_row is not None
    assert "skills/wide2/SKILL.md" in op_row.group(0)
    assert '<span class="badge tier-project">project</span>' not in op_row.group(0)


def test_tier_tokens_present_in_light_and_dark_static_style():
    style = rh.STATIC_STYLE
    assert style.count("--tier-operator:var(--muted)") == 4
    assert style.count("--tier-project:#0e7490") == 2   # light theme (base :root + [data-theme=light])
    assert style.count("--tier-project:#22d3ee") == 2   # dark theme (media dark + [data-theme=dark])


def _theme_block(css, opener):
    """Return the declaration text of a theme variable block, given its exact opener."""
    i = css.index(opener)
    return css[i + len(opener):css.index("}", i)]


def _theme_tokens(block):
    return dict(d.split(":", 1) for d in block.split(";") if d.startswith("--"))


def test_light_and_dark_theme_blocks_each_stay_in_sync():
    """Rejects the single most likely dark-mode regression in this file: the dark theme is
    declared TWICE (@media prefers-color-scheme, and :root[data-theme="dark"]) and the light
    theme TWICE (:root, and :root[data-theme="light"]). Editing or adding a token in only one
    member of a pair makes the manual theme toggle render differently from the same theme
    picked up from the OS -- a divergence nothing else in the suite would catch."""
    css = rh.STATIC_STYLE
    # --r and --mono are declared once, only in the base :root{} block, on purpose: they are
    # theme-invariant (same border-radius and font stack in both themes), and every
    # [data-theme=...] selector still resolves them from :root{} via normal CSS custom
    # property inheritance. They are excluded from the pairwise sync check below because
    # they are the one deliberate exception to "every token appears in all four blocks".
    invariant_tokens = {"--r", "--mono"}
    light_a = {k: v for k, v in _theme_tokens(_theme_block(css, ":root{")).items()
               if k not in invariant_tokens}
    light_b = _theme_tokens(_theme_block(css, ':root[data-theme="light"]{'))
    dark_a = {k: v for k, v in _theme_tokens(
        _theme_block(css, "@media (prefers-color-scheme: dark){:root{")).items()
        if k not in invariant_tokens}
    dark_b = _theme_tokens(_theme_block(css, ':root[data-theme="dark"]{'))
    assert light_a == light_b, "light :root and [data-theme=light] diverged"
    assert dark_a == dark_b, "dark @media and [data-theme=dark] diverged"
    assert set(light_a) == set(dark_a), "a token exists in one theme but not the other"


def test_no_unthemed_color_literal_in_the_stylesheet():
    """Rejects a hardcoded hex/rgb() creeping into a rule outside the four theme-variable
    blocks -- exactly the class of defect that makes a control legible in light mode and
    invisible in dark. Every remaining literal must be on this allowlist with a stated
    reason; a new one fails the test until it is either mapped to a variable or added here
    deliberately."""
    css = rh.STATIC_STYLE
    for opener in (":root{", ':root[data-theme="light"]{',
                   "@media (prefers-color-scheme: dark){:root{", ':root[data-theme="dark"]{'):
        block = _theme_block(css, opener)
        css = css.replace(block, "", 1)
    found = set(re.findall(r"#[0-9a-fA-F]{3,8}\b|rgba?\([^)]*\)", css))
    allowed = {
        # .friction-badge: paint-order:stroke halo. White fill over a black outline must read
        # over ANY cell fill in EITHER theme -- these two are load-bearing BECAUSE they do
        # not theme.
        "#fff", "#000",
        # HEAT_RAMP (render_html.py:89), appended via _HEAT_CSS. A sequential magnitude ramp
        # emitted identically into the cell stroke and the legend swatch; theming it would
        # desynchronize the legend from the data it explains.
        "#FCAE91", "#FB6A4A", "#DE2D26", "#A50F15",
    }
    assert found <= allowed, f"unthemed color literal(s) in the stylesheet: {sorted(found - allowed)}"


def test_svg_fill_fallbacks_use_the_accent_token(tmp_path):
    """Rejects a regression the stylesheet scan structurally cannot see: the defensive
    `c.get("fill", ...)` fallbacks in _render_treemap_svg and _render_ladder_svg live in
    Python f-strings OUTSIDE STATIC_STYLE, so test_no_unthemed_color_literal_in_the_stylesheet
    never reads them. Pre-TRK-021 they were the hardcoded hex #56b4e9 -- legible in light
    mode, unthemed in dark. Both builders always set "fill" today, so the fallback is
    unreachable dead code; this pins it to the themed accent token anyway, because a future
    builder change could make it live without any test noticing."""
    src = Path(rh.__file__).with_suffix(".py").read_text(encoding="utf-8")
    assert '"#56b4e9"' not in src
    assert src.count('c.get("fill", "var(--accent)")') == 2


def _wcag_contrast(hex_a, hex_b):
    def lum(h):
        h = h.lstrip("#")
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [(x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4) for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    la, lb = lum(hex_a), lum(hex_b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_expand_all_pressed_state_meets_wcag_contrast_in_both_themes():
    """Rejects the exact defect Codex found in the first TRK-021 review round: the pressed
    expand-all rule composed var(--accent) text on var(--accent-soft) background, which in
    the light theme is #6366f1 on #e5e7fb -- about 3.65:1, under the 4.5:1 WCAG AA floor
    for the control's 0.85rem text. Every literal-scan audit missed it because the rule
    contains no hex literal at all; this test therefore resolves the rule's var() references
    against BOTH parsed theme palettes and computes the actual ratio. Changing this value
    requires a spec change (WCAG 2.1 AA 1.4.3: 4.5:1 for normal-size text)."""
    css = rh.STATIC_STYLE
    rule = _css_decls(css, '#expand-all[aria-pressed="true"]')
    assert rule != "", "pressed-state rule missing"
    m = re.search(r"(?:^|;)color:var\((--[a-z-]+)\)", rule)
    assert m, "pressed-state rule must set color via a theme token"
    fg_token = m.group(1)
    mb = re.search(r"background:var\((--[a-z-]+)\)", rule)
    assert mb, "pressed-state rule must set background via a theme token"
    bg_token = mb.group(1)
    light = _theme_tokens(_theme_block(css, ":root{"))
    dark = _theme_tokens(_theme_block(css, ':root[data-theme="dark"]{'))
    for theme_name, tokens in (("light", light), ("dark", dark)):
        ratio = _wcag_contrast(tokens[fg_token], tokens[bg_token])
        assert ratio >= 4.5, (
            f"{theme_name}: {fg_token} on {bg_token} = {ratio:.2f}:1, below WCAG AA 4.5:1")


def test_one_executable_script_and_csp_hash_reconciles_with_tier_composition(tmp_path):
    doc = _minimal_doc()
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    out_dir = tmp_path / "tier_csp"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    p = _ExternalRefParser()
    p.feed(text)
    assert p.tag_counts.get("style", 0) == 1
    assert p.on_handlers == []
    assert p.style_attrs == []
    import re
    exe_scripts = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe_scripts) == 1
    m = re.search(r"style-src 'sha256-([^']+)'; script-src 'sha256-([^']+)'", text)
    assert m is not None
    assert m.group(1) == rh._csp_hash(rh.STATIC_STYLE)
    assert m.group(2) == rh._csp_hash(rh.STATIC_SCRIPT)


def test_byte_determinism_with_tier_composition_across_pythonhashseed(tmp_path):
    doc = _minimal_doc()
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    doc["instruction_length_flags"] = [
        {"path": ".claude/skills/wide/SKILL.md", "lines": 250, "threshold": 200,
         "evidence": "VERIFIED", "tier": "project"},
    ]
    out_dir = tmp_path / "tier_det"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    outs = []
    for seed in ("0", "1"):
        p = run_render(out_dir, "--date", "2026-07-15", "--no-friction",
                       env={**os.environ, "PYTHONHASHSEED": seed})
        assert p.returncode == 0, p.stderr
        outs.append((out_dir / "harness-map-2026-07-15.html").read_bytes())
    assert outs[0] == outs[1]


# ============================================================= 12. Tier filter toggle (T7)
def test_tier_filter_control_renders_with_single_roving_tabstop(tmp_path):
    """M3/P2-7: a three-state radiogroup (All / Operator only / Project only), gated on
    `tier_composition` presence (same gate T6 used for the summary band). TRUE roving
    tabindex from the start: exactly one button is a tabstop ("All", checked by
    default), the other two are removed from tab order entirely -- not just an
    `aria-selected` flip (the pre-existing `.view-switch` tablist's own gap, P2-7)."""
    doc = _minimal_doc()
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    out_dir = tmp_path / "tier_filter_control"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    group = re.search(r'<div class="tier-filter" role="radiogroup" aria-label="Tier filter">'
                       r'.*?</div>', text, re.S)
    assert group is not None
    btns = re.findall(r'<button class="tier-filter-btn"[^>]*>', group.group(0))
    assert len(btns) == 3
    assert [b for b in btns if 'data-tier-filter="all"' in b][0].count('tabindex="0"') == 1
    assert [b for b in btns if 'data-tier-filter="all"' in b][0].count('aria-checked="true"') == 1
    for filt in ("operator-only", "project-only"):
        btn = [b for b in btns if f'data-tier-filter="{filt}"' in b][0]
        assert 'tabindex="-1"' in btn
        assert 'aria-checked="false"' in btn
    assert sum(b.count('tabindex="0"') for b in btns) == 1


def test_tier_filter_control_absent_without_tier_composition(tmp_path):
    """C15 back-compat: a non-compose (no `tier_composition`) sidecar has nothing to
    filter -- the control, and every trace of it, must be absent so the controls bar
    stays exactly as it rendered pre-T7."""
    doc = _minimal_doc()
    assert "tier_composition" not in doc
    out_dir = tmp_path / "tier_filter_absent"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'class="tier-filter"' not in text
    assert 'role="radiogroup"' not in text
    assert "data-tier-filter" not in text


def test_tier_filter_dim_css_targets_wrapper_and_is_ordered_after_heat_css():
    """P2-8: the dim rule must target the tier-bearing WRAPPER (`.tier-node`, not
    `.fhN`/`.heatable` which live on the child rect/bar) -- opacity then composes
    automatically via nested compositing instead of fighting the heat rules for the
    same element. Also asserts the dim rules are textually the LAST declarations in
    the stylesheet (appended after `_HEAT_CSS`), so a later-appended heat rule can
    never silently win an equal-specificity tie."""
    style = rh.STATIC_STYLE
    dim_op = "body.tier-project-only .tier-node.tier-operator{opacity:.25}"
    dim_proj = "body.tier-operator-only .tier-node.tier-project{opacity:.25}"
    assert dim_op in style
    assert dim_proj in style
    heat_fh4_idx = style.index("body.friction-on .fh4{")
    assert style.index(dim_op) > heat_fh4_idx
    assert style.index(dim_proj) > heat_fh4_idx
    # the selector never touches .fhN/.heatable directly (would fight the heat rule
    # for the SAME element instead of composing via the parent wrapper)
    assert ".fh" not in dim_op and ".fh" not in dim_proj
    assert ".heatable" not in dim_op and ".heatable" not in dim_proj


def test_tier_filter_triple_interaction_treemap_composes_heat_and_length_crit(tmp_path):
    """Triple-interaction (mandatory): a node that is simultaneously tier-project,
    friction-heated, AND length-critical must render as ONE `tier-node tier-project`
    wrapper containing the heated (`fhN`) rect and the length-crit ring + marker as
    siblings -- proving a single opacity dim on the wrapper covers all three at once,
    never independently.

    T12: uses the REAL `project_rule` category (`_walk_project_tier`'s actual output,
    collector.py:619) at a REAL project-relative path (`.claude/rules/proj.md` -- the
    shape `_project_file_entry` emits, not the operator-shaped `rules/proj.md` this
    test used pre-T12, which happened to already work because the P1 bug hadn't been
    fixed AND the P2 node_key collision hadn't been introduced yet). The friction
    decision below joins by BARE basename (`proj.md`, no `/`) -- T12's P2 fix makes a
    PATH-bearing ref operator-tier-only (`_canonical_ref_candidates` never emits a
    `project:`-keyed candidate, by design: telemetry paths are always operator-root-
    relative), but the basename index (`build_node_index`) is tier-blind by
    construction (`_basename_of_node_key` strips everything up to the last `/`, and
    the `project:` discriminator sits before the final path segment) -- so a bare-name
    join still finds the project node. This proves both halves of P2 at once: a
    path-bearing operator-style ref would NOT resolve here (see the dedicated
    node_key-disambiguation test below), while a basename ref still can."""
    doc = _minimal_doc(extra_files=[
        {"path": ".claude/rules/proj.md", "category": "project_rule", "words": 30, "lines": 700,
         "tokens_est": 40, "evidence": "VERIFIED", "tier": "project"},
    ])
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    doc["instruction_length_flags"] = [
        {"path": ".claude/rules/proj.md", "lines": 700, "threshold": 200, "evidence": "VERIFIED",
         "tier": "project"},
    ]
    out_dir = tmp_path / "triple_treemap"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions_file = out_dir / "decisions.jsonl"
    decisions_file.write_text(json.dumps({"date": "2026-07-01", "component": "proj.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    weight = re.search(r'<section id="view-weight".*?</section>', text, re.S).group(0)
    # exactly one project-tier cell exists in this fixture, so the FIRST (treemap)
    # `tier-node tier-project` group is unambiguous and closes at its own `</g>`
    g = re.search(r'<g class="tier-node tier-project">.*?</g>', weight, re.S)
    assert g is not None
    cell = g.group(0)
    node_key = "always_loaded:project:.claude/rules/proj.md"
    assert f'data-node-key="{node_key}"' in cell
    assert re.search(r'class="cell-rect heatable fh\d"', cell)
    assert 'class="length-crit-ring"' in cell
    assert 'class="length-crit-marker"' in cell
    # both the treemap AND ladder panels render this cell (T6's existing 2-ring invariant)
    assert weight.count(f'data-node-key="{node_key}"') >= 2


def test_tier_filter_triple_interaction_table_row_crit_and_project_survive_dim(tmp_path):
    """Extends T6's table coverage gap (a row that is BOTH length-critical AND
    project-tier was only indirectly tested there via the non-clobbering `border-left`
    signal). Assert the `tr:has(.pill-critical)` crit background rule and the
    `.tier-node.tier-project` dim-target class land on the SAME `<tr>` -- the crit
    background is never overridden/removed by the tier dim, only uniformly faded
    (opacity on the row, not a property collision)."""
    doc = _minimal_doc()
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    doc["instruction_length_flags"] = [
        {"path": ".claude/skills/wide/SKILL.md", "lines": 700, "threshold": 200,
         "evidence": "VERIFIED", "tier": "project"},
    ]
    out_dir = tmp_path / "triple_table"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    hyg = re.search(r'<section id="view-hygiene".*?</section>', text, re.S).group(0)
    proj_row = re.search(r'<tr class="tier-node tier-project">.*?</tr>', hyg, re.S)
    assert proj_row is not None
    row = proj_row.group(0)
    assert '.claude/skills/wide/SKILL.md' in row
    assert 'class="pill pill-critical">critical</span>' in row
    # the CSS invariant this row depends on for its crit signal to survive a tier dim:
    # `tr:has(.pill-critical)` paints the background, `.tier-node.tier-project` is the
    # dim-target class on the same element -- opacity fades both together, never
    # strips the background rule itself.
    assert "tr:has(.pill-critical){background:var(--crit-bg)}" in rh.STATIC_STYLE


def test_tier_filter_roving_tabindex_js_present_in_rendered_script(tmp_path):
    """The roving-tabindex mechanism (single tabstop, Arrow moves focus+tabindex+
    selection together) must exist in the ACTUAL embedded script of a real render --
    not merely in the Python source. Mirrors the existing `ArrowRight`/`ArrowLeft`
    tablist assertion for `.view-switch`."""
    doc = _minimal_doc()
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    out_dir = tmp_path / "tier_roving_js"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "querySelector('.tier-filter')" in text
    assert "setAttribute('tabindex'" in text
    assert "tier-operator-only" in text and "tier-project-only" in text
    assert "ArrowRight" in text and "ArrowLeft" in text and "ArrowUp" in text and "ArrowDown" in text


def test_tier_filter_zero_inline_style_and_on_handlers_with_control_present(tmp_path):
    doc = _minimal_doc()
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    out_dir = tmp_path / "tier_filter_clean"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'class="tier-filter"' in text
    p = _ExternalRefParser()
    p.feed(text)
    assert p.on_handlers == []
    assert p.style_attrs == []
    assert p.tag_counts.get("style", 0) == 1
    import re
    exe_scripts = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe_scripts) == 1
    m = re.search(r"style-src 'sha256-([^']+)'; script-src 'sha256-([^']+)'", text)
    assert m.group(1) == rh._csp_hash(rh.STATIC_STYLE)
    assert m.group(2) == rh._csp_hash(rh.STATIC_SCRIPT)


def test_byte_determinism_with_tier_filter_and_triple_interaction_across_pythonhashseed(tmp_path):
    # T12: real `project_rule` category + real `.claude/rules/` path (see the sibling
    # triple-interaction test above for why -- the synthetic `category: "rule"` /
    # `rules/proj.md` shape this test used pre-T12 never exercised the P1/P2 fixes).
    doc = _minimal_doc(extra_files=[
        {"path": ".claude/rules/proj.md", "category": "project_rule", "words": 30, "lines": 700,
         "tokens_est": 40, "evidence": "VERIFIED", "tier": "project"},
    ])
    doc["tier_composition"] = TIER_COMPOSITION_FIXTURE
    doc["instruction_length_flags"] = [
        {"path": ".claude/rules/proj.md", "lines": 700, "threshold": 200, "evidence": "VERIFIED",
         "tier": "project"},
    ]
    out_dir = tmp_path / "tier_filter_det"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions_file = out_dir / "decisions.jsonl"
    decisions_file.write_text(json.dumps({"date": "2026-07-01", "component": "proj.md"}) + "\n")
    outs = []
    for seed in ("0", "1"):
        p = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions_file),
                       env={**os.environ, "PYTHONHASHSEED": seed})
        assert p.returncode == 0, p.stderr
        outs.append((out_dir / "harness-map-2026-07-15.html").read_bytes())
    assert outs[0] == outs[1]


def test_tier_filter_tokens_use_existing_theme_vars_no_new_theme_debt():
    """The dim/control CSS reuses existing tokens (`--muted`, `--surface-2`, `--ink`,
    `--line`) that are already defined for both light and dark -- no NEW token was
    introduced that would need its own light/dark duplication (regression guard
    against silently shipping a light-only or dark-only color)."""
    import re
    style = rh.STATIC_STYLE
    filter_rule = re.search(r"\.tier-filter\{[^}]*\}", style).group(0)
    btn_rule = re.search(r"button\.tier-filter-btn\{[^}]*\}", style).group(0)
    checked_rule = re.search(r'button\.tier-filter-btn\[aria-checked="true"\]\{[^}]*\}', style).group(0)
    assert "var(--line)" in filter_rule
    assert "var(--muted)" in btn_rule
    assert "var(--surface-2)" in checked_rule and "var(--ink)" in checked_rule


# ============================================================= 12b. project-tier category + node_key
# fixes (T12). `_walk_project_tier` (collector.py) emits three real always-loaded
# categories -- `project_rule`, `project_claude_local_md`, `project_claude_md_nested`
# -- that T6/T7's own tests never used (they hand-crafted `category: "rule"` instead,
# which happened to already be in `ALWAYS_CATEGORIES`), so the P1 gap (the real
# categories missing from the allowlist) slipped through. Real category names +
# real project-relative path shapes (`.claude/rules/*.md`, matching
# `_project_file_entry`) throughout this section, per the same lesson.
def test_always_categories_extended_with_three_project_tier_entries():
    cats = dict(rh.ALWAYS_CATEGORIES)
    assert cats["project_rule"] == "Project rules"
    assert cats["project_claude_local_md"] == "CLAUDE.local.md"
    assert cats["project_claude_md_nested"] == "CLAUDE.md (nested)"
    # still a FIXED tuple (§4.4 determinism -- never sorted(set(...)))
    assert isinstance(rh.ALWAYS_CATEGORIES, tuple)
    assert len(rh.ALWAYS_CATEGORIES) == 9


def test_al_node_key_operator_unchanged_project_gets_tier_segment():
    """Direct unit coverage of the P2 fix's core function: operator format is
    byte-identical to pre-T12 (`tier` absent or "operator"); project gains the
    `project:` discriminator; an unrecognized/untrusted tier value (T3's threat
    model -- an adversarial sidecar could carry anything) defaults safely to
    operator, never lets an arbitrary string ride into the node_key."""
    assert rh._al_node_key("CLAUDE.md") == "always_loaded:CLAUDE.md"
    assert rh._al_node_key("CLAUDE.md", "operator") == "always_loaded:CLAUDE.md"
    assert rh._al_node_key("CLAUDE.md", "project") == "always_loaded:project:CLAUDE.md"
    assert rh._al_node_key("CLAUDE.md", "bogus-tier") == "always_loaded:CLAUDE.md"


def test_project_tier_categories_render_as_treemap_cells_and_ladder_rows(tmp_path):
    """P1 fix, end-to-end: a compose fixture carrying all three previously-missing
    real categories -- each must render exactly twice (treemap cell + ladder row,
    T6's existing 2-ring invariant), wrapped `tier-node tier-project` (accent, never
    muted operator)."""
    doc = _minimal_doc(extra_files=[
        {"path": ".claude/rules/proj-rule.md", "category": "project_rule", "words": 10,
         "lines": 20, "tokens_est": 15, "evidence": "VERIFIED", "tier": "project"},
        {"path": "CLAUDE.local.md", "category": "project_claude_local_md", "words": 10,
         "lines": 20, "tokens_est": 16, "evidence": "VERIFIED", "tier": "project"},
        {"path": "sub/CLAUDE.md", "category": "project_claude_md_nested", "words": 10,
         "lines": 20, "tokens_est": 17, "evidence": "VERIFIED", "tier": "project"},
    ])
    out_dir = tmp_path / "project_categories"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    weight = re.search(r'<section id="view-weight".*?</section>', text, re.S).group(0)
    for path in (".claude/rules/proj-rule.md", "CLAUDE.local.md", "sub/CLAUDE.md"):
        node_key = f"always_loaded:project:{path}"
        assert weight.count(f'data-node-key="{node_key}"') == 2, path
        g = re.search(r'<g class="tier-node tier-project">.*?data-node-key="' +
                       re.escape(node_key) + r'".*?</g>', weight, re.S)
        assert g is not None, f"{path} not wrapped tier-node tier-project"


def test_operator_and_project_claude_md_get_distinct_node_keys(tmp_path):
    """P2 fix: operator `CLAUDE.md` (category `claude_md`) and project `CLAUDE.md`
    (category `project_claude_md`) both give `path == "CLAUDE.md"` -- pre-T12 both
    produced the identical `data-node-key="always_loaded:CLAUDE.md"`."""
    doc = _minimal_doc(extra_files=[
        {"path": "CLAUDE.md", "category": "project_claude_md", "words": 10, "lines": 20,
         "tokens_est": 18, "evidence": "VERIFIED", "tier": "project"},
    ])
    out_dir = tmp_path / "claude_md_collision"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    weight = re.search(r'<section id="view-weight".*?</section>', text, re.S).group(0)
    assert weight.count('data-node-key="always_loaded:CLAUDE.md"') == 2
    assert weight.count('data-node-key="always_loaded:project:CLAUDE.md"') == 2


def test_friction_join_matches_operator_claude_md_not_project_counterpart(tmp_path):
    """Regression guard: with BOTH an operator and project CLAUDE.md present, a
    friction decision naming the bare `CLAUDE.md` basename must heat ONLY the
    operator node -- the project node (same basename, different tier, and now a
    DISTINCT node_key per P2) must never pick up the same heat via node_key
    collision (the pre-T12 bug) -- proving the friction-join stays operator-only and
    correctly attributed once tiers are disambiguated."""
    doc = _minimal_doc(extra_files=[
        {"path": "CLAUDE.md", "category": "project_claude_md", "words": 10, "lines": 20,
         "tokens_est": 18, "evidence": "VERIFIED", "tier": "project"},
    ])
    out_dir = tmp_path / "friction_no_cross_tier"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    decisions_file = out_dir / "decisions.jsonl"
    decisions_file.write_text(json.dumps({"date": "2026-07-01", "component": "CLAUDE.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15", "--decisions-file", str(decisions_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    weight = re.search(r'<section id="view-weight".*?</section>', text, re.S).group(0)
    op_title = re.search(r'data-node-key="always_loaded:CLAUDE\.md"><title>([^<]*)</title>', weight)
    proj_title = re.search(r'data-node-key="always_loaded:project:CLAUDE\.md"><title>([^<]*)</title>',
                            weight)
    assert op_title is not None and proj_title is not None
    assert "churn: 1 friction record" in op_title.group(1)
    assert "churn: none recorded" in proj_title.group(1)


def test_non_compose_doc_emits_no_project_tier_markup_for_new_categories(tmp_path):
    """Non-compose byte-identical, in spirit (C15): a sidecar with zero project-tier
    files must emit zero `tier-project`/`project:`-keyed markup for the 3 new
    categories -- `_tokens_treemap`'s existing `if tokens <= 0: continue` skip means
    an empty category never produces a group or a cell."""
    doc = _minimal_doc()
    out_dir = tmp_path / "non_compose_new_categories"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # `tier-project` alone is too broad -- it's a static CSS class/token name present
    # in every render (`.tier-node.tier-project{...}`, `--tier-project` custom
    # property) regardless of doc content; the actual per-node markup is the quoted
    # `class="tier-node tier-project"` wrapper, which only a real project-tier cell
    # emits.
    assert 'class="tier-node tier-project"' not in text
    assert "always_loaded:project:" not in text


def test_project_categories_render_clean_csp_and_light_dark(tmp_path):
    doc = _minimal_doc(extra_files=[
        {"path": ".claude/rules/proj-rule.md", "category": "project_rule", "words": 10,
         "lines": 20, "tokens_est": 15, "evidence": "VERIFIED", "tier": "project"},
    ])
    out_dir = tmp_path / "project_categories_clean"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    p = _ExternalRefParser()
    p.feed(text)
    assert p.on_handlers == []
    assert p.style_attrs == []
    assert p.tag_counts.get("style", 0) == 1
    import re
    exe_scripts = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe_scripts) == 1
    m = re.search(r"style-src 'sha256-([^']+)'; script-src 'sha256-([^']+)'", text)
    assert m.group(1) == rh._csp_hash(rh.STATIC_STYLE)
    assert m.group(2) == rh._csp_hash(rh.STATIC_SCRIPT)
    assert "prefers-color-scheme: dark" in rh.STATIC_STYLE


def test_project_categories_byte_identical_across_pythonhashseed(tmp_path):
    doc = _minimal_doc(extra_files=[
        {"path": ".claude/rules/proj-rule.md", "category": "project_rule", "words": 10,
         "lines": 20, "tokens_est": 15, "evidence": "VERIFIED", "tier": "project"},
        {"path": "CLAUDE.local.md", "category": "project_claude_local_md", "words": 10,
         "lines": 20, "tokens_est": 16, "evidence": "VERIFIED", "tier": "project"},
        {"path": "sub/CLAUDE.md", "category": "project_claude_md_nested", "words": 10,
         "lines": 20, "tokens_est": 17, "evidence": "VERIFIED", "tier": "project"},
    ])
    out_dir = tmp_path / "project_categories_det"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    outs = []
    for seed in ("0", "1"):
        p = run_render(out_dir, "--date", "2026-07-15", "--no-friction",
                       env={**os.environ, "PYTHONHASHSEED": seed})
        assert p.returncode == 0, p.stderr
        outs.append((out_dir / "harness-map-2026-07-15.html").read_bytes())
    assert outs[0] == outs[1]


def test_maximal_two_tier_fixture_end_to_end_render(fake_harness, tmp_path):
    """T9 built `_build_two_tier_maximal_fixture` as ONE end-to-end exercise and
    consumed it at the collector-doc layer (`test_maximal_two_tier_fixture_end_to_end_
    collector_doc`, test_collector.py) but never wrote the matching render-layer
    assertion here -- exactly the P1/P2 gap this task closes: the fixture's real
    project rule (`.claude/rules/only-project.md`) would have been silently dropped
    from the treemap/ladder pre-fix (P1), and its project `CLAUDE.md` would have
    collided node_keys with the operator's `CLAUDE.md` pre-fix (P2). Runs the REAL
    collector (`--compose`) over the REAL fixture and feeds its actual output
    straight to the renderer -- no hand-crafted doc anywhere in this test."""
    proj, home = _build_two_tier_maximal_fixture(fake_harness, tmp_path)
    doc = run_collector(fake_harness, "--compose", project_root=proj, env={"HOME": str(home)})
    out_dir = tmp_path / "maximal_render"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    weight = re.search(r'<section id="view-weight".*?</section>', text, re.S).group(0)
    # P1: the fixture's real project-only UNION rule (category `project_rule`)
    # renders -- twice (treemap + ladder), tier-project wrapped
    proj_rule_key = "always_loaded:project:.claude/rules/only-project.md"
    assert weight.count(f'data-node-key="{proj_rule_key}"') == 2
    assert re.search(r'<g class="tier-node tier-project">.*?data-node-key="' +
                      re.escape(proj_rule_key) + r'".*?</g>', weight, re.S)
    # P2: operator + project CLAUDE.md both render, with DISTINCT node_keys
    assert weight.count('data-node-key="always_loaded:CLAUDE.md"') == 2
    assert weight.count('data-node-key="always_loaded:project:CLAUDE.md"') == 2
    # secret-safety end-to-end (T5's collector-doc leak test, T9 spec) also holds
    # once this same fixture reaches the RENDER layer
    for secret in _SECRET_SENTINELS:
        assert secret not in text, f"raw secret leaked into rendered HTML: {secret}"


# ============================================================= 13. Composed settings display (T7b)
# T5's exact output shape (`doc["composed_settings"]`) as a STATIC dict literal — never a
# live collector run, same insulation posture as `TIER_COMPOSITION_FIXTURE` above. The
# "leaky" MCP server also carries raw `env`/`headers` keys T5 itself would NEVER emit
# (only `env_keys`/`header_keys` NAME lists survive T5's own redaction) — this fixture
# probes that the RENDER layer never touches those hypothetical raw fields either, even
# if a malformed/hand-edited sidecar carried them.
COMPOSED_SETTINGS_FIXTURE = {
    "permissions": {"allow_count": 3, "deny_count": 1, "ask_count": 2, "evidence": "VERIFIED"},
    "hooks": [
        {"event": "PreToolUse", "matcher": "Bash", "command": "python3 hooks/op-hook.py",
         "script": "hooks/op-hook.py", "exists": True, "tier": "user",
         "source_file": "/fake/root/settings.json"},
        {"event": "PostToolUse", "matcher": "Write", "command": "python3 ./hooks/proj-hook.py",
         "script": "hooks/proj-hook.py", "exists": True, "tier": "project",
         "source_file": "/fake/project/.claude/settings.json"},
        {"event": "SessionStart", "matcher": None, "command": "echo local-only",
         "script": None, "exists": None, "tier": "local",
         "source_file": "/fake/project/.claude/settings.local.json"},
    ],
    "overrides": [
        {"key": "model", "winning_tier": "local", "winning_value": "local-model",
         "overridden_tiers": ["project", "user"]},
        {"key": "env", "winning_tier": "project", "winning_value": ["EXTRA_KEY", "GITHUB_TOKEN"],
         "overridden_tiers": ["user"]},
    ],
    "mcp": [
        {"name": "quiet", "tier": "user", "source_file": "/fake/home/.claude.json",
         "type": "http", "enabled": False, "env_keys": [], "header_keys": []},
        {"name": "leaky", "tier": "local", "source_file": "/fake/home/.claude.json",
         "type": "stdio", "enabled": True,
         "env_keys": ["API_KEY"], "header_keys": ["Authorization"],
         # T5 would NEVER emit these two raw fields — present here only to prove render
         # structurally cannot leak them (it only ever reads env_keys/header_keys).
         "env": {"API_KEY": "SECRET-render-leak-abc123"},
         "headers": {"Authorization": "Bearer SECRET-render-leak-xyz789"}},
    ],
}


def test_composed_mcp_servers_rendered_with_tier_enabled_and_key_names_no_secret_leak(tmp_path):
    doc = _minimal_doc()
    doc["composed_settings"] = COMPOSED_SETTINGS_FIXTURE
    out_dir = tmp_path / "composed_mcp"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "MCP servers (composed)" in text
    import re
    mcp_card = re.search(
        r'<div class="card"><h2>MCP servers \(composed\)</h2>.*?</ul></div>', text, re.S).group(0)
    leaky = re.search(r'<li><span class="badge tier-src-local">.*?</li>', mcp_card, re.S).group(0)
    assert "leaky" in leaky
    assert '<span class="badge mcp-enabled">enabled</span>' in leaky
    assert "API_KEY" in leaky and "Authorization" in leaky
    quiet = re.search(r'<li><span class="badge tier-src-user">.*?</li>', mcp_card, re.S).group(0)
    assert "quiet" in quiet
    assert '<span class="badge mcp-disabled">disabled</span>' in quiet
    assert "no env/header keys" in quiet
    # mandatory leak assertion: the raw secret VALUES never reach the serialized HTML,
    # even though this fixture deliberately carries them on the source dict.
    assert "SECRET-render-leak-abc123" not in text
    assert "SECRET-render-leak-xyz789" not in text


def test_composed_hooks_rendered_tier_tagged_with_source_file(tmp_path):
    doc = _minimal_doc()
    doc["composed_settings"] = COMPOSED_SETTINGS_FIXTURE
    out_dir = tmp_path / "composed_hooks"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Hooks (composed, all tiers)" in text
    import re
    hooks_card = re.search(
        r'<div class="card"><h2>Hooks \(composed, all tiers\)</h2>.*?</ul></div>', text, re.S).group(0)
    for tier, event, source in (
        ("user", "PreToolUse", "/fake/root/settings.json"),
        ("project", "PostToolUse", "/fake/project/.claude/settings.json"),
        ("local", "SessionStart", "/fake/project/.claude/settings.local.json"),
    ):
        row = re.search(rf'<li><span class="badge tier-src-{tier}">.*?</li>', hooks_card, re.S).group(0)
        assert event in row
        assert source in row
    # the pre-existing operator-only wiring card is untouched, still present alongside
    assert "Registered hooks (settings.json)" in text


def test_composed_permissions_union_counts_rendered(tmp_path):
    doc = _minimal_doc()
    doc["composed_settings"] = COMPOSED_SETTINGS_FIXTURE
    out_dir = tmp_path / "composed_perms"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Permissions (composed, union)" in text
    assert "allow 3" in text and "deny 1" in text and "ask 2" in text


def test_composed_overrides_rendered_with_allowlisted_key_and_winning_tier(tmp_path):
    doc = _minimal_doc()
    doc["composed_settings"] = COMPOSED_SETTINGS_FIXTURE
    out_dir = tmp_path / "composed_overrides"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Settings overrides (composed)" in text
    import re
    overrides_card = re.search(
        r'<div class="card"><h2>Settings overrides \(composed\)</h2>.*?</ul></div>', text, re.S).group(0)
    model_row = re.search(r'<li><span class="badge tier-src-local">.*?model.*?</li>',
                          overrides_card, re.S).group(0)
    assert "local-model" in model_row
    assert "overrides: project, user" in model_row
    env_row = re.search(r'<li><span class="badge tier-src-project">.*?env.*?</li>',
                        overrides_card, re.S).group(0)
    assert "EXTRA_KEY, GITHUB_TOKEN" in env_row
    assert "overrides: user" in env_row


def test_composed_overrides_body_hides_malformed_non_scalar_winning_value():
    """P1-C (renderer half): collector.py's `_settings_override_safe_value` only ever
    emits a safe SCALAR `winning_value` (or None + a `value_kind` marker for a complex/
    oversized one) — but the RENDERER must defend independently, never trusting that
    upstream invariant blindly. A hand-crafted/malformed sidecar carrying a raw dict
    straight through `winning_value` (no `value_kind` marker at all) must never reach
    the emitted HTML verbatim."""
    overrides = [
        {"key": "model", "winning_tier": "local",
         "winning_value": {"token": "SECRET_SENTINEL"}, "overridden_tiers": ["user"]},
    ]
    html = rh._render_composed_overrides_body(overrides)
    assert "SECRET_SENTINEL" not in html
    assert "(complex value hidden)" in html


def test_composed_overrides_body_hides_value_kind_marked_records():
    """The collector's OWN `value_kind` marker (`"complex"`/`"redacted"`) must also be
    honored, rendering the documented placeholder instead of the (already-None)
    `winning_value`."""
    overrides = [
        {"key": "model", "winning_tier": "local", "winning_value": None,
         "value_kind": "complex", "overridden_tiers": ["user"]},
        {"key": "model", "winning_tier": "project", "winning_value": None,
         "value_kind": "redacted", "overridden_tiers": ["user"]},
    ]
    html = rh._render_composed_overrides_body(overrides)
    assert "(complex value hidden)" in html
    assert "(redacted)" in html


def test_composed_overrides_body_still_renders_normal_scalar_and_env_list_values():
    """Regression pin: the P1-C defensive re-check must not break the two LEGITIMATE
    shapes the collector actually emits -- a plain scalar (`model`) and the special-
    cased `env` override's list of key NAMES (never values)."""
    overrides = [
        {"key": "model", "winning_tier": "local", "winning_value": "local-model",
         "overridden_tiers": ["project"]},
        {"key": "env", "winning_tier": "project", "winning_value": ["EXTRA_KEY", "GITHUB_TOKEN"],
         "overridden_tiers": ["user"]},
    ]
    html = rh._render_composed_overrides_body(overrides)
    assert "local-model" in html
    assert "EXTRA_KEY, GITHUB_TOKEN" in html


def test_composed_overrides_body_renders_genuine_json_null_as_null():
    """P3 defect fix: when a settings override has a genuine JSON `null` winning value
    (no `value_kind` marker), the renderer should display `null` (matching the JSON
    source) instead of the Python string `None`. Regression guards: a value_kind-marked
    record must still show its placeholder, and normal scalars must still work."""
    overrides = [
        # The defect case: genuine JSON null → should render as `null`
        {"key": "model", "winning_tier": "local", "winning_value": None,
         "overridden_tiers": ["user"]},
        # Regression: value_kind marker should still work
        {"key": "other", "winning_tier": "project", "winning_value": None,
         "value_kind": "complex", "overridden_tiers": ["user"]},
        # Regression: normal scalar should still work
        {"key": "cleanup", "winning_tier": "local", "winning_value": "opus",
         "overridden_tiers": ["user"]},
        # Regression: False/0/"" should NOT be treated as null
        {"key": "enabled", "winning_tier": "local", "winning_value": False,
         "overridden_tiers": []},
    ]
    html = rh._render_composed_overrides_body(overrides)
    # Check the defect case: should have `null` not `None`
    assert "model = null" in html, f"Expected 'model = null' in HTML but got: {html}"
    assert "model = None" not in html, f"Found 'model = None' (Python repr) in HTML: {html}"
    # Regression: value_kind marker should still show placeholder
    assert "(complex value hidden)" in html
    # Regression: normal scalar should still work
    assert "cleanup = opus" in html
    # Regression: False should render as false (JSON), not treated as null
    assert "enabled = false" in html or "enabled = False" in html


def test_composed_settings_section_absent_without_composed_settings_key(tmp_path):
    """C15 back-compat, mirroring T6's `tier_composition` gate: an old-shape sidecar
    (no `composed_settings` key at all) renders with zero errors and the whole
    composed-settings section — every one of its four cards — is simply absent, never
    a KeyError, never a stray empty card."""
    doc = _minimal_doc()
    assert "composed_settings" not in doc
    out_dir = tmp_path / "composed_settings_absent"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Composed settings (compose mode)" not in text
    assert "MCP servers (composed)" not in text
    assert "Hooks (composed, all tiers)" not in text
    assert "Permissions (composed, union)" not in text
    assert "Settings overrides (composed)" not in text
    # the `tier-src-*` badge CSS rules are static (always in the stylesheet); what must
    # be absent is any ELEMENT actually wearing one -- the attribute-value pattern never
    # appears in CSS selector syntax, only in a rendered `class="..."` attribute.
    assert 'class="badge tier-src-' not in text


def test_composed_settings_badge_css_defined_once_no_new_theme_debt():
    """The four `tier-src-*`/`mcp-*` badge rules reuse existing themed CSS variables
    (`--tier-operator*`, `--tier-project*`, `--accent-2`, `--good`, `--crit` — every one
    already defined for both light and dark) rather than introducing a new bare color,
    and each selector is written exactly once (not duplicated per theme block, matching
    how `.badge.tier-project` etc. are already written once and rely on `var()` for
    theming)."""
    style = rh.STATIC_STYLE
    for selector in (".badge.tier-src-user{", ".badge.tier-src-project{",
                     ".badge.tier-src-local{", ".badge.mcp-enabled{", ".badge.mcp-disabled{"):
        assert style.count(selector) == 1
    assert ".badge.tier-src-local{border-color:var(--accent-2);color:var(--accent-2)}" in style


def test_one_executable_script_and_csp_hash_reconciles_with_composed_settings(tmp_path):
    doc = _minimal_doc()
    doc["composed_settings"] = COMPOSED_SETTINGS_FIXTURE
    out_dir = tmp_path / "composed_csp"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    p = _ExternalRefParser()
    p.feed(text)
    assert p.tag_counts.get("style", 0) == 1
    assert p.on_handlers == []
    assert p.style_attrs == []
    import re
    exe_scripts = re.findall(r'<script(?![^>]*type="application/json")[^>]*>', text)
    assert len(exe_scripts) == 1
    m = re.search(r"style-src 'sha256-([^']+)'; script-src 'sha256-([^']+)'", text)
    assert m is not None
    assert m.group(1) == rh._csp_hash(rh.STATIC_STYLE)
    assert m.group(2) == rh._csp_hash(rh.STATIC_SCRIPT)


def test_byte_determinism_with_composed_settings_across_pythonhashseed(tmp_path):
    doc = _minimal_doc()
    doc["composed_settings"] = COMPOSED_SETTINGS_FIXTURE
    out_dir = tmp_path / "composed_det"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    outs = []
    for seed in ("0", "1"):
        p = run_render(out_dir, "--date", "2026-07-15", "--no-friction",
                       env={**os.environ, "PYTHONHASHSEED": seed})
        assert p.returncode == 0, p.stderr
        outs.append((out_dir / "harness-map-2026-07-15.html").read_bytes())
    assert outs[0] == outs[1]


# ============================================================= 14. T13 QA-finding fixes
# F1: dup-web tier filter was dead (build_dupweb_model discarded a_tier/b_tier; the
# pairs table had no tier-node wrapper). F2: render_html.main()'s write guard only
# checked the operator root. F3: out_of_root_refs/inspected_roots/excluded_count were
# collected but rendered nowhere.

def test_build_dupweb_model_threads_tier_and_raw_path():
    doc = _minimal_doc()
    doc["duplication"] = {"shingle_k": 8, "metric": "containment", "threshold": 0.6,
                           "pairs": [{"a": "rules/a.md", "b": ".claude/rules/x.md", "score": 0.9,
                                      "shared_sample": "shared words", "evidence": "INFERRED",
                                      "a_tier": "operator", "b_tier": "project"}]}
    model = rh.build_dupweb_model(doc)
    edge = model["edges"][0]
    # existing node_key fields ("a"/"b") are UNTOUCHED (`_dup_node_key` is still called
    # WITHOUT a tier arg, exactly as pre-F1 — an existing consumer, _collect_node_keys /
    # the markdown export, still reads them that way; node_key tier-disambiguation for
    # dup-web is an explicitly separate, out-of-scope concern per _dup_node_key's own
    # docstring).
    assert edge["a"] == "always_loaded:rules/a.md"
    assert edge["b"] == "dup:.claude/rules/x.md"
    # new fields carry the raw path + normalized tier
    assert edge["a_path"] == "rules/a.md"
    assert edge["b_path"] == ".claude/rules/x.md"
    assert edge["a_tier"] == "operator"
    assert edge["b_tier"] == "project"


def test_build_dupweb_model_defaults_tier_operator_when_absent():
    # C15 back-compat: a non-compose/pre-tier pair carries no a_tier/b_tier at all.
    doc = _minimal_doc()
    model = rh.build_dupweb_model(doc)
    edge = model["edges"][0]
    assert edge["a_tier"] == "operator"
    assert edge["b_tier"] == "operator"


def test_dupweb_cross_tier_pair_row_tagged_project_with_human_path_and_badge(tmp_path):
    doc = _minimal_doc()
    doc["duplication"] = {"shingle_k": 8, "metric": "containment", "threshold": 0.6,
                           "pairs": [{"a": "rules/operator-only.md", "b": ".claude/rules/proj-dup.md",
                                      "score": 0.9, "shared_sample": "dup words", "evidence": "INFERRED",
                                      "a_tier": "operator", "b_tier": "project"}]}
    out_dir = tmp_path / "dupweb_cross_tier"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    hyg = re.search(r'<section id="view-hygiene".*?</section>', text, re.S).group(0)
    row = re.search(r'<tr class="tier-node tier-project">.*?</tr>', hyg, re.S)
    assert row is not None, "cross-tier dup pair must be tagged tier-project (one endpoint is project)"
    row_html = row.group(0)
    # human-readable RAW paths, never the internal node-key strings
    assert "rules/operator-only.md" in row_html
    assert ".claude/rules/proj-dup.md" in row_html
    assert "always_loaded:project:" not in row_html
    assert "dup:" not in row_html
    # project endpoint carries the standard project badge (color never the only signal)
    assert '<span class="badge tier-project">project</span>' in row_html
    # the tier filter's EXISTING generic dim rule already targets .tier-node.tier-project
    # (asserted in test_tier_filter_dim_css_targets_wrapper_and_is_ordered_after_heat_css) —
    # no new CSS needed; this proves the row actually wears that class in real output.
    assert "body.tier-operator-only .tier-node.tier-project{opacity:.25}" in rh.STATIC_STYLE


def test_dupweb_same_tier_pair_row_tagged_operator(tmp_path):
    doc = _minimal_doc()
    doc["duplication"] = {"shingle_k": 8, "metric": "containment", "threshold": 0.6,
                           "pairs": [{"a": "rules/a.md", "b": "rules/b.md", "score": 0.9,
                                      "shared_sample": "shared", "evidence": "INFERRED",
                                      "a_tier": "operator", "b_tier": "operator"}]}
    out_dir = tmp_path / "dupweb_same_tier"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    import re
    hyg = re.search(r'<section id="view-hygiene".*?</section>', text, re.S).group(0)
    row = re.search(r'<tr class="tier-node tier-operator">.*?rules/a\.md.*?</tr>', hyg, re.S)
    assert row is not None
    assert '<span class="badge tier-project">project</span>' not in row.group(0)


def test_non_compose_dup_row_still_wears_operator_wrapper_no_new_markup_gate(tmp_path):
    """Non-compose byte-identical, in spirit (C15): every treemap/ladder cell and the
    length-flags table already wear an unconditional `tier-node tier-operator` wrapper
    (T6) even outside compose mode — the dup table now matching that SAME established
    convention is not new project-tier markup, only the badge/`tier-project` class is
    gated on real project-tier data."""
    doc = _minimal_doc()
    out_dir = tmp_path / "dupweb_non_compose"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'class="tier-node tier-project"' not in text
    assert "always_loaded:project:" not in text


def test_render_out_dir_inside_project_containment_root_rejected_in_compose_mode(tmp_path):
    """F2/F5: render_html.main()'s write guard used to check ONLY the operator root —
    an --out-dir inside the composed PROJECT repo was not rejected, defeating H2 for
    this entry point. Mirrors test_serve.py's
    test_out_dir_inside_project_root_rejected_in_compose_mode."""
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    out_dir = proj / "leak-out"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(operator_root)
    doc["inspected_roots"] = {"operator": str(operator_root.resolve()),
                              "project_containment": str(proj.resolve()),
                              "project_harness": str((proj / ".claude").resolve())}
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode != 0
    assert not (out_dir / "harness-map-2026-07-15.html").exists()


def test_render_out_dir_outside_both_roots_accepted_in_compose_mode(tmp_path):
    operator_root = tmp_path / "operator"
    operator_root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(operator_root)
    doc["inspected_roots"] = {"operator": str(operator_root.resolve()),
                              "project_containment": str(proj.resolve()),
                              "project_harness": str((proj / ".claude").resolve())}
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / "harness-map-2026-07-15.html").is_file()


def test_render_non_compose_out_dir_guard_unaffected_by_f2_change(tmp_path):
    """A non-compose (no inspected_roots) sidecar must skip the new compose-only
    project-root check entirely and still be governed only by write_html_safely's
    existing operator-root guard — this is the SAME scenario
    test_write_html_safely_refuses_inside_harness_root already covers; re-asserted
    here to pin that the F2 change didn't alter non-compose behavior."""
    fake_root = tmp_path / "fakeclaude"
    fake_root.mkdir()
    out_dir = fake_root / "reports"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(fake_root)
    assert "inspected_roots" not in doc
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode != 0
    assert not (out_dir / "harness-map-2026-07-15.html").exists()


def test_out_of_root_refs_card_renders_untrusted_refs_esc_html(tmp_path):
    doc = _minimal_doc()
    doc["inspected_roots"] = {"operator": "/fake/op", "project_containment": "/fake/proj",
                              "project_harness": "/fake/proj/.claude"}
    doc["out_of_root_refs"] = [
        {"name": ".claude/rules/evil.md", "target": "/etc/<script>passwd</script>", "trusted": False},
    ]
    out_dir = tmp_path / "oor_card"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Out-of-root refs (1)" in text
    assert ".claude/rules/evil.md" in text
    # the untrusted target is esc_html'd — no raw unescaped tag reaches the HTML
    assert "<script>passwd</script>" not in text
    assert "&lt;script&gt;passwd&lt;/script&gt;" in text
    assert '<span class="badge tier-dark">untrusted</span>' in text


def test_out_of_root_refs_card_absent_without_inspected_roots(tmp_path):
    doc = _minimal_doc()
    assert "inspected_roots" not in doc and "out_of_root_refs" not in doc
    out_dir = tmp_path / "oor_absent"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "Out-of-root refs" not in text


def test_transparency_note_present_in_compose_with_excluded_count_and_roots(tmp_path):
    doc = _minimal_doc()
    doc["inspected_roots"] = {"operator": "/fake/op", "project_containment": "/fake/proj",
                              "project_harness": "/fake/proj/.claude"}
    doc["always_loaded"]["totals"]["excluded_count"] = 3
    out_dir = tmp_path / "transparency_present"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="weight-transparency-note"' in text
    assert "3 file(s) excluded from weight" in text
    assert "roots walked:" in text
    assert "operator" in text and "project" in text


def test_transparency_note_distinguishes_absent_from_zero_excluded_count(tmp_path):
    doc = _minimal_doc()
    doc["inspected_roots"] = {"operator": "/fake/op", "project_containment": "/fake/proj",
                              "project_harness": "/fake/proj/.claude"}
    doc["always_loaded"]["totals"]["excluded_count"] = 0
    out_dir = tmp_path / "transparency_zero"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "0 file(s) excluded from weight" in text
    assert "not measured" not in text


def test_transparency_note_reports_not_measured_when_excluded_count_key_absent(tmp_path):
    doc = _minimal_doc()
    doc["inspected_roots"] = {"operator": "/fake/op", "project_containment": "/fake/proj",
                              "project_harness": "/fake/proj/.claude"}
    assert "excluded_count" not in doc["always_loaded"]["totals"]
    out_dir = tmp_path / "transparency_not_measured"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "weight-exclusion count not measured" in text
    assert "excluded from weight" not in text


def test_transparency_note_absent_in_non_compose(tmp_path):
    doc = _minimal_doc()
    assert "inspected_roots" not in doc
    out_dir = tmp_path / "transparency_absent"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="weight-transparency-note"' not in text
    assert "roots walked:" not in text


# S2 gate fix (Control 1, S1/S2/S9): esc_html's str() is not total.
def test_esc_html_bounds_oversized_int_instead_of_raising():
    """str(int) raises ValueError above sys.get_int_max_str_digits() (4300). esc_html is
    on EVERY value path, so an unguarded str() turns one corrupt leaf into a page crash."""
    out = rh.esc_html(10 ** 5000)
    assert "unrenderable value" in out
    assert "ValueError" in out
    assert "<" not in out and ">" not in out   # the marker itself is escape-safe


def test_esc_html_still_escapes_normal_values():
    assert rh.esc_html('<a href="x">&') == "&lt;a href=&quot;x&quot;&gt;&amp;"
    assert rh.esc_html(42) == "42"
    assert rh.esc_html("\ud800") == "\\ud800"   # existing surrogate behavior unchanged


def test_esc_html_bounds_deeply_nested_structure_instead_of_crashing():
    """Harden-audit fix (T1 round 2): the RecursionError branch of the Control 1 guard.
    The trigger is DEEP, NON-CIRCULAR nesting -- a truly self-referential list prints
    '[[...]]' via repr's re-entrancy guard and raises nothing."""
    deep = []
    for _ in range(100_000):
        deep = [deep]
    out = rh.esc_html(deep)
    assert "unrenderable value" in out
    assert "RecursionError" in out
    assert "<" not in out and ">" not in out


# S2 gate fix (Control 2): ONE shared numeric gate. Values verified against live code.
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"),
                                 # Explicit id REQUIRED: pytest builds parameter ids with
                                 # str(val), which raises the >4300-digit ValueError this
                                 # very case exists to gate -- an auto-id aborts COLLECTION
                                 # of the whole module (verified).
                                 pytest.param(10 ** 5000, id="oversized_int"),
                                 "big", None, [1], True, False, 1e300])
def test_finite_number_rejects_every_unsafe_shape(bad):
    assert rh.finite_number(bad) is None


@pytest.mark.parametrize("good,expected", [(0, 0.0), (-5, -5.0), (1234, 1234.0), (1.5, 1.5)])
def test_finite_number_passes_ordinary_values(good, expected):
    assert rh.finite_number(good) == expected


def test_trend_delta_refuses_nan_instead_of_painting_it_green():
    """A19b/S3, P1: BEFORE this fix _trend_delta(cur=nan, prev=1) returned
    ('- nan', 'good') -- because `nan > prev` is False the arrow is down, and for
    polarity 'up' a down arrow is assigned semantic 'good'. A corrupt headline painted
    the metric IMPROVING, in green, while _gauge_band(nan) painted ('HEAVY','bad') on
    the same value. Two widgets confidently disagreeing is worse than either failing."""
    model = {"first_run": False,
             "series": [{"key": "k", "values": [1, float("nan")], "polarity": "up"}]}
    assert rh._trend_delta(model, "k") is None


def test_trend_delta_refuses_inf():
    model = {"first_run": False,
             "series": [{"key": "k", "values": [1, float("inf")], "polarity": "up"}]}
    assert rh._trend_delta(model, "k") is None


def test_coerce_floats_rejects_nan_and_oversized_int():
    assert rh._coerce_floats([float("nan"), 1.0]) is None
    assert rh._coerce_floats([10 ** 5000]) is None
    assert rh._coerce_floats([1, 2.5]) == [1.0, 2.5]


def test_gauge_band_nan_is_neutral_not_heavy():
    assert rh._gauge_band("always_loaded_words", float("nan")) == ("", "neutral")


def test_tokens_treemap_survives_non_numeric_tokens_est():
    """S4/S5: a string tokens_est raised TypeError and aborted the WHOLE render.

    MUST MIX a valid file with the string file IN THE SAME CATEGORY (Codex F4). With the
    string file ALONE the group sum is 0, `if tokens <= 0: continue` skips the category,
    and the crashing sort key is NEVER REACHED -- the test passes
    while proving nothing. The valid sibling forces the sum positive so the sort runs."""
    out = rh._tokens_treemap([
        {"category": "rule", "tokens_est": "big", "words": 1, "path": "bad.md",
         "tier": "operator"},
        {"category": "rule", "tokens_est": 10, "words": 1, "path": "ok.md",
         "tier": "operator"}])
    assert [c["path"] for c in out["cells"]] == ["ok.md"]   # the render SURVIVED
    assert out["unrenderable"] == ["bad.md"]                # and DISCLOSED the omission


def test_tokens_treemap_nan_does_not_silently_delete_a_file():
    """S4: squarify filters `size > 0`, which is False for NaN -- the file VANISHED from
    the Weight view with no error, no blind_spot and no count. A silent suppression
    primitive.

    A file with no usable size has no AREA, so it cannot be drawn -- the fix is not to
    draw it anyway but to make the omission VISIBLE. Note this case reaches the sort key
    without crashing (`-nan` is legal), which is why the string case above is the one that
    proves the sort-key site; this one proves the disclosure."""
    out = rh._tokens_treemap([
        {"category": "rule", "tokens_est": float("nan"), "words": 1, "path": "bad.md",
         "tier": "operator"},
        {"category": "rule", "tokens_est": 10, "words": 1, "path": "ok.md",
         "tier": "operator"}])
    assert [c["path"] for c in out["cells"]] == ["ok.md"]
    assert out["unrenderable"] == ["bad.md"]   # NOT silently gone


def test_all_invalid_category_still_discloses():
    """R3-4: both tests above mix a valid sibling in, so the category's gated sum is
    positive and the group loop runs. When EVERY file in a category is invalid the
    category never enters group_rects (`if tokens <= 0: continue`) -- if
    `unrenderable` were accumulated inside the group loop these files would vanish from
    the map AND the disclosure. Accumulation from the complete input is what this pins."""
    out = rh._tokens_treemap([
        {"category": "rule", "tokens_est": "big", "words": 1, "path": "bad1.md",
         "tier": "operator"},
        {"category": "rule", "tokens_est": float("nan"), "words": 1, "path": "bad2.md",
         "tier": "operator"},
        # R4-3: a PATHLESS invalid row -- load_sidecar does no row validation, and the
        # disclosure comprehension is the first code to touch f["path"] for rows whose
        # category never survives the tokens<=0 gate. Must disclose, never KeyError.
        {"category": "rule", "tokens_est": "nope", "words": 1, "tier": "operator"}])
    assert out["cells"] == [] and out["groups"] == []
    assert out["unrenderable"] == ["(unknown path)", "bad1.md", "bad2.md"]


def test_on_demand_treemap_discloses_unrenderable_size():
    """The same gating class applied to `_on_demand_treemap`'s `size` fields. Without a
    disclosure here the mandated gate would CONVERT the string-crash into exactly the
    silent deletion S4 names (squarify drops `size > 0` == False), i.e. trade a loud
    defect for a quiet one in the on-demand panel."""
    out = rh._on_demand_treemap({"on_demand": {
        "skills": [{"name": "bad-skill", "words": "big"},
                   {"name": "ok-skill", "words": 10}],
        "skill_internal_bodies": [],
        "memory_bodies": [{"path": "m/bad.md", "words": float("nan")}]}})
    assert [c["path"] for c in out["cells"]] == ["ok-skill"]
    assert out["unrenderable"] == ["bad-skill", "m/bad.md"]


def test_treemap_omission_is_rendered_not_just_modelled(tmp_path):
    """rules/dark-features.md: `unrenderable` must reach an entry point. A model field no
    template reads is a dark feature -- the omission would be recorded and still invisible
    to the operator, which is the S4 defect wearing a different hat.

    Full CLI, real helpers, no mocks. `_minimal_doc(extra_files=...)` APPENDS to its two
    default VERIFIED files, so the `rule` category already has
    a valid member and the group sum is positive regardless of this file."""
    out_dir = tmp_path / "dark"
    out_dir.mkdir()
    doc = _minimal_doc(extra_files=[
        {"path": "bad.md", "category": "rule", "words": 1, "lines": 1,
         "tokens_est": "big", "evidence": "VERIFIED"}])
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0
    html = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "omitted from this map" in html
    assert "bad.md" in html


def test_tokens_treemap_pathless_row_with_valid_size_reaches_the_cell_loop(tmp_path):
    """QA exit gate (MEDIUM 2): the R4-3 hardening stopped at the `unrenderable`
    comprehension. A pathless row with a USABLE size clears the `tokens <= 0` gate, so it
    reaches the per-file loop's bare `f["path"]` subscripts (sort key, cell dict,
    node_key) -- a KeyError inside `_RENDER_FALLBACK_ERRORS`, which T3's envelope converts
    to RenderError and kills the WHOLE dashboard over ONE malformed row. It is drawable
    (it has area), so it is LABELLED, not disclosed: the disclosure's message is "no
    usable size value", which would be false for it."""
    out = rh._tokens_treemap([
        {"category": "rule", "tokens_est": 100, "words": 40, "tier": "operator"},
        {"category": "rule", "tokens_est": 10, "words": 1, "path": "ok.md",
         "tier": "operator"}])
    assert sorted(c["path"] for c in out["cells"]) == ["(unknown path)", "ok.md"]
    assert out["unrenderable"] == []          # drawable -> labelled, not omitted
    assert all(c["node_key"] for c in out["cells"])


def test_tokens_treemap_non_dict_row_degrades_to_the_disclosure():
    """QA exit gate (MEDIUM 2): `load_sidecar` does no row validation, so `files[]` can
    hold a non-dict. `f.get(...)` raises AttributeError -- also in
    `_RENDER_FALLBACK_ERRORS` -- taking down the render under a comment asserting the
    fault is "most likely a real defect in this module". A non-dict has no size and no
    path, so it degrades to the disclosure built for exactly this."""
    out = rh._tokens_treemap([
        "oops",
        {"category": "rule", "tokens_est": 10, "words": 1, "path": "ok.md",
         "tier": "operator"}])
    assert [c["path"] for c in out["cells"]] == ["ok.md"]      # the render SURVIVED
    assert out["unrenderable"] == ["(malformed entry: str)"]   # and DISCLOSED it


def test_malformed_rows_do_not_take_down_the_whole_render(tmp_path):
    """QA exit gate (MEDIUM 2), end to end through the real CLI: one malformed row must
    cost its own cell, never the dashboard. Both shapes in ONE sidecar, appended to
    `_minimal_doc`'s valid `rule` members so the category's gated sum stays positive and
    the per-file loop actually runs."""
    out_dir = tmp_path / "malformed"
    out_dir.mkdir()
    doc = _minimal_doc(extra_files=[
        {"category": "rule", "words": 1, "lines": 1, "tokens_est": 70,
         "evidence": "VERIFIED"}])                    # (a) pathless, valid size
    doc["always_loaded"]["files"].append("oops")      # (b) not a dict at all
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    html = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert "(unknown path)" in html                   # labelled cell, still on the map
    assert "omitted from this map" in html            # the disclosure fired...
    assert "(malformed entry: str)" in html           # ...and NAMES the non-dict row


def test_treemap_size_keeps_integer_presentation():
    """Regression, caught by test_serve's byte-equality assertions: `size`/`words` are
    not purely geometry -- they are rendered as TEXT (ladder cell labels, the overview
    weight-tax list, the copy payloads). Gating them through a float-returning helper
    rewrote every "100 tokens" as "100.0 tokens" report-wide. `esc_html` is the exact
    primitive those render sites use, so pinning it pins the emitted bytes."""
    out = rh._tokens_treemap([{"category": "rule", "tokens_est": 100, "words": 40,
                               "path": "ok.md", "tier": "operator"}])
    cell = out["cells"][0]
    assert rh.esc_html(cell["size"]) == "100"
    assert rh.esc_html(cell["words"]) == "40"
    on_demand = rh._on_demand_treemap({"on_demand": {"skills": [{"name": "s", "words": 300}]}})
    assert rh.esc_html(on_demand["cells"][0]["size"]) == "300"


def test_fmt_float_rejects_nan():
    assert rh._fmt_float(float("nan")) == "0.00"


def test_build_dragcandidate_model_still_raises_on_mixed_type_n():
    """Site (f) of Control 2 is NOT applied — pinned here so the reason is not lost.

    Two existing serve tests use a mixed int/str `n` as their deliberate
    exception injection vector (test_watcher_survives_uncaught_exception,
    test_startup_malformed_synthesis_clean_fatal). Gating this sort key makes the render
    succeed, which breaks the first and HANGS the second. This test documents the
    surviving TypeError so a future change to the sort key fails HERE, next to the
    explanation, instead of silently wedging the serve suite.

    After T3 (commit f601ba2) this TypeError IS one of the enumerated
    `_RENDER_FALLBACK_ERRORS` and converts to RenderError once it reaches
    `render_from_out_dir` — but this test calls `build_dragcandidate_model` directly,
    BELOW that wrapper, so the raw TypeError still surfaces here unchanged; the two
    serve tests above are what exercise the wrapped, converted path."""
    with pytest.raises(TypeError):
        rh.build_dragcandidate_model({"drag_candidates": [
            {"n": 1, "surface": "a"}, {"n": "x", "surface": "b"}]})


# S2 gate fix (Control 3, S8/S12): the collector has an envelope rule (binding rule 5);
# the renderer had no counterpart.
def test_deeply_nested_sidecar_degrades_instead_of_tracebacking(tmp_path):
    """RecursionError subclasses RuntimeError -- NOT json.JSONDecodeError (verified) --
    so it escaped load_sidecar's except clause, escaped render_from_out_dir, and escaped
    main(). In serve mode that bricks startup."""
    out_dir = tmp_path / "deep"
    out_dir.mkdir()
    (out_dir / "harness-map-2026-07-15.json").write_text("[" * 200000 + "]" * 200000)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 1
    assert "Traceback" not in proc.stderr           # no raw traceback ever
    assert "harness-map-2026-07-15.json" in proc.stderr   # names the bad sidecar
    assert "RecursionError" in proc.stderr          # names WHAT failed (GP#15)


def test_render_from_out_dir_converts_unenumerated_faults_to_render_error(tmp_path):
    """The conversion must happen INSIDE render_from_out_dir: serve.py calls it directly
    (serve.py:264), so a main()-only catch would leave --serve startup unprotected."""
    out_dir = tmp_path / "deep2"
    out_dir.mkdir()
    (out_dir / "harness-map-2026-07-15.json").write_text("[" * 200000 + "]" * 200000)
    with pytest.raises(rh.RenderError) as ei:
        rh.render_from_out_dir(out_dir, date="2026-07-15", no_friction=True)
    assert "RecursionError" in str(ei.value)


# T3 harden round (MEDIUM): the Control 3 envelope above is correct but too WIDE. Two
# call sites load sidecars the operator did NOT ask for, and load_sidecar handles only
# OSError/json.JSONDecodeError -- so one deeply-nested UNSELECTED file took the whole
# render down. The documented invariant ("a corrupt sidecar among several is excluded +
# listed in skipped[]") only ever held for JSON-SYNTAX corruption.
def test_deeply_nested_other_sidecar_is_skipped_not_fatal(tmp_path):
    """The requested date is perfectly fine; a DIFFERENT date's sidecar -- needed only
    for the trend series -- raises RecursionError inside json.loads. That must degrade to
    a per-file skipped[] entry, exactly like the invalid-JSON case one screen up."""
    doc = _minimal_doc()
    out_dir = tmp_path / "deep_other"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    (out_dir / "harness-map-2026-07-13.json").write_text("[" * 200000 + "]" * 200000)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    out_file = out_dir / "harness-map-2026-07-15.html"
    assert out_file.is_file()
    text = out_file.read_text(encoding="utf-8")
    assert "2026-07-13" in text          # disclosed in the provenance footer's skipped list
    assert "RecursionError" in text      # and the REASON is disclosed, not swallowed


def test_deeply_nested_newest_sidecar_falls_back_to_earlier_date(tmp_path):
    """--date OMITTED: select_current's reverse scan hits the corrupt NEWEST file first.
    Before the per-file guard that pre-empted the fallback entirely and no date rendered,
    even though an older valid sidecar was sitting right there."""
    doc = _minimal_doc()
    out_dir = tmp_path / "deep_newest"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", doc)
    (out_dir / "harness-map-2026-07-15.json").write_text("[" * 200000 + "]" * 200000)
    proc = run_render(out_dir, "--no-friction")
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / "harness-map-2026-07-14.html").is_file()
    assert not (out_dir / "harness-map-2026-07-15.html").exists()
    text = (out_dir / "harness-map-2026-07-14.html").read_text(encoding="utf-8")
    assert "2026-07-15" in text          # the skipped newest file is disclosed


# T3 harden round (LOW): with per-file faults handled above and the SELECTED sidecar's
# own fault converted at its load site, a fault reaching the top-level envelope is a
# LATER-PIPELINE fault -- most likely a real defect in a build_*_model function. str(exc)
# alone made that indistinguishable from corrupt input.
def test_later_pipeline_fault_prints_traceback_to_stderr(tmp_path):
    """`always_loaded` present but a string: valid JSON, valid schema_version, so it
    passes sidecar selection and blows up inside build_contextweight_model. The operator
    gets the frames on STDERR -- and the RenderError line itself stays unchanged."""
    doc = _minimal_doc()
    doc["always_loaded"] = "not a mapping"
    out_dir = tmp_path / "pipeline_fault"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 1
    assert "AttributeError" in proc.stderr
    assert "Traceback" in proc.stderr
    assert "build_contextweight_model" in proc.stderr   # the frame that actually failed
    assert "fatal: could not render" in proc.stderr     # single-line message unchanged
    assert not (out_dir / "harness-map-2026-07-15.html").exists()   # never into a page


# ===================================================================================
# Codex cross-model gate (final round). Three renderer findings, each a variant of the
# ONE invariant this dashboard is built on: inaccessible is not clean, and a value
# nobody measured must never be presented as a measurement.
# ===================================================================================

def test_crash_envelope_as_newest_sidecar_is_not_selected_as_the_current_run(tmp_path):
    """Codex gate finding 1 (HIGH). The trend series learned to exclude a crash envelope
    (`_run_was_measured`), but sidecar SELECTION never did. With the crash envelope as
    the NEWEST file, `select_current`'s latest-valid fallback picked it and the whole
    dashboard rendered `_empty_document`'s fabricated zeros as LEAN / COMPLIANT / CLEAN
    with "no hygiene flags" -- a confident all-clear for a run that measured nothing.

    The pre-existing crash test selects the PRECEDING good date explicitly, so it passed
    with this fully broken."""
    out_dir = tmp_path / "crash_newest"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", _minimal_doc(tokens_a=200, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-15", _crash_envelope_doc())
    proc = run_render(out_dir, "--no-friction")          # no --date: newest wins
    assert proc.returncode == 0, proc.stderr
    # the MEASURED run is the one rendered; the crash envelope never becomes a page
    assert (out_dir / "harness-map-2026-07-14.html").is_file()
    assert not (out_dir / "harness-map-2026-07-15.html").exists()
    text = (out_dir / "harness-map-2026-07-14.html").read_text(encoding="utf-8")
    # and the skip is DISCLOSED, never silent -- the operator must learn the newest run
    # exists and why it was passed over
    assert "2026-07-15" in text
    assert "crash envelope" in text


def test_render_refuses_when_every_sidecar_is_a_crash_envelope(tmp_path):
    """Codex gate finding 1, the degenerate half: with nothing measured anywhere there is
    no honest dashboard to draw, so the render is FATAL rather than a fabricated all-clear
    over `_empty_document`'s zeros. The message names the date and the reason."""
    out_dir = tmp_path / "crash_only"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _crash_envelope_doc())
    proc = run_render(out_dir, "--no-friction")
    assert proc.returncode == 1
    assert "no valid sidecar found" in proc.stderr
    assert "crash envelope" in proc.stderr
    assert "2026-07-15" in proc.stderr
    assert not (out_dir / "harness-map-2026-07-15.html").exists()


def test_explicit_date_naming_a_crash_envelope_is_fatal_not_a_clean_page(tmp_path):
    """Codex gate finding 1, explicit-`--date` half. Codex F8 forbids silently
    substituting another date for one the operator NAMED, so the honest answer here is
    the same one a corrupt sidecar already gets: fatal, with the reason. What it must
    never be is a page of zeros banded CLEAN."""
    out_dir = tmp_path / "crash_named"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", _minimal_doc())
    _write_sidecar(out_dir, "2026-07-15", _crash_envelope_doc())
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 1
    assert "crash envelope" in proc.stderr
    assert not (out_dir / "harness-map-2026-07-15.html").exists()
    # and NOT silently substituted with the good neighbour
    assert not (out_dir / "harness-map-2026-07-14.html").exists()


def test_select_current_skips_a_profile_rejection_envelope(tmp_path):
    # The selection surface, not just the predicate: a profile-rejection envelope that is
    # the NEWEST file must be skipped with a published reason, and the next-older MEASURED
    # sidecar selected -- never silently, because "inaccessible != clean". Mirrors
    # test_crash_envelope_as_newest_sidecar_is_not_selected_as_the_current_run with the
    # OTHER unmeasured-run marker.
    out_dir = tmp_path / "profile_rejection_newest"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-14", _minimal_doc(tokens_a=200, tokens_b=50))
    _write_sidecar(out_dir, "2026-07-15", _profile_rejection_envelope_doc())
    proc = run_render(out_dir, "--no-friction")          # no --date: newest wins
    assert proc.returncode == 0, proc.stderr
    # the MEASURED run is the one rendered; the profile-rejection envelope never becomes a page
    assert (out_dir / "harness-map-2026-07-14.html").is_file()
    assert not (out_dir / "harness-map-2026-07-15.html").exists()
    text = (out_dir / "harness-map-2026-07-14.html").read_text(encoding="utf-8")
    # and the skip is DISCLOSED, never silent -- the operator must learn the newest run
    # exists and why it was passed over
    assert "2026-07-15" in text
    assert "profile rejected" in text


def test_missing_headline_key_is_unmeasured_in_the_gauge_not_zero(tmp_path):
    """Codex gate finding 3 (HIGH). The trend learned that an absent headline key means
    "not measured"; the CURRENT gauge still did `headline.get(key, 0)`, so ONE page
    showed the metric as unmeasured in the trend table and simultaneously as `0 / CLEAN`
    in the gauge -- the same contradiction-on-one-page shape as the phantom-gauge bug."""
    doc = _minimal_doc()
    doc["headline"].pop("duplicate_pair_count")
    out_dir = tmp_path / "missing_headline"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    gauge = re.search(
        r'<button class="gauge gauge-(\w+)" data-gauge="duplicate_pair_count".*?</button>',
        text, re.S)
    assert gauge is not None
    assert gauge.group(1) == "neutral"                       # no severity verdict at all
    assert f'<div class="v">{rh.NOT_MEASURED_TEXT}</div>' in gauge.group(0)
    assert "CLEAN" not in gauge.group(0)
    assert '<div class="v">0</div>' not in gauge.group(0)


def test_missing_headline_key_is_unmeasured_in_the_overview_digest(tmp_path):
    """Codex gate finding 3, the digest half (`build_overview_model`). Same key, same
    page, same rule -- a `.get(..., 0)` here reads as a measurement nobody made."""
    model = rh.build_overview_model(
        {"civc": {"available": False, "cells": []},
         "context_weight": {"always": {"cells": []}},
         "drag": {"available": False, "rows": []}},
        {"instruction_files_over_200": 2},        # duplicate_pair_count ABSENT
        0, 0, phantom_confirmed_count=0)
    assert model["hygiene"]["dup_pairs"] == rh.NOT_MEASURED_TEXT
    assert model["hygiene"]["over_cap"] == 2      # present keys are untouched
    html = rh._render_overview_digest(model)
    assert f"Duplicate pairs: {rh.NOT_MEASURED_TEXT}" in html
    assert "Duplicate pairs: 0" not in html
    # an unmeasured metric carries NO severity dot verdict
    dup_li = re.search(r'<li><span class="sev-dot sev-(\w+)"[^>]*></span>Duplicate pairs:',
                       html)
    assert dup_li is not None and dup_li.group(1) == "neutral"


def test_negative_size_is_disclosed_not_silently_dropped():
    """Codex gate finding 5 (MEDIUM). `finite_number(-5)` passes BY DESIGN (deltas are
    legitimately negative), so `tokens_est` rows of +10 and -10 in one category summed to
    zero and the whole category was dropped by the `tokens <= 0` gate WITHOUT entering
    `unrenderable` -- a silent suppression of the drawable row beside it."""
    files = [{"path": "good.md", "category": "claude_md", "tokens_est": 10, "words": 5},
             {"path": "bad.md", "category": "claude_md", "tokens_est": -10, "words": 5}]
    model = rh._tokens_treemap(files)
    assert "bad.md" in model["unrenderable"]
    assert any(c["path"] == "good.md" for c in model["cells"])   # not silently suppressed
    assert all(c["path"] != "bad.md" for c in model["cells"])


def test_negative_on_demand_size_is_disclosed():
    """Same domain rule on the on-demand treemap: a negative word count has no area."""
    doc = {"on_demand": {"skills": [{"name": "neg", "words": -7, "tier": "operator"}],
                         "skill_internal_bodies": [], "memory_bodies": []}}
    model = rh._on_demand_treemap(doc)
    assert "neg" in model["unrenderable"]


def test_negative_headline_count_does_not_band_clean():
    """Codex gate finding 5, the banding half: a headline count of -1 satisfied
    `value <= 0` and painted the reassuring green CLEAN/COMPLIANT/LEAN verdict. Counts
    and sizes have no negative domain, so the honest answer is the SAME no-verdict
    neutral a NaN already gets."""
    assert rh._gauge_band("duplicate_pair_count", -1) == ("", "neutral")
    assert rh._gauge_band("instruction_files_over_200", -1) == ("", "neutral")
    assert rh._gauge_band("always_loaded_tokens_est", -5) == ("", "neutral")
    assert rh._gauge_band("friction_total", -1) == ("", "neutral")
    # non-negative values band exactly as before
    assert rh._gauge_band("duplicate_pair_count", 0) == ("CLEAN", "good")
    assert rh._gauge_band("always_loaded_tokens_est", 100) == ("LEAN", "good")


def test_finite_number_still_accepts_negatives():
    """The domain gate is ADDITIVE — `finite_number`'s contract is untouched, because a
    negative delta is legitimate. Only the size/count call sites narrow."""
    assert rh.finite_number(-5) == -5.0
    assert rh.nonneg_number(-5) is None
    assert rh.nonneg_number(0) == 0.0
    assert rh.nonneg_number(5) == 5.0


# ------------------------------------------------- S6a: date-key contract (§4.3, finding #12)
def test_record_date_reads_timestamp_key():
    """T1.1 — every interventions record dates via `timestamp`; before S6a all 48 were
    invisibly UNDATED (AMENDMENTS A27).
    # Changing this key set requires a spec change (S6 §4.3)."""
    assert rh._record_date({"timestamp": "2026-07-31T01:18:24"}) == "2026-07-31"


def test_record_date_prefers_date_over_timestamp_when_both_present():
    """T1.2 — the TAIL position of `timestamp` is load-bearing and is CONTRACT, not
    accident: `_record_date` returns on the first matching key, so any record that already
    resolves via date/ts/verified_date returns before `timestamp` is consulted. That is what
    makes the three wired streams byte-frozen by CONSTRUCTION rather than by today's data.
    # Changing this ordering requires a spec change (S6 §4.3)."""
    rec = {"date": "2026-01-01", "timestamp": "2026-07-31T01:18:24"}
    assert rh._record_date(rec) == "2026-01-01"


def test_record_date_rejects_calendar_invalid_date():
    """T1.3 — DATE_RE is `\\d{4}-\\d{2}-\\d{2}` used with .match(): purely STRUCTURAL, so
    `2026-13-45` parsed clean before S6a. A calendar-invalid date must be treated as
    UNDATED and must never be trusted for the `d > current_date` future-filter comparison —
    an invalid date must not be able to skip a record, nor to sneak past the guard."""
    assert rh._record_date({"date": "2026-13-45"}) is None
    assert rh._record_date_info({"date": "2026-13-45"}) == (None, "invalid", False)


def test_record_date_ignores_unregistered_date_like_keys():
    """T1.4 — negative guard: a date mentioned in free prose must not silently backdate a
    record. Only the declared keys carry a record's date."""
    assert rh._record_date({"rationale_snippet": "on 2026-07-31 the operator said"}) is None


def test_record_date_conflict_is_date_vs_timestamp_only():
    """T1.5 — `records_conflicting_date` compares `date` against `timestamp` ONLY.
    Generalising it to 'any two recognised keys disagree' produces ~39 FALSE conflicts on
    the live decisions stream, where `date` (39 records) and `verified_date` (43 records)
    carry deliberately different semantics (AMENDMENTS A27 key census).
    # Changing this comparison requires a spec change (S6 §4.3)."""
    conflict = {"date": "2026-01-01", "timestamp": "2026-07-31T00:00:00"}
    agree = {"date": "2026-07-31", "timestamp": "2026-07-31T01:18:24"}
    other = {"date": "2026-01-01", "verified_date": "2026-07-31"}
    assert rh._record_date_info(conflict) == ("2026-01-01", "dated", True)
    assert rh._record_date_info(agree) == ("2026-07-31", "dated", False)
    assert rh._record_date_info(other) == ("2026-01-01", "dated", False)


def test_aggregate_codex_does_not_drop_a_calendar_invalid_record():
    """T1.6 — `aggregate_codex` is the one stream that did NOT route through the shared
    date helper: it kept a raw `or rec["ts"][:10]` fallback that reached the unvalidated
    string. `2026-13-45` matches DATE_RE structurally and string-compares greater than any
    real date, so the record was SILENTLY DROPPED -- an invalid date must never be able to
    skip a record, nor to sneak past the guard (finding #12).

    Also pins that a genuinely future-dated record IS still skipped, so the fix cannot be
    'satisfied' by disabling the guard."""
    invalid = [{"mode": "plan", "verdict": "SHIP", "ts": "2026-13-45T00:00:00Z"}]
    assert rh.aggregate_codex(invalid, "2026-07-15")["runs"] == 1
    future = [{"mode": "plan", "verdict": "SHIP", "ts": "2099-12-31T00:00:00Z"}]
    assert rh.aggregate_codex(future, "2026-07-15")["runs"] == 0


# ---------------------------------- S6a: date-provenance counters + rename (finding #12)
def _one_stream_render(tmp_path, name, stream_flag, lines, date="2026-07-15"):
    """Render `_minimal_doc` with exactly ONE telemetry stream wired from an explicit
    fixture path (never a default), returning the rendered HTML text."""
    out_dir = tmp_path / name
    out_dir.mkdir()
    _write_sidecar(out_dir, date, _minimal_doc())
    stream = tmp_path / f"{name}.jsonl"
    stream.write_text("".join(json.dumps(r) + "\n" for r in lines))
    proc = run_render(out_dir, "--date", date, stream_flag, str(stream))
    assert proc.returncode == 0, proc.stderr
    return (out_dir / f"harness-map-{date}.html").read_text()


def test_interventions_sentence_says_dated_never_in_window(tmp_path):
    """T2.1 — naming guard. The old wording asserted a 30-day bound the code never had:
    the join only excludes FUTURE dates. The phrase must be gone from the WHOLE document,
    not just the interventions row — the decisions branch carried it too."""
    text = _one_stream_render(tmp_path, "dated", "--interventions-file", [
        {"timestamp": "2026-07-14T10:00:00", "memory_file": "feedback_note.md"},
        {"timestamp": "2026-07-13T10:00:00", "memory_file": "feedback_note.md"},
    ])
    assert "2 records parsed, 2 dated" in text
    assert "in window" not in text


def test_interventions_counts_undated_invalid_and_conflicting(tmp_path):
    """T2.2/T2.3/T2.4 — the three disclosed counters. First-match-wins still returns the
    `date` value for the conflicting record; the disagreement is COUNTED, not swallowed."""
    text = _one_stream_render(tmp_path, "prov", "--interventions-file", [
        {"memory_file": "feedback_note.md"},                              # undated
        {"date": "2026-13-45", "memory_file": "feedback_note.md"},        # invalid calendar
        {"date": "2026-07-01", "timestamp": "2026-07-14T00:00:00",
         "memory_file": "feedback_note.md"},                              # conflicting
    ])
    assert "records_undated" in text and "records_invalid_date" in text
    assert "records_conflicting_date" in text
    assert "1 undated" in text and "1 invalid" in text and "1 conflicting" in text


def test_decisions_counts_undated_invalid_and_conflicting(tmp_path):
    """QA P3 — analogous to `test_interventions_counts_undated_invalid_and_conflicting`,
    proving the SAME date-provenance counters reach the decisions stream's raw-counters
    `<details>`, not only the interventions stream's. Unlike interventions, `_friction_sentence`
    has no decisions-branch words for these counters (T2.2-T2.4 only worded the
    interventions branch), so this asserts the `json.dumps` raw-counters values directly
    (HTML-escaped: `esc_html` turns `"` into `&quot;`) rather than a sentence phrase."""
    text = _one_stream_render(tmp_path, "decisions-prov", "--decisions-file", [
        {"component": "rules/a.md"},                                       # undated
        {"date": "2026-13-45", "component": "rules/a.md"},                 # invalid calendar
        {"date": "2026-07-01", "timestamp": "2026-07-14T00:00:00",
         "component": "rules/a.md"},                                       # conflicting
    ])
    assert "&quot;records_undated&quot;: 1" in text
    assert "&quot;records_invalid_date&quot;: 1" in text
    assert "&quot;records_conflicting_date&quot;: 1" in text


def test_first_date_shaped_key_wins_even_when_calendar_invalid():
    """Post-exec Codex round 2, finding 1. `date` is calendar-invalid; `timestamp` is
    valid. First-match-wins is absolute (§4.3): the malformed higher-priority `date` key
    is never skipped in favor of the later valid `timestamp` -- the record is `invalid`,
    not silently rescued as `dated` via a lower-priority key."""
    rec = {"date": "2026-13-45", "timestamp": "2026-08-02T00:00:00"}
    assert rh._record_date_info(rec) == (None, "invalid", False)


def test_valid_first_key_still_wins_over_later_keys():
    """Anti-vacuity for the fix above: a genuinely valid first key must still win, and
    `conflict` detection (computed AFTER the loop, from `date`/`timestamp` directly) must
    still fire -- proving the `break` did not disable it."""
    rec = {"date": "2026-07-14", "timestamp": "2026-07-20T00:00:00"}
    assert rh._record_date_info(rec) == ("2026-07-14", "dated", True)


def test_invalid_later_key_does_not_taint_a_valid_first_key():
    """The mirror case: a valid first key wins outright even though a LATER key is
    calendar-invalid -- the loop never reaches `timestamp` at all once `date` matches."""
    rec = {"date": "2026-07-14", "timestamp": "2026-13-45"}
    assert rh._record_date_info(rec) == ("2026-07-14", "dated", False)


def test_record_with_no_date_shaped_key_is_undated():
    """Anti-vacuity for the other branch: a record with no recognised date key at all is
    `undated`, not `invalid` -- `saw_structural` must stay False."""
    rec = {"memory_file": "x.md"}
    assert rh._record_date_info(rec) == (None, "undated", False)


def test_invalid_first_key_record_counted_in_invalid_not_dated(tmp_path):
    """End-to-end: a record whose FIRST date-shaped key (`date`) is calendar-invalid but
    whose `timestamp` is valid must land in `records_invalid_date`, not in
    `records_dated_as_of` -- proving the fix reaches the rendered raw counters, not just
    the pure-function unit tests above."""
    text = _one_stream_render(tmp_path, "invalid-first-key", "--interventions-file", [
        {"date": "2026-13-45", "timestamp": "2026-08-02T00:00:00",
         "memory_file": "feedback_note.md"},                                # invalid
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "feedback_note.md"},  # dated
    ], date="2026-08-02")
    assert "&quot;records_invalid_date&quot;: 1" in text
    assert "&quot;records_dated_as_of&quot;: 1" in text


def test_interventions_future_dated_record_is_skipped_and_counted(tmp_path):
    """T2.5 — the future guard does not merely exclude a record from a COUNT: it skips the
    whole record, so it never heats a node. Before S6a the guard was DEAD for this stream
    because no interventions record was ever dated. A skipped record must not vanish
    unremarked."""
    text = _one_stream_render(tmp_path, "future", "--interventions-file", [
        {"timestamp": "2099-12-31T00:00:00", "memory_file": "feedback_note.md"},
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "feedback_note.md"},
    ])
    assert "2 records parsed, 1 dated" in text
    assert "1 skipped as future-dated" in text


def test_stream_card_numeral_and_sentence_agree(tmp_path):
    """T2.6 — the defect §4.3a names: wiring the path WITHOUT the timestamp fix ships a
    card reading `3` above a sentence reading `0 in window`. Two numbers, one card, one
    dataset, disagreeing — the confidently-disagreeing-widgets class."""
    text = _one_stream_render(tmp_path, "agree", "--interventions-file", [
        {"timestamp": f"2026-07-1{i}T00:00:00", "memory_file": "feedback_note.md"}
        for i in (1, 2, 3)
    ])
    assert "3 records parsed, 3 dated" in text
    card = re.search(r'<div class="stream-card"><div class="count">(\d+)</div>'
                     r'<h3>Interventions</h3>', text)
    assert card is not None and card.group(1) == "3"


def test_interventions_card_count_exceeds_its_friction_total_contribution(tmp_path):
    """QA P2 — `_stream_event_count`'s documented asymmetry, pinned: for `interventions`
    the card shows records PARSED, not records ATTRIBUTED. An unmatched record (its
    memory file deleted, so it joins no map component) is parsed but contributes NOTHING
    to `friction_total`. This is the CURRENT, deliberate behaviour — the sentence beneath
    the card discloses the gap in words ("1 unmatched ...") — so a future change to
    either number is a conscious decision, not silent drift. Not a bug: do not "fix" this
    by switching the card to `segments_joined`, which would break
    `test_stream_card_numeral_and_sentence_agree`."""
    text = _one_stream_render(tmp_path, "diverge", "--interventions-file", [
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "no-such-memory-file.md"},
    ])
    card = re.search(r'<div class="stream-card"><div class="count">(\d+)</div>'
                     r'<h3>Interventions</h3>', text)
    assert card is not None and card.group(1) == "1"
    gauge = re.search(r'data-gauge="friction_total"[^>]*>\s*<div class="v">(\d+)</div>', text)
    assert gauge is not None and gauge.group(1) == "0"
    assert "1 unmatched (the named memory file is no longer a node on this map)" in text


# --------------------------------------------- S6a: the default interventions path (§4.1)
def test_default_streams_derives_interventions_path_from_home(tmp_path):
    """T3.2/T3.4 — the slug is DERIVED from $HOME at call time, never a literal.
    `-Users-<user>--claude` is machine-specific; a literal would also ship the operator's
    username into a PUBLIC repo (§4.1 publication requirement). The expected value is
    re-derived here by an INDEPENDENT implementation (`_slug`), so the assertion is not
    circular."""
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "projects" / _slug(claude) / "memory").mkdir(parents=True)
    streams = _default_streams_under_home(home)
    assert streams["interventions"] == str(
        claude / "projects" / _slug(claude) / "memory" / "interventions.jsonl")


def test_default_streams_interventions_is_none_for_a_foreign_root(tmp_path):
    """T3.1 — CONTAINMENT BY SIGNATURE (finding #3). The slug alone gives NO containment:
    derived from $HOME/.claude unconditionally, it would hand this harness's interventions
    log to a scan of a completely different root. The `root` comparison is what closes it.
    This assertion is the one that would have failed against the round-1 design."""
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "projects" / _slug(claude) / "memory").mkdir(parents=True)
    foreign = tmp_path / "some-other-repo"
    foreign.mkdir()
    streams = _default_streams_under_home(home, root=foreign)
    assert streams["interventions"] is None
    assert streams["decisions"] is not None   # the other three are unaffected


def test_no_stream_in_stream_order_defaults_to_none(tmp_path):
    """T3.3 — the general guard. A key in STREAM_ORDER is a stream with a card, a footer
    row, and a join branch. One that defaults to None is a DARK FEATURE: it renders
    'absent' forever and no test notices, because every friction test injects its path
    explicitly. That is exactly how `interventions: None` shipped.

    NOTE (finding #4): this test builds its OWN $HOME with the memory directory present.
    A `None` under a bare fake $HOME -- where projects/<slug>/memory/ does not exist -- is
    CORRECT behaviour (§4.1's directory gate), not the dark-feature bug this test hunts.
    Do NOT 'fix' a failure here by weakening the assertion; give the home its memory dir.
    # Changing this requires a spec change (SPEC_6 preamble)."""
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "projects" / _slug(claude) / "memory").mkdir(parents=True)
    streams = _default_streams_under_home(home)
    assert set(streams) == set(rh.STREAM_ORDER)
    assert sorted(k for k, v in streams.items() if v is None) == []


def test_interventions_stream_is_reachable_without_the_cli_flag(tmp_path):
    """T3.5 — end-to-end reachability: drive the CLI with NO --interventions-file and prove
    the stream loads from the default path. Every pre-S6a friction test passed the flag
    explicitly, which is why the dead default survived.

    The sidecar's `root` is set to this home's .claude dir on purpose: the post-load
    containment (T3.6) correctly drops a DEFAULT interventions path when the sidecar was
    scanned from a different root, so a `/fake/root` sidecar would render 'not provided'
    for the right reason at the wrong time."""
    home = tmp_path / "home"
    claude = home / ".claude"
    mem = claude / "projects" / _slug(claude) / "memory"
    mem.mkdir(parents=True)
    (mem / "interventions.jsonl").write_text(
        json.dumps({"timestamp": "2026-07-14T00:00:00",
                    "memory_file": "feedback_note.md"}) + "\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(claude)
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15",
                      env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text()
    assert "1 records parsed, 1 dated" in text
    assert "Interventions — stream not provided" not in text


def test_default_interventions_is_contained_when_the_sidecar_root_is_foreign(tmp_path):
    """T3.6 — the CLI path has NO --root argument (verified: render_html.main's parser
    declares --out-dir/--date/the four --*-file flags/--no-friction), and it builds
    `streams` BEFORE any sidecar is loaded, so the selected root is not knowable there.
    Containment is therefore re-applied against doc["root"] once it IS known. Without this,
    serve mode would be contained while the more common CLI path stayed leaky."""
    home = tmp_path / "home"
    claude = home / ".claude"
    mem = claude / "projects" / _slug(claude) / "memory"
    mem.mkdir(parents=True)
    (mem / "interventions.jsonl").write_text(
        json.dumps({"timestamp": "2026-07-14T00:00:00",
                    "memory_file": "feedback_note.md"}) + "\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(tmp_path / "some-other-repo")
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15",
                      env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text()
    assert "Interventions — stream not provided" in text


def test_explicit_interventions_file_survives_a_foreign_sidecar_root(tmp_path):
    """T3.7 — containment applies to the DEFAULT-derived path only. An explicit
    --interventions-file naming a different file is never dropped: the operator asked for
    it by name, and the flag is the documented override (§4.1 rejects a second env hatch
    for the same reason)."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = str(tmp_path / "some-other-repo")
    _write_sidecar(out_dir, "2026-07-15", doc)
    stream = tmp_path / "explicit.jsonl"
    stream.write_text(json.dumps({"timestamp": "2026-07-14T00:00:00",
                                  "memory_file": "feedback_note.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15",
                      "--interventions-file", str(stream))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text()
    assert "1 records parsed, 1 dated" in text


def test_default_interventions_fails_closed_when_the_sidecar_root_is_absent(tmp_path):
    """T3.11 — a sidecar with no `root` cannot establish "the selected root IS the harness
    root", so the DEFAULT-derived stream must be dropped, not retained. Returning it
    unchanged would let one missing field bypass the entire containment -- doubt removed
    rather than added, which is the asymmetry §6.3's one-way override rule exists to
    protect. An explicit --interventions-file is unaffected (see the test above)."""
    home = tmp_path / "home"
    claude = home / ".claude"
    mem = claude / "projects" / _slug(claude) / "memory"
    mem.mkdir(parents=True)
    (mem / "interventions.jsonl").write_text(
        json.dumps({"timestamp": "2026-07-14T00:00:00",
                    "memory_file": "feedback_note.md"}) + "\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc.pop("root")
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15",
                      env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text()
    assert "Interventions — stream not provided" in text


def test_default_interventions_fails_closed_when_the_sidecar_root_is_not_a_string(tmp_path):
    """T3.12 (S6a Task 3 audit, MEDIUM, harden) -- superseded by A26 (S6a guard-fix audit,
    HIGH). A non-string truthy `doc["root"]` (e.g. a sidecar field that came back as an
    int, list, or dict instead of a path string) cannot establish "the selected root IS
    the harness root" either -- same asymmetry as an absent root above. This test used to
    pin that the render degraded gracefully (default-interventions stream dropped, rc 0):
    that was wrong, because dropping the unparseable root also emptied `guard_roots`
    passed to `write_html_safely`, and an EMPTY `guard_roots` skips containment
    validation ENTIRELY -- `main()` reproducibly wrote the HTML file inside a guarded
    root when `--out-dir` pointed there. A26 fixes `main()` to refuse the render instead
    of dropping the unparseable root, since an empty guard list means "cannot verify
    containment," not "containment not needed." This test now pins the CORRECTED
    contract: fail closed with a non-zero exit, no HTML written, and a stderr message
    naming the bad root -- never a silent write with the containment guard disabled."""
    home = tmp_path / "home"
    claude = home / ".claude"
    mem = claude / "projects" / _slug(claude) / "memory"
    mem.mkdir(parents=True)
    (mem / "interventions.jsonl").write_text(
        json.dumps({"timestamp": "2026-07-14T00:00:00",
                    "memory_file": "feedback_note.md"}) + "\n")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = 5
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15",
                      env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 1, proc.stdout
    assert not (out_dir / "harness-map-2026-07-15.html").exists()
    assert "root field is not a string" in proc.stderr


def test_nonstring_sidecar_root_does_not_write_inside_the_would_be_guarded_root(tmp_path):
    """A26 (S6a guard-fix audit, HIGH) -- this is the exact probe that reproduced the
    fail-open. Before the fix, `main()` dropped a non-string `doc["root"]` from
    `guard_roots` instead of refusing, leaving `guard_roots` EMPTY; `write_html_safely`
    skips containment validation entirely on an empty `guard_roots` (its own docstring:
    "A falsy/empty guard_roots skips validation entirely"). So `--out-dir` pointed
    straight INSIDE the harness root the sidecar's `root` field would otherwise have
    named, and the HTML was written there uncaught, rc 0. Pins that the fix closes this:
    no write happens at all, regardless of where `--out-dir` resolves, because `main()`
    now refuses before `write_html_safely` is ever reached."""
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    out_dir = claude / "harness-map-out"       # INSIDE the would-be guarded root
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = 5
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15",
                      env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 1, proc.stdout
    assert not (out_dir / "harness-map-2026-07-15.html").exists()
    assert "root field is not a string" in proc.stderr


@pytest.mark.parametrize("mutate,label", [
    (lambda doc: doc.pop("root"), "absent"),
    (lambda doc: doc.__setitem__("root", None), "null"),
    (lambda doc: doc.__setitem__("root", ""), "empty-string"),
])
def test_write_guard_floor_refuses_falsy_root_write_inside_home_claude(tmp_path, mutate, label):
    """S6a guard-fix v2. A26 (above) closed the WRONG-TYPE half of the write-time
    containment fail-open; this half is the FALSY one. `sidecar_root` legitimately falls
    out of `guard_roots` when it is "" / None / absent (T3.11, above, requires exactly
    that for STREAM-SELECTION containment), and on a non-compose run
    `project_containment_root` is also always None -- so both truthy roots can be absent
    at once, and the old truthiness filter emptied `guard_roots` to `[]` in that case,
    reproducing the exact fail-open A26 closed on the wrong-type path: `write_html_safely`
    skips containment validation entirely on an empty guard list. The fix folds in a
    permanent floor root (`Path.home() / ".claude"`), ADDITIVE to the sidecar-derived
    roots, so `guard_roots` is never empty. Pins all three falsy shapes refusing the
    write when `--out-dir` resolves inside the floor: rc 1, no HTML written, stderr
    names the guarded root."""
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    out_dir = claude / "harness-map-out"       # INSIDE the floor root
    out_dir.mkdir()
    doc = _minimal_doc()
    mutate(doc)
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15",
                      env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 1, proc.stdout
    assert not (out_dir / "harness-map-2026-07-15.html").exists()
    assert "refusing to write inside a guarded root" in proc.stderr, proc.stderr


# The T3.11 shape above (falsy root, `--out-dir` at `tmp_path / "out"`, provably outside
# every guarded root including the floor) already pins that this case still succeeds
# (rc 0, default interventions stream dropped) -- not duplicated here.


def test_write_guard_floor_residual_other_mapped_root_still_writes(tmp_path):
    """Documents the CURRENT DISCLOSED behavior (S6a guard-fix v2 residual) rather than
    leaving it implicit: the floor root added above is exactly `Path.home() / ".claude"`
    -- it cannot know about some OTHER mapped harness root the sidecar failed to report.
    A falsy sidecar root plus `--out-dir` inside a DIFFERENT root (not `$HOME/.claude`)
    still writes, uncaught, exactly as before this fix. Closure condition: the sidecar
    reliably reporting the root it scanned -- not reachable in this scenario, since the
    whole point is a root the running process has no record of."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)     # the floor root -- stays untouched, empty
    other_root = tmp_path / "some-other-mapped-harness-root"
    out_dir = other_root / "harness-map-out"   # INSIDE the OTHER root, not the floor
    out_dir.mkdir(parents=True)
    doc = _minimal_doc()
    doc.pop("root")
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15",
                      env={**os.environ, "HOME": str(home)})
    assert proc.returncode == 0, proc.stderr
    assert (out_dir / "harness-map-2026-07-15.html").exists(), (
        "documents the disclosed residual: the floor only guards ~/.claude, not other "
        "mapped harness roots the sidecar failed to report")


def test_render_html_and_collector_derive_the_same_project_slug(tmp_path):
    """The interventions default is reachable only if render_html's slug rule stays
    byte-identical to collector.py's. Drift renders 'stream not provided' silently --
    the directory gate makes it MORE silent, since without it you would at least see a
    bogus path in the footer. Nothing else pins these two copies together.
    # Changing this requires a spec change (S6 §4.1)."""
    collector_mod = rh._get_sibling_collector()
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    candidates = [
        tmp_path,
        tmp_path / "some-project-dir" / "nested",
        account_home / ".claude",
    ]
    for p in candidates:
        assert rh._project_slug(p) == collector_mod._project_slug(p)


def test_no_absolute_home_literal_in_runtime_modules():
    """PUBLICATION REQUIREMENT (operator directive 2026-07-31): this skill is being
    extracted to a PUBLIC repo. A `/Users/` literal in a runtime module would ship the
    operator's username and home-directory layout into it -- a release blocker, not a
    hermeticity nicety. Verified: test_release_decoupling.py reads only SKILL.md,
    report-template.md and civc-reference.md, so nothing in the existing suite would catch
    this.

    Scoped to the three RUNTIME modules on purpose. The test tree is deliberately excluded:
    tests/test_render_html.py:18 pins a real sample path (REAL_SAMPLE) and the whole-tree
    pre-publication scan is separate, larger scope (S6 §14).

    TWO patterns, because one is not enough. A CC project slug is the absolute path with
    "/" and "." replaced by "-", so it leaks the same username while containing no "/Users/"
    substring at all. The second assertion derives the RUNNING machine's own slug and
    asserts its absence, which makes the check portable: on any machine, a runtime module
    must not contain that machine's home-derived identifier.
    The account home is read from the PASSWORD DATABASE, never from `Path.home()`.
    `Path.home()` reads $HOME, and this module's session-scoped autouse fixture
    (tests/test_render_html.py:26) rewrites $HOME to a tmp_path_factory directory for every
    test here -- so a Path.home()-derived slug is a RANDOM TEMP SLUG, and asserting its
    absence from the source is trivially true and can never catch the real leak this guard
    exists to stop. pwd.getpwuid(os.getuid()).pw_dir is stdlib (binding rule 9) and is
    unaffected by the fixture. Do NOT reintroduce Path.home() here. This is a TEST-SIDE
    derivation only: `default_streams` and `_project_slug` still derive from Path.home() at
    call time, which is correct and is what the §9-R D hermeticity contract requires --
    the guard needs a home the fixture cannot rewrite, the runtime needs one it can.
    # Changing this scope requires a spec change (S6 §4.1).
    # Changing this home derivation requires a spec change (S6 §4.1)."""
    skill_dir = RENDER.parent
    account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    home_slug = _slug(account_home / ".claude")
    for name in ("render_html.py", "collector.py", "serve.py"):
        text = (skill_dir / name).read_text()
        assert "/Users/" not in text, (
            f"{name} contains a /Users/ literal -- derive the path from $HOME at call time")
        assert home_slug not in text, (
            f"{name} contains this machine's derived project slug ({home_slug}) -- it leaks "
            f"the username without matching a /Users/ grep; derive it at call time")


# ---------------------------------------------------- S6a: truncation (finding #13, S-3)
def _truncated_render(tmp_path, name, date="2026-07-15"):
    """Render with an interventions stream that trips the LINE cap while staying FAR under
    the byte cap — the case a single `truncated` boolean cannot express.

    A REAL over-cap file, never a patched constant: rh.STREAM_MAX_LINES + 5 compact records
    at ~69 bytes each is ~1.4 MB, i.e. 20,005 lines against a 5,000,000-byte cap. That is
    what proves the two caps are genuinely independent parameters rather than one derived
    from the other."""
    out_dir = tmp_path / name
    out_dir.mkdir()
    _write_sidecar(out_dir, date, _minimal_doc())
    stream = tmp_path / f"{name}.jsonl"
    record = json.dumps({"timestamp": "2026-07-14T00:00:00",
                         "memory_file": "feedback_note.md"}) + "\n"
    stream.write_text(record * (rh.STREAM_MAX_LINES + 5))
    assert stream.stat().st_size < rh.STREAM_MAX_BYTES, \
        "fixture must trip the LINE cap WITHOUT approaching the byte cap"
    proc = run_render(out_dir, "--date", date, "--interventions-file", str(stream))
    assert proc.returncode == 0, proc.stderr
    return (out_dir / f"harness-map-{date}.html").read_text()


def test_read_jsonl_reports_line_cap_independently_of_byte_cap(tmp_path):
    """T5.1 — VERIFIED against source: read_jsonl(path, max_bytes=STREAM_MAX_BYTES,
    max_lines=STREAM_MAX_LINES) -- the line cap is a GENUINELY SEPARATE parameter. A stream
    of many compact records trips max_lines while nowhere near 5 MB. A single boolean
    `truncated` conflates them and cannot say which limit to raise."""
    stream = tmp_path / "many.jsonl"
    stream.write_text("".join(json.dumps({"i": i}) + "\n" for i in range(40)))
    caps = {}
    records, malformed, nonblank = rh.read_jsonl(stream, max_lines=10, caps_out=caps)
    assert len(records) == 10
    assert caps == {"lines": True}          # byte cap NOT tripped


def test_read_jsonl_reports_byte_cap(tmp_path):
    """T5.2 — the byte cap keeps the OLDEST records and silently drops the NEWEST: for an
    append-only log the rejected tail IS the newest data, disclosed before S6a only as
    `malformed += 1`, indistinguishable from one bad line."""
    stream = tmp_path / "big.jsonl"
    stream.write_text("".join(json.dumps({"pad": "x" * 100}) + "\n" for _ in range(50)))
    caps = {}
    rh.read_jsonl(stream, max_bytes=1000, caps_out=caps)
    assert caps.get("bytes") is True


def test_read_jsonl_reports_both_caps_when_both_trip(tmp_path):
    """T5.3 — the two caps are detected independently and both are reported."""
    stream = tmp_path / "both.jsonl"
    stream.write_text("".join(json.dumps({"pad": "x" * 100}) + "\n" for _ in range(50)))
    caps = {}
    rh.read_jsonl(stream, max_bytes=1000, max_lines=3, caps_out=caps)
    assert caps == {"bytes": True, "lines": True}


@pytest.mark.parametrize("n_records,trailing_newline,expect_flag", [
    (10, True, False),    # exactly at cap, file ends in "\n" -> split yields a trailing
                          # empty segment; the loop breaks at the boundary but NOTHING was
                          # dropped, so flagging here would be a false blind spot
    (10, False, False),   # exactly at cap, no trailing newline -> no boundary artifact
    (11, True, True),     # one over -> a real record was omitted
])
def test_read_jsonl_line_cap_flags_only_real_overflow(
        tmp_path, n_records, trailing_newline, expect_flag):
    """T5.8 — the off-by-one. `split("\\n")` on newline-terminated content produces a
    terminal empty element, so an `i >= max_lines` branch alone reports truncation for a
    file that fit exactly. That suppresses a severity band that was legitimately earned:
    false doubt is still a false reading, and it would fire on every well-formed file whose
    record count happens to equal the cap."""
    stream = tmp_path / f"cap-{n_records}-{trailing_newline}.jsonl"
    body = "\n".join(json.dumps({"i": i}) for i in range(n_records))
    stream.write_text(body + ("\n" if trailing_newline else ""))
    caps = {}
    records, _malformed, _nonblank = rh.read_jsonl(stream, max_lines=10, caps_out=caps)
    assert len(records) == 10
    assert caps.get("lines", False) is expect_flag


def test_read_jsonl_return_arity_is_three_and_overlay_arity_is_four(tmp_path):
    """T5.7 — finding #2, pinned at RUNTIME (not against the annotation string, which
    renders as `tuple[list[dict[str, typing.Any]], int, int]` and would make this test
    brittle for no gain). Adding a positional element to either return breaks every existing
    unpacking site and its tests — a binding-rule-7 violation — AND ships a dead API for
    M8's drag composite, which does not exist and which §4.4's own fence forbids S6a from
    building. Per-stream detail rides the EXISTING footer dict.
    # Changing either arity requires a spec change (S6 §4.4 item 1)."""
    stream = tmp_path / "arity.jsonl"
    stream.write_text('{"a": 1}\n')
    assert len(rh.read_jsonl(stream)) == 3
    assert len(rh.read_jsonl(stream, caps_out={})) == 3
    overlay = rh.build_friction_overlay(
        _minimal_doc(), {k: None for k in rh.STREAM_ORDER}, {}, "2026-07-15", False)
    assert len(overlay) == 4


def test_truncated_stream_counts_render_as_lower_bounds(tmp_path):
    """T5.4 — the read stopped early, so the true value is UNKNOWN and can only be larger.
    A bare N asserts a completeness the read did not have."""
    text = _truncated_render(tmp_path, "lb")
    assert "≥" in text
    assert re.search(r'<div class="stream-card"><div class="count">≥\d+</div>'
                     r'<h3>Interventions</h3>', text) is not None
    assert "read truncated at the lines cap" in text
    assert "every count above is a lower bound" in text


def test_truncated_stream_paints_no_severity_band(tmp_path):
    """T5.5 — DISCLOSURE ALONE IS NOT THE CONTROL. Round 1 surfaced the blind spot while
    totals and bands went on being computed from the surviving prefix, so a truncated file
    could render CLEAN or LOW. `inaccessible != clean`, applied at the stream level: a
    green band beside a footnote IS a green band."""
    text = _truncated_render(tmp_path, "band")
    gauge = re.search(r'data-gauge="friction_total"[^>]*>(.*?)</button>', text, re.S)
    assert gauge is not None
    assert '<div class="band">' not in gauge.group(1)
    hero = re.search(r'<div class="hero-friction[^"]*">(.*?)<h3>', text, re.S)
    assert hero is not None and '<span class="badge">' not in hero.group(1)


def test_untruncated_stream_still_paints_its_band(tmp_path):
    """T5.6 — the anti-vacuity half of T5.5. Without this, deleting the band entirely would
    make T5.5 pass while destroying the feature."""
    text = _one_stream_render(tmp_path, "untrunc", "--interventions-file", [
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "feedback_note.md"},
    ])
    gauge = re.search(r'data-gauge="friction_total"[^>]*>(.*?)</button>', text, re.S)
    assert gauge is not None and '<div class="band">' in gauge.group(1)
    assert "≥" not in gauge.group(1)
    assert '<div class="stream-card"><div class="count">1</div><h3>Interventions</h3>' in text


def test_truncated_run_lower_bounds_every_visible_count(tmp_path):
    """T5.9 — `≥N` on one surface beside a bare `N` on four others is WORSE than neither:
    the bare numbers read as precision, and the operator has no way to tell which surfaces
    were affected. Every count derived from a truncated read is a lower bound, wherever it
    is displayed.

    Covers the surfaces the stream card does not: the per-component join table and the
    friction-gauge drill decomposition.

    The plan's third clause here asserted an `≥N events (≥N observed, ≥N backfilled)`
    split. That surface DOES NOT EXIST yet -- `events_backfilled` is a footer counter only
    (render_html.py::join_interventions) and nothing renders an observed/backfilled split.
    Task 6 builds it and asserts its `_lb` wrapping; asserting it here would have been red
    for a surface this task is fenced out of building."""
    text = _truncated_render(tmp_path, "everywhere")
    table = re.search(r'<table class="friction-components sortable">(.*?)</table>', text, re.S)
    assert table is not None and "≥" in table.group(1)
    panel = re.search(r'id="gdrawer-friction_total"[^>]*>(.*?)</div>', text, re.S)
    assert panel is not None and "≥" in panel.group(1)


def test_codex_sentence_not_lower_bounded_by_other_streams_truncation(tmp_path):
    """Post-exec Codex finding #2 (S6a). `_codex_sentence`'s `truncated` arg at both call
    sites was `_any_stream_truncated(footer)` -- a RUN-level flag any of the four streams
    can trip. `_render_stream_card` uses the per-stream `_stream_truncated(f)` for the
    codex CARD's count, so a truncated interventions stream made the codex card show an
    exact number while the codex-aggregate SENTENCE beside it claimed a lower bound for a
    read that finished completely -- two widgets disagreeing about the same run on the
    same page. `_codex_stream_truncated` now scopes the sentence to the codex stream's own
    cap.

    Interventions trips the LINE cap (Task 5's real-fixture pattern); codex stays a small,
    entirely untruncated file."""
    out_dir = tmp_path / "codexscope"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    interventions = tmp_path / "codexscope-iv.jsonl"
    record = json.dumps({"timestamp": "2026-07-14T00:00:00",
                         "memory_file": "feedback_note.md"}) + "\n"
    interventions.write_text(record * (rh.STREAM_MAX_LINES + 5))
    codex_file = tmp_path / "codexscope-codex.jsonl"
    codex_file.write_text(
        json.dumps({"ts": "2026-07-01T00:00:00Z", "mode": "plan", "verdict": "APPROVED"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15",
                       "--interventions-file", str(interventions),
                       "--codex-file", str(codex_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    # the truncated stream's own card is still a lower bound (anti-vacuity: the fix must
    # not have accidentally removed EVERY truncation marker)
    assert re.search(r'<div class="stream-card"><div class="count">≥\d+</div>'
                     r'<h3>Interventions</h3>', text) is not None
    # the codex CARD is untouched by this fix (already per-stream) and stays exact
    assert re.search(r'<div class="stream-card"><div class="count">1</div>'
                     r'<h3>Codex reviews</h3>', text) is not None
    # the codex-aggregate SENTENCE (the bug) must also be exact, not lower-bounded
    codex_card = re.search(
        r'<h2>Codex aggregate \(not node-joined\)</h2><p>(.*?)</p>', text, re.S)
    assert codex_card is not None
    assert "≥" not in codex_card.group(1)
    assert "1 Codex review" in codex_card.group(1)


def test_codex_sentence_lower_bounded_when_codex_itself_truncated(tmp_path):
    """Post-exec Codex finding #2, the reverse case: when the codex stream's OWN read
    stops at a cap, its aggregate sentence must still carry the lower bound -- the fix
    narrows the scope to the codex stream, it does not remove the bound entirely."""
    out_dir = tmp_path / "codextrunc"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    codex_file = tmp_path / "codextrunc-codex.jsonl"
    record = json.dumps({"ts": "2026-07-01T00:00:00Z", "mode": "plan",
                         "verdict": "APPROVED"}) + "\n"
    codex_file.write_text(record * (rh.STREAM_MAX_LINES + 5))
    proc = run_render(out_dir, "--date", "2026-07-15", "--codex-file", str(codex_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    codex_card = re.search(
        r'<h2>Codex aggregate \(not node-joined\)</h2><p>(.*?)</p>', text, re.S)
    assert codex_card is not None
    assert "≥" in codex_card.group(1)


def test_component_table_not_lower_bounded_by_codex_only_truncation(tmp_path):
    """Post-exec Codex round 2, finding 2. `joined` never contains a codex record -- codex
    is aggregate-only and never joins a node -- so a codex-only truncation must not mark the
    per-component table's exact counts as lower bounds. Codex trips the LINE cap; the one
    interventions record stays small and untruncated."""
    out_dir = tmp_path / "componenttable"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    codex_file = tmp_path / "componenttable-codex.jsonl"
    record = json.dumps({"ts": "2026-07-01T00:00:00Z", "mode": "plan",
                         "verdict": "APPROVED"}) + "\n"
    codex_file.write_text(record * (rh.STREAM_MAX_LINES + 5))
    interventions = tmp_path / "componenttable-iv.jsonl"
    interventions.write_text(json.dumps({"timestamp": "2026-07-14T00:00:00",
                                         "memory_file": "feedback_note.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15",
                       "--interventions-file", str(interventions),
                       "--codex-file", str(codex_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    table = re.search(r'<table class="friction-components sortable">(.*?)</table>', text, re.S)
    assert table is not None
    assert "≥" not in table.group(1)
    # anti-vacuity: the codex stream's own card must still show the lower bound, proving
    # truncation is live in this fixture and the fix did not simply delete every marker
    assert re.search(r'<div class="stream-card"><div class="count">≥\d+</div>'
                     r'<h3>Codex reviews</h3>', text) is not None


def test_drill_terms_bounded_per_contribution_not_run_wide(tmp_path):
    """Post-exec Codex round 2, findings 2-3. Inside the friction_total drill, each
    decomposition term takes the bound of the stream(s) that FEED it: a codex-only
    truncation lower-bounds the codex term but must leave the joined-telemetry term exact."""
    out_dir = tmp_path / "drillterms"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    codex_file = tmp_path / "drillterms-codex.jsonl"
    record = json.dumps({"ts": "2026-07-01T00:00:00Z", "mode": "plan",
                         "verdict": "APPROVED"}) + "\n"
    codex_file.write_text(record * (rh.STREAM_MAX_LINES + 5))
    interventions = tmp_path / "drillterms-iv.jsonl"
    interventions.write_text(json.dumps({"timestamp": "2026-07-14T00:00:00",
                                         "memory_file": "feedback_note.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15",
                       "--interventions-file", str(interventions),
                       "--codex-file", str(codex_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    panel = re.search(r'id="gdrawer-friction_total"[^>]*>(.*?)</div>', text, re.S)
    assert panel is not None
    body = panel.group(1)
    codex_li = re.search(r'<li>Codex review runs \(not node-joined\): (.*?)</li>', body)
    joined_li = re.search(r'<li>Telemetry events joined to a component: (.*?)</li>', body)
    assert codex_li is not None and joined_li is not None
    assert "≥" in codex_li.group(1)
    assert "≥" not in joined_li.group(1)


def test_interventions_note_not_bounded_by_codex_only_truncation(tmp_path):
    """The friction_total drill's interventions-attribution note reads `segments_joined`
    from the interventions footer entry alone -- a codex-only truncation must not lower-
    bound it."""
    out_dir = tmp_path / "notebound"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    codex_file = tmp_path / "notebound-codex.jsonl"
    record = json.dumps({"ts": "2026-07-01T00:00:00Z", "mode": "plan",
                         "verdict": "APPROVED"}) + "\n"
    codex_file.write_text(record * (rh.STREAM_MAX_LINES + 5))
    interventions = tmp_path / "notebound-iv.jsonl"
    interventions.write_text(json.dumps({"timestamp": "2026-07-14T00:00:00",
                                         "memory_file": "feedback_note.md"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15",
                       "--interventions-file", str(interventions),
                       "--codex-file", str(codex_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    note = re.search(r'<p class="gauge-drill-note">(.*?)</p>', text, re.S)
    assert note is not None
    assert "≥" not in note.group(1)


def test_codex_term_exact_when_only_interventions_truncated(tmp_path):
    """The reverse of the previous three: interventions trips the LINE cap and codex stays
    a single small record. The joined-telemetry drill term takes the interventions bound;
    the codex term must NOT -- the case commit 6d1f5c9 fixed for `_codex_sentence` but not
    for this drill decomposition."""
    out_dir = tmp_path / "reversecase"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    interventions = tmp_path / "reversecase-iv.jsonl"
    record = json.dumps({"timestamp": "2026-07-14T00:00:00",
                         "memory_file": "feedback_note.md"}) + "\n"
    interventions.write_text(record * (rh.STREAM_MAX_LINES + 5))
    codex_file = tmp_path / "reversecase-codex.jsonl"
    codex_file.write_text(
        json.dumps({"ts": "2026-07-01T00:00:00Z", "mode": "plan", "verdict": "APPROVED"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15",
                       "--interventions-file", str(interventions),
                       "--codex-file", str(codex_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    panel = re.search(r'id="gdrawer-friction_total"[^>]*>(.*?)</div>', text, re.S)
    assert panel is not None
    body = panel.group(1)
    codex_li = re.search(r'<li>Codex review runs \(not node-joined\): (.*?)</li>', body)
    joined_li = re.search(r'<li>Telemetry events joined to a component: (.*?)</li>', body)
    assert codex_li is not None and joined_li is not None
    assert "≥" not in codex_li.group(1)
    assert "≥" in joined_li.group(1)


def test_contribution_truncation_table_covers_every_label():
    """Pure-unit drift guard: `_CONTRIBUTION_TRUNCATION`'s keys must exactly match
    `_friction_contributions`'s labels, so a future label edit fails loudly with a
    KeyError at the render site instead of silently mis-bounding a term."""
    assert set(rh._CONTRIBUTION_TRUNCATION) == {
        label for label, _ in rh._friction_contributions({}, [], {"runs": 0})
    }


def test_codex_copy_payload_not_lower_bounded_by_other_streams_truncation(tmp_path):
    """Post-exec Codex finding #2, clipboard-payload half: `build_copy_payloads` had the
    identical `_any_stream_truncated(footer)` bug at its own `_codex_sentence` call site,
    a second home for the same defect. Same fixture shape as the dashboard test above."""
    out_dir = tmp_path / "codexscopepayload"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    interventions = tmp_path / "codexscopepayload-iv.jsonl"
    record = json.dumps({"timestamp": "2026-07-14T00:00:00",
                         "memory_file": "feedback_note.md"}) + "\n"
    interventions.write_text(record * (rh.STREAM_MAX_LINES + 5))
    codex_file = tmp_path / "codexscopepayload-codex.jsonl"
    codex_file.write_text(
        json.dumps({"ts": "2026-07-01T00:00:00Z", "mode": "plan", "verdict": "APPROVED"}) + "\n")
    proc = run_render(out_dir, "--date", "2026-07-15",
                       "--interventions-file", str(interventions),
                       "--codex-file", str(codex_file))
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    island = re.search(r'<script type="application/json" id="copy-friction">(.*?)</script>',
                       text, re.S)
    assert island is not None, "friction copy island missing"
    body = island.group(1)
    # the truncated interventions bullet still carries the note (anti-vacuity)
    assert "read truncated at the lines cap" in body
    # the codex sentence appended after it must NOT be lower-bounded
    assert "1 Codex review" in body
    assert "≥1 Codex review" not in body


def test_truncated_copy_payload_agrees_with_the_dashboard(tmp_path):
    """T5.10 — `build_copy_payloads` INDEPENDENTLY recomputes the friction total and re-bands
    it through `build_overview_model`, so without this fix the clipboard payload renders
    `friction events: 20000 (HIGH)` for the very run whose dashboard shows `≥20000` and no
    band at all. That is the two-homes divergence A3 already cost this codebase once,
    reintroduced between the page and the thing the operator pastes into a ticket.

    Island id VERIFIED against source, not guessed: `_render_copy_island("overview", ...)`
    delegates to `_render_json_island(f"copy-{view_id}", ...)`, so the id is `copy-overview`
    and the body is `esc_json_script(...)` -- json.dumps with ensure_ascii=False, so the
    payload carries a LITERAL "≥", never a "\\u2265" escape."""
    text = _truncated_render(tmp_path, "payload")
    island = re.search(r'<script type="application/json" id="copy-overview">(.*?)</script>',
                       text, re.S)
    assert island is not None, "overview copy island missing"
    body = island.group(1)
    assert "friction events: ≥" in body
    for band in ("CLEAN", "LOW", "HIGH"):
        assert f"({band})" not in body, f"copy payload still bands a truncated total: {band}"


def test_every_friction_count_surface_is_lower_bounded(tmp_path):
    """T5.11 — the anti-rot guard for the per-site threading decision. `_lb` is applied at
    each render site rather than carried by the value itself (see the bypass-proofing box),
    which is only safe if a regression in one of the KNOWN surfaces fails loudly instead of
    silently rendering bare.

    WHAT THIS TEST DOES AND DOES NOT CLAIM. It pins the SIX ENUMERATED surfaces below: if
    one of them regresses to a bare count, or is renamed so its pattern stops matching, this
    test goes red. It CANNOT detect a surface that is not in the dict -- a seventh surface
    added elsewhere is invisible to it, because the dict is the whole of what it inspects.
    `_render_friction_row`'s raw-counters <details> is the live proof of that limit: it
    displays every footer counter and is covered by T5.12, not by this list.

    If you are adding a seventh surface: route it through `_lb`, then add it here -- do not
    delete an entry.
    # Changing this surface list requires a spec change (S6 §5 S-3 / finding #13)."""
    text = _truncated_render(tmp_path, "surfaces")
    surfaces = {
        # ANCHORED to the interventions stream by name: `_render_friction_panel` builds
        # `stream_cards` by iterating `footer` in STREAM_ORDER, so `decisions` is the FIRST
        # card. An unanchored r'<div class="stream-card"><div class="count">(≥?\d+)</div>'
        # matches THAT card -- untruncated, therefore bare, therefore this test would be
        # GUARANTEED RED. Stream cards carry no data-stream attribute; the <h3> label is
        # what identifies one, so the label is what the pattern anchors on.
        "stream card": (r'<div class="stream-card"><div class="count">(≥?\d+)</div>'
                        r'<h3>Interventions</h3>'),
        "friction panel headline": r'<h2>Friction events:\s*(≥?\d+)</h2>',
        "gauge tile value": r'data-gauge="friction_total"[^>]*><div class="v">(≥?\d+)</div>',
        "overview hero count": r'<p class="count">(≥?\d+) events',
        "component table cell": (r'<tr class="friction-component-row"[^>]*>'
                                 r'<td>[^<]*</td><td>(≥?\d+)</td>'),
        "gauge drill decomposition": r'Telemetry events joined to a component: (≥?\d+)',
    }
    bare = []
    for name, pattern in surfaces.items():
        m = re.search(pattern, text)
        assert m is not None, f"surface disappeared or was renamed: {name}"
        if not m.group(1).startswith("≥"):
            bare.append(f"{name} -> {m.group(1)}")
    assert not bare, (
        "these surfaces render a bare count for a TRUNCATED stream; route each through "
        f"_lb(value, truncated): {bare}")


def test_truncated_stream_raw_counters_detail_states_the_lower_bound(tmp_path):
    """T5.12 — finding 1c, round 2. `_render_friction_row` dumps EVERY footer counter as
    bare ints into a visible (expandable) <details>:

        raw = {k: v for k, v in f.items() if k not in ("stream","status","path_display")}

    After Task 5 that dict gains `truncated_at_cap`, which helps -- but it does not make the
    neighbouring `records_parsed` / `lines_nonblank` / `segments_joined` ints honest. They
    are still exact-looking numbers from a read that stopped early.

    Wrapping each value in `_lb` was REJECTED: json.dumps would render "\\u2265123" (the
    default is ensure_ascii=True), turning a readable number into an escape sequence, and it
    would silently change every count-valued key from int to str inside a machine-readable
    dump. A LEAD-IN LINE inside the <details> is the honest form -- it costs zero bytes when
    nothing truncated, changes no value's type, and mirrors the footer sentence's own
    "every count above is a lower bound" wording.

    Anchored to the INTERVENTIONS row, not the first <details>: the rows are emitted in
    STREAM_ORDER, so an unanchored search finds the decisions row (untruncated, no lead-in)
    -- the same first-match trap that made T5.11 guaranteed-red."""
    text = _truncated_render(tmp_path, "rawrow")
    row = re.search(r'<tr><td>interventions</td>.*?'
                    r'<details class="friction-row-detail">(.*?)</details>', text, re.S)
    assert row is not None, "interventions raw-counters detail disappeared or was renamed"
    assert "every count below is a LOWER BOUND" in row.group(1)
    assert "truncated_at_cap" in row.group(1)
    # Anti-vacuity: an UNTRUNCATED row must carry no lead-in at all.
    clean = _one_stream_render(tmp_path, "rawrow-clean", "--interventions-file", [
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "feedback_note.md"},
    ])
    assert "every count below is a LOWER BOUND" not in clean


# ------------------------------------- S6a: attribution disclosure (finding #11, §4.2)
def test_interventions_footer_labels_attribution_inferred(tmp_path):
    """T6.1/T6.2 — VERIFIED means 'read the actual bytes that establish the claim'.
    Basename matching does not establish node identity, so INFERRED is the honest label.
    The footer must also disclose that attribution can REATTRIBUTE across a
    delete-and-recreate -- the failure the anti-smear rule does nothing about, because that
    rule guards against multiple CURRENT matches, not identity over time."""
    text = _one_stream_render(tmp_path, "infer", "--interventions-file", [
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "feedback_note.md"},
    ])
    assert "attribution_evidence" in text and "INFERRED" in text
    assert "joined on `memory_file`" in text or "joined on memory_file" in text
    assert "a rule written in response to friction, not a rule that caused it" in text
    assert "basename" in text and "delete-and-recreate" in text


def test_interventions_footer_shows_the_backfilled_split(tmp_path):
    """T6.3 — 30 of the 48 live records are backfilled. Excluding them drops 62% of the
    signal and makes the number JUMP the moment someone runs a backfill -- a definition
    change masquerading as a trend. Count them, display the split."""
    text = _one_stream_render(tmp_path, "backfill", "--interventions-file", [
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "feedback_note.md",
         "backfilled": True},
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "feedback_note.md"},
    ])
    assert "2 events (1 observed, 1 backfilled)" in text


def test_interventions_footer_surfaces_unmatched_as_a_deletion_signal(tmp_path):
    """T6.4 — a record whose memory file was DELETED persists while the node vanishes, so
    the segment becomes `unmatched`. That is a deletion signal, which is interesting to an
    operator auditing their own harness -- it must appear in WORDS, not only inside the
    collapsed raw-counters detail."""
    text = _one_stream_render(tmp_path, "unmatched", "--interventions-file", [
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "no-such-memory-file.md"},
    ])
    assert "1 unmatched (the named memory file is no longer a node on this map)" in text


def test_friction_total_drill_discloses_inferred_contribution(tmp_path):
    """T6.5 — §14, corrected: `friction_total` is NOT exempt from the no-consequential-
    arithmetic rule. It is arithmetic and it drives a gauge, so accepting basename-injected
    inflation there while prohibiting it in §4.2 cannot stand. Where the aggregate would
    otherwise present as a DETERMINED count, it does not.

    The three-term decomposition itself is untouched -- it must keep reconciling exactly to
    friction_total (its documented invariant)."""
    text = _one_stream_render(tmp_path, "drill", "--interventions-file", [
        {"timestamp": "2026-07-14T00:00:00", "memory_file": "feedback_note.md"},
    ])
    panel = re.search(r'id="gdrawer-friction_total"[^>]*>(.*?)</div>', text, re.S)
    assert panel is not None
    assert "INFERRED" in panel.group(1) and "basename" in panel.group(1)


def test_truncated_drill_note_is_lower_bounded(tmp_path):
    """T6.6 — the drill note this task adds is a friction-derived count on a NEW display
    surface, so it takes the same lower bound as every other one (finding #13). A bare
    `Includes 20005 events` beside a suppressed severity band and five `≥` surfaces is the
    single worst place to print an exact-looking number: this note's entire purpose is to
    say the aggregate is not what it appears.

    Uses Task 5's `_truncated_render` fixture -- a real over-cap file, never a patched
    constant. The note only renders when interventions actually contributed, which the
    fixture's 20,000 joinable records guarantee."""
    text = _truncated_render(tmp_path, "drillnote")
    panel = re.search(r'id="gdrawer-friction_total"[^>]*>(.*?)</div>', text, re.S)
    assert panel is not None
    note = re.search(r'<p class="gauge-drill-note">Includes (≥?[\d,]+) events', panel.group(1))
    assert note is not None, "gauge-drill note disappeared or was renamed"
    assert note.group(1).startswith("≥"), (
        f"the drill note renders a bare count for a TRUNCATED stream: {note.group(1)}")


def test_truncated_backfilled_split_is_lower_bounded(tmp_path):
    """T6.7 — CARRIED FORWARD from Task 5. T5.9's third clause asserted an
    `≥N events (≥N observed, ≥N backfilled)` split against a surface that did not exist
    yet, so Task 5 correctly dropped it; Task 6 builds the surface and owes the assertion.

    T6.3 above pins the split UNTRUNCATED (bare numbers) and T6.6 pins the drill NOTE under
    truncation. Neither pins the split itself under truncation, and that is exactly where a
    half-bounded rendering does the most damage: a `≥20000 events` beside a bare
    `20000 observed, 0 backfilled` reads as though the split were exact when only the total
    was bounded. All THREE numbers take the lower bound together."""
    text = _truncated_render(tmp_path, "truncsplit")
    m = re.search(r'(≥?[\d,]+) events \((≥?[\d,]+) observed, (≥?[\d,]+) backfilled\)', text)
    assert m is not None, "the observed/backfilled split did not render for a truncated stream"
    assert all(g.startswith("≥") for g in m.groups()), (
        f"a truncated split rendered bare numbers: {m.group(0)}")


def _metrics_byte_cap_render(tmp_path, name, date="2026-07-15", bad_line=None):
    """A metrics stream sized to trip ONLY the byte cap: each record carries a large `pad`
    field so the byte budget is crossed in far fewer than `STREAM_MAX_LINES` records --
    isolates `read_jsonl`'s byte-cap sentinel from the line cap, the same isolation
    `_truncated_render` gives the line-cap case in the opposite direction. `bad_line`, when
    given, is a genuinely malformed line prepended so it survives inside the byte budget
    (the byte cap discards only the file's END, never its start)."""
    out_dir = tmp_path / name
    out_dir.mkdir()
    _write_sidecar(out_dir, date, _minimal_doc())
    stream = tmp_path / f"{name}.jsonl"
    record = json.dumps({"date": "2026-07-01", "rework_iterations": 1, "pad": "x" * 600}) + "\n"
    lines = ([bad_line + "\n"] if bad_line else []) + [record] * 8000
    stream.write_text("".join(lines))
    assert stream.stat().st_size > rh.STREAM_MAX_BYTES, "fixture must trip the BYTE cap"
    assert len(lines) < rh.STREAM_MAX_LINES, \
        "fixture must stay far under the LINE cap -- isolates the byte-cap sentinel"
    proc = run_render(out_dir, "--date", date, "--metrics-file", str(stream))
    assert proc.returncode == 0, proc.stderr
    return (out_dir / f"harness-map-{date}.html").read_text()


def test_metrics_byte_cap_sentinel_not_shown_as_invalid_line(tmp_path):
    """Post-exec Codex finding #3 (S6a). `read_jsonl` counts the rejected byte-cap overflow
    tail as one synthetic malformed record (its own comment: "the rejected overflow tail
    counts once as malformed") -- a parse-layer bookkeeping artifact marking WHERE the read
    stopped, not a real invalid line. Before this fix, a metrics stream whose every line is
    syntactically valid but which merely exceeds `max_bytes` rendered "≥1 invalid lines",
    asserting a data-quality problem this stream never had -- in the one stage whose whole
    thesis is evidence honesty. `≥0` (not bare `0`): the metrics stream's OWN read stopped
    at a cap, so every count in its sentence still takes the run's ordinary lower bound
    (finding #3 fixes the sentinel, it does not exempt this sentence from truncation)."""
    text = _metrics_byte_cap_render(tmp_path, "mbytecap")
    assert "read truncated at the bytes cap" in text
    matches = set(re.findall(r"(≥?\d+) invalid lines", text))
    assert matches == {"≥0"}, f"expected every 'invalid lines' surface to read ≥0, got {matches}"


def test_metrics_byte_cap_sentinel_subtraction_keeps_a_real_malformed_line(tmp_path):
    """Post-exec Codex finding #3, the anti-vacuity half: a stream with ONE genuinely
    malformed line AND byte-cap truncation must still report that one real invalid line --
    the fix subtracts exactly the parse-layer sentinel (`max(records_invalid - 1, 0)` when
    the byte cap fired), it does not zero the counter outright. Without the `max(..., 0)`
    floor this would under-report; without the subtraction at all it would over-report by
    one (≥2 instead of ≥1)."""
    text = _metrics_byte_cap_render(tmp_path, "mbytecapreal", bad_line="not valid json")
    assert "read truncated at the bytes cap" in text
    matches = set(re.findall(r"(≥?\d+) invalid lines", text))
    assert matches == {"≥1"}, f"expected every 'invalid lines' surface to read ≥1, got {matches}"


# --------------------------------------------------- S6b §8.1 metric definition versions
def test_definition_version_read_from_the_collector_map():
    doc = {"metric_definitions": {"phantom_ref_count": 4}}
    assert rh.resolve_metric_definition_version(doc, b"x", "phantom_ref_count", {}) == 4


def test_definition_version_rejects_bool_true_which_equals_one_in_python():
    """Requirement 29: `isinstance(True, int)` is True. A stray boolean silently
    resolving as version 1 would report a series comparable when it is not."""
    for bad in (True, False, 0, -1, 1.0, "1", None, [], {}):
        doc = {"metric_definitions": {"phantom_ref_count": bad}}
        assert rh.resolve_metric_definition_version(doc, b"x", "phantom_ref_count", {}) is None, bad


def test_definition_version_rejects_a_non_dict_metric_definitions():
    for bad in ([], "x", 3, None):
        assert rh.resolve_metric_definition_version(
            {"metric_definitions": bad}, b"x", "phantom_ref_count", {}) is None, bad


def test_definition_version_falls_back_to_the_content_digest_table():
    raw = b'{"phantom_refs": []}'
    digest = hashlib.sha256(raw).hexdigest()
    legacy = {digest: {"phantom_ref_count": 2}}
    assert rh.resolve_metric_definition_version({}, raw, "phantom_ref_count", legacy) == 2


def test_collector_map_wins_over_the_legacy_table():
    raw = b'{"x": 1}'
    legacy = {hashlib.sha256(raw).hexdigest(): {"phantom_ref_count": 2}}
    doc = {"metric_definitions": {"phantom_ref_count": 4}}
    assert rh.resolve_metric_definition_version(doc, raw, "phantom_ref_count", legacy) == 4


def test_unknown_digest_resolves_to_unknown_never_an_inferred_version():
    """Requirement 28: an artifact whose digest matches nothing resolves to UNKNOWN."""
    legacy = {hashlib.sha256(b"other").hexdigest(): {"phantom_ref_count": 2}}
    assert rh.resolve_metric_definition_version({}, b"mutated", "phantom_ref_count", legacy) is None


def test_no_date_fallback_exists_anywhere_in_resolution():
    """Requirement 28/30: date-based provenance is UNSOUND here — this repo mutates
    report files and has a same-date overwrite on record. A doc carrying a date and
    nothing else must still resolve to UNKNOWN."""
    doc = {"generated_at": "2026-07-17T10:00:00+00:00", "date": "2026-07-17"}
    assert rh.resolve_metric_definition_version(doc, b"x", "phantom_ref_count", {}) is None
    assert not hasattr(rh, "LEGACY_WINDOW_END")


def test_series_confounded_true_on_more_than_one_distinct_version():
    assert rh.series_confounded([1, 1, 2, 3]) is True


def test_series_confounded_true_when_any_version_is_unknown():
    assert rh.series_confounded([1, 1, None]) is True


def test_series_confounded_false_on_a_uniform_window():
    assert rh.series_confounded([1, 1, 1]) is False
    assert rh.series_confounded([4]) is False
    assert rh.series_confounded([]) is False


def test_marker_first_appearance_is_not_a_transition(tmp_path):
    """Requirement 32, REQUIRED negative test. The run that ships the marker has four
    markerless predecessors that resolve through the legacy table and one marked sidecar.
    Where the predecessors' versions AGREE with the new one, the series must NOT be
    flagged — an `absent -> N` step is the marker's introduction, not an observed change.
    Flagging here would fire a confound on every metric on the run the marker ships."""
    legacy_raws = [b'{"n": 1}', b'{"n": 2}', b'{"n": 3}', b'{"n": 4}']
    legacy = {hashlib.sha256(r).hexdigest(): {"memory_body_count": 1} for r in legacy_raws}
    window = [({}, r) for r in legacy_raws]
    window.append(({"metric_definitions": {"memory_body_count": 1}}, b'{"n": 5}'))
    versions = [rh.resolve_metric_definition_version(d, r, "memory_body_count", legacy)
                for d, r in window]
    assert versions == [1, 1, 1, 1, 1]
    assert rh.series_confounded(versions) is False


def test_no_value_shape_heuristic_is_possible_by_signature():
    """Requirement 35, REQUIRED negative test (§8.4). ANY value-shape heuristic (a large
    jump, a sign flip) fires on `memory_body_count` 98 -> 117 — the one real drift finding
    in the operator's dataset — converting the single true positive into a false negative.
    `series_confounded` takes VERSIONS ONLY and cannot see values, so the heuristic is
    unwritable without changing this signature. This test exists to stop anyone shipping
    one later.

    Changing this contract requires a spec change (S6 §8.4)."""
    import inspect
    params = list(inspect.signature(rh.series_confounded).parameters)
    assert params == ["versions"], params
    # Identical versions, wildly different implied values -> still not confounded.
    assert rh.series_confounded([1, 1, 1, 1]) is False
    # And a version change with no value movement at all IS confounded.
    assert rh.series_confounded([1, 1, 2, 2]) is True


def test_confounded_reason_is_factual_only_and_carries_no_verdict_word():
    """Requirement 33: renderer-generated, dates and version numbers, no verdict word."""
    reason = rh.build_confounded_reason(
        "phantom_ref_count",
        [("2026-07-17", 1), ("2026-07-24", 2), ("2026-07-31", 3), ("2026-08-02", None)])
    assert reason == ("phantom_ref_count — 2026-07-17: definition v1; "
                      "2026-07-24: definition v2; 2026-07-31: definition v3; "
                      "2026-08-02: definition unknown")
    low = reason.lower()
    for word in rh._CONFOUND_REASON_FORBIDDEN:
        assert word not in low, word


def test_confounded_reason_is_deterministic_regardless_of_input_order():
    a = rh.build_confounded_reason("m", [("2026-07-24", 2), ("2026-07-17", 1)])
    b = rh.build_confounded_reason("m", [("2026-07-17", 1), ("2026-07-24", 2)])
    assert a == b


def test_legacy_table_keys_are_sha256_digests_and_the_table_is_frozen():
    """Requirement 27/28: CLOSED, FROZEN, digest-keyed. Every key is 64 lowercase hex."""
    for key in rh.LEGACY_METRIC_DEFINITIONS:
        assert re.fullmatch(r"[0-9a-f]{64}", key), key
    for values in rh.LEGACY_METRIC_DEFINITIONS.values():
        for metric, version in values.items():
            assert rh._valid_definition_version(version), (metric, version)


def test_legacy_table_entries_carry_all_fourteen_metric_definition_keys():
    """The plan requires every legacy entry to carry all fourteen METRIC_DEFINITIONS keys
    explicitly — a metric absent from an entry resolves to None -> UNKNOWN -> confounded,
    which would flag all fourteen series as 100% noise exactly when the mechanism is
    introduced. There is no default; each key must be present."""
    from collector import METRIC_DEFINITIONS
    expected_keys = set(METRIC_DEFINITIONS)
    assert len(expected_keys) == 14, expected_keys
    for digest, values in rh.LEGACY_METRIC_DEFINITIONS.items():
        assert set(values) == expected_keys, (digest, set(values) ^ expected_keys)


# ------------------------------------------------------------ T5.1 standing totality guard
# Every field below has, at some point in S6b, arrived from untrusted sidecar/definition
# JSON and been fed — unguarded — into an operation that HASHES or ORDER-COMPARES it
# (`dict.get`, `x in <frozenset>`, `set(...)`, `sorted(..., key=...)`), turning one
# hostile-but-valid-JSON value into a whole-page render failure instead of a degraded
# row. Four confirmed instances, all in this module, none caught by reading the code:
#   1. `_phantom_guidance`                          -- `dict.get(kind)` on an unhashable
#      kind (T3.1; caught by review before ship).
#   2. `_phantom_group_key` / `_phantom_status_word` -- bare `kind in <frozenset>` on an
#      unhashable kind (T4; caught when T3.1's OWN end-to-end test failed under T4).
#   3. `series_confounded`                          -- `set(resolved)` on an unhashable
#      element (T5.1; found only by executing the predicate, not by reading it).
#   4. `build_confounded_reason`                    -- `sorted(..., key=lambda p: p[0])`
#      comparing an incomparable date (T5.1; same discovery path as #3).
# Reactive per-instance fixes were not closing this pattern, so this test drives EVERY
# S6b function that reads untrusted content with a hostile-value matrix and asserts NONE
# may raise — a fifth instance fails HERE, at the source, instead of surviving to audit.
#
# Add a new S6b function's `(callable, argument-builder)` pair to `_TOTALITY_TARGETS` the
# day it is written if it reads a value straight out of sidecar/definition JSON — that is
# the whole reason the table is a tuple of pairs and not nine separate assertions.
#
# `sidecar_bytes` (on `resolve_metric_definition_version`) and the outer `doc`/`legacy`
# CONTAINER shapes are held at a fixed, valid shape rather than hostile-varied: unlike
# `kind`/`resolved`/a version NUMBER (all of which are attacker/corruption-controlled leaf
# content read straight out of parsed JSON), `sidecar_bytes` is guaranteed bytes by its
# only production source (`Path.read_bytes()`, and `hashlib.sha256` legitimately rejects
# non-bytes), `doc`/`legacy` are guaranteed dict-or-None by the collector's own JSON-object
# parse layer, and `metric` is always one of the 14 internal `METRIC_DEFINITIONS` literals
# — never externally supplied. The hostile matrix instead targets the one position in
# `resolve_metric_definition_version` that genuinely IS untrusted leaf content:
# `doc["metric_definitions"][metric]`, exactly what
# `test_definition_version_rejects_bool_true_which_equals_one_in_python` already probes
# and what `_valid_definition_version` exists to guard. This narrowing is deliberate, not
# an omission — see the `sidecar_bytes` note in the T5.1 dispatch for the same principle
# applied to the file-bytes parameter.
_TOTALITY_HOSTILE_VALUES = (
    [], {}, b"x", 0, -1, 1.0, "1", True, False, None, "",
    float("nan"), "x" * 10000, {"a": [1, {"b": 2}]},
)

_TOTALITY_TARGETS = (
    # T3.1
    (rh._phantom_guidance, lambda v: (v, v)),
    # T4
    (rh._phantom_group_key, lambda v: ({"kind": v, "resolved": v},)),
    (rh.build_phantom_groups, lambda v: ([{"kind": v, "resolved": v}],)),
    (rh._phantom_never_resolvable_count, lambda v: ([{"kind": v, "resolved": v}],)),
    (rh._phantom_status_word, lambda v: (v, v)),
    # T5 / T5.1
    (rh._valid_definition_version, lambda v: (v,)),
    (rh.resolve_metric_definition_version,
     lambda v: ({"metric_definitions": {"phantom_ref_count": v}}, b"x",
                "phantom_ref_count", {})),
    (rh.series_confounded, lambda v: ([v],)),
    # A second, DIFFERENTLY-TYPED tuple in the window forces the sort key to actually
    # COMPARE against `v` rather than merely compute it — a single-element list never
    # exercises the comparison that broke #4 above.
    (rh.build_confounded_reason, lambda v: ("m", [(v, 1), ("2026-01-02", 2)])),
    # S6c Task 1: `_collector._metric_quality` is a new total function over
    # collection-derived structures whose shapes vary with a hostile filesystem — not an
    # S6b sidecar reader (both its arguments are built THIS SAME RUN, never parsed back
    # out of a past run's JSON), but the guard is free and the property it checks
    # (never raise on a malformed argument shape) is the same one every entry above
    # earns its place by. Two entries: the hostile value as an `inaccessible[]` ENTRY
    # (exercising the per-entry `.get("path", "")` guard), and the hostile value as the
    # whole `duplication_section` (exercising the `.get("pairs", [])` guard).
    (_collector._metric_quality, lambda v: ([v], {"pairs": []})),
    (_collector._metric_quality, lambda v: ([], v)),
    # S6c Task 2: the six derived-trend extractors read untrusted sidecar JSON leaves the
    # same way S6b's phantom/definition readers do. The two ratio extractors are the
    # highest-value entries here -- `{value, total, ratio}` construction over a hostile
    # `total` is exactly the "compare or divide an untrusted value without a shape guard"
    # class this guard exists for.
    (rh._derived_promotion_candidate_count, lambda v: ({"promotion_candidates": v},)),
    (rh._derived_memory_body_count, lambda v: ({"on_demand": {"memory_bodies": v}},)),
    (rh._derived_phantom_ref_count, lambda v: ({"phantom_refs": v},)),
    (rh._derived_phantom_confirmed_count, lambda v: ({"phantom_refs": [{"resolved": v}]},)),
    (rh._derived_hooks_test_ratio,
     lambda v: ({"test_coverage": {"summary": {"hooks_with_test": v, "hooks_total": v}}},)),
    (rh._derived_skills_test_ratio,
     lambda v: ({"test_coverage": {"summary": {"skills_with_test": v, "skills_total": v}}},)),
    (rh.build_derived_trend_model,
     lambda v: ([("2026-01-01", {
         "headline": {}, "errors": [], "promotion_candidates": v, "phantom_refs": v,
         "on_demand": {"memory_bodies": v},
         "test_coverage": {"summary": {"hooks_with_test": v, "hooks_total": v,
                                        "skills_with_test": v, "skills_total": v}}})],)),
    # S6c Task 3: the classifier consumes point values straight out of untrusted sidecar
    # JSON -- precisely this guard's subject. THREE entries, because the hostile value has
    # three genuinely different jobs to do here:
    #   * as a POINT (and a DATE) inside well-shaped lists -- the arithmetic/comparison
    #     positions, where `all(value == first ...)` and `last > first` live;
    #   * as the whole `points`/`dates` ARGUMENT -- a non-list where the window slice and
    #     the 1:1 alignment check happen;
    #   * as `polarity`/`latest_direction`/`comparability` -- the membership test that
    #     would raise TypeError on an unhashable value if it were ever a set instead of a
    #     tuple, and the `str(...)` the refusal path takes.
    (rh._trend_point_value, lambda v: (v,)),
    (rh.trend_verdict, lambda v: ([v, v, v], [v, v, v], v, v, v)),
    (rh.trend_verdict, lambda v: (v, v, "up", "good", rh.COMPARABLE)),
    # S6c Task 4: the three-axis comparability engine. `collection_scope`,
    # `metric_quality` and a ratio's `total` are all sidecar leaves read straight out of
    # parsed JSON -- exactly this guard's subject, and each one reaches an operation the
    # block comment above names: `dict.get` on a possibly-unhashable metric key, a
    # whole-dict `==` scan, and a `sorted(..., key=...)` over mixed-typed rows. Every
    # axis gets the hostile value BOTH as a leaf inside a well-shaped list (the
    # comparison position) AND as the whole argument (the window-slice position), the
    # same two jobs Task 3's entries split.
    (rh.metric_quality_state,
     lambda v: ({"metric_quality": {"memory_body_count": v}}, "memory_body_count")),
    (rh.metric_quality_state, lambda v: ({"metric_quality": v}, "memory_body_count")),
    # the METRIC position: an unhashable key would raise inside `dict.get` itself
    (rh.metric_quality_state,
     lambda v: ({"metric_quality": {"memory_body_count": "complete"}}, v)),
    (rh.metric_quality_state, lambda v: (v, "memory_body_count")),
    (rh._scope_readable, lambda v: (v,)),
    (rh.scope_comparable,
     lambda v: ([v, {"root": "/r", "project_root": None, "compose": False}],)),
    (rh.scope_comparable, lambda v: (v,)),
    # the hostile value as each scope FIELD, which is where the type guards live
    (rh.scope_comparable,
     lambda v: ([{"root": v, "project_root": v, "compose": v},
                 {"root": "/r", "project_root": None, "compose": False}],)),
    (rh._scope_display, lambda v: (v,)),
    # A second, DIFFERENTLY-TYPED row forces the sort key to actually COMPARE, the same
    # reason `build_confounded_reason`'s entry above carries a second tuple.
    (rh.build_scope_reason,
     lambda v: ([(v, v),
                 ("2026-01-02", {"root": "/r", "project_root": None, "compose": False})],)),
    (rh.build_scope_reason, lambda v: (v,)),
    (rh.quality_comparable, lambda v: ([v, "complete"],)),
    (rh.quality_comparable, lambda v: (v,)),
    (rh.build_quality_reason, lambda v: (v, [(v, v), ("2026-01-02", "complete")])),
    (rh.build_quality_reason, lambda v: ("memory_body_count", v)),
    (rh._trend_point_denominator, lambda v: (v,)),
    (rh._trend_point_denominator, lambda v: ({"total": v},)),
    (rh._observed_denominators, lambda v: ([v, v, 20],)),
    (rh._observed_denominators, lambda v: (v,)),
    (rh.denominators_comparable, lambda v: ([v, 20],)),
    (rh.denominators_comparable, lambda v: (v,)),
    (rh.build_denominator_reason, lambda v: (v, [(v, v), ("2026-01-02", 20)])),
    (rh.build_denominator_reason, lambda v: ("hooks_with_test_ratio", v)),
    (rh._dated_axis, lambda v: (v, v)),
    (rh._dated_axis, lambda v: ([v], [v])),
    (rh.series_comparability, lambda v: (v, v, v, v, v, v)),
    (rh.series_comparability,
     lambda v: ("memory_body_count", ["2026-01-01"], [v], [v], [v], [v])),
    # S6c Task 5: the basis digest and its row resolver both read model-authored sidecar
    # JSON, untrusted by the same argument the entries above already make -- a hostile
    # `trend_basis` row, a hostile leaf inside a `points`/`definition_versions`/`scopes`
    # window, and the whole argument in the window-slice position, the same two jobs
    # Task 4's entries split.
    (rh.trend_inputs_digest, lambda v: (v, v, v, v, v)),
    (rh.trend_inputs_digest,
     lambda v: ("memory_body_count", [v, (v, v, v)], [v], [v], v)),
    (rh.trend_basis_for, lambda v: (v, v, v, v, v, v)),
    (rh.trend_basis_for,
     lambda v: ([{"metric": v, "inputs_digest": v, "prose": v}], v, [v], [v], [v], v)),
    # S6c Task 7: the wiring. Every reader below is handed model- or sidecar-derived state
    # -- a series dict, a provenance table, a date used as a dict KEY, a derived per-date
    # value -- and each reaches an operation the block comment above names: `dict.get` on a
    # possibly-unhashable key, a length comparison against a non-sequence, and arithmetic on
    # an untrusted `ratio`. Same two jobs Tasks 3-5 split: the hostile value as a leaf, and
    # as the whole argument.
    # The SERIES container is held at a valid dict, the same narrowing the `doc`/`legacy`
    # note above records and for the same reason: both producers are this module's own
    # builders, and every test that hand-builds one builds a dict. The untrusted leaf is
    # `series["point_dates"]`, which is what the hostile matrix targets.
    (rh._series_point_dates, lambda v: ({"point_dates": v},)),
    (rh._trend_window, lambda v: ({"points": [v], "point_dates": v},)),
    (rh._provenance_record, lambda v: (v, v)),
    (rh._provenance_record, lambda v: ({"by_date": {"2026-01-01": v}}, v)),
    (rh._provenance_metric, lambda v: ({"quality": v}, "quality", v, None)),
    (rh._provenance_metric,
     lambda v: ({"quality": {"memory_body_count": v}}, "quality", v, None)),
    (rh._series_axes, lambda v: (v, [v], [v], v)),
    (rh._series_axes,
     lambda v: ("memory_body_count", ["2026-01-01"], [v],
                {"by_date": {"2026-01-01": {"scope": v, "quality": v, "versions": v}}})),
    (rh.build_trend_provenance, lambda v: (v, v)),
    (rh.build_trend_provenance, lambda v: ([(v, v)], {"x": v})),
    (rh._verdict_slug, lambda v: (v,)),
    (rh._fmt_trend_value, lambda v: (v,)),
    (rh._fmt_trend_value, lambda v: ({"value": v, "total": v, "ratio": v},)),
    (rh._trend_latest_direction, lambda v: (v, v)),
    (rh._trend_latest_direction,
     lambda v: ({"first_run": False, "series": [{"key": "k", "polarity": v,
                                                 "points": [v, v]}]}, "k")),
)


def test_every_s6b_function_reading_untrusted_json_is_total():
    """Standing guard against the defect class named in the block comment above: no
    function in `_TOTALITY_TARGETS`, called with any value in `_TOTALITY_HOSTILE_VALUES`
    in the position that value's argument-builder places it, may raise. A raise here means
    a new S6b function (or an edit to an existing one) reintroduced "hash/compare an
    untrusted value without a shape guard" — fix the function, do not narrow this test."""
    failures = []
    for fn, build_args in _TOTALITY_TARGETS:
        for value in _TOTALITY_HOSTILE_VALUES:
            args = build_args(value)
            try:
                fn(*args)
            except Exception as exc:  # noqa: BLE001 -- totality proof: ANY raise is a bug
                failures.append((fn.__name__, value, type(exc).__name__, str(exc)))
    assert failures == [], failures


# ================================= S6c Task 3: TREND_VERDICTS + the verdict classifier
# EVERY assertion below calls the classifier DIRECTLY. Nothing in this section renders,
# because nothing reaches HTML yet: the S6b comparability functions shipped unwired and
# Task 7 owns the whole rendered half of this contract (verdict strings drawn only from
# the enum, the retired words absent, and the two zero-delta states rendering
# differently). This is the same shape as the shipped S6b precedent --
# `test_marker_first_appearance_is_not_a_transition` and
# `test_no_value_shape_heuristic_is_possible_by_signature` both call their function with
# no render in the loop.
#
# `verdict_text` (never `text`) is the binding for a classifier result. `text` means
# rendered HTML everywhere else in this suite.
_DAILY_DATES = [f"2026-07-{d:02d}" for d in range(1, 15)]


def _dates_for(points, start=1):
    """Point-aligned dates, one calendar day apart -- the alignment `trend_verdict`
    requires (`dates` is 1:1 with `points`, so the span is measured over the points that
    actually contributed)."""
    return [f"2026-07-{start + i:02d}" for i in range(len(points))]


def test_verdict_enum_is_exactly_seven_values_in_strict_order():
    """S6 §6.3. Guard states pre-empt direction states, so ORDER is contract, not
    presentation. Asserts over the WORD column of the seven (word, gate, polarity)
    rows. # Changing this value requires a spec change (S6 §6.3)."""
    assert [row[0] for row in rh.TREND_VERDICTS] == [
        "not measured", "not comparable", "no direction", "unchanged across N",
        "net unchanged", "improving", "worsening"]
    # three columns, one per column of report-template.md's TREND_VERDICT_TABLE block
    # (Task 6 single-sources the two homes against each other).
    assert all(len(row) == 3 for row in rh.TREND_VERDICTS)


def test_below_floor_two_points_gets_no_direction_word():
    """SPARKLINE_MIN_POINTS is THE one minimum-point rule. 2 is the largest series that
    must NOT verdict, and the reason is visible (`2 pts · needs 3`) rather than a blank.
    # Changing this format requires a spec change (S6 §6.3)."""
    points = [10, 20]
    verdict = rh.trend_verdict(points=points, dates=_dates_for(points), polarity="up")
    assert verdict.word == "not measured"
    assert "needs 3" in verdict.reason
    assert "2 pts" in verdict.reason
    # the floor is the constant, never a typed literal
    assert f"needs {rh.SPARKLINE_MIN_POINTS}" in verdict.reason


def test_at_floor_three_points_the_verdict_appears():
    """The 2->3 transition is the only thing proving the gate is `>= 3`."""
    two = [10, 20]
    three = [10, 20, 30]
    assert rh.trend_verdict(points=two, dates=_dates_for(two),
                            polarity="up").word == "not measured"
    assert rh.trend_verdict(points=three, dates=_dates_for(three),
                            polarity="up").word == "worsening"


def test_the_two_zero_delta_states_are_distinct_verdicts():
    """`net unchanged` (10, 20, 10) means 'moved and came back to baseline';
    `unchanged across N` (10, 10, 10) means 'never moved at any measured point'. Same
    arithmetic, opposite meanings. Task 7 asserts they also RENDER differently -- if
    they collapse at either layer the distinction is decorative."""
    moved = [10, 20, 10]
    flat = [10, 10, 10]
    returned_verdict = rh.trend_verdict(points=moved, dates=_dates_for(moved), polarity="up")
    flat_verdict = rh.trend_verdict(points=flat, dates=_dates_for(flat), polarity="up")
    assert returned_verdict.word == "net unchanged"
    assert flat_verdict.word == "unchanged across N"
    assert returned_verdict.word != flat_verdict.word
    assert returned_verdict.text != flat_verdict.text
    # the N in the word is interpolated in the display text, never left as the letter
    assert "unchanged across 3 measured runs" in flat_verdict.text
    assert "measured runs" not in returned_verdict.text


def test_polarity_none_yields_no_direction():
    """Regression test for §6.7's round-1 table, which printed `improving` for
    always_loaded_file_count -- a direction claim about a metric declared to have no
    good direction."""
    points = [10, 8, 6]
    assert rh._HEADLINE_POLARITY["always_loaded_file_count"] == "none"
    verdict = rh.trend_verdict(points=points, dates=_dates_for(points), polarity="none")
    assert verdict.word == "no direction"
    assert "improving" not in verdict.text
    assert "worsening" not in verdict.text


def test_dual_horizon_disagreement_carries_both_clauses():
    """100 -> 50 -> 90. Net improving over the window while the LATEST interval
    worsens. Asserting only the net word does not discharge this -- both clauses are
    the contract."""
    points = [100, 50, 90]
    verdict = rh.trend_verdict(points=points, dates=_dates_for(points), polarity="up",
                               latest_direction="bad")
    verdict_text = verdict.text
    assert verdict.word == "improving"
    assert "net improving over 3 measured runs" in verdict_text
    assert "latest interval worsening" in verdict_text


def test_dual_horizon_agreement_uses_the_plain_form():
    """The other half of the same contract: when the two horizons agree there is only
    one claim to make, and padding it with a redundant second clause would train the
    operator to skim past the disagreement case that matters."""
    points = [100, 90, 80]
    verdict = rh.trend_verdict(points=points, dates=_dates_for(points), polarity="up",
                               latest_direction="good")
    assert verdict.word == "improving"
    assert "latest interval" not in verdict.text
    assert "measured runs" not in verdict.text


def test_every_verdict_except_not_measured_states_point_count_and_date_span():
    """The `N pts / Md` contract has TWO halves and the date span is the half most
    likely to be dropped. `improving  4 pts / 14d` is honest; bare `improving` is not --
    four samples across two weeks and four across two years are different claims.
    # Changing this format requires a spec change (S6 §6.3)."""
    points = [7685, 7961, 7944, 6654]
    dates = ["2026-07-01", "2026-07-05", "2026-07-11", "2026-07-15"]   # span 14 days
    verdict = rh.trend_verdict(points=points, dates=dates, polarity="up")
    verdict_text = verdict.text
    assert "4 pts" in verdict_text and "14d" in verdict_text
    # every non-`not measured` verdict, not just the direction ones
    for polarity, comparability, expected in (
            ("none", rh.COMPARABLE, "no direction"),
            ("up", "scope changed on 2026-07-11", "not comparable"),
            ("up", rh.COMPARABLE, "improving")):
        other = rh.trend_verdict(points=points, dates=dates, polarity=polarity,
                                 comparability=comparability)
        assert other.word == expected
        assert "4 pts" in other.text and "14d" in other.text


def test_span_is_measured_over_the_points_not_over_the_window():
    """A count and a span drawn from DIFFERENT sets is the mismatch the companions exist
    to prevent. Three points that all landed inside one day is a different claim from
    three spread across a fortnight, and both must be stated honestly."""
    points = [10, 20, 30]
    same_day = ["2026-07-15", "2026-07-15", "2026-07-15"]
    spread = ["2026-07-01", "2026-07-08", "2026-07-15"]
    assert "3 pts · 0d" in rh.trend_verdict(points=points, dates=same_day,
                                            polarity="up").reason
    assert "3 pts · 14d" in rh.trend_verdict(points=points, dates=spread,
                                             polarity="up").reason


def test_polarity_both_directions_in_one_run():
    """THE load-bearing polarity fixture. A falling lower-is-better metric beside a
    rising higher-is-better metric proves nothing -- both are 'improving' and a
    classifier that infers direction FROM THE DATA passes. Move both in the SAME
    numeric direction and demand OPPOSITE verdicts.

    No HEADLINE_KEYS row carries polarity "down", so this is the first exercise of a
    down-polarity series anywhere in the suite -- for the CLASSIFIER. The companion test
    below covers `_trend_delta`'s own down arm."""
    # always_loaded_tokens_est 6000->6500->7000 (polarity "up")   => worsening
    tokens = [6000, 6500, 7000]
    # hooks_with_test_ratio    12/20->14/20->16/20 (polarity "down") => improving
    ratios = [{"value": 12, "total": 20, "ratio": 0.6},
              {"value": 14, "total": 20, "ratio": 0.7},
              {"value": 16, "total": 20, "ratio": 0.8}]
    assert rh._HEADLINE_POLARITY["always_loaded_tokens_est"] == "up"
    assert dict((k, p) for k, _, p in rh.DERIVED_TREND_KEYS)["hooks_with_test_ratio"] == "down"
    rising = rh.trend_verdict(points=tokens, dates=_dates_for(tokens), polarity="up")
    also_rising = rh.trend_verdict(points=ratios, dates=_dates_for(ratios), polarity="down")
    assert rising.word == "worsening"
    assert also_rising.word == "improving"


def test_trend_delta_down_branch_runs_on_a_derived_series():
    """FIRST-EVER COVERAGE of `_trend_delta`'s `polarity == "down"` arm, which has never
    executed: zero HEADLINE_KEYS rows carry that polarity, and until S6c no other model
    existed to feed it.

    Reachable ONLY because Task 2 mirrors build_trend_model's shape closely enough that
    `_trend_delta` serves BOTH models. Calling it with the derived model is a NEW CALL
    SITE, not an edit -- `_trend_delta` itself is untouched.

    PRECISE CLAIM, because the plain form is not achievable and overclaiming it would be
    worse than not writing the test: a ratio series' points are `{value, total, ratio}`
    DICTS, and `_trend_delta` -> `finite_number(dict)` is None, so
    `rh._trend_delta(model, "hooks_with_test_ratio")` returns None on the raw model
    (measured, not assumed -- asserted below). The points are therefore mapped through
    `_trend_point_value`, the SAME normalizer the classifier uses, before the call.
    `_trend_delta`'s down arm then executes for the first time, on values that came out
    of `build_derived_trend_model`, with `_trend_delta` unmodified.

    A rising ratio under polarity "down" must read GOOD; the up-polarity metric rising
    in the same fixture must read BAD."""
    dated_docs = [("2026-07-13", _trend_doc(hooks=(12, 20), phantom=(1, 1))),
                  ("2026-07-14", _trend_doc(hooks=(14, 20), phantom=(2, 2))),
                  ("2026-07-15", _trend_doc(hooks=(16, 20), phantom=(3, 3)))]
    model = rh.build_derived_trend_model(dated_docs)
    # the raw model cannot reach the down arm -- stated as a measurement, not a belief
    assert rh._trend_delta(model, "hooks_with_test_ratio") is None
    numeric = {"first_run": model["first_run"],
               "series": [dict(series,
                               points=[rh._trend_point_value(p) for p in series["points"]])
                          for series in model["series"]]}
    assert rh._trend_delta(numeric, "hooks_with_test_ratio")[1] == "good"
    # the up-polarity metric rising in the SAME fixture must read the other way
    assert rh._trend_delta(numeric, "phantom_ref_count")[1] == "bad"


def test_non_finite_point_in_the_window_yields_not_measured():
    """A NaN must not produce a green verdict: `nan > prev` is False and a down arrow
    reads 'good' under polarity `up`, so the naive path emits a plausible-looking
    IMPROVING for corrupt data -- the A19b class, in the reassuring direction. Mirror
    `_coerce_floats`' all-or-nothing guard rather than narrowing it."""
    for bad in (float("nan"), float("inf"), "12", None):
        points = [10, bad, 30]
        verdict = rh.trend_verdict(points=points, dates=_dates_for(points), polarity="up")
        assert verdict.word == "not measured", bad
        assert "improving" not in verdict.text, bad
    # a ratio point whose `ratio` is missing is unusable the same all-or-nothing way
    unusable = [{"value": 0, "total": 0, "ratio": None},
                {"value": 1, "total": 2, "ratio": 0.5},
                {"value": 2, "total": 2, "ratio": 1.0}]
    assert rh.trend_verdict(points=unusable, dates=_dates_for(unusable),
                            polarity="down").word == "not measured"


def test_rise_then_fall_mirrors_the_real_series():
    """7685, 7961, 7944, 6654."""
    points = [7685, 7961, 7944, 6654]
    assert points[1] > points[0]   # anti-vacuity guard: a future edit cannot flatten the
                                   # data and leave this passing while testing nothing
    assert points[-1] < points[0]
    verdict = rh.trend_verdict(points=points, dates=_dates_for(points), polarity="up",
                               latest_direction="good")
    assert verdict.word == "improving"


def test_series_length_honesty_three_versus_four():
    """Two calls; the STATED counts must DIFFER, else a hardcoded string passes."""
    three = [10, 20, 30]
    four = [10, 20, 30, 40]
    three_verdict = rh.trend_verdict(points=three, dates=_dates_for(three), polarity="up")
    four_verdict = rh.trend_verdict(points=four, dates=_dates_for(four), polarity="up")
    assert "3 pts" in three_verdict.reason
    assert "4 pts" in four_verdict.reason
    assert three_verdict.reason != four_verdict.reason


def test_window_and_verdict_read_the_same_slice():
    """12 points: the verdict reads points[-SPARKLINE_WINDOW:], the same slice the
    sparkline draws. The first two points are chosen so the WHOLE series and the WINDOW
    disagree -- a classifier reading the whole series returns the opposite word."""
    points = [1000, 900] + list(range(10, 20))
    assert len(points) == 12
    assert points[-1] < points[0]     # whole series falls  -> `improving` under "up"
    assert points[-1] > points[2]     # window rises        -> `worsening` under "up"
    dates = _dates_for(points)
    verdict = rh.trend_verdict(points=points, dates=dates, polarity="up")
    assert verdict.word == "worsening"
    assert f"{rh.SPARKLINE_WINDOW} pts" in verdict.reason
    # the same slice `_sparkline_cell` draws
    series = {"key": "k", "polarity": "up", "values": points, "points": points}
    assert len(rh._series_points(series)[-rh.SPARKLINE_WINDOW:]) == rh.SPARKLINE_WINDOW


def test_refusing_comparability_pre_empts_every_direction_word():
    """S6 §6.3 strict order: a refusing record pre-empts direction. Task 4 supplies the
    records; Task 3 pins that the parameter is honoured and that its factual reason
    survives into the text."""
    points = [100, 50, 10]
    reason = "memory_body_count — 2026-07-13: definition v1; 2026-07-15: definition v2"
    verdict = rh.trend_verdict(points=points, dates=_dates_for(points), polarity="up",
                               latest_direction="good", comparability=reason)
    assert verdict.word == "not comparable"
    assert reason in verdict.text
    assert "improving" not in verdict.text


def test_classifier_returns_only_enum_words():
    """Vocabulary is ASSERTED, not assumed. Drive the classifier across every shape
    Tasks 3-5 construct and assert the word is always drawn from TREND_VERDICTS.
    Task 7 asserts the same property of RENDERED output."""
    words = {row[0] for row in rh.TREND_VERDICTS}
    shapes = ([], [10], [10, 20], [10, 10, 10], [10, 20, 10], [10, 20, 30], [30, 20, 10],
              [1000, 900] + list(range(10, 20)), [10, float("nan"), 30], [10, "12", 30],
              [{"value": 1, "total": 2, "ratio": 0.5}, {"value": 1, "total": 2, "ratio": 0.5},
               {"value": 2, "total": 2, "ratio": 1.0}],
              [{"value": 1, "total": 0, "ratio": None}] * 3)
    seen = set()
    for points in shapes:
        for polarity in ("up", "down", "none", None, "sideways"):
            for latest in (None, "good", "bad", "neutral"):
                for comparability in (rh.COMPARABLE, "scope changed on 2026-07-11"):
                    verdict = rh.trend_verdict(points=points, dates=_dates_for(points),
                                               polarity=polarity, latest_direction=latest,
                                               comparability=comparability)
                    assert verdict.word in words, (points, polarity, latest, verdict)
                    seen.add(verdict.word)
    # anti-vacuity: the sweep must actually reach every one of the seven, or it is
    # asserting membership over a set it never populated.
    assert seen == words


def test_verdict_is_deterministic_across_repeat_calls():
    """Binding rule 9: fixed orderings, no set iteration into output."""
    points = [10, 20, 15, 40]
    dates = _dates_for(points)
    first = rh.trend_verdict(points=points, dates=dates, polarity="up")
    second = rh.trend_verdict(points=list(points), dates=list(dates), polarity="up")
    assert first == second


def test_trend_point_value_normalizes_both_model_shapes():
    """One normalizer for both trend models: `build_trend_model` emits bare numbers,
    `build_derived_trend_model` emits `{value, total, ratio}` for the two ratio series.
    Without this, a ratio series is unusable to the classifier and both ratio rows would
    read `not measured` forever."""
    assert rh._trend_point_value(7) == 7.0
    assert rh._trend_point_value(0.6) == 0.6
    assert rh._trend_point_value({"value": 12, "total": 20, "ratio": 0.6}) == 0.6
    assert rh._trend_point_value({"value": 0, "total": 0, "ratio": None}) is None
    assert rh._trend_point_value("12") is None
    assert rh._trend_point_value(True) is None          # bool is not a measurement
    assert rh._trend_point_value(float("nan")) is None
    assert rh._trend_point_value(None) is None


def test_trend_doc_is_comparable_and_measurable_on_all_six_derived_series():
    """The `_trend_doc` contract itself (Step 1): no-argument call => comparable on all
    three axes AND a measured point on every derived series. A regression here silently
    breaks every verdict fixture in Tasks 4, 5, 7, 8 and 13."""
    doc = _trend_doc()
    assert doc["collection_scope"] == {"root": "/fake/root", "project_root": None,
                                       "compose": False}
    assert doc["metric_definitions"] == dict(_collector.METRIC_DEFINITIONS)
    assert set(doc["metric_quality"].values()) == {"complete"}
    model = rh.build_derived_trend_model([("2026-07-15", doc)])
    assert len(model["series"]) == len(rh.DERIVED_TREND_KEYS)
    for series in model["series"]:
        assert len(series["points"]) == 1, series["key"]
    # and every refusal is opt-in, never the default
    markerless = _trend_doc(scope_root=_NO_MARKERS, definitions=_NO_MARKERS)
    assert "collection_scope" not in markerless
    assert "metric_definitions" not in markerless
    dropped = _trend_doc(hooks=(0, 0), skills=(0, 0))
    dropped_model = rh.build_derived_trend_model([("2026-07-15", dropped)])
    assert [s["key"] for s in dropped_model["series"] if not s["points"]] == [
        "hooks_with_test_ratio", "skills_with_test_ratio"]


def test_trend_doc_derived_values_actually_vary_across_a_window():
    """Shape every derived-value fixture takes -- the values must actually vary, or the
    test proves nothing."""
    docs = []
    for i, n in enumerate((92, 96, 98)):
        docs.append((_DAILY_DATES[i], _trend_doc(memory_bodies=n)))
    model = rh.build_derived_trend_model(docs)
    bodies = next(s for s in model["series"] if s["key"] == "memory_body_count")
    assert bodies["points"] == [92, 96, 98]
    verdict = rh.trend_verdict(points=bodies["points"], dates=model["dates"], polarity="up")
    assert verdict.word == "worsening"
    assert "3 pts · 2d" in verdict.reason


# ============================== S6c Task 4: the three-axis comparability engine (§6.5a)
# EVERY assertion below calls a comparability function DIRECTLY. Nothing here renders --
# Task 7 owns the rendered half, exactly as the shipped S6b precedent
# (`test_marker_first_appearance_is_not_a_transition`,
# `test_no_value_shape_heuristic_is_possible_by_signature`) calls its functions with no
# render in the loop.
#
# ONE DEVIATION from the task brief, measured against live source rather than assumed.
# The brief's `test_markerless_but_audited_...` sketch asserts `"scope" in
# verdict.reason.lower()`. The SHIPPED `trend_verdict` (Task 3, commit 1ec0d2c) splits
# the two fields deliberately: `.reason` is always the `N pts · Md` companion pair, and
# the comparability detail rides in `.text`
# (`f"not comparable — {detail} · {reason}"`). `trend_verdict` is frozen and the branch
# holds zero deletions, so the axis assertions below target `.text` -- the field that
# actually carries the detail -- plus the `series_comparability` record itself. The
# INTENT (a refusal names WHICH axis refused, and a broken digest lookup cannot hide
# behind a same-word-different-cause row) is asserted in full.
_SCOPE_MAIN = {"root": "/fake/root", "project_root": None, "compose": False}
_SCOPE_COMPOSED = {"root": "/fake/root", "project_root": "/fake/project", "compose": True}
# A markerless historical sidecar carries NO `collection_scope` key, so the renderer's
# extraction yields None. A52 rejected a legacy scope table, so None stays UNKNOWN.
_SCOPE_ABSENT = None


def _comparable_window(metric, dates, versions=None, scopes=None, quality=None,
                       denominators=None):
    """`series_comparability` over a window that is comparable on all four axes unless
    the caller opts one out -- the same "refusals are explicit" contract `_trend_doc`
    established in Task 3. Returns the comparability record (`rh.COMPARABLE` or a
    factual reason)."""
    count = len(dates)
    return rh.series_comparability(
        metric, dates,
        [_SCOPE_MAIN] * count if scopes is None else scopes,
        [rh.QUALITY_COMPLETE] * count if quality is None else quality,
        [None] * count if denominators is None else denominators,
        [1] * count if versions is None else versions)


def test_scope_transition_compose_to_non_compose_is_not_comparable():
    """§6.5a axis 2. `compose` is a FIELD of the run's identity: the composed run walks
    a project tier the non-composed run never reads, so the two numbers are not the same
    measurement even when the root matches."""
    scopes = [_SCOPE_COMPOSED, _SCOPE_COMPOSED, _SCOPE_MAIN]
    assert rh.scope_comparable(scopes) is False
    # anti-vacuity: the same window WITHOUT the transition is comparable, so the refusal
    # above is caused by the transition and not by the fixture's shape
    assert rh.scope_comparable(scopes[:2]) is True
    reason = rh.build_scope_reason(list(zip(_dates_for(scopes), scopes)))
    assert "compose=true" in reason and "compose=false" in reason


def test_differing_project_root_under_one_root_is_not_comparable():
    """One operator root, two different project roots -- the composed corpus differs, so
    the points are not comparable. The second half pins the collector's own rule: a null
    `project_root` is a DISTINCT scope, never 'same as whatever ran last'."""
    two_projects = [{"root": "/fake/root", "project_root": "/fake/one", "compose": True},
                    {"root": "/fake/root", "project_root": "/fake/two", "compose": True}]
    assert rh.scope_comparable(two_projects) is False
    null_versus_set = [{"root": "/fake/root", "project_root": None, "compose": True},
                       {"root": "/fake/root", "project_root": "/fake/one", "compose": True}]
    assert rh.scope_comparable(null_versus_set) is False
    # anti-vacuity: identical project roots ARE comparable
    assert rh.scope_comparable([two_projects[0], dict(two_projects[0])]) is True


def test_markerless_scope_is_unknown_and_not_comparable():
    """UNKNOWN adjacent to ANYTHING -- including another UNKNOWN -- is not comparable.
    Do NOT default a missing scope to 'same as current'; that assumption is the whole
    finding (§6.5a). Same fail-toward-doubt direction as §8.1's markerless rule."""
    assert rh.scope_comparable([_SCOPE_ABSENT, _SCOPE_ABSENT]) is False
    assert rh.scope_comparable([_SCOPE_ABSENT, _SCOPE_MAIN]) is False
    assert rh.scope_comparable([_SCOPE_MAIN, _SCOPE_ABSENT]) is False
    assert rh.scope_comparable([_SCOPE_ABSENT]) is False
    # anti-vacuity: two readable, identical scopes ARE comparable
    assert rh.scope_comparable([_SCOPE_MAIN, dict(_SCOPE_MAIN)]) is True
    # and a markerless point states its own fact in the reason, never a blank
    reason = rh.build_scope_reason([("2026-07-13", _SCOPE_ABSENT),
                                    ("2026-07-14", _SCOPE_MAIN)])
    assert "2026-07-13: scope unknown" in reason


def test_malformed_collection_scope_is_unknown_never_raises():
    """Failure-modes row. A hand-crafted or corrupt sidecar can put anything here. A
    non-dict scope, or a dict whose fields are non-string, degrades to UNKNOWN -- the
    same degrade-don't-raise posture `series_confounded`'s T5.1 totality fix already
    established for unhashable version elements."""
    malformed = ([], "x", 0, None, {"root": 1, "project_root": [], "compose": "yes"},
                 {"root": "/fake/root"},                                   # fields missing
                 {"root": "/fake/root", "project_root": None, "compose": 1})  # 1 is not a bool
    for bad in malformed:
        assert rh.scope_comparable([bad, _SCOPE_MAIN]) is False, bad
        assert rh.scope_comparable([_SCOPE_MAIN, bad]) is False, bad
        # the reason builder degrades the same way rather than raising
        assert "scope unknown" in rh.build_scope_reason([("2026-07-13", bad),
                                                         ("2026-07-14", _SCOPE_MAIN)]), bad


def test_hygiene_tiers_makes_an_old_and_new_compose_scope_incomparable():
    """TRK-023 T5, R3-5/R3-8. The mechanism proof the whole resolution rests on: a scope
    carrying the new `hygiene_tiers` field differs from an otherwise-identical
    pre-TRK-023 scope in a field this module's own docstring already promised to notice
    ("a field this module does not yet name"), so `scope_comparable` refuses the pair
    without any new comparability logic -- and two scopes of the SAME new shape still
    compare equal, so the mechanism does not merely refuse everything."""
    old_composed = dict(_SCOPE_COMPOSED)
    new_composed = dict(_SCOPE_COMPOSED, hygiene_tiers=["operator", "project"])
    assert rh.scope_comparable([old_composed, new_composed]) is False
    assert rh.scope_comparable([new_composed, old_composed]) is False
    # anti-vacuity: two identically-shaped new scopes still compare
    assert rh.scope_comparable([new_composed, dict(new_composed)]) is True


def test_scope_readable_accepts_the_three_emitted_hygiene_tiers_values():
    # Changing this value requires a spec change (SPEC_6 §6.5a).
    for tiers in (["operator"], ["operator", "project"], ["operator", "project:partial"]):
        scope = dict(_SCOPE_COMPOSED, hygiene_tiers=tiers)
        assert rh._scope_readable(scope) is True, tiers


def test_scope_readable_rejects_malformed_hygiene_tiers():
    # `[]` is in the REJECT list here, matching R3-8's prose -- the earlier
    # []-is-hostile-but-also-valid contradiction is resolved in favour of the emitter.
    bad_values = ([], None, "operator", ["operator", 3], ["bogus"], ["project"],
                  ["project", "operator"], ["operator", "operator"])
    for bad in bad_values:
        scope = dict(_SCOPE_COMPOSED, hygiene_tiers=bad)
        assert rh._scope_readable(scope) is False, bad


def test_scope_readable_still_accepts_an_unknown_future_key():
    """R3-8's anti-whitelist pin: an unnamed key must not make an otherwise-valid scope
    UNKNOWN. `scope_comparable`'s "differing in ANY field includes a field this module
    does not yet name" depends on staying permissive about keys it has never heard of --
    validating `hygiene_tiers`'s VALUE and staying permissive about unknown KEYS are
    different things, and only the first is tightened."""
    scope = dict(_SCOPE_MAIN, future_key=1)
    assert rh._scope_readable(scope) is True


def test_absent_metric_quality_is_treated_as_unmeasured_never_complete():
    """Failure-modes row, and it fires on EVERY legacy sidecar -- A52 measured all 7
    live sidecars and none carries `metric_quality`. Absent is not a measurement:
    reading it as `complete` would let every pre-S6c point claim a quality it never
    reported. Direction suppressed, value shown."""
    legacy = _minimal_doc()
    assert "metric_quality" not in legacy          # the live legacy shape, asserted
    state = rh.metric_quality_state(legacy, "memory_body_count")
    assert state == rh.QUALITY_UNMEASURED
    assert state != rh.QUALITY_COMPLETE
    assert rh.quality_comparable([state, state, state]) is False
    # a non-dict `metric_quality`, and a metric absent from a present dict, take the
    # same path -- absent is absent however it got that way
    assert rh.metric_quality_state({"metric_quality": "x"}, "memory_body_count") == \
        rh.QUALITY_UNMEASURED
    assert rh.metric_quality_state({"metric_quality": {}}, "memory_body_count") == \
        rh.QUALITY_UNMEASURED
    # anti-vacuity: Task 1's collector output DOES report complete
    assert rh.metric_quality_state(_trend_doc(), "memory_body_count") == rh.QUALITY_COMPLETE
    # direction suppressed, VALUE SHOWN: the verdict refuses while the measured points
    # and their span still render
    points = [92, 96, 98]
    dates = _dates_for(points)
    comparability = _comparable_window("memory_body_count", dates, quality=[state] * 3)
    verdict = rh.trend_verdict(points=points, dates=dates, polarity="up",
                               comparability=comparability)
    assert verdict.word == "not comparable"
    assert "quality unmeasured" in verdict.text
    assert verdict.reason == "3 pts · 2d"


def test_partial_quality_suppresses_direction_but_keeps_the_value():
    """Suppression is DIRECTION-ONLY. The value survives and the reason is produced --
    'add doubt, never remove it'."""
    states = [rh.QUALITY_COMPLETE, "partial", rh.QUALITY_COMPLETE]
    assert rh.quality_comparable(states) is False
    assert rh.quality_comparable([rh.QUALITY_COMPLETE] * 3) is True   # anti-vacuity
    points = [7685, 7961, 7944]
    dates = _dates_for(points)
    comparability = _comparable_window("always_loaded_words", dates, quality=states)
    assert comparability != rh.COMPARABLE
    assert "quality partial" in comparability
    assert "2026-07-02" in comparability          # the reason names WHEN
    verdict = rh.trend_verdict(points=points, dates=dates, polarity="up",
                               comparability=comparability)
    assert verdict.word == "not comparable"
    # the value half survives: point count and span still stated, no direction word
    assert verdict.reason == "3 pts · 2d"
    for direction in ("improving", "worsening", "unchanged"):
        assert direction not in verdict.text, direction


def test_saturated_quality_suppresses_direction():
    """`saturated` is a STRUCTURAL ceiling (`duplicate_pair_count` pinned at MAX_PAIRS),
    not an accessibility caveat -- but it suppresses direction by the same rule, because
    a capped series that stops moving is not a series that stopped changing."""
    states = [rh.QUALITY_COMPLETE, "saturated", "saturated"]
    assert rh.quality_comparable(states) is False
    points = [40, 50, 50]
    dates = _dates_for(points)
    comparability = _comparable_window("duplicate_pair_count", dates, quality=states)
    assert "quality saturated" in comparability
    verdict = rh.trend_verdict(points=points, dates=dates, polarity="up",
                               comparability=comparability)
    assert verdict.word == "not comparable"
    # specifically NOT `unchanged across N` / `net unchanged`, the words a pinned-at-cap
    # series would otherwise earn
    assert "unchanged" not in verdict.text


def test_denominator_change_anywhere_in_the_window_forces_confounded():
    """The fixture is 21 -> 20 -> 21, NOT an endpoint-only change. A fixture that moves
    only first vs last passes under the retired first/last rule and therefore proves
    nothing about the all-pairs rule that replaced it.
    # Changing this rule requires a spec change (S6 §6.5 / finding #9 sub-fix 5)."""
    window = [{"value": 16, "total": 21, "ratio": 16 / 21},
              {"value": 16, "total": 20, "ratio": 16 / 20},
              {"value": 17, "total": 21, "ratio": 17 / 21}]
    denominators = [rh._trend_point_denominator(point) for point in window]
    assert denominators == [21, 20, 21]
    # the load-bearing property of the fixture: the ENDPOINTS agree, so a first-vs-last
    # check would return comparable and this test would prove nothing
    assert denominators[0] == denominators[-1]
    assert rh.denominators_comparable(denominators) is False
    # anti-vacuity: one stable denominator IS comparable, and a bare-number series
    # (no denominator at all) never refuses on this axis
    assert rh.denominators_comparable([21, 21, 21]) is True
    assert rh.denominators_comparable([None, None, None]) is True
    dates = _dates_for(window)
    comparability = _comparable_window("hooks_with_test_ratio", dates,
                                       denominators=denominators)
    reason = comparability
    assert "21" in reason and "20" in reason   # the reason names the OBSERVED set
    assert "denominators observed: 20, 21" in reason
    verdict = rh.trend_verdict(points=window, dates=dates, polarity="down",
                               comparability=comparability)
    assert verdict.word == "not comparable"


def test_definition_change_mid_series_is_flagged_and_yields_no_direction_word():
    """A verdict beside the flag IS the failure. `confounded` pre-empts the direction
    words rather than sitting next to one."""
    points = [92, 96, 98]
    dates = _dates_for(points)
    versions = [1, 2, 2]
    assert rh.series_confounded(versions) is True
    comparability = _comparable_window("memory_body_count", dates, versions=versions)
    assert "definition v1" in comparability and "definition v2" in comparability
    verdict = rh.trend_verdict(points=points, dates=dates, polarity="up",
                               latest_direction="bad", comparability=comparability)
    assert verdict.word == "not comparable"
    for direction in ("improving", "worsening", "unchanged", "no direction"):
        assert direction not in verdict.text, direction
    # anti-vacuity: the SAME points under one stable version do earn a direction word
    stable = _comparable_window("memory_body_count", dates, versions=[1, 1, 1])
    assert stable == rh.COMPARABLE
    assert rh.trend_verdict(points=points, dates=dates, polarity="up",
                            comparability=stable).word == "worsening"


def test_markerless_but_audited_sidecar_resolves_its_version_but_scope_still_refuses():
    """SUPERSEDES design §9.5 item 2, which said "verdict still given". That was written
    when the definition axis was the ONLY axis; A52 added scope, and a sidecar markerless
    for `metric_definitions` is markerless for `collection_scope` too -- both fields are
    absent from the same historical artifacts. So the scope axis refuses this point
    INDEPENDENTLY, and the original claim asserts an outcome that cannot occur.

    What survives, and is still worth testing, is the ORIGINAL PURPOSE: the digest lookup
    resolves the version, so the DEFINITION axis does not falsely flag it.

    The third assertion is the load-bearing one. Both axes produce `not comparable`, so
    without it a regression that BROKE the digest lookup would be invisible here -- the
    row would still read `not comparable`, for the other reason.

    Pairs with the shipped `test_marker_first_appearance_is_not_a_transition`, which
    proves the same digest-resolution property for the definition axis alone."""
    legacy_raws = [b'{"n": 1}', b'{"n": 2}', b'{"n": 3}']
    legacy = {hashlib.sha256(raw).hexdigest(): {"memory_body_count": 1}
              for raw in legacy_raws}
    audited_window = [({}, raw) for raw in legacy_raws]
    versions = [rh.resolve_metric_definition_version(doc, raw, "memory_body_count", legacy)
                for doc, raw in audited_window]
    assert None not in versions                      # the digest path RESOLVED
    assert rh.series_confounded(versions) is False   # definition axis does NOT flag
    # the SAME markerless artifacts carry no collection_scope either
    scopes = [doc.get("collection_scope") for doc, _ in audited_window]
    assert scopes == [_SCOPE_ABSENT] * 3
    points = [92, 96, 98]
    dates = _dates_for(points)
    comparability = _comparable_window("memory_body_count", dates, versions=versions,
                                       scopes=scopes)
    verdict = rh.trend_verdict(points=points, dates=dates, polarity="up",
                               comparability=comparability)
    assert verdict.word == "not comparable"          # ...but scope refuses anyway
    detail = verdict.text.lower()
    assert "scope" in detail                         # and the reason names WHICH axis
    assert "definition" not in detail
    assert "scope" in comparability.lower() and "definition" not in comparability.lower()


def test_markerless_and_unaudited_sidecar_is_not_comparable():
    """A digest matching none of the frozen entries resolves UNKNOWN. An unrecognised
    markerless artifact must never receive an INFERRED version. THIS IS THE LIVE CORPUS
    SHAPE (A52): the 08-01 and 08-02 sidecars match no legacy digest and carry no
    marker, so this is the path every real render takes today."""
    legacy = {hashlib.sha256(b'{"n": 1}').hexdigest(): {"memory_body_count": 1}}
    audited = rh.resolve_metric_definition_version({}, b'{"n": 1}', "memory_body_count",
                                                    legacy)
    unaudited = rh.resolve_metric_definition_version({}, b'{"n": 99}', "memory_body_count",
                                                      legacy)
    assert audited == 1                # anti-vacuity: the table itself works
    assert unaudited is None           # ...and an unrecognised artifact stays UNKNOWN
    assert rh.series_confounded([1, 1, unaudited]) is True
    points = [92, 96, 98]
    dates = _dates_for(points)
    # scope carried (so the definition axis is the one under test), version unknown
    comparability = _comparable_window("memory_body_count", dates,
                                       versions=[1, 1, unaudited])
    assert comparability != rh.COMPARABLE
    assert "definition unknown" in comparability
    assert rh.trend_verdict(points=points, dates=dates, polarity="up",
                            comparability=comparability).word == "not comparable"


def test_unaudited_markerless_window_yields_not_comparable_WITH_ITS_REASON():
    """A52, and the blackout is CONTRACT now, not an accident. A window holding an
    unaudited markerless sidecar must produce `not comparable` AND a factual reason
    naming the dates and the axis. A refusal with no reason reads as a broken feature,
    which is how a correct refusal gets 'fixed' by weakening it a milestone later.
    Task 8 asserts the reason RENDERS; this pins that one is produced."""
    # the live corpus shape: no `metric_definitions`, no `collection_scope`, no
    # `metric_quality`, and a digest matching no frozen entry
    markerless = _trend_doc(scope_root=_NO_MARKERS, definitions=_NO_MARKERS)
    markerless.pop("metric_quality")
    dates = ["2026-08-01", "2026-08-02", "2026-08-03"]
    scopes = [markerless.get("collection_scope")] * 3
    versions = [rh.resolve_metric_definition_version(
        markerless, json.dumps(markerless).encode(), "memory_body_count",
        rh.LEGACY_METRIC_DEFINITIONS)] * 3
    quality = [rh.metric_quality_state(markerless, "memory_body_count")] * 3
    assert scopes == [None] * 3 and versions == [None] * 3
    assert quality == [rh.QUALITY_UNMEASURED] * 3
    comparability = rh.series_comparability("memory_body_count", dates, scopes, quality,
                                            [None] * 3, versions)
    assert comparability != rh.COMPARABLE
    assert comparability.strip() != ""
    for date in dates:
        assert date in comparability, date            # the reason names the DATES
    assert "scope" in comparability.lower()           # ...and the AXIS
    verdict = rh.trend_verdict(points=[92, 96, 98], dates=dates, polarity="up",
                               comparability=comparability)
    assert verdict.word == "not comparable"
    assert comparability in verdict.text              # available to render (Task 8)


def test_scope_axis_has_NO_first_appearance_exemption():
    """The SECOND instance of the CODEX-1 contradiction class, found by this plan's own
    sweep. An earlier draft of this test extended the shipped
    `test_marker_first_appearance_is_not_a_transition`'s `absent -> N is not a
    transition` rule from the definition axis to the scope axis. THAT RULE CANNOT HOLD
    FOR SCOPE, and the difference is A52's doing:

      - DEFINITION axis: a markerless sidecar resolves through the frozen legacy DIGEST
        table to a real version, so `series_confounded` never sees None and the
        introduction step genuinely is not a transition.
      - SCOPE axis: A52 REJECTED a legacy scope table (it would have to invent
        `project_root`), so there is NOTHING to resolve a markerless scope through. It
        is UNKNOWN, and UNKNOWN adjacent to anything is `not comparable` (§6.5a).

    So an `absent -> present` scope step IS a refusal, and asserting otherwise would
    have required defaulting a missing scope to "same as current" -- the single
    assumption §6.5a exists to forbid.

    THIS IS WHY THE BLACKOUT EXISTS, and why it lifts only when two ADJACENT points both
    carry scope, never on the first run that ships the field."""
    assert rh.scope_comparable([_SCOPE_ABSENT, {"root": "/r", "project_root": None,
                                                "compose": False}]) is False
    # the definition axis' exemption is UNTOUCHED by this rule -- the two axes differ
    assert rh.series_confounded([1, 1]) is False
    # and the blackout lifts on TWO ADJACENT carriers, with no code change
    carried = {"root": "/r", "project_root": None, "compose": False}
    assert rh.scope_comparable([carried, dict(carried)]) is True
    assert rh.scope_comparable([_SCOPE_ABSENT, carried, dict(carried)]) is False


def test_real_drift_under_stable_markers_is_worsening_never_confounded():
    """THE NEGATIVE TEST -- do not drop it for time. memory_body_count 92/96/98/117
    under versions 1,1,1,1 AND one identical scope must read `worsening`. If a
    jump-magnitude heuristic ever creeps in, it eats this series: the ONE real finding
    in the operator's data becomes a false negative (§8.4).

    Driven through the real builder, not a hand-typed point list, so a regression in
    `build_derived_trend_model` cannot hide behind a synthetic window."""
    counts = (92, 96, 98, 117)
    dated_docs = [(_DAILY_DATES[i], _trend_doc(memory_bodies=n))
                  for i, n in enumerate(counts)]
    model = rh.build_derived_trend_model(dated_docs)
    bodies = next(s for s in model["series"] if s["key"] == "memory_body_count")
    assert bodies["points"] == list(counts)
    assert counts[-1] - counts[-2] > counts[1] - counts[0]   # the big jump is present
    scopes = [doc["collection_scope"] for _, doc in dated_docs]
    quality = [rh.metric_quality_state(doc, "memory_body_count") for _, doc in dated_docs]
    versions = [doc["metric_definitions"]["memory_body_count"] for _, doc in dated_docs]
    assert rh.series_confounded(versions) is False
    comparability = rh.series_comparability("memory_body_count", model["dates"], scopes,
                                            quality, [None] * 4, versions)
    assert comparability == rh.COMPARABLE
    verdict = rh.trend_verdict(points=bodies["points"], dates=model["dates"],
                               polarity="up", comparability=comparability)
    assert verdict.word == "worsening"
    assert "4 pts" in verdict.reason


def test_no_value_shape_heuristic_is_possible_on_the_new_axes_by_signature():
    """EXTENDS the shipped test_no_value_shape_heuristic_is_possible_by_signature to the
    scope, quality and denominator functions. Each takes scopes/quality states/
    denominators ONLY and cannot see metric values, so the heuristic §8.4 forbids is
    unwritable without changing a signature this test pins.

    Changing this contract requires a spec change (S6 §8.4)."""
    import inspect
    for fn, expected in ((rh.scope_comparable, ["scopes"]),
                         (rh.quality_comparable, ["states"]),
                         (rh.denominators_comparable, ["denominators"])):
        assert list(inspect.signature(fn).parameters) == expected, fn.__name__
    assert "points" not in inspect.signature(rh.scope_comparable).parameters
    assert "values" not in inspect.signature(rh.scope_comparable).parameters
    # the composer is value-blind too, which is what makes the axis functions' blindness
    # more than a formality -- nothing upstream of them holds a value to leak downward
    composer = list(inspect.signature(rh.series_comparability).parameters)
    assert composer == ["metric", "dates", "scopes", "quality_states", "denominators",
                        "definition_versions"]
    for forbidden in ("points", "values", "doc", "model"):
        assert forbidden not in composer, forbidden
    # anti-vacuity: one identical scope stays comparable no matter how far the implied
    # values moved, because the function never saw them
    assert rh.scope_comparable([_SCOPE_MAIN, dict(_SCOPE_MAIN), dict(_SCOPE_MAIN)]) is True


@pytest.mark.parametrize("bad", [True, 0, -1, "1", 1.0, None])
def test_invalid_definition_version_resolves_unknown(bad):
    """`True == 1` in Python -- a stray boolean would silently read as version 1 and
    report a series comparable when it is not. That is the whole point.

    The shipped `test_definition_version_rejects_bool_true_which_equals_one_in_python`
    pins the RESOLVER; this extends the property through the comparability engine, so a
    rejected version is proved to actually refuse the window rather than merely resolve
    to None somewhere upstream."""
    doc = {"metric_definitions": {"memory_body_count": bad}}
    version = rh.resolve_metric_definition_version(doc, b"unaudited", "memory_body_count", {})
    assert version is None, bad
    dates = _dates_for([0, 0, 0])
    comparability = _comparable_window("memory_body_count", dates,
                                       versions=[1, 1, version])
    assert comparability != rh.COMPARABLE, bad
    assert "definition unknown" in comparability, bad
    assert rh.trend_verdict(points=[92, 96, 98], dates=dates, polarity="up",
                            comparability=comparability).word == "not comparable", bad


def test_every_comparability_reason_is_factual_only_and_carries_no_verdict_word():
    """Binding rule 6, applied to the three reasons this task adds. The renderer states
    FACTS -- dates, scope fields, quality states, denominators -- and the judgment stays
    the model's. `_CONFOUND_REASON_FORBIDDEN` is the shared ban the shipped
    `build_confounded_reason` test already applies to the definition axis."""
    reasons = [
        rh.build_scope_reason([("2026-07-13", _SCOPE_ABSENT), ("2026-07-14", _SCOPE_MAIN)]),
        rh.build_quality_reason("memory_body_count",
                                [("2026-07-13", "partial"), ("2026-07-14", "complete")]),
        rh.build_denominator_reason("hooks_with_test_ratio",
                                    [("2026-07-13", 21), ("2026-07-14", 20)]),
    ]
    for reason in reasons:
        assert reason.strip() != ""
        low = reason.lower()
        for word in rh._CONFOUND_REASON_FORBIDDEN:
            assert word not in low, (word, reason)


def test_comparability_reasons_are_deterministic_regardless_of_input_order():
    """Binding rule 9: fixed orderings, no bare `set()` iteration into output. Each
    reason is sorted by its own display text, so two windows holding the same dated
    readings in different orders produce byte-identical strings."""
    scope_rows = [("2026-07-14", _SCOPE_MAIN), ("2026-07-13", _SCOPE_ABSENT)]
    assert rh.build_scope_reason(scope_rows) == rh.build_scope_reason(scope_rows[::-1])
    quality_rows = [("2026-07-14", "complete"), ("2026-07-13", "partial")]
    assert rh.build_quality_reason("m", quality_rows) == \
        rh.build_quality_reason("m", quality_rows[::-1])
    denominator_rows = [("2026-07-14", 20), ("2026-07-13", 21)]
    assert rh.build_denominator_reason("m", denominator_rows) == \
        rh.build_denominator_reason("m", denominator_rows[::-1])
    # and the observed set itself is order-independent
    assert rh._observed_denominators([21, 20, 21]) == rh._observed_denominators([20, 21])


# ==================== S6c Task 5 (§6.8): inputs_digest recompute + basis suppression
# EVERY assertion below calls `trend_inputs_digest`/`trend_basis_for` DIRECTLY. Nothing
# here renders -- Task 7 owns the rendered `basis stale for this series` assertion, the
# same function-level/rendered split Tasks 3 and 4 already took.

_BASIS_DATES = _dates_for([92, 96, 98], start=13)
_BASIS_POINTS = list(zip(_BASIS_DATES, [92, 96, 98], [None, None, None]))
_BASIS_VERSIONS = [1, 1, 1]
_BASIS_SCOPES = [_SCOPE_MAIN, _SCOPE_MAIN, _SCOPE_MAIN]
_BASIS_WINDOW_LENGTH = 3


def test_digest_matches_yield_the_stored_prose():
    """S6 §6.8: a row whose stored digest matches the recomputed one is CURRENT -- its
    prose renders and no stale note accompanies it."""
    digest = rh.trend_inputs_digest("memory_body_count", _BASIS_POINTS, _BASIS_VERSIONS,
                                    _BASIS_SCOPES, _BASIS_WINDOW_LENGTH)
    trend_basis = [{"metric": "memory_body_count",
                    "prose": "steadily growing across the window",
                    "inputs_digest": digest}]
    prose, stale = rh.trend_basis_for(trend_basis, "memory_body_count", _BASIS_POINTS,
                                      _BASIS_VERSIONS, _BASIS_SCOPES, _BASIS_WINDOW_LENGTH)
    assert prose == "steadily growing across the window"
    assert stale is None


def test_mutating_one_covered_input_invalidates_the_digest():
    """THE mechanism. Build a trend_basis row, mutate ONE covered input, recompute. The
    happy path alone does not discharge this finding -- the whole feature exists for the
    mismatch branch (S6 §6.8 finding #8). Do this once per covered input: a point value,
    a denominator, a definition version, a scope, the window length."""
    digest = rh.trend_inputs_digest("memory_body_count", _BASIS_POINTS, _BASIS_VERSIONS,
                                    _BASIS_SCOPES, _BASIS_WINDOW_LENGTH)
    trend_basis = [{"metric": "memory_body_count", "prose": "cached basis",
                    "inputs_digest": digest}]
    # sanity: the unmutated window resolves current, so every mismatch below is caused by
    # the mutation and not by some other defect in the fixture
    assert rh.trend_basis_for(trend_basis, "memory_body_count", _BASIS_POINTS,
                              _BASIS_VERSIONS, _BASIS_SCOPES,
                              _BASIS_WINDOW_LENGTH) == ("cached basis", None)

    mutated_value = [("2026-07-13", 999, None)] + _BASIS_POINTS[1:]
    mutated_denominator = [("2026-07-13", 92, 20)] + _BASIS_POINTS[1:]
    mutated_versions = [2] + _BASIS_VERSIONS[1:]
    other_scope = {"root": "/other/root", "project_root": None, "compose": False}
    mutated_scopes = [other_scope] + _BASIS_SCOPES[1:]
    cases = (
        (mutated_value, _BASIS_VERSIONS, _BASIS_SCOPES, _BASIS_WINDOW_LENGTH),
        (mutated_denominator, _BASIS_VERSIONS, _BASIS_SCOPES, _BASIS_WINDOW_LENGTH),
        (_BASIS_POINTS, mutated_versions, _BASIS_SCOPES, _BASIS_WINDOW_LENGTH),
        (_BASIS_POINTS, _BASIS_VERSIONS, mutated_scopes, _BASIS_WINDOW_LENGTH),
        (_BASIS_POINTS, _BASIS_VERSIONS, _BASIS_SCOPES, _BASIS_WINDOW_LENGTH + 1),
    )
    for points, versions, scopes, window_length in cases:
        prose, stale = rh.trend_basis_for(trend_basis, "memory_body_count", points,
                                          versions, scopes, window_length)
        assert prose is None, (points, versions, scopes, window_length)
        assert stale == "basis stale for this series", (points, versions, scopes,
                                                         window_length)


def test_absent_or_malformed_digest_resolves_to_stale_with_no_fallback():
    """No fallback, ever. A renderer-authored default is a judgment wearing a default's
    clothing (§6.8 item 7). Empty string, None, a non-string, and a wrong-length hex
    string all resolve STALE."""
    real_digest = rh.trend_inputs_digest("memory_body_count", _BASIS_POINTS,
                                         _BASIS_VERSIONS, _BASIS_SCOPES,
                                         _BASIS_WINDOW_LENGTH)
    for bad_digest in ("", None, 12345, real_digest[:8], real_digest + "0000"):
        trend_basis = [{"metric": "memory_body_count", "prose": "should never render",
                        "inputs_digest": bad_digest}]
        prose, stale = rh.trend_basis_for(trend_basis, "memory_body_count", _BASIS_POINTS,
                                          _BASIS_VERSIONS, _BASIS_SCOPES,
                                          _BASIS_WINDOW_LENGTH)
        assert prose is None, bad_digest
        assert stale == "basis stale for this series", bad_digest


def test_absent_trend_basis_yields_no_prose_AND_NO_STALE_NOTE():
    """Failure-modes row, and it is a DIFFERENT branch from a malformed digest -- which is
    why the malformed-digest test does not cover it. A missing or non-list `trend_basis`
    means the model never wrote a row at all; there was nothing to go stale, so emitting
    'basis stale for this series' would invent a history. No prose, NO note, no crash."""
    for absent in (None, {}, "x", 0, []):
        assert rh.trend_basis_for(absent, "memory_body_count", _BASIS_POINTS,
                                  _BASIS_VERSIONS, _BASIS_SCOPES,
                                  _BASIS_WINDOW_LENGTH) == (None, None)
    # a well-formed list holding no row for THIS metric is the same "never wrote one" case
    other_row = [{"metric": "always_loaded_words", "prose": "x", "inputs_digest": "y"}]
    assert rh.trend_basis_for(other_row, "memory_body_count", _BASIS_POINTS,
                              _BASIS_VERSIONS, _BASIS_SCOPES,
                              _BASIS_WINDOW_LENGTH) == (None, None)


def test_digest_is_canonical_and_stable_across_pythonhashseed():
    """Both producers must agree byte-for-byte or the prose never renders at all -- a
    silent, TOTAL feature loss that looks like 'the model didn't write basis prose'.
    Exercised as a REAL subprocess under two differing seeds: asserting the digest equals
    itself inside one interpreter proves nothing, since the hazard is CROSS-PROCESS
    instability, which only a subprocess under differing `PYTHONHASHSEED` values can
    exercise."""
    code = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('rh', sys.argv[1])\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "points = [('2026-07-13', 92, None), ('2026-07-14', 96, None),\n"
        "          ('2026-07-15', 98, 20)]\n"
        "scopes = [{'root': '/fake/root', 'project_root': None, 'compose': False}] * 3\n"
        "digest = m.trend_inputs_digest('memory_body_count', points, [1, 1, 1], scopes, 3)\n"
        "sys.stdout.write(digest)\n"
    )
    proc1 = subprocess.run([sys.executable, "-c", code, str(RENDER)],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "PYTHONHASHSEED": "0"})
    assert proc1.returncode == 0, proc1.stderr
    proc2 = subprocess.run([sys.executable, "-c", code, str(RENDER)],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "PYTHONHASHSEED": "12345"})
    assert proc2.returncode == 0, proc2.stderr
    assert proc1.stdout == proc2.stdout
    assert len(proc1.stdout) == 16


# ------------------------------------------------------- S6c Task 6: template single-source
def _parse_marker_block(path, marker):
    """Extract the text between `<!-- {marker} -->` and `<!-- /{marker} -->` in `path`, or
    "" if the markers are missing or the file cannot be parsed -- an empty return keeps the
    non-vacuity assert in the test below honest: deleting the markers must FAIL the test,
    never silently pass it."""
    text = path.read_text(encoding="utf-8")
    m = re.search(rf"<!--\s*{re.escape(marker)}\s*-->(.*?)<!--\s*/{re.escape(marker)}\s*-->",
                  text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _parse_verdict_rows(block):
    """Parse a markdown pipe table's DATA rows out of `block`: strip each cell of
    surrounding whitespace and the leading/trailing pipes, skip the header row and the
    `|---|---|---|` separator row, and ignore blank lines. Case-sensitive on purpose -- the
    rendered verdict words and the template's words must match byte-for-byte."""
    rows = []
    for line in block.splitlines():
        line = line.strip()
        if not line or not line.startswith("|"):
            continue
        cells = tuple(c.strip() for c in line.strip("|").split("|"))
        if all(re.fullmatch(r"-+", c) for c in cells):
            continue  # the `|---|---|---|` separator row
        if cells and cells[0] == "Verdict":
            continue  # header row
        rows.append(cells)
    return rows


def _verdict_rows(verdicts):
    """Project `TREND_VERDICTS`' (word, gate, polarity) rows into the same three-column
    row shape `_parse_verdict_rows` yields from the markdown table, so the two can be
    compared for exact equality -- same words, same gate text, same polarity, same order."""
    return [tuple(row) for row in verdicts]


def test_trend_verdict_table_is_single_sourced_against_the_template():
    """S6 §6.2-R item 4. A COMMENT is governance, and governance is exactly what
    produced the A3 wart -- CHECK_BANDS and GAUGE_BANDS each had a human-readable
    justification too, and they still drifted, because nothing failed when they did.
    Declaring 'one set of constants' while shipping two literals in two files IS the
    two-homes divergence restated. This test is the enforcing artifact: editing either
    home without the other turns the suite red.

    The parse lives HERE, not in render_html.py -- the renderer keeps its plain literal
    (stdlib-only runtime, offline render unaffected, no file read at import time)."""
    block = _parse_marker_block(TEMPLATE_PATH, "TREND_VERDICT_TABLE")
    # A green test over a MISSING block is the false-green class this project has
    # already been burned by. Assert the block exists and is non-empty FIRST, so
    # deleting the markers cannot make the check vacuous.
    assert block, "TREND_VERDICT_TABLE marker block missing or empty"
    assert _parse_verdict_rows(block) == _verdict_rows(rh.TREND_VERDICTS)
    # same words, same gate text, same polarity, same ORDER -- case-sensitive.
    # (No "thresholds" here: that column belongs to the BANDS precedent, not to this
    # table, whose three columns are Verdict | Gate | Polarity.)


# ========================= S6c Task 7: the RENDERED verdict surface (§6.8, Ambiguities A/B)
# EVERY assertion below reads a real rendered document. Tasks 3-5 asserted at the FUNCTION
# level because nothing they built reached HTML -- the S6b comparability machinery they
# consume shipped unwired, called from nowhere. This section is the other half of that
# split: the wiring, and the properties only a rendered page can prove.
#
# N = len(HEADLINE_KEYS) + len(DERIVED_TREND_KEYS), DERIVED and never typed. The legacy
# table keeps iterating all 8 headline series INCLUDING unchecked_binary_count (three
# shipped sparkline-count assertions require it); §6.8's exclusion of that metric governs
# the DERIVED table, which never had it.
_RENDERED_TREND_ROWS = len(rh.HEADLINE_KEYS) + len(rh.DERIVED_TREND_KEYS)

# Anchored on the VERDICT CELL, not on a row class: the trend `<tr>` stays bare because
# `test_trend_table_renders_a_missing_point_as_not_measured` matches `<tr><td>Duplicate
# pairs</td>` on this table, and binding rule 7 forbids editing that assertion. The
# verdict cell is what makes a row a trend row anyway.
_TREND_ROW_RE = re.compile(
    r'<tr><td>(?:(?!</tr>).)*?<td class="trend-verdict verdict-[a-z0-9-]+"'
    r'(?:(?!</tr>).)*?</tr>', re.S)
_VERDICT_CELL_RE = re.compile(
    r'<td class="trend-verdict verdict-[a-z0-9-]+" data-verdict="([^"]*)">'
    r'((?:(?!</td>).)*)</td>', re.S)


def _dated_trend_docs(per_date, start=13):
    """`[(date, doc)]` from one `_trend_doc(**kwargs)` mapping per date, one calendar day
    apart. Each doc is a fresh dict the caller may mutate to opt into a refusal."""
    return [(f"2026-07-{start + i:02d}", _trend_doc(**kwargs))
            for i, kwargs in enumerate(per_date)]


def _render_corpus(tmp_path, name, dated_docs, synthesis=None):
    """Write a real dated sidecar corpus -- optionally with a synthesis sidecar -- and
    render it through the CLI, returning the HTML. Real files, real subprocess, the same
    fixture style every other render test in this module uses."""
    out_dir = tmp_path / name
    out_dir.mkdir()
    for sidecar_date, doc in dated_docs:
        _write_sidecar(out_dir, sidecar_date, doc)
    selected = dated_docs[-1][0]
    if synthesis is not None:
        (out_dir / f"harness-synthesis-{selected}.json").write_text(json.dumps(synthesis))
    proc = run_render(out_dir, "--date", selected, "--no-friction")
    assert proc.returncode == 0, proc.stderr
    return (out_dir / f"harness-map-{selected}.html").read_text(encoding="utf-8")


def _trend_rows(text):
    """Every rendered trend row across BOTH tables, as raw HTML.

    NON-VACUITY IS THE CALLER'S JOB, and every caller below discharges it: a
    `for row in _trend_rows(text)` loop passes with ZERO iterations the moment this regex
    stops matching the markup -- the same false-green Task 6 guards against with its
    `assert block, "...marker block missing or empty"`. Assert the length against
    `_RENDERED_TREND_ROWS` before iterating."""
    return _TREND_ROW_RE.findall(text)


def _trend_row(text, label):
    """The one trend row whose metric cell is exactly `label`. Asserting there is EXACTLY
    one also pins that no metric renders twice -- two rows for one metric would mean two
    verdicts for one series."""
    rows = [row for row in _trend_rows(text) if f"<td>{label}</td>" in row]
    assert len(rows) == 1, (label, len(rows))
    return rows[0]


def _verdict_of(row_html):
    """`(enum word, verdict-cell inner HTML)` for one trend row."""
    match = _VERDICT_CELL_RE.search(row_html)
    assert match is not None, row_html
    return match.group(1), match.group(2)


def _moving_corpus():
    """Three comparable dated docs carrying three measured points on every one of the 14
    rendered series -- the baseline every presentation test below renders."""
    return _dated_trend_docs([
        dict(memory_bodies=5, promotion=1, phantom=(2, 2), hooks=(12, 20), skills=(4, 10)),
        dict(memory_bodies=9, promotion=2, phantom=(2, 2), hooks=(14, 20), skills=(5, 10)),
        dict(memory_bodies=5, promotion=3, phantom=(2, 2), hooks=(16, 20), skills=(6, 10)),
    ])


# --------------------------------------------------------- Ambiguity A/B: the two surfaces
def test_sparkline_svg_css_class_and_title_default_to_the_legacy_bytes():
    """Byte-identity guard (Ambiguity B). `test_sparkline_flat_series_renders_at_the_
    bottom_not_mid_height` calls `_sparkline_svg("spark-x", [5.0, 5.0, 5.0])` POSITIONALLY
    with exactly two arguments, so both new parameters must be keyword-only -- and their
    defaults must reproduce the pre-change bytes exactly, because three shipped assertions
    COUNT `class="sparkline"` occurrences and a fourth asserts its ABSENCE."""
    import inspect
    assert rh._sparkline_svg("spark-x", [5.0, 5.0, 5.0]) == (
        '<svg class="sparkline" id="spark-x" viewBox="0 0 120.00 24.00" width="120" '
        'height="24" role="img" aria-labelledby="spark-x-title">'
        '<title id="spark-x-title">trend sparkline</title>'
        '<polyline points="0.00,22.00 60.00,22.00 120.00,22.00" fill="none" '
        'stroke="currentColor" stroke-width="1.5"/></svg>')
    params = inspect.signature(rh._sparkline_svg).parameters
    assert params["css_class"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["css_class"].default == "sparkline"
    assert params["title"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["title"].default is None


def test_legacy_headline_sparkline_cell_is_byte_unchanged():
    """Ambiguity A from the other side. The LEGACY per-date table keeps its min/max/cur
    span, byte for byte: a shipped regex pins `</svg><span class="sparkline-stats">`
    adjacency AND the exact stats text, so `_sparkline_cell` is frozen and both new
    surfaces get their own renderer instead of an extension of this one."""
    series = {"key": "always_loaded_words", "label": "Always-loaded words",
              "polarity": "up", "values": [150, 160, 161], "points": [150, 160, 161],
              "point_dates": ["2026-07-13", "2026-07-14", "2026-07-15"]}
    assert rh._sparkline_cell(series) == (
        '<svg class="sparkline" id="spark-always_loaded_words" '
        'viewBox="0 0 120.00 24.00" width="120" height="24" role="img" '
        'aria-labelledby="spark-always_loaded_words-title">'
        '<title id="spark-always_loaded_words-title">trend sparkline</title>'
        '<polyline points="0.00,22.00 60.00,3.82 120.00,2.00" fill="none" '
        'stroke="currentColor" stroke-width="1.5"/></svg>'
        '<span class="sparkline-stats">min 150.00 · max 161.00 · cur 161.00</span>')


def test_derived_sparklines_carry_their_own_class_and_namespaced_dom_id(tmp_path):
    """Pinned from BOTH sides (Ambiguity B): the derived class is present where expected
    AND the bare `class="sparkline"` count is unchanged. The shipped count assertions are
    substring-fragile -- `class="sparkline"` WITH its closing quote does not occur inside
    `class="sparkline sparkline-derived"`. The DOM id is namespaced too, or
    `id="spark-memory_body_count"` would collide and break `aria-labelledby`."""
    text = _render_corpus(tmp_path, "derivedspark", _moving_corpus())
    assert text.count('class="sparkline"') == len(rh.HEADLINE_KEYS)
    assert text.count('class="sparkline sparkline-derived"') == len(rh.DERIVED_TREND_KEYS)
    assert 'id="spark-derived-memory_body_count"' in text
    assert 'id="spark-memory_body_count"' not in text
    # ...and the legacy namespace is untouched in the same document
    assert 'id="spark-always_loaded_words"' in text
    assert 'id="spark-derived-always_loaded_words"' not in text


def test_derived_sparklines_respect_the_point_floor_like_the_legacy_ones(tmp_path):
    """The FOURTH shipped assertion in the sparkline-count family is a NEGATIVE one
    (`test_sparkline_absent_below_three_sidecars`), and it survives only because every new
    surface respects `SPARKLINE_MIN_POINTS`. Two sidecars: NEITHER table draws a mark."""
    text = _render_corpus(tmp_path, "derivedfloor", _dated_trend_docs(
        [dict(memory_bodies=5), dict(memory_bodies=9)]))
    assert 'class="sparkline"' not in text
    assert 'class="sparkline sparkline-derived"' not in text
    # the tables themselves still render, verdicts and all
    assert len(_trend_rows(text)) == _RENDERED_TREND_ROWS


def test_derived_cell_omits_the_min_max_cur_span_the_legacy_cell_keeps(tmp_path):
    """§6.8 item 2 on the PROMOTED surface: the span duplicates the adjacent date columns,
    is wider than the mark it annotates, and prints `.00` on integer counts. Ambiguity A:
    the LEGACY headline table keeps its span, which a shipped assertion pins."""
    text = _render_corpus(tmp_path, "derivedspan", _moving_corpus())
    derived = _trend_row(text, "Memory bodies")
    legacy = _trend_row(text, "Always-loaded words")
    assert 'class="sparkline sparkline-derived"' in derived
    assert 'class="sparkline-stats"' not in derived
    assert 'class="sparkline-stats"' in legacy


def test_svg_title_carries_the_metric_the_verdict_and_the_basis(tmp_path):
    """`role="img"` + `aria-labelledby` means the `<title>` is the ENTIRE accessible
    content of the graphic, and today's default says "trend sparkline" -- which tells a
    screen-reader user nothing the sighted reader of the same row does not get (§6.8
    item 5). The digest is recomputed HERE from the same covered inputs, so a renderer that
    fingerprinted a different window would suppress the prose and fail this."""
    counts = (92, 96, 98)
    docs = _dated_trend_docs([dict(memory_bodies=n) for n in counts])
    digest = rh.trend_inputs_digest(
        "memory_body_count",
        [[date, float(n), None] for (date, _), n in zip(docs, counts)],
        [_collector.METRIC_DEFINITIONS["memory_body_count"]] * len(counts),
        [doc["collection_scope"] for _, doc in docs],
        rh.SPARKLINE_WINDOW)
    synthesis = {"schema_version": 1, "trend_basis": [
        {"metric": "memory_body_count", "prose": "six more bodies since the 13th",
         "inputs_digest": digest}]}
    text = _render_corpus(tmp_path, "sparktitle", docs, synthesis=synthesis)
    row = _trend_row(text, "Memory bodies")
    title = re.search(
        r'<title id="spark-derived-memory_body_count-title">([^<]*)</title>', row)
    assert title is not None, row
    assert "Memory bodies" in title.group(1)
    assert "worsening" in title.group(1)
    assert "six more bodies since the 13th" in title.group(1)
    # the same prose renders VISIBLY too, with no stale note beside it
    assert 'class="trend-basis">six more bodies since the 13th</span>' in row
    assert rh.TREND_BASIS_STALE_NOTE not in row


# ------------------------------------------------- the verdict vocabulary, on the page
def test_rendered_verdict_words_are_drawn_only_from_the_enum(tmp_path):
    """Asserted over the FULL rendered document, not one row: `data-verdict` carries the
    enum word alone (the display text interpolates N and may attach a second horizon
    clause, so the word is not recoverable from it by parsing)."""
    text = _render_corpus(tmp_path, "enumonly", _moving_corpus())
    words = re.findall(r'data-verdict="([^"]*)"', text)
    assert len(words) == _RENDERED_TREND_ROWS, words
    allowed = {row[0] for row in rh.TREND_VERDICTS}
    assert set(words) <= allowed, sorted(set(words) - allowed)


def test_the_retired_verdict_words_are_absent_from_the_document_and_the_cells(tmp_path):
    """`frozen 14d` was §6.7's round-1 wording and is retired outright. `stable` is scoped
    to the verdict CELLS on purpose -- the substring occurs in unrelated prose elsewhere in
    the page, so a document-wide ban would be a false failure."""
    text = _render_corpus(tmp_path, "retired", _moving_corpus())
    assert "frozen 14d" not in text
    cells = [_verdict_of(row)[1] for row in _trend_rows(text)]
    assert len(cells) == _RENDERED_TREND_ROWS
    for cell in cells:
        assert "stable" not in cell.lower(), cell


def test_every_verdict_word_has_a_distinct_mark():
    """Single-sourced against TREND_VERDICTS: a verdict added without a mark turns this red
    instead of rendering a silent `?`. Distinctness is the whole point -- two words sharing
    a silhouette would make the shape channel decorative."""
    words = [row[0] for row in rh.TREND_VERDICTS]
    assert sorted(rh._VERDICT_MARKS) == sorted(words)
    marks = [rh._VERDICT_MARKS[word] for word in words]
    assert len(set(marks)) == len(marks), marks


def test_each_rendered_verdict_carries_word_shape_and_colour_not_colour_alone(tmp_path):
    """Colour is the THIRD channel, never the first. Every cell carries a WORD (the
    display text), a SHAPE (a silhouette readable in greyscale, in forced-colors and under
    every CVD type) and only then a colour class."""
    text = _render_corpus(tmp_path, "verdictshape", _moving_corpus())
    rows = _trend_rows(text)
    assert len(rows) == _RENDERED_TREND_ROWS
    for row in rows:
        word, cell = _verdict_of(row)
        mark = rh._VERDICT_MARKS[word]
        assert f'<span class="verdict-mark" aria-hidden="true">{mark}</span>' in cell
        display = re.search(r'<span class="verdict-text">([^<]*)</span>', cell)
        assert display is not None and display.group(1).strip() != "", cell
        slug = rh._verdict_slug(word)
        assert f'<td class="trend-verdict verdict-{slug}"' in row
        assert f'<td class="trend-mark verdict-{slug}">' in row
    # ...and every slug the surface can emit has a colour rule, so the third channel is
    # actually wired rather than merely named
    for word in rh._VERDICT_MARKS:
        rule = f".verdict-{rh._verdict_slug(word)} .verdict-mark{{color:var("
        assert rule in rh.STATIC_STYLE, word


def test_verdict_mark_colours_meet_the_large_text_contrast_floor():
    """The composed-value defect a literal scan of the stylesheet structurally cannot see
    (the rules carry no hex at all). Every mark colour is resolved against BOTH parsed
    theme palettes. The mark is sized as WCAG large-scale text (>=18.66px bold), whose
    floor is 3:1 -- which is why the WORD deliberately keeps the default ink instead:
    --good and --warn are about 3.1:1 on --surface in the light theme, under the 4.5:1
    floor for this cell's 0.8rem text.
    # Changing these values requires a spec change (WCAG 2.1 AA 1.4.3)."""
    css = rh.STATIC_STYLE
    sizing = _css_decls(css, ".trend-verdict .verdict-mark")
    assert "font-size:1.25rem" in sizing and "font-weight:700" in sizing, sizing
    light = _theme_tokens(_theme_block(css, ":root{"))
    dark = _theme_tokens(_theme_block(css, ':root[data-theme="dark"]{'))
    checked = 0
    for word in rh._VERDICT_MARKS:
        rule = _css_decls(css, f".verdict-{rh._verdict_slug(word)} .verdict-mark")
        match = re.search(r"color:var\((--[a-z0-9-]+)\)", rule)
        assert match, (word, rule)
        for theme_name, tokens in (("light", light), ("dark", dark)):
            ratio = _wcag_contrast(tokens[match.group(1)], tokens["--surface"])
            assert ratio >= 3.0, (
                f"{theme_name}: {match.group(1)} on --surface = {ratio:.2f}:1, "
                f"below the 3:1 large-text floor ({word})")
            checked += 1
    assert checked == len(rh._VERDICT_MARKS) * 2   # anti-vacuity: the loop really ran


def test_unchanged_across_n_gets_an_amber_mark_and_a_dashed_stroke(tmp_path):
    """`net unchanged` and `unchanged across N` are the same arithmetic and opposite
    meanings. Three devices separate them: different words, an AMBER token (deliberately
    NOT --crit -- never having moved is not a regression), and a DASHED stroke."""
    docs = _dated_trend_docs([dict(memory_bodies=n, phantom=(2, 2)) for n in (5, 9, 5)])
    text = _render_corpus(tmp_path, "dashed", docs)
    flat = _trend_row(text, "Phantom refs (total)")
    assert _verdict_of(flat)[0] == "unchanged across N"
    assert '<td class="trend-mark verdict-unchanged-across-n">' in flat
    style = rh.STATIC_STYLE
    assert "td.verdict-unchanged-across-n .sparkline polyline{stroke-dasharray:3 2}" in style
    assert ".verdict-unchanged-across-n .verdict-mark{color:var(--warn)}" in style
    assert ".verdict-unchanged-across-n .verdict-mark{color:var(--crit)}" not in style


def test_net_unchanged_and_unchanged_across_n_render_differently(tmp_path):
    """Same arithmetic, opposite meanings -- if they collapse at the rendering layer the
    distinction the classifier draws is decorative."""
    docs = _dated_trend_docs([dict(memory_bodies=n, phantom=(2, 2)) for n in (5, 9, 5)])
    text = _render_corpus(tmp_path, "twozerostates", docs)
    flat_word, flat_cell = _verdict_of(_trend_row(text, "Phantom refs (total)"))
    moved_word, moved_cell = _verdict_of(_trend_row(text, "Memory bodies"))
    assert flat_word == "unchanged across N"
    assert moved_word == "net unchanged"
    assert flat_cell != moved_cell
    assert "unchanged across 3 measured runs" in flat_cell
    assert "measured runs" not in moved_cell
    assert rh._VERDICT_MARKS[flat_word] != rh._VERDICT_MARKS[moved_word]


def test_no_direction_renders_for_every_polarity_none_metric_on_both_tables(tmp_path):
    """§6.7's round-1 table printed `improving` for always_loaded_file_count -- a direction
    claim about a metric declared to have no good direction. Both tables carry such a
    metric, so both are checked; `unchecked_binary_count` is the honest case for the word,
    being permanently 0 and explicitly never inspected."""
    text = _render_corpus(tmp_path, "nodirection", _moving_corpus())
    headline = [label for _, label, polarity in rh.HEADLINE_KEYS if polarity == "none"]
    derived = [label for _, label, polarity in rh.DERIVED_TREND_KEYS if polarity == "none"]
    assert headline and derived        # anti-vacuity: BOTH tables must contribute a metric
    for label in headline + derived:
        word, cell = _verdict_of(_trend_row(text, label))
        assert word == "no direction", (label, word)
        assert "improving" not in cell and "worsening" not in cell, label


def test_dual_horizon_disagreement_renders_both_clauses(tmp_path):
    """10 -> 4 -> 6 on a polarity-`up` metric: net improving over the window while the
    LATEST interval worsens. The bare net word would hide the interval the operator can
    still act on."""
    docs = _dated_trend_docs([dict(memory_bodies=n) for n in (10, 4, 6)])
    text = _render_corpus(tmp_path, "dualhorizon", docs)
    word, cell = _verdict_of(_trend_row(text, "Memory bodies"))
    assert word == "improving"
    assert "net improving over 3 measured runs" in cell
    assert "latest interval worsening" in cell


def test_every_rendered_verdict_except_not_measured_states_count_and_span(tmp_path):
    """`improving 3 pts / 14d` is honest; a bare `improving` is not -- three samples across
    a fortnight and three across two years are different claims. Both halves, on every one
    of the 14 rows."""
    docs = [("2026-07-01", _trend_doc(memory_bodies=5)),
            ("2026-07-08", _trend_doc(memory_bodies=9)),
            ("2026-07-15", _trend_doc(memory_bodies=12))]
    text = _render_corpus(tmp_path, "companions", docs)
    rows = _trend_rows(text)
    assert len(rows) == _RENDERED_TREND_ROWS
    for row in rows:
        word, cell = _verdict_of(row)
        assert word != "not measured", row
        assert re.search(r"\d+ pts", cell), cell
        assert "3 pts · 14d" in cell, cell


def test_a_below_floor_series_renders_not_measured_with_its_reason(tmp_path):
    """The other half of the companions contract: below the floor there is no span to
    state, so the reason states the shortfall instead (`2 pts · needs 3`) rather than a
    blank cell."""
    docs = _dated_trend_docs([dict(memory_bodies=n) for n in (5, 9, 12)])
    del docs[0][1]["on_demand"]["memory_bodies"]
    text = _render_corpus(tmp_path, "belowfloor", docs)
    word, cell = _verdict_of(_trend_row(text, "Memory bodies"))
    assert word == "not measured"
    assert f"2 pts · needs {rh.SPARKLINE_MIN_POINTS}" in cell
    # a fully measured sibling in the SAME document is unaffected -- the floor is per series
    assert _verdict_of(_trend_row(text, "Phantom refs (total)"))[0] != "not measured"


# ------------------------------------------- the refusals, rendered beside their reasons
def test_scope_and_quality_suppression_render_not_comparable_with_the_value_visible(
        tmp_path):
    """§6.5a: suppression is DIRECTION-ONLY, on every axis. The value the operator came to
    read survives every refusal, and the refusal names WHICH axis refused."""
    scope_docs = _dated_trend_docs([dict(memory_bodies=n) for n in (5, 9, 12)])
    scope_docs[2][1]["collection_scope"] = {"root": "/other/root", "project_root": None,
                                            "compose": False}
    scope_text = _render_corpus(tmp_path, "scopesuppress", scope_docs)
    scope_row = _trend_row(scope_text, "Memory bodies")
    scope_word, scope_cell = _verdict_of(scope_row)
    assert scope_word == "not comparable"
    assert "collection scope" in scope_cell
    assert "root=/other/root" in scope_cell
    assert "<td>12</td>" in scope_row                    # the VALUE still renders

    quality_docs = _dated_trend_docs([dict(memory_bodies=n) for n in (5, 9, 12)])
    quality_docs[1][1]["metric_quality"]["memory_body_count"] = "partial"
    quality_text = _render_corpus(tmp_path, "qualitysuppress", quality_docs)
    quality_row = _trend_row(quality_text, "Memory bodies")
    quality_word, quality_cell = _verdict_of(quality_row)
    assert quality_word == "not comparable"
    assert "quality partial" in quality_cell
    assert "<td>12</td>" in quality_row
    # ...and ONLY that metric: quality is recorded per (run, metric), not per run
    assert _verdict_of(_trend_row(quality_text,
                                  "Phantom refs (total)"))[0] != "not comparable"


def test_a_definition_confounded_row_renders_the_flag_and_no_direction_word(tmp_path):
    """§8.5: a definition change must never render as a trend. The row states the versions
    it observed and withholds every direction word."""
    baseline = dict(_collector.METRIC_DEFINITIONS)
    bumped = {**baseline, "memory_body_count": baseline["memory_body_count"] + 1}
    docs = _dated_trend_docs([dict(memory_bodies=5), dict(memory_bodies=9),
                              dict(memory_bodies=12, definitions=bumped)])
    text = _render_corpus(tmp_path, "defconfound", docs)
    word, cell = _verdict_of(_trend_row(text, "Memory bodies"))
    assert word == "not comparable"
    assert f"definition v{baseline['memory_body_count']}" in cell
    assert f"definition v{bumped['memory_body_count']}" in cell
    for direction in ("improving", "worsening", "unchanged", "no direction"):
        assert direction not in cell, direction
    # anti-vacuity: an untouched sibling metric in the SAME document keeps its direction
    assert _verdict_of(_trend_row(text, "Phantom refs (total)"))[0] != "not comparable"


def test_a_denominator_confounded_row_names_the_observed_denominators(tmp_path):
    """ALL PAIRS, never first-versus-last: `21 -> 20 -> 21` escapes an endpoint check
    entirely while the middle ratio was computed against a different base, and a shrinking
    denominator manufactures a fake improvement. The reason states the observed SET."""
    docs = _dated_trend_docs([dict(hooks=(16, 21)), dict(hooks=(16, 20)),
                              dict(hooks=(16, 21))])
    text = _render_corpus(tmp_path, "denomconfound", docs)
    word, cell = _verdict_of(_trend_row(text, "Hooks with test"))
    assert word == "not comparable"
    assert "denominators observed: 20, 21" in cell
    assert "denominator 21" in cell and "denominator 20" in cell
    assert _verdict_of(_trend_row(text, "Skills with test"))[0] != "not comparable"


def test_a_stale_basis_row_renders_the_note_and_not_the_cached_prose(tmp_path):
    """§6.8: the model's basis prose must not outlive its inputs. NO FALLBACK SENTENCE --
    a renderer-authored default is a judgment wearing a default's clothing."""
    docs = _dated_trend_docs([dict(memory_bodies=n) for n in (92, 96, 98)])
    synthesis = {"schema_version": 1, "trend_basis": [
        {"metric": "memory_body_count", "prose": "PROSE THAT MUST NOT RENDER",
         "inputs_digest": "0123456789abcdef"}]}
    text = _render_corpus(tmp_path, "stalebasis", docs, synthesis=synthesis)
    row = _trend_row(text, "Memory bodies")
    assert 'class="trend-basis-stale"' in row
    assert rh.TREND_BASIS_STALE_NOTE in row
    assert "PROSE THAT MUST NOT RENDER" not in text     # nowhere in the whole document


def test_a_row_with_no_basis_row_renders_neither_prose_nor_the_stale_note(tmp_path):
    """A DIFFERENT branch from a stale digest, which is why the stale test does not cover
    it: the model never wrote a row for this metric, so there was nothing to go stale and
    emitting the note would invent a history."""
    docs = _dated_trend_docs([dict(memory_bodies=n) for n in (92, 96, 98)])
    synthesis = {"schema_version": 1, "trend_basis": [
        {"metric": "always_loaded_words", "prose": "a row for a different metric",
         "inputs_digest": "0123456789abcdef"}]}
    text = _render_corpus(tmp_path, "nobasisrow", docs, synthesis=synthesis)
    row = _trend_row(text, "Memory bodies")
    assert 'class="trend-basis"' not in row
    assert rh.TREND_BASIS_STALE_NOTE not in row
    # anti-vacuity: the metric that DOES have a row gets the note, so the absence above is
    # the missing row rather than a dead resolver
    assert rh.TREND_BASIS_STALE_NOTE in _trend_row(text, "Always-loaded words")
    # ...and a corpus with no synthesis at all is the same "never wrote one" case
    assert rh.TREND_BASIS_STALE_NOTE not in _render_corpus(tmp_path, "nosynthesis", docs)


def test_derived_ratios_render_as_value_over_total_with_a_percent(tmp_path):
    """`16 / 21 (76%)` -- never a bare percentage (which hides a shrinking denominator, the
    exact way a fake improvement is manufactured) and never a bare float."""
    docs = _dated_trend_docs([dict(hooks=(16, 21)) for _ in range(3)])
    text = _render_corpus(tmp_path, "ratiofmt", docs)
    row = _trend_row(text, "Hooks with test")
    assert row.count("<td>16 / 21 (76%)</td>") == 3
    assert "0.761" not in row
    # a measured-but-undefined ratio keeps its raw counts instead of printing a fake 0%
    zero_text = _render_corpus(tmp_path, "ratiozero",
                               _dated_trend_docs([dict(skills=(0, 0)) for _ in range(3)]))
    assert f"<td>0 / 0 ({rh.NOT_MEASURED_TEXT})</td>" in _trend_row(zero_text,
                                                                    "Skills with test")


def test_two_metrics_moving_the_same_direction_render_opposite_verdicts(tmp_path):
    """THE load-bearing polarity property, RENDERED. Both metrics RISE; a classifier that
    infers direction FROM THE DATA gives them the same word, so the fixture demands
    opposite ones in ONE document. Task 3 proves this at the classifier -- without this row
    it never reaches a page."""
    docs = _dated_trend_docs([dict(tokens_a=6000, hooks=(12, 20)),
                              dict(tokens_a=6500, hooks=(14, 20)),
                              dict(tokens_a=7000, hooks=(16, 20))])
    text = _render_corpus(tmp_path, "polarityrendered", docs)
    tokens_row = _trend_row(text, "Always-loaded tokens (est)")
    ratio_row = _trend_row(text, "Hooks with test")
    assert rh._HEADLINE_POLARITY["always_loaded_tokens_est"] == "up"
    assert dict((k, p) for k, _, p in rh.DERIVED_TREND_KEYS)["hooks_with_test_ratio"] == "down"
    assert _verdict_of(tokens_row)[0] == "worsening"
    assert _verdict_of(ratio_row)[0] == "improving"
    # anti-vacuity: both really did rise, so a flattened fixture cannot leave this passing
    assert "<td>6050</td>" in tokens_row and "<td>7050</td>" in tokens_row
    assert "<td>12 / 20 (60%)</td>" in ratio_row and "<td>16 / 20 (80%)</td>" in ratio_row


# ------------------------------------------------------------------------- THE SEAM TEST
def test_the_renderer_threads_per_point_dates_versions_and_scopes_to_the_classifier(
        tmp_path):
    """THE SEAM TEST, and the reason the function-level/rendered split needed one. Tasks
    3-5 assert against functions that RECEIVE points, dates, resolved versions and
    `collection_scope` values as ARGUMENTS, so they pass whether or not anything ever
    computes them. A green function-level suite can therefore sit on top of a renderer
    that never wires its inputs -- a dark feature.

    Three properties, each holding only if the renderer threads the value it claims to:

    1. PER-POINT DATES. `memory_body_count` is unmeasured on the first of four runs, so its
       window spans 07-02..07-15 (3 pts, 13d) while a fully measured sibling in the SAME
       document spans 07-01..07-15 (4 pts, 14d). A renderer passing the model's full date
       list would print 14d for both; one passing no dates at all would print 0d.
    2. PER-POINT DEFINITION VERSIONS. Flipping ONE run's marker names both version numbers
       in the reason -- values that exist nowhere but in the sidecars.
    3. PER-POINT SCOPES. Flipping ONE run's `collection_scope` names the scope fields it
       read, likewise.

    Each has its paired positive: the unflipped corpus yields a DIRECTION word, which is
    only reachable when the scope and version axes both resolved to real, uniform values.
    A renderer that refused everything unconditionally fails just as loudly as one that
    threaded nothing."""
    dates = ["2026-07-01", "2026-07-02", "2026-07-08", "2026-07-15"]
    docs = [(date, _trend_doc(memory_bodies=count, phantom=(2, 2)))
            for date, count in zip(dates, (1, 92, 96, 98))]
    del docs[0][1]["on_demand"]["memory_bodies"]
    text = _render_corpus(tmp_path, "seam", docs)

    # 1. the dates that actually CONTRIBUTED, not the model's full date list
    bodies_word, bodies_cell = _verdict_of(_trend_row(text, "Memory bodies"))
    sibling_word, sibling_cell = _verdict_of(_trend_row(text, "Phantom refs (total)"))
    assert "3 pts · 13d" in bodies_cell, bodies_cell
    assert "4 pts · 14d" in sibling_cell, sibling_cell
    # ...and both still carry a DIRECTION word, which no unthreaded axis could produce
    assert bodies_word == "worsening"
    assert sibling_word == "unchanged across N"

    # 2. the resolved definition version, per point
    baseline = dict(_collector.METRIC_DEFINITIONS)
    bumped = {**baseline, "memory_body_count": baseline["memory_body_count"] + 1}
    version_docs = _dated_trend_docs([dict(memory_bodies=92), dict(memory_bodies=96),
                                      dict(memory_bodies=98, definitions=bumped)])
    version_text = _render_corpus(tmp_path, "seamversions", version_docs)
    version_word, version_cell = _verdict_of(_trend_row(version_text, "Memory bodies"))
    assert version_word == "not comparable"
    assert f"2026-07-15: definition v{bumped['memory_body_count']}" in version_cell
    assert f"2026-07-13: definition v{baseline['memory_body_count']}" in version_cell

    # 3. the collection scope, per point
    scope_docs = _dated_trend_docs([dict(memory_bodies=92), dict(memory_bodies=96),
                                    dict(memory_bodies=98)])
    scope_docs[2][1]["collection_scope"] = {"root": "/fake/root",
                                            "project_root": "/fake/project",
                                            "compose": True}
    scope_text = _render_corpus(tmp_path, "seamscopes", scope_docs)
    scope_word, scope_cell = _verdict_of(_trend_row(scope_text, "Memory bodies"))
    assert scope_word == "not comparable"
    assert "2026-07-15: scope compose=true project_root=/fake/project" in scope_cell
    assert "2026-07-13: scope compose=false project_root=none" in scope_cell

    # the paired positive for BOTH axes: the unflipped corpus above verdicted a direction
    assert bodies_word in ("improving", "worsening")


def test_series_point_dates_are_recorded_by_the_builders_not_re_derived():
    """The pairing `trend_verdict` needs lives in the ONE place that knows which slots
    became points -- the builders' own drop loop. A second reader re-deriving it is the
    two-homes defect this stage cites against itself three times."""
    docs = [("2026-07-01", _trend_doc(memory_bodies=5)),
            ("2026-07-08", _trend_doc(memory_bodies=9)),
            ("2026-07-15", _trend_doc(memory_bodies=12))]
    del docs[0][1]["on_demand"]["memory_bodies"]
    docs[1][1]["headline"].pop("duplicate_pair_count")
    headline = rh.build_trend_model(docs)
    derived = rh.build_derived_trend_model(docs)
    for model in (headline, derived):
        for series in model["series"]:
            assert len(rh._series_point_dates(series)) == len(rh._series_points(series)), \
                series["key"]
    dropped = next(s for s in derived["series"] if s["key"] == "memory_body_count")
    assert rh._series_point_dates(dropped) == ["2026-07-08", "2026-07-15"]
    pair = next(s for s in headline["series"] if s["key"] == "duplicate_pair_count")
    assert rh._series_point_dates(pair) == ["2026-07-01", "2026-07-15"]
    # a series built before `point_dates` existed degrades to [] -- never a guess, and
    # `_trend_window` then refuses to pair the window at all
    legacy_shape = {"key": "k", "polarity": "up", "values": [1, 2, 3], "points": [1, 2, 3]}
    assert rh._series_point_dates(legacy_shape) == []
    assert rh._trend_window(legacy_shape) == ([1, 2, 3], ["undated"] * 3)


def test_an_unpaired_window_refuses_rather_than_reading_as_comparable():
    """The degrade must add doubt, never remove it. With no dates every axis list would be
    EMPTY, and an empty window is VACUOUSLY comparable on scope, definition and quality --
    a direction word backed by no comparability evidence at all, the single outcome §6.5a
    exists to forbid. `_trend_window` pads to `undated` instead, so every axis lookup
    misses and the row refuses."""
    legacy_shape = {"key": "memory_body_count", "label": "Memory bodies", "polarity": "up",
                    "values": [92, 96, 98], "points": [92, 96, 98]}
    model = {"first_run": False, "series": [legacy_shape], "dates": ["a", "b", "c"]}
    verdict, prose, stale_note = rh._trend_row_state(legacy_shape, model, None, None)
    assert verdict.word == "not comparable"
    assert "scope unknown" in verdict.text
    assert (prose, stale_note) == (None, None)


def test_build_trend_provenance_records_scope_quality_and_resolved_versions():
    """The extractor half of the seam, asserted directly: one row per date, carrying the
    run's scope and every trended metric's quality state and RESOLVED definition version.
    A markerless sidecar whose bytes match no frozen digest resolves UNKNOWN -- never an
    inferred version, and never a default of `complete`."""
    marked = _trend_doc(memory_bodies=5)
    markerless = _trend_doc(scope_root=_NO_MARKERS, definitions=_NO_MARKERS)
    markerless.pop("metric_quality")
    provenance = rh.build_trend_provenance(
        [("2026-07-13", marked), ("2026-07-14", markerless)],
        {"2026-07-13": json.dumps(marked).encode(),
         "2026-07-14": json.dumps(markerless).encode()})
    rows = provenance["by_date"]
    assert sorted(rows) == ["2026-07-13", "2026-07-14"]
    assert rows["2026-07-13"]["scope"] == {"root": "/fake/root", "project_root": None,
                                           "compose": False}
    assert rows["2026-07-13"]["quality"]["memory_body_count"] == rh.QUALITY_COMPLETE
    assert rows["2026-07-13"]["versions"]["memory_body_count"] == \
        _collector.METRIC_DEFINITIONS["memory_body_count"]
    assert rows["2026-07-14"]["scope"] is None
    assert rows["2026-07-14"]["quality"]["memory_body_count"] == rh.QUALITY_UNMEASURED
    assert rows["2026-07-14"]["versions"]["memory_body_count"] is None
    # every rendered metric gets a row on both axes, derived from the two key tables
    expected = {key for key, _, _ in rh.HEADLINE_KEYS} | {
        key for key, _, _ in rh.DERIVED_TREND_KEYS}
    assert set(rows["2026-07-13"]["quality"]) == expected
    assert set(rows["2026-07-13"]["versions"]) == expected
    assert len(expected) == _RENDERED_TREND_ROWS


def test_both_trend_tables_render_inside_one_card(tmp_path):
    """Not a layout preference: the card's title states how many metrics it covers, and N
    is the number of rows the card renders. Two cards would mean two titles and a count
    with no single denominator."""
    text = _render_corpus(tmp_path, "onecard", _moving_corpus())
    card = re.search(r'<div class="card"><h2>Trend[^<]*</h2>(?:(?!<div class="card">).)*',
                     text, re.S)
    assert card is not None
    assert len(_TREND_ROW_RE.findall(card.group(0))) == _RENDERED_TREND_ROWS
    assert card.group(0).count('class="sparkline sparkline-derived"') == \
        len(rh.DERIVED_TREND_KEYS)


# ============================= S6c Task 8 (A52, deliverable 22): disclose the refusal
# A52 measured the LIVE corpus on 2026-08-06: every series was `not comparable`, for two
# independent causes -- no sidecar carries `metric_definitions` matching a legacy digest,
# and none carries `collection_scope` at all. That is the CORRECT verdict. The risk is
# not the refusal -- it is a page of fourteen identical `not comparable` cells with
# nothing explaining them, which reads as a broken feature and invites a later "fix"
# that weakens the refusal into a guess. Task 7 already renders the per-row reason (the
# built axis string rides inside `trend_verdict`'s `not comparable` text); this section
# pins that as a CONTRACT with its own extractors and adds the one thing that was
# missing: a section-level sentence disclosing the blackout, present only while it lasts.

def _all_rendered_metric_labels(text):
    """One label per rendered trend row, across BOTH tables, in document order -- the
    same row set `_trend_rows` walks."""
    return [row.split("<td>", 1)[1].split("</td>", 1)[0] for row in _trend_rows(text)]


def _value_cell(text, label):
    """The per-date VALUE columns of one trend row -- everything after the Direction
    cell, concatenated. Never the verdict text: suppression is direction-only, and this
    is the half of the row that must survive every refusal."""
    row = _trend_row(text, label)
    match = _VERDICT_CELL_RE.search(row)
    assert match is not None, row
    return row[match.end():row.rindex("</tr>")]


# The boundary between the built axis reason and `trend_verdict`'s own `N pts · Md`
# companion -- the SAME " · " separator `trend_verdict` joins them with, so this reads
# the format rather than re-deciding it.
_REFUSAL_DETAIL_RE = re.compile(r"not comparable — (.*?) · \d+ pts", re.S)


def _reason_cell(text, label):
    """The factual axis reason for one row, or `""` when the row does not refuse.
    Extracts the SAME string `series_comparability` already built and `trend_verdict`
    already rendered -- no second reason vocabulary."""
    word, cell = _verdict_of(_trend_row(text, label))
    if word != "not comparable":
        return ""
    match = _REFUSAL_DETAIL_RE.search(cell)
    assert match is not None, cell
    return match.group(1)


def _all_reason_cells(text):
    """One reason string per rendered row (possibly `""`), in document order -- the same
    row set `_all_rendered_metric_labels` walks."""
    return [_reason_cell(text, label) for label in _all_rendered_metric_labels(text)]


def test_every_refusing_row_states_its_reason(tmp_path):
    """A52. A `not comparable` with no stated reason reads as a broken feature. Every
    refusing row carries a factual one-liner naming the dates, the versions or scopes
    observed, and WHICH AXIS refused."""
    scope_docs = _dated_trend_docs([dict(memory_bodies=n) for n in (5, 9, 12)])
    scope_docs[2][1]["collection_scope"] = {"root": "/other/root", "project_root": None,
                                            "compose": False}
    scope_text = _render_corpus(tmp_path, "reasondiscscope", scope_docs)
    labels = _all_rendered_metric_labels(scope_text)
    assert len(labels) == _RENDERED_TREND_ROWS, "metric-label extraction missing or incomplete"
    # scope is a property of the RUN, not the metric -- the refusal poisons every row
    reasons = {label: _reason_cell(scope_text, label) for label in labels}
    assert all(reasons.values()), reasons
    assert all("collection scope" in reason for reason in reasons.values())
    assert all("root=/other/root" in reason for reason in reasons.values())

    quality_docs = _dated_trend_docs([dict(memory_bodies=n) for n in (5, 9, 12)])
    quality_docs[1][1]["metric_quality"]["memory_body_count"] = "partial"
    quality_text = _render_corpus(tmp_path, "reasondiscquality", quality_docs)
    memory_reason = _reason_cell(quality_text, "Memory bodies")
    sibling_reason = _reason_cell(quality_text, "Phantom refs (total)")
    assert memory_reason != "" and "quality partial" in memory_reason
    # ...and ONLY that metric: quality is recorded per (run, metric), not per run
    assert sibling_reason == ""


def test_the_full_blackout_corpus_shape_renders_values_and_reasons(tmp_path):
    """THE LIVE CORPUS SHAPE, pinned as contract rather than left as an accident.
    Fixture mirrors A52's measurement: markerless sidecars whose digests match no
    legacy entry. Every metric refuses a direction -- AND every metric still shows its
    value, its series and its point count, each beside a reason."""
    docs = _dated_trend_docs([
        dict(scope_root=_NO_MARKERS, definitions=_NO_MARKERS, memory_bodies=n)
        for n in (5, 9, 12)])
    text = _render_corpus(tmp_path, "blackoutshape", docs)
    labels = _all_rendered_metric_labels(text)
    assert len(labels) == _RENDERED_TREND_ROWS, "metric-label extraction missing or incomplete"
    for label in labels:
        word = _verdict_of(_trend_row(text, label))[0]
        assert word == "not comparable", (label, word)
        assert _value_cell(text, label) != ""      # the VALUE always survives
        assert _reason_cell(text, label) != ""     # and never refuses silently


def test_reason_strings_carry_no_verdict_word(tmp_path):
    """Binding rule 6, and the shipped precedent is `_CONFOUND_REASON_FORBIDDEN` --
    reuse that tuple rather than writing a second list."""
    docs = _dated_trend_docs([
        dict(scope_root=_NO_MARKERS, definitions=_NO_MARKERS, memory_bodies=n)
        for n in (5, 9, 12)])
    text = _render_corpus(tmp_path, "reasonnoverdict", docs)
    reasons = _all_reason_cells(text)
    assert len(reasons) == _RENDERED_TREND_ROWS, "reason extraction missing or incomplete"
    assert all(reasons), "every row in this corpus refuses -- an empty reason is a miss"
    for reason in reasons:
        assert not any(w in reason.lower() for w in rh._CONFOUND_REASON_FORBIDDEN)


def test_section_level_disclosure_appears_exactly_once(tmp_path):
    """One section-level sentence explaining the blackout, not one per row -- N
    identical sentences is noise that trains the operator to skip the column. The
    per-row reasons carry the specifics."""
    docs = _dated_trend_docs([
        dict(scope_root=_NO_MARKERS, definitions=_NO_MARKERS, memory_bodies=n)
        for n in (5, 9, 12)])
    text = _render_corpus(tmp_path, "blackoutonce", docs)
    assert text.count(rh.TREND_COMPARABILITY_BLACKOUT_NOTE) == 1


def test_disclosure_is_absent_once_the_corpus_is_comparable(tmp_path):
    """The blackout is a STATE, not a permanent banner. Two adjacent sidecars carrying
    both markers get a verdict and no blackout disclosure -- which is what every run
    from this stage forward produces. Without this test the disclosure becomes
    unconditional chrome nobody notices is stale."""
    text = _render_corpus(tmp_path, "blackoutlifted", _moving_corpus())
    assert rh.TREND_COMPARABILITY_BLACKOUT_NOTE not in text
    # anti-vacuity: the comparable corpus really does verdict every row, not just
    # happen to omit the note for an unrelated reason (e.g. an empty card)
    for label in _all_rendered_metric_labels(text):
        assert _verdict_of(_trend_row(text, label))[0] != "not comparable", label


# ============================================== S6c Task 9: fork (1) discoverability
# The reported bug, root-caused: an operator saw a `down 992` delta on the Weight view,
# opened the only affordance attached to it -- the gauge drill -- and hit a dead end.
# `_GAUGE_TAB_HINT` had no entry for `always_loaded_words` or `always_loaded_tokens_est`,
# so those two tiles' drill panels returned WITHOUT the "-> open the ... tab" pointer
# every other count/aggregate gauge already carries. The Trend card (which DOES carry
# the real per-file history those two tiles summarize) was also buried 5th of 6 cards
# deep in the Hygiene view, roughly 20 scroll ticks past the fold -- so even an operator
# who found the tab by other means had to hunt for the table once there.

def _panel_for(text, key):
    """The gauge-drawer inner HTML for one gauge key -- same non-greedy `</div>` anchor
    every gauge-drill assertion in this module already uses (the drawer's own content
    carries no nested `<div>`, so the first `</div>` closes the drawer, not a false
    early stop)."""
    match = re.search(rf'id="gdrawer-{key}"[^>]*>(.*?)</div>', text, re.S)
    assert match is not None, key
    return match.group(1)


def test_both_weight_tiles_point_onward_to_hygiene(tmp_path):
    """THE REGRESSION TEST FOR THE REPORTED BUG -- it must fail against pre-S6c source.
    The operator saw a `down 992` delta, opened the only affordance attached to it, and
    hit a dead end: `_GAUGE_TAB_HINT` had no entry for always_loaded_words or
    always_loaded_tokens_est, so those two tiles' drill panels returned WITHOUT the
    '-> open the ... tab' pointer every other tile gets."""
    doc = _minimal_doc()
    out_dir = tmp_path / "weighthint"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    for key in ("always_loaded_words", "always_loaded_tokens_est"):
        assert "open the Hygiene tab for the full table" in _panel_for(text, key)


def test_trend_card_is_first_in_the_hygiene_view(tmp_path):
    """Was 5th of 6 cards deep in the Hygiene view, roughly 20 scroll ticks past the
    fold. The Trend card is now the first thing the view renders -- assert its heading
    precedes Length flags, Duplication pairs, Phantom refs and Unchecked binaries."""
    text = _render_corpus(tmp_path, "trendfirst", _moving_corpus())
    hyg = re.search(r'<section id="view-hygiene".*?</section>', text, re.S)
    assert hyg is not None
    hyg_html = hyg.group(0)
    trend_idx = hyg_html.index("<h2>Trend")
    for marker in ("<h2>Length flags</h2>", "<h2>Duplication pairs",
                  "<h2>Phantom refs</h2>", "Unchecked binaries:"):
        marker_idx = hyg_html.index(marker)
        assert trend_idx < marker_idx, (marker, trend_idx, marker_idx)


def test_trend_card_title_still_starts_with_trend(tmp_path):
    """Pins compatibility with the shipped
    test_hygiene_view_folds_dup_phantom_trend_and_wiring (`"Trend" in hyg_html`) rather
    than trusting it -- the retitle changes everything after the word "Trend", never
    the word itself."""
    text = _render_corpus(tmp_path, "trendtitleword", _moving_corpus())
    match = re.search(r'<div class="card"><h2>(Trend[^<]*)</h2>', text)
    assert match is not None, text
    assert match.group(1).startswith("Trend")


def test_trend_card_title_count_equals_the_rendered_row_count(tmp_path):
    """N IS DERIVED. A test pinning the literal 13 or 14 recreates the exact defect one
    milestone later -- the round-1 text hardcoded '(12 metrics)' and was already stale
    when written. This assertion is what makes the count self-correcting when a metric
    is added or excluded. N counts BOTH tables: the 8 HEADLINE_KEYS the legacy table
    still iterates (including unchecked_binary_count, which three shipped sparkline
    assertions require) plus the 6 DERIVED_TREND_KEYS."""
    text = _render_corpus(tmp_path, "trendtitlecount", _moving_corpus())
    match = re.search(r'<h2>Trend[^(]*\((\d+) metrics\)</h2>', text)
    assert match is not None, text
    title_n = int(match.group(1))
    rendered_n = len(_trend_rows(text))
    assert rendered_n == _RENDERED_TREND_ROWS
    assert rendered_n == len(rh.HEADLINE_KEYS) + len(rh.DERIVED_TREND_KEYS)
    assert title_n == rendered_n


def test_four_views_and_copy_payloads_are_untouched(tmp_path):
    """Fork (1): NO NEW TAB. Moving the Trend card inside the Hygiene view and
    retitling it must not add, remove, or rename any view or copy-payload island -- a
    sibling guard naming that invariant explicitly, since Task 9 touches the same view
    fork (1) already resolved."""
    doc = _minimal_doc()
    out_dir = tmp_path / "fourviewsuntouched"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    for vid in ("view-overview", "view-weight", "view-friction", "view-hygiene"):
        assert f'id="{vid}"' in text
    assert 'id="view-coverage"' not in text
    assert text.count('class="view-btn"') == 4
    for vid in ("overview", "weight", "friction", "hygiene"):
        assert f'<script type="application/json" id="copy-{vid}">' in text
    assert 'id="copy-coverage"' not in text


# ==================================== S6c Task 10: the sparkline on the stat tile
# The third sparkline surface (Ambiguity B): the gauge card that already shows this
# series' delta now also carries its own namespaced sparkline, `sparkline-tile`,
# distinct from the legacy per-date table's `sparkline` and the derived trend table's
# `sparkline-derived`. Only the 5 headline-kind GAUGE_SPECS keys get a tile sparkline —
# `GAUGE_SPECS` wires 5 of the 8 HEADLINE_KEYS to a gauge card; the other 3
# (`orphan_registration_count`, `orphan_script_count`, `unchecked_binary_count`) have no
# gauge card to carry one.
_TILE_GAUGE_KEYS = [key for kind, key, _ in rh.GAUGE_SPECS if kind == "headline"]


def test_existing_gauge_call_sites_render_byte_identical():
    """Default "" is LOAD-BEARING. This is the exact pattern `band_value=None` already
    established in this function, whose docstring says so in terms: 'Default None ->
    band follows the displayed value, so every existing call site and its rendered
    bytes are unchanged.' Follow that precedent rather than inventing a new one."""
    import inspect
    params = inspect.signature(rh._render_gauge).parameters
    assert params["sparkline_html"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert params["sparkline_html"].default == ""
    assert rh._render_gauge("always_loaded_words", "Always-loaded words", 60) == (
        '<div class="gauge gauge-good" data-gauge="always_loaded_words">'
        '<div class="v">60</div><div class="l">Always-loaded words</div>'
        '<div class="band">LEAN</div></div>')
    assert rh._render_gauge("always_loaded_words", "Always-loaded words", 60,
                            delta=("▲ 10", "bad"), has_drill=True) == (
        '<button class="gauge gauge-good" data-gauge="always_loaded_words" '
        'aria-expanded="false" aria-controls="gdrawer-always_loaded_words">'
        '<div class="v">60</div><div class="l">Always-loaded words</div>'
        '<div class="band">LEAN</div><div class="delta delta-bad">▲ 10</div>'
        '<span class="gauge-chev" aria-hidden="true">▾</span></button>')


def test_weight_tiles_carry_a_sparkline_at_or_above_the_floor(tmp_path):
    """>=3 dated sidecars: every headline-kind gauge card gets its own `sparkline-tile`
    mark, namespaced by DOM id so it cannot collide with the legacy or derived marks
    for the same key."""
    text = _render_corpus(tmp_path, "tilefloor", _moving_corpus())
    assert text.count('class="sparkline sparkline-tile"') == len(_TILE_GAUGE_KEYS)
    for key in _TILE_GAUGE_KEYS:
        assert f'id="spark-tile-{key}"' in text
        gauge = re.search(rf'data-gauge="{key}"[^>]*>(.*?)<span class="gauge-chev"',
                          text, re.S)
        assert gauge is not None, key
        assert f'id="spark-tile-{key}"' in gauge.group(1)


def test_weight_tiles_carry_no_sparkline_below_the_floor(tmp_path):
    """Sub-floor series stay byte-identical to today."""
    text = _render_corpus(tmp_path, "tilenofloor", _dated_trend_docs(
        [dict(memory_bodies=5), dict(memory_bodies=9)]))
    assert 'class="sparkline"' not in text
    assert 'class="sparkline sparkline-tile"' not in text
    for key in _TILE_GAUGE_KEYS:
        assert f'id="spark-tile-{key}"' not in text
        assert f'data-gauge="{key}"' in text          # the tile itself still renders


def test_tile_sparkline_is_namespaced_and_follows_the_delta(tmp_path):
    """Ambiguity B: the tile needs its OWN class AND its own DOM id, or
    `id="spark-always_loaded_words"` appears twice in one document (invalid HTML, broken
    aria-labelledby) and the three shipped count assertions go 8 -> 10.
    Emitted order: value (.v) -> label (.l) -> band -> delta -> sparkline. Fork (1)'s
    phrase 'third field' is the diagnostic's CONCEPTUAL wording, not a positional
    index: `_render_gauge` has no positional field slots, it concatenates named
    fragments. Do not reorder or reindex the existing fragments."""
    docs = []
    for i, words in enumerate((50, 55, 60)):
        doc = _minimal_doc()
        doc["headline"]["always_loaded_words"] = words
        docs.append((f"2026-07-{13 + i:02d}", doc))
    out_dir = tmp_path / "tilenamespace"
    out_dir.mkdir()
    for date, doc in docs:
        _write_sidecar(out_dir, date, doc)
    proc = run_render(out_dir, "--date", "2026-07-15", "--no-friction")
    assert proc.returncode == 0, proc.stderr
    text = (out_dir / "harness-map-2026-07-15.html").read_text(encoding="utf-8")
    assert 'id="spark-tile-always_loaded_words"' in text
    assert text.count('class="sparkline"') == len(rh.HEADLINE_KEYS)
    # the legacy namespace for the same key is untouched, and the two ids coexist
    assert 'id="spark-always_loaded_words"' in text
    gauge = re.search(r'data-gauge="always_loaded_words"[^>]*>(.*?)<span class="gauge-chev"',
                      text, re.S)
    assert gauge is not None
    inner = gauge.group(1)
    v_idx = inner.index('class="v"')
    l_idx = inner.index('class="l"')
    band_idx = inner.index('class="band"')
    delta_idx = inner.index('class="delta')
    spark_idx = inner.index('id="spark-tile-always_loaded_words"')
    assert v_idx < l_idx < band_idx < delta_idx < spark_idx


# ============================================================= S6c Task 11 (§6.8): the
# trend_basis contract's skeleton, mirroring the 36-cell CIVC completeness doctrine.
def test_synthesis_template_has_one_trend_basis_row_per_trended_metric():
    """Mirrors the 36-cell CIVC skeleton doctrine: the writer emits the full skeleton so
    a gap in coverage is INTENTIONAL (a real judgment) rather than ACCIDENTAL (an
    omitted row that merely looks like one)."""
    template_path = Path(__file__).resolve().parents[1] / "synthesis-template.json"
    rows = json.loads(template_path.read_text(encoding="utf-8"))["trend_basis"]
    assert ({r["metric"] for r in rows}
            == {k for k, _, _ in rh.HEADLINE_KEYS} | {k for k, _, _ in rh.DERIVED_TREND_KEYS})


# =========================================================== S6c Task 13: the residual
# coverage audit. Every case below is CROSS-CUTTING -- it belongs to no single earlier
# task and exists to close a gap the three-list audit in the task report names
# explicitly. Nothing here changes production code; every assertion pins EXISTING,
# already-correct behavior (see the report's Pre-fix RED evidence: BASELINE-GREEN).

# One fixture per NON-DIRECTION `TREND_VERDICTS` word -- the five "no answer" states a
# refusing or non-directional series can render. `improving`/`worsening` are excluded on
# purpose: those two ARE answers, and this test is about the ones that are not.
_NON_DIRECTION_FIXTURES = {
    "not measured": dict(points=[10, 20], dates=_dates_for([10, 20]), polarity="up"),
    "not comparable": dict(points=[10, 20, 30], dates=_dates_for([10, 20, 30]),
                           polarity="up", comparability="scope changed on 2026-07-14"),
    "no direction": dict(points=[10, 20, 30], dates=_dates_for([10, 20, 30]),
                         polarity="none"),
    "unchanged across N": dict(points=[10, 10, 10], dates=_dates_for([10, 10, 10]),
                               polarity="up"),
    "net unchanged": dict(points=[10, 20, 10], dates=_dates_for([10, 20, 10]),
                          polarity="up"),
}


def test_no_answer_states_render_as_distinct_strings():
    """The flavours of 'no answer' -- `unchanged across N`, `not comparable`,
    `not measured`, `no direction` and `net unchanged` -- must be DISTINCT rendered
    strings. Lookalike no-answers are the discoverability defect (A52) reappearing one
    layer down: a page where two different refusals print the same text is as opaque as
    a page with no reason at all. Cardinality is DERIVED FROM THE ENUM, never pinned at
    a literal -- it is `len(TREND_VERDICTS)` minus the direction words, so a future
    eighth verdict state fails this test's own sanity check rather than silently passing
    a stale count."""
    all_words = [row[0] for row in rh.TREND_VERDICTS]
    non_direction_words = [w for w in all_words if w not in ("improving", "worsening")]
    # sanity: the hand-built fixture map has not drifted from the enum it mirrors
    assert set(non_direction_words) == set(_NON_DIRECTION_FIXTURES)
    assert len(non_direction_words) == len(all_words) - 2

    texts = []
    for word in non_direction_words:
        verdict = rh.trend_verdict(**_NON_DIRECTION_FIXTURES[word])
        assert verdict.word == word, (word, verdict.word)
        texts.append(verdict.text)
    assert len(set(texts)) == len(non_direction_words), texts


# The explicit, hand-maintained roster of every S6c function `_TOTALITY_TARGETS`
# registers (Tasks 1-2, 3, 4, 5 and 7 -- the S6b comparability machinery Task 4 extends
# stays out, since it is not S6c's to claim).
_S6C_TOTAL_FUNCTIONS = (
    "_metric_quality",
    "_derived_promotion_candidate_count",
    "_derived_memory_body_count",
    "_derived_phantom_ref_count",
    "_derived_phantom_confirmed_count",
    "_derived_hooks_test_ratio",
    "_derived_skills_test_ratio",
    "build_derived_trend_model",
    "_trend_point_value",
    "trend_verdict",
    "metric_quality_state",
    "_scope_readable",
    "scope_comparable",
    "_scope_display",
    "build_scope_reason",
    "quality_comparable",
    "build_quality_reason",
    "_trend_point_denominator",
    "_observed_denominators",
    "denominators_comparable",
    "build_denominator_reason",
    "_dated_axis",
    "series_comparability",
    "trend_inputs_digest",
    "trend_basis_for",
    "_series_point_dates",
    "_trend_window",
    "_provenance_record",
    "_provenance_metric",
    "_series_axes",
    "build_trend_provenance",
    "_verdict_slug",
    "_fmt_trend_value",
    "_trend_latest_direction",
)


def test_the_enumerated_s6c_functions_are_registered_in_the_totality_guard():
    """Asserts that every name in the EXPLICIT list above appears in
    `_TOTALITY_TARGETS`. That is the whole claim -- deliberately narrower than the name
    an earlier draft gave this test.

    WHY IT CANNOT BE STRONGER: nothing distinguishes an "S6c function" by
    introspection, and `_TOTALITY_TARGETS` holds bare callables with no metadata, so the
    only comparison available is `__name__` against a hand-maintained list. It therefore
    catches a registration someone REMOVED, or renamed on one side only -- and CANNOT
    catch a new function nobody registered anywhere, which is exactly the case a
    coverage-sounding name would have implied it covered.

    THE REAL ANTI-ROT MECHANISM FOR THAT CASE IS THE TASK 13 WRITTEN AUDIT, done by a
    human reading the diff against three lists. This test is a cheap regression pin
    underneath it, never a substitute for it. Do not reword this docstring into a
    completeness claim -- a test that appears to guarantee coverage it cannot deliver is
    the false-green class this project has been burned by."""
    registered = {fn.__name__ for fn, _ in _TOTALITY_TARGETS}
    for name in _S6C_TOTAL_FUNCTIONS:      # explicit list, maintained by hand
        assert name in registered, name


def test_zero_one_and_two_measured_points_all_read_not_measured():
    """Failure-modes row 12 names three counts explicitly -- 0, 1 or 2 -- and the shipped
    suite pinned only the boundary (2, in `test_below_floor_two_points_gets_no_direction_word`).
    The guard is `count < SPARKLINE_MIN_POINTS`, the SAME branch for all three, so this is
    not a new code path -- it closes the letter of the row rather than leaving 0 and 1
    to a reader's inference from the boundary case."""
    for points in ([], [10]):
        dates = _dates_for(points)
        verdict = rh.trend_verdict(points=points, dates=dates, polarity="up")
        assert verdict.word == "not measured", points
        assert f"{len(points)} pts" in verdict.reason, points
        assert f"needs {rh.SPARKLINE_MIN_POINTS}" in verdict.reason, points
