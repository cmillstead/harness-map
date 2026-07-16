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
HEAT_RAMP = ("#FEE5D9", "#FCAE91", "#FB6A4A", "#CB181D")
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
:root{--bg:#0b0f14;--panel:#121824;--text:#e6edf3;--muted:#8b98a5;--border:#2a3341;--accent:#56b4e9;--crit:#cb181d}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--text);font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:0}
header{padding:16px 20px;border-bottom:1px solid var(--border)}
h1{font-size:1.25rem;margin:0 0 4px 0}
.subtitle{color:var(--muted);font-size:0.85rem}
.tiles{display:flex;flex-wrap:wrap;gap:10px;padding:12px 20px}
.tile{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px;min-width:150px}
.tile .v{font-size:1.4rem;font-weight:600}
.tile .l{color:var(--muted);font-size:0.75rem}
.warn-badge{background:var(--crit);color:#fff;border-radius:6px;padding:2px 8px;font-size:0.75rem;text-decoration:none}
.controls{position:sticky;top:0;background:var(--bg);border-bottom:1px solid var(--border);padding:8px 20px;display:flex;gap:8px;flex-wrap:wrap;z-index:5}
button.tab-btn,button.action-btn{background:var(--panel);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 12px;cursor:pointer;font-size:0.85rem}
button.tab-btn[aria-selected="true"]{border-color:var(--accent);color:var(--accent)}
button[aria-pressed="true"]{border-color:var(--accent);color:var(--accent)}
main{padding:16px 20px}
.tab-panel[hidden]{display:none}
.card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px;margin-bottom:14px}
.empty-state{color:var(--muted);font-style:italic}
table{border-collapse:collapse;width:100%;font-size:0.85rem}
th,td{border:1px solid var(--border);padding:6px 8px;text-align:left}
th{color:var(--muted);font-weight:600}
.badge{display:inline-block;border-radius:5px;padding:1px 6px;font-size:0.72rem;border:1px solid var(--border)}
.badge.orphan{border-color:var(--crit);color:var(--crit)}
.badge.direct{border-color:#009e73;color:#009e73}
.badge.dispatcher{border-color:var(--accent);color:var(--accent)}
.cell-label{font-size:8px;fill:var(--text)}
.legend-swatch{display:inline-block;width:10px;height:10px;margin-right:4px;border-radius:2px;vertical-align:middle}
svg text{font-family:inherit}
footer.sources{border-top:1px solid var(--border);padding:10px 20px;color:var(--muted);font-size:0.78rem}
.overflow-x{overflow-x:auto}
@media (prefers-reduced-motion: no-preference){button{transition:border-color .15s}}
.cell-rect{stroke:var(--border);stroke-width:0.5}
.friction-badge{display:none;font-size:7px;font-weight:600;fill:var(--text)}
body.friction-on .friction-badge{display:inline}
.friction-legend{display:flex;align-items:center;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:0.75rem;padding:4px 20px 0}
.legend-entry{display:inline-flex;align-items:center;gap:4px}
.legend-swatch.fh0{background:var(--panel);border:1px solid var(--border)}
.legend-note{color:var(--muted)}
.friction-explainer{color:var(--muted);font-size:0.85rem;margin:0 0 10px 0}
.friction-row-detail{display:block;color:var(--muted);font-size:0.78rem;margin-top:2px}
details{color:var(--muted)}
details > summary{cursor:pointer;color:var(--accent)}
.civc-legend{color:var(--muted);font-size:0.8rem;margin:0 0 10px 0}
td.verdict-covered{background:rgba(0,158,115,0.15)}
td.verdict-thin{background:rgba(86,180,233,0.12)}
td.verdict-empty{color:var(--muted)}
.badge.verdict-thin{border-color:var(--accent);color:var(--accent)}
.badge.verdict-covered{border-color:#009e73;color:#009e73}
"""
_HEAT_CSS = "".join(
    f"body.friction-on .fh{i}{{stroke:{color};stroke-width:2}}"
    f".legend-swatch.fh{i}{{background:{color}}}"
    for i, color in enumerate(HEAT_RAMP, start=1)
)
STATIC_STYLE = STATIC_STYLE + _HEAT_CSS

STATIC_SCRIPT = """
(function(){
  var buttons = document.querySelectorAll('.tab-btn');
  var panels = document.querySelectorAll('.tab-panel');
  function activate(id){
    panels.forEach(function(p){ p.hidden = (p.id !== id); });
    buttons.forEach(function(b){
      b.setAttribute('aria-selected', b.dataset.target === id ? 'true' : 'false');
    });
  }
  buttons.forEach(function(b){
    b.addEventListener('click', function(){ activate(b.dataset.target); });
  });
  var overlayToggle = document.getElementById('friction-toggle');
  if (overlayToggle) {
    overlayToggle.addEventListener('click', function(){
      var pressed = overlayToggle.getAttribute('aria-pressed') === 'true';
      overlayToggle.setAttribute('aria-pressed', pressed ? 'false' : 'true');
      document.body.classList.toggle('friction-on', !pressed);
    });
  }
  var expandAll = document.getElementById('expand-all');
  if (expandAll) {
    expandAll.addEventListener('click', function(){
      panels.forEach(function(p){ p.hidden = false; });
    });
  }
  if (panels.length) { activate(panels[0].id); }
})();
"""


def _csp_hash(block):
    return base64.b64encode(hashlib.sha256(block.encode("utf-8")).digest()).decode("ascii")


# --------------------------------------------------------------------------- HTML render
def _render_treemap_svg(tree, heat, dom_id):
    """Heat is shown two ways once the friction overlay toggle is on (never color
    alone, §UI): a CSS-class-driven stroke ramp on the cell, AND a text join-count
    badge in the corner. Both are hidden-by-default via `body.friction-on` CSS so the
    toggle button has a visible, demonstrable effect."""
    w, h = tree["canvas_w"], tree["canvas_h"]
    parts = [f'<svg viewBox="0 0 {_fmt_float(w)} {_fmt_float(h)}" '
             f'width="100%" height="360" role="img" aria-labelledby="{esc_html(dom_id)}-title">']
    parts.append(f'<title id="{esc_html(dom_id)}-title">Context-weight treemap</title>')
    for c in tree["cells"]:
        heat_n = heat.get(c["node_key"], 0)
        bucket = min(heat_n, len(HEAT_RAMP)) if heat_n else 0
        rect_cls = f"cell-rect fh{bucket}" if bucket else "cell-rect"
        label = esc_html(Path(c["path"]).name)
        parts.append(
            f'<rect x="{c["x"]}" y="{c["y"]}" width="{c["w"]}" height="{c["h"]}" '
            f'fill="{esc_html(c.get("fill", "#56b4e9"))}" class="{rect_cls}" '
            f'data-node-key="{esc_html(c["node_key"])}"><title>{esc_html(c["path"])} '
            f'(friction: {heat_n})</title></rect>')
        if float(c["w"]) > 40 and float(c["h"]) > 14:
            tx, ty = _fmt_float(float(c["x"]) + 2), _fmt_float(float(c["y"]) + 10)
            parts.append(f'<text x="{tx}" y="{ty}" class="cell-label">{label}</text>')
            if heat_n:
                bx = _fmt_float(float(c["x"]) + float(c["w"]) - 2)
                parts.append(f'<text x="{bx}" y="{ty}" text-anchor="end" '
                              f'class="friction-badge">{heat_n}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _render_tile(key, label, polarity, value):
    return (f'<div class="tile"><div class="v">{esc_html(value)}</div>'
            f'<div class="l">{esc_html(label)}</div></div>')


def _render_headline(headline):
    tiles = "".join(_render_tile(k, label, pol, headline.get(k, 0)) for k, label, pol in HEADLINE_KEYS)
    return f'<div class="tiles">{tiles}</div>'


def _render_context_weight_tab(model, heat):
    always_svg = _render_treemap_svg(model["always"], heat, "always-treemap")
    ondemand_svg = _render_treemap_svg(model["on_demand"], heat, "ondemand-treemap")
    return (
        '<section id="panel-1" class="tab-panel" role="tabpanel" aria-labelledby="tab-btn-1" hidden>'
        '<div class="card"><h2>Always-loaded (by category, sized by est. tokens)</h2>'
        f'{always_svg}</div>'
        '<div class="card"><h2>On-demand (skills / phases / prompts / agents / memory, sized by words)</h2>'
        f'{ondemand_svg}</div></section>'
    )


def _render_bipartite_tab(model):
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
        '<section id="panel-2" class="tab-panel" role="tabpanel" aria-labelledby="tab-btn-2" hidden>'
        '<div class="card"><h2>Registered hooks (settings.json)</h2><ul>' + left_html + '</ul></div>'
        '<div class="card"><h2>Orphan registrations</h2><ul>' + orphan_html + '</ul></div>'
        '<div class="card"><h2>Scripts on disk (registration/reachability status)</h2><ul>'
        + right_html + '</ul></div></section>'
    )


def _render_trend_tab(model):
    if model["first_run"]:
        body = '<p class="empty-state">first run — no baseline</p>'
    else:
        rows = "".join(
            f'<tr><td>{esc_html(s["label"])}</td>'
            + "".join(f'<td>{esc_html(v)}</td>' for v in s["values"]) + '</tr>'
            for s in model["series"])
        header = "".join(f'<th>{esc_html(d)}</th>' for d in model["dates"])
        body = f'<div class="overflow-x"><table><tr><th>Metric</th>{header}</tr>{rows}</table></div>'
    return f'<section id="panel-3" class="tab-panel" role="tabpanel" aria-labelledby="tab-btn-3" hidden><div class="card"><h2>Trend (8 headline metrics)</h2>{body}</div></section>'


def _render_dupweb_tab(model):
    if model["edges"]:
        rows = "".join(
            f'<tr><td>{esc_html(e["a"])}</td><td>{esc_html(e["b"])}</td>'
            f'<td>{esc_html(round(e["score"], 3))}</td><td>{esc_html(e["shared_sample"])}</td></tr>'
            for e in model["edges"])
        dup_body = f'<div class="overflow-x"><table><tr><th>File A</th><th>File B</th><th>Score</th><th>Shared sample</th></tr>{rows}</table></div>'
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
        '<section id="panel-4" class="tab-panel" role="tabpanel" aria-labelledby="tab-btn-4" hidden>'
        f'<div class="card"><h2>Duplication pairs (threshold {esc_html(model["threshold"])}, '
        f'{esc_html(model["metric"])})</h2>{dup_body}</div>'
        f'<div class="card"><h2>Phantom refs</h2>{phantom_body}</div></section>'
    )


def _render_civc_drag_tab(civc, drag):
    if not civc["available"]:
        civc_body = '<p class="empty-state">synthesis sidecar not found — Coverage Matrix unavailable this run.</p>'
    else:
        legend = (
            '<p class="civc-legend">Coverage scale (empty cells are intentional roadmap, not blanks): '
            '<span class="badge verdict-empty">empty</span> → '
            '<span class="badge verdict-thin">thin</span> → '
            '<span class="badge verdict-covered">covered</span>. '
            'Cells with a "note" expose it via a details toggle.</p>'
        )
        header = "".join(f"<th>{esc_html(s)}</th>" for s in SURFACES)
        rows = []
        by_verb = {}
        for c in civc["cells"]:
            by_verb.setdefault(c["verb"], []).append(c)
        for verb in VERBS:
            cell_html = []
            for c in by_verb.get(verb, []):
                note_html = (f'<details><summary>note</summary>{esc_html(c["note"])}</details>'
                             if c.get("note") else "")
                cell_html.append(
                    f'<td class="verdict-{esc_html(c["verdict"])}">{esc_html(c["verdict"])}{note_html}</td>')
            rows.append(f"<tr><th>{esc_html(verb)}</th>{''.join(cell_html)}</tr>")
        civc_body = (legend + f'<div class="overflow-x"><table><tr><th></th>{header}</tr>'
                     f'{"".join(rows)}</table></div>')
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
        '<section id="panel-5" class="tab-panel" role="tabpanel" aria-labelledby="tab-btn-5" hidden>'
        '<div class="card"><h2>Coverage Matrix</h2>'
        '<p class="subtitle">six verbs (what the harness does to behavior) '
        '× six surfaces (what it’s made of)</p>'
        f'{civc_body}</div>'
        f'<div class="card"><h2>Drag candidates</h2>{drag_body}</div></section>'
    )


def _render_notes_tab(doc, skipped):
    def _list(items, empty_msg):
        if not items:
            return f'<p class="empty-state">{esc_html(empty_msg)}</p>'
        return "<ul>" + "".join(f"<li>{esc_html(i)}</li>" for i in items) + "</ul>"

    inaccessible = doc.get("inaccessible", []) or []
    blind_spots = doc.get("blind_spots", []) or []
    errors = doc.get("errors", []) or []
    inacc_html = ("<ul>" + "".join(
        f'<li>{esc_html(i.get("path",""))} ({esc_html(i.get("reason",""))})</li>' for i in inaccessible)
        + "</ul>") if inaccessible else '<p class="empty-state">none</p>'
    skipped_html = ("<ul>" + "".join(
        f'<li>{esc_html(s.get("date",""))}: {esc_html(s.get("reason",""))}</li>' for s in skipped)
        + "</ul>") if skipped else '<p class="empty-state">none</p>'
    return (
        '<section id="panel-6" class="tab-panel" role="tabpanel" aria-labelledby="tab-btn-6" hidden>'
        f'<div class="card"><h2>Inaccessible ({len(inaccessible)})</h2>{inacc_html}</div>'
        f'<div class="card"><h2>Blind spots ({len(blind_spots)})</h2>{_list(blind_spots, "none")}</div>'
        f'<div class="card"><h2>Errors ({len(errors)})</h2>{_list(errors, "none")}</div>'
        f'<div class="card"><h2>Skipped sidecars ({len(skipped)})</h2>{skipped_html}</div></section>'
    )


def _render_friction_row(f, codex_aggregate):
    sentence = esc_html(_friction_sentence(f, codex_aggregate))
    raw = {k: v for k, v in f.items() if k not in ("stream", "status", "path_display")}
    raw_html = (f'<details class="friction-row-detail"><summary>raw counters</summary>'
                f'{esc_html(json.dumps(raw, sort_keys=True))}</details>') if raw else ""
    return (f'<tr><td>{esc_html(f["stream"])}</td><td>{esc_html(f["status"])}</td>'
            f'<td>{esc_html(f["path_display"])}</td>'
            f'<td>{sentence}{raw_html}</td></tr>')


def _render_friction_panel(joined, footer, codex_aggregate):
    explainer = (
        '<p class="friction-explainer">Friction = where your harness has seen the most churn. '
        'These local telemetry streams (decisions, review metrics, Codex reviews, interventions) '
        'are matched by name onto the components on the map — a data join, not a judgment.</p>'
    )
    rows = "".join(_render_friction_row(f, codex_aggregate) for f in footer)
    joined_count = sum(len(v) for v in joined.values())
    codex_html = (
        f'<div class="card"><h2>Codex aggregate (not node-joined)</h2>'
        f'<p>{esc_html(_codex_sentence(codex_aggregate))}</p></div>'
    )
    return (
        '<aside class="card" id="friction-panel">'
        f'<h2>Friction overlay — joined records: {joined_count}</h2>'
        f'{explainer}'
        f'<div class="overflow-x"><table><tr><th>Stream</th><th>Status</th><th>Path</th><th>What matched</th></tr>{rows}</table></div>'
        f'{codex_html}</aside>'
    )


def render_html(date, models, friction, notes):
    """Assembles the final HTML document — a fixed named-section sequence (§4.8),
    never set/dict-driven order."""
    doc = notes["doc"]
    skipped = notes["skipped"]
    headline = doc.get("headline", {}) or {}
    heat, joined, footer, codex_aggregate = friction

    warn_count = len(doc.get("inaccessible", []) or []) + len(doc.get("errors", []) or [])
    warn_badge = (f'<a class="warn-badge" href="#panel-6" data-target="panel-6">'
                  f'{warn_count} warning(s)</a>') if warn_count else ""

    style_hash = _csp_hash(STATIC_STYLE)
    script_hash = _csp_hash(STATIC_SCRIPT)
    csp = (f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
           f"style-src 'sha256-{style_hash}'; script-src 'sha256-{script_hash}'; "
           f"connect-src 'none'; base-uri 'none'; form-action 'none'\">")

    tabs = [("panel-1", "Context Weight"), ("panel-2", "Hook Wiring"), ("panel-3", "Trends"),
            ("panel-4", "Duplication & Phantom Refs"), ("panel-5", "Coverage Matrix"),
            ("panel-6", "Notes & Blind Spots")]
    tab_buttons = "".join(
        f'<button class="tab-btn" id="tab-btn-{i+1}" role="tab" data-target="{pid}" '
        f'aria-selected="false">{esc_html(label)}</button>'
        for i, (pid, label) in enumerate(tabs))

    footer_line = " | ".join(f'{f["stream"]}: {f["status"]}' for f in footer) or "friction disabled"

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
        _render_headline(headline),
        '<div class="controls" role="tablist">',
        tab_buttons,
        '<button class="action-btn" id="friction-toggle" aria-pressed="false">Show friction overlay</button>',
        '<button class="action-btn" id="expand-all">Expand all / print view</button>',
        "</div>",
        '<div class="friction-legend" id="friction-legend">'
        '<span>Friction heat, once the overlay is on:</span>'
        '<span class="legend-entry"><span class="legend-swatch fh0"></span>none</span>'
        '<span class="legend-entry"><span class="legend-swatch fh1"></span>some</span>'
        '<span class="legend-entry"><span class="legend-swatch fh4"></span>most-active</span>'
        '<span class="legend-note">every heated cell also shows a join-count '
        'badge in the corner (color is never the only signal)</span></div>',
        "<main>",
        _render_context_weight_tab(models["context_weight"], heat),
        _render_bipartite_tab(models["bipartite"]),
        _render_trend_tab(models["trend"]),
        _render_dupweb_tab(models["dupweb"]),
        _render_civc_drag_tab(models["civc"], models["drag"]),
        _render_notes_tab(doc, skipped),
        _render_friction_panel(joined, footer, codex_aggregate),
        "</main>",
        f'<footer class="sources">data sources: {esc_html(footer_line)}</footer>',
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
