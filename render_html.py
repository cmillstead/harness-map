#!/usr/bin/env python3
"""harness-map HTML renderer: deterministic, stdlib-only, read-only over the harness.

Reads the sidecar JSON(s) already written by collector.py (Step A) in `--out-dir`,
plus four optional friction telemetry streams, and emits ONE offline
`harness-map-<date>.html`. Never re-runs the collector, never calls a model, never
reads `os.environ`/`settings.json` directly (secrets structurally cannot leak — the
renderer only ever sees `config.env_keys`, which the collector already limited to
names). See docs/plans/2026-07-15-harness-map-html-viz-design.md for the full spec;
this module implements it (precedence: 9-C2 > 9-R > body).
"""
import argparse
import base64
import hashlib
import html
import json
import os
import re
import sys
import tempfile
from pathlib import Path

SCHEMA_VERSION = 1
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SIDECAR_RE = re.compile(r"^harness-map-(\d{4}-\d{2}-\d{2})\.json$")

# --- fixed enums (determinism §4.4: never sorted(set(...)), always the schema tuple) ---
ALWAYS_CATEGORIES = (
    ("claude_md", "CLAUDE.md (root)"),
    ("project_claude_md", "CLAUDE.md (project)"),
    ("memory", "Memory index"),
    ("rule", "Rules"),
    ("coding_team_rule", "coding-team rules"),
    ("skill_rule", "Sub-skill rules"),
)
ON_DEMAND_GROUPS = (
    ("skill", "Skill bodies"),
    ("phase", "Phase files"),
    ("prompt", "Prompt files"),
    ("agent", "Agent files"),
    ("memory", "Memory bodies"),
)
HEADLINE_KEYS = (
    ("always_loaded_words", "Always-loaded words", "up"),
    ("always_loaded_tokens_est", "Always-loaded tokens (est)", "up"),
    ("always_loaded_file_count", "Always-loaded files", "none"),
    ("duplicate_pair_count", "Duplicate pairs", "up"),
    ("unchecked_binary_count", "Unchecked binaries (reserved)", "none"),
    ("instruction_files_over_200", "Instruction files > 200 lines", "up"),
    ("orphan_registration_count", "Orphan registrations", "up"),
    ("orphan_script_count", "Orphan scripts", "up"),
)
VERBS = ("Afford", "Inform", "Constrain", "Verify", "Correct", "Evolve")
SURFACES = ("context", "tools", "memory", "permissions", "orchestration", "observability")

# §9-R E — CLOSED allowlists, verified against skills/coding-team/ on 2026-07-15.
PHASE_ALIAS = {
    "execute": "execution.md", "plan": "planning.md", "audit": "audit-loop.md",
    "complete": "completion.md", "post-exec-review": "post-execution-review.md",
    "design": "design-team.md", "spec": "spec-review.md",
}
AGENT_ALIAS = {
    "builder": "ct-implementer.md", "reviewer": "ct-spec-reviewer.md", "qa": "ct-qa-reviewer.md",
    "harden": "ct-harden-auditor.md", "simplify": "ct-simplify-auditor.md",
    "prompt": "ct-prompt-craft-auditor.md", "spec_review": "ct-spec-doc-reviewer.md",
    "plan_review": "ct-plan-doc-reviewer.md",
}

CATEGORICAL_PALETTE = ("#0072B2", "#E69F00", "#009E73", "#CC79A7", "#56B4E9", "#D55E00")
HEAT_RAMP = ("#FCAE91", "#FB6A4A", "#DE2D26", "#A50F15")
STREAM_ORDER = ("decisions", "metrics", "interventions", "codex")
STREAM_LABELS = {"decisions": "Decisions", "metrics": "Review metrics",
                  "interventions": "Interventions", "codex": "Codex reviews"}
CODEX_VERDICT_LABELS = {"APPROVED": "approved", "PASS": "pass", "REVISE": "needed revision",
                         "SHIP": "shipped"}


# --------------------------------------------------------------------------- escaping
def esc_html(value):
    """HTML/attribute/SVG-text escaping — the single primitive for every scanned or
    telemetry string leaf (§3.1). Covers text content, attributes, and SVG text/attrs
    (shared HTML5 tokenizer). Lone UTF-16 surrogates (the collector deliberately
    preserves them — Codex F9) are neutralized to a deterministic backslash escape
    BEFORE html.escape, since json.dumps/str() pass them through untouched otherwise."""
    text = str(value)
    text = re.sub(r"[\ud800-\udfff]", lambda m: f"\\u{ord(m.group(0)):04x}", text)
    return html.escape(text, quote=True)


def esc_json_script(value, *, ordered=True):
    """Fallback-only helper (§3.1) for the sole content of a `<script
    type="application/json">` data island read via `.textContent` + `JSON.parse`.
    NOT used by default — the renderer keeps dynamic data out of executable script
    blocks entirely (§9-R C)."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=ordered)
    return text.translate({ord("<"): "\\u003c", ord(">"): "\\u003e", ord("&"): "\\u0026"})


def _fmt_float(x):
    """One shared fixed-precision float formatter (§4.6) for every SVG/text number."""
    return f"{round(float(x), 2):.2f}"


# --------------------------------------------------------------------- discovery / load
def find_sidecars(out_dir):
    """[(date_str, Path)] for every `harness-map-YYYY-MM-DD.json` in `out_dir`, sorted
    ascending by date. Filename-regex + lexicographic sort — never mtime/iterdir order
    (§4.1). Explicitly excludes `harness-synthesis-*.json` (Codex F7)."""
    out = []
    try:
        names = sorted(p.name for p in Path(out_dir).iterdir())
    except OSError:
        return []
    for name in names:
        m = SIDECAR_RE.match(name)
        if m:
            out.append((m.group(1), Path(out_dir) / name))
    return out


def load_sidecar(path):
    """(doc|None, error|None) — never raises. Structural TYPE validation (§6): top-level
    must be a dict; `schema_version` must be present."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        return None, f"unreadable: {e}"
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as e:
        return None, f"invalid JSON: {e}"
    if not isinstance(doc, dict):
        return None, "top-level JSON is not an object"
    if "schema_version" not in doc:
        return None, "missing schema_version"
    return doc, None


def load_synthesis(out_dir, date):
    """(doc|None, error|None) — the synthesis sidecar is OPTIONAL; absence is not an
    error (returns (None, None)). An invalid file is an explicit unavailable state
    (§6), never a partial render."""
    path = Path(out_dir) / f"harness-synthesis-{date}.json"
    if not path.exists():
        return None, None
    doc, err = load_sidecar(path)
    if err is not None:
        return None, f"synthesis unavailable: {err}"
    return doc, None


def select_current(sidecars, date):
    """(date_str, doc, skipped[]) — exact-match only when `date` given (typo is FATAL,
    Codex F8); otherwise the LATEST VALID sidecar, using ITS actual date consistently.
    A corrupt sidecar among several is excluded + listed in `skipped[]`; an explicit
    `--date` naming a corrupt sidecar is fatal (never silently substitutes)."""
    skipped = []
    if date is not None:
        match = next((p for d, p in sidecars if d == date), None)
        if match is None:
            return None, None, skipped, f"no sidecar found for date {date}"
        doc, err = load_sidecar(match)
        if err is not None:
            return None, None, skipped, f"sidecar for {date} is corrupt: {err}"
        return date, doc, skipped, None
    for d, p in reversed(sidecars):
        doc, err = load_sidecar(p)
        if err is not None:
            skipped.append({"date": d, "reason": err})
            continue
        return d, doc, skipped, None
    return None, None, skipped, "no valid sidecar found"


# --------------------------------------------------------------------------- node keys
def _al_node_key(path):
    return f"always_loaded:{path}"


def _od_node_key(rel):
    return f"on_demand:{rel}"


def _hook_node_key(name):
    return f"hook:{Path(name).name}"


def _dup_node_key(path):
    """Map a duplication-corpus path onto the SAME node_key an existing view already
    uses (§1.3) so friction/dup heat lands on one identity, not a shadow duplicate."""
    if re.match(r"^(rules/|skills/[^/]+/rules/)", path):
        return _al_node_key(path)
    m = re.match(r"^skills/([^/]+)/SKILL\.md$", path)
    if m:
        return _od_node_key(m.group(1))
    if re.match(r"^skills/[^/]+/(phases|prompts|agents)/", path):
        return _od_node_key(path)
    return f"dup:{path}"


def _basename_of_node_key(node_key):
    _, _, rel = node_key.partition(":")
    return Path(rel or node_key).name.lower()


# ------------------------------------------------------------------------- squarify
def _worst_ratio(row, side):
    total = sum(row)
    if total <= 0 or side <= 0:
        return float("inf")
    thickness = total / side
    if thickness <= 0:
        return float("inf")
    worst = 0.0
    for a in row:
        length = a / thickness
        ratio = max(thickness / length, length / thickness) if length > 0 else float("inf")
        worst = max(worst, ratio)
    return worst


def squarify(items, x, y, w, h):
    """2-D squarified treemap layout (ratified §9-R A). `items`: list of dicts each
    carrying a numeric 'size' key; non-positive sizes must already be filtered out by
    the caller (Codex F12). Returns new dicts (input dicts + x/y/w/h float geometry),
    in the same fill order as input — callers must pass items pre-sorted by a TOTAL
    key for determinism. Last cell of each row snaps to the row boundary to absorb
    float drift (§4.6)."""
    items = [i for i in items if i.get("size", 0) > 0]
    n = len(items)
    if n == 0 or w <= 0 or h <= 0:
        return []
    total = sum(i["size"] for i in items)
    scale = (w * h) / total
    sizes = [i["size"] * scale for i in items]

    out = []
    idx = 0
    cx, cy, cw, ch = x, y, w, h
    while idx < n and cw > 0 and ch > 0:
        vertical = cw >= ch
        side = ch if vertical else cw
        row = [sizes[idx]]
        row_idx = [idx]
        best = _worst_ratio(row, side)
        j = idx + 1
        while j < n:
            trial = row + [sizes[j]]
            trial_ratio = _worst_ratio(trial, side)
            if trial_ratio <= best:
                row, row_idx, best = trial, row_idx + [j], trial_ratio
                j += 1
            else:
                break
        row_total = sum(row)
        thickness = row_total / side if side > 0 else 0.0
        offset = 0.0
        for k, ridx in enumerate(row_idx):
            is_last = k == len(row_idx) - 1
            length = (side - offset) if is_last else (row[k] / thickness if thickness > 0 else 0.0)
            if vertical:
                rx, ry, rw, rh = cx, cy + offset, thickness, length
            else:
                rx, ry, rw, rh = cx + offset, cy, length, thickness
            out.append({**items[ridx], "x": _fmt_float(rx), "y": _fmt_float(ry),
                        "w": _fmt_float(rw), "h": _fmt_float(rh)})
            offset += length
        if vertical:
            cx, cw = cx + thickness, cw - thickness
        else:
            cy, ch = cy + thickness, ch - thickness
        idx = j
    return out


# --------------------------------------------------------------------------- transforms
def _tokens_treemap(files, canvas_w=960.0, canvas_h=420.0):
    by_cat = {}
    for f in files:
        by_cat.setdefault(f.get("category"), []).append(f)
    groups = []
    all_cells = []
    group_items = []
    for cat, label in ALWAYS_CATEGORIES:
        cat_files = by_cat.get(cat, [])
        tokens = sum(f.get("tokens_est", 0) for f in cat_files)
        if tokens <= 0:
            continue
        group_items.append({"size": tokens, "category": cat, "label": label, "file_count": len(cat_files)})
    group_rects = squarify(sorted(group_items, key=lambda g: (-g["size"], g["category"])),
                            0.0, 0.0, canvas_w, canvas_h)
    color_by_cat = {cat: CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)]
                     for i, (cat, _) in enumerate(ALWAYS_CATEGORIES)}
    for g in group_rects:
        groups.append(g)
        cat_files = sorted(by_cat.get(g["category"], []), key=lambda f: (-f.get("tokens_est", 0), f["path"]))
        cell_items = [{"size": f.get("tokens_est", 0), "path": f["path"], "node_key": _al_node_key(f["path"])}
                      for f in cat_files]
        cells = squarify(cell_items, float(g["x"]), float(g["y"]), float(g["w"]), float(g["h"]))
        for c in cells:
            c["fill"] = color_by_cat[g["category"]]
            c["category"] = g["category"]
        all_cells.extend(cells)
    return {"groups": groups, "cells": all_cells,
            "canvas_w": canvas_w, "canvas_h": canvas_h}


def _on_demand_treemap(doc, canvas_w=960.0, canvas_h=420.0):
    on_demand = doc.get("on_demand", {}) or {}
    items_by_group = {g: [] for g, _ in ON_DEMAND_GROUPS}
    for s in on_demand.get("skills", []) or []:
        items_by_group["skill"].append({"size": s.get("words", 0), "path": s.get("name", ""),
                                         "node_key": _od_node_key(s.get("name", ""))})
    for b in on_demand.get("skill_internal_bodies", []) or []:
        kind = b.get("kind")
        if kind in items_by_group:
            items_by_group[kind].append({"size": b.get("words", 0), "path": b.get("path", ""),
                                          "node_key": _od_node_key(b.get("path", ""))})
    for m in on_demand.get("memory_bodies", []) or []:
        items_by_group["memory"].append({"size": m.get("words", 0), "path": m.get("path", ""),
                                          "node_key": _od_node_key(m.get("path", ""))})
    group_items = []
    for g, label in ON_DEMAND_GROUPS:
        total = sum(i["size"] for i in items_by_group[g])
        if total <= 0:
            continue
        group_items.append({"size": total, "category": g, "label": label, "file_count": len(items_by_group[g])})
    group_rects = squarify(sorted(group_items, key=lambda g: (-g["size"], g["category"])),
                            0.0, 0.0, canvas_w, canvas_h)
    color_by_group = {g: CATEGORICAL_PALETTE[i % len(CATEGORICAL_PALETTE)]
                       for i, (g, _) in enumerate(ON_DEMAND_GROUPS)}
    all_cells = []
    for g in group_rects:
        cell_items = sorted(items_by_group[g["category"]], key=lambda i: (-i["size"], i["path"]))
        cells = squarify(cell_items, float(g["x"]), float(g["y"]), float(g["w"]), float(g["h"]))
        for c in cells:
            c["fill"] = color_by_group[g["category"]]
            c["category"] = g["category"]
        all_cells.extend(cells)
    return {"groups": group_rects, "cells": all_cells, "canvas_w": canvas_w, "canvas_h": canvas_h}


def build_contextweight_model(doc):
    """(a) Context-weight: TWO treemaps (Codex F3) — always-loaded (by category, sized
    by tokens_est) and on-demand (skills/phases/prompts/agents/memory, sized by
    words). Both squarified (§9-R A); every cell carries `node_key` for the friction
    merge."""
    always_loaded = doc.get("always_loaded", {}) or {}
    return {
        "always": _tokens_treemap(always_loaded.get("files", []) or []),
        "on_demand": _on_demand_treemap(doc),
        "totals": always_loaded.get("totals", {"words": 0, "tokens_est": 0, "file_count": 0}),
    }


def build_bipartite_model(doc):
    """(b) Hook wiring: registration/reachability STATUS (Codex F4) — direct edges only
    are drawn; dispatcher reachability is a badge, never a fabricated edge."""
    hooks = (doc.get("enforcement", {}) or {}).get("hooks", {}) or {}
    registered = hooks.get("registered", []) or []
    orphan_registrations = hooks.get("orphan_registrations", []) or []
    scripts_on_disk = hooks.get("scripts_on_disk", []) or []
    orphan_scripts = hooks.get("orphan_scripts", []) or []

    left = sorted(
        [{"node_key": _hook_node_key(r["script"]), "command": r.get("command", ""),
          "script": r.get("script", "")} for r in registered],
        key=lambda n: n["node_key"])
    left_orphans = sorted(
        [{"node_key": f"hook_orphan:{r.get('script', '')}", "script": r.get("script", ""),
          "target_status": r.get("target_status", "missing")} for r in orphan_registrations],
        key=lambda n: n["node_key"])
    right = sorted(
        [{"node_key": _hook_node_key(s["name"]), "name": s.get("name", ""),
          "registered_via": s.get("registered_via", "none"),
          "is_symlink": bool(s.get("is_symlink", False))} for s in scripts_on_disk],
        key=lambda n: n["node_key"])
    edges = sorted(
        [{"from": _hook_node_key(r["script"]), "to": _hook_node_key(r["script"])}
         for r in registered if r.get("registered_via") == "direct"],
        key=lambda e: e["from"])
    return {"left": left, "left_orphans": left_orphans, "right": right, "edges": edges,
            "orphan_script_count": len(orphan_scripts)}


def build_trend_model(dated_docs):
    """(c) Trend: 8 headline series across ALL loaded sidecars in `--out-dir`
    (filtered to the SELECTED sidecar's `root`, Codex F13)."""
    dates = [d for d, _ in dated_docs]
    series = [{"key": key, "label": label, "polarity": polarity,
               "values": [doc.get("headline", {}).get(key, 0) for _, doc in dated_docs]}
              for key, label, polarity in HEADLINE_KEYS]
    return {"dates": dates, "series": series, "first_run": len(dated_docs) <= 1}


def build_dupweb_model(doc):
    """(d) Duplication web: dedup node set (lex-sorted) + edges in pair order +
    phantom_refs table."""
    dup = doc.get("duplication", {}) or {}
    pairs = dup.get("pairs", []) or []
    node_paths = sorted({p for pair in pairs for p in (pair["a"], pair["b"])})
    nodes = [{"node_key": _dup_node_key(p), "path": p} for p in node_paths]
    edges = [{"a": _dup_node_key(pair["a"]), "b": _dup_node_key(pair["b"]),
              "score": pair.get("score", 0.0), "shared_sample": pair.get("shared_sample", "")}
             for pair in pairs]
    return {"nodes": nodes, "edges": edges, "threshold": dup.get("threshold", 0.6),
            "metric": dup.get("metric", "containment"), "phantom_refs": doc.get("phantom_refs", []) or []}


def build_civc_model(synth):
    """CIVC 6x6 grid. Absent synthesis -> graceful empty-state (`available=False`,
    §6). A malformed cell set never crashes: missing cells fall back to 'empty'."""
    if synth is None:
        return {"available": False, "cells": []}
    by_key = {}
    for c in synth.get("civc", []) or []:
        if isinstance(c, dict) and c.get("verb") in VERBS and c.get("surface") in SURFACES:
            by_key[(c["verb"], c["surface"])] = c
    cells = []
    for verb in VERBS:
        for surface in SURFACES:
            c = by_key.get((verb, surface), {})
            cells.append({"verb": verb, "surface": surface,
                           "verdict": c.get("verdict", "empty"),
                           "evidence": c.get("evidence"), "note": c.get("note", "")})
    return {"available": True, "cells": cells}


def build_dragcandidate_model(synth):
    """Drag-candidate table. Absent synthesis -> graceful empty-state."""
    if synth is None:
        return {"available": False, "rows": []}
    rows = sorted((r for r in synth.get("drag_candidates", []) or [] if isinstance(r, dict)),
                  key=lambda r: r.get("n", 0))
    return {"available": True, "rows": rows}


# ---------------------------------------------------------------------- gauges / overview
# Deterministic severity bands (tunable constants). Ordered tuples: (upper_inclusive|None,
# band_label, semantic). semantic ∈ {"good","warn","bad","neutral"} -> stripe class.
GAUGE_BANDS = {
    "always_loaded_words":        ((8000, "LEAN", "good"), (20000, "MODERATE", "warn"), (None, "HEAVY", "bad")),
    "always_loaded_tokens_est":   ((6000, "LEAN", "good"), (15000, "MODERATE", "warn"), (None, "HEAVY", "bad")),
    "instruction_files_over_200": ((0, "COMPLIANT", "good"), (4, "FLAGGED", "warn"), (None, "OVER", "bad")),
    "duplicate_pair_count":       ((0, "CLEAN", "good"), (3, "SOME", "warn"), (None, "MANY", "bad")),
    "phantom_ref_count":          ((0, "CLEAN", "good"), (None, "BROKEN", "bad")),
    "friction_total":             ((0, "CLEAN", "good"), (5, "LOW", "warn"), (None, "HIGH", "bad")),
}


def _gauge_band(key, value):
    """(band_label, semantic) for a gauge value. Unknown key -> neutral (informational,
    no severity). First band whose `upper` is None or value <= upper wins."""
    bands = GAUGE_BANDS.get(key)
    if not bands:
        return ("", "neutral")
    for upper, label, semantic in bands:
        if upper is None or value <= upper:
            return (label, semantic)
    return bands[-1][1], bands[-1][2]


def friction_total(joined, codex_aggregate):
    """AM-1 gauge value: total friction events across the 4 streams = joined telemetry
    records (decisions/metrics/interventions) + codex runs. This is a JOIN-EVENT count:
    a basename-ambiguous record that heats N nodes counts N. That is INTENTIONAL
    (DECISION 6) — this same value is rendered as the Friction view's headline total
    (Task 8), so the header gauge and the Friction view show ONE consistent friction
    number rather than two disagreeing totals. Do NOT dedupe to unique source records."""
    return sum(len(v) for v in joined.values()) + codex_aggregate.get("runs", 0)


def build_overview_model(models, headline, phantom_ref_count, friction_total_value):
    """A3/AM-2 digest — pure aggregation over already-built models. No new data derived:
    roadmap gaps = empty civc cells; weight tax = top always-loaded files by size;
    hygiene = headline counts; drag = synthesis rows; friction hero = count + band + top drag."""
    civc = models["civc"]
    roadmap_gaps = ([(c["verb"], c["surface"]) for c in civc["cells"] if c["verdict"] == "empty"]
                    if civc.get("available") else [])
    always_cells = models["context_weight"]["always"]["cells"]
    weight_tax = sorted(always_cells, key=lambda c: (-c.get("size", 0), c.get("path", "")))[:3]
    drag = models["drag"]
    drag_rows = drag["rows"] if drag.get("available") else []
    band_label, band_semantic = _gauge_band("friction_total", friction_total_value)
    return {
        "roadmap_gaps": roadmap_gaps,
        "weight_tax": weight_tax,
        "hygiene": {"over_cap": headline.get("instruction_files_over_200", 0),
                    "dup_pairs": headline.get("duplicate_pair_count", 0),
                    "phantom_refs": phantom_ref_count},
        "drag_candidates": drag_rows,
        "friction": {"count": friction_total_value, "band": band_label,
                     "semantic": band_semantic, "top_drag": drag_rows[:3]},
    }


def build_copy_payloads(date, models, friction, doc):
    """A8: per-view clean-markdown copy payload. Pure function of inputs (deterministic).
    Rendered into inert <script type='application/json'> islands; read via textContent +
    JSON.parse at click time."""
    heat, joined, footer, codex_aggregate = friction
    civc = models["civc"]
    # --- coverage: markdown table ---
    header = "| verb \\ surface | " + " | ".join(SURFACES) + " |"
    divider = "|" + "---|" * (len(SURFACES) + 1)
    by_verb = {}
    for c in civc["cells"]:
        by_verb.setdefault(c["verb"], {})[c["surface"]] = c["verdict"]
    cov_rows = ["| " + verb + " | "
                + " | ".join(by_verb.get(verb, {}).get(s, "empty") for s in SURFACES) + " |"
                for verb in VERBS]
    coverage_md = "\n".join([header, divider] + cov_rows) if civc.get("available") \
        else "_Coverage Matrix unavailable (no synthesis sidecar)._"
    # --- friction: sentences ---
    friction_md = "\n".join(f"- {_friction_sentence(f, codex_aggregate)}" for f in footer) \
        or "_Friction overlay disabled._"
    friction_md += "\n\n" + _codex_sentence(codex_aggregate)
    # --- weight: top files per always-loaded category ---
    weight_lines = [f"- `{c['path']}` — {c.get('size', 0)} tokens"
                    for c in sorted(models["context_weight"]["always"]["cells"],
                                    key=lambda c: (-c.get("size", 0), c.get("path", "")))[:10]]
    weight_md = "\n".join(weight_lines) or "_No always-loaded files._"
    # --- hygiene: dup pairs + phantom refs ---
    dup = models["dupweb"]
    hyg_lines = [f"- dup: `{e['a']}` <-> `{e['b']}` ({_fmt_float(e['score'])})" for e in dup["edges"]]
    hyg_lines += [f"- phantom: `{r.get('source','')}` -> `{r.get('ref','')}` ({r.get('kind','')})"
                  for r in dup["phantom_refs"]]
    hygiene_md = "\n".join(hyg_lines) or "_No hygiene flags._"
    # --- overview: digest summary ---
    over = build_overview_model(models, doc.get("headline", {}) or {},
                                len(doc.get("phantom_refs", []) or []),
                                friction_total(joined, codex_aggregate))
    overview_md = (f"# harness-map {date}\n\n"
                   f"- roadmap gaps: {len(over['roadmap_gaps'])}\n"
                   f"- friction events: {over['friction']['count']} ({over['friction']['band']})\n"
                   f"- over-cap files: {over['hygiene']['over_cap']}, "
                   f"dup pairs: {over['hygiene']['dup_pairs']}, "
                   f"phantom refs: {over['hygiene']['phantom_refs']}")
    return {"overview": overview_md, "coverage": coverage_md, "weight": weight_md,
            "friction": friction_md, "hygiene": hygiene_md}


# ---------------------------------------------------------------------------- node index
def _collect_node_keys(models):
    keys = []
    cw = models["context_weight"]
    for tree in (cw["always"], cw["on_demand"]):
        keys.extend(c["node_key"] for c in tree["cells"])
    bp = models["bipartite"]
    keys.extend(n["node_key"] for n in bp["left"])
    keys.extend(n["node_key"] for n in bp["right"])
    keys.extend(n["node_key"] for n in models["dupweb"]["nodes"])
    return keys


def build_node_index(models):
    """basename(lower) -> sorted [node_key, ...] across every rendered view, used by
    the friction join so a joined basename heats EVERY matching node (§1.3), never
    first-match-wins."""
    index = {}
    for key in _collect_node_keys(models):
        b = _basename_of_node_key(key)
        index.setdefault(b, set()).add(key)
    return {b: sorted(v) for b, v in index.items()}


# ------------------------------------------------------------------------------ friction
def extract_basename(ref):
    """Shared basename normalizer (§2.3) for loose telemetry text refs."""
    token = ref.split(":")[0].strip()
    token = re.split(r"\s+--", token)[0].strip()
    return (Path(token).name if ("/" in token or token.endswith((".py", ".sh", ".md")))
            else token).lower()


def _split_component(component):
    segments = []
    for part in component.split(" + "):
        segments.extend(p.strip() for p in part.split(", ") if p.strip())
    return segments


def read_jsonl(path, max_bytes=5_000_000, max_lines=20_000):
    """(records, malformed_count, lines_nonblank) — never raises. `path` must already
    be known to exist and be a regular file (caller's job, Codex F11). Disclosed caps
    guard against a FIFO/unbounded stream hanging the renderer."""
    records, malformed, nonblank = [], 0, 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= max_lines:
                    break
                line = line.strip()
                if not line:
                    continue
                nonblank += 1
                if len(line) > 200_000:
                    malformed += 1
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    malformed += 1
                    continue
                if not isinstance(rec, dict):
                    malformed += 1
                    continue
                records.append(rec)
    except OSError:
        return [], 0, 0
    return records, malformed, nonblank


def _record_date(rec):
    for key in ("date", "ts", "verified_date"):
        val = rec.get(key)
        if isinstance(val, str):
            m = DATE_RE.match(val)
            if m:
                return m.group(0)
    return None


def join_decisions(records, node_index, current_date):
    heat, joined = {}, {}
    segments_total = segments_joined = segments_ambiguous = dated_in_window = 0
    for rec in records:
        d = _record_date(rec)
        if d is not None:
            if d > current_date:
                continue
            dated_in_window += 1
        component = rec.get("component")
        if not isinstance(component, str) or not component.strip():
            continue
        for seg in _split_component(component):
            segments_total += 1
            keys = node_index.get(extract_basename(seg))
            if not keys:
                continue
            segments_joined += 1
            if len(keys) > 1:
                segments_ambiguous += 1
            for k in keys:
                heat[k] = heat.get(k, 0) + 1
                joined.setdefault(k, []).append(rec)
    return heat, joined, {"segments_total": segments_total, "segments_joined": segments_joined,
                           "segments_ambiguous": segments_ambiguous, "records_dated_in_window": dated_in_window}


def _metrics_eligible(rec):
    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    return _num(rec.get("rework_iterations")) > 0 or _num(rec.get("audit_rounds")) > 1 \
        or _num(rec.get("findings_total")) > 0


def join_metrics(records, node_index, current_date):
    heat, joined = {}, {}
    records_eligible = records_aggregate_only = dated_in_window = 0
    base_keys = node_index.get("coding-team", [])
    for rec in records:
        d = _record_date(rec)
        if d is not None:
            if d > current_date:
                continue
            dated_in_window += 1
        if not _metrics_eligible(rec):
            continue
        records_eligible += 1
        attributed = False
        for k in base_keys:
            heat[k] = heat.get(k, 0) + 1
            joined.setdefault(k, []).append(rec)
            attributed = True
        phases = rec.get("phases_used")
        if isinstance(phases, list):
            for p in phases:
                fname = PHASE_ALIAS.get(p) if isinstance(p, str) else None
                for k in (node_index.get(fname.lower(), []) if fname else []):
                    heat[k] = heat.get(k, 0) + 1
                    joined.setdefault(k, []).append(rec)
                    attributed = True
        agents = rec.get("agents_dispatched")
        if isinstance(agents, dict):
            for a, count in agents.items():
                if not (isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0):
                    continue
                fname = AGENT_ALIAS.get(a) if isinstance(a, str) else None
                for k in (node_index.get(fname.lower(), []) if fname else []):
                    heat[k] = heat.get(k, 0) + 1
                    joined.setdefault(k, []).append(rec)
                    attributed = True
        if not attributed:
            records_aggregate_only += 1
    return heat, joined, {"records_eligible": records_eligible,
                           "records_aggregate_only": records_aggregate_only,
                           "records_dated_in_window": dated_in_window}


def join_interventions(records, node_index, current_date):
    heat, joined = {}, {}
    dated_in_window = 0
    for rec in records:
        d = _record_date(rec)
        if d is not None:
            if d > current_date:
                continue
            dated_in_window += 1
        mem = rec.get("memory_file")
        if not isinstance(mem, str) or not mem.strip():
            continue
        keys = node_index.get(extract_basename(mem))
        if not keys:
            continue
        for k in keys:
            heat[k] = heat.get(k, 0) + 1
            joined.setdefault(k, []).append(rec)
    return heat, joined, {"records_dated_in_window": dated_in_window}


def aggregate_codex(records, current_date):
    """Side-panel-only aggregate (no node join — `target` names a plan file, not a
    map node, §2.2)."""
    by_mode, by_verdict = {}, {}
    revise_rounds = []
    runs = 0
    for rec in records:
        d = _record_date(rec) or (rec.get("ts", "")[:10] if isinstance(rec.get("ts"), str) else None)
        if d and DATE_RE.match(d) and d > current_date:
            continue
        runs += 1
        mode = rec.get("mode")
        verdict = rec.get("verdict")
        if isinstance(mode, str):
            by_mode[mode] = by_mode.get(mode, 0) + 1
        if isinstance(verdict, str):
            by_verdict[verdict] = by_verdict.get(verdict, 0) + 1
            if verdict == "REVISE" and isinstance(rec.get("round"), (int, float)):
                revise_rounds.append(rec["round"])
    return {"runs": runs, "by_mode": dict(sorted(by_mode.items())),
            "by_verdict": dict(sorted(by_verdict.items())),
            "max_revise_round": max(revise_rounds) if revise_rounds else 0}


def _friction_sentence(f, codex_aggregate):
    """One human-readable sentence per stream row, built ONLY from the same counters
    already computed by the join functions (never new numbers) — the raw dict stays
    available as a secondary/collapsed detail; this is the headline (demo-readability
    follow-up)."""
    label = STREAM_LABELS[f["stream"]]
    status = f["status"]
    if status == "disabled":
        return f"{label} — friction overlay disabled for this render."
    if status == "absent":
        return f"{label} — stream not provided."
    if status == "inaccessible":
        return f"{label} — telemetry file exists but is not a readable file."
    if f["stream"] == "decisions":
        total, joined = f.get("segments_total", 0), f.get("segments_joined", 0)
        window, ambiguous = f.get("records_dated_in_window", 0), f.get("segments_ambiguous", 0)
        return (f"{label} — {joined} of {total} component references matched to map "
                f"components ({window} records in window; {ambiguous} ambiguous).")
    if f["stream"] == "metrics":
        eligible, agg_only = f.get("records_eligible", 0), f.get("records_aggregate_only", 0)
        attributed, invalid = eligible - agg_only, f.get("records_invalid", 0)
        return (f"{label} — {attributed} of {eligible} eligible pipeline records attributed "
                f"to phase/agent components ({agg_only} aggregate-only); {invalid} invalid lines.")
    if f["stream"] == "interventions":
        parsed, window = f.get("records_parsed", 0), f.get("records_dated_in_window", 0)
        return f"{label} — {parsed} records parsed, {window} in window."
    # codex — aggregate-only, no node join (§2.2)
    runs = codex_aggregate["runs"]
    return f"{label} — {runs} records, aggregate-only (target is a plan filename, not a map component)."


def _codex_sentence(agg):
    """English summary of `codex_aggregate` — derived entirely from its own dict,
    never hardcoded numbers."""
    runs = agg["runs"]
    if runs == 0:
        return "No Codex reviews recorded in this window."
    mode_bits = ", ".join(f"{v} on {k}s" for k, v in agg["by_mode"].items())
    mode_clause = f" — {mode_bits}" if mode_bits else ""
    verdict_bits = ", ".join(f"{v} {CODEX_VERDICT_LABELS.get(k, k.lower())}"
                              for k, v in agg["by_verdict"].items())
    max_round = agg["max_revise_round"]
    revise_clause = f" (up to {max_round} revise round{'s' if max_round != 1 else ''})" if max_round else ""
    verdict_clause = f" Verdicts: {verdict_bits}{revise_clause}." if verdict_bits else ""
    plural = "s" if runs != 1 else ""
    return f"{runs} Codex review{plural}{mode_clause}.{verdict_clause}"


def _stream_status(path, disabled):
    if disabled:
        return "disabled"
    if path is None:
        return "absent"
    p = Path(path)
    try:
        if not p.exists():
            return "absent"
        if not p.is_file():
            return "inaccessible"
    except OSError:
        return "inaccessible"
    return "loaded"


def _display_path(path):
    if path is None:
        return "(not provided)"
    try:
        home = str(Path.home())
        s = str(path)
        return "~" + s[len(home):] if s.startswith(home) else s
    except OSError:
        return str(path)


def build_friction_overlay(doc, streams, node_index, current_date, disabled):
    """Joins the four optional streams onto `node_index` (data join only, never a
    judgment, §2.2). Returns (heat, joined_records, sources_footer, codex_aggregate)."""
    heat, joined = {}, {}
    footer = []

    def _merge(h, j):
        for k, v in h.items():
            heat[k] = heat.get(k, 0) + v
        for k, recs in j.items():
            joined.setdefault(k, []).extend(recs)

    codex_aggregate = {"runs": 0, "by_mode": {}, "by_verdict": {}, "max_revise_round": 0}

    for stream in STREAM_ORDER:
        path = streams.get(stream)
        status = _stream_status(path, disabled)
        counters = {}
        if status == "loaded":
            records, malformed, nonblank = read_jsonl(path)
            counters["lines_nonblank"] = nonblank
            counters["records_parsed"] = len(records)
            counters["records_invalid"] = malformed
            if stream == "decisions":
                h, j, extra = join_decisions(records, node_index, current_date)
                _merge(h, j)
                counters.update(extra)
            elif stream == "metrics":
                h, j, extra = join_metrics(records, node_index, current_date)
                _merge(h, j)
                counters.update(extra)
            elif stream == "interventions":
                h, j, extra = join_interventions(records, node_index, current_date)
                _merge(h, j)
                counters.update(extra)
            elif stream == "codex":
                codex_aggregate = aggregate_codex(records, current_date)
                counters["records_aggregate_only"] = codex_aggregate["runs"]
        footer.append({"stream": stream, "status": status, "path_display": _display_path(path), **counters})
    return heat, joined, footer, codex_aggregate


# ----------------------------------------------------------------------------- write safety
def _resolves_inside_root(candidate, root, root_stat):
    """Reused from collector.py's guard (§3.3), inverted use here: harness-map's
    write target must NOT resolve inside the harness root."""
    if candidate == root or root in candidate.parents:
        return True
    for anc in (candidate, *candidate.parents):
        try:
            st = os.stat(anc)
        except OSError:
            continue
        if os.path.samestat(st, root_stat):
            return True
    return False


def write_html_safely(out_path, text, harness_root):
    """Hard-link-safe write (Codex F1): mkstemp in the SAME dir + fsync + os.replace,
    reusing the collector's pattern verbatim — never `write_text()`, which would
    truncate a hard-linked inode also linked under the harness root."""
    out_path = Path(out_path)
    if harness_root is not None:
        try:
            root_stat = os.stat(harness_root)
            if _resolves_inside_root(out_path.resolve(), Path(harness_root).resolve(), root_stat):
                raise SystemExit(f"fatal: refusing to write inside harness root: {out_path}")
        except OSError:
            pass
    tmp_name = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(out_path.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, out_path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# ---------------------------------------------------------------------------------- CSS/JS
STATIC_STYLE = """
:root{--bg:#0b0f14;--panel:#121824;--text:#e6edf3;--muted:#8b98a5;--border:#2a3341;--accent:#6366f1;--sem-covered:#009e73;--sem-thin:#e69f00;--sem-empty:#d1242f;--mono:ui-monospace,SFMono-Regular,Menlo,monospace}
:root[data-theme="dark"]{--bg:#0b0f14;--panel:#121824;--text:#e6edf3;--muted:#8b98a5;--border:#2a3341;--accent:#6366f1;--sem-covered:#009e73;--sem-thin:#e69f00;--sem-empty:#d1242f}
@media (prefers-color-scheme: light){:root{--bg:#f6f8fa;--panel:#ffffff;--text:#1b1f24;--muted:#57606a;--border:#d0d7de;--accent:#4f46e5;--sem-covered:#036a52;--sem-thin:#9a6700;--sem-empty:#b3261e}}
:root[data-theme="light"]{--bg:#f6f8fa;--panel:#ffffff;--text:#1b1f24;--muted:#57606a;--border:#d0d7de;--accent:#4f46e5;--sem-covered:#036a52;--sem-thin:#9a6700;--sem-empty:#b3261e}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:0}
header{padding:16px 20px;border-bottom:1px solid var(--border)}
h1{font-size:1.25rem;margin:0 0 4px 0}
.subtitle{color:var(--muted);font-size:0.85rem}
.tiles,.gauges{display:flex;flex-wrap:wrap;gap:10px;padding:12px 20px}
.tile,.gauge{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px;min-width:150px}
.tile .v,.gauge .v{font-size:1.4rem;font-weight:600;font-family:var(--mono);font-variant-numeric:tabular-nums}
.tile .l,.gauge .l{color:var(--muted);font-size:0.75rem}
.gauge{border-left:4px solid var(--border)}
.gauge-good{border-left-color:var(--sem-covered)}
.gauge-warn{border-left-color:var(--sem-thin)}
.gauge-bad{border-left-color:var(--sem-empty)}
.gauge-neutral{border-left-color:var(--border)}
.gauge .band{color:var(--muted);font-size:0.72rem}
.gauge .delta{font-size:0.75rem;font-weight:600}
.warn-badge{background:var(--sem-empty);color:#fff;border-radius:6px;padding:2px 8px;font-size:0.75rem;text-decoration:none}
.controls{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--border);padding:8px 20px;display:flex;gap:8px;flex-wrap:wrap;z-index:5}
.view-switch,.seg{display:inline-flex;gap:6px;flex-wrap:wrap;border:1px solid var(--border);border-radius:6px;padding:2px}
button.action-btn,button.view-btn,button.seg-btn,button.copy-btn{background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 12px;cursor:pointer;font-size:0.85rem}
button.view-btn[aria-selected="true"]{border-color:var(--accent);color:var(--accent)}
button[aria-pressed="true"]{border-color:var(--accent);color:var(--accent)}
button.seg-btn[aria-pressed="true"]{border-color:var(--accent);color:var(--accent);background:var(--bg)}
main{padding:16px 20px}
.view[hidden]{display:none}
.view-toolbar{display:flex;justify-content:flex-end;margin-bottom:8px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:14px}
.digest{color:var(--muted);font-size:0.85rem;margin:0 0 10px 0}
.hero-friction{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:14px}
.inspector{position:sticky;top:52px;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:12px;max-height:70vh;overflow-y:auto}
.empty-state{color:var(--muted);font-style:italic}
table{border-collapse:collapse;width:100%;font-size:0.85rem}
th,td{border:1px solid var(--border);padding:6px 8px;text-align:left;font-family:var(--mono);font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:600}
.badge{display:inline-block;border-radius:5px;padding:1px 6px;font-size:0.72rem;border:1px solid var(--border)}
.badge.orphan{border-color:var(--sem-empty);color:var(--sem-empty)}
.badge.direct{border-color:var(--sem-covered);color:var(--sem-covered)}
.badge.dispatcher{border-color:var(--accent);color:var(--accent)}
.cell-label{font-size:12px;fill:var(--text);font-family:var(--mono);font-variant-numeric:tabular-nums}
.legend-swatch{display:inline-block;width:10px;height:10px;margin-right:4px;border-radius:2px;vertical-align:middle}
.mini-grid{display:flex;flex-wrap:wrap;gap:2px}
.mini-cell{width:10px;height:10px;border-radius:2px;background:var(--border);cursor:pointer}
.mini-cell.sel{outline:2px solid var(--accent)}
.mini-cell.verdict-covered{background:var(--sem-covered)}
.mini-cell.verdict-thin{background:var(--sem-thin)}
.mini-cell.verdict-empty{background:var(--sem-empty)}
.mini-cell:focus-visible{outline:2px solid var(--accent)}
.overview-grid{display:grid;grid-template-columns:1fr 340px;gap:14px;align-items:start}
.hero-friction-good{border-left:4px solid var(--sem-covered)}
.hero-friction-warn{border-left:4px solid var(--sem-thin)}
.hero-friction-bad{border-left:4px solid var(--sem-empty)}
.hero-friction-neutral{border-left:4px solid var(--border)}
.hero-friction .count{font-size:1.2rem;font-weight:600;font-family:var(--mono);font-variant-numeric:tabular-nums;margin:4px 0}
.digest-group{margin-bottom:10px}
.digest-group h3{font-size:0.82rem;margin:0 0 4px 0;color:var(--muted)}
.digest-group ul{margin:0;padding:0;list-style:none}
.digest-group li{font-size:0.82rem;margin:2px 0;display:flex;align-items:center;gap:6px}
.sev-dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:0 0 auto}
.sev-dot.sev-good{background:var(--sem-covered)}
.sev-dot.sev-warn{background:var(--sem-thin)}
.sev-dot.sev-bad{background:var(--sem-empty)}
.stream-cards{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 14px 0}
.stream-card{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:10px 14px;flex:1 1 200px}
.stream-card .count{font-size:1.3rem;font-weight:600;font-family:var(--mono);font-variant-numeric:tabular-nums;margin:0 0 4px 0}
.stream-card h3{margin:0 0 4px 0;font-size:0.9rem}
.stream-card p{margin:0 0 6px 0;font-size:0.82rem;color:var(--muted)}
.stream-card .source{font-size:0.75rem;color:var(--muted);font-family:var(--mono)}
.sev-dot.sev-neutral{background:var(--muted)}
svg text{font-family:inherit}
footer.sources{border-top:1px solid var(--border);padding:10px 20px;color:var(--muted);font-size:0.78rem}
.overflow-x{overflow-x:auto}
@media (prefers-reduced-motion: no-preference){button{transition:border-color .15s}}
.cell-rect{stroke:var(--border);stroke-width:0.5}
body.friction-on .heatable:not(.fh1):not(.fh2):not(.fh3):not(.fh4){opacity:0.25}
body.friction-on .fh1,body.friction-on .fh2,body.friction-on .fh3,body.friction-on .fh4{opacity:1}
.friction-badge{display:none;font-size:10px;font-weight:700;fill:#fff;paint-order:stroke;stroke:#000;stroke-width:2}
body.friction-on .friction-badge{display:inline}
#friction-toggle[aria-pressed="true"]{background:var(--sem-empty);border-color:var(--sem-empty);color:#fff;font-weight:600}
.friction-legend{display:flex;align-items:center;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:0.75rem;padding:4px 20px 0}
.legend-entry{display:inline-flex;align-items:center;gap:4px}
.legend-swatch.fh0{background:var(--panel);border:1px solid var(--border)}
.legend-note{color:var(--muted)}
.friction-explainer{color:var(--muted);font-size:0.85rem;margin:0 0 10px 0}
.friction-row-detail{display:block;color:var(--muted);font-size:0.78rem;margin-top:2px}
details{color:var(--muted)}
details > summary{cursor:pointer;color:var(--accent)}
.civc-legend{color:var(--muted);font-size:0.8rem;margin:0 0 10px 0}
td.verdict-covered{background:rgba(0,158,115,0.18)}
td.verdict-thin{background:rgba(230,159,0,0.15)}
td.verdict-empty{color:var(--muted);border:2px dashed var(--sem-empty);background:repeating-linear-gradient(135deg,rgba(209,36,47,0.12) 0,rgba(209,36,47,0.12) 4px,transparent 4px,transparent 8px)}
.badge.verdict-thin{border-color:var(--sem-thin);color:var(--sem-thin)}
.badge.verdict-covered{border-color:var(--sem-covered);color:var(--sem-covered)}
.coverage-grid{display:grid;grid-template-columns:1fr 320px;gap:14px;align-items:start}
.matrix-cell{cursor:pointer}
.matrix-cell.sel{outline:2px solid var(--accent);outline-offset:-2px}
.matrix-cell:focus-visible{outline:2px solid var(--accent)}
.inspector-panel .surface-tag{color:var(--muted);font-size:0.72rem;text-transform:uppercase;margin:0}
.inspector-panel .verb-tag{font-weight:600;margin:2px 0 6px 0}
.inspector-panel .evidence{margin:6px 0}
.seg .seg-btn{border:none}
.treemap-panel{display:block}
.ladder-panel{display:none}
.mode-ladder .treemap-panel{display:none}
.mode-ladder .ladder-panel{display:block}
.copy-btn{font-size:0.78rem;padding:4px 10px}
.visually-hidden{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.pill{display:inline-block;border-radius:5px;padding:1px 6px;font-size:0.72rem;border:1px solid var(--sem-thin);color:var(--sem-thin)}
.pill-critical{border-color:var(--sem-empty);color:var(--sem-empty);font-weight:600}
.hygiene-unchecked{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:600}
.warn-count{font-weight:600;color:var(--sem-empty)}
"""
_HEAT_CSS = "".join(
    f"body.friction-on .fh{i}{{stroke:{color};stroke-width:4}}"
    f".legend-swatch.fh{i}{{background:{color}}}"
    for i, color in enumerate(HEAT_RAMP, start=1)
)
STATIC_STYLE = STATIC_STYLE + _HEAT_CSS

STATIC_SCRIPT = """
(function(){
  var views = document.querySelectorAll('.view');
  var vbtns = document.querySelectorAll('.view-btn');
  function activate(id){
    views.forEach(function(v){ v.hidden = (v.id !== id); });
    vbtns.forEach(function(b){ b.setAttribute('aria-selected', b.dataset.target === id ? 'true':'false'); });
  }
  vbtns.forEach(function(b){ b.addEventListener('click', function(){ activate(b.dataset.target); }); });

  // cross-view nav: any element with data-goto (+ optional data-cell-id) switches view & selects
  document.querySelectorAll('[data-goto]').forEach(function(el){
    el.addEventListener('click', function(){
      activate(el.dataset.goto);
      var cid = el.dataset.cellId;
      if (cid) { selectCell(cid); }
    });
  });

  // coverage inspector selection
  var cells = document.querySelectorAll('.matrix-cell');
  var panels = document.querySelectorAll('.inspector-panel');
  function selectCell(cid){
    cells.forEach(function(c){ c.classList.toggle('sel', c.dataset.cellId === cid); });
    panels.forEach(function(p){ p.hidden = (p.dataset.cellId !== cid); });
  }
  cells.forEach(function(c){ c.addEventListener('click', function(){ selectCell(c.dataset.cellId); }); });

  // weight mode toggle (treemap <-> ladder)
  var segRoot = document.getElementById('weight-mode');
  if (segRoot){
    segRoot.querySelectorAll('.seg-btn').forEach(function(b){
      b.addEventListener('click', function(){
        var ladder = b.dataset.mode === 'ladder';
        var panel = document.getElementById('view-weight');
        panel.classList.toggle('mode-ladder', ladder);
        segRoot.querySelectorAll('.seg-btn').forEach(function(x){
          x.setAttribute('aria-pressed', x === b ? 'true' : 'false'); });
      });
    });
  }

  // friction overlay toggle (local to weight view)
  var ov = document.getElementById('friction-toggle');
  if (ov){ ov.addEventListener('click', function(){
    var on = document.body.classList.toggle('friction-on');
    ov.setAttribute('aria-pressed', on ? 'true':'false'); }); }

  // copy buttons -> read JSON island -> clipboard, textarea fallback for file://
  document.querySelectorAll('.copy-btn').forEach(function(b){
    b.addEventListener('click', function(){
      var island = document.getElementById(b.dataset.copyTarget);
      if (!island) return;
      var md;
      try { md = JSON.parse(island.textContent); } catch (e) { return; }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(md).then(function(){ flash(b); },
          function(){ fallbackCopy(md, b); });
      } else { fallbackCopy(md, b); }
    });
  });
  function fallbackCopy(md, b){
    var ta = document.createElement('textarea');
    ta.className = 'visually-hidden'; ta.value = md;
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); flash(b); } catch (e) {}
    document.body.removeChild(ta);
  }
  function flash(b){ b.setAttribute('aria-pressed', 'true');
    setTimeout(function(){ b.setAttribute('aria-pressed', 'false'); }, 600); }

  // WCAG 2.2 AA keyboard access: Enter/Space activate the role="button" cells
  // (mini-grid data-goto cells + coverage matrix cells) that only had click handlers.
  function keyActivate(e){
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar'){
      if (e.key !== 'Enter'){ e.preventDefault(); }   // Space would scroll the page
      e.currentTarget.click();
    }
  }
  document.querySelectorAll('[data-goto], .matrix-cell').forEach(function(el){
    el.addEventListener('keydown', keyActivate);
  });

  // expand-all (print view) preserved
  var expand = document.getElementById('expand-all');
  if (expand){ expand.addEventListener('click', function(){
    views.forEach(function(v){ v.hidden = false; }); }); }

  if (views.length){ activate('view-overview'); }
})();
"""


def _csp_hash(block):
    return base64.b64encode(hashlib.sha256(block.encode("utf-8")).digest()).decode("ascii")


# --------------------------------------------------------------------------- HTML render
# A5 fidelity (finding #6): approved label auto-hide threshold — a tile smaller than
# this in either dimension can't fit a readable label, so its `<text>` is skipped.
# Module-level so the test can pin the approved value (a 56x18 regression must fail).
TREEMAP_LABEL_MIN_W = 58
TREEMAP_LABEL_MIN_H = 30
# Value-scaled fill-opacity ramp (bigger tax = more opaque). Module-level for
# determinism; MIN keeps the smallest cell visibly on-canvas rather than near-invisible.
_OPACITY_MIN = 0.35
_OPACITY_MAX = 1.0


def _scaled_opacity(size, max_size):
    """Map a cell's size onto the `[_OPACITY_MIN, _OPACITY_MAX]` ramp relative to the
    largest cell in the same tree, so opacity communicates relative weight (A5).
    `max_size<=0` (degenerate/empty tree) falls back to full opacity."""
    if max_size <= 0:
        return _OPACITY_MAX
    ratio = max(0.0, min(1.0, float(size) / float(max_size)))
    return _OPACITY_MIN + (_OPACITY_MAX - _OPACITY_MIN) * ratio


def _render_treemap_svg(tree, heat, dom_id):
    """Heat is shown two ways once the friction overlay toggle is on (never color
    alone, §UI): a CSS-class-driven stroke ramp on the cell, AND a text join-count
    badge in the corner. Both are hidden-by-default via `body.friction-on` CSS so the
    toggle button has a visible, demonstrable effect. Fill opacity is value-scaled
    (A5) via the SVG `fill-opacity` attribute — never `style=`."""
    w, h = tree["canvas_w"], tree["canvas_h"]
    max_size = max((float(c.get("size", 0)) for c in tree["cells"]), default=0.0)
    parts = [f'<svg id="{esc_html(dom_id)}" viewBox="0 0 {_fmt_float(w)} {_fmt_float(h)}" '
             f'width="100%" height="360" role="img" aria-labelledby="{esc_html(dom_id)}-title">']
    parts.append(f'<title id="{esc_html(dom_id)}-title">Context-weight treemap</title>')
    for c in tree["cells"]:
        heat_n = heat.get(c["node_key"], 0)
        bucket = min(heat_n, len(HEAT_RAMP)) if heat_n else 0
        rect_cls = f"cell-rect heatable fh{bucket}" if bucket else "cell-rect heatable"
        label = esc_html(Path(c["path"]).name)
        opacity = _fmt_float(_scaled_opacity(c.get("size", 0), max_size))
        parts.append(
            f'<rect x="{c["x"]}" y="{c["y"]}" width="{c["w"]}" height="{c["h"]}" '
            f'fill="{esc_html(c.get("fill", "#56b4e9"))}" fill-opacity="{opacity}" '
            f'class="{rect_cls}" data-node-key="{esc_html(c["node_key"])}">'
            f'<title>{esc_html(c["path"])} (friction: {heat_n})</title></rect>')
        if float(c["w"]) > TREEMAP_LABEL_MIN_W and float(c["h"]) > TREEMAP_LABEL_MIN_H:
            tx, ty = _fmt_float(float(c["x"]) + 2), _fmt_float(float(c["y"]) + 13)
            parts.append(f'<text x="{tx}" y="{ty}" class="cell-label">{label}</text>')
            if heat_n:
                bx = _fmt_float(float(c["x"]) + float(c["w"]) - 2)
                parts.append(f'<text x="{bx}" y="{ty}" text-anchor="end" '
                              f'class="friction-badge">{heat_n}</text>')
    parts.append("</svg>")
    return "".join(parts)


# Ladder layout constants (module-level for determinism — §4.6).
_LADDER_ROW_H = 22.0
_LADDER_LABEL_W = 220.0
_LADDER_BAR_MAX_W = 300.0
_LADDER_COUNT_W = 50.0


def _render_ladder_svg(tree, heat, dom_id):
    """A5 alternative representation: one horizontal bar per cell instead of nested
    rectangles — same cells as the matching treemap (`tree["cells"]`), sorted by
    descending size (path as the tie-break, for a total-order determinism key, §4.4).
    Reuses the treemap's `fhN` heat-bucket logic (AM-3) so ladder bars heat too; bar
    width is the SVG `width` attribute, value-scaled to the row's max size — never
    `style=`."""
    cells = sorted(tree["cells"], key=lambda c: (-float(c.get("size", 0)), c["path"]))
    max_size = max((float(c.get("size", 0)) for c in cells), default=0.0)
    row_h = _LADDER_ROW_H
    canvas_w = _LADDER_LABEL_W + _LADDER_BAR_MAX_W + _LADDER_COUNT_W
    canvas_h = max(row_h * len(cells), row_h)
    parts = [f'<svg id="{esc_html(dom_id)}" viewBox="0 0 {_fmt_float(canvas_w)} {_fmt_float(canvas_h)}" '
             f'width="100%" height="{_fmt_float(canvas_h)}" role="img" '
             f'aria-labelledby="{esc_html(dom_id)}-title">']
    parts.append(f'<title id="{esc_html(dom_id)}-title">Context-weight ladder</title>')
    for i, c in enumerate(cells):
        heat_n = heat.get(c["node_key"], 0)
        bucket = min(heat_n, len(HEAT_RAMP)) if heat_n else 0
        bar_cls = f"ladder-bar heatable fh{bucket}" if bucket else "ladder-bar heatable"
        size = float(c.get("size", 0))
        width = _LADDER_BAR_MAX_W * (size / max_size) if max_size > 0 else 0.0
        y = row_h * i
        label = esc_html(Path(c["path"]).name)
        text_y = _fmt_float(y + row_h - 7)
        parts.append(
            f'<text x="0" y="{text_y}" class="cell-label">{label}</text>'
            f'<rect x="{_fmt_float(_LADDER_LABEL_W)}" y="{_fmt_float(y + 3)}" '
            f'width="{_fmt_float(width)}" height="{_fmt_float(row_h - 6)}" '
            f'fill="{esc_html(c.get("fill", "#56b4e9"))}" class="{bar_cls}" '
            f'data-node-key="{esc_html(c["node_key"])}">'
            f'<title>{esc_html(c["path"])} (friction: {heat_n})</title></rect>'
            f'<text x="{_fmt_float(_LADDER_LABEL_W + _LADDER_BAR_MAX_W + 8)}" '
            f'y="{text_y}" class="cell-label">{esc_html(c.get("size", 0))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


GAUGE_SPECS = (  # (source_kind, key, label) — source_kind selects where the value comes from
    ("headline", "always_loaded_words", "Always-loaded words"),
    ("headline", "always_loaded_tokens_est", "Est. tokens / turn"),
    ("headline", "always_loaded_file_count", "Always-loaded files"),
    ("headline", "instruction_files_over_200", "Files > 200 lines"),
    ("headline", "duplicate_pair_count", "Duplicate pairs"),
    ("phantom", "phantom_ref_count", "Phantom refs"),
    ("friction", "friction_total", "Friction events"),
)


def _render_gauge(key, label, value, delta=None):
    band, semantic = _gauge_band(key, value)
    band_html = f'<div class="band">{esc_html(band)}</div>' if band else ""
    delta_html = f'<div class="delta">{esc_html(delta)}</div>' if delta else ""
    return (f'<div class="gauge gauge-{esc_html(semantic)}" data-gauge="{esc_html(key)}">'
            f'<div class="v">{esc_html(value)}</div><div class="l">{esc_html(label)}</div>'
            f'{band_html}{delta_html}</div>')


def _trend_delta(trend_model, key):
    """Polarity-aware delta arrow vs the previous sidecar, or None on first run."""
    if trend_model.get("first_run"):
        return None
    series = next((s for s in trend_model["series"] if s["key"] == key), None)
    if not series or len(series["values"]) < 2:
        return None
    cur, prev = series["values"][-1], series["values"][-2]
    if cur == prev:
        return "= 0"
    arrow = "▲" if cur > prev else "▼"   # ▲ / ▼
    return f"{arrow} {abs(cur - prev)}"


def _render_instrument_readout(headline, phantom_ref_count, friction_total_value, trend_model):
    values = {"phantom_ref_count": phantom_ref_count, "friction_total": friction_total_value}
    cards = []
    for kind, key, label in GAUGE_SPECS:
        value = values[key] if kind in ("phantom", "friction") else headline.get(key, 0)
        delta = _trend_delta(trend_model, key) if kind == "headline" else None
        cards.append(_render_gauge(key, label, value, delta))
    return f'<div class="gauges">{"".join(cards)}</div>'


def _render_copy_controls(view_id):
    """A8 per-view copy button — clicking reads the sibling JSON island's markdown
    payload via `.textContent` + `JSON.parse` (executable script never embeds the
    payload directly, §9-R C)."""
    return f'<button class="copy-btn action-btn" data-copy-target="copy-{view_id}">Copy</button>'


def _render_copy_island(view_id, payload):
    """A8 inert data island — `type="application/json"` so it is never counted as an
    executable `<script>` (CSP §9-R C); the payload is a plain markdown string."""
    return f'<script type="application/json" id="copy-{view_id}">{esc_json_script(payload)}</script>'


# Hygiene digest rows: (overview_model["hygiene"] key, GAUGE_BANDS key for severity, label).
_HYGIENE_DIGEST_SPECS = (
    ("over_cap", "instruction_files_over_200", "Files over 200 lines"),
    ("dup_pairs", "duplicate_pair_count", "Duplicate pairs"),
    ("phantom_refs", "phantom_ref_count", "Phantom refs"),
)


def _sev_dot(semantic):
    """Severity dot for a digest row — color via the same `sem-*` CSS custom
    properties as gauges/verdicts (Task-2 CSS), no inline `style=` (CSP §9-R C)."""
    return f'<span class="sev-dot sev-{esc_html(semantic)}" aria-hidden="true"></span>'


def _render_overview_digest(overview_model):
    """A3 'Needs attention' digest — severity-dotted roadmap gaps, weight tax, hygiene
    counts, and drag candidates. Pure render over `build_overview_model`'s output."""
    gaps = overview_model["roadmap_gaps"]
    if gaps:
        gaps_html = "".join(
            f'<li>{_sev_dot("warn")}{esc_html(verb)} × {esc_html(surface)}</li>'
            for verb, surface in gaps)
    else:
        gaps_html = f'<li>{_sev_dot("good")}no roadmap gaps — full coverage</li>'
    tax = overview_model["weight_tax"]
    if tax:
        tax_html = "".join(
            f'<li>{_sev_dot("neutral")}<code>{esc_html(c.get("path",""))}</code> '
            f'— {esc_html(c.get("size",0))} tokens</li>' for c in tax)
    else:
        tax_html = f'<li>{_sev_dot("good")}no always-loaded files</li>'
    hyg = overview_model["hygiene"]
    hyg_html = "".join(
        f'<li>{_sev_dot(_gauge_band(band_key, hyg[mkey])[1])}{esc_html(label)}: {esc_html(hyg[mkey])}</li>'
        for mkey, band_key, label in _HYGIENE_DIGEST_SPECS)
    drag_rows = overview_model["drag_candidates"]
    if drag_rows:
        drag_html = "".join(
            f'<li>{_sev_dot("bad" if r.get("outcome") == "probation" else "warn")}'
            f'#{esc_html(r.get("n",""))} {esc_html(r.get("surface",""))} '
            f'<span class="badge">{esc_html(r.get("outcome",""))}</span></li>'
            for r in drag_rows)
    else:
        drag_html = f'<li>{_sev_dot("good")}no drag candidates flagged</li>'
    return (
        '<div class="card"><h2>Needs attention</h2>'
        f'<div class="digest-group"><h3>Roadmap gaps ({len(gaps)})</h3><ul>{gaps_html}</ul></div>'
        f'<div class="digest-group"><h3>Weight tax (top always-loaded)</h3><ul>{tax_html}</ul></div>'
        f'<div class="digest-group"><h3>Hygiene</h3><ul>{hyg_html}</ul></div>'
        f'<div class="digest-group"><h3>Drag candidates ({len(drag_rows)})</h3><ul>{drag_html}</ul></div>'
        '</div>'
    )


def _render_friction_hero(friction_model):
    """AM-2 hero card — friction COUNT + band + top drag candidates. Color-coded via
    `hero-friction-{semantic}` (Task-2 CSS) only — no `node_key`/heat markers here
    (RESOLVED DECISION 1: friction on Overview is a count, not node-keyed heat)."""
    top_drag = friction_model["top_drag"]
    top_drag_html = "".join(
        f'<li>#{esc_html(r.get("n",""))} {esc_html(r.get("surface",""))}</li>' for r in top_drag
    ) or '<li class="empty-state">none</li>'
    return (
        f'<div class="hero-friction hero-friction-{esc_html(friction_model["semantic"])}">'
        '<h2>Friction</h2>'
        f'<p class="count">{esc_html(friction_model["count"])} events '
        f'<span class="badge">{esc_html(friction_model["band"])}</span></p>'
        f'<h3>Top drag candidates</h3><ul>{top_drag_html}</ul>'
        '</div>'
    )


def _render_overview_view(overview_model, civc):
    """A3/AM-2 — left: `.mini-grid` of verdict-colored, keyboard-operable mini-cells
    that navigate to Coverage + preselect the matching inspector cell (shared
    `[data-goto]` handler, Task 4). Right: the friction hero card + "Needs attention"
    digest. RESOLVED DECISION 1: mini-cells carry verdict color ONLY — no friction
    heat, no `node_key`, no `heatable`/`fhN` classes anywhere in this view."""
    if civc["available"]:
        cells_html = "".join(
            f'<div class="mini-cell verdict-{esc_html(c["verdict"])}" '
            f'data-goto="view-coverage" data-cell-id="{esc_html(c["verb"])}-{esc_html(c["surface"])}" '
            f'role="button" tabindex="0" '
            f'aria-label="{esc_html(c["verb"])} × {esc_html(c["surface"])}: {esc_html(c["verdict"])}"></div>'
            for c in civc["cells"])
        mini_grid = f'<div class="mini-grid">{cells_html}</div>'
    else:
        mini_grid = '<p class="empty-state">synthesis sidecar not found — Coverage Matrix unavailable this run.</p>'
    return (
        '<section id="view-overview" class="view" role="tabpanel" aria-labelledby="view-btn-overview">'
        f'<div class="view-toolbar">{_render_copy_controls("overview")}</div>'
        '<div class="overview-grid">'
        f'<div class="card"><h2>Coverage at a glance</h2>{mini_grid}</div>'
        f'<div>{_render_friction_hero(overview_model["friction"])}{_render_overview_digest(overview_model)}</div>'
        '</div>'
        '</section>'
    )


# A4: the cell selected by default on first render — also the nav TARGET clicked from
# the Overview mini-cell (Task 7). Must exist in VERBS x SURFACES.
COVERAGE_PRESELECT = ("Constrain", "memory")


def _render_coverage_view(civc):
    """Sticky-inspector rework of the former `_render_civc_drag_tab` matrix half — the
    drag half now lives in `_render_friction_view` (IA mapping). Every one of the 36
    verb x surface cells is pre-rendered as a clickable `.matrix-cell` plus a matching
    `.inspector-panel`; client-side selection (Task 4's shared script) just toggles the
    `sel` class / `hidden` attribute — no data is computed or fetched at click time."""
    if not civc["available"]:
        civc_body = '<p class="empty-state">synthesis sidecar not found — Coverage Matrix unavailable this run.</p>'
        return (
            '<section id="view-coverage" class="view" role="tabpanel" aria-labelledby="view-btn-coverage">'
            f'<div class="view-toolbar">{_render_copy_controls("coverage")}</div>'
            '<div class="card"><h2>Coverage Matrix</h2>'
            '<p class="subtitle">six verbs (what the harness does to behavior) '
            '× six surfaces (what it’s made of)</p>'
            f'{civc_body}</div></section>'
        )
    legend = (
        '<p class="civc-legend">Coverage scale (empty cells are intentional roadmap, not blanks): '
        '<span class="badge verdict-empty">empty</span> → '
        '<span class="badge verdict-thin">thin</span> → '
        '<span class="badge verdict-covered">covered</span>. '
        'Cells with a "note" expose it via a details toggle.</p>'
    )
    header = "".join(f"<th>{esc_html(s)}</th>" for s in SURFACES)
    by_key = {(c["verb"], c["surface"]): c for c in civc["cells"]}
    rows = []
    panels = []
    for verb in VERBS:
        cell_html = []
        for surface in SURFACES:
            c = by_key.get((verb, surface), {"verdict": "empty", "evidence": None, "note": ""})
            verdict = c.get("verdict", "empty")
            cell_id = f"{verb}-{surface}"
            preselect = (verb, surface) == COVERAGE_PRESELECT
            sel_token = " sel" if preselect else ""
            # Fixed attribute order — `class` FIRST, `sel` token AFTER the verdict
            # token, then `data-cell-id` — the A4 preselect test asserts this exact
            # string; do not reorder.
            cell_html.append(
                f'<td class="matrix-cell verdict-{esc_html(verdict)}{sel_token}" '
                f'data-cell-id="{esc_html(cell_id)}" role="button" tabindex="0">'
                f'<span class="badge verdict-{esc_html(verdict)}">{esc_html(verdict)}</span></td>'
            )
            evidence = c.get("evidence")
            note = c.get("note") or ""
            evidence_html = (f'<p class="evidence">{esc_html(evidence)}</p>' if evidence
                              else '<p class="evidence empty-state">no evidence recorded</p>')
            note_html = (f'<details><summary>note</summary>{esc_html(note)}</details>' if note
                         else '<p class="empty-state">no note</p>')
            hidden_attr = "" if preselect else " hidden"
            panels.append(
                f'<div class="inspector-panel" data-cell-id="{esc_html(cell_id)}"{hidden_attr}>'
                f'<p class="surface-tag">{esc_html(surface)}</p>'
                f'<p class="verb-tag">{esc_html(verb)}</p>'
                f'<span class="badge verdict-{esc_html(verdict)}">{esc_html(verdict)}</span>'
                f'{evidence_html}{note_html}</div>'
            )
        rows.append(f"<tr><th>{esc_html(verb)}</th>{''.join(cell_html)}</tr>")
    civc_body = (
        legend + '<div class="coverage-grid">'
        f'<div class="overflow-x"><table><tr><th></th>{header}</tr>'
        f'{"".join(rows)}</table></div>'
        f'<aside class="inspector">{"".join(panels)}</aside></div>'
    )
    return (
        '<section id="view-coverage" class="view" role="tabpanel" aria-labelledby="view-btn-coverage">'
        f'<div class="view-toolbar">{_render_copy_controls("coverage")}</div>'
        '<div class="card"><h2>Coverage Matrix</h2>'
        '<p class="subtitle">six verbs (what the harness does to behavior) '
        '× six surfaces (what it’s made of)</p>'
        f'{civc_body}</div></section>'
    )


def _render_weight_view(model, heat):
    """Verbatim body of the former `_render_context_weight_tab`, now also carrying the
    friction legend + overlay toggle (moved here — heat only ever lands on these two
    treemaps, RESOLVED DECISION 1) and the A5/AM-3 treemap<->ladder toggle: both
    representations are pre-rendered for both panels so the client just flips a CSS
    class (§ progressive-enhancement pattern) — no re-render at click time."""
    always_treemap = _render_treemap_svg(model["always"], heat, "treemap-always")
    always_ladder = _render_ladder_svg(model["always"], heat, "ladder-always")
    ondemand_treemap = _render_treemap_svg(model["on_demand"], heat, "treemap-ondemand")
    ondemand_ladder = _render_ladder_svg(model["on_demand"], heat, "ladder-ondemand")
    return (
        '<section id="view-weight" class="view" role="tabpanel" aria-labelledby="view-btn-weight">'
        '<div class="view-toolbar">'
        '<div class="seg" id="weight-mode" role="group" aria-label="weight representation">'
        '<button class="seg-btn" data-mode="treemap" aria-pressed="true">▦ Treemap</button>'
        '<button class="seg-btn" data-mode="ladder" aria-pressed="false">▤ Ladder</button>'
        '</div>'
        '<button class="action-btn" id="friction-toggle" aria-pressed="false">'
        'Show friction heat on treemap + ladder cells</button>'
        f'{_render_copy_controls("weight")}'
        '</div>'
        '<div class="friction-legend" id="friction-legend">'
        '<span>Friction heat, once the overlay is on:</span>'
        '<span class="legend-entry"><span class="legend-swatch fh0"></span>none</span>'
        '<span class="legend-entry"><span class="legend-swatch fh1"></span>some</span>'
        '<span class="legend-entry"><span class="legend-swatch fh4"></span>most-active</span>'
        '<span class="legend-note">every heated cell also shows a join-count '
        'badge in the corner (color is never the only signal)</span></div>'
        '<p class="subtitle">On-demand skills cost only when invoked; MEMORY.md + '
        'CLAUDE.md are the real per-turn tax — the treemap/ladder toggle shows the '
        'same weights two ways.</p>'
        '<div class="card"><h2>Always-loaded (by category, sized by est. tokens)</h2>'
        f'<div class="treemap-panel">{always_treemap}</div>'
        f'<div class="ladder-panel">{always_ladder}</div></div>'
        '<div class="card"><h2>On-demand (skills / phases / prompts / agents / memory, sized by words)</h2>'
        f'<div class="treemap-panel">{ondemand_treemap}</div>'
        f'<div class="ladder-panel">{ondemand_ladder}</div></div></section>'
    )


def _render_bipartite_body(model):
    def _row(n, side):
        badge = ""
        if side == "right":
            cls = {"direct": "direct", "dispatcher": "dispatcher", "none": "orphan"}[n["registered_via"]]
            badge = f'<span class="badge {cls}">{esc_html(n["registered_via"])}</span>'
        label = esc_html(n.get("name") or n.get("command") or n.get("script", ""))
        return f'<li data-node-key="{esc_html(n["node_key"])}">{label} {badge}</li>'

    left_html = "".join(_row(n, "left") for n in model["left"]) or '<li class="empty-state">none</li>'
    orphan_html = "".join(
        f'<li class="badge orphan">{esc_html(n["script"])} ({esc_html(n["target_status"])})</li>'
        for n in model["left_orphans"]) or '<li class="empty-state">none</li>'
    right_html = "".join(_row(n, "right") for n in model["right"]) or '<li class="empty-state">none</li>'
    return (
        '<div class="card"><h2>Registered hooks (settings.json)</h2><ul>' + left_html + '</ul></div>'
        '<div class="card"><h2>Orphan registrations</h2><ul>' + orphan_html + '</ul></div>'
        '<div class="card"><h2>Scripts on disk (registration/reachability status)</h2><ul>'
        + right_html + '</ul></div>'
    )


def _render_trend_body(model):
    if model["first_run"]:
        body = '<p class="empty-state">first run — no baseline</p>'
    else:
        rows = "".join(
            f'<tr><td>{esc_html(s["label"])}</td>'
            + "".join(f'<td>{esc_html(v)}</td>' for v in s["values"]) + '</tr>'
            for s in model["series"])
        header = "".join(f'<th>{esc_html(d)}</th>' for d in model["dates"])
        body = f'<div class="overflow-x"><table><tr><th>Metric</th>{header}</tr>{rows}</table></div>'
    return f'<div class="card"><h2>Trend (8 headline metrics)</h2>{body}</div>'


def _render_dupweb_body(model):
    """A6 duplication presentation (finding #5b): one row per pair as `{a} ⇄ {b}` …
    `{pct}% shared` — the arrow between the two node keys and a percent, never the
    old separate node-key columns + a raw decimal score."""
    if model["edges"]:
        rows = "".join(
            f'<tr><td>{esc_html(e["a"])} ⇄ {esc_html(e["b"])}</td>'
            f'<td class="tabular-nums">{_fmt_float(e["score"] * 100)}% shared</td>'
            f'<td>{esc_html(e["shared_sample"])}</td></tr>'
            for e in model["edges"])
        dup_body = f'<div class="overflow-x"><table><tr><th>Pair</th><th>Overlap</th><th>Sample</th></tr>{rows}</table></div>'
    else:
        dup_body = '<p class="empty-state">no duplicate pairs above threshold</p>'
    if model["phantom_refs"]:
        prows = "".join(
            f'<tr><td>{esc_html(r.get("source",""))}</td><td>{esc_html(r.get("ref",""))}</td>'
            f'<td>{esc_html(r.get("kind",""))}</td><td>{esc_html(r.get("resolved"))}</td></tr>'
            for r in model["phantom_refs"])
        phantom_body = f'<div class="overflow-x"><table><tr><th>Source</th><th>Ref</th><th>Kind</th><th>Resolved</th></tr>{prows}</table></div>'
    else:
        phantom_body = '<p class="empty-state">no phantom refs</p>'
    return (
        f'<div class="card"><h2>Duplication pairs (threshold {esc_html(model["threshold"])}, '
        f'{esc_html(model["metric"])})</h2>{dup_body}</div>'
        f'<div class="card"><h2>Phantom refs</h2>{phantom_body}</div>'
    )


def _render_length_flags_body(doc):
    """A6 length-flag table (finding #5b): a CRITICAL pill at >600 lines, a plain
    'over' pill otherwise. Iterated sorted by `(-lines, path)` — a total key, so
    output stays deterministic regardless of the flag list's original order."""
    flags = doc.get("instruction_length_flags", []) or []
    if flags:
        rows = "".join(
            (f'<tr><td>{esc_html(f["path"])}</td><td class="tabular-nums">{esc_html(f["lines"])}</td>'
             f'<td><span class="pill pill-critical">critical</span></td></tr>')
            if f.get("lines", 0) > 600 else
            (f'<tr><td>{esc_html(f["path"])}</td><td class="tabular-nums">{esc_html(f["lines"])}</td>'
             f'<td><span class="pill">over</span></td></tr>')
            for f in sorted(flags, key=lambda f: (-f.get("lines", 0), f["path"]))
        )
        body = f'<div class="overflow-x"><table><tr><th>Path</th><th>Lines</th><th>Flag</th></tr>{rows}</table></div>'
    else:
        body = '<p class="empty-state">no instruction files over cap</p>'
    return f'<div class="card"><h2>Length flags</h2>{body}</div>'


def _render_unchecked_binaries_body(doc):
    """finding #5a: `unchecked_binary_count` moved off the gauge readout (Task 3) —
    it MUST resurface here in a dedicated element so it is never silently dropped.
    Kept out of the folded Trend table's reach (a stray digit there can't false-green
    this element's own class scope)."""
    n = (doc.get("headline", {}) or {}).get("unchecked_binary_count", 0)
    return f'<div class="card"><p>Unchecked binaries: <span class="hygiene-unchecked">{esc_html(n)}</span></p></div>'


def _render_hygiene_view(doc, models):
    """Composes the former bipartite/trend/dupweb tab bodies plus length flags
    (finding #5b) and the unchecked-binary count (finding #5a) under ONE view
    (RESOLVED DECISION 2 — hook wiring folded here as 'Wiring integrity', never
    dropped)."""
    return (
        '<section id="view-hygiene" class="view" role="tabpanel" aria-labelledby="view-btn-hygiene">'
        f'<div class="view-toolbar">{_render_copy_controls("hygiene")}</div>'
        f'{_render_length_flags_body(doc)}'
        f'{_render_dupweb_body(models["dupweb"])}'
        f'{_render_unchecked_binaries_body(doc)}'
        f'{_render_trend_body(models["trend"])}'
        '<h2>Wiring integrity</h2>'
        f'{_render_bipartite_body(models["bipartite"])}'
        '</section>'
    )


def _render_provenance_footer(doc, skipped, footer, date):
    """Former `_render_notes_tab`, relocated to a `<footer>` (never `<main>`) — the
    root/date/generated_at + data-sources + warning-count lines stay always-visible;
    the rest collapses behind `<details>`."""
    def _list(items, empty_msg):
        if not items:
            return f'<p class="empty-state">{esc_html(empty_msg)}</p>'
        return "<ul>" + "".join(f"<li>{esc_html(i)}</li>" for i in items) + "</ul>"

    inaccessible = doc.get("inaccessible", []) or []
    blind_spots = doc.get("blind_spots", []) or []
    errors = doc.get("errors", []) or []
    warn_count = len(inaccessible) + len(errors)
    inacc_html = ("<ul>" + "".join(
        f'<li>{esc_html(i.get("path",""))} ({esc_html(i.get("reason",""))})</li>' for i in inaccessible)
        + "</ul>") if inaccessible else '<p class="empty-state">none</p>'
    skipped_html = ("<ul>" + "".join(
        f'<li>{esc_html(s.get("date",""))}: {esc_html(s.get("reason",""))}</li>' for s in skipped)
        + "</ul>") if skipped else '<p class="empty-state">none</p>'
    footer_line = " | ".join(f'{f["stream"]}: {f["status"]}' for f in footer) or "friction disabled"
    return (
        '<footer class="sources" id="provenance">'
        f'<div>root: {esc_html(doc.get("root",""))} | date: {esc_html(date)} '
        f'| generated_at: {esc_html(doc.get("generated_at",""))}</div>'
        f'<div>data sources: {esc_html(footer_line)}</div>'
        f'<div class="warn-count">{warn_count} warning(s)</div>'
        '<details><summary>provenance detail</summary>'
        f'<div class="card"><h2>Inaccessible ({len(inaccessible)})</h2>{inacc_html}</div>'
        f'<div class="card"><h2>Blind spots ({len(blind_spots)})</h2>{_list(blind_spots, "none")}</div>'
        f'<div class="card"><h2>Errors ({len(errors)})</h2>{_list(errors, "none")}</div>'
        f'<div class="card"><h2>Skipped sidecars ({len(skipped)})</h2>{skipped_html}</div>'
        '</details></footer>'
    )


def _stream_event_count(f, codex_aggregate):
    """A6 headline count for one stream card — the SAME figure `_friction_sentence`
    already leads with, so the card count and the sentence never disagree. Pure
    function of the footer dict `f` (from `build_friction_overlay`) + `codex_aggregate`;
    reuses counters the join functions already computed, never re-derives."""
    if f["status"] != "loaded":
        return 0
    stream = f["stream"]
    if stream == "decisions":
        return f.get("segments_joined", 0)
    if stream == "metrics":
        return f.get("records_eligible", 0) - f.get("records_aggregate_only", 0)
    if stream == "interventions":
        return f.get("records_parsed", 0)
    return codex_aggregate.get("runs", 0)   # codex — aggregate-only


def _render_stream_card(f, codex_aggregate):
    """A6 stream card: event count, title, plain-English description, source filename."""
    count = _stream_event_count(f, codex_aggregate)
    title = STREAM_LABELS[f["stream"]]
    sentence = _friction_sentence(f, codex_aggregate)
    return (
        '<div class="stream-card">'
        f'<div class="count">{esc_html(count)}</div>'
        f'<h3>{esc_html(title)}</h3>'
        f'<p>{esc_html(sentence)}</p>'
        f'<div class="source">{esc_html(f["path_display"])}</div>'
        '</div>'
    )


def _render_component_friction_table(joined):
    """A6 per-component join table (finding #1): which map nodes got heated, and how
    many friction records each. Reads the SAME `joined` dict `build_friction_overlay`
    already returns — does not re-derive. `sorted(joined.items())` (node_key ascending)
    for deterministic, insertion-order-independent output."""
    if not joined:
        rows = '<tr class="friction-component-row"><td colspan="2" class="empty-state">no components joined</td></tr>'
    else:
        rows = "".join(
            f'<tr class="friction-component-row"><td>{esc_html(node_key)}</td>'
            f'<td>{esc_html(len(records))}</td></tr>'
            for node_key, records in sorted(joined.items())
        )
    return (
        '<div class="overflow-x"><table class="friction-components">'
        '<tr><th>Component</th><th>Friction records</th></tr>'
        f'{rows}</table></div>'
    )


def _render_friction_row(f, codex_aggregate):
    sentence = esc_html(_friction_sentence(f, codex_aggregate))
    raw = {k: v for k, v in f.items() if k not in ("stream", "status", "path_display")}
    raw_html = (f'<details class="friction-row-detail"><summary>raw counters</summary>'
                f'{esc_html(json.dumps(raw, sort_keys=True))}</details>') if raw else ""
    return (f'<tr><td>{esc_html(f["stream"])}</td><td>{esc_html(f["status"])}</td>'
            f'<td>{esc_html(f["path_display"])}</td>'
            f'<td>{sentence}{raw_html}</td></tr>')


def _render_friction_panel(joined, footer, codex_aggregate, friction_total_value):
    explainer = (
        '<p class="friction-explainer">Friction = where your harness has seen the most churn. '
        'These local telemetry streams (decisions, review metrics, Codex reviews, interventions) '
        'are matched by name onto the components on the map — a data join, not a judgment.</p>'
    )
    stream_cards = "".join(_render_stream_card(f, codex_aggregate) for f in footer)
    component_table = _render_component_friction_table(joined)
    rows = "".join(_render_friction_row(f, codex_aggregate) for f in footer)
    codex_html = (
        f'<div class="card"><h2>Codex aggregate (not node-joined)</h2>'
        f'<p>{esc_html(_codex_sentence(codex_aggregate))}</p></div>'
    )
    return (
        '<aside class="card" id="friction-panel">'
        f'<h2>Friction events: {esc_html(friction_total_value)}</h2>'
        f'<div class="stream-cards">{stream_cards}</div>'
        f'{explainer}'
        f'{component_table}'
        f'<div class="overflow-x"><table><tr><th>Stream</th><th>Status</th><th>Path</th><th>What matched</th></tr>{rows}</table></div>'
        f'{codex_html}</aside>'
    )


def _render_friction_view(joined, footer, codex_aggregate, drag, friction_total_value):
    """Former `_render_friction_panel`, moved verbatim, plus the drag-candidate table
    half of the former `_render_civc_drag_tab` appended (IA mapping). A6/DECISION 6
    (Task 8): 4 stream cards + a per-component join table now render above the
    explainer, and the header reads `friction_total` — the SAME value the AM-1
    instrument gauge renders — instead of the raw joined-record count."""
    friction_body = _render_friction_panel(joined, footer, codex_aggregate, friction_total_value)
    if not drag["available"]:
        drag_body = '<p class="empty-state">synthesis sidecar not found — drag-candidate table unavailable this run.</p>'
    elif not drag["rows"]:
        drag_body = '<p class="empty-state">no drag candidates</p>'
    else:
        rows = "".join(
            f'<tr><td>{esc_html(r.get("n",""))}</td><td>{esc_html(r.get("surface",""))}</td>'
            f'<td>{esc_html(r.get("evidence",""))}</td><td class="badge">{esc_html(r.get("outcome",""))}</td></tr>'
            for r in drag["rows"])
        drag_body = f'<div class="overflow-x"><table><tr><th>#</th><th>Surface</th><th>Evidence</th><th>Outcome</th></tr>{rows}</table></div>'
    return (
        '<section id="view-friction" class="view" role="tabpanel" aria-labelledby="view-btn-friction">'
        f'<div class="view-toolbar">{_render_copy_controls("friction")}</div>'
        f'{friction_body}'
        f'<div class="card"><h2>Drag candidates</h2>{drag_body}</div>'
        '</section>'
    )


VIEWS = (("view-overview", "Overview"), ("view-coverage", "Coverage"),
         ("view-weight", "Weight"), ("view-friction", "Friction"), ("view-hygiene", "Hygiene"))


def render_html(date, models, friction, notes):
    """Assembles the final HTML document — a fixed named-section sequence (§4.8),
    never set/dict-driven order. 5-view IA (A1): all views render WITHOUT `hidden`
    server-side (progressive enhancement) — the static script collapses to Overview
    on load."""
    doc = notes["doc"]
    skipped = notes["skipped"]
    headline = doc.get("headline", {}) or {}
    heat, joined, footer, codex_aggregate = friction
    phantom_ref_count = len(doc.get("phantom_refs", []) or [])
    friction_total_value = friction_total(joined, codex_aggregate)
    overview_model = build_overview_model(models, headline, phantom_ref_count, friction_total_value)

    warn_count = len(doc.get("inaccessible", []) or []) + len(doc.get("errors", []) or [])
    warn_badge = (f'<a class="warn-badge" href="#provenance" data-target="provenance">'
                  f'{warn_count} warning(s)</a>') if warn_count else ""

    style_hash = _csp_hash(STATIC_STYLE)
    script_hash = _csp_hash(STATIC_SCRIPT)
    csp = (f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
           f"style-src 'sha256-{style_hash}'; script-src 'sha256-{script_hash}'; "
           f"connect-src 'none'; base-uri 'none'; form-action 'none'\">")

    view_buttons = "".join(
        f'<button class="view-btn" id="view-btn-{vid.split("-", 1)[1]}" role="tab" '
        f'data-target="{vid}" aria-selected="false">{esc_html(label)}</button>'
        for vid, label in VIEWS)

    copy_payloads = build_copy_payloads(date, models, friction, doc)

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        csp,
        f'<title>harness-map {esc_html(date)}</title>',
        f"<style>{STATIC_STYLE}</style>",
        "</head><body>",
        "<header><h1>harness-map</h1>",
        f'<div class="subtitle">root: {esc_html(doc.get("root",""))} | date: {esc_html(date)} '
        f'| generated_at: {esc_html(doc.get("generated_at",""))} {warn_badge}</div></header>',
        _render_instrument_readout(headline, phantom_ref_count, friction_total_value, models["trend"]),
        '<div class="controls">',
        '<nav class="view-switch" role="tablist">',
        view_buttons,
        '</nav>',
        '<button class="action-btn" id="expand-all">Expand all / print view</button>',
        "</div>",
        "<main>",
        _render_overview_view(overview_model, models["civc"]),
        _render_coverage_view(models["civc"]),
        _render_weight_view(models["context_weight"], heat),
        _render_friction_view(joined, footer, codex_aggregate, models["drag"], friction_total_value),
        _render_hygiene_view(doc, models),
        "</main>",
        _render_copy_island("overview", copy_payloads["overview"]),
        _render_copy_island("coverage", copy_payloads["coverage"]),
        _render_copy_island("weight", copy_payloads["weight"]),
        _render_copy_island("friction", copy_payloads["friction"]),
        _render_copy_island("hygiene", copy_payloads["hygiene"]),
        _render_provenance_footer(doc, skipped, footer, date),
        f"<script>{STATIC_SCRIPT}</script>",
        "</body></html>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------------- main
def main(argv=None):
    ap = argparse.ArgumentParser(description="Render an interactive HTML map from harness-map sidecar(s).")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--date", default=None)
    ap.add_argument("--metrics-file", default=None)
    ap.add_argument("--decisions-file", default=None)
    ap.add_argument("--codex-file", default=None)
    ap.add_argument("--interventions-file", default=None)
    ap.add_argument("--no-friction", action="store_true")
    args = ap.parse_args(argv)

    out_dir = Path(args.out_dir)
    if not out_dir.is_dir():
        print(f"fatal: --out-dir does not exist or is not a directory: {out_dir}", file=sys.stderr)
        return 1
    if args.date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        print(f"fatal: --date must be YYYY-MM-DD: {args.date}", file=sys.stderr)
        return 1

    sidecars = find_sidecars(out_dir)
    if not sidecars:
        print(f"fatal: zero sidecars found in {out_dir}", file=sys.stderr)
        return 1

    date, doc, skipped, err = select_current(sidecars, args.date)
    if err is not None:
        print(f"fatal: {err}", file=sys.stderr)
        return 1
    if doc.get("schema_version") != SCHEMA_VERSION:
        print(f"fatal: selected sidecar has unsupported schema_version {doc.get('schema_version')}",
              file=sys.stderr)
        return 1

    # Trend series: load every OTHER sidecar too, filtered to the same root + schema version
    # (Codex F13); corrupt/incompatible ones are excluded and noted in skipped[].
    dated_docs = []
    for d, p in sidecars:
        if d == date:
            dated_docs.append((d, doc))
            continue
        other_doc, other_err = load_sidecar(p)
        if other_err is not None:
            skipped.append({"date": d, "reason": other_err})
            continue
        if other_doc.get("schema_version") != SCHEMA_VERSION or other_doc.get("root") != doc.get("root"):
            skipped.append({"date": d, "reason": "schema_version/root mismatch with selected sidecar"})
            continue
        dated_docs.append((d, other_doc))
    dated_docs.sort(key=lambda t: t[0])

    synth, synth_err = load_synthesis(out_dir, date)
    if synth_err is not None:
        skipped.append({"date": date, "reason": synth_err})

    models = {
        "context_weight": build_contextweight_model(doc),
        "bipartite": build_bipartite_model(doc),
        "trend": build_trend_model(dated_docs),
        "dupweb": build_dupweb_model(doc),
        "civc": build_civc_model(synth),
        "drag": build_dragcandidate_model(synth),
    }
    node_index = build_node_index(models)

    if args.no_friction:
        streams = {"decisions": None, "metrics": None, "interventions": None, "codex": None}
    else:
        home = Path.home()
        streams = {
            "decisions": Path(args.decisions_file) if args.decisions_file else home / ".claude" / "harness-decisions.jsonl",
            "metrics": Path(args.metrics_file) if args.metrics_file else home / ".claude" / "harness-metrics.jsonl",
            "codex": Path(args.codex_file) if args.codex_file else home / ".claude" / "harness-codex.jsonl",
            "interventions": Path(args.interventions_file) if args.interventions_file else None,
        }
    friction = build_friction_overlay(doc, streams, node_index, date, args.no_friction)

    html_text = render_html(date, models, friction, {"doc": doc, "skipped": skipped})

    out_path = out_dir / f"harness-map-{date}.html"
    try:
        write_html_safely(out_path, html_text, doc.get("root"))
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"fatal: could not write {out_path}: {e}", file=sys.stderr)
        return 1
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
