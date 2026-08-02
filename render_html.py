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
import dataclasses
import datetime
import hashlib
import html
import importlib.util
import json
import math
import os
import re
import stat
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, cast

SCHEMA_VERSION = 1
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SIDECAR_RE = re.compile(r"^harness-map-(\d{4}-\d{2}-\d{2})\.json$")

# --- fixed enums (determinism §4.4: never sorted(set(...)), always the schema tuple) ---
# T12 (P1 fix): the operator-only 6-entry tuple below MISSED the three categories
# `_walk_project_tier` (collector.py) emits in compose mode -- project rules,
# CLAUDE.local.md, and nested CLAUDE.md were silently dropped from BOTH the treemap
# (`_tokens_treemap`, which iterates this tuple) and the ladder (which renders the
# SAME `tree["cells"]` `_tokens_treemap` built -- one fix covers both). Kept a FIXED
# tuple, never `sorted(set(...))` (§4.4 determinism).
ALWAYS_CATEGORIES = (
    ("claude_md", "CLAUDE.md (root)"),
    ("project_claude_md", "CLAUDE.md (project)"),
    ("memory", "Memory index"),
    ("rule", "Rules"),
    ("coding_team_rule", "coding-team rules"),
    ("skill_rule", "Sub-skill rules"),
    ("project_rule", "Project rules"),
    ("project_claude_local_md", "CLAUDE.local.md"),
    ("project_claude_md_nested", "CLAUDE.md (nested)"),
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
VERDICTS = ("covered", "thin", "empty")
# Single source of truth for the Hygiene tab's `critical` pill threshold — the
# treemap's length-criticality outline (B-t3 follow-up) reuses this SAME constant
# via `_length_critical_node_keys` so the two views can never silently disagree.
LENGTH_CRITICAL_LINES = 600

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

HEAT_RAMP = ("#FCAE91", "#FB6A4A", "#DE2D26", "#A50F15")
STREAM_ORDER = ("decisions", "metrics", "interventions", "codex")
STREAM_LABELS = {"decisions": "Decisions", "metrics": "Review metrics",
                  "interventions": "Interventions", "codex": "Codex reviews"}
CODEX_VERDICT_LABELS = {"APPROVED": "approved", "PASS": "pass", "REVISE": "needed revision",
                         "SHIP": "shipped"}

# read_jsonl's two caps, named so build_friction_overlay passes the SAME values the
# function defaults to and a test can trip either one with a real fixture instead of a mock.
# They are INDEPENDENT: a stream of many compact records trips STREAM_MAX_LINES without ever
# approaching STREAM_MAX_BYTES.
STREAM_MAX_BYTES = 5_000_000
STREAM_MAX_LINES = 20_000


# --------------------------------------------------------------------------- escaping
def esc_html(value: Any) -> str:
    """HTML/attribute/SVG-text escaping — the single primitive for every scanned or
    telemetry string leaf (§3.1). Covers text content, attributes, and SVG text/attrs
    (shared HTML5 tokenizer). Lone UTF-16 surrogates (the collector deliberately
    preserves them — Codex F9) are neutralized to a deterministic backslash escape
    BEFORE html.escape, since json.dumps/str() pass them through untouched otherwise."""
    try:
        text = str(value)
    except (ValueError, RecursionError) as exc:
        # Control 1 (S2 gate fix, S1/S2/S9): str() is NOT total. An int over
        # sys.get_int_max_str_digits() (4300) raises ValueError -- verified:
        # esc_html(10**5000). A deeply nested structure raises RecursionError.
        # esc_html is the single escaping primitive on EVERY value path in this module,
        # so an unguarded str() turns one corrupt sidecar leaf into a whole-page crash.
        # This is STRICTLY BROADER than Codex #3 (_coerce_floats guards one series).
        # Degrade to a VISIBLE marker built only from type names (both are Python
        # identifiers, so the marker itself needs no further escaping) -- never a silent
        # empty string, which would look like a legitimately blank field.
        return f"[unrenderable value: {type(value).__name__} ({type(exc).__name__})]"
    text = re.sub(r"[\ud800-\udfff]", lambda m: f"\\u{ord(m.group(0)):04x}", text)
    return html.escape(text, quote=True)


def esc_json_script(value: Any, *, ordered: bool = True) -> str:
    """Fallback-only helper (§3.1) for the sole content of a `<script
    type="application/json">` data island read via `.textContent` + `JSON.parse`.
    NOT used by default — the renderer keeps dynamic data out of executable script
    blocks entirely (§9-R C)."""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=ordered)
    return text.translate({ord("<"): "\\u003c", ord(">"): "\\u003e", ord("&"): "\\u0026"})


# --------------------------------------------------------------------- numeric gate
# Control 2 (S2 gate fix): THE shared numeric gate for every arithmetic site in this
# module. Do NOT add a seventh bespoke guard -- extend this one.
#
# `bool` is excluded EXPLICITLY: isinstance(True, int) is True (verified), and a stray
# boolean would silently arithmetic as 1/0 rather than being rejected.
#
# math.isfinite rejects nan/inf. NaN is the dangerous case, not a cosmetic one: verified
# _trend_delta(cur=nan, prev=1) -> ('▼ nan', 'good') -- a GREEN "improving" verdict on
# corrupt data -- while _gauge_band(nan) -> ('HEAVY','bad') on the SAME value.
#
# The magnitude bound stops an int too large for float() (OverflowError, verified at
# 10**5000). It is deliberately far above any plausible harness metric (the largest live
# headline value is ~5 digits), so it can only ever reject corruption.
_MAX_ABS_NUMERIC = 1e15


def finite_number(value: Any) -> float | None:
    """Coerce `value` to a finite, in-range float, or None. NEVER raises."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        coerced = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(coerced) or abs(coerced) > _MAX_ABS_NUMERIC:
        return None
    return coerced


def nonneg_number(value: Any) -> float | None:
    """`finite_number` narrowed to the NON-NEGATIVE domain — the gate for SIZES and
    COUNTS (Codex gate finding 5).

    `finite_number` accepts negatives BY DESIGN and must keep doing so: a trend delta is
    legitimately negative, and an existing assertion pins `-5` as passing. But a negative
    is nonsensical wherever the value means "how much" or "how many", and there it does
    real damage in the reassuring direction:
      * `tokens_est` rows of +10 and -10 in one category sum to zero, so the whole
        category died at `_tokens_treemap`'s `tokens <= 0` gate WITHOUT entering
        `unrenderable` — silently deleting the drawable row beside it.
      * a headline count of -1 satisfies `value <= 0` and paints the green CLEAN /
        COMPLIANT / LEAN verdict.
    So the narrowing lives at the call sites where the domain says non-negative, never in
    `finite_number` itself."""
    num = finite_number(value)
    return None if num is None or num < 0 else num


def _gated_size(value: Any) -> float:
    """`nonneg_number` for treemap `size`/`words`, preserving INTEGER presentation.

    NOT a seventh bespoke guard — it delegates to the one gate and only fixes the value's
    display type. `size` is not purely geometry: it is also rendered as TEXT (ladder cell
    labels, the overview weight-tax list, the copy payloads), so returning a bare float
    would silently rewrite every "100 tokens" as "100.0 tokens" across the whole report —
    caught by test_serve's byte-equality assertions. Unusable values become `0` (int, the
    same integer presentation the accept path gives), which squarify's `size > 0` filter
    drops; they are disclosed via `unrenderable` instead."""
    num = nonneg_number(value)
    if num is None:
        # T2 spec-review LOW: the rejection path must return an int-presentable
        # value too, same as the accept path below -- a bare 0.0 here survives on a
        # field the `unrenderable` disclosure doesn't key on (e.g. `words` beside a
        # valid `tokens_est`) and renders the literal "0.0" text this gate exists
        # to prevent.
        return 0
    return int(num) if num.is_integer() else num


def _fmt_float(x):
    """One shared fixed-precision float formatter (§4.6) for every SVG/text number.
    Control 2: a non-finite/out-of-range value formats as 0.00 rather than emitting the
    literal token `nan`/`inf` into an SVG coordinate (which silently breaks the path)."""
    coerced = finite_number(x)
    return f"{round(coerced, 2):.2f}" if coerced is not None else "0.00"


# --------------------------------------------------------------------- discovery / load
def find_sidecars(out_dir: Path) -> list[tuple[str, Path]]:
    """[(date_str, Path)] for every `harness-map-YYYY-MM-DD.json` in `out_dir`, sorted
    ascending by date. Filename-regex + lexicographic sort — never mtime/iterdir order
    (§4.1). Explicitly excludes `harness-synthesis-*.json` (Codex F7)."""
    out: list[tuple[str, Path]] = []
    try:
        names = sorted(p.name for p in Path(out_dir).iterdir())
    except OSError:
        return []
    for name in names:
        m = SIDECAR_RE.match(name)
        if m:
            out.append((m.group(1), Path(out_dir) / name))
    return out


def load_sidecar(path: Path) -> tuple[dict[str, Any] | None, str | None]:
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


def _load_sidecar_guarded(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """(doc|None, error|None) for a sidecar the operator did NOT explicitly request —
    the trend-series scan and `select_current`'s latest-valid fallback.

    `load_sidecar` handles only OSError/json.JSONDecodeError, so a deeply-nested file
    raises RecursionError out of `json.loads` and escapes it entirely; before this guard
    that reached `render_from_out_dir`'s envelope and failed the ENTIRE render over a
    file nobody asked for. Every `_RENDER_FALLBACK_ERRORS` member (defined with that
    envelope further down) degrades HERE to the same per-file `skipped[]` reason shape
    `load_sidecar` returns, so the documented invariant — a corrupt sidecar among several
    is excluded and listed in `skipped[]` — finally holds for more than JSON-syntax
    corruption. The explicitly requested date's own sidecar stays FATAL: that branch of
    `select_current` calls `load_sidecar` directly, on purpose."""
    try:
        return load_sidecar(path)
    except _RENDER_FALLBACK_ERRORS as exc:
        return None, f"unparseable: {type(exc).__name__}: {exc}"


def load_synthesis(out_dir: Path, date: str) -> tuple[dict[str, Any] | None, str | None]:
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


def select_current(
    sidecars: list[tuple[str, Path]], date: str | None
) -> tuple[str | None, dict[str, Any] | None, list[dict[str, Any]], str | None]:
    """(date_str, doc, skipped[]) — exact-match only when `date` given (typo is FATAL,
    Codex F8); otherwise the LATEST VALID sidecar, using ITS actual date consistently.
    A corrupt sidecar among several is excluded + listed in `skipped[]`; an explicit
    `--date` naming a corrupt sidecar is fatal (never silently substitutes).

    VALID here excludes a collector CRASH ENVELOPE (Codex gate finding 1). `_run_was_measured`
    was applied to the trend series only, so a crash envelope that happened to be the
    NEWEST file was still SELECTED — and `_empty_document`'s eight fabricated zeros then
    rendered as LEAN / COMPLIANT / CLEAN with "No hygiene flags": a confident all-clear
    for a run that measured nothing. That is the identical defect the trend fix closed,
    left half-applied one function upstream.

    Neither branch degrades SILENTLY, because "inaccessible != clean" is this codebase's
    core invariant. The fallback branch skips to the next older MEASURED sidecar and
    publishes the skip in `skipped[]` (rendered in the provenance footer), exactly as it
    already does for a corrupt file; if nothing measured survives, the error names every
    skip. The explicit-`--date` branch is FATAL instead of substituting, because F8
    forbids answering with a date the operator did not ask for."""
    skipped: list[dict[str, Any]] = []
    if date is not None:
        match = next((p for d, p in sidecars if d == date), None)
        if match is None:
            return None, None, skipped, f"no sidecar found for date {date}"
        doc, err = load_sidecar(match)
        if err is not None:
            return None, None, skipped, f"sidecar for {date} is corrupt: {err}"
        # load_sidecar's contract: err is None iff doc is populated.
        if not _run_was_measured(cast(dict[str, Any], doc)):
            return None, None, skipped, f"sidecar for {date} is a {CRASH_ENVELOPE_REASON}"
        return date, doc, skipped, None
    for d, p in reversed(sidecars):
        doc, err = _load_sidecar_guarded(p)
        if err is not None:
            skipped.append({"date": d, "reason": err})
            continue
        if not _run_was_measured(cast(dict[str, Any], doc)):
            skipped.append({"date": d, "reason": CRASH_ENVELOPE_REASON})
            continue
        return d, doc, skipped, None
    return None, None, skipped, _no_valid_sidecar_message(skipped)


def _no_valid_sidecar_message(skipped: list[dict[str, Any]]) -> str:
    """The fatal message when every candidate was excluded. `skipped[]` is returned but
    NOT rendered on this path — there is no page to render it into — so the reasons ride
    the message instead. Without them, a directory holding nothing but crash envelopes
    reported the same bare line as an empty one, and the operator could not tell "your
    collector has been crashing" from "you have no data"."""
    if not skipped:
        return "no valid sidecar found"
    detail = "; ".join(f'{s.get("date", "")}: {s.get("reason", "")}' for s in skipped)
    return f"no valid sidecar found ({detail})"


# ------------------------------------------------------------ metric definition versions
# S6b §8.1 — CLOSED and FROZEN. This table covers ONLY sidecars that predate the
# collector's `metric_definitions` marker. It is NEVER appended to: after the first marked
# sidecar the collector's own map is authoritative and this table is history. Freezing it
# is what stops it rotting — a table that needs a human to remember to append a row on
# every detector change silently falls behind and then reports a genuinely confounded
# series as comparable, a false negative in the reassuring direction.
#
# KEYED BY SHA256 CONTENT DIGEST OF THE SIDECAR BYTES, NEVER BY DATE (finding #7).
# Date-based provenance assumes filenames are truthful, files are immutable once written,
# an old collector is never rerun under a backfilled date, and no same-day overwrite
# occurs. This repo violates two of those: report files ARE mutated, and a same-date
# overwrite is on record (two 2026-07-31 runs, 18:37 and 20:04, one filename). An artifact
# whose digest is not in this table resolves to UNKNOWN, never to an inferred version.
# There is deliberately no LEGACY_WINDOW_END and no date fallback of any kind.
LEGACY_METRIC_DEFINITIONS: dict[str, dict[str, int]] = {
    # 2026-07-17 — pre-S1.M0
    "d66dfa906ab488a5876b2dae6195903148de88907b639d1b2bfd8c98de63e539": {
        "always_loaded_tokens_est": 1,
        "always_loaded_words": 1,
        "always_loaded_file_count": 1,
        "duplicate_pair_count": 1,
        "instruction_files_over_200": 1,
        "orphan_registration_count": 1,
        "orphan_script_count": 1,
        "unchecked_binary_count": 1,
        "promotion_candidate_count": 1,
        "memory_body_count": 1,
        "hooks_with_test_ratio": 1,
        "skills_with_test_ratio": 1,
        "phantom_ref_count": 1,
        "phantom_confirmed_count": 1,
    },
    # 2026-07-23 — pre-S1.M0
    "300b3d48776cfedc58b586548b24134e4ef3db6a85c2b83b8d611cd0431f7b77": {
        "always_loaded_tokens_est": 1,
        "always_loaded_words": 1,
        "always_loaded_file_count": 1,
        "duplicate_pair_count": 1,
        "instruction_files_over_200": 1,
        "orphan_registration_count": 1,
        "orphan_script_count": 1,
        "unchecked_binary_count": 1,
        "promotion_candidate_count": 1,
        "memory_body_count": 1,
        "hooks_with_test_ratio": 1,
        "skills_with_test_ratio": 1,
        "phantom_ref_count": 1,
        "phantom_confirmed_count": 1,
    },
    # 2026-07-24 — S1.M0 sibling probe + S2.M4 slash_command carve-out
    "77f1c3539bbf09db6ba832711cbaa7eec5d29f0d1ff2b94584a9fb30d7fe87c6": {
        "always_loaded_tokens_est": 1,
        "always_loaded_words": 1,
        "always_loaded_file_count": 1,
        "duplicate_pair_count": 1,
        "instruction_files_over_200": 1,
        "orphan_registration_count": 1,
        "orphan_script_count": 1,
        "unchecked_binary_count": 1,
        "promotion_candidate_count": 1,
        "memory_body_count": 1,
        "hooks_with_test_ratio": 1,
        "skills_with_test_ratio": 1,
        "phantom_ref_count": 2,
        "phantom_confirmed_count": 2,
    },
    # 2026-07-31 — S2-gate D2
    "74fd26ee34a7a32f9589af1962e4a8e6830d1211feae1c72ad25bb3802e08fec": {
        "always_loaded_tokens_est": 1,
        "always_loaded_words": 1,
        "always_loaded_file_count": 1,
        "duplicate_pair_count": 1,
        "instruction_files_over_200": 1,
        "orphan_registration_count": 1,
        "orphan_script_count": 1,
        "unchecked_binary_count": 1,
        "promotion_candidate_count": 1,
        "memory_body_count": 1,
        "hooks_with_test_ratio": 1,
        "skills_with_test_ratio": 1,
        "phantom_ref_count": 3,
        "phantom_confirmed_count": 3,
    },
    # 2026-08-01 — catch-all: any later markerless sidecar predating this branch, nothing
    # changed the detector between 07-31 and S6b.
    "e9b416d6771a89b5e01af76d539aa54825373d88c26a1ac00838ee76f3273e16": {
        "always_loaded_tokens_est": 1,
        "always_loaded_words": 1,
        "always_loaded_file_count": 1,
        "duplicate_pair_count": 1,
        "instruction_files_over_200": 1,
        "orphan_registration_count": 1,
        "orphan_script_count": 1,
        "unchecked_binary_count": 1,
        "promotion_candidate_count": 1,
        "memory_body_count": 1,
        "hooks_with_test_ratio": 1,
        "skills_with_test_ratio": 1,
        "phantom_ref_count": 3,
        "phantom_confirmed_count": 3,
    },
}


def _valid_definition_version(value: Any) -> bool:
    """A version is a positive int and NOT a bool. `True == 1` in Python, so a stray
    boolean would silently resolve as version 1 and report a series comparable when it is
    not. Anything failing this is UNKNOWN, never a default."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def resolve_metric_definition_version(
    doc: dict[str, Any] | None,
    sidecar_bytes: bytes,
    metric: str,
    legacy: dict[str, dict[str, int]] | None = None,
) -> int | None:
    """Definition version for one (sidecar, metric), or None meaning UNKNOWN.

    Resolution order, exactly (§8.1):
      1. `doc["metric_definitions"][metric]` if present AND it passes strict validation;
      2. the sidecar's sha256 CONTENT DIGEST looked up in the frozen legacy table;
      3. None (UNKNOWN).
    There is NO date fallback and NO silent default of 1 — an unrecognized artifact is
    unknown, and saying so is the whole point of the mechanism.

    Derived metrics (phantom_confirmed_count, the ratios) inherit the COLLECTOR definition
    version of their underlying data. A separate renderer-derivation version was
    considered and dropped: it is identical across every sidecar in a window (it is the
    running code), so it can never differ and therefore has zero detection power. Do not
    reintroduce it.

    `legacy` is a parameter so tests can supply their own frozen table without patching a
    module constant — the table's real contents are one operator's historical artifacts
    and cannot be reproduced hermetically."""
    table = LEGACY_METRIC_DEFINITIONS if legacy is None else legacy
    declared = (doc or {}).get("metric_definitions")
    if isinstance(declared, dict) and metric in declared:
        value = declared[metric]
        return value if _valid_definition_version(value) else None
    digest = hashlib.sha256(sidecar_bytes).hexdigest()
    value = table.get(digest, {}).get(metric)
    return value if _valid_definition_version(value) else None


def series_confounded(versions: list[int | None]) -> bool:
    """A series is confounded iff its resolved versions across the window hold more than
    one distinct value, OR any resolved version is None (UNKNOWN).

    Takes VERSIONS ONLY, by signature — it can see no metric values at all. That is
    deliberate and load-bearing: see §8.4 and `test_no_value_shape_heuristic_is_possible`.

    THE MARKER'S FIRST APPEARANCE IS NOT A TRANSITION. An `absent -> N` step is the
    marker's introduction, not an observed change in how the metric was computed. Flagging
    there would fire a confound on EVERY metric on the run the marker ships — 100% noise
    exactly when the mechanism is introduced. Callers pass only RESOLVED versions; a
    sidecar with no marker resolves through the legacy table, and only a genuine digest
    miss yields None.

    TOTALITY (T5.1): `resolved is None` was the only guarded case, so `set(resolved)`
    hashed every OTHER element unconditionally — a hand-crafted or corrupt element
    (`[1,2]`, `{}`) raised `TypeError: unhashable type` instead of degrading, the same
    defect class `_phantom_guidance`/`_phantom_group_key`/`_phantom_status_word` were
    already fixed for. An element that is neither an int (excluding `bool`, since
    `True == 1` in Python — same reasoning as `_valid_definition_version`) nor `None` is
    not a version this function was built to reason about. It is treated the SAME as
    UNKNOWN (returns True): a value we cannot interpret gives no more evidence of a
    stable definition than a value we never saw, and folding it into the honest
    "cannot claim uniform" outcome is the conservative choice already made for `None` —
    never a silently invented default."""
    resolved = list(versions)
    if any(v is None or not (isinstance(v, int) and not isinstance(v, bool))
           for v in resolved):
        return True
    return len(set(resolved)) > 1


# Verdict words the reason string must never contain — the renderer states FACTS
# (dates, version numbers); the judgment stays the model's (binding rule 6).
_CONFOUND_REASON_FORBIDDEN = ("worse", "better", "improving", "worsening", "broken",
                              "good", "bad", "healthy", "degraded", "regressed")


def build_confounded_reason(
    metric: str, dated_versions: list[tuple[str, int | None]]
) -> str:
    """Factual reason string for a confounded series: dates and version numbers only, no
    verdict word. `dated_versions` is an ordered list of (date_str, version|None).

    Sorted by date-as-string then version-as-string for cross-PYTHONHASHSEED byte
    determinism.

    TOTALITY (T5.1): the sort key used to compare `p[0]` directly, so a malformed date
    (e.g. `None`, from a hand-crafted or corrupt tuple) raised `TypeError: '<' not
    supported between instances of 'str' and 'NoneType'` the moment the window held a
    second, differently-typed date to compare it against — the same "raise instead of
    degrade" defect class fixed elsewhere in this module. Coercing with `str(...)`, same
    as the version half already does, makes the key comparable across any input shape.
    For every REAL date (already a `str`) `str(date) == date`, so this is a no-op on
    valid input — the sort ORDER and the emitted text (`date` is still used un-coerced in
    the f-string below) are byte-identical to before."""
    parts = [f"{date}: definition v{version}" if version is not None
             else f"{date}: definition unknown"
             for date, version in sorted(dated_versions,
                                          key=lambda p: (str(p[0]), str(p[1])))]
    return f"{metric} — " + "; ".join(parts)


# --------------------------------------------------------------------------- node keys
def _al_node_key(path, tier="operator"):
    """T12 (P2 fix): operator and project roots each surface files at the SAME
    relative path (an operator `CLAUDE.md` and a project `CLAUDE.md` both give
    `path == "CLAUDE.md"`), so the plain `always_loaded:{path}` key used to collide --
    friction-join/tooltip/click couldn't tell the two apart. The OPERATOR format is
    UNCHANGED (`tier` unrecognized/absent also falls through to it, via
    `_normalize_tier`) so the friction telemetry-join -- which is operator-tier only,
    §_canonical_ref_candidates -- keeps matching exactly as before; only project-tier
    entries gain the `project:` discriminator segment."""
    if _normalize_tier(tier) == "project":
        return f"always_loaded:project:{path}"
    return f"always_loaded:{path}"


def _od_node_key(rel):
    return f"on_demand:{rel}"


def _hook_node_key(name):
    return f"hook:{Path(name).name}"


def _dup_node_key(path, tier="operator"):
    """Map a duplication-corpus path onto the SAME node_key an existing view already
    uses (§1.3) so friction/dup heat lands on one identity, not a shadow duplicate.
    `tier` (T12 P2) is forwarded to the `_al_node_key` (rules/) branch, so a
    project-tier rule's dup/length-crit identity matches the tier-disambiguated
    treemap node_key `_tokens_treemap` now emits for it -- `.claude/rules/*.md` is the
    ONE fixed shape `_walk_project_tier` (collector.py) ever writes a project rule at,
    so it's gated behind `tier == "project"` (never matched for the default/operator
    tier, so `build_dupweb_model`/`_canonical_ref_candidates` -- neither of which pass
    `tier` -- see byte-identical pre-T12 behavior; extending dup-web itself to be
    tier-aware is out of T12's scope, see the T12 report)."""
    if re.match(r"^(rules/|skills/[^/]+/rules/)", path) or (
            tier == "project" and re.match(r"^\.claude/rules/", path)):
        return _al_node_key(path, tier)
    m = re.match(r"^skills/([^/]+)/SKILL\.md$", path)
    if m:
        return _od_node_key(m.group(1))
    if re.match(r"^skills/[^/]+/(phases|prompts|agents)/", path):
        return _od_node_key(path)
    return f"dup:{path}"


def _basename_of_node_key(node_key):
    _, _, rel = node_key.partition(":")
    return Path(rel or node_key).name.lower()


def _length_critical_node_keys(doc):
    """`{node_key: lines}` for files hygiene's OWN length-flag table classifies as
    `critical` (lines > LENGTH_CRITICAL_LINES) — the SAME per-file classification
    `_render_length_flags_body` renders as the pill-critical row, reused (never a
    new threshold) so the treemap's length-crit outline and the Hygiene tab's
    critical pill can never silently disagree. The `lines` value lets the
    treemap/ladder `<title>` explain WHY a cell is ringed (a plain node_key set
    can't); membership tests (`c["node_key"] in length_crit_keys`) still work
    unchanged against a dict's keys. v1 scope is deliberately CRITICAL-only, not
    the lesser `over`-cap tier — a blanket outline on every over-cap file would be
    noisy; the point is the standout. Reuses `_dup_node_key` (built for the same
    repo-relative path format the dup-web corpus uses) — a path outside its known
    patterns (rules/, skills/*/SKILL.md, skills/*/{phases,prompts,agents}/*) falls
    back to a `dup:`-prefixed key that won't match any real treemap cell, a silent
    no-op rather than a crash. `f.get("tier", "operator")` (T12 P2) is forwarded so a
    project-tier flag's key matches the SAME tier-disambiguated node_key
    `_tokens_treemap` now gives that file, instead of desyncing from it."""
    flags = doc.get("instruction_length_flags", []) or []
    return {_dup_node_key(f["path"], f.get("tier", "operator")): f.get("lines", 0)
            for f in flags if f.get("lines", 0) > LENGTH_CRITICAL_LINES}


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


def squarify(
    items: list[dict[str, Any]], x: float, y: float, w: float, h: float
) -> list[dict[str, Any]]:
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
def _tokens_treemap(
    files: list[dict[str, Any]], canvas_w: float = 960.0, canvas_h: float = 420.0
) -> dict[str, Any]:
    # QA exit gate (MEDIUM 2): `load_sidecar` does no row validation, so `files[]` can hold
    # a NON-DICT -- on which even `f.get(...)` raises AttributeError. That is in
    # `_RENDER_FALLBACK_ERRORS`, so T3's envelope would convert ONE malformed row into a
    # RenderError that kills the whole dashboard, under a comment blaming a defect in this
    # module. A row with no readable fields has no size and no path: it degrades to the
    # disclosure built for exactly this. The TYPE name is disclosed, never the value --
    # a hostile sidecar's contents never reach the page through this path.
    malformed = [f for f in files if not isinstance(f, dict)]
    rows = [f for f in files if isinstance(f, dict)]
    # R4-3: total accessor — this comprehension runs over the COMPLETE input, including
    # rows that previously never reached f["path"] (their category died at the
    # `tokens <= 0` gate before the per-file loop). load_sidecar does no row validation,
    # so a pathless row must not turn the disclosure feature into a new KeyError.
    # Codex gate finding 5: `nonneg_number`, not `finite_number`. A NEGATIVE tokens_est
    # passes the finite gate, contributes negatively to the category sum, and could take
    # the whole category below the `tokens <= 0` gate below -- deleting drawable rows with
    # no trace. It has no area either way, so it belongs in this disclosure.
    unrenderable = sorted([(f.get("path") or "(unknown path)") for f in rows
                           if nonneg_number(f.get("tokens_est", 0)) is None]
                          + [f"(malformed entry: {type(f).__name__})" for f in malformed])
    by_cat: dict[Any, list[dict[str, Any]]] = {}
    for f in rows:
        by_cat.setdefault(f.get("category"), []).append(f)
    groups = []
    all_cells = []
    group_items = []
    for cat, label in ALWAYS_CATEGORIES:
        cat_files = by_cat.get(cat, [])
        tokens = sum(_gated_size(f.get("tokens_est", 0)) for f in cat_files)
        if tokens <= 0:
            continue
        group_items.append({"size": tokens, "category": cat, "label": label, "file_count": len(cat_files)})
    group_rects = squarify(sorted(group_items, key=lambda g: (-g["size"], g["category"])),
                            0.0, 0.0, canvas_w, canvas_h)
    for g in group_rects:
        groups.append(g)
        # The SORT KEY must be gated too (Codex F4) — `-f.get(...)` on a str is a TypeError.
        # QA exit gate (MEDIUM 2): the PATH accessors are gated too, at all three sites. A
        # pathless row with a usable size clears the `tokens <= 0` gate and reaches HERE,
        # where a bare `f["path"]` was a KeyError -> RenderError -> no dashboard. Such a
        # row is drawable (it has area), so it is LABELLED rather than disclosed --
        # `unrenderable` says "no usable size value", which would be false for it.
        cat_files = sorted(
            by_cat.get(g["category"], []),
            key=lambda f: (-_gated_size(f.get("tokens_est", 0)), f.get("path") or "(unknown path)"))
        cell_items = [{"size": _gated_size(f.get("tokens_est", 0)),
                       "words": _gated_size(f.get("words", 0)),
                       "path": (f.get("path") or "(unknown path)"),
                       "node_key": _al_node_key(f.get("path") or "(unknown path)",
                                                f.get("tier", "operator")),
                       "tier": f.get("tier", "operator")}
                      for f in cat_files]
        cells = squarify(cell_items, float(g["x"]), float(g["y"]), float(g["w"]), float(g["h"]))
        for c in cells:
            # Demo parity #5 (B-t3 follow-up): one hue per SECTION, not per category —
            # depth (size->fill-opacity, unchanged) is now the only within-section
            # differentiator. `var(--accent)` tracks the light/dark theme token
            # automatically (inline SVG honors CSS custom properties under file://).
            c["fill"] = "var(--accent)"
            c["category"] = g["category"]
        all_cells.extend(cells)
    return {"groups": groups, "cells": all_cells, "unrenderable": unrenderable,
            "canvas_w": canvas_w, "canvas_h": canvas_h}


def _on_demand_treemap(
    doc: dict[str, Any], canvas_w: float = 960.0, canvas_h: float = 420.0
) -> dict[str, Any]:
    on_demand = doc.get("on_demand", {}) or {}
    items_by_group: dict[str, list[dict[str, Any]]] = {g: [] for g, _ in ON_DEMAND_GROUPS}
    unrenderable: list[str] = []

    def _add(group: str, raw_size: Any, path: str, tier: str) -> None:
        """Control 2, same gate as `_tokens_treemap`: gating `size` HERE covers all three
        downstream arithmetic sites at once (the group sum, the `-i["size"]` sort key, and
        squarify). An item with no usable size has no AREA and cannot be drawn, so it is
        DISCLOSED rather than dropped — without this, gating alone would trade the
        str-sum TypeError for exactly the silent deletion S4 names (squarify filters
        `size > 0`, which is False for NaN). Codex gate finding 5: the gate is
        `nonneg_number` — a negative word count has no area either, and would otherwise
        skew its group's sum silently."""
        if nonneg_number(raw_size) is None:
            unrenderable.append(path or "(unknown path)")
            return
        items_by_group[group].append({"size": _gated_size(raw_size), "path": path,
                                       "node_key": _od_node_key(path), "tier": tier})

    for s in on_demand.get("skills", []) or []:
        _add("skill", s.get("words", 0), s.get("name", ""), s.get("tier", "operator"))
    for b in on_demand.get("skill_internal_bodies", []) or []:
        kind = b.get("kind")
        if kind in items_by_group:
            _add(kind, b.get("words", 0), b.get("path", ""), b.get("tier", "operator"))
    for m in on_demand.get("memory_bodies", []) or []:
        _add("memory", m.get("words", 0), m.get("path", ""), m.get("tier", "operator"))
    unrenderable.sort()
    group_items = []
    for g, label in ON_DEMAND_GROUPS:
        total = sum(i["size"] for i in items_by_group[g])
        if total <= 0:
            continue
        group_items.append({"size": total, "category": g, "label": label, "file_count": len(items_by_group[g])})
    group_rects = squarify(sorted(group_items, key=lambda g: (-g["size"], g["category"])),
                            0.0, 0.0, canvas_w, canvas_h)
    all_cells = []
    for rect in group_rects:
        cell_items = sorted(items_by_group[rect["category"]], key=lambda i: (-i["size"], i["path"]))
        cells = squarify(cell_items, float(rect["x"]), float(rect["y"]), float(rect["w"]), float(rect["h"]))
        for c in cells:
            # Demo parity #5 (B-t3 follow-up): a distinct but still COOL hue from the
            # always-loaded section (never crit-red/good-green — the friction-heat
            # overlay's red stroke ramp needs the base fill to stay out of its way).
            c["fill"] = "var(--accent-2)"
            c["category"] = rect["category"]
        all_cells.extend(cells)
    return {"groups": group_rects, "cells": all_cells, "unrenderable": unrenderable,
            "canvas_w": canvas_w, "canvas_h": canvas_h}


def build_contextweight_model(doc: dict[str, Any]) -> dict[str, Any]:
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


def build_bipartite_model(doc: dict[str, Any]) -> dict[str, Any]:
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
          "is_symlink": bool(s.get("is_symlink", False)),
          "description": s.get("description", "")} for s in scripts_on_disk],
        key=lambda n: n["node_key"])
    edges = sorted(
        [{"from": _hook_node_key(r["script"]), "to": _hook_node_key(r["script"])}
         for r in registered if r.get("registered_via") == "direct"],
        key=lambda e: e["from"])
    return {"left": left, "left_orphans": left_orphans, "right": right, "edges": edges,
            "orphan_script_count": len(orphan_scripts)}


# The collector's crash marker, read at the other end of a cross-module string contract
# (collector._CRASH_ERROR_PREFIX is the writer; the two are pinned equal by a test).
CRASH_ERROR_PREFIX = "collector crashed: "
# What the page prints where a metric has no measurement. NOT "0" and NOT "" — a blank
# cell reads as a rendering bug, a zero reads as a measurement nobody made.
# Codex gate finding 3: ONE constant for every surface. The trend table learned this
# treatment first; the CURRENT gauge and the Overview digest kept doing `.get(key, 0)`,
# so a schema-1 sidecar lacking a headline key was honestly unmeasured in the trend and
# simultaneously `0 / CLEAN` in the gauge — the same contradiction-on-one-page shape as
# the phantom-gauge bug. Two homes for the string would let them drift apart again.
NOT_MEASURED_TEXT = "—"
# The trend table's original name for it, kept because existing assertions read it.
TREND_NOT_MEASURED_TEXT = NOT_MEASURED_TEXT

# What `skipped[]` records for a sidecar that is a collector CRASH ENVELOPE rather than a
# measurement (Codex gate finding 1). Shared by the skip path and the fatal message so a
# reader of either learns the same thing.
CRASH_ENVELOPE_REASON = (
    "collector crash envelope — the run measured nothing, so its all-zero headline is "
    "not a measurement"
)


def _run_was_measured(doc: dict[str, Any]) -> bool:
    """False for a collector CRASH ENVELOPE — a run that measured nothing.

    `_empty_document` sets all eight headline keys to 0 and `main()` writes that envelope
    to `--out` as an ordinary dated sidecar, so it passes `load_sidecar` (dict +
    schema_version) and matches on `root`. Joined to the trend series it contributes
    eight measured-looking zeros; as the latest point that renders a GREEN "improving"
    delta for every polarity=="up" metric — a fabricated verdict in the reassuring
    direction, which is the failure mode the A17 numeric guard exists to prevent.

    CRASH MARKER ONLY, not "errors[] is non-empty". `errors[]` also carries benign
    per-surface warnings (a failed glob, an unparseable settings.json) from runs that
    measured everything else correctly; disqualifying those would silently drop most real
    runs from the series and quietly shrink the operator's history. The crash marker is
    the one entry that means the document is an envelope rather than a measurement.

    Defensive on shape (the doc is untrusted sidecar JSON): a non-list `errors` is
    wrapped, and non-string entries are skipped rather than raising."""
    errors = doc.get("errors") or []
    entries = errors if isinstance(errors, list) else [errors]
    return not any(isinstance(entry, str) and entry.startswith(CRASH_ERROR_PREFIX)
                   for entry in entries)


def build_trend_model(dated_docs: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    """(c) Trend: 8 headline series across ALL loaded sidecars in `--out-dir`
    (filtered to the SELECTED sidecar's `root`, Codex F13).

    Each series carries TWO lists, and they are not redundant:
      `values` is aligned 1:1 with `dates` for the per-date table, holding `None` where
      the key was absent (the renderer prints `TREND_NOT_MEASURED_TEXT` there — the
      model stays data, the display string stays in the renderer).
      `points` is the measured subsequence — the ONLY input to sparkline geometry and
      the delta verdict. A missing headline key means "not measured", so the point is
      DROPPED rather than defaulted to 0: a sparkline over fewer real points is honest,
      a zero is a fabricated measurement. Both are built in one pass here, so they
      cannot disagree.

    A non-numeric but PRESENT value stays in `points` deliberately: it was measured and
    reported, it is just unusable, and `_coerce_floats`/`finite_number` already reject
    the whole window for it. Dropping it here instead would silently narrow that
    all-or-nothing guard into a partial line."""
    measured = [(date, doc) for date, doc in dated_docs if _run_was_measured(doc)]
    dates = [date for date, _ in measured]
    series: list[dict[str, Any]] = []
    for key, label, polarity in HEADLINE_KEYS:
        values: list[Any] = []
        points: list[Any] = []
        for _date, doc in measured:
            headline = doc.get("headline")
            if not isinstance(headline, dict) or key not in headline:
                values.append(None)
                continue
            values.append(headline[key])
            points.append(headline[key])
        series.append({"key": key, "label": label, "polarity": polarity,
                       "values": values, "points": points})
    return {"dates": dates, "series": series, "first_run": len(measured) <= 1}


def build_dupweb_model(doc: dict[str, Any]) -> dict[str, Any]:
    """(d) Duplication web: dedup node set (lex-sorted) + edges in pair order +
    phantom_refs table.

    F1 (T13 QA): the collector already emits `pair["a_tier"]`/`["b_tier"]` in compose
    mode, but this builder used to discard them — the tier filter toggle had NOTHING
    to dim on a cross-tier dup pair, the ONE M4 view whose whole purpose is cross-tier
    duplication. Each edge now also carries `a_tier`/`b_tier` (`_normalize_tier`
    defaults an absent/unrecognized value to "operator" — C15 back-compat for a
    pre-tier or non-compose sidecar) and the RAW `a_path`/`b_path` (unprefixed, unlike
    `a`/`b` which stay the existing `_dup_node_key`-format node_key an existing
    consumer — `_collect_node_keys`, the markdown copy export — already reads that way,
    so those two fields are left untouched)."""
    dup = doc.get("duplication", {}) or {}
    pairs = dup.get("pairs", []) or []
    node_paths = sorted({p for pair in pairs for p in (pair["a"], pair["b"])})
    nodes = [{"node_key": _dup_node_key(p), "path": p} for p in node_paths]
    edges = [{"a": _dup_node_key(pair["a"]), "b": _dup_node_key(pair["b"]),
              "a_path": pair["a"], "b_path": pair["b"],
              "a_tier": _normalize_tier(pair.get("a_tier")),
              "b_tier": _normalize_tier(pair.get("b_tier")),
              "score": pair.get("score", 0.0), "shared_sample": pair.get("shared_sample", "")}
             for pair in pairs]
    return {"nodes": nodes, "edges": edges, "threshold": dup.get("threshold", 0.6),
            "metric": dup.get("metric", "containment"), "phantom_refs": doc.get("phantom_refs", []) or []}


def build_civc_model(synth: dict[str, Any] | None) -> dict[str, Any]:
    """CIVC 6x6 grid. Absent synthesis -> graceful empty-state (`available=False`,
    §6). A malformed cell set never crashes: missing cells fall back to 'empty'.
    `verdict` is allowlisted to VERDICTS here — the ONE normalization point every
    consumer (Coverage matrix cells, copy payloads) reads from — so an unallowlisted
    synthesis value (e.g. a crafted `"covered fh1 heatable"`) can never ride through
    as an extra CSS class (Codex P1 class-injection finding)."""
    if synth is None:
        return {"available": False, "cells": []}
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for c in synth.get("civc", []) or []:
        if isinstance(c, dict) and c.get("verb") in VERBS and c.get("surface") in SURFACES:
            by_key[(c["verb"], c["surface"])] = c
    cells = []
    for verb in VERBS:
        for surface in SURFACES:
            c = by_key.get((verb, surface), {})
            verdict = c.get("verdict", "empty")
            if verdict not in VERDICTS:
                verdict = "empty"
            cells.append({"verb": verb, "surface": surface,
                           "verdict": verdict,
                           "evidence": c.get("evidence"), "note": c.get("note", "")})
    return {"available": True, "cells": cells}


def build_dragcandidate_model(synth: dict[str, Any] | None) -> dict[str, Any]:
    """Drag-candidate table. Absent synthesis -> graceful empty-state."""
    if synth is None:
        return {"available": False, "rows": []}
    # Control 2 site (f) is DELIBERATELY NOT APPLIED here — see the S2 gate-fix report.
    # Two existing serve tests inject a mixed int/str `n` precisely BECAUSE this sort key
    # raises TypeError, and use it as their "unenumerated exception" vector:
    # test_serve.py::test_watcher_survives_uncaught_exception (the watcher backstop must
    # survive a non-CollectorError/RenderError/OSError) and
    # test_serve.py::test_startup_malformed_synthesis_clean_fatal (main() must exit 1 with
    # a clean fatal). Gating this key makes the render SUCCEED, which breaks the first and
    # HANGS the second (serve starts and never returns). Landing site (f) requires giving
    # those two tests a new injection vector first — a design decision, not a local fix.
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
    no severity). First band whose `upper` is None or value <= upper wins. Control 2: a
    non-finite/non-numeric value is NOT silently banded — NaN compares False against
    every `upper` and fell through to the LAST band ('HEAVY','bad'), inventing a severity
    verdict from corruption.

    Codex gate finding 5: the gate is `nonneg_number`. EVERY key in GAUGE_BANDS is a size
    or a count, so a negative is corruption of the same class as a NaN — and a strictly
    more dangerous one, because it lands in the REASSURING direction: `-1 <= 0` painted
    the green CLEAN / COMPLIANT / LEAN verdict. It gets the same no-verdict neutral a NaN
    already gets, while the raw value still DISPLAYS, so the operator sees the -1."""
    bands = GAUGE_BANDS.get(key)
    num = nonneg_number(value)
    if not bands or num is None:
        return ("", "neutral")
    for upper, label, semantic in bands:
        if upper is None or num <= upper:
            return (label, semantic)
    return bands[-1][1], bands[-1][2]


def friction_total(
    joined: dict[str, Any],
    codex_aggregate: dict[str, Any],
    metrics_aggregate_only: int = 0,
) -> int:
    """AM-1 gauge value: total friction events across the 4 streams = joined telemetry
    records (decisions/metrics/interventions) + codex runs + metrics-eligible records
    that resolved to NO node (`metrics_aggregate_only`). This is a JOIN-EVENT count, not
    a unique-source-record count — do NOT dedupe. UPDATED SEMANTICS (§C1): a basename- or
    path-ambiguous ref now heats NONE (DECISION 6 is superseded — ambiguity used to fan
    out heat to every matching node and count N; it no longer does, since that was the
    subtree-smear bug). To keep the eligible-but-unattributed signal from silently
    disappearing when its record joins no node, `join_metrics`'s `records_aggregate_only`
    count is folded in here — this same value is rendered as the Friction view's headline
    total (Task 8), so the header gauge and the Friction view show ONE consistent friction
    number rather than two disagreeing totals."""
    return sum(len(v) for v in joined.values()) + codex_aggregate.get("runs", 0) + metrics_aggregate_only


def _metrics_aggregate_only(footer):
    """Pulls `join_metrics`'s aggregate-only count back out of the friction footer (the
    single place it's already computed, §C1 change 3) — never re-derived independently."""
    return next((f.get("records_aggregate_only", 0) for f in footer if f["stream"] == "metrics"), 0)


def _friction_contributions(joined, footer, codex_aggregate):
    """Friction-gauge drill breakdown that PROVABLY reconciles: the returned counts sum
    EXACTLY to friction_total(joined, codex_aggregate, _metrics_aggregate_only(footer)).
    We split by the three terms friction_total itself adds — NOT by stream, because the
    `joined` records carry no stream tag and join_metrics appends per-alias (so a per-
    stream split would not sum to the joined total). Deterministic fixed order."""
    return [
        ("Telemetry events joined to a component", sum(len(v) for v in joined.values())),
        ("Metrics events not attributed to a component (aggregate-only)",
         _metrics_aggregate_only(footer)),
        ("Codex review runs (not node-joined)", codex_aggregate.get("runs", 0)),
    ]


def _phantom_counts(doc: dict[str, Any]) -> tuple[int, int]:
    """(total, confirmed) phantom-ref counts — the ONE derivation of the R2-F6/R3-2 split.

    QA exit gate (HIGH 1): the split shipped inline at the header-gauge call site only, so
    `build_overview_model` kept banding the Overview digest on the TOTAL and painted a red
    dot over rows the collector explicitly declined to verify — the same metric, two
    contradictory severities on one page. Both consumers now read the pair from HERE, so a
    third consumer cannot re-acquire the total-only half of it.

    DISPLAY uses the total (every row is a row the operator should see counted); the
    CLEAN/BROKEN BAND uses `confirmed` (resolved=False rows only), because banding a
    resolved=null row BROKEN is a verdict the renderer has no evidence for (rule 6)."""
    rows = doc.get("phantom_refs", []) or []
    return len(rows), sum(1 for r in rows if r.get("resolved") is False)


def build_overview_model(
    models: dict[str, Any],
    headline: dict[str, Any],
    phantom_ref_count: int,
    friction_total_value: int | str,
    phantom_confirmed_count: int | None = None,
) -> dict[str, Any]:
    """A3/AM-2 digest — pure aggregation over already-built models. No new data derived:
    roadmap gaps = empty civc cells; weight tax = top always-loaded files by size;
    hygiene = headline counts; drag = synthesis rows; friction hero = count + band + top drag.

    `phantom_confirmed_count` is the BAND input for the phantom row, mirroring
    `_render_instrument_readout`'s `band_value` (see `_phantom_counts`). It is optional
    ONLY because an existing assertion calls this function without it and pins the
    `hygiene` dict by exact equality (binding rule 7 forbids editing either); None means
    "not supplied" and preserves the pre-split behaviour of banding on the displayed
    total. Every production call site passes it.

    `friction_total_value` is `int | str` because a truncated read displays `≥N`
    (`_friction_total_display`). That is not a widening for its own sake: the string is what
    SUPPRESSES the band, since `_gauge_band` gives any non-numeric value ("", "neutral")."""
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
        # Codex gate finding 3: same rule as the header gauge — an absent headline key is
        # UNMEASURED, not 0. The digest's severity dot reads these through `_gauge_band`,
        # which gives the non-numeric marker a neutral no-verdict, so an unmeasured metric
        # can no longer wear a green dot beside a `—` in the trend table.
        "hygiene": {"over_cap": headline.get("instruction_files_over_200", NOT_MEASURED_TEXT),
                    "dup_pairs": headline.get("duplicate_pair_count", NOT_MEASURED_TEXT),
                    "phantom_refs": phantom_ref_count},
        # Severity inputs that DIVERGE from the displayed number, keyed by `hygiene` key.
        # A key absent here means displayed value == band value (the normal case).
        "hygiene_band_values": ({} if phantom_confirmed_count is None
                                else {"phantom_refs": phantom_confirmed_count}),
        "drag_candidates": drag_rows,
        "friction": {"count": friction_total_value, "band": band_label,
                     "semantic": band_semantic, "top_drag": drag_rows[:3]},
    }


def build_copy_payloads(date, models, friction, doc):
    """A8: per-view clean-markdown copy payload. Pure function of inputs (deterministic).
    Rendered into inert <script type='application/json'> islands; read via textContent +
    JSON.parse at click time. Task B-t2 tab merge: the former standalone "coverage"
    payload is now folded into "overview" (one merged tab, one copy button)."""
    heat, joined, footer, codex_aggregate = friction
    civc = models["civc"]
    # --- coverage matrix: markdown table (folded into the "overview" payload below) ---
    header = "| verb \\ surface | " + " | ".join(SURFACES) + " |"
    divider = "|" + "---|" * (len(SURFACES) + 1)
    by_verb: dict[str, dict[str, str]] = {}
    for c in civc["cells"]:
        by_verb.setdefault(c["verb"], {})[c["surface"]] = c["verdict"]
    cov_rows = ["| " + verb + " | "
                + " | ".join(by_verb.get(verb, {}).get(s, "empty") for s in SURFACES) + " |"
                for verb in VERBS]
    coverage_md = "\n".join([header, divider] + cov_rows) if civc.get("available") \
        else (f"_Coverage Matrix unavailable — no `harness-synthesis-{date}.json` in this "
              f"report directory. Re-run `/harness-map` Step B to generate it._")
    # --- friction: sentences ---
    friction_md = "\n".join(f"- {_friction_sentence(f, codex_aggregate)}" for f in footer) \
        or "_Friction overlay disabled._"
    # Finding #2 (post-exec Codex): scoped to the codex stream's own cap, not the
    # run-level bound — same reasoning as `_render_friction_panel`'s codex_html.
    friction_md += "\n\n" + _codex_sentence(codex_aggregate,
                                            truncated=_codex_stream_truncated(footer))
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
    payload_phantom_total, payload_phantom_confirmed = _phantom_counts(doc)
    # The clipboard payload recomputes the friction total INDEPENDENTLY of render_html(),
    # so it must apply the same lower-bound display — otherwise the thing the operator
    # pastes into a ticket carries a bare count AND a severity band for the very run whose
    # dashboard suppresses both (the two-homes divergence A3 already cost this codebase).
    over = build_overview_model(models, doc.get("headline", {}) or {},
                                payload_phantom_total,
                                _friction_total_display(
                                    friction_total(joined, codex_aggregate,
                                                   _metrics_aggregate_only(footer)), footer),
                                phantom_confirmed_count=payload_phantom_confirmed)
    # A suppressed band is NO parenthetical, never an empty "()" — the same shape
    # `_render_friction_hero` uses, so page and payload stay in step.
    band_md = f" ({over['friction']['band']})" if over["friction"]["band"] else ""
    overview_md = (f"# harness-map {date}\n\n"
                   f"- roadmap gaps: {len(over['roadmap_gaps'])}\n"
                   f"- friction events: {over['friction']['count']}{band_md}\n"
                   f"- over-cap files: {over['hygiene']['over_cap']}, "
                   f"dup pairs: {over['hygiene']['dup_pairs']}, "
                   f"phantom refs: {over['hygiene']['phantom_refs']}\n\n"
                   f"## Coverage Matrix\n\n{coverage_md}")
    return {"overview": overview_md, "weight": weight_md,
            "friction": friction_md, "hygiene": hygiene_md}


# ---------------------------------------------------------------------------- node index
def _collect_node_keys(models: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    cw = models["context_weight"]
    for tree in (cw["always"], cw["on_demand"]):
        keys.extend(c["node_key"] for c in tree["cells"])
    bp = models["bipartite"]
    keys.extend(n["node_key"] for n in bp["left"])
    keys.extend(n["node_key"] for n in bp["right"])
    keys.extend(n["node_key"] for n in models["dupweb"]["nodes"])
    return keys


def build_node_index(models: dict[str, Any]) -> dict[str, list[str]]:
    """basename(lower) -> sorted [node_key, ...] across every rendered view, used by
    the friction join so a joined basename heats EVERY matching node (§1.3), never
    first-match-wins."""
    index: dict[str, set[str]] = {}
    for key in _collect_node_keys(models):
        b = _basename_of_node_key(key)
        index.setdefault(b, set()).add(key)
    return {b: sorted(v) for b, v in index.items()}


# ------------------------------------------------------------------------------ friction
# NOT the same thing as `collector.py::_LINE_SUFFIX_RE`, and the two must STAY separate:
# this one normalizes TELEMETRY refs for the friction join; that one strips a `:line`
# citation off a phantom-ref PROBE TARGET in a different module on a different input space
# with different failure consequences. Merging them would couple the friction join to the
# phantom detector. If a future simplify pass proposes unifying them, this is the answer.
def _normalize_ref_token(ref):
    """Strips the `:symbol` suffix and ` --flag` tail off a loose telemetry text ref,
    WITHOUT collapsing it to a basename — kept on the full string so a path-bearing ref
    can still be resolved exactly (§C1 change 2) instead of basename-fanning-out."""
    token = ref.split(":")[0].strip()
    return re.split(r"\s+--", token)[0].strip()


def _valid_node_keys(node_index):
    """The flat set of every rendered node_key (union of `build_node_index`'s
    basename -> [node_key,...] values) — the membership test an exact-path or exact-alias
    resolution must pass before it is allowed to heat anything (§C1)."""
    return {k for keys in node_index.values() for k in keys}


def _canonical_ref_candidates(rel):
    """Every canonical node_key a path-bearing telemetry ref could map to, tried via the
    SAME per-view resolvers real nodes are keyed by (§C1 change 2) — never a basename
    fan-out. Sorted for byte-determinism; the caller intersects with the rendered node
    set and requires exactly one survivor."""
    return sorted({_al_node_key(rel), _od_node_key(rel), _hook_node_key(rel), _dup_node_key(rel)})


def _resolve_ref(token, root, node_index, valid_keys):
    """Exact join resolution (§C1 change 2) for ONE already `_normalize_ref_token`-ed
    telemetry ref. Returns ('matched', node_key) | ('ambiguous', None) | ('unmatched',
    None). A path-bearing ref (contains '/') is normalized LEXICALLY against `root`
    (string prefix strip — never filesystem resolve()/realpath) and resolved via the
    canonical per-view node-key resolvers; a bare name still resolves via `node_index`
    (basename -> [node_key,...]). Either way >1 candidate is ambiguous (heats nothing),
    0 candidates is unmatched (heats nothing), exactly 1 is a match."""
    if "/" in token:
        rel = token
        if root:
            root_norm = root.rstrip("/")
            if rel == root_norm:
                rel = ""
            elif rel.startswith(root_norm + "/"):
                rel = rel[len(root_norm) + 1:]
        hits = [k for k in _canonical_ref_candidates(rel) if k in valid_keys]
    else:
        hits = node_index.get(token.lower(), [])
    if not hits:
        return "unmatched", None
    if len(hits) > 1:
        return "ambiguous", None
    return "matched", hits[0]


def _split_component(component: str) -> list[str]:
    segments: list[str] = []
    for part in component.split(" + "):
        segments.extend(p.strip() for p in part.split(", ") if p.strip())
    return segments


def read_jsonl(
    path: Path,
    max_bytes: int = STREAM_MAX_BYTES,
    max_lines: int = STREAM_MAX_LINES,
    *,
    caps_out: dict[str, bool] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """(records, malformed_count, lines_nonblank) — never raises. Disclosed caps guard
    against a FIFO/device/unbounded stream hanging or OOMing the renderer.

    Codex F7 (TOCTOU-safe open): open via os.open(O_RDONLY | O_NONBLOCK) and accept ONLY a
    regular file (stat.S_ISREG on the OPEN fd) — if the path was atomically swapped to a
    FIFO/device after any caller existence check, O_NONBLOCK makes the open return instead of
    blocking forever on a writer-less FIFO, and the fstat check then rejects it (treated as
    absent, the same [], 0, 0 an absent file yields).

    Codex F2 (hard byte cap): read AT MOST `max_bytes + 1` bytes total (binary), so a single
    multi-GB line — or many near-200k-char lines — can never be fully allocated. A stream
    that exceeds `max_bytes` has its overflow tail rejected (never parsed, since it may be cut
    mid-token) and counted once as malformed; complete lines fully inside the budget still
    parse normally. For the common under-cap case the return is byte-for-byte the same records/
    counters as a full read, preserving serve.py's size-based stream-offset heuristic (which
    stats the file itself and never depends on this function's byte accounting).

    Finding #13: `caps_out`, when supplied, receives {"bytes": True} and/or {"lines": True}
    for each cap that actually TRUNCATED the read. It is an out-parameter, not a fourth
    tuple element, deliberately: five existing tests unpack this function as a 3-tuple, and
    a positional addition would break them (binding rule 7) as well as `build_friction_
    overlay`'s own four-value contract (§4.4 item 1 / finding #2). Default None keeps every
    existing call site byte-identical."""
    records: list[dict[str, Any]] = []
    malformed = 0
    nonblank = 0
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return [], 0, 0
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return [], 0, 0  # FIFO/device/dir swapped in after any caller check: treat as absent
        # closefd=False -> the `finally` owns the single close; the wrapper never double-closes.
        with os.fdopen(fd, "rb", closefd=False) as f:
            data = f.read(max_bytes + 1)  # +1 lets us DETECT (not allocate) a past-cap overflow
    except OSError:
        return [], 0, 0
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        # Over the cap: keep only the complete lines fully inside the budget and reject the
        # (possibly truncated) overflow tail — never parse a line that may be cut mid-token.
        data = data[:max_bytes]
        cut = data.rfind(b"\n")
        data = data[:cut + 1] if cut != -1 else b""  # no whole line under the cap -> reject all
        malformed += 1  # the rejected overflow tail counts once as malformed
        if caps_out is not None:
            caps_out["bytes"] = True
    text = data.decode("utf-8", errors="replace")
    segments = text.split("\n")
    if caps_out is not None and any(s.strip() for s in segments[max_lines:]):
        # Real overflow only. A file of exactly max_lines records ending in "\n" splits into
        # max_lines + 1 segments, the last of them empty: the loop below still breaks at the
        # boundary, but NOTHING was dropped. Flagging truncation there would report a blind
        # spot that does not exist and suppress a band that was legitimately earned --
        # false doubt is still a false reading.
        caps_out["lines"] = True
    for i, raw in enumerate(segments):
        if i >= max_lines:
            break
        line = raw.strip()
        if not line:
            continue
        nonblank += 1
        if len(line) > 200_000:
            malformed += 1
            continue
        try:
            rec = json.loads(line)
        except (ValueError, RecursionError):
            # FIX 3: a pathological bare number (e.g. a 5000-digit int) trips Python's
            # int-string conversion limit -> ValueError, which json.JSONDecodeError (a
            # ValueError SUBCLASS) would NOT catch; a deeply-nested array trips RecursionError.
            # Both mean "malformed line", handled identically to a JSON syntax error — never
            # allowed to propagate and fail every rebuild while serving stale state.
            malformed += 1
            continue
        if not isinstance(rec, dict):
            malformed += 1
            continue
        records.append(rec)
    return records, malformed, nonblank


# The keys that carry a record's date, in FIRST-MATCH-WINS order. `timestamp` is LAST and
# its tail position is LOAD-BEARING: _record_date_info returns on the first DATE-SHAPED
# match (valid or not — a malformed higher-priority key is never skipped in favor of a
# later valid one), so any record that already resolves via date/ts/verified_date returns
# before `timestamp` is consulted. That is what freezes the three pre-S6 streams by
# CONSTRUCTION, not merely because no decisions/metrics/codex record happens to carry
# `timestamp` today (measured: 0 of 182, AMENDMENTS A27). Prepending, or a per-stream
# date-key map, forfeits that guarantee and creates a second home for one rule (cf. A3's
# two-band-homes wart).
# Changing this tuple or its ORDER requires a spec change (S6 §4.3).
_DATE_KEYS = ("date", "ts", "verified_date", "timestamp")


def _date_prefix(value):
    """The leading YYYY-MM-DD of `value` if it is a string carrying a REAL calendar date,
    else None. DATE_RE alone is purely STRUCTURAL (`\\d{4}-\\d{2}-\\d{2}` via .match()), so
    `2026-13-45` matched it and was trusted as a date — including for the `d > current_date`
    future-filter, where a bogus date could skip a whole record. datetime.date.fromisoformat
    is the calendar gate."""
    if not isinstance(value, str):
        return None
    m = DATE_RE.match(value)
    if not m:
        return None
    try:
        datetime.date.fromisoformat(m.group(0))
    except ValueError:
        return None
    return m.group(0)


def _record_date_info(rec):
    """(date_or_None, status, conflict) — the ONE date-provenance reading every stream join
    uses, so the three joins can never disagree about what a date is.

    status is one of:
      "dated"   — a recognised key carried a valid calendar date (returned as `date`)
      "invalid" — the FIRST recognised key carrying anything date-shaped was not a real
                  calendar date. A later valid key does NOT rescue the record: first-match-
                  wins is absolute (§4.3), and silently preferring a lower-priority key
                  would swallow a malformed higher-priority one with no trace -- the same
                  silent-degradation defect `records_invalid_shape` fixed for `phases_used`.
                  The record is treated as UNDATED, is never compared against current_date,
                  and is counted in `records_invalid_date`.
      "undated" — no recognised key carried anything date-shaped at all

    `conflict` is True iff BOTH `date` and `timestamp` are present with valid but DIFFERENT
    dates. Scoped to that ONE pair deliberately: `date` vs `verified_date` legitimately
    differ on the decisions stream (39 vs 43 records, AMENDMENTS A27), so a generalised
    any-two-keys-disagree rule would manufacture ~39 false conflicts. First-match-wins still
    picks `date` — that is correct (§4.3's ordering guarantee) — but the disagreement is now
    COUNTED rather than silently swallowed."""
    date = None
    saw_structural = False
    for key in _DATE_KEYS:
        val = rec.get(key)
        if isinstance(val, str) and DATE_RE.match(val):
            saw_structural = True
            date = _date_prefix(val)
            break
    if date is None:
        return (None, "invalid" if saw_structural else "undated", False)
    d_date, d_ts = _date_prefix(rec.get("date")), _date_prefix(rec.get("timestamp"))
    conflict = d_date is not None and d_ts is not None and d_date != d_ts
    return (date, "dated", conflict)


def _record_date(rec):
    """The date half of `_record_date_info`, kept as the single-value accessor
    `aggregate_codex` already calls. One home, two readers — never a second extractor
    (§4.3 rejects a per-join normalizer for exactly this reason)."""
    return _record_date_info(rec)[0]


# Sentinel: `_accumulate_date` returns this when the record must be skipped entirely. A
# future-dated record is not merely excluded from a count -- it never joins and never heats
# a node (pre-existing, correct: never attribute friction that has not happened yet). None
# is already taken by "undated", which does NOT skip, so the two need distinct values.
_SKIP_FUTURE = object()


def _new_date_counters():
    """The five date-provenance counters, in a FIXED insertion order so the rendered raw
    counters are byte-deterministic across PYTHONHASHSEED."""
    return {"records_dated_as_of": 0, "records_undated": 0, "records_invalid_date": 0,
            "records_conflicting_date": 0, "records_skipped_future": 0}


def _accumulate_date(rec, current_date, counters):
    """Read one record's date, fold its provenance into `counters`, and say whether the
    caller must skip it. The ONE place the five counters are incremented, so the three
    stream joins can never disagree.

    `records_dated_as_of` is deliberately NOT called `..._in_window`: the joins have no
    30-day lower bound, they only exclude FUTURE dates. The live minimum interventions
    timestamp is 2026-06-07 against a current date of 2026-07-31 -- 54 days back and
    counted. A real inclusive 30-day counter belongs to M8, where a window actually exists.
    # Changing these counter NAMES requires a spec change (S6 §4.3, finding #12)."""
    date, status, conflict = _record_date_info(rec)
    if conflict:
        counters["records_conflicting_date"] += 1
    if status == "invalid":
        counters["records_invalid_date"] += 1
    elif status == "undated":
        counters["records_undated"] += 1
    if date is None:
        return None
    if date > current_date:
        counters["records_skipped_future"] += 1
        return _SKIP_FUTURE
    counters["records_dated_as_of"] += 1
    return date


def join_decisions(
    records: list[dict[str, Any]],
    node_index: dict[str, list[str]],
    current_date: str,
    root: str = "",
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]], dict[str, int]]:
    heat: dict[str, int] = {}
    joined: dict[str, list[dict[str, Any]]] = {}
    valid_keys = _valid_node_keys(node_index)
    segments_total = segments_joined = segments_ambiguous = segments_unmatched = 0
    prov = _new_date_counters()
    for rec in records:
        d = _accumulate_date(rec, current_date, prov)
        if d is _SKIP_FUTURE:
            continue
        component = rec.get("component")
        if not isinstance(component, str) or not component.strip():
            continue
        for seg in _split_component(component):
            segments_total += 1
            status, key = _resolve_ref(_normalize_ref_token(seg), root, node_index, valid_keys)
            if status == "ambiguous":
                segments_ambiguous += 1
                continue
            if status == "unmatched":
                segments_unmatched += 1
                continue
            segments_joined += 1
            heat[key] = heat.get(key, 0) + 1
            joined.setdefault(key, []).append(rec)
    return heat, joined, {"segments_total": segments_total, "segments_joined": segments_joined,
                           "segments_ambiguous": segments_ambiguous,
                           "segments_unmatched": segments_unmatched, **prov}


def _metrics_eligible(rec):
    def _num(v):
        return v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0
    return _num(rec.get("rework_iterations")) > 0 or _num(rec.get("audit_rounds")) > 1 \
        or _num(rec.get("findings_total")) > 0


def join_metrics(
    records: list[dict[str, Any]],
    node_index: dict[str, list[str]],
    current_date: str,
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]], dict[str, int]]:
    """§C1 change 1: the blanket 'coding-team' base-node heat is GONE — an eligible
    record with no resolvable phase/agent alias no longer reattaches to the skill node
    (that was the smear: every eligible record, no matter which phase/agent it named,
    also always heated the single coding-team node). Phase/agent aliases now resolve
    DIRECTLY to their canonical `on_demand:skills/coding-team/{phases,agents}/<file>` key
    and heat only if that EXACT key is a rendered node — never a basename lookup (a
    future second `planning.md` elsewhere in the tree would otherwise re-smear)."""
    heat: dict[str, int] = {}
    joined: dict[str, list[dict[str, Any]]] = {}
    valid_keys = _valid_node_keys(node_index)
    records_eligible = records_aggregate_only = records_invalid_shape = 0
    prov = _new_date_counters()
    for rec in records:
        if _accumulate_date(rec, current_date, prov) is _SKIP_FUTURE:
            continue
        if not _metrics_eligible(rec):
            continue
        records_eligible += 1
        attributed = False
        # A field that is ABSENT is a legitimate older record shape and never counts. A
        # field that is PRESENT but the wrong type is a contract violation: the join
        # below silently skips it (post-exec Codex finding, S6a) and the record loses
        # that half of its attribution with no trace anywhere else -- `bad_shape` makes
        # that loss disclosed instead of silent.
        bad_shape = False
        phases = rec.get("phases_used")
        if isinstance(phases, list):
            for p in phases:
                fname = PHASE_ALIAS.get(p) if isinstance(p, str) else None
                if not fname:
                    continue
                key = _od_node_key(f"skills/coding-team/phases/{fname}")
                if key in valid_keys:
                    heat[key] = heat.get(key, 0) + 1
                    joined.setdefault(key, []).append(rec)
                    attributed = True
        elif "phases_used" in rec:
            bad_shape = True
        agents = rec.get("agents_dispatched")
        if isinstance(agents, dict):
            for a, count in agents.items():
                if not (isinstance(count, (int, float)) and not isinstance(count, bool) and count > 0):
                    continue
                fname = AGENT_ALIAS.get(a) if isinstance(a, str) else None
                if not fname:
                    continue
                key = _od_node_key(f"skills/coding-team/agents/{fname}")
                if key in valid_keys:
                    heat[key] = heat.get(key, 0) + 1
                    joined.setdefault(key, []).append(rec)
                    attributed = True
        elif "agents_dispatched" in rec:
            bad_shape = True
        if bad_shape:
            records_invalid_shape += 1
        if not attributed:
            records_aggregate_only += 1
    return heat, joined, {"records_eligible": records_eligible,
                           "records_aggregate_only": records_aggregate_only,
                           "records_invalid_shape": records_invalid_shape, **prov}


def join_interventions(
    records: list[dict[str, Any]],
    node_index: dict[str, list[str]],
    current_date: str,
    root: str = "",
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]], dict[str, int | str]]:
    heat: dict[str, int] = {}
    joined: dict[str, list[dict[str, Any]]] = {}
    valid_keys = _valid_node_keys(node_index)
    segments_joined = segments_ambiguous = segments_unmatched = events_backfilled = 0
    prov = _new_date_counters()
    for rec in records:
        if _accumulate_date(rec, current_date, prov) is _SKIP_FUTURE:
            continue
        mem = rec.get("memory_file")
        if not isinstance(mem, str) or not mem.strip():
            continue
        status, key = _resolve_ref(_normalize_ref_token(mem), root, node_index, valid_keys)
        if status == "ambiguous":
            segments_ambiguous += 1
            continue
        if status == "unmatched":
            segments_unmatched += 1
            continue
        segments_joined += 1
        if rec.get("backfilled") is True:
            events_backfilled += 1
        heat[key] = heat.get(key, 0) + 1
        joined.setdefault(key, []).append(rec)
    return heat, joined, {
        "segments_joined": segments_joined,
        "segments_ambiguous": segments_ambiguous,
        "segments_unmatched": segments_unmatched,
        "events_backfilled": events_backfilled,
        # Finding #11: node attribution here is by BASENAME, which is not stable identity
        # over time -- delete foo.md, let a different foo.md later become the sole match,
        # and every historical intervention silently reassigns. VERIFIED means "read the
        # bytes that establish the claim"; basename matching does not, so INFERRED is the
        # honest label. This is the machine-readable half; _friction_sentence carries the
        # human half. The durable fix is writer-side (store a stable node identity in the
        # record) and is outside the skill dir -- binding rule 12, S6 §14 Deferred.
        "attribution_evidence": "INFERRED",
        **prov,
    }


def aggregate_codex(records: list[dict[str, Any]], current_date: str) -> dict[str, Any]:
    """Side-panel-only aggregate (no node join — `target` names a plan file, not a
    map node, §2.2)."""
    by_mode: dict[str, int] = {}
    by_verdict: dict[str, int] = {}
    revise_rounds = []
    runs = 0
    for rec in records:
        # The raw `or rec["ts"][:10]` fallback that used to sit here is DELETED. It reached
        # the unvalidated string, so a calendar-invalid `ts` (2026-13-45) matched DATE_RE,
        # string-compared greater than any real date, and SILENTLY DROPPED the record --
        # the exact defect the calendar gate exists to close, surviving in the one stream
        # that did not route through the shared helper (finding #12). It was also already
        # dead: DATE_RE is anchored by .match(), so matching the full `ts` and matching
        # `ts[:10]` accept identical strings -- it could only ever fire on the invalid case,
        # where firing is wrong. _record_date_info is now the ONLY date reader in this file.
        d = _record_date(rec)
        if d is not None and d > current_date:
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


def _stream_truncated(f):
    """True when this stream's read stopped at a cap, so every count derived from it is a
    LOWER BOUND and its severity band must be suppressed entirely."""
    return bool(f.get("truncated_at_cap"))


def _any_stream_truncated(footer):
    """True when ANY stream's read stopped at a cap. Correct for exactly one surface:
    `friction_total`, which sums joined telemetry + metrics-aggregate-only + codex runs and
    therefore genuinely spans all four streams. Every OTHER surface takes a narrower bound
    -- see `_joined_streams_truncated` and `_stream_named_truncated`. The earlier claim here
    that the per-component table also spans all four was wrong: `joined` never contains a
    codex record (post-exec Codex round 2, finding 2)."""
    return any(_stream_truncated(f) for f in footer)


def _stream_named_truncated(footer, name):
    """True when the NAMED stream's own read stopped at a cap. The general form of
    `_codex_stream_truncated`: a count derived from exactly one stream takes THAT stream's
    bound, never the run-level one."""
    return next((_stream_truncated(f) for f in footer if f["stream"] == name), False)


def _joined_streams_truncated(footer):
    """True when any stream that actually FEEDS `joined` stopped at a cap.

    `codex` is aggregate-only -- it never joins a node and contributes no record to
    `joined` -- so a codex-only truncation must not lower-bound the per-component table or
    the joined-telemetry drill term. Rendering an exact count as `≥N` is the mirror of the
    defect `_codex_stream_truncated` fixed (commit 6d1f5c9): two widgets disagreeing about
    the same number, one of them asserting an incompleteness the read did not have."""
    return any(_stream_truncated(f) for f in footer if f["stream"] != "codex")


def _codex_stream_truncated(footer):
    """True when the CODEX stream itself stopped at a cap (finding #2, post-exec Codex
    review). `_codex_sentence` is an aggregate-only surface with no node join — unlike
    `friction_total`/the component table, it never merges other streams' records, so it
    must not inherit `_any_stream_truncated`'s run-level bound. Without this, a truncated
    interventions stream made the codex CARD (`_stream_truncated`, per-stream) show an
    exact count while the codex SENTENCE beside it showed a lower bound for a read that
    finished completely — two widgets disagreeing about the same number on the same page.
    Now the named-stream helper's codex case; kept as a name because three call sites read
    better for it."""
    return _stream_named_truncated(footer, "codex")


# Which streams each `_friction_contributions` term is derived from, and therefore whose
# cap bounds it. Keyed by the term's LABEL rather than by position so that a label edit
# fails loudly with a KeyError at the render site instead of silently mis-bounding a term.
# `_friction_contributions` keeps returning 2-tuples -- its arity is pinned by an existing
# assertion (test_render_html.py:406, commit 3018096, predates this branch; rule 7).
_CONTRIBUTION_TRUNCATION = {
    "Telemetry events joined to a component": _joined_streams_truncated,
    "Metrics events not attributed to a component (aggregate-only)":
        lambda footer: _stream_named_truncated(footer, "metrics"),
    "Codex review runs (not node-joined)": _codex_stream_truncated,
}


def _lb(value, truncated):
    """`≥N` for a count from a truncated stream, else the value unchanged. The read stopped
    early; the true value is unknown and can only be larger, so a bare N asserts a
    completeness the read did not have."""
    return f"≥{value}" if truncated else value


def _displayed_invalid_count(f):
    """`records_invalid` minus the `read_jsonl` byte-cap sentinel, never below zero
    (post-exec Codex finding #3, S6a). `read_jsonl` counts the rejected byte-cap overflow
    tail as one synthetic malformed record ("the rejected overflow tail counts once as
    malformed") -- that is a parse-layer bookkeeping artifact marking WHERE the read
    stopped, not a real invalid line, and a stream whose lines are ALL valid but which
    merely exceeds `max_bytes` must not be displayed as having an invalid line it never
    had. `max(..., 0)` keeps a genuinely malformed line visible when both it AND the byte
    cap fired on the same read."""
    invalid = f.get("records_invalid", 0)
    if "bytes" in f.get("truncated_at_cap", ""):
        invalid -= 1
    return max(invalid, 0)


def _friction_total_display(value, footer):
    """The friction total as DISPLAYED. `≥N` when ANY stream was truncated -- and that
    string is also what SUPPRESSES the severity band, because `nonneg_number` rejects it and
    `_gauge_band` then returns ("", "neutral"), which `_render_gauge` renders as no band
    element at all. The same mechanism NOT_MEASURED_TEXT already uses; no new flag, no new
    parameter threaded through four render functions.

    `friction_total()` itself is UNCHANGED and still returns an int -- this is presentation,
    and the reconciling arithmetic in `_friction_contributions` must keep summing exactly.

    The run-level bound is the RIGHT one HERE specifically, and only here: friction_total is
    the one displayed count that genuinely sums all four streams. Do not "make it
    consistent" with the narrower bounds elsewhere."""
    return _lb(value, _any_stream_truncated(footer))


def _truncation_note(f):
    """The clause naming the cap that stopped this stream's read, or "" when none did."""
    if not _stream_truncated(f):
        return ""
    return (f"read truncated at the {f['truncated_at_cap']} cap — every count above is a "
            f"lower bound and no severity band is shown")


def _sentence_with_note(sentence, f):
    """Append the truncation clause to an already-terminated stream sentence, as a
    semicolon clause rather than a second sentence — the note is lowercase, so a full stop
    ahead of it renders as `dated. read truncated…`. Emits ZERO extra bytes when the stream
    was not truncated, so untruncated output is unchanged."""
    note = _truncation_note(f)
    if not note:
        return sentence
    body = sentence[:-1] if sentence.endswith(".") else sentence
    return f"{body}; {note}."


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
    # Every count below is a LOWER BOUND once this stream's read stopped at a cap: the
    # records past the cap were never parsed, so each counter can only be larger.
    trunc = _stream_truncated(f)
    if f["stream"] == "decisions":
        total, joined = f.get("segments_total", 0), f.get("segments_joined", 0)
        dated, ambiguous = f.get("records_dated_as_of", 0), f.get("segments_ambiguous", 0)
        return _sentence_with_note(
            f"{label} — {_lb(joined, trunc)} of {_lb(total, trunc)} component references "
            f"matched to map components ({_lb(dated, trunc)} records dated; "
            f"{_lb(ambiguous, trunc)} ambiguous).", f)
    if f["stream"] == "metrics":
        eligible, agg_only = f.get("records_eligible", 0), f.get("records_aggregate_only", 0)
        attributed, invalid = eligible - agg_only, _displayed_invalid_count(f)
        sentence = (f"{label} — {_lb(attributed, trunc)} of {_lb(eligible, trunc)} eligible pipeline "
                    f"records attributed to phase/agent components ({_lb(agg_only, trunc)} "
                    f"aggregate-only); {_lb(invalid, trunc)} invalid lines")
        malformed = f.get("records_invalid_shape", 0)
        if malformed:
            # Disclosure, not a new failure mode: these records already counted toward
            # `eligible`/`agg_only` above -- this clause names WHY some of them lost
            # phase/agent attribution (a contract-violating field shape) rather than
            # letting that loss vanish silently into the aggregate-only bucket.
            sentence += (f"; {_lb(malformed, trunc)} of {_lb(eligible, trunc)} records "
                         f"malformed (phase/agent attribution incomplete)")
        return _sentence_with_note(sentence + ".", f)
    if f["stream"] == "interventions":
        parsed, dated = f.get("records_parsed", 0), f.get("records_dated_as_of", 0)
        bits = [f"{label} — {_lb(parsed, trunc)} records parsed, {_lb(dated, trunc)} dated"]
        for count_key, word in (("records_undated", "undated"),
                                ("records_invalid_date", "invalid"),
                                ("records_conflicting_date", "conflicting"),
                                ("records_skipped_future", "skipped as future-dated")):
            n = f.get(count_key, 0)
            if n:
                bits.append(f"{_lb(n, trunc)} {word}")
        joined_events = f.get("segments_joined", 0)
        backfilled = f.get("events_backfilled", 0)
        if joined_events:
            # All three numbers take the lower bound together (finding #5 propagation): a
            # `≥N events` beside a bare `M observed` reads as though the split were exact.
            bits.append(f"{_lb(joined_events, trunc)} events "
                        f"({_lb(joined_events - backfilled, trunc)} observed, "
                        f"{_lb(backfilled, trunc)} backfilled)")
        unmatched = f.get("segments_unmatched", 0)
        if unmatched:
            bits.append(f"{_lb(unmatched, trunc)} unmatched (the named memory file is no "
                        f"longer a node on this map)")
        # `_sentence_with_note` is NOT optional here: it is what appends the truncation
        # clause Task 5 pinned (T5.4). The attribution clause follows it as a semicolon
        # continuation (lowercase opener) rather than a new sentence, so it reads as one
        # provenance statement rather than an abrupt capitalized fragment.
        sentence = _sentence_with_note("; ".join(bits) + ".", f)
        body = sentence[:-1] if sentence.endswith(".") else sentence
        return (f"{body}; joined on `memory_file` — a rule written in response to friction, "
                "not a rule that caused it. Attribution is by basename and is labelled "
                "INFERRED: it can reattribute across a delete-and-recreate.")
    # codex — aggregate-only, no node join (§2.2)
    runs = codex_aggregate["runs"]
    return _sentence_with_note(
        f"{label} — {_lb(runs, trunc)} records, aggregate-only (target is a plan filename, "
        f"not a map component).", f)


def _codex_sentence(agg, *, truncated=False):
    """English summary of `codex_aggregate` — derived entirely from its own dict,
    never hardcoded numbers.

    `truncated` (keyword-only, default False so every direct caller is unchanged): when the
    run's read stopped at a cap, every count here is a LOWER BOUND. The `runs == 0` early
    return is left alone deliberately — `≥0 Codex reviews` reads worse than the plain
    sentence and adds no information."""
    runs = agg["runs"]
    if runs == 0:
        return "No Codex reviews recorded in this window."
    mode_bits = ", ".join(f"{_lb(v, truncated)} on {k}s" for k, v in agg["by_mode"].items())
    mode_clause = f" — {mode_bits}" if mode_bits else ""
    verdict_bits = ", ".join(f"{_lb(v, truncated)} {CODEX_VERDICT_LABELS.get(k, k.lower())}"
                              for k, v in agg["by_verdict"].items())
    max_round = agg["max_revise_round"]
    revise_clause = f" (up to {max_round} revise round{'s' if max_round != 1 else ''})" if max_round else ""
    verdict_clause = f" Verdicts: {verdict_bits}{revise_clause}." if verdict_bits else ""
    plural = "s" if runs != 1 else ""
    return f"{_lb(runs, truncated)} Codex review{plural}{mode_clause}.{verdict_clause}"


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


def build_friction_overlay(
    doc: dict[str, Any],
    streams: dict[str, Any],
    node_index: dict[str, list[str]],
    current_date: str,
    disabled: bool,
) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[str, Any]]:
    """Joins the four optional streams onto `node_index` (data join only, never a
    judgment, §2.2). Returns (heat, joined_records, sources_footer, codex_aggregate)."""
    heat: dict[str, int] = {}
    joined: dict[str, list[dict[str, Any]]] = {}
    footer: list[dict[str, Any]] = []
    root = doc.get("root") or ""

    def _merge(h: dict[str, int], j: dict[str, list[dict[str, Any]]]) -> None:
        for k, v in h.items():
            heat[k] = heat.get(k, 0) + v
        for k, recs in j.items():
            joined.setdefault(k, []).extend(recs)

    codex_aggregate: dict[str, Any] = {
        "runs": 0, "by_mode": {}, "by_verdict": {}, "max_revise_round": 0
    }

    for stream in STREAM_ORDER:
        path = streams.get(stream)
        status = _stream_status(path, disabled)
        counters: dict[str, Any] = {}
        if status == "loaded":
            # _stream_status only returns "loaded" when `path` passed its own is-not-None
            # check — cast (not assert) so this is a pure type narrowing with zero
            # runtime effect, matching the M1 typing-only scope.
            caps: dict[str, bool] = {}
            records, malformed, nonblank = read_jsonl(
                cast(Path, path), STREAM_MAX_BYTES, STREAM_MAX_LINES, caps_out=caps)
            counters["lines_nonblank"] = nonblank
            counters["records_parsed"] = len(records)
            counters["records_invalid"] = malformed
            if caps:
                # Fixed order, never sorted(set(...)) -- determinism §4.4.
                counters["truncated_at_cap"] = "+".join(
                    k for k in ("bytes", "lines") if caps.get(k))
            if stream == "decisions":
                h, j, extra = join_decisions(records, node_index, current_date, root)
                _merge(h, j)
                counters.update(extra)
            elif stream == "metrics":
                h, j, extra = join_metrics(records, node_index, current_date)
                _merge(h, j)
                counters.update(extra)
            elif stream == "interventions":
                # `extra_i` gets its own name rather than reusing `extra`: mypy fixes a
                # reused local's declared type from its first assignment in the function
                # (dict[str, int] from the decisions/metrics branches above), and
                # join_interventions's widened dict[str, int | str] return would conflict
                # with that declared type under dict's invariance.
                h, j, extra_i = join_interventions(records, node_index, current_date, root)
                _merge(h, j)
                counters.update(extra_i)
            elif stream == "codex":
                codex_aggregate = aggregate_codex(records, current_date)
                counters["records_aggregate_only"] = codex_aggregate["runs"]
        footer.append({"stream": stream, "status": status, "path_display": _display_path(path), **counters})
    return heat, joined, footer, codex_aggregate


# ----------------------------------------------------------------------------- write safety
def write_html_safely(
    out_path: Path,
    text: str,
    guard_roots: str | Path | list[Any] | None,
    input_paths: tuple[Any, ...] = (),
) -> None:
    """Hard-link-safe, TOCTOU-narrowed write (P1-B, Codex challenge): re-validates the
    RESOLVED write target against EVERY root in `guard_roots` (+ `input_paths`) via the
    shared `collector.validate_write_target` guard IMMEDIATELY before writing — no
    caller-side check that ran earlier (build_server's startup guard, main()'s own
    compose pre-check, or nothing at all) can see a `--out-dir` symlink (or the html
    filename itself) retargeted AFTERWARD; this re-check runs FRESH on the raw
    `out_path` on EVERY call, so it always resolves the CURRENT on-disk target. Every
    caller now routes its write-time root guard through here rather than duplicating
    it, closing the "check once, write many times later" gap that made a --out-dir
    symlink swapped after startup slip past a stale one-shot validation.

    Then writes via the collector's mkstemp/fsync/os.replace-in-the-resolved-directory
    pattern (reused verbatim) — never `write_text()`, which would truncate a
    hard-linked inode also linked under a guarded root — through the VALIDATED
    resolved path, re-checked ONE MORE TIME immediately before the `mkstemp` call
    below (mirrors collector.main's own recheck-then-write shape exactly, closing the
    validate-then-mkstemp parent-dir-swap window the same way), so a directory-
    component symlink hop is settled at the check closest to the write, not re-followed
    at write time.

    A rejection at EITHER check raises `RenderError` (a catchable `Exception`, not
    `SystemExit`) — serve.py's watcher-loop degrade handlers (`except Exception`,
    `except (CollectorError, RenderError, OSError)`) catch it and keep the daemon
    thread alive on last-good, exactly like any other rebuild fault; only main()'s own
    top-level CLI handler turns it into a non-zero exit.

    `guard_roots` is a single root (str/Path) or an iterable of roots — the caller
    decides operator-only (non-compose) vs operator+project (compose); falsy entries
    are dropped. A falsy/empty `guard_roots` skips validation entirely (no root to
    guard against) — none of the current callers pass one."""
    out_path = Path(out_path)
    if guard_roots is None:
        roots = []
    elif isinstance(guard_roots, (str, Path)):
        roots = [guard_roots]
    else:
        roots = list(guard_roots)
    roots = [r for r in roots if r]
    if roots:
        collector_mod = _get_sibling_collector()
        ok, resolved = collector_mod.validate_write_target(out_path, roots, input_paths)
        if not ok:
            raise RenderError(f"fatal: refusing to write inside a guarded root: {out_path}")
        out_path = resolved
    tmp_name = None
    try:
        if roots:
            # P30/TOCTOU narrowing (parity with collector.main's own pre-mkstemp re-check,
            # collector.py's `--out` write path): re-resolve the ALREADY-validated target
            # FRESH from disk and re-check it against every guard root IMMEDIATELY before
            # mkstemp — a parent-directory symlink swapped in during the window between the
            # check above and this line would otherwise slip through on the now-stale
            # `out_path`. Reuses the same `validate_write_target` guard (not a hand-rolled
            # duplicate), so this stays in lockstep with the check above; on rejection it
            # raises the SAME catchable RenderError. The residual window between THIS
            # re-check and the mkstemp call itself is the same accepted, documented
            # low-risk limitation collector.main carries (single-user local tool; not fully
            # closed).
            ok, out_path = _get_sibling_collector().validate_write_target(out_path, roots, input_paths)
            if not ok:
                raise RenderError(f"fatal: refusing to write inside a guarded root: {out_path}")
        fd, tmp_name = tempfile.mkstemp(dir=str(out_path.parent), suffix=".tmp")
        # errors="backslashreplace" (Codex P1): a lone UTF-16 surrogate anywhere in `text`
        # (a non-UTF-8 filename the collector preserved via surrogateescape, reaching
        # esc_json_script's ensure_ascii=False copy islands, or esc_html's own
        # pre-html.escape substitution) must never raise UnicodeEncodeError here and
        # abort the write with no report produced — it deterministically becomes a
        # literal `\udNNN` escape instead, so the output file is always complete,
        # always valid UTF-8. This changes ONLY the encoding error mode, never the
        # write-safety/path-security logic above.
        with os.fdopen(fd, "w", encoding="utf-8", errors="backslashreplace") as f:
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
:root{--paper:#f5f7fb;--surface:#ffffff;--surface-2:#eef1f7;--line:#d9dfea;--ink:#161a23;--muted:#5a6376;--faint:#8b93a5;--accent:#6366f1;--accent-2:#8b5cf6;--accent-soft:#e5e7fb;--good:#12a37e;--good-bg:#d8f3ea;--good-line:#7fd9c2;--warn:#c9820a;--warn-bg:#fbeecd;--warn-line:#e6c878;--crit:#d83f47;--crit-bg:#fbdedf;--crit-line:#eaa0a4;--shadow:0 1px 2px rgba(22,26,35,.06),0 6px 20px rgba(22,26,35,.05);--r:10px;--mono:ui-monospace,"SF Mono","JetBrains Mono","Menlo",monospace;--tier-operator:var(--muted);--tier-operator-bg:var(--surface-2);--tier-operator-line:var(--line);--tier-project:#0e7490;--tier-project-bg:#cffafe;--tier-project-line:#67e8f9}
@media (prefers-color-scheme: dark){:root{--paper:#0e1117;--surface:#161b25;--surface-2:#1d2431;--line:#2b3342;--ink:#e8ecf4;--muted:#9aa4b8;--faint:#6b7484;--accent:#818cf8;--accent-2:#a78bfa;--accent-soft:#262b45;--good:#2dd4a7;--good-bg:#123a30;--good-line:#1f6f57;--warn:#f0b13c;--warn-bg:#3a2c10;--warn-line:#7a5a1c;--crit:#f2666d;--crit-bg:#3a1418;--crit-line:#7a2830;--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.35);--tier-operator:var(--muted);--tier-operator-bg:var(--surface-2);--tier-operator-line:var(--line);--tier-project:#22d3ee;--tier-project-bg:#083344;--tier-project-line:#155e75}}
:root[data-theme="dark"]{--paper:#0e1117;--surface:#161b25;--surface-2:#1d2431;--line:#2b3342;--ink:#e8ecf4;--muted:#9aa4b8;--faint:#6b7484;--accent:#818cf8;--accent-2:#a78bfa;--accent-soft:#262b45;--good:#2dd4a7;--good-bg:#123a30;--good-line:#1f6f57;--warn:#f0b13c;--warn-bg:#3a2c10;--warn-line:#7a5a1c;--crit:#f2666d;--crit-bg:#3a1418;--crit-line:#7a2830;--shadow:0 1px 2px rgba(0,0,0,.4),0 8px 24px rgba(0,0,0,.35);--tier-operator:var(--muted);--tier-operator-bg:var(--surface-2);--tier-operator-line:var(--line);--tier-project:#22d3ee;--tier-project-bg:#083344;--tier-project-line:#155e75}
:root[data-theme="light"]{--paper:#f5f7fb;--surface:#ffffff;--surface-2:#eef1f7;--line:#d9dfea;--ink:#161a23;--muted:#5a6376;--faint:#8b93a5;--accent:#6366f1;--accent-2:#8b5cf6;--accent-soft:#e5e7fb;--good:#12a37e;--good-bg:#d8f3ea;--good-line:#7fd9c2;--warn:#c9820a;--warn-bg:#fbeecd;--warn-line:#e6c878;--crit:#d83f47;--crit-bg:#fbdedf;--crit-line:#eaa0a4;--shadow:0 1px 2px rgba(22,26,35,.06),0 6px 20px rgba(22,26,35,.05);--tier-operator:var(--muted);--tier-operator-bg:var(--surface-2);--tier-operator-line:var(--line);--tier-project:#0e7490;--tier-project-bg:#cffafe;--tier-project-line:#67e8f9}
*{box-sizing:border-box}
body{background:var(--paper);color:var(--ink);font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;padding:0}
header{padding:16px 20px;border-bottom:1px solid var(--line)}
h1{font-size:1.25rem;margin:0 0 4px 0}
.subtitle{color:var(--muted);font-size:0.85rem}
.tiles,.gauges{display:flex;flex-wrap:wrap;gap:10px;padding:12px 20px}
.tile,.gauge{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);padding:10px 14px 12px;min-width:150px}
.tile .v,.gauge .v{font-size:1.625rem;font-weight:700;font-family:var(--mono);font-variant-numeric:tabular-nums;line-height:1.15}
.tile .l,.gauge .l{color:var(--muted);font-size:0.72rem;margin-top:2px}
.gauge{border-left:3px solid var(--line)}
.gauge-good{border-left-color:var(--good)}
.gauge-good .v{color:var(--good)}
.gauge-warn{border-left-color:var(--warn)}
.gauge-warn .v{color:var(--warn)}
.gauge-bad{border-left-color:var(--crit)}
.gauge-bad .v{color:var(--crit)}
.gauge-neutral{border-left-color:var(--line)}
.gauge .band{color:var(--muted);font-size:0.65rem;text-transform:uppercase;letter-spacing:.04em;margin-top:2px}
.gauge .delta{font-size:0.75rem;font-weight:600}
.gauge .delta-good{color:var(--good)}
.gauge .delta-bad{color:var(--crit)}
.gauge .delta-neutral{color:var(--muted)}
.warn-badge{background:var(--crit);color:#fff;border-radius:6px;padding:2px 8px;font-size:0.75rem;text-decoration:none}
.controls{position:sticky;top:0;background:var(--paper);border-bottom:1px solid var(--line);padding:8px 20px;display:flex;gap:8px;flex-wrap:wrap;z-index:5}
.seg{display:inline-flex;gap:6px;flex-wrap:wrap;border:1px solid var(--line);border-radius:6px;padding:2px}
.view-switch{display:inline-flex;gap:4px;flex-wrap:wrap;border-bottom:1px solid var(--line)}
button.action-btn,button.view-btn,button.seg-btn,button.copy-btn{background:var(--surface);color:var(--ink);border:1px solid var(--line);border-radius:6px;padding:6px 12px;cursor:pointer;font-size:0.85rem}
button.view-btn{background:transparent;border:none;border-bottom:2px solid transparent;border-radius:0;padding:8px 14px;color:var(--muted)}
button.view-btn[aria-selected="true"]{border-bottom-color:var(--accent);color:var(--accent);font-weight:600}
button[aria-pressed="true"]{border-color:var(--accent);color:var(--accent)}
button.seg-btn[aria-pressed="true"]{border-color:var(--accent);color:var(--accent);background:var(--paper)}
.tier-filter{display:inline-flex;gap:4px;flex-wrap:wrap;border:1px solid var(--line);border-radius:6px;padding:2px;margin-left:auto}
button.tier-filter-btn{background:transparent;border:none;border-radius:4px;padding:6px 10px;color:var(--muted);font-size:0.85rem;cursor:pointer}
button.tier-filter-btn[aria-checked="true"]{background:var(--surface-2);color:var(--ink);font-weight:600}
button:focus-visible,a:focus-visible,summary:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
main{padding:16px 20px}
.view[hidden]{display:none}
@media (prefers-reduced-motion: no-preference){.view:not([hidden]){animation:fade .18s ease-out}}
@keyframes fade{from{opacity:0}to{opacity:1}}
.view-toolbar{display:flex;justify-content:flex-end;margin-bottom:8px}
.card{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);padding:14px;margin-bottom:14px}
.digest{color:var(--muted);font-size:0.85rem;margin:0 0 10px 0}
.hero-friction{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);padding:14px;margin-bottom:14px}
.inspector{position:sticky;top:52px;background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);padding:14px;max-height:70vh;overflow-y:auto}
.empty-state{color:var(--muted);font-style:italic}
.script-desc{display:block;color:var(--muted);font-size:0.78rem;margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:0.85rem}
th,td{border:1px solid var(--line);padding:6px 8px;text-align:left;font-family:var(--mono);font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:600}
.badge{display:inline-block;border-radius:5px;padding:1px 6px;font-size:0.72rem;border:1px solid var(--line)}
.badge.orphan{border-color:var(--crit);color:var(--crit)}
.badge.direct{border-color:var(--good);color:var(--good)}
.badge.dispatcher{border-color:var(--accent);color:var(--accent)}
.cell-label{font-size:12px;fill:var(--ink);font-family:var(--mono);font-variant-numeric:tabular-nums;paint-order:stroke;stroke:var(--paper);stroke-width:3;stroke-linejoin:round}
.legend-swatch{display:inline-block;width:10px;height:10px;margin-right:4px;border-radius:2px;vertical-align:middle}
.overview-grid{display:grid;grid-template-columns:1fr 340px;gap:14px;align-items:start}
.hero-friction-good{border-left:4px solid var(--good)}
.hero-friction-warn{border-left:4px solid var(--warn)}
.hero-friction-bad{border-left:4px solid var(--crit)}
.hero-friction-neutral{border-left:4px solid var(--line)}
.hero-friction .count{font-size:1.2rem;font-weight:600;font-family:var(--mono);font-variant-numeric:tabular-nums;margin:4px 0}
.digest-group{margin-bottom:10px}
.digest-group h3{font-size:0.7rem;margin:0 0 6px 0;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:.03em}
.digest-group ul{margin:0;padding:0;list-style:none}
.digest-group li{font-size:0.82rem;margin:2px 0;display:flex;align-items:center;gap:6px}
.sev-dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex:0 0 auto}
.sev-dot.sev-good{background:var(--good)}
.sev-dot.sev-warn{background:var(--warn)}
.sev-dot.sev-bad{background:var(--crit)}
.stream-cards{display:flex;flex-wrap:wrap;gap:10px;margin:0 0 14px 0}
.stream-card{background:var(--surface);border-radius:var(--r);box-shadow:var(--shadow);padding:12px 14px;flex:1 1 200px}
.stream-card .count{font-size:1.75rem;font-weight:700;font-family:var(--mono);font-variant-numeric:tabular-nums;color:var(--accent);margin:0 0 4px 0}
.stream-card h3{margin:0 0 4px 0;font-size:0.9rem;font-weight:700}
.stream-card p{margin:0 0 6px 0;font-size:0.82rem;color:var(--muted)}
.stream-card .source{font-size:0.75rem;color:var(--faint);font-family:var(--mono)}
.sev-dot.sev-neutral{background:var(--muted)}
svg text{font-family:inherit}
footer.sources{border-top:1px solid var(--line);padding:10px 20px;color:var(--muted);font-size:0.78rem}
.overflow-x{overflow-x:auto}
@media (prefers-reduced-motion: no-preference){button{transition:border-color .15s}.tier-node{transition:opacity .15s}}
.cell-rect{stroke:var(--line);stroke-width:0.5}
.ladder-track{fill:var(--surface-2)}
body.friction-on .heatable:not(.fh1):not(.fh2):not(.fh3):not(.fh4){opacity:0.25}
.friction-badge{display:none;font-size:13px;font-weight:700;fill:#fff;paint-order:stroke;stroke:#000;stroke-width:2.5}
body.friction-on .friction-badge{display:inline}
/* Length-criticality outline (honest, separate from friction heat — hygiene's own
   >600-line `critical` classification, never fabricated churn). `--warn` (amber),
   deliberately NOT `--crit` (red) — red is friction heat's own fh1-4 ramp, and a
   crit-colored ring on a zero-friction tile read as a false friction claim.
   Rendered as sibling elements OUTSIDE the `.heatable` class entirely, so the
   friction-overlay dimming rule above structurally cannot touch them: always-on,
   in every lens, in both themes (`--warn`/`--paper` already theme). */
.length-crit-ring{fill:none;stroke:var(--warn);stroke-width:2.5;pointer-events:none}
.length-crit-marker{fill:var(--warn);stroke:var(--paper);stroke-width:1.5;pointer-events:none}
.legend-swatch.length-crit-swatch{background:transparent;border:2px solid var(--warn)}
#friction-toggle[aria-pressed="true"]{background:var(--crit);border-color:var(--crit);color:#fff;font-weight:600}
.friction-legend{display:flex;align-items:center;gap:10px;flex-wrap:wrap;color:var(--muted);font-size:0.75rem;padding:4px 20px 0}
.legend-entry{display:inline-flex;align-items:center;gap:4px}
.legend-swatch.fh0{background:var(--surface);border:1px solid var(--line)}
.legend-note{color:var(--muted)}
.friction-explainer{color:var(--muted);font-size:0.85rem;margin:0 0 10px 0}
.friction-row-detail{display:block;color:var(--muted);font-size:0.78rem;margin-top:2px}
details{color:var(--muted)}
details > summary{cursor:pointer;color:var(--accent)}
.civc-legend{color:var(--muted);font-size:0.8rem;margin:0 0 10px 0}
.badge.verdict-thin{border-color:var(--warn);color:var(--warn)}
.badge.verdict-covered{border-color:var(--good);color:var(--good)}
.badge.verdict-empty{border-color:var(--crit);color:var(--crit)}
.coverage-grid{display:grid;grid-template-columns:1fr 320px;gap:14px;align-items:start}
.matrix{display:grid;grid-template-columns:88px repeat(6,1fr);gap:6px}
.matrix .mhead{font-family:var(--mono);font-size:0.72rem;color:var(--muted);display:flex;align-items:center;padding:4px 6px}
.matrix .mhead.mhead-row{justify-content:flex-end;text-align:right}
.matrix .cell{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;aspect-ratio:1/.74;border-radius:8px;border:1px solid var(--line);background:var(--surface);cursor:pointer;transition:transform .15s ease,box-shadow .15s ease}
.matrix .cell:hover,.matrix .cell:focus-visible{transform:translateY(-2px);box-shadow:var(--shadow)}
.matrix .cell.verdict-covered{background:var(--good-bg);border-color:var(--good-line)}
.matrix .cell.verdict-thin{background:var(--warn-bg);border-color:var(--warn-line)}
.matrix .cell.verdict-empty{border:1px dashed var(--crit-line);background-color:var(--surface);background-image:repeating-linear-gradient(135deg,var(--crit-bg) 0,var(--crit-bg) 4px,transparent 4px,transparent 8px)}
.phantom-group-not_a_path td,.phantom-group-not_a_path th{background-image:repeating-linear-gradient(135deg,var(--surface-2) 0,var(--surface-2) 4px,transparent 4px,transparent 8px)}
.gauge-drill-faint{font-size:0.8rem;color:var(--faint)}
.matrix .cell.sel{outline:2px solid var(--accent);outline-offset:-2px}
.matrix .cell:focus-visible{outline:2px solid var(--accent)}
.matrix .cell .cv{font-family:var(--mono);font-size:0.66rem;text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
.matrix .cell .dot{width:8px;height:8px;border-radius:50%;background:var(--line)}
.matrix .cell.verdict-covered .dot{background:var(--good)}
.matrix .cell.verdict-thin .dot{background:var(--warn)}
.matrix .cell.verdict-empty .dot{background:var(--crit)}
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
.pill{display:inline-block;border-radius:5px;padding:1px 6px;font-size:0.72rem;border:1px solid var(--warn);color:var(--warn)}
.pill-critical{background:var(--crit);border-color:var(--crit);color:#fff;font-weight:700;padding:2px 9px;box-shadow:var(--shadow)}
tr:has(.pill-critical){background:var(--crit-bg)}
tr:has(.pill-critical) td{border-color:var(--crit-line);font-weight:600}
.hygiene-unchecked{font-family:var(--mono);font-variant-numeric:tabular-nums;font-weight:600}
.warn-count{font-weight:600;color:var(--crit)}
table.sortable thead th{padding:0}
button.th-sort{width:100%;background:transparent;border:none;color:var(--muted);font:inherit;font-weight:600;text-align:left;cursor:pointer;padding:6px 8px}
button.th-sort:hover{color:var(--accent)}
thead th[aria-sort="ascending"] button.th-sort,thead th[aria-sort="descending"] button.th-sort{color:var(--accent);text-decoration:underline}
button.gauge{font:inherit;text-align:left;cursor:pointer;border:1px solid var(--line);border-left-width:3px}
.gauge-chev{float:right;color:var(--muted);font-size:0.7rem;margin-left:8px}
button.gauge[aria-expanded="true"]{border-color:var(--accent)}
.gauge-drawer{padding:0 20px 8px}
.gauge-drill-panel{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:10px 14px;margin-top:6px;font-size:0.82rem}
.gauge-drill,.gauge-contributors{margin:4px 0 0;padding-left:18px}
.gauge-drill li,.gauge-contributors li{margin:2px 0}
.gauge-contributors-label{color:var(--muted);font-size:0.72rem;text-transform:uppercase;letter-spacing:.03em;margin:0}
.gauge-drill-tab{color:var(--accent);font-size:0.78rem;margin:6px 0 0}
.copy-preview{display:inline-block;text-align:left}
.copy-preview > summary{cursor:pointer;color:var(--accent);font-size:0.78rem}
.copy-preview-body{max-height:280px;overflow:auto;background:var(--surface-2);border:1px solid var(--line);border-radius:6px;padding:8px 10px;margin:6px 0;font-family:var(--mono);font-size:0.75rem;white-space:pre-wrap;word-break:break-word}
.drag-fields{margin:4px 0}
.drag-fields > summary{cursor:pointer;color:var(--accent);font-size:0.78rem}
.drag-fields div{font-size:0.8rem;margin:2px 0}
tr.friction-component-row.sel td{background:var(--accent-soft);border-color:var(--accent)}
svg .cell-rect,svg .ladder-bar{cursor:pointer}
/* Project-tier targeting (T6): a wrapper `<g>`/`<tr>` carries `tier-node
   tier-{operator|project}` around a treemap cell / ladder row / hygiene table row, so a
   later opacity-dim (T7's filter toggle) dims sibling overlays (the length-crit ring +
   marker) along with the cell, not just the rect. Operator tier stays visually
   unchanged (muted tokens alias the existing --muted/--line) — only project-tier gets a
   distinct accent stroke/border, so a non-compose (operator-only) render is unaffected. */
.tier-node.tier-project .cell-rect,.tier-node.tier-project .ladder-bar{stroke:var(--tier-project);stroke-width:1.5}
/* Border-left on the first cell, not a `tr` background — `tr:has(.pill-critical)`
   above already owns full-row background under `border-collapse:collapse`, and a
   length-critical row can ALSO be project-tier; this stays a non-clobbering signal
   layered alongside it instead of fighting it for the same property. */
tr.tier-project td:first-child{border-left:3px solid var(--tier-project)}
.badge.tier-operator{border-color:var(--tier-operator-line);color:var(--tier-operator)}
.badge.tier-project{border-color:var(--tier-project-line);color:var(--tier-project)}
.badge.tier-dark{border-color:var(--crit);color:var(--crit)}
.tier-summary .tier-surface-list{list-style:none;margin:0;padding:0}
.tier-summary .tier-surface-list li{margin:4px 0;font-size:0.85rem}
.tier-summary .tier-surface{font-family:var(--mono);color:var(--muted);text-transform:uppercase;font-size:0.72rem;letter-spacing:.03em;margin-right:6px}
.tier-dark-callout{margin-top:10px}
.tier-dark-callout h3{font-size:0.78rem;margin:0 0 6px 0;color:var(--muted);font-family:var(--mono);text-transform:uppercase;letter-spacing:.03em}
.tier-dark-callout ul{margin:0;padding-left:18px}
.tier-dark-callout li{margin:3px 0;font-size:0.82rem}
/* Trend sparklines (S2.M5): inline-SVG, no external assets. `currentColor` picks up
   the cell's text color so the mark stays legible in both themes without a new token. */
.sparkline{color:var(--accent);vertical-align:middle}
.sparkline-stats{margin-left:6px;font-size:0.72rem;color:var(--muted);font-variant-numeric:tabular-nums}
/* Composed-settings source-tier badges (T7b) — the 3-way user/project/local vocabulary
   (`_normalize_settings_tier`), distinct from the `.badge.tier-operator`/`.badge.tier-project`
   pair above (the binary skills/agents/rules NODE tag). "user" and "project" reuse those same
   tokens (same conceptual weight: user~operator's muted baseline, project~project's accent);
   "local" (the highest-precedence settings source) reuses `--accent-2`, already themed
   light/dark, rather than inventing a new CSS variable. */
.badge.tier-src-user{border-color:var(--tier-operator-line);color:var(--tier-operator)}
.badge.tier-src-project{border-color:var(--tier-project-line);color:var(--tier-project)}
.badge.tier-src-local{border-color:var(--accent-2);color:var(--accent-2)}
.badge.mcp-enabled{border-color:var(--good);color:var(--good)}
.badge.mcp-disabled{border-color:var(--crit);color:var(--crit)}
"""
# Graduated friction-heat ramp (Codex/demo parity finding: fh1 rendered visually
# IDENTICAL to fh4 — both hit opacity:1, differing only by a subtle stroke-color
# swap). Opacity + stroke-width now step up monotonically with HEAT_RAMP's own
# light->dark color progression, so "some" (fh1) genuinely recedes relative to
# "most-active" (fh4) instead of only the outline color changing.
_HEAT_OPACITY = ("0.55", "0.72", "0.88", "1")
_HEAT_STROKE_W = (2, 3, 4, 5)
_HEAT_CSS = "".join(
    f"body.friction-on .fh{i}{{stroke:{color};stroke-width:{_HEAT_STROKE_W[i - 1]};"
    f"opacity:{_HEAT_OPACITY[i - 1]}}}"
    f".legend-swatch.fh{i}{{background:{color}}}"
    for i, color in enumerate(HEAT_RAMP, start=1)
)
# Tier filter dim-in-place (T7, P2-8) — appended AFTER `_HEAT_CSS` so these two
# declarations are the LAST rules in the stylesheet: if a future edit ever raises the
# heat block's specificity, the tier dim still wins any equal-specificity tie. In
# practice no such tie exists today — the selector targets the WRAPPER
# (`.tier-node`, the `<g>`/`<tr>` T6 wraps every cell/row in), never `.fhN`/
# `.heatable` (which live on the CHILD rect/bar), so opacity composes automatically
# via ordinary nested compositing: a heated + tier-dimmed cell renders at
# (dim-opacity * heat-opacity), and the length-crit ring/marker — a SIBLING inside
# the same wrapper — dims right along with it, never independently. "All" (no body
# class) leaves both rules inert; a non-compose render (no `.tier-filter` control,
# every node tagged `tier-operator` by `_normalize_tier`'s default) is unaffected.
_TIER_FILTER_CSS = (
    "body.tier-project-only .tier-node.tier-operator{opacity:.25}"
    "body.tier-operator-only .tier-node.tier-project{opacity:.25}"
)
STATIC_STYLE = STATIC_STYLE + _HEAT_CSS + _TIER_FILTER_CSS

STATIC_SCRIPT = """
(function(){
  var views = document.querySelectorAll('.view');
  var vbtns = document.querySelectorAll('.view-btn');
  function activate(id){
    views.forEach(function(v){ v.hidden = (v.id !== id); });
    vbtns.forEach(function(b){ b.setAttribute('aria-selected', b.dataset.target === id ? 'true':'false'); });
  }
  vbtns.forEach(function(b){ b.addEventListener('click', function(){ activate(b.dataset.target); }); });

  // WCAG APG tablist pattern: ArrowLeft/ArrowRight (+ Home/End) traverse the view-btn
  // group and activate the newly-focused tab — click/Enter/Space alone left the
  // tablist incomplete (finding P3-6).
  var viewSwitch = document.querySelector('.view-switch');
  if (viewSwitch){
    viewSwitch.addEventListener('keydown', function(e){
      if (['ArrowRight', 'ArrowLeft', 'Home', 'End'].indexOf(e.key) === -1) { return; }
      e.preventDefault();
      var btns = Array.prototype.slice.call(vbtns);
      var idx = btns.indexOf(document.activeElement);
      if (idx === -1) { idx = 0; }
      var next;
      if (e.key === 'ArrowRight') { next = (idx + 1) % btns.length; }
      else if (e.key === 'ArrowLeft') { next = (idx - 1 + btns.length) % btns.length; }
      else if (e.key === 'Home') { next = 0; }
      else { next = btns.length - 1; }
      btns[next].focus();
      activate(btns[next].dataset.target);
    });
  }

  // Tier filter (T7): TRUE roving-tabindex radiogroup (WAI-ARIA APG radio pattern) --
  // unlike the view-switch tablist above (arrow-key but NOT roving: buttons carry no
  // tabindex, only aria-selected flips -- P2-7 finding), this control keeps exactly
  // ONE tabstop at all times. Arrow keys move focus AND selection AND the tabindex=0
  // together (radio-group convention: moving focus changes the checked state, unlike
  // a tablist where selection may lag focus). Selecting sets a body-level class the
  // CSS reads to dim-in-place (never a re-layout, M3) -- "All" clears both classes.
  // The control is absent entirely on a non-compose render (no tier_composition in
  // the sidecar), so this whole block is a silent no-op there.
  var tierGroup = document.querySelector('.tier-filter');
  if (tierGroup){
    var tierBtns = Array.prototype.slice.call(tierGroup.querySelectorAll('.tier-filter-btn'));
    var selectTier = function(btn){
      tierBtns.forEach(function(b){
        var checked = b === btn;
        b.setAttribute('aria-checked', checked ? 'true' : 'false');
        b.setAttribute('tabindex', checked ? '0' : '-1');
      });
      document.body.classList.remove('tier-operator-only', 'tier-project-only');
      var f = btn.dataset.tierFilter;
      if (f === 'operator-only' || f === 'project-only') { document.body.classList.add(f); }
    };
    tierBtns.forEach(function(b){
      b.addEventListener('click', function(){ selectTier(b); b.focus(); });
    });
    tierGroup.addEventListener('keydown', function(e){
      var step = {ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1}[e.key];
      if (e.key !== 'Home' && e.key !== 'End' && step === undefined) { return; }
      e.preventDefault();
      var idx = tierBtns.indexOf(document.activeElement);
      var next;
      if (e.key === 'Home') { next = 0; }
      else if (e.key === 'End') { next = tierBtns.length - 1; }
      else { next = ((idx === -1 ? 0 : idx) + step + tierBtns.length) % tierBtns.length; }
      tierBtns[next].focus();
      selectTier(tierBtns[next]);
    });
  }

  // theme toggle (target #10): an explicit user choice always wins over the OS-level
  // prefers-color-scheme media query, by stamping [data-theme] on the root -- the CSS
  // attribute selectors are written to out-specificity the media query either direction.
  var themeBtn = document.getElementById('theme-toggle');
  if (themeBtn){
    themeBtn.addEventListener('click', function(){
      var root = document.documentElement;
      var current = root.getAttribute('data-theme');
      if (!current){
        current = (window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches) ? 'dark' : 'light';
      }
      var next = current === 'dark' ? 'light' : 'dark';
      root.setAttribute('data-theme', next);
      themeBtn.setAttribute('aria-pressed', next === 'dark' ? 'true' : 'false');
    });
  }

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

  // Sortable tables (item 4): delegated click on a <th> sort button reorders the
  // <tbody> rows by the column's data-sort-type. Server order is the initial DOM state;
  // client sort NEVER changes emitted bytes. Stable via the original-index tiebreak.
  document.querySelectorAll('table.sortable').forEach(function(tbl){
    tbl.querySelectorAll('thead th button[data-sort-col]').forEach(function(btn){
      btn.addEventListener('click', function(){
        var th = btn.closest('th');
        var col = parseInt(btn.getAttribute('data-sort-col'), 10);
        var type = btn.getAttribute('data-sort-type');
        var asc = th.getAttribute('aria-sort') !== 'ascending';
        var tbody = tbl.querySelector('tbody');
        var rows = Array.prototype.slice.call(tbody.querySelectorAll('tr'));
        var decorated = rows.map(function(r, i){
          var cell = r.children[col];
          var raw = cell ? cell.textContent.trim() : '';
          // A lower-bounded cell reads "≥N" (server-side _lb: f"≥{value}", no space): strip
          // the marker before parseFloat, or every truncated-run count collapses to NaN ->
          // -Infinity and the column silently stops sorting on the one report where the
          // reader needs it most.
          var key = (type === 'num') ? parseFloat(raw.replace(/^≥/, '')) : raw;
          if (type === 'num' && isNaN(key)) { key = -Infinity; }
          return {r: r, key: key, i: i};
        });
        decorated.sort(function(a, b){
          if (a.key < b.key) { return asc ? -1 : 1; }
          if (a.key > b.key) { return asc ? 1 : -1; }
          return a.i - b.i;
        });
        decorated.forEach(function(d){ tbody.appendChild(d.r); });
        // reset all headers to neutral, then mark the active one (indicator: shape, not color)
        tbl.querySelectorAll('thead th[aria-sort]').forEach(function(h){
          h.setAttribute('aria-sort', 'none');
          var ind = h.querySelector('.sort-ind');
          if (ind) { ind.textContent = '↕'; }
        });
        th.setAttribute('aria-sort', asc ? 'ascending' : 'descending');
        var activeInd = th.querySelector('.sort-ind');
        if (activeInd) { activeInd.textContent = asc ? '▲' : '▼'; }
      });
    });
  });

  // Header gauge drill-down accordion (item 1): each .gauge button toggles its shared
  // drawer panel; one open at a time, re-click closes.
  var gaugeBtns = document.querySelectorAll('button.gauge[aria-controls]');
  gaugeBtns.forEach(function(g){
    g.addEventListener('click', function(){
      var open = g.getAttribute('aria-expanded') === 'true';
      gaugeBtns.forEach(function(other){
        other.setAttribute('aria-expanded', 'false');
        var p = document.getElementById(other.getAttribute('aria-controls'));
        if (p) { p.hidden = true; }
      });
      var panel = document.getElementById(g.getAttribute('aria-controls'));
      if (!open && panel){ g.setAttribute('aria-expanded', 'true'); panel.hidden = false; }
    });
  });

  // WCAG 2.2 AA keyboard access: Enter/Space activate the role="button" coverage
  // matrix cells, which only had click handlers.
  function keyActivate(e){
    if (e.key === 'Enter' || e.key === ' ' || e.key === 'Spacebar'){
      if (e.key !== 'Enter'){ e.preventDefault(); }   // Space would scroll the page
      e.currentTarget.click();
    }
  }
  document.querySelectorAll('.matrix-cell').forEach(function(el){
    el.addEventListener('keydown', keyActivate);
  });

  // expand-all (print view) preserved
  var expand = document.getElementById('expand-all');
  if (expand){ expand.addEventListener('click', function(){
    views.forEach(function(v){ v.hidden = false; }); }); }

  if (views.length){ activate('view-overview'); }

  // Treemap/ladder click-to-act (item 6): clicking a cell jumps to the Friction tab (the
  // keyboard-accessible home for per-cell data) and highlights that node_key's row in the
  // sortable component table. Guarded: the tab jump is always useful; the row highlight
  // only fires when a matching row exists. Length-crit rings have pointer-events:none, so
  // only the real cell-rect / ladder-bar receives the click. NEVER interpolate `key` into a
  // selector string — a node_key derived from a POSIX path can contain ", ], or \\ and
  // would throw a DOMException; iterate the rows and compare getAttribute instead.
  var componentRows = document.querySelectorAll('tr.friction-component-row[data-node-key]');
  document.querySelectorAll('svg [data-node-key]').forEach(function(cell){
    cell.addEventListener('click', function(){
      var key = cell.getAttribute('data-node-key');
      activate('view-friction');
      var match = null;
      componentRows.forEach(function(r){
        var hit = r.getAttribute('data-node-key') === key;
        r.classList.toggle('sel', hit);
        if (hit) { match = r; }
      });
      if (match){ match.scrollIntoView({block: 'center'}); }
    });
  });

  // Live-serve progressive enhancement: when served by serve.py, subscribe to the SSE
  // /events endpoint and reload on 'refresh'. On file:// (or a STATIC one-shot artifact
  // served over plain HTTP, e.g. `python -m http.server`) there is no /events endpoint, so
  // this MUST be a silent no-op with zero behavior change (D4). The AUTHORITATIVE serve-mode
  // signal is the hm-generation <meta>: ONLY serve.py's live render emits it (a one-shot /
  // file:// render passes generation=None -> no meta). We gate EventSource construction on
  // that marker being PRESENT and parsing as an integer -- so a static artifact (which has no
  // meta) never opens '/events' and never triggers the missing-endpoint reconnect storm
  // (EventSource AUTO-RECONNECTS on error; an empty error handler does NOT stop it). The
  // http(s) protocol check is kept as an ADDITIONAL guard. The marker doubles as the
  // reconnect generation the page was rendered from: on (re)connect the server sends its
  // current generation and we reload ONLY when it is AHEAD (serverGen > pageGen), so a fresh
  // page whose gens are equal never loops and a refresh missed during a disconnect is caught
  // up on reconnect. addEventListener only, no inline handlers, no style writes (CSP preserved).
  try {
    var genMeta = document.querySelector('meta[name="hm-generation"]');
    var pageGen = genMeta ? parseInt(genMeta.getAttribute('content'), 10) : NaN;
    var isHttp = (location.protocol === 'http:' || location.protocol === 'https:');
    if (genMeta && !isNaN(pageGen) && isHttp) {
      var es = new EventSource('/events');
      es.addEventListener('refresh', function(){ location.reload(); });
      es.addEventListener('sync', function(ev){
        var serverGen = parseInt(ev.data, 10);
        if (!isNaN(serverGen) && serverGen > pageGen) { location.reload(); }
      });
      es.addEventListener('error', function(){ /* transient drop: EventSource auto-reconnects and the sync-on-reconnect catches up any missed refresh */ });
    }
  } catch (e) { /* no server / construction failed: silent no-op */ }
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
# Demo parity #5: rounded corners + inter-tile gap on every treemap rect (the
# shipped renderer drew flat, edge-to-edge tiles with no separation).
TREEMAP_CELL_RX = 6
TREEMAP_CELL_GAP = 2.5
# Deterministic label-fit budget: the `.cell-label`/ladder-name font is the mono
# stack at 12px, and a stdlib SVG renderer has no real text-metrics API to measure
# an exact glyph width, so width is estimated from a fixed px-per-char ratio
# (monospace glyphs are near-uniform width). The ratio is deliberately
# conservative — errs toward truncating a hair early rather than ever
# overflowing into a neighboring tile — which is the actual worst treemap defect
# this fixes: long basenames on small tiles stacking illegibly over each other.
_LABEL_CHAR_PX = 12 * 0.62
_LABEL_INSET_PX = 8


def _fit_label(text, avail_w):
    """Truncate `text` with an ellipsis so it fits within `avail_w` px of the
    label font — returns "" when there is no room for even one character plus
    the ellipsis (the caller then omits the `<text>` element entirely)."""
    max_chars = int((float(avail_w) - _LABEL_INSET_PX) / _LABEL_CHAR_PX)
    if max_chars < 2:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _scaled_opacity(size, max_size):
    """Map a cell's size onto the `[_OPACITY_MIN, _OPACITY_MAX]` ramp relative to the
    largest cell in the same tree, so opacity communicates relative weight (A5).
    `max_size<=0` (degenerate/empty tree) falls back to full opacity."""
    if max_size <= 0:
        return _OPACITY_MAX
    ratio = max(0.0, min(1.0, float(size) / float(max_size)))
    return _OPACITY_MIN + (_OPACITY_MAX - _OPACITY_MIN) * ratio


def _heat_bucket_map(heat):
    """Rank/quantile heat->bucket map (C2), built ONCE per render (by
    `_render_weight_view`) from the FULL heat dict and shared by every treemap/ladder
    call so a given heat value always gets the same fhN class no matter which panel
    draws it. Buckets the DISTINCT heat values, not the raw count (the old
    `min(heat_n, len(HEAT_RAMP))` flooded fh4 with every node at heat>=4) — so the top
    bucket stays a genuine minority slice on real distributions instead of a flood.
    `sorted(set(...))` (never a bare set iterated directly) so ties are a pure function
    of value, independent of dict/set iteration order or PYTHONHASHSEED (§4.4
    determinism). Zero heated nodes returns `{}` (no max()/min()/quantile on an empty
    sequence). A single distinct value has nothing to rank against — rank/k == 1.0
    deterministically resolves it to the top bucket rather than crashing or dividing
    by zero. Returns {value: bucket}; an unheated node (heat 0, absent from `heat`)
    stays bucket 0 via the caller's `bucket_map.get(heat_n, 0)`."""
    distinct = sorted({v for v in heat.values() if v})
    k = len(distinct)
    if k == 0:
        return {}
    n = len(HEAT_RAMP)
    return {v: max(1, min(n, math.ceil((i + 1) / k * n))) for i, v in enumerate(distinct)}


def _normalize_tier(value):
    """Project-tier targeting (T6): project-tier data is UNTRUSTED input (T3's threat
    model) — an adversarial/malformed sidecar could carry any string in a node's `tier`
    field. Render only ever emits one of the two known enum members into a CSS class
    attribute, never the raw value; an absent OR unrecognized value defaults to
    "operator" (back-compat, C15 — old-shape sidecars carry no `tier` key at all)."""
    return value if value in ("operator", "project") else "operator"


def _normalize_settings_tier(value):
    """T7b (`composed_settings`, T5): the settings/hooks/MCP compose chain uses its OWN
    3-way `user|project|local` tier vocabulary — distinct from `_normalize_tier`'s binary
    `operator|project` NODE tag above (skills/agents/rules/commands). The two are never
    interchangeable: a project's `.claude/settings.local.json` is "local" here but its
    skills are still tagged "project" by `_normalize_tier`. Same defensive posture as
    `_normalize_tier` — an absent or unrecognized value (a stale/hand-edited sidecar) never
    reaches a CSS class attribute raw; it defaults to "user", the vocabulary's own
    least-distinctive/muted member."""
    return value if value in ("user", "project", "local") else "user"


def _cell_title(path, heat_n, crit_lines, tier=None):
    """Self-explaining hover-`<title>` for a treemap/ladder cell. Leads with a churn
    word so a zero-friction cell reads `churn: none recorded` (never the old
    `(friction: 0)` contradiction the operator caught), and — when the length-crit
    ring is present — states explicitly that the amber ring is SIZE, not churn
    (honors the amber/friction decouple lesson). `crit_lines` is `None` for a
    non-critical cell. `tier` (T6) adds a "tier: project" note ONLY for project-tier
    cells — the common operator-tier case stays silent so the hover text is unchanged
    for every non-compose (operator-only) render."""
    churn = f"churn: {heat_n} friction record(s)" if heat_n else "churn: none recorded"
    parts = [str(path), churn]
    if crit_lines is not None:
        parts.append(f"size: {crit_lines} lines (over the {LENGTH_CRITICAL_LINES}-line cap)")
        parts.append("amber ring = oversize, NOT churn")
    if _normalize_tier(tier) == "project":
        parts.append("tier: project (this repo's .claude/)")
    return " · ".join(parts)


def _render_treemap_svg(tree, heat, dom_id, bucket_map=None, length_crit_keys=None):
    """Heat is shown two ways once the friction overlay toggle is on (never color
    alone, §UI): a CSS-class-driven stroke ramp on the cell, AND a text join-count
    badge in the corner. Both are hidden-by-default via `body.friction-on` CSS so the
    toggle button has a visible, demonstrable effect. Fill opacity is value-scaled
    (A5) via the SVG `fill-opacity` attribute — never `style=`. `bucket_map` (C2) is
    the rank/quantile value->bucket map shared with the ladder site; `_render_weight_view`
    always passes it explicitly (computed once from the full heat dict) — the `None`
    default (fallback to a locally-derived map) only serves callers/tests that render a
    single tree in isolation. `length_crit_keys` (B-t3 follow-up) is a `{node_key:
    lines}` dict — the SEPARATE, honest length-criticality signal
    (`_length_critical_node_keys`) — deliberately independent of `heat`/
    `bucket_map`: a file can be length-critical with zero friction (e.g. `review`,
    never named by any telemetry record) or vice versa. The `lines` value feeds
    `_cell_title` so the hover text explains WHY a ringed cell is ringed."""
    if bucket_map is None:
        bucket_map = _heat_bucket_map(heat)
    length_crit_keys = length_crit_keys or {}
    w, h = tree["canvas_w"], tree["canvas_h"]
    max_size = max((float(c.get("size", 0)) for c in tree["cells"]), default=0.0)
    parts = [f'<svg id="{esc_html(dom_id)}" viewBox="0 0 {_fmt_float(w)} {_fmt_float(h)}" '
             f'width="100%" height="360" role="img" aria-labelledby="{esc_html(dom_id)}-title">']
    parts.append(f'<title id="{esc_html(dom_id)}-title">Context-weight treemap</title>')
    for c in tree["cells"]:
        heat_n = heat.get(c["node_key"], 0)
        bucket = bucket_map.get(heat_n, 0)
        rect_cls = f"cell-rect heatable fh{bucket}" if bucket else "cell-rect heatable"
        opacity = _fmt_float(_scaled_opacity(c.get("size", 0), max_size))
        # Demo parity #5: inset every rect by half the gap on each side so adjacent
        # tiles show paper-colored separation instead of touching edge-to-edge, and
        # round the corners — `max(0.0, ...)` clamps a gap that would otherwise
        # exceed a tiny tile's own size to a zero (never negative) width/height.
        gx = max(0.0, float(c["x"]) + TREEMAP_CELL_GAP / 2)
        gy = max(0.0, float(c["y"]) + TREEMAP_CELL_GAP / 2)
        gw = max(0.0, float(c["w"]) - TREEMAP_CELL_GAP)
        gh = max(0.0, float(c["h"]) - TREEMAP_CELL_GAP)
        # Tier-bearing wrapper (T6, P2-8): every cell's rect/label/badge/ring/marker are
        # SIBLINGS inside one `<g class="tier-node tier-{tier}">` — a later opacity-dim
        # (T7's filter toggle, mirroring `body.friction-on .heatable`) targets the
        # wrapper and so dims the length-crit ring/marker along with the cell, not just
        # the rect. `data-node-key` stays on the rect only (the existing click-to-act
        # listener attaches per-element via querySelectorAll; putting it on both would
        # double-attach the handler).
        tier = _normalize_tier(c.get("tier"))
        parts.append(f'<g class="tier-node tier-{tier}">')
        title = _cell_title(c["path"], heat_n, length_crit_keys.get(c["node_key"]), tier)
        parts.append(
            f'<rect x="{_fmt_float(gx)}" y="{_fmt_float(gy)}" width="{_fmt_float(gw)}" '
            f'height="{_fmt_float(gh)}" rx="{TREEMAP_CELL_RX}" '
            f'fill="{esc_html(c.get("fill", "#56b4e9"))}" fill-opacity="{opacity}" '
            f'class="{rect_cls}" data-node-key="{esc_html(c["node_key"])}">'
            f'<title>{esc_html(title)}</title></rect>')
        tx, ty = _fmt_float(gx + 4), _fmt_float(gy + 14)
        # Original (un-inset) w/h decide the auto-hide threshold — the gap itself
        # shouldn't push a tile that would otherwise fit just under the cutoff.
        if float(c["w"]) > TREEMAP_LABEL_MIN_W and float(c["h"]) > TREEMAP_LABEL_MIN_H:
            # Demo parity defect (worst offender): an un-truncated basename on a
            # small-but-above-threshold tile overflowed past the tile edge into
            # neighboring tiles, stacking illegibly. Fit the label to the tile's
            # own (inset) width so it never renders wider than the tile it labels.
            fitted = _fit_label(Path(c["path"]).name, gw)
            if fitted:
                parts.append(f'<text x="{tx}" y="{ty}" class="cell-label">{esc_html(fitted)}</text>')
        if heat_n:
            # FIX (Codex P2): the badge renders for EVERY heated cell, independent of the
            # text-label threshold above — otherwise a sub-threshold cell is heat-colored
            # with no other signal, breaking the legend's "color is never the only
            # signal" claim (and WCAG use-of-color).
            bx = _fmt_float(gx + gw - 3)
            parts.append(f'<text x="{bx}" y="{ty}" text-anchor="end" '
                          f'class="friction-badge">{heat_n}</text>')
        if c["node_key"] in length_crit_keys:
            # Length-criticality outline (B-t3 follow-up): a persistent `--crit`
            # ring + corner dot, rendered as SIBLING elements that carry neither
            # `heatable` nor an `fhN` class — so `body.friction-on
            # .heatable:not(.fhN){opacity:.25}` structurally cannot dim them.
            # Always on, in both friction modes, on tiles too small for a label.
            parts.append(
                f'<rect x="{_fmt_float(gx)}" y="{_fmt_float(gy)}" width="{_fmt_float(gw)}" '
                f'height="{_fmt_float(gh)}" rx="{TREEMAP_CELL_RX}" '
                f'class="length-crit-ring" data-node-key="{esc_html(c["node_key"])}"/>')
            mx = _fmt_float(gx + max(0.0, gw - 7))
            my = _fmt_float(gy + max(0.0, gh - 7))
            parts.append(f'<circle cx="{mx}" cy="{my}" r="4" class="length-crit-marker"/>')
        parts.append("</g>")
    parts.append("</svg>")
    return "".join(parts)


# Ladder layout constants (module-level for determinism — §4.6).
_LADDER_ROW_H = 22.0
_LADDER_LABEL_W = 220.0
_LADDER_BAR_MAX_W = 300.0
_LADDER_COUNT_W = 50.0


def _render_ladder_svg(tree, heat, dom_id, bucket_map=None, length_crit_keys=None):
    """A5 alternative representation: one horizontal bar per cell instead of nested
    rectangles — same cells as the matching treemap (`tree["cells"]`), sorted by
    descending size (path as the tie-break, for a total-order determinism key, §4.4).
    Reuses the treemap's `fhN` heat-bucket logic (AM-3) so ladder bars heat too; bar
    width is the SVG `width` attribute, value-scaled to the row's max size — never
    `style=`. `bucket_map` (C2) is the SAME rank/quantile value->bucket map the
    matching treemap used — `_render_weight_view` always passes it explicitly so the
    two representations never diverge; the `None` default only serves isolated
    callers/tests. `length_crit_keys` (B-t3 follow-up) mirrors the treemap's ring —
    ladder gets the ring only (no corner marker: the truncated name column already
    runs close to the bar, and a value/badge already occupy the row's other ends)."""
    if bucket_map is None:
        bucket_map = _heat_bucket_map(heat)
    length_crit_keys = length_crit_keys or {}
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
        bucket = bucket_map.get(heat_n, 0)
        bar_cls = f"ladder-bar heatable fh{bucket}" if bucket else "ladder-bar heatable"
        size = float(c.get("size", 0))
        width = _LADDER_BAR_MAX_W * (size / max_size) if max_size > 0 else 0.0
        y = row_h * i
        # Demo parity #5/#6: same collision guard as the treemap — a long basename
        # is truncated to the fixed label column so it never runs into the bar.
        label = esc_html(_fit_label(Path(c["path"]).name, _LADDER_LABEL_W))
        text_y = _fmt_float(y + row_h - 7)
        track_y = _fmt_float(y + 3)
        track_h = _fmt_float(row_h - 6)
        # Right-aligned tabular value in the reserved count column (demo parity #6):
        # anchored at the column's own right edge, not the bar's start, so values
        # stay column-aligned regardless of bar width.
        value_x = _fmt_float(_LADDER_LABEL_W + _LADDER_BAR_MAX_W + _LADDER_COUNT_W - 4)
        # Tier-bearing wrapper (T6, P2-8) — same rationale as the treemap: wraps the
        # label/track/bar/badge/ring group so a later opacity-dim covers all of them.
        tier = _normalize_tier(c.get("tier"))
        parts.append(f'<g class="tier-node tier-{tier}">')
        title = _cell_title(c["path"], heat_n, length_crit_keys.get(c["node_key"]), tier)
        parts.append(
            f'<text x="0" y="{text_y}" class="cell-label">{label}</text>'
            f'<rect x="{_fmt_float(_LADDER_LABEL_W)}" y="{track_y}" '
            f'width="{_fmt_float(_LADDER_BAR_MAX_W)}" height="{track_h}" '
            f'rx="4" class="ladder-track"/>'
            f'<rect x="{_fmt_float(_LADDER_LABEL_W)}" y="{track_y}" '
            f'width="{_fmt_float(width)}" height="{track_h}" rx="4" '
            f'fill="{esc_html(c.get("fill", "#56b4e9"))}" class="{bar_cls}" '
            f'data-node-key="{esc_html(c["node_key"])}">'
            f'<title>{esc_html(title)}</title></rect>'
            f'<text x="{value_x}" y="{text_y}" text-anchor="end" '
            f'class="cell-label">{esc_html(c.get("size", 0))}</text>'
        )
        if heat_n:
            # FIX (Codex round-2 P2, ladder residual): mirror the treemap's
            # `friction-badge` fix — a heated ladder bar must show the join count as
            # VISIBLE text, not only via the hover-only `<title>` above. Anchored at
            # the bar's own end (like the treemap badge's `x+w-2`) so it reads against
            # the bar's fill; the shared `.friction-badge` CSS (white fill + black
            # stroke, §CSS) keeps it legible over any bar color.
            bx = _fmt_float(_LADDER_LABEL_W + width - 3)
            parts.append(f'<text x="{bx}" y="{text_y}" text-anchor="end" '
                          f'class="friction-badge">{heat_n}</text>')
        if c["node_key"] in length_crit_keys:
            # Same always-on, friction-independent ring as the treemap — a sibling
            # element with neither `heatable` nor `fhN`, so the dimming rule can't
            # touch it.
            parts.append(
                f'<rect x="{_fmt_float(_LADDER_LABEL_W)}" y="{track_y}" '
                f'width="{_fmt_float(_LADDER_BAR_MAX_W)}" height="{track_h}" rx="4" '
                f'class="length-crit-ring" data-node-key="{esc_html(c["node_key"])}"/>')
        parts.append("</g>")
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


def _render_gauge(key, label, value, delta=None, has_drill=False, band_value=None):
    """A header gauge. `has_drill=True` renders a `<button>` (item 1 accordion trigger,
    `aria-expanded`/`aria-controls` wired to the shared drawer panel) instead of an inert
    `<div>` — `class="gauge gauge-{semantic}"` and `data-gauge` are preserved in both forms
    so existing regression assertions hold either way.
    R4-2: `band_value`, when given, drives `_gauge_band`; `value` stays the DISPLAYED
    text. Default None -> band follows the displayed value, so every existing call
    site and its rendered bytes are unchanged."""
    band, semantic = _gauge_band(key, band_value if band_value is not None else value)
    band_html = f'<div class="band">{esc_html(band)}</div>' if band else ""
    delta_html = ""
    if delta:
        delta_text, delta_semantic = delta
        delta_html = (f'<div class="delta delta-{esc_html(delta_semantic)}">'
                      f'{esc_html(delta_text)}</div>')
    inner = (f'<div class="v">{esc_html(value)}</div><div class="l">{esc_html(label)}</div>'
             f'{band_html}{delta_html}')
    if has_drill:
        return (f'<button class="gauge gauge-{esc_html(semantic)}" data-gauge="{esc_html(key)}" '
                f'aria-expanded="false" aria-controls="gdrawer-{esc_html(key)}">'
                f'{inner}<span class="gauge-chev" aria-hidden="true">▾</span></button>')
    return (f'<div class="gauge gauge-{esc_html(semantic)}" data-gauge="{esc_html(key)}">'
            f'{inner}</div>')


# Which tab hosts the full detail for a count gauge (drill footer pointer text).
_GAUGE_TAB_HINT = {
    "always_loaded_file_count": "Weight", "instruction_files_over_200": "Hygiene",
    "duplicate_pair_count": "Hygiene", "phantom_ref_count": "Hygiene",
    "friction_total": "Friction",
}


def _drill_list(items, cls="gauge-drill"):
    body = "".join(f'<li>{it}</li>' for it in items) or '<li class="empty-state">none</li>'
    return f'<ul class="{cls}">{body}</ul>'


def _gauge_drill_html(key, models, doc, joined, footer, codex_aggregate):
    """Per-gauge drill content. Count gauges -> the real underlying items (`.gauge-drill`).
    Aggregate gauges -> a `.gauge-contributors` 'top contributors' set (DISTINCT class,
    explicitly NOT a complete list). friction_total -> a three-term decomposition
    (joined-events / metrics-aggregate-only / codex-runs) that reconciles to the total
    (`_friction_contributions`) — NOT a per-stream split. Every list sorts by a total key
    for cross-PYTHONHASHSEED byte-determinism."""
    hint = _GAUGE_TAB_HINT.get(key)
    tab = f'<p class="gauge-drill-tab">→ open the {esc_html(hint)} tab for the full table</p>' if hint else ""
    if key == "always_loaded_file_count":
        cells = sorted(models["context_weight"]["always"]["cells"],
                       key=lambda c: (c.get("path", ""),))
        items = [f'<code>{esc_html(c.get("path",""))}</code>' for c in cells]
        return _drill_list(items) + tab
    if key == "instruction_files_over_200":
        flags = sorted(doc.get("instruction_length_flags", []) or [],
                       key=lambda f: (-f.get("lines", 0), f.get("path", "")))
        items = [f'<code>{esc_html(f.get("path",""))}</code> — {esc_html(f.get("lines",0))} lines'
                 for f in flags]
        return _drill_list(items) + tab
    if key == "duplicate_pair_count":
        pairs = sorted((doc.get("duplication") or {}).get("pairs", []) or [],
                       key=lambda p: (-p.get("score", 0.0), p.get("a", ""), p.get("b", "")))
        items = [f'<code>{esc_html(p.get("a",""))}</code> ⇄ <code>{esc_html(p.get("b",""))}</code>'
                 for p in pairs]
        return _drill_list(items) + tab
    if key == "phantom_ref_count":
        refs = sorted(doc.get("phantom_refs", []) or [],
                      key=lambda r: (r.get("source", ""), r.get("ref", "")))
        items = [f'<code>{esc_html(r.get("source",""))}</code> → {esc_html(r.get("ref",""))}'
                 + (' <em>(unverifiable)</em>' if r.get("resolved") is None else '')
                 for r in refs]
        n_rows = len(refs)
        n_never = _phantom_never_resolvable_count(refs)
        # §7.4: the sentence that stops the operator ignoring the whole table. BOTH
        # figures derived at render time from the classified row set — no literal.
        # This sentence ASSERTS CERTAINTY, so it may only ever count kinds that are
        # never-resolvable by construction — see `_phantom_never_resolvable_count` for
        # the bar, and honour it if a kind is ever added to the group.
        never = (f'<p class="gauge-drill-note gauge-drill-faint">{n_never} of {n_rows} '
                 f'rows were never resolvable paths.</p>') if n_rows else ""
        # Requirement 12, binding disclosure: stripping `:999999` and probing only the
        # FILE makes a stale LINE reference disappear from this table. The operator must
        # not read that disappearance as "the citation was checked".
        lines = ('<p class="gauge-drill-note gauge-drill-faint">Line ranges in citations '
                 '(<code>path.md:12-19</code>) are not validated — only the file is '
                 'checked.</p>') if n_rows else ""
        return _drill_list(items) + never + lines + tab
    if key == "friction_total":
        # `_friction_contributions` keeps returning INTS — its "provably reconciles" sum is
        # the contract an existing assertion pins. The lower bound belongs here, at the
        # render site, never inside the arithmetic. Each term is bounded by the streams
        # that FEED it (post-exec Codex round 2, findings 2-3): a codex-only cap must not
        # mark the joined-telemetry and metrics terms as lower bounds, and an
        # interventions-only cap must not mark the codex term.
        items = [f'{esc_html(label)}: {esc_html(_lb(n, _CONTRIBUTION_TRUNCATION[label](footer)))}'
                 for label, n in _friction_contributions(joined, footer, codex_aggregate)]
        # Finding #11 / §14: friction_total is arithmetic and it drives a gauge, so it is
        # NOT exempt from the basename-attribution prohibition. Where the aggregate would
        # otherwise present as a DETERMINED count, it says so. The decomposition above is
        # untouched and still reconciles exactly to friction_total. The note is a
        # friction-derived count like any other and takes the same lower bound.
        interventions_events = next(
            (f.get("segments_joined", 0) for f in footer if f["stream"] == "interventions"), 0)
        note = (f'<p class="gauge-drill-note">Includes '
                f'{esc_html(_lb(interventions_events, _stream_named_truncated(footer, "interventions")))} '
                f'events attributed by basename — evidence INFERRED, not VERIFIED.</p>'
                ) if interventions_events else ""
        return _drill_list(items) + note + tab
    if key in ("always_loaded_words", "always_loaded_tokens_est"):
        # Codex/QA finding 2: `always_loaded_words` must sort and display each
        # contributor's WORD count, not its token estimate — the two fields diverge
        # per file (schema.md `always_loaded.files[].words` vs `.tokens_est`).
        field = "words" if key == "always_loaded_words" else "size"
        cells = sorted(models["context_weight"]["always"]["cells"],
                       key=lambda c: (-c.get(field, 0), c.get("path", "")))[:5]
        items = [f'<code>{esc_html(c.get("path",""))}</code> — {esc_html(c.get(field,0))}'
                 for c in cells]
        return ('<p class="gauge-contributors-label">Top contributors '
                '(largest always-loaded files — not a complete list)</p>'
                + _drill_list(items, cls="gauge-contributors"))
    return ""


def _series_points(series: dict[str, Any]) -> list[Any]:
    """The MEASURED points of one trend series — the single accessor every consumer of a
    series uses (delta verdict, sparkline geometry, the column gate).

    `.get` with a values-derived fallback, not `series["points"]`: this reads a MODEL
    dict, and the same reader-tolerance the sidecar readers apply holds here — a series
    built before `points` existed (several tests construct one literally) must degrade,
    not raise. The fallback drops exactly the unmeasured slots `build_trend_model`
    already excludes, so both paths yield measurements only."""
    points = series.get("points")
    if points is None:
        return [value for value in series.get("values", []) if value is not None]
    return list(points)


def _trend_delta(trend_model, key):
    """Polarity-aware delta vs the previous sidecar: (text, semantic) tuple where
    `semantic` is one of "good"/"bad"/"neutral", or None on first run / no comparable
    series. The series' own `polarity` (HEADLINE_KEYS, via build_trend_model) decides
    which arrow direction is good vs bad — Codex P3: a bad-direction change (e.g.
    `always_loaded_words` growing) must not render identically to a good one."""
    if trend_model.get("first_run"):
        return None
    series = next((s for s in trend_model["series"] if s["key"] == key), None)
    if not series:
        return None
    # `points`, not `values` (pre-flight exit gate): `values` carries a None wherever the
    # headline key was absent, and comparing against an unmeasured slot is exactly the
    # fabricated-verdict class this guard exists to prevent. Two real measurements are
    # the minimum for a delta; below that there is nothing to compare.
    points = _series_points(series)
    if len(points) < 2:
        return None
    cur_f = finite_number(points[-1])
    prev_f = finite_number(points[-2])
    if cur_f is None or prev_f is None:
        # A17 + A19b/S3 (P1): a corrupt/hostile sidecar value is not comparable —
        # degrade to no-delta. Previously a NaN produced ('▼ nan', 'good'): a GREEN
        # "improving" verdict, because `nan > prev` is False and a down arrow reads
        # "good" under polarity 'up'. A plausible-looking wrong verdict in the
        # reassuring direction is worse than no verdict.
        return None
    cur, prev = cur_f, prev_f
    if cur == prev:
        return ("= 0", "neutral")
    arrow = "▲" if cur > prev else "▼"   # ▲ / ▼
    delta = abs(cur - prev)
    # Step 4(c), MANDATORY: finite_number returns floats, so a bare f-string would render
    # "▲ 50.0". Three existing assertions pin the INTEGER form, and editing them would
    # fire the kill signal (CLAUDE.md binding rule 7). This shim keeps the rendered text
    # byte-identical for the integer inputs the collector actually emits, while still
    # formatting a genuine float sensibly.
    text = f"{arrow} {int(delta) if delta.is_integer() else delta}"
    polarity = series.get("polarity", "none")
    if polarity == "up":            # increasing this metric is the BAD direction
        semantic = "bad" if cur > prev else "good"
    elif polarity == "down":        # increasing this metric is the GOOD direction
        semantic = "good" if cur > prev else "bad"
    else:
        semantic = "neutral"
    return (text, semantic)


def _render_instrument_readout(headline, phantom_ref_count, phantom_confirmed_count,
                               friction_total_value, trend_model, models, doc, joined,
                               footer, codex_aggregate):
    """Item 1: every gauge is now a drill-down accordion trigger — a shared
    `.gauge-drawer` (one `.gauge-drill-panel` per gauge, closed on load) follows the
    `.gauges` row so the accordion JS (STATIC_SCRIPT) can toggle `hidden`/`aria-expanded`
    without touching emitted bytes elsewhere.
    R2-F6/R3-2/R4-2: the phantom_ref_count card is the only gauge whose displayed value
    and severity band diverge — `phantom_ref_count` is the total (every row the operator
    should see counted); `phantom_confirmed_count` (resolved=False rows only) drives the
    band, so unverifiable (resolved=null) rows never paint a false BROKEN verdict."""
    values = {"phantom_ref_count": phantom_ref_count, "friction_total": friction_total_value}
    cards, panels = [], []
    for kind, key, label in GAUGE_SPECS:
        # Codex gate finding 3: an ABSENT headline key means "not measured", never 0. The
        # trend already read it that way, so `.get(key, 0)` here put both readings of the
        # same metric on one page: `—` in the trend table, `0 / CLEAN` in the gauge.
        # NOT_MEASURED_TEXT is non-numeric, so `_gauge_band` gives it the same no-verdict
        # neutral it gives any unusable value — no band is painted at all.
        value = values[key] if kind in ("phantom", "friction") \
            else headline.get(key, NOT_MEASURED_TEXT)
        band_value = None
        if key == "phantom_ref_count":
            band_value = phantom_confirmed_count
            if phantom_confirmed_count != phantom_ref_count:
                value = f"{phantom_ref_count} ({phantom_confirmed_count} confirmed)"
        delta = _trend_delta(trend_model, key) if kind == "headline" else None
        drill = _gauge_drill_html(key, models, doc, joined, footer, codex_aggregate)
        cards.append(_render_gauge(key, label, value, delta, has_drill=bool(drill),
                                    band_value=band_value))
        if drill:
            panels.append(f'<div class="gauge-drill-panel" id="gdrawer-{esc_html(key)}" '
                          f'role="region" aria-label="{esc_html(label)} detail" hidden>'
                          f'{drill}</div>')
    drawer = f'<div class="gauge-drawer">{"".join(panels)}</div>' if panels else ""
    return f'<div class="gauges">{"".join(cards)}</div>{drawer}'


def _render_copy_disclosure(target_id, payload, summary_label):
    """Transparency-before-copy (item 2): a native <details> revealing the EXACT payload
    the copy button will grab, in a scrollable <pre>, plus an explicit inner copy button.
    Zero new JS — the inner .copy-btn reuses the existing generic data-copy-target handler;
    the <pre> shows esc_html(payload) (the same markdown the island holds). No silent
    auto-copy: copy is gated behind the inner button, exactly as the operator asked."""
    return (
        '<details class="copy-preview">'
        f'<summary>{esc_html(summary_label)} ▾</summary>'
        f'<pre class="copy-preview-body">{esc_html(payload)}</pre>'
        f'<button class="copy-btn action-btn" data-copy-target="{esc_html(target_id)}">'
        'Copy to clipboard</button>'
        '</details>'
    )


def _render_copy_controls(view_id, payload):
    """A8 per-view copy control, now wrapped in a copy-preview disclosure (item 2). The
    inert JSON island (rendered separately by _render_copy_island) still backs the copy;
    the disclosure shows the same payload for transparency."""
    return _render_copy_disclosure(f"copy-{view_id}", payload, "Copy view as markdown")


def _render_json_island(island_id, payload):
    """Shared inert data-island builder — `type="application/json"` so it is never
    counted as an executable `<script>` (CSP §9-R C); the payload is a plain markdown
    string. Both the A8 per-view copy islands and the B3/D6 per-finding brief islands
    delegate here, differing only in their id-string format."""
    return f'<script type="application/json" id="{island_id}">{esc_json_script(payload)}</script>'


def _render_copy_island(view_id, payload):
    """A8 inert data island — see `_render_json_island`."""
    return _render_json_island(f"copy-{view_id}", payload)


# --------------------------------------------------------------------------- B3/D6 action-launcher briefs
# Clipboard-only `/coding-team`-ready markdown briefs for actionable findings (dup
# pairs, over-cap files, empty Coverage Matrix cells). Each builder is a PURE
# function of its inputs — byte-identical output for the same input, never a file
# write, never a network call. They reuse the A8 `.copy-btn` + inert JSON-island
# machinery (below) so `STATIC_SCRIPT` is untouched and the script CSP hash never
# moves; only the JSON-island payload differs from the A8 per-view digest islands.
def build_consolidation_brief(pair):
    """dup pair -> `/coding-team`-ready consolidation brief. Pure function of `pair`
    (`a`, `b`, `score`, `shared_sample`) — same pair, same markdown, every call."""
    a, b = pair.get("a", ""), pair.get("b", "")
    score = pair.get("score", 0.0)
    shared = pair.get("shared_sample", "")
    pct = f"{score * 100:.0f}"
    return (
        "# Consolidate duplicate instruction pair\n\n"
        "## Finding\n"
        f"`{a}` and `{b}` overlap {pct}% (containment score {score}).\n\n"
        "## Shared content sample\n"
        f"> {shared}\n\n"
        "## Action\n"
        "Route this through `/coding-team`: review both files, merge the shared "
        "guidance into one canonical location, and update the other file to reference "
        "it (or delete it) so the harness carries the rule exactly once.\n"
    )


# Relabel the bare synthesis `outcome` token as an operator-facing instruction. Known
# synthesis values (schema.md drag-candidate enum) map explicitly; unknown/free-text
# outcome (it is semi-free synthesis prose) falls through to "Recommended: {raw}".
_DRAG_OUTCOME_LABELS = {
    "keep": "Keep — cost is justified, no action",
    "give it one home": "Consolidate — give it one canonical home",
    "load it later": "Defer — load it later (off the always-loaded path)",
    "turn it into a check": "Automate — turn it into a check/hook",
    "probation": "Demotion candidate — on probation (watch/act next cycle)",
    "retire safely": "Retire — remove it safely",
}


def _drag_outcome_label(outcome):
    label = _DRAG_OUTCOME_LABELS.get(outcome)
    if label is not None:
        return label
    return f"Recommended: {outcome}" if outcome else "No recommendation recorded"


def build_dragcandidate_brief(row):
    """drag candidate -> /coding-team-ready simplify/consolidate brief. Pure function of
    `row` ({n, surface, evidence, outcome, what_must_survive, risk_if_wrong})."""
    surface = row.get("surface", "")
    evidence = row.get("evidence", "") or "(none recorded)"
    what = row.get("what_must_survive", "") or "(none recorded)"
    risk = row.get("risk_if_wrong", "") or "(none recorded)"
    return (
        f"# Address drag candidate: {surface}\n\n"
        "## Finding\n"
        f"`{surface}` is flagged as a drag candidate. Recommendation: "
        f"{_drag_outcome_label(row.get('outcome',''))}.\n\n"
        f"## Evidence\n{evidence}\n\n"
        f"## What must survive\n{what}\n\n"
        f"## Risk if wrong\n{risk}\n\n"
        "## Action\n"
        "Route this through `/coding-team`: weigh the upkeep cost against the value, then "
        "simplify, consolidate, or demote the component while preserving what must "
        "survive above.\n"
    )


_DRAG_DEFINITION = ('A drag candidate is a component whose upkeep cost (churn / '
                    'duplication / size) may exceed its value — a candidate to simplify, '
                    'consolidate, or demote.')


def _drag_fields_details(row):
    """The three labeled synthesis fields as a native <details> (zero JS)."""
    return (
        '<details class="drag-fields"><summary>why / what to preserve ▾</summary>'
        f'<div><b>Evidence:</b> {esc_html(row.get("evidence","") or "(none recorded)")}</div>'
        f'<div><b>Must survive:</b> {esc_html(row.get("what_must_survive","") or "(none recorded)")}</div>'
        f'<div><b>Risk if wrong:</b> {esc_html(row.get("risk_if_wrong","") or "(none recorded)")}</div>'
        '</details>')


# Deterministic phantom-ref guidance keyed by `kind`; catch-all last (every mapping
# table needs an else branch). `resolved` True is the provenance-only case.
_PHANTOM_GUIDANCE = {
    "path": "Broken path — fix the link to point at the real file, or create the missing target if it should exist.",
    "external": "External ref — verify the target still exists and is correct, or remove the pointer.",
    "env_flag": "Env-flag ref — wire the flag to a real gate, or drop the mention.",
    "slash_command": "Retired slash command — both homes (commands/<name>.md, skills/<name>/SKILL.md) are absent; the rule points at a command that no longer exists. Update or drop the reference.",
    "template": "Template/placeholder reference — the token names a SHAPE (`<...>`, `{...}`, a glob, or a YYYY-MM-DD stencil), not a file, so there is nothing to resolve. No action needed unless the placeholder itself is wrong.",
    # NO `refspec` entry. Requirement 18 specifies one, but the `refspec` KIND is deferred
    # to S6c (DEVIATION 5), and a guidance string for a kind the collector never emits is
    # a dark feature. S6c adds the kind and its guidance in the same change.
}
_PHANTOM_GUIDANCE_DEFAULT = "Verify the target exists or remove the pointer."

# S2 gate fix (R2/F1): the collector now emits resolved=null / evidence=INFERRED for
# slash commands. The legacy resolved=false text above is kept VERBATIM for old sidecars
# (and is pinned by an existing test). This is the honest replacement: it leads with what
# the collector could NOT check (GP#3) and tells the operator what to DO about it (GP#15),
# instead of instructing them to delete a valid reference.
_PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE = (
    "No home for this command under the scanned root (commands/<name>.md, "
    "commands/<ns>/<name>.md, skills/<name>/SKILL.md). Claude Code BUILT-INS and plugin "
    "commands live OUTSIDE this root and cannot be checked from here — confirm the "
    "command is gone before updating or removing the reference."
    # S6b: the residual half of the §7.2 finding-#14 inversion, which was REJECTED by
    # orchestrator ruling 2026-08-01 as incoherent with this very feature — `/token` rows
    # flag RETIRED commands, and "a token is a command only if it is a KNOWN command"
    # cannot flag one by definition. Closed here by DISCLOSURE instead, the same remedy
    # §7.2 proposed for `refspec`, for the same reason: a bare single-segment `/token` is
    # lexically indistinguishable from an absolute filesystem path and the classifier
    # cannot separate them, so the label stays and the ambiguity is stated.
    # APPEND-ONLY. The four sentences above are pinned by assertions in several tests --
    # constant-equality, required substrings, negative phrase checks, and PAGE-LEVEL
    # negatives that never name this constant at all (they render it into a page and
    # assert on the page). Never edit them, and treat any word added below as subject to
    # every page-wide `not in text` assertion in the suite. The authoritative set is the
    # S6b plan's Task 3 Step 6 table; enumerate by ROUTE TO OUTPUT, never by grepping
    # this symbol's name.
    " This row may not describe a command at all: a bare single-segment `/token` is "
    "lexically indistinguishable from an absolute filesystem path, and the collector "
    "cannot tell the two apart from syntax alone."
)

# S6b QA P2: `_PHANTOM_GROUP_ORDER`'s "unverifiable" header reads "the target space
# extends outside the scanned root" — true for `external` and `slash_command`, but NOT
# for `env_flag`. An env_flag row gets resolved=null only when `_hooks_body_corpus`
# (collector.py) reports `complete=False`: a hook file INSIDE `--root` (hooks/*.py or
# hooks/*.sh) could not be read, so the negative ("no hook reads this flag") is
# unprovable. Nothing about that token's resolution space is outside the root — the
# unreadable file is IN it. The header stays kind-neutral (it is pinned verbatim by
# test_rendered_phantom_table_carries_three_group_headers and A27 permits tighten-only,
# never loosening, correction of a same-execution assertion — this one predates this
# execution), so the correction lives at the row level instead: this guidance sits
# directly beside the row and states the REAL reason. No verdict words (binding rule 6)
# — this states what happened, not a judgment about it.
_PHANTOM_GUIDANCE_ENV_FLAG_UNVERIFIABLE = (
    "A hook file inside the scanned root (hooks/*.py or hooks/*.sh) could not be read, "
    "so the collector cannot prove no hook consumes this flag by literal-string search "
    "of hook bodies. This is not the same case as a target outside the scanned root — "
    "the unreadable file is IN this root, not beyond it. The unreadable path is listed "
    "in the Inaccessible card. Confirm whether that file references this flag before "
    "deciding whether to wire it to a gate or drop the mention."
)


def _resolved_state(resolved):
    """The ONE derivation of the tri-state `resolved` — True / False / None-meaning-
    unverifiable — for every consumer that reads it.

    Pre-flight exit gate: `_resolved_label` mapped hostile shapes to "unverifiable" by
    identity while `_phantom_guidance` branched on TRUTHINESS, so a non-bool truthy value
    out of an untrusted sidecar made one row say "unverifiable" in the Resolved column and
    "Resolved at collection time — no action needed" in the guidance beside it. Two
    policies for one field is how that happens; this is the single policy.

    IDENTITY, never truthiness or a dict lookup: `resolved` arrives straight from sidecar
    JSON, so it can be any JSON value, and `dict.get` HASHES its key (a `resolved: []`
    would raise TypeError: unhashable type). Anything that is not literally True or False
    is a value the collector did not produce, and the honest reading of a value we cannot
    interpret is the same as the one we could not check: unverifiable."""
    if resolved is True:
        return True
    if resolved is False:
        return False
    return None


# S6b / D4 §7.4. The three groups encode the ONE distinction the operator must read
# instantly: "we checked and it's missing" vs "there was never anything to check" vs "we
# could not see far enough to check". Group membership is derived from SEMANTICS, not from
# a kind allowlist, so a kind added later cannot silently vanish from the table.
_NEVER_RESOLVABLE_KINDS = frozenset({"template"})

_PHANTOM_GROUP_ORDER: tuple[tuple[str, str], ...] = (
    ("verified_missing", "Verified missing — the target was looked for and is not there"),
    ("not_a_path", "Not a path — a template or glob; nothing was ever resolvable, so "
                   "nothing was checked"),
    ("unverifiable", "Unverifiable — the target space extends outside the scanned root"),
)


def _phantom_group_key(row: dict[str, Any]) -> str:
    """Which of the three §7.4 groups a row belongs to. TOTAL by construction: the last
    return is an unconditional catch-all, so an unrecognized `kind` lands in Unverifiable
    rather than disappearing from the table (every mapping table needs an else branch).

    Reads `resolved` through `_resolved_state`, the shared identity-based policy — never
    truthiness and never a dict lookup, because `resolved` arrives straight from sidecar
    JSON and can be any JSON value. The kind check comes FIRST so that a hostile sidecar
    claiming `{"kind": "template", "resolved": false}` still lands in `not_a_path`: a
    shape classification can never carry a confirmed negative.

    Same T3.1 invariant as `_phantom_guidance`: `kind` arrives straight from sidecar JSON,
    so an unhashable shape (`[]`, `{}`) is valid JSON a stale/corrupt/hand-crafted sidecar
    can carry, and `in _NEVER_RESOLVABLE_KINDS` (a frozenset membership test) HASHES its
    operand. Only a `str` can match a `_NEVER_RESOLVABLE_KINDS` entry, so the `isinstance`
    guard is a pure short-circuit before the hash — it changes no row's group, and
    catching this here (rather than upstream) keeps the existing per-row degradation
    doctrine intact instead of trading a whole-page failure for a whole-page render."""
    kind = row.get("kind", "")
    if isinstance(kind, str) and kind in _NEVER_RESOLVABLE_KINDS:
        return "not_a_path"
    if _resolved_state(row.get("resolved")) is False:
        return "verified_missing"
    return "unverifiable"


def build_phantom_groups(rows):
    """Rows -> [(group_key, header_text, [rows])] in _PHANTOM_GROUP_ORDER, input order
    preserved within each group. Pure. EMPTY groups are returned too, so this is the ONE
    derivation both the table and the tile drawer read — a second count is how §7.4's
    hardcoded `2/6/1` drifted out of agreement with its own after-table."""
    bucketed: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in _PHANTOM_GROUP_ORDER}
    for row in rows:
        bucketed[_phantom_group_key(row)].append(row)
    return [(key, header, bucketed[key]) for key, header in _PHANTOM_GROUP_ORDER]


def _phantom_never_resolvable_count(rows):
    """Rows that were NEVER resolvable paths — the `not_a_path` group.

    THE NAME OF THIS COUNT IS A CLAIM OF CERTAINTY, AND EVERY KIND IN THE GROUP MUST
    EARN IT. Today the group holds `template` alone: a stencil names a SHAPE, so there
    is no target, and no filesystem state and no future evidence could make one appear.
    That is never-resolvable BY CONSTRUCTION, which is exactly what this sentence
    asserts.

    THE BAR FOR ADDING A KIND TO `not_a_path`, and it is the guardrail S6c will need:
    a kind belongs in this group only if NO POSSIBLE EVIDENCE could resolve it. An
    INFERENTIAL kind does not qualify -- it must either stay out of the group, or this
    count and the drawer sentence must change with it to stop asserting certainty they
    no longer have. The deferred `refspec` kind is precisely that case: its own guidance
    would tell the operator the token may be a real path, and one layer hedging while
    three assert is how a confident false claim survives the fix aimed at it. When S6c
    brings `refspec` back, this docstring is the check it has to pass.

    Codex P2-4 already narrowed this once, dropping `unverifiable` (`external` and
    `slash_command` MAY resolve outside the scanned root -- a CC built-in, a plugin
    command, a file the walk cannot see).

    DERIVED from the single grouping, never counted a second way, so the drawer and the
    table can never disagree."""
    return len(build_phantom_groups(rows)[1][2])


def _phantom_status_word(kind, resolved):
    """§7.4: the Status word for the never-resolvable kinds is `not a path`, NEVER `no`.
    `no` answers "does it exist", and these tokens were never asked that question. Every
    other kind keeps `_resolved_label`'s three words unchanged.

    Same T3.1 invariant as `_phantom_guidance`/`_phantom_group_key`: `kind` arrives
    straight from sidecar JSON, so an unhashable shape is valid JSON a corrupt sidecar
    can carry, and `in _NEVER_RESOLVABLE_KINDS` hashes its operand. The `isinstance`
    guard is a pure short-circuit — only a `str` can match a `_NEVER_RESOLVABLE_KINDS`
    entry — so it changes no row's status word, only which branch reaches it safely."""
    if isinstance(kind, str) and kind in _NEVER_RESOLVABLE_KINDS:
        return "not a path"
    return _resolved_label(resolved)


def _phantom_guidance(kind, resolved):
    state = _resolved_state(resolved)
    if state is True:
        return "Resolved at collection time — listed for provenance; no action needed."
    if state is None and kind == "slash_command":
        return _PHANTOM_GUIDANCE_SLASH_UNVERIFIABLE
    # S6b QA P2: same shape as the slash_command override immediately above — the
    # unverifiable env_flag row's real cause is a hook file the collector could not
    # read, not a target outside the scanned root, so it needs its own text rather
    # than falling through to the resolved=False dict entry ("wire the flag to a real
    # gate, or drop the mention"), which asserts a confirmed negative this state does
    # not have.
    if state is None and kind == "env_flag":
        return _PHANTOM_GUIDANCE_ENV_FLAG_UNVERIFIABLE
    # T3.1: same invariant as `_tokens_treemap` (render_html.py:488-494) -- `kind` arrives
    # straight from sidecar JSON, so an unhashable shape (`[]`, `{}`) is valid JSON a
    # stale/corrupt/hand-crafted sidecar can carry. `dict.get` HASHES its key, so an
    # unguarded lookup would raise TypeError and, via `_RENDER_FALLBACK_ERRORS`, turn one
    # malformed row into a whole-page render failure -- the doctrine here is per-row
    # degradation, never that. Only a `str` can be a real kind; anything else falls
    # through to the catch-all, same as an unknown string kind already does.
    if not isinstance(kind, str):
        return _PHANTOM_GUIDANCE_DEFAULT
    return _PHANTOM_GUIDANCE.get(kind, _PHANTOM_GUIDANCE_DEFAULT)


# S2 gate fix: the tri-state `resolved` rendered verbatim, so the new null shape would
# print a bare Python "None" in the operator-facing Resolved column. Three states, three
# words. `unverifiable` is the honest label for null: the collector could not see the
# whole resolution space, so it neither confirmed nor denied.
# MUST be total. Do NOT use a dict lookup here: `resolved` arrives straight from sidecar
# JSON, and `dict.get` HASHES its key, so a corrupt/hostile `resolved: []` or `{}` raises
# TypeError: unhashable type. The line this replaces (`esc_html(r.get("resolved"))`)
# accepted anything, so a dict form would ADD a fault path in the one batch whose purpose
# is removing them. Totality now comes from `_resolved_state` — the shared policy — so
# this function only names the three states; it no longer re-derives them.
def _resolved_label(resolved):
    state = _resolved_state(resolved)
    return {True: "yes", False: "no", None: "unverifiable"}[state]


def build_phantom_ref_brief(ref):
    """phantom ref -> /coding-team-ready fix brief. Pure function of `ref`
    ({source, ref, kind, resolved}) — same ref, same markdown, every call."""
    source = ref.get("source", "")
    r = ref.get("ref", "")
    kind = ref.get("kind", "")
    resolved = ref.get("resolved", False)
    # S6b QA P1: mirrors `_phantom_group_key`/`_phantom_status_word`'s kind-first check
    # (isinstance guard because `kind` arrives straight from sidecar JSON and `in
    # _NEVER_RESOLVABLE_KINDS` hashes its operand — an unhashable kind must short-circuit
    # before the hash, same reasoning as those two functions). A stencil is a SHAPE, not
    # a target: there was never a resolution space for it to extend outside of, and a
    # CORRECT template still renders as a template row afterward, so neither the Finding
    # nor the Action may claim otherwise — this is the feature's flagship live example
    # and the fourth consumer `_phantom_guidance`/`_phantom_group_key`/`_phantom_status_word`
    # already special-case for `template`.
    if isinstance(kind, str) and kind in _NEVER_RESOLVABLE_KINDS:
        finding = (f"`{source}` points at `{r}` (kind: {kind}) — a stencil naming a "
                   f"SHAPE (`<...>`, `{{...}}`, a glob, or a YYYY-MM-DD pattern), not a "
                   f"file. There was never a target to resolve.\n\n")
        action = (
            "No action needed unless the placeholder text itself is wrong. This is not "
            "a broken reference: a correct template still renders as a template row, so "
            "re-running `/harness-map` will not make the row disappear.\n"
        )
    # S6b QA P3: third surface of the same defect class as P1 (template) and P2
    # (_phantom_guidance's env_flag text) — this function's own generic
    # `_resolved_state(resolved) is None` branch below claims "the resolution space
    # extends outside the scanned root", which is false for env_flag: resolved=null there
    # means `_hooks_body_corpus` (collector.py) reported an unreadable hook file INSIDE
    # `--root`, the same real cause `_PHANTOM_GUIDANCE_ENV_FLAG_UNVERIFIABLE` already names
    # in the "What to do" section beside it. Leaving this branch generic put a claim in
    # the Finding that contradicted the guidance immediately below it, in the same
    # document. The Action must not promise the row disappears on re-run (nothing about
    # the hooks corpus changes by editing `source`) and must not tell the operator to
    # correct or remove the reference — the reference is not the problem, the unreadable
    # hook file is.
    elif kind == "env_flag" and _resolved_state(resolved) is None:
        finding = (f"`{source}` points at `{r}` (kind: {kind}) — a hook file inside the "
                   f"scanned root could not be read, so the collector cannot prove no "
                   f"hook consumes this flag.\n\n")
        action = (
            "Read the unreadable hook file listed in the Inaccessible card first — it is "
            f"the blocker, not the reference in `{source}`. If it references this flag, "
            "route the fix through `/coding-team`; if not, no action is needed here.\n"
        )
    else:
        # Same shared policy as the table row beside it (`_resolved_state`), so the
        # brief and the Resolved column can never describe one row differently.
        if _resolved_state(resolved) is None:
            finding = (f"`{source}` points at `{r}` (kind: {kind}), which the collector "
                       f"could not verify — the resolution space extends outside the "
                       f"scanned root.\n\n")
        else:
            finding = (f"`{source}` points at `{r}` (kind: {kind}) which does not resolve "
                       f"to a real target.\n\n")
        action = (
            "Route this through `/coding-team`: correct or remove the reference in "
            f"`{source}`, then re-run `/harness-map` to confirm the phantom ref is gone.\n"
        )
    return (
        "# Fix phantom reference\n\n"
        "## Finding\n"
        f"{finding}"
        "## What to do\n"
        f"{_phantom_guidance(kind, resolved)}\n\n"
        "## Action\n"
        f"{action}"
    )


def build_refactor_brief(flag):
    """over-cap instruction file -> `/coding-team`-ready refactor brief. Pure function
    of `flag` (`path`, `lines`)."""
    path = flag.get("path", "")
    lines = flag.get("lines", 0)
    return (
        "# Refactor oversized instruction file\n\n"
        "## Finding\n"
        f"`{path}` is {lines} lines — over the 200-line instruction cap.\n\n"
        "## Action\n"
        f"Route this through `/coding-team`: split `{path}` into focused, "
        "single-purpose files, or move on-demand detail behind a reference doc, "
        "until it is back under the cap.\n"
    )


def build_gap_stub_brief(verb, surface):
    """empty Coverage Matrix cell -> `/coding-team`-ready gap-fill brief. Pure
    function of `verb` and `surface` (the VERBS x SURFACES cell key)."""
    return (
        f"# Fill roadmap gap: {verb} × {surface}\n\n"
        "## Finding\n"
        f"The Coverage Matrix cell for **{verb}** × **{surface}** is empty — no "
        f"harness component currently {verb.lower()}s behavior via the {surface} "
        "surface.\n\n"
        "## Action\n"
        "Route this through `/coding-team`: design and implement a minimal component "
        "that closes this gap, then record it in the harness-synthesis sidecar's "
        "Coverage Matrix entry for this cell (verdict `thin` or `covered`) with "
        "evidence.\n"
    )


def _render_brief_island(kind, index, markdown):
    """B3/D6 inert data island for one action-launcher brief — see `_render_json_island`,
    keyed `brief-{kind}-{index}` so each finding's island id is derived deterministically
    from its position in the finding's own (already-deterministic) render order."""
    return _render_json_island(f"brief-{kind}-{index}", markdown)


def _render_brief_control(kind, index, markdown, summary_label="Copy brief"):
    """Per-finding action brief: the inert island + a copy-preview disclosure that reveals
    the exact brief markdown before copy. Replaces the bare _render_brief_button/island
    pairing at every finding site (dup, overcap, gap, phantom, drag)."""
    return (_render_brief_island(kind, index, markdown)
            + _render_copy_disclosure(f"brief-{kind}-{index}", markdown, summary_label))


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
    # QA exit gate (HIGH 1): the dot's severity keys off the BAND value, which for phantom
    # refs is the confirmed subset -- the displayed number stays the total. Same
    # display/band split as the header gauge, so the two can no longer contradict.
    hyg_bands = overview_model.get("hygiene_band_values", {})
    hyg_html = "".join(
        f'<li>{_sev_dot(_gauge_band(band_key, hyg_bands.get(mkey, hyg[mkey]))[1])}'
        f'{esc_html(label)}: {esc_html(hyg[mkey])}</li>'
        for mkey, band_key, label in _HYGIENE_DIGEST_SPECS)
    drag_rows = overview_model["drag_candidates"]
    if drag_rows:
        drag_html = "".join(
            f'<li>{_sev_dot("bad" if r.get("outcome") == "probation" else "warn")}'
            f'#{esc_html(r.get("n",""))} {esc_html(r.get("surface",""))} '
            f'<span class="badge">{esc_html(_drag_outcome_label(r.get("outcome","")))}</span>'
            f'{_drag_fields_details(r)}'
            f'{_render_brief_control("dragov", i, build_dragcandidate_brief(r))}</li>'
            for i, r in enumerate(drag_rows))
    else:
        drag_html = f'<li>{_sev_dot("good")}no drag candidates flagged</li>'
    return (
        '<div class="card"><h2>Needs attention</h2>'
        f'<div class="digest-group"><h3>Roadmap gaps ({len(gaps)})</h3><ul>{gaps_html}</ul></div>'
        f'<div class="digest-group"><h3>Weight tax (top always-loaded)</h3><ul>{tax_html}</ul></div>'
        f'<div class="digest-group"><h3>Hygiene</h3><ul>{hyg_html}</ul></div>'
        f'<div class="digest-group"><h3>Drag candidates ({len(drag_rows)})</h3>'
        f'<p class="digest">{esc_html(_DRAG_DEFINITION)}</p><ul>{drag_html}</ul></div>'
        '</div>'
    )


def _render_friction_hero(friction_model):
    """AM-2 hero card — friction COUNT + band + top drag candidates. Color-coded via
    `hero-friction-{semantic}` (Task-2 CSS) only — no `node_key`/heat markers here
    (RESOLVED DECISION 1: friction on Overview is a count, not node-keyed heat)."""
    top_drag = friction_model["top_drag"]
    top_drag_html = "".join(
        f'<li>#{esc_html(r.get("n",""))} {esc_html(r.get("surface",""))} '
        f'<span class="badge">{esc_html(_drag_outcome_label(r.get("outcome","")))}</span></li>'
        for r in top_drag
    ) or '<li class="empty-state">none</li>'
    # A suppressed band leaves NO element, not an empty one — an empty badge still reads as
    # a verdict pill. When a band exists the emitted bytes are unchanged: the leading space
    # already sat between `events` and the span.
    band = friction_model["band"]
    band_html = f' <span class="badge">{esc_html(band)}</span>' if band else ""
    return (
        f'<div class="hero-friction hero-friction-{esc_html(friction_model["semantic"])}">'
        '<h2>Friction</h2>'
        f'<p class="count">{esc_html(friction_model["count"])} events{band_html}</p>'
        f'<h3>Top drag candidates</h3><p class="digest">{esc_html(_DRAG_DEFINITION)}</p>'
        f'<ul>{top_drag_html}</ul>'
        '</div>'
    )


# A4: the cell selected by default on first render. Must exist in VERBS x SURFACES.
COVERAGE_PRESELECT = ("Constrain", "memory")


def _render_coverage_matrix_body(civc, date):
    """The Coverage Matrix legend + `.matrix` card-grid + sticky `.inspector` — the
    matrix/inspector half of the merged Overview view (Task B-t2 tab merge folded the
    former standalone `_render_coverage_view` in here). Every one of the 36 verb x
    surface cells is pre-rendered as a clickable `.cell.matrix-cell` plus a matching
    `.inspector-panel`; client-side selection (the shared script) just toggles the
    `sel` class / `hidden` attribute — no data is computed or fetched at click time."""
    if not civc["available"]:
        return (f'<p class="empty-state">Coverage Matrix unavailable — no '
                f'<code>harness-synthesis-{esc_html(date)}.json</code> in this report directory. '
                f'Re-run <code>/harness-map</code> Step B to generate it.</p>')
    legend = (
        '<p class="civc-legend">Coverage scale (empty cells are intentional roadmap, not blanks): '
        '<span class="badge verdict-empty">empty</span> → '
        '<span class="badge verdict-thin">thin</span> → '
        '<span class="badge verdict-covered">covered</span>. '
        'Cells with a "note" expose it via a details toggle.</p>'
    )
    by_key = {(c["verb"], c["surface"]): c for c in civc["cells"]}
    grid_cells = ['<div class="mhead"></div>']
    grid_cells += [f'<div class="mhead">{esc_html(s)}</div>' for s in SURFACES]
    panels = []
    gap_index = 0   # B3/D6: brief index for empty cells, incremented in the SAME
                     # fixed VERBS x SURFACES traversal order the grid itself uses —
                     # deterministic regardless of which cells the synthesis provided.
    for verb in VERBS:
        grid_cells.append(f'<div class="mhead mhead-row">{esc_html(verb)}</div>')
        for surface in SURFACES:
            c = by_key.get((verb, surface), {"verdict": "empty", "evidence": None, "note": ""})
            verdict = c.get("verdict", "empty")
            cell_id = f"{verb}-{surface}"
            preselect = (verb, surface) == COVERAGE_PRESELECT
            sel_token = " sel" if preselect else ""
            # Fixed attribute order — `class` FIRST, `sel` token AFTER the verdict
            # token, then `data-cell-id` — the A4 preselect test asserts this exact
            # string; do not reorder.
            grid_cells.append(
                f'<div class="cell matrix-cell verdict-{esc_html(verdict)}{sel_token}" '
                f'data-cell-id="{esc_html(cell_id)}" role="button" tabindex="0" '
                f'aria-label="{esc_html(verb)} × {esc_html(surface)}: {esc_html(verdict)}">'
                f'<span class="cv">{esc_html(verdict)}</span>'
                f'<span class="dot" aria-hidden="true"></span></div>'
            )
            evidence = c.get("evidence")
            note = c.get("note") or ""
            evidence_html = (f'<p class="evidence">{esc_html(evidence)}</p>' if evidence
                              else '<p class="evidence empty-state">no evidence recorded</p>')
            note_html = (f'<details><summary>note</summary>{esc_html(note)}</details>' if note
                         else '<p class="empty-state">no note</p>')
            if verdict == "empty":
                brief_html = _render_brief_control("gap", gap_index, build_gap_stub_brief(verb, surface))
                gap_index += 1
            else:
                brief_html = ""
            hidden_attr = "" if preselect else " hidden"
            panels.append(
                f'<div class="inspector-panel" data-cell-id="{esc_html(cell_id)}"{hidden_attr}>'
                f'<p class="surface-tag">{esc_html(surface)}</p>'
                f'<p class="verb-tag">{esc_html(verb)}</p>'
                f'<span class="badge verdict-{esc_html(verdict)}">{esc_html(verdict)}</span>'
                f'{evidence_html}{note_html}{brief_html}</div>'
            )
    matrix_html = f'<div class="matrix">{"".join(grid_cells)}</div>'
    return (
        legend + '<div class="coverage-grid">'
        f'<div class="overflow-x">{matrix_html}</div>'
        f'<aside class="inspector">{"".join(panels)}</aside></div>'
    )


def _render_tier_summary_band(doc):
    """Project-tier composition summary (T6, P2-7 precision) — "project adds N /
    overrides M" on the Overview view, sourced from `doc["tier_composition"]` (T4's
    additive, compose-mode-only field). Absent entirely on a non-compose or old-shape
    sidecar (C15 back-compat): returns "" so the Overview view — and every existing
    byte-determinism assertion built against a tier-less fixture — is unaffected.

    Per-surface rollup reads `surfaces[<surface>]["merge"]` to distinguish UNION
    surfaces (every project entry is an "add", never an override/dark — R5-A) from
    SHADOW surfaces (add/override/dark all meaningful). The dark-skill callout lists
    every `status:"shadowed"` PROJECT node — a project skill/command/agent the
    operator-tier (or, for agents, user-tier) collision winner shadows out, i.e.
    defined but never runs."""
    tc = doc.get("tier_composition")
    if not tc:
        return ""
    surfaces = tc.get("surfaces", {}) or {}
    participating = tc.get("participating_surfaces") or sorted(surfaces)
    nodes = tc.get("nodes", []) or []
    total_adds = sum(s.get("adds", 0) for s in surfaces.values())
    total_overrides = sum(s.get("overrides", 0) for s in surfaces.values())
    total_dark = sum(s.get("dark", 0) for s in surfaces.values())

    def _surface_row(surface):
        s = surfaces.get(surface, {})
        merge = s.get("merge", "shadow")
        adds = s.get("adds", 0)
        if merge == "union":
            detail = f'<span class="badge tier-project">{esc_html(adds)} add(s)</span> (union — both tiers load)'
        else:
            winner = s.get("winner_tier") or "n/a"
            detail = (
                f'<span class="badge tier-project">{esc_html(adds)} add(s)</span> '
                f'<span class="badge tier-project">{esc_html(s.get("overrides", 0))} override(s)</span> '
                f'<span class="badge tier-dark">{esc_html(s.get("dark", 0))} dark</span> '
                f'(shadow — {esc_html(winner)} wins a collision)'
            )
        return f'<li><span class="tier-surface">{esc_html(surface)}</span>{detail}</li>'

    rows_html = "".join(_surface_row(s) for s in participating)

    dark_nodes = [n for n in nodes if n.get("status") == "shadowed" and n.get("tier") == "project"]
    dark_html = ""
    if dark_nodes:
        items = "".join(
            f'<li><span class="badge tier-dark">dark</span> '
            f'{esc_html(n.get("surface", ""))}:{esc_html(n.get("name", ""))} '
            f'<span class="script-desc">{esc_html(n.get("path", ""))} — shadowed by operator '
            f'{esc_html((n.get("shadowed_by") or {}).get("path", ""))}, defined but never runs</span></li>'
            for n in dark_nodes)
        dark_html = f'<div class="tier-dark-callout"><h3>Dark project skills &amp; commands</h3><ul>{items}</ul></div>'

    return (
        '<div class="card tier-summary" id="tier-summary"><h2>Project-tier composition</h2>'
        f'<p class="digest">project adds {esc_html(total_adds)} / overrides {esc_html(total_overrides)}'
        f'{f" / {esc_html(total_dark)} dark" if total_dark else ""}</p>'
        f'<ul class="tier-surface-list">{rows_html}</ul>'
        f'{dark_html}</div>'
    )


def _render_overview_view(overview_model, civc, date, copy_payload, doc):
    """Merged Overview + Coverage tab (Task B-t2 tab merge — the two former tabs
    overlapped: Overview's mini-grid duplicated Coverage's full matrix at a smaller
    scale). Main area: the full `.matrix` card-grid + its sticky `.inspector`
    (`_render_coverage_matrix_body`). Sidebar: the friction hero card + "Needs
    attention" digest, retained verbatim from the former Overview tab. RESOLVED
    DECISION 1 still holds: no friction heat markers (`heatable`/`fhN`/`node_key`)
    anywhere in this view — friction here stays a count, never node-keyed heat.
    `doc` (T6) feeds `_render_tier_summary_band`, which renders "" absent
    `tier_composition` — a non-compose render's markup is unaffected."""
    matrix_body = _render_coverage_matrix_body(civc, date)
    tier_summary = _render_tier_summary_band(doc)
    return (
        '<section id="view-overview" class="view" role="tabpanel" '
        'aria-labelledby="view-btn-overview" tabindex="-1">'
        f'<div class="view-toolbar">{_render_copy_controls("overview", copy_payload)}</div>'
        f'{tier_summary}'
        '<div class="overview-grid">'
        '<div class="card"><h2>Coverage Matrix</h2>'
        '<p class="subtitle">six verbs (what the harness does to behavior) '
        '× six surfaces (what it’s made of)</p>'
        f'{matrix_body}</div>'
        f'<div>{_render_friction_hero(overview_model["friction"])}{_render_overview_digest(overview_model)}</div>'
        '</div>'
        '</section>'
    )


def _render_unrenderable_note(tree):
    """Control 2 disclosure: a file whose size value is unusable (non-numeric, NaN/inf,
    out of range) has no AREA and cannot be drawn. Silently dropping it is the S4 defect —
    the file vanishes from the Weight view with no error and no count. This makes the
    omission VISIBLE. Renderer-local model state: no new gauge, no band, no schema field."""
    paths = tree.get("unrenderable") or []
    if not paths:
        return ""
    files_word = "file" if len(paths) == 1 else "files"
    listed = ", ".join(f"<code>{esc_html(p)}</code>" for p in paths)
    return (f'<p class="empty-state">{len(paths)} {files_word} omitted from this map: '
            f'no usable size value — {listed}</p>')


def _render_weight_view(model, heat, friction_enabled, doc, copy_payload):
    """Verbatim body of the former `_render_context_weight_tab`, now also carrying the
    friction legend + overlay toggle (moved here — heat only ever lands on these two
    treemaps, RESOLVED DECISION 1) and the A5/AM-3 treemap<->ladder toggle: both
    representations are pre-rendered for both panels so the client just flips a CSS
    class (§ progressive-enhancement pattern) — no re-render at click time.
    `friction_enabled=False` (i.e. `--no-friction`) omits the toggle button + legend
    entirely (Codex P3). §C1 change 4 (Codex-caught): `friction_enabled=True` alone is
    NOT enough — a loaded-but-all-ambiguous/unmatched run leaves `heat={}`, and an active
    toggle over zero heated nodes would only dim EVERY cell to 0.25 with nothing
    highlighted. The toggle + legend now require BOTH friction_enabled AND at least one
    heated node; the loaded-but-empty case renders a plain 'no node-attributed friction'
    note instead of a toggle with nothing to reveal. C2: `bucket_map` is computed ONCE
    here from the full heat dict (both panels combined) and passed to all four render
    calls, so a given heat value gets the same fhN bucket everywhere in this view.
    `doc` (B-t3 follow-up) feeds `_length_critical_node_keys` — computed ONCE here,
    same pattern as `bucket_map`, and passed to all four calls so a length-critical
    file gets the ring consistently across both representations."""
    bucket_map = _heat_bucket_map(heat)
    length_crit_keys = _length_critical_node_keys(doc)
    always_omitted = _render_unrenderable_note(model["always"])
    ondemand_omitted = _render_unrenderable_note(model["on_demand"])
    always_treemap = _render_treemap_svg(model["always"], heat, "treemap-always", bucket_map, length_crit_keys)
    always_ladder = _render_ladder_svg(model["always"], heat, "ladder-always", bucket_map, length_crit_keys)
    ondemand_treemap = _render_treemap_svg(model["on_demand"], heat, "treemap-ondemand", bucket_map, length_crit_keys)
    ondemand_ladder = _render_ladder_svg(model["on_demand"], heat, "ladder-ondemand", bucket_map, length_crit_keys)
    show_toggle = friction_enabled and bool(heat)
    toggle_html = (
        '<button class="action-btn" id="friction-toggle" aria-pressed="false">'
        'Show friction heat on treemap + ladder cells</button>'
    ) if show_toggle else (
        '<p class="empty-state" id="friction-empty-note">no node-attributed friction — '
        'telemetry loaded but nothing joined to a map component</p>'
    ) if friction_enabled else ""
    legend_html = (
        '<div class="friction-legend" id="friction-legend">'
        '<span>Friction heat, once the overlay is on:</span>'
        '<span class="legend-entry"><span class="legend-swatch fh0"></span>none</span>'
        '<span class="legend-entry"><span class="legend-swatch fh1"></span>light</span>'
        '<span class="legend-entry"><span class="legend-swatch fh2"></span>some</span>'
        '<span class="legend-entry"><span class="legend-swatch fh3"></span>frequent</span>'
        '<span class="legend-entry"><span class="legend-swatch fh4"></span>most-active</span>'
        '<span class="legend-note">every heated cell also shows a join-count '
        'badge in the corner (color is never the only signal)</span></div>'
    ) if show_toggle else ""
    # Length-crit legend (B-t3 follow-up): ALWAYS visible whenever at least one
    # length-critical file exists — independent of `show_toggle`/`friction_enabled`,
    # since the ring itself is always-on regardless of friction mode.
    length_crit_legend_html = (
        '<div class="friction-legend" id="length-crit-legend">'
        '<span class="legend-entry"><span class="legend-swatch length-crit-swatch">'
        '</span>critically oversized file (&gt;'
        f'{esc_html(LENGTH_CRITICAL_LINES)} lines, from the Hygiene tab — always '
        'shown, independent of friction)</span></div>'
    ) if length_crit_keys else ""
    return (
        '<section id="view-weight" class="view" role="tabpanel" '
        'aria-labelledby="view-btn-weight" tabindex="-1">'
        '<div class="view-toolbar">'
        '<div class="seg" id="weight-mode" role="group" aria-label="weight representation">'
        '<button class="seg-btn" data-mode="treemap" aria-pressed="true">▦ Treemap</button>'
        '<button class="seg-btn" data-mode="ladder" aria-pressed="false">▤ Ladder</button>'
        '</div>'
        f'{toggle_html}'
        f'{_render_copy_controls("weight", copy_payload)}'
        '</div>'
        f'{legend_html}'
        f'{length_crit_legend_html}'
        '<p class="subtitle">On-demand skills cost only when invoked; MEMORY.md + '
        'CLAUDE.md are the real per-turn tax — the treemap/ladder toggle shows the '
        'same weights two ways.</p>'
        f'{_render_transparency_note(doc)}'
        '<div class="card"><h2>Always-loaded (by category, sized by est. tokens)</h2>'
        f'<div class="treemap-panel">{always_treemap}</div>'
        f'<div class="ladder-panel">{always_ladder}</div>{always_omitted}</div>'
        '<div class="card"><h2>On-demand (skills / phases / prompts / agents / memory, sized by words)</h2>'
        f'<div class="treemap-panel">{ondemand_treemap}</div>'
        f'<div class="ladder-panel">{ondemand_ladder}</div>{ondemand_omitted}</div></section>'
    )


def _render_bipartite_body(model):
    def _row(n, side):
        badge = ""
        if side == "right":
            cls = {"direct": "direct", "dispatcher": "dispatcher", "none": "orphan"}[n["registered_via"]]
            badge = f'<span class="badge {cls}">{esc_html(n["registered_via"])}</span>'
        label = esc_html(n.get("name") or n.get("command") or n.get("script", ""))
        desc = ""
        if side == "right":
            d = n.get("description", "")
            desc = (f'<span class="script-desc">{esc_html(d)}</span>' if d
                    else '<span class="script-desc empty-state">no description</span>')
        return f'<li data-node-key="{esc_html(n["node_key"])}">{label} {badge}{desc}</li>'

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


# S2.M5 [DECISION]: the sparkline display window is the LAST N points of a series
# (`build_trend_model` already loads every sidecar in `--out-dir`; this module adds
# NO new I/O, it only slices the existing model). Encoded as a named constant so the
# test suite can pin it (SPEC_4 §3).
SPARKLINE_WINDOW = 10
# Sparklines appear once a series has at least this many dated sidecars — below the
# gate the existing Metric×dates table/empty-state renders alone (SPEC_4 §3).
SPARKLINE_MIN_POINTS = 3
_SPARKLINE_W = 120.0
_SPARKLINE_H = 24.0
_SPARKLINE_PAD = 2.0


def _coerce_floats(values: list[Any]) -> list[float] | None:
    """Coerce a headline series window to floats for sparkline geometry. Values are
    nominally ints from the collector, but a hostile/corrupt sidecar can put a
    non-numeric string there — returns None (never raises) if ANY value in the
    window isn't numeric, so the caller can degrade THAT series' sparkline instead
    of crashing (SPEC_4 §3 escaping/robustness discipline). The existing table cell
    still renders the raw value via `esc_html` regardless of this result."""
    out: list[float] = []
    for value in values:
        coerced = finite_number(value)
        if coerced is None:
            return None       # Control 2: covers a string (existing), a non-finite
        out.append(coerced)   # (A19a) and an int too large for float() (Codex #3).
    return out


def _sparkline_svg(dom_id: str, floats: list[float]) -> str:
    """One inline `<svg><polyline>` sparkline (no external assets, §9-R). `floats`
    is already-coerced, already-windowed (SPARKLINE_WINDOW) geometry data — every
    coordinate goes through `_fmt_float` (§4.6), never raw `str()`/f-string floats."""
    lo, hi = min(floats), max(floats)
    span = (hi - lo) or 1.0  # flat series: avoid /0. NOTE the line renders at the BOTTOM
                             # (y = H - PAD = 22.0), not mid-height -- (f - lo) is 0 for
                             # every point, so the normalized term is 1.0. Verified.
    n = len(floats)
    step = _SPARKLINE_W / (n - 1) if n > 1 else 0.0
    points = []
    for i, f in enumerate(floats):
        x = i * step if n > 1 else _SPARKLINE_W / 2
        y = _SPARKLINE_PAD + (1.0 - (f - lo) / span) * (_SPARKLINE_H - 2 * _SPARKLINE_PAD)
        points.append(f"{_fmt_float(x)},{_fmt_float(y)}")
    poly = " ".join(points)
    return (
        f'<svg class="sparkline" id="{esc_html(dom_id)}" '
        f'viewBox="0 0 {_fmt_float(_SPARKLINE_W)} {_fmt_float(_SPARKLINE_H)}" '
        f'width="120" height="24" role="img" aria-labelledby="{esc_html(dom_id)}-title">'
        f'<title id="{esc_html(dom_id)}-title">trend sparkline</title>'
        f'<polyline points="{esc_html(poly)}" fill="none" stroke="currentColor" '
        f'stroke-width="1.5"/></svg>'
    )


def _sparkline_cell(series: dict[str, Any]) -> str:
    """Sparkline + min/max/current for one headline series, windowed to the last
    `SPARKLINE_WINDOW` points. Returns "" (no crash, no sparkline) when the window
    contains a non-numeric value — the existing per-date table cells still show the
    raw (escaped) value regardless.

    Pre-flight exit gate: the window is taken from `points` (measured only) and the
    `SPARKLINE_MIN_POINTS` gate is applied HERE, per series. Sidecar count is no longer a
    proxy for measurement count — with the gate left upstream, a series with two real
    points among three sidecars would still be drawn, smuggling the dropped point back in
    as geometry."""
    window = _series_points(series)[-SPARKLINE_WINDOW:]
    if len(window) < SPARKLINE_MIN_POINTS:
        return ""
    floats = _coerce_floats(window)
    if floats is None or not floats:
        return ""
    svg = _sparkline_svg(f"spark-{series['key']}", floats)
    stats = (
        '<span class="sparkline-stats">'
        f'min {esc_html(_fmt_float(min(floats)))} · '
        f'max {esc_html(_fmt_float(max(floats)))} · '
        f'cur {esc_html(_fmt_float(floats[-1]))}</span>'
    )
    return svg + stats


def _render_trend_body(model):
    if model["first_run"]:
        body = '<p class="empty-state">first run — no baseline</p>'
    else:
        # ≥3 MEASURED points on at least one series: prepend a sparkline column ahead of
        # the unchanged per-date columns (SPEC_4 §3) — below the gate the table renders
        # exactly as before this milestone. Per-series, not per-sidecar (pre-flight exit
        # gate): a series can now hold fewer points than there are dates, and
        # `_sparkline_cell` re-applies the same floor to each cell, so a column only
        # appears when something can actually fill it.
        show_sparklines = any(len(_series_points(s)) >= SPARKLINE_MIN_POINTS
                              for s in model["series"])
        rows = "".join(
            f'<tr><td>{esc_html(s["label"])}</td>'
            + (f'<td>{_sparkline_cell(s)}</td>' if show_sparklines else '')
            + "".join(f'<td>{esc_html(TREND_NOT_MEASURED_TEXT if v is None else v)}</td>'
                      for v in s["values"]) + '</tr>'
            for s in model["series"])
        spark_header = '<th>Trend</th>' if show_sparklines else ''
        header = "".join(f'<th>{esc_html(d)}</th>' for d in model["dates"])
        body = (f'<div class="overflow-x"><table><tr><th>Metric</th>{spark_header}'
                f'{header}</tr>{rows}</table></div>')
    return f'<div class="card"><h2>Trend (8 headline metrics)</h2>{body}</div>'


def _render_dupweb_body(model, raw_pairs):
    """A6 duplication presentation (finding #5b): one row per pair as `{a} ⇄ {b}` …
    `{pct}% shared` — the arrow between the two node keys and a percent, never the
    old separate node-key columns + a raw decimal score.

    B3/D6: each row also gets an action-launcher brief button. `raw_pairs` is the
    UNPREFIXED `doc["duplication"]["pairs"]` list (`build_dupweb_model` builds
    `model["edges"]` from this exact list, in the same order, only prefixing `a`/`b`
    into node-keys for the arrow display) — `build_consolidation_brief` needs the
    real paths, not the display node-keys, so the brief is index-aligned to it.

    F1 (T13 QA): each row also carries a `tier-node tier-{tier}` wrapper class (same
    convention `_render_length_flags_body` already uses for the length-flags table) so
    the tier filter toggle actually dims dup-pair rows. A row's tier is "project" if
    EITHER endpoint is project-tier, else "operator" — a cross-tier pair is a PROJECT
    signal (the project introduced content duplicating the operator's), so tagging it
    dim-with-operator would hide the exact case M4 exists to surface. The pair text
    shows the RAW `a_path`/`b_path` (a human-readable repo-relative path), never the
    internal `dup:<path>`/`always_loaded:...` node-key string `e["a"]`/`e["b"]` carry;
    a project-tier endpoint also gets the standard `.badge.tier-project` marker (color
    is never the only signal, matching this file's existing convention). On a
    non-compose/pre-tier sidecar every edge normalizes to `a_tier=b_tier="operator"`
    (C15), so every row renders `tier-node tier-operator` with no badge — the SAME
    "operator" class every treemap/ladder cell and length-flags row already carries
    unconditionally, not new markup this table alone introduces."""
    if model["edges"]:
        def _row_tier(e):
            return "project" if "project" in (e["a_tier"], e["b_tier"]) else "operator"

        def _endpoint(path, tier):
            badge = ' <span class="badge tier-project">project</span>' if tier == "project" else ""
            return f'{esc_html(path)}{badge}'

        rows = "".join(
            f'<tr class="tier-node tier-{_row_tier(e)}">'
            f'<td>{_endpoint(e["a_path"], e["a_tier"])} ⇄ {_endpoint(e["b_path"], e["b_tier"])}</td>'
            f'<td class="tabular-nums">{_fmt_float(e["score"] * 100)}% shared</td>'
            f'<td>{esc_html(e["shared_sample"])}</td>'
            f'<td>{_render_brief_control("dup", i, build_consolidation_brief(raw_pairs[i]))}</td>'
            '</tr>'
            for i, e in enumerate(model["edges"]))
        dup_body = f'<div class="overflow-x"><table><tr><th>Pair</th><th>Overlap</th><th>Sample</th><th>Action</th></tr>{rows}</table></div>'
    else:
        dup_body = '<p class="empty-state">no duplicate pairs above threshold</p>'
    if model["phantom_refs"]:
        all_rows = model["phantom_refs"]
        index_of = {id(r): i for i, r in enumerate(all_rows)}
        prows = ""
        for key, header, group_rows in build_phantom_groups(all_rows):
            if not group_rows:
                continue
            # The GROUP carrier is the <tbody>, never the <tr>. An existing test regex
            # (test_render_html.py:3173) matches a BARE `<tr><td>` on a row that groups
            # as `unverifiable`; adding a class to the data-row <tr> breaks it, and
            # binding rule 7 forbids editing that assertion. <tbody> reaches the same
            # cells for styling and leaves every <tr> byte untouched.
            prows += f'<tbody class="phantom-group phantom-group-{key}">'
            prows += (f'<tr class="phantom-group-header">'
                      f'<th colspan="6">{esc_html(header)} ({len(group_rows)})</th></tr>')
            for r in group_rows:
                i = index_of[id(r)]
                prows += (
                    f'<tr>'
                    f'<td>{esc_html(r.get("source",""))}</td><td>{esc_html(r.get("ref",""))}</td>'
                    f'<td>{esc_html(r.get("kind",""))}</td>'
                    f'<td>{esc_html(_phantom_status_word(r.get("kind",""), r.get("resolved")))}</td>'
                    f'<td>{esc_html(_phantom_guidance(r.get("kind",""), r.get("resolved", False)))}</td>'
                    f'<td>{_render_brief_control("phantom", i, build_phantom_ref_brief(r))}</td></tr>')
            prows += '</tbody>'
        phantom_body = (
            '<p class="digest">A phantom ref is a pointer in an instruction file to a '
            'target that doesn’t resolve — a dangling link. Rows marked "unverifiable" '
            'could not be checked — their target space extends outside the scanned '
            'root — and are listed for review, not asserted broken.</p>'
            '<div class="overflow-x"><table><tr><th>Source</th><th>Ref</th><th>Kind</th>'
            f'<th>Resolved</th><th>What to do</th><th>Action</th></tr>{prows}</table></div>')
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
    output stays deterministic regardless of the flag list's original order.

    B3/D6: each row also gets an action-launcher refactor-brief button, indexed by
    its position in this same sorted order (the row's own deterministic identity).

    T6: each row also carries a `tier-node tier-{operator|project}` class (same
    wrapper vocabulary as the treemap/ladder `<g>`, C15 back-compat default when a
    flag entry carries no `tier` key) — a project-tier row also gets a visible
    "project" badge (never color alone, matching this file's existing convention)."""
    flags = doc.get("instruction_length_flags", []) or []
    if flags:
        sorted_flags = sorted(flags, key=lambda f: (-f.get("lines", 0), f["path"]))

        def _row(i, f):
            pill = ('<span class="pill pill-critical">critical</span>'
                    if f.get("lines", 0) > LENGTH_CRITICAL_LINES else '<span class="pill">over</span>')
            tier = _normalize_tier(f.get("tier"))
            tier_badge = (' <span class="badge tier-project">project</span>'
                          if tier == "project" else "")
            return (f'<tr class="tier-node tier-{tier}">'
                    f'<td>{esc_html(f["path"])}{tier_badge}</td>'
                    f'<td class="tabular-nums">{esc_html(f["lines"])}</td>'
                    f'<td>{pill}</td>'
                    f'<td>{_render_brief_control("overcap", i, build_refactor_brief(f))}</td>'
                    '</tr>')

        rows = "".join(_row(i, f) for i, f in enumerate(sorted_flags))
        body = f'<div class="overflow-x"><table><tr><th>Path</th><th>Lines</th><th>Flag</th><th>Action</th></tr>{rows}</table></div>'
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


def _render_composed_mcp_body(mcp_servers):
    """T7b: `composed_settings.mcp` (T5's `collect_composed_mcp`/`_redact_mcp_server`) —
    one row per registered server: source-tier badge, enabled/disabled state, and
    env/header-key NAMES only. T5 already redacts every raw `env`/`headers` VALUE before
    it ever reaches the sidecar — this function only ever reads `env_keys`/`header_keys`
    (never a hypothetical raw `env`/`headers` field on `s`), so a value can't leak here
    even if one were mistakenly present on the dict."""
    if not mcp_servers:
        return ('<div class="card"><h2>MCP servers (composed)</h2>'
                '<p class="empty-state">none registered</p></div>')

    def _row(s):
        tier = _normalize_settings_tier(s.get("tier"))
        enabled = bool(s.get("enabled"))
        state_badge = (f'<span class="badge {"mcp-enabled" if enabled else "mcp-disabled"}">'
                        f'{"enabled" if enabled else "disabled"}</span>')
        key_parts = []
        if s.get("env_keys"):
            key_parts.append("env: " + ", ".join(esc_html(k) for k in s["env_keys"]))
        if s.get("header_keys"):
            key_parts.append("headers: " + ", ".join(esc_html(k) for k in s["header_keys"]))
        keys_html = (f'<span class="script-desc">{"; ".join(key_parts)}</span>' if key_parts
                     else '<span class="script-desc empty-state">no env/header keys</span>')
        return (f'<li><span class="badge tier-src-{tier}">{esc_html(tier)}</span> '
                f'{esc_html(s.get("name", ""))} {state_badge}{keys_html}</li>')

    rows = "".join(_row(s) for s in sorted(mcp_servers, key=lambda s: s.get("name") or ""))
    return f'<div class="card"><h2>MCP servers (composed)</h2><ul>{rows}</ul></div>'


def _render_composed_hooks_body(hooks):
    """T7b: `composed_settings.hooks` (T5's `_compose_hooks`) — the tier-tagged UNION
    across user/project/local (every matching hook fires, unlike the precedence-winner
    settings/MCP merges). Renders ALONGSIDE the pre-existing operator-only "Registered
    hooks (settings.json)" card in `_render_bipartite_body` (untouched — still the
    wiring-integrity/orphan-detection view fed by operator settings only); this card is
    compose mode's cross-tier view, carrying each hook's source file."""
    if not hooks:
        return ('<div class="card"><h2>Hooks (composed, all tiers)</h2>'
                '<p class="empty-state">none registered across any tier</p></div>')

    def _row(h):
        tier = _normalize_settings_tier(h.get("tier"))
        matcher = f' ({esc_html(h.get("matcher"))})' if h.get("matcher") else ""
        exists = h.get("exists")
        exists_note = ("script found" if exists is True
                        else "script missing" if exists is False
                        else "existence unknown (out-of-root or non-script command)")
        return (f'<li><span class="badge tier-src-{tier}">{esc_html(tier)}</span> '
                f'{esc_html(h.get("event", ""))}{matcher} '
                f'<span class="script-desc">{esc_html(h.get("command", ""))} — {exists_note} — '
                f'source: {esc_html(h.get("source_file") or "unknown")}</span></li>')

    rows = "".join(_row(h) for h in hooks)
    return f'<div class="card"><h2>Hooks (composed, all tiers)</h2><ul>{rows}</ul></div>'


def _render_composed_permissions_body(perms):
    """T7b: `composed_settings.permissions` (T5's `_merge_permissions_union_deny_wins`)
    — allow/deny/ask counts unioned across every tier, deny always wins a same-rule
    conflict."""
    return (
        '<div class="card"><h2>Permissions (composed, union)</h2>'
        f'<p class="digest">allow {esc_html(perms.get("allow_count", 0))} · '
        f'deny {esc_html(perms.get("deny_count", 0))} · '
        f'ask {esc_html(perms.get("ask_count", 0))} '
        f'({esc_html(perms.get("evidence", ""))}) — a rule denied by any tier is '
        'denied everywhere</p></div>'
    )


_SETTINGS_OVERRIDE_SCALAR_TYPES = (str, int, float, bool, type(None))


def _render_composed_overrides_body(overrides):
    """T7b: `composed_settings.overrides` (T5's `_settings_overrides`) — the ALLOWLISTED
    non-permission settings keys (`model`/`cleanupPeriodDays`/`sandbox`/`enabledPlugins`,
    plus `env` key-names-only) that differ across tiers, winner + overridden tiers.

    P1-C (renderer-side defense): collector.py's `_settings_override_safe_value`
    allowlists/type-gates `winning_value` before it reaches the sidecar — but this
    renderer does NOT trust that upstream invariant blindly. A hand-crafted/malformed
    sidecar could still carry a raw non-scalar in `winning_value` with no `value_kind`
    marker at all, so `_fmt_winning_value` independently re-checks the runtime TYPE:
    a scalar (str/int/float/bool/None) or a list OF scalars (the `env` override's list
    of key NAMES, the one legitimate list shape the collector emits) renders normally;
    anything else — a dict, a list containing a non-scalar, or an explicit
    `value_kind` of `"complex"`/`"redacted"` — renders a fixed placeholder, NEVER the
    raw value."""
    if not overrides:
        return ('<div class="card"><h2>Settings overrides (composed)</h2>'
                '<p class="empty-state">no cross-tier setting overrides</p></div>')

    def _fmt_winning_value(o):
        value_kind = o.get("value_kind")
        if value_kind == "complex":
            return "(complex value hidden)"
        if value_kind == "redacted":
            return "(redacted)"
        v = o.get("winning_value")
        if isinstance(v, list):
            if all(isinstance(x, _SETTINGS_OVERRIDE_SCALAR_TYPES) for x in v):
                return ", ".join(esc_html(x) for x in v)
            return "(complex value hidden)"
        if isinstance(v, _SETTINGS_OVERRIDE_SCALAR_TYPES):
            # P3 defect fix: genuine JSON null (v is None, no value_kind marker)
            # should render as "null" (JSON repr) not "None" (Python repr)
            if v is None:
                return "null"
            return esc_html(v)
        return "(complex value hidden)"

    def _row(o):
        tier = _normalize_settings_tier(o.get("winning_tier"))
        overridden = ", ".join(esc_html(_normalize_settings_tier(t))
                               for t in (o.get("overridden_tiers") or []))
        return (f'<li><span class="badge tier-src-{tier}">{esc_html(tier)}</span> '
                f'{esc_html(o.get("key", ""))} = {_fmt_winning_value(o)} '
                f'<span class="script-desc">overrides: {overridden or "none"}</span></li>')

    rows = "".join(_row(o) for o in overrides)
    return f'<div class="card"><h2>Settings overrides (composed)</h2><ul>{rows}</ul></div>'


def _render_composed_settings_body(doc):
    """T7b (dark-feature closure): `doc["composed_settings"]` (T5) was fully collected —
    MCP registrations, the tier-tagged hooks union, the permissions union, and settings
    overrides — but rendered NOWHERE; compose mode's whole point is operator visibility
    into what a project's `.claude/` layer adds on top of the operator's own `~/.claude/`,
    and "collected but never shown" defeats that (dark feature). Gated on
    `composed_settings` presence, same pattern as T6's `_render_tier_summary_band` gating
    on `tier_composition` — a non-compose or old-shape sidecar carries no key at all, so
    this returns "" and the Hygiene view's markup is unaffected. Uses the 3-way
    `user|project|local` SETTINGS-tier vocabulary (T5, `_normalize_settings_tier`) —
    distinct from `_normalize_tier`'s binary `operator|project` NODE tier used everywhere
    else in this view."""
    cs = doc.get("composed_settings")
    if not cs:
        return ""
    return (
        '<h2>Composed settings (compose mode)</h2>'
        f'{_render_composed_mcp_body(cs.get("mcp") or [])}'
        f'{_render_composed_hooks_body(cs.get("hooks") or [])}'
        f'{_render_composed_permissions_body(cs.get("permissions") or {})}'
        f'{_render_composed_overrides_body(cs.get("overrides") or [])}'
    )


def _render_hygiene_view(doc, models, copy_payload):
    """Composes the former bipartite/trend/dupweb tab bodies plus length flags
    (finding #5b) and the unchecked-binary count (finding #5a) under ONE view
    (RESOLVED DECISION 2 — hook wiring folded here as 'Wiring integrity', never
    dropped). `_render_composed_settings_body` (T7b) appends last, compose-mode-only —
    absent entirely (returns "") on any non-compose or old-shape `doc`."""
    return (
        '<section id="view-hygiene" class="view" role="tabpanel" '
        'aria-labelledby="view-btn-hygiene" tabindex="-1">'
        f'<div class="view-toolbar">{_render_copy_controls("hygiene", copy_payload)}</div>'
        f'{_render_length_flags_body(doc)}'
        f'{_render_dupweb_body(models["dupweb"], (doc.get("duplication") or {}).get("pairs", []) or [])}'
        f'{_render_unchecked_binaries_body(doc)}'
        f'{_render_trend_body(models["trend"])}'
        '<h2>Wiring integrity</h2>'
        f'{_render_bipartite_body(models["bipartite"])}'
        f'{_render_composed_settings_body(doc)}'
        '</section>'
    )


def _render_out_of_root_refs_body(doc):
    """F3 (T13 QA, DARK FEATURE): `out_of_root_refs` (T3's H2 audit trail — every
    project-tier path a symlink/traversal escaped `project-containment-root` into, so
    it was noted but NOT read/traversed/excerpted) is UNTRUSTED input — `name` +
    `target` (a raw `readlink()` string), `trusted: False` — collected but rendered
    NOWHERE (confirmed zero matches). Reuses the exact `esc_html`'d `<ul>` card
    pattern the adjacent Blind spots card already uses. Compose-mode only, C15: a
    non-compose or old-shape doc carries no `out_of_root_refs` key at all (`build_document`
    only sets it inside `if compose:`), so this returns "" and a non-compose render's
    markup is byte-identical to before this card existed."""
    refs = doc.get("out_of_root_refs")
    if refs is None:
        return ""
    if refs:
        items = "".join(
            f'<li>{esc_html(r.get("name", ""))} → {esc_html(r.get("target", ""))} '
            '<span class="badge tier-dark">untrusted</span></li>' for r in refs)
        body = f"<ul>{items}</ul>"
    else:
        body = '<p class="empty-state">none</p>'
    return f'<div class="card"><h2>Out-of-root refs ({len(refs)})</h2>{body}</div>'


def _render_transparency_note(doc):
    """F3 (T13 QA, DARK FEATURE): the composed weight-exclusion count (P31/C18 weight
    honesty) and the roots actually walked (R2-B `inspected_roots`) were collected but
    never surfaced anywhere in the HTML. Absent `inspected_roots` (non-compose or
    old-shape sidecar, C15) returns "" so a non-compose render's markup is unaffected.
    `excluded_count`'s ABSENCE (vs. `0`) distinguishes "not measured" from "0 excluded,
    measured" — both states are rendered explicitly, never silently folded into a bare
    "0" that could mean either."""
    inspected = doc.get("inspected_roots")
    if not inspected:
        return ""
    root_labels = (("operator", "operator"), ("project_containment", "project"),
                   ("project_harness", "project harness (.claude/)"))
    roots_walked = [label for key, label in root_labels if inspected.get(key)]
    roots_text = ("roots walked: " + ", ".join(esc_html(r) for r in roots_walked)) \
        if roots_walked else "roots walked: none"
    excluded = ((doc.get("always_loaded", {}) or {}).get("totals", {}) or {}).get("excluded_count")
    excluded_text = (f"{esc_html(excluded)} file(s) excluded from weight (out-of-root/inaccessible)"
                      if excluded is not None else "weight-exclusion count not measured")
    return f'<p class="digest" id="weight-transparency-note">{excluded_text} · {roots_text}</p>'


def _render_provenance_footer(doc, skipped, footer, date):
    """Former `_render_notes_tab`, relocated to a `<footer>` (never `<main>`) — the
    root/date/generated_at + data-sources + warning-count lines stay always-visible;
    the rest collapses behind `<details>`. `_render_out_of_root_refs_body` (F3) appends
    last inside the detail — compose-mode-only, "" (no markup change) otherwise."""
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
        f'{_render_out_of_root_refs_body(doc)}'
        '</details></footer>'
    )


def _stream_event_count(f, codex_aggregate):
    """A6 headline count for one stream card — the SAME figure `_friction_sentence`
    already leads with, so the card count and the sentence never disagree. Pure
    function of the footer dict `f` (from `build_friction_overlay`) + `codex_aggregate`;
    reuses counters the join functions already computed, never re-derives.

    Deliberate asymmetry, not an oversight: each stream's card shows that stream's
    OWN headline figure, and the headline differs by stream. `decisions`/`metrics`
    report their ATTRIBUTED count (`segments_joined` / eligible-minus-aggregate-only)
    because that is what those streams lead with. `interventions` instead reports its
    PARSED count (`records_parsed`) because that is what `_friction_sentence` leads
    with there, and it is what an operator checking "did my log get read" wants first
    — it includes unmatched/ambiguous/future-skipped records that contribute ZERO to
    `friction_total`, and the sentence below the card discloses the full chain down
    to the contributed count. Consequence: the four card numerals do NOT sum to
    `friction_total`. This is by design, not a bug to fix by switching interventions
    to `segments_joined` — see `test_stream_card_numeral_and_sentence_agree`, which
    pins the card and the sentence to the SAME lead figure per stream."""
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
    """A6 stream card: event count, title, plain-English description, source filename.

    `_stream_event_count` keeps returning an int — its contract is unchanged; the lower
    bound is applied HERE, at the display site."""
    count = _lb(_stream_event_count(f, codex_aggregate), _stream_truncated(f))
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


def _render_component_friction_table(joined, *, truncated=False):
    """A6 per-component join table (finding #1): which map nodes got heated, and how
    many friction records each. Reads the SAME `joined` dict `build_friction_overlay`
    already returns — does not re-derive. `sorted(joined.items())` (node_key ascending)
    for deterministic, insertion-order-independent output.

    Sortable table (item 4): server order (`sorted(joined.items())`, node_key ascending)
    is the deterministic INITIAL DOM state (`aria-sort="none"` on load — never
    pre-toggled); the client sort reorders only the live DOM, never the emitted bytes.
    Each row carries `data-node-key` so the treemap click-to-jump (item 6) can
    highlight it.

    `truncated` (keyword-only, default False so any direct caller is unchanged): `joined`
    merges records from all four streams and carries no stream tag, so once ANY stream's
    read stopped at a cap every per-component count here is a lower bound."""
    if not joined:
        rows = ('<tr class="friction-component-row"><td colspan="2" class="empty-state">'
                 'no components joined</td></tr>')
    else:
        rows = "".join(
            f'<tr class="friction-component-row" data-node-key="{esc_html(node_key)}">'
            f'<td>{esc_html(node_key)}</td>'
            f'<td>{esc_html(_lb(len(records), truncated))}</td></tr>'
            for node_key, records in sorted(joined.items())
        )
    return (
        '<div class="overflow-x"><table class="friction-components sortable">'
        '<thead><tr>'
        '<th aria-sort="none"><button class="th-sort" data-sort-col="0" data-sort-type="text">'
        'Component <span class="sort-ind" aria-hidden="true">↕</span></button></th>'
        '<th aria-sort="none"><button class="th-sort" data-sort-col="1" data-sort-type="num">'
        'Friction records <span class="sort-ind" aria-hidden="true">↕</span></button></th>'
        '</tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def _render_friction_row(f, codex_aggregate):
    """The per-stream provenance row, whose <details> dumps EVERY footer counter as bare
    ints into visible (expandable) UI. `truncated_at_cap` discloses THAT the read stopped,
    but the neighbouring `records_parsed`/`lines_nonblank`/`segments_joined` ints still read
    as exact — so a lead-in says in words what `_lb` says in a glyph.

    A lead-in rather than `_lb` per value: this is a `json.dumps` of a whole dict, so `_lb`
    per value would render "\\u2265123" (json.dumps defaults to ensure_ascii=True) and would
    silently change every count-valued key from int to str inside a machine-readable dump.
    The lead-in changes no value's type and emits ZERO bytes when nothing truncated."""
    sentence = esc_html(_friction_sentence(f, codex_aggregate))
    raw = {k: v for k, v in f.items() if k not in ("stream", "status", "path_display")}
    lead = ('<p class="friction-row-lb">Read truncated at the '
            f'{esc_html(f.get("truncated_at_cap", ""))} cap — every count below is a '
            'LOWER BOUND.</p>') if _stream_truncated(f) else ""
    raw_html = (f'<details class="friction-row-detail"><summary>raw counters</summary>'
                f'{lead}{esc_html(json.dumps(raw, sort_keys=True))}</details>') if raw else ""
    return (f'<tr><td>{esc_html(f["stream"])}</td><td>{esc_html(f["status"])}</td>'
            f'<td>{esc_html(f["path_display"])}</td>'
            f'<td>{sentence}{raw_html}</td></tr>')


def _render_friction_panel(joined, footer, codex_aggregate, friction_total_value):
    explainer = (
        '<p class="friction-explainer">Friction = where your harness has seen the most churn. '
        'These local telemetry streams (decisions, review metrics, Codex reviews, interventions) '
        'are matched by name onto the components on the map — a data join, not a judgment.</p>'
    )
    joined_truncated = _joined_streams_truncated(footer)
    stream_cards = "".join(_render_stream_card(f, codex_aggregate) for f in footer)
    component_table = _render_component_friction_table(joined, truncated=joined_truncated)
    rows = "".join(_render_friction_row(f, codex_aggregate) for f in footer)
    # Finding #2 (post-exec Codex): the codex aggregate is scoped to the codex STREAM's own
    # cap, not the joined-stream bound above — see `_codex_stream_truncated`.
    codex_sentence = _codex_sentence(codex_aggregate, truncated=_codex_stream_truncated(footer))
    codex_html = (
        f'<div class="card"><h2>Codex aggregate (not node-joined)</h2>'
        f'<p>{esc_html(codex_sentence)}</p></div>'
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


def _render_friction_view(joined, footer, codex_aggregate, drag, friction_total_value, date, copy_payload):
    """Former `_render_friction_panel`, moved verbatim, plus the drag-candidate table
    half of the former `_render_civc_drag_tab` appended (IA mapping). A6/DECISION 6
    (Task 8): 4 stream cards + a per-component join table now render above the
    explainer, and the header reads `friction_total` — the SAME value the AM-1
    instrument gauge renders — instead of the raw joined-record count."""
    friction_body = _render_friction_panel(joined, footer, codex_aggregate, friction_total_value)
    if not drag["available"]:
        drag_body = (f'<p class="empty-state">drag-candidate table unavailable — no '
                     f'<code>harness-synthesis-{esc_html(date)}.json</code> in this report directory. '
                     f'Re-run <code>/harness-map</code> Step B to generate it.</p>')
    elif not drag["rows"]:
        drag_body = '<p class="empty-state">no drag candidates</p>'
    else:
        # Sortable table (item 4, T1's JS via `class="sortable"`); `i` = row index in
        # `drag["rows"]` — drives the per-row `kind="drag"` brief island id. Rows carry
        # NO `data-node-key`: Task 6's click-to-jump targets only `tr.friction-component-row`,
        # and drag rows are neither map components nor telemetry-joined.
        rows = "".join(
            f'<tr>'
            f'<td>{esc_html(r.get("n",""))}</td><td>{esc_html(r.get("surface",""))}</td>'
            f'<td>{esc_html(_drag_outcome_label(r.get("outcome","")))}</td>'
            f'<td>{_drag_fields_details(r)}'
            f'{_render_brief_control("drag", i, build_dragcandidate_brief(r))}</td></tr>'
            for i, r in enumerate(drag["rows"]))
        drag_body = (
            f'<p class="digest">{esc_html(_DRAG_DEFINITION)}</p>'
            '<div class="overflow-x"><table class="sortable"><thead><tr>'
            '<th aria-sort="none"><button class="th-sort" data-sort-col="0" data-sort-type="num">#'
            ' <span class="sort-ind" aria-hidden="true">↕</span></button></th>'
            '<th aria-sort="none"><button class="th-sort" data-sort-col="1" data-sort-type="text">'
            'Surface <span class="sort-ind" aria-hidden="true">↕</span></button></th>'
            '<th aria-sort="none"><button class="th-sort" data-sort-col="2" data-sort-type="text">'
            'Recommendation <span class="sort-ind" aria-hidden="true">↕</span></button></th>'
            f'<th>Detail</th></tr></thead><tbody>{rows}</tbody></table></div>')
    return (
        '<section id="view-friction" class="view" role="tabpanel" '
        'aria-labelledby="view-btn-friction" tabindex="-1">'
        f'<div class="view-toolbar">{_render_copy_controls("friction", copy_payload)}</div>'
        f'{friction_body}'
        f'<div class="card"><h2>Drag candidates</h2>{drag_body}</div>'
        '</section>'
    )


VIEWS = (("view-overview", "Overview"),
         ("view-weight", "Weight"), ("view-friction", "Friction"), ("view-hygiene", "Hygiene"))


def render_html(
    date: str,
    models: dict[str, Any],
    friction: tuple[Any, ...],
    notes: dict[str, Any],
    generation: int | None = None,
) -> str:
    """Assembles the final HTML document — a fixed named-section sequence (§4.8),
    never set/dict-driven order. 4-view IA (A1, Task B-t2 merged the former separate
    Overview/Coverage tabs into one): all views render WITHOUT `hidden` server-side
    (progressive enhancement) — the static script collapses to Overview on load."""
    doc = notes["doc"]
    skipped = notes["skipped"]
    headline = doc.get("headline", {}) or {}
    heat, joined, footer, codex_aggregate = friction
    # every footer entry is "disabled" iff `--no-friction` was passed (build_friction_overlay's
    # `disabled` short-circuit sets ALL 4 streams to "disabled"; an enabled-but-absent stream
    # is "absent"/"inaccessible", never "disabled") — the single derivation the Weight view's
    # toggle gate (Codex P3) reads from.
    friction_enabled = any(f["status"] != "disabled" for f in footer)
    # Codex R2-F6 + R3-2: TWO values. The displayed count stays the TOTAL -- every row,
    # verifiable or not, is a row the operator should see counted. Only the BAND (the
    # CLEAN/BROKEN verdict) keys off the CONFIRMED count: resolved=null rows are
    # unverifiable, and painting them BROKEN would be rule 6's forbidden verdict, cast
    # by the renderer instead of the collector. Derived in `_phantom_counts` so the
    # Overview digest below bands off the SAME pair (QA exit gate, HIGH 1).
    phantom_ref_count, phantom_confirmed_count = _phantom_counts(doc)
    # ONE display conversion, shared by every band consumer. `friction_total()` still
    # returns an int and `_friction_contributions` still reconciles against it exactly; the
    # `≥N` string exists only downstream of here, where it also suppresses the band.
    friction_total_value = friction_total(joined, codex_aggregate, _metrics_aggregate_only(footer))
    friction_total_shown = _friction_total_display(friction_total_value, footer)
    overview_model = build_overview_model(models, headline, phantom_ref_count, friction_total_shown,
                                          phantom_confirmed_count=phantom_confirmed_count)

    warn_count = len(doc.get("inaccessible", []) or []) + len(doc.get("errors", []) or [])
    warn_badge = (f'<a class="warn-badge" href="#provenance" data-target="provenance">'
                  f'{warn_count} warning(s)</a>') if warn_count else ""

    style_hash = _csp_hash(STATIC_STYLE)
    script_hash = _csp_hash(STATIC_SCRIPT)
    csp = (f'<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
           f"style-src 'sha256-{style_hash}'; script-src 'sha256-{script_hash}'; "
           f"connect-src 'self'; base-uri 'none'; form-action 'none'\">")

    # Live-serve build generation (serve.py's monotonic publish counter, FIX 4). A CSP-safe
    # <meta> the static script reads via DOM so a reconnecting client can compare it to the
    # server's current generation and catch up a refresh missed during a disconnect. `None`
    # (the one-shot report / file:// path) emits NO meta, so that output stays deterministic.
    gen_meta = (f'<meta name="hm-generation" content="{int(generation)}">'
                if generation is not None else "")

    # Tier filter toggle (T7): rendered ONLY when the sidecar carries `tier_composition`
    # (T6's own gate for tier UI, `test_tier_summary_band_absent_without_tier_composition`)
    # -- a non-compose (operator-only) render has nothing to filter (`_normalize_tier`
    # defaults every node to "operator"), so the control is absent and the controls bar
    # stays byte-identical to pre-T7 (C15). Three-state radiogroup, single tabstop
    # ("All" checked by default) -- roving tabindex wired in STATIC_SCRIPT.
    tier_filter_html = ""
    if doc.get("tier_composition"):
        tier_filter_html = (
            '<div class="tier-filter" role="radiogroup" aria-label="Tier filter">'
            '<button class="tier-filter-btn" role="radio" aria-checked="true" '
            'tabindex="0" data-tier-filter="all">All</button>'
            '<button class="tier-filter-btn" role="radio" aria-checked="false" '
            'tabindex="-1" data-tier-filter="operator-only">Operator only</button>'
            '<button class="tier-filter-btn" role="radio" aria-checked="false" '
            'tabindex="-1" data-tier-filter="project-only">Project only</button>'
            '</div>'
        )

    view_buttons = "".join(
        f'<button class="view-btn" id="view-btn-{vid.split("-", 1)[1]}" role="tab" '
        f'data-target="{vid}" aria-selected="false">{esc_html(label)}</button>'
        for vid, label in VIEWS)

    copy_payloads = build_copy_payloads(date, models, friction, doc)

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        csp,
        gen_meta,
        f'<title>harness-map {esc_html(date)}</title>',
        f"<style>{STATIC_STYLE}</style>",
        "</head><body>",
        "<header><h1>harness-map</h1>",
        f'<div class="subtitle">root: {esc_html(doc.get("root",""))} | date: {esc_html(date)} '
        f'| generated_at: {esc_html(doc.get("generated_at",""))} {warn_badge}</div></header>',
        _render_instrument_readout(headline, phantom_ref_count, phantom_confirmed_count,
                                   friction_total_shown, models["trend"], models, doc, joined,
                                   footer, codex_aggregate),
        '<div class="controls">',
        '<nav class="view-switch" role="tablist">',
        view_buttons,
        '</nav>',
        '<button class="action-btn" id="expand-all">Expand all / print view</button>',
        '<button class="action-btn" id="theme-toggle" type="button" '
        'aria-pressed="false" aria-label="Toggle color theme">◐</button>',
        tier_filter_html,
        "</div>",
        "<main>",
        _render_overview_view(overview_model, models["civc"], date, copy_payloads["overview"], doc),
        _render_weight_view(models["context_weight"], heat, friction_enabled, doc, copy_payloads["weight"]),
        _render_friction_view(joined, footer, codex_aggregate, models["drag"], friction_total_shown, date,
                              copy_payloads["friction"]),
        _render_hygiene_view(doc, models, copy_payloads["hygiene"]),
        "</main>",
        _render_copy_island("overview", copy_payloads["overview"]),
        _render_copy_island("weight", copy_payloads["weight"]),
        _render_copy_island("friction", copy_payloads["friction"]),
        _render_copy_island("hygiene", copy_payloads["hygiene"]),
        _render_provenance_footer(doc, skipped, footer, date),
        f"<script>{STATIC_SCRIPT}</script>",
        "</body></html>",
    ]
    return "".join(parts)


class RenderError(Exception):
    """Fatal render-pipeline condition (bad out-dir, no sidecar, schema mismatch).
    Carries the exact message main() prints to stderr; serve.py surfaces it too."""


@dataclasses.dataclass(frozen=True)
class RenderContext:
    date: str
    doc: dict
    models: dict
    node_index: dict
    friction: tuple          # (heat, joined, footer, codex_aggregate) — build_friction_overlay output
    streams: dict            # the exact streams dict used, so the cheap path re-reads the same paths
    friction_disabled: bool  # the retained --no-friction flag; MUST be threaded into every
                              # build_friction_overlay call (all-None streams + disabled=False renders
                              # "absent", disabled=True renders "disabled" — NOT interchangeable)
    skipped: list
    html_text: str
    html_bytes: bytes        # html_text.encode("utf-8", "backslashreplace") — the served bytes


def _project_slug(project_root: Path) -> str:
    """The CC per-project memory directory name: the absolute path with every "/" and "."
    replaced by "-". Mirrors collector.py's own slug rule. DERIVED from $HOME at call time,
    never a literal: a real slug is machine-specific, it would break the §9-R D hermeticity
    contract, and (PUBLICATION REQUIREMENT, operator directive 2026-07-31) it would ship the
    operator's username and home-directory layout into a PUBLIC repo. Note a hardcoded slug
    would NOT contain the absolute-path prefix that guard greps for -- it is dash-separated
    -- so it slips past a naive path-literal grep while leaking the same information; the
    guard test therefore checks for the running machine's own derived slug as well.
    `tests/test_release_decoupling.py` does not read this module, so that guard is a
    separate test (see test_no_absolute_home_literal_in_runtime_modules)."""
    return re.sub(r"[/.]", "-", os.path.abspath(str(project_root)))


def default_streams(root: Path | None = None) -> dict[str, Path | None]:
    """The default friction-telemetry stream paths: real ~/.claude JSONL paths, resolved
    through $HOME at CALL time (never frozen at import -- §9-R D hermeticity contract).
    Shared by main()'s CLI-override branch and serve.py's _build_streams so the two
    default-path sets can never drift apart.

    The interventions stream is the harness directory's own per-project memory log: the
    harness is edited AS a project, so its interventions land under
    projects/<slug-of-the-harness-root>/memory/.

    CONTAINMENT (binding, finding #3): `root` is the SELECTED scan root. The interventions
    default is non-None ONLY when the selected root IS the harness root, so a foreign-root
    run can never ingest this harness's interventions log. That is enforced by the
    comparison below, NOT asserted in prose -- the slug alone gives no containment at all,
    because it is derived from $HOME unconditionally. `root=None` means "the harness root",
    which is today's behaviour, so an unupdated caller is still treated as scanning the
    harness root.

    Also gated on the memory DIRECTORY existing (not the file): under a fake or foreign
    $HOME the directory is absent and the value stays None, matching pre-S6 behaviour and
    keeping test_default_streams_keys_and_paths green without an exemption. Where the
    directory exists the path is returned unconditionally, exactly like the other three
    streams, so serve.py's absent->present sweep can still see the file being CREATED.
    This directory gate is a deliberate ASYMMETRY with the other three streams: `_stream_status`
    already reports "absent" for a non-existent path, so the gate's real observable cost is
    (a) the footer shows "(not provided)" rather than the path, and (b) serve gains a
    directory-creation blind spot -- if projects/<slug>/memory/ does not exist at server
    start, interventions stays None for the whole process lifetime and no sweep recovers it."""
    claude = Path.home() / ".claude"
    selected = (Path(root) if root is not None else claude).resolve()
    mem_dir = claude / "projects" / _project_slug(claude) / "memory"
    return {
        "decisions": claude / "harness-decisions.jsonl",
        "metrics": claude / "harness-metrics.jsonl",
        "codex": claude / "harness-codex.jsonl",
        "interventions": (
            (mem_dir / "interventions.jsonl")
            if selected == claude.resolve() and mem_dir.is_dir()
            else None
        ),
    }


def _contain_default_interventions(streams, doc_root):
    """Re-apply §4.1's containment once the SELECTED ROOT is actually known.

    render_html.py has NO --root argument (its parser declares --out-dir, --date, the four
    --*-file flags and --no-friction), and main() builds `streams` BEFORE any sidecar is
    loaded -- so the CLI path cannot pass a root to default_streams(). The scanned root is
    recorded authoritatively in the sidecar, and this is the first point it is available.
    Adding a --root flag was rejected: it would be a SECOND source of truth for a value the
    sidecar already carries.

    Applies to the DEFAULT-derived path only, identified by equality with
    default_streams()'s own value. An explicit --interventions-file naming a different file
    is never dropped -- the operator asked for it by name. An explicit flag naming exactly
    the default path IS contained; that is the safe direction.

    FAILS CLOSED on an unknown root. The guarantee is "non-None ONLY when the selected root
    IS the harness root" -- so an absent or unreadable `doc["root"]` cannot establish it and
    must yield None. Returning the default unchanged there would let a missing field bypass
    the whole containment, which is the "add doubt, never remove it" asymmetry inverted. A
    non-string `doc["root"]` (an int, list, or dict from a malformed sidecar) is treated as
    absent for the same reason, rather than raising out of `Path(doc_root)`.

    Returns a NEW dict; never mutates the caller's."""
    configured = streams.get("interventions")
    if configured is None:
        return streams
    if Path(configured) != default_streams()["interventions"]:
        return streams                      # explicitly overridden -> untouched
    if (isinstance(doc_root, str) and doc_root
            and Path(doc_root).resolve() == (Path.home() / ".claude").resolve()):
        return streams                      # harness root -> the default stands
    return {**streams, "interventions": None}


# Control 3 (S2 gate fix, S8/S12): the RENDERER envelope. Binding rule 5 requires the
# COLLECTOR to emit valid JSON on any crash; there was no counterpart here.
#
# The concrete escape: a deeply-nested sidecar raises RecursionError, which subclasses
# RuntimeError (verified) and NOT json.JSONDecodeError -- so it escapes load_sidecar's
# except clause, escapes render_from_out_dir, and escapes main()'s `except RenderError`.
#
# The conversion lives HERE, not in main(), because serve.py:264 calls
# render_from_out_dir DIRECTLY. Converting to RenderError routes the fault into
# containment that ALREADY exists and is already tested: main()'s handler, serve.py's
# startup path, and serve.py's `except (CollectorError, RenderError, OSError)`
# keep-last-good handler. (serve.py's watcher backstop already covered the
# steady-state loop; startup was the exposed path.)
#
# Enumerated, never `except Exception` (code-style): these are the types an unexpected
# sidecar/synthesis SHAPE can produce, mirroring the list serve.py already names.
_RENDER_FALLBACK_ERRORS = (
    RecursionError, ValueError, TypeError, KeyError, AttributeError,
    IndexError, OverflowError, ZeroDivisionError,
)


def _render_fault_message(out_dir: Path, date: str | None, exc: BaseException) -> str:
    """The ONE stderr line for an unenumerated render fault, shared by the two sites that
    raise it (the selected sidecar's own load, and the top-level envelope) so their
    wording can never drift. Names the SIDECAR, not just the directory (Codex F2): with
    the degraded page dropped (F3, operator decision) this line is the only diagnostic the
    operator gets, so it must identify the unusable file by name -- GP#15. When --date is
    absent the filename was chosen by the sidecar scan and is not reconstructable here, so
    fall back to the directory rather than assert a name that may be wrong. Path()
    coercion mirrors the render body's own first line: a caller passing a str must not
    make THIS helper raise TypeError and re-expose the traceback it exists to suppress."""
    target = (Path(out_dir) / f"harness-map-{date}.json") if date else Path(out_dir)
    return f"fatal: could not render {target}: {type(exc).__name__}: {exc}"


def render_from_out_dir(
    out_dir: Path,
    date: str | None = None,
    streams: dict[str, Any] | None = None,
    no_friction: bool = False,
    generation: int | None = None,
) -> RenderContext:
    """Build the HTML IN MEMORY (D3) from the collector sidecar(s) already present in
    `out_dir` — the exact pipeline main() uses, minus the file write. Returns a frozen
    RenderContext (carries date/doc/models/node_index/friction/streams/skipped/html_text/
    html_bytes so the cheap B2 path reuses identical state). Raises RenderError on fatal
    conditions. Deterministic: same sidecars + streams (+ same `generation`) -> byte-identical
    html_text (render_html() contract preserved). `generation` None (the one-shot/file:// path)
    emits NO generation meta, so that output is unchanged. `streams` None is treated as the
    no-friction all-None dict.

    Control 3: every UNENUMERATED fault from an unexpected sidecar/synthesis shape is
    converted to RenderError naming the sidecar and the exception type. RenderError
    itself passes through unchanged."""
    try:
        return _render_from_out_dir_inner(
            out_dir, date=date, streams=streams, no_friction=no_friction,
            generation=generation)
    except RenderError:
        raise
    except _RENDER_FALLBACK_ERRORS as exc:
        # T3 harden round (LOW): per-file sidecar faults now degrade in
        # _load_sidecar_guarded, and the SELECTED sidecar's own fault is converted at its
        # load site -- so a fault arriving HERE is a LATER-pipeline fault (a model builder,
        # the friction join, the HTML assembly) that no single sidecar can be blamed for,
        # i.e. most likely a real defect in this module. ONE KNOWN EXCEPTION (QA exit gate,
        # LOW 6): a corrupt SYNTHESIS sidecar also lands here, because `load_synthesis`
        # still calls `load_sidecar` unguarded (accepted follow-up) -- check it before
        # hunting a renderer bug. `str(exc)` alone made a genuine
        # bug indistinguishable from corrupt input, and neither main() nor serve.py prints
        # the chained cause, so the frames go to STDERR -- never into the page (the render
        # is abandoned, nothing is written) and never into a file. The RenderError message
        # itself stays the same single readable line.
        print(traceback.format_exc(), end="", file=sys.stderr)
        raise RenderError(_render_fault_message(out_dir, date, exc)) from exc


def _render_from_out_dir_inner(
    out_dir: Path,
    date: str | None = None,
    streams: dict[str, Any] | None = None,
    no_friction: bool = False,
    generation: int | None = None,
) -> RenderContext:
    """The render body; the public `render_from_out_dir` wrapper above carries the
    documented contract and converts unenumerated faults to RenderError."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        raise RenderError(f"fatal: --out-dir does not exist or is not a directory: {out_dir}")
    if date is not None and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise RenderError(f"fatal: --date must be YYYY-MM-DD: {date}")

    sidecars = find_sidecars(out_dir)
    if not sidecars:
        raise RenderError(f"fatal: zero sidecars found in {out_dir}")

    # The SELECTED sidecar's own unenumerated fault is converted HERE rather than at the
    # top-level envelope, so that envelope is left to later-pipeline faults (which get the
    # traceback diagnostic). An explicit --date naming a corrupt sidecar stays FATAL --
    # select_current's contract, never a silent substitution -- and the message keeps
    # naming the file exactly as the envelope did before.
    try:
        sel_date, doc, skipped, err = select_current(sidecars, date)
    except _RENDER_FALLBACK_ERRORS as exc:
        raise RenderError(_render_fault_message(out_dir, date, exc)) from exc
    if err is not None:
        raise RenderError(f"fatal: {err}")
    # select_current's contract: err is None iff sel_date/doc are both populated — cast
    # (not assert) so this is a pure type narrowing with zero runtime effect.
    sel_date = cast(str, sel_date)
    doc = cast(dict[str, Any], doc)
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise RenderError(
            f"fatal: selected sidecar has unsupported schema_version {doc.get('schema_version')}")

    # Trend series: load every OTHER sidecar too, filtered to the same root + schema version
    # (Codex F13); corrupt/incompatible ones are excluded and noted in skipped[].
    dated_docs: list[tuple[str, dict[str, Any]]] = []
    for d, p in sidecars:
        if d == sel_date:
            dated_docs.append((d, doc))
            continue
        other_doc, other_err = _load_sidecar_guarded(p)
        if other_err is not None:
            skipped.append({"date": d, "reason": other_err})
            continue
        # load_sidecar's contract: other_err is None iff other_doc is populated — cast
        # (not assert) so this is a pure type narrowing with zero runtime effect.
        other_doc = cast(dict[str, Any], other_doc)
        if other_doc.get("schema_version") != SCHEMA_VERSION or other_doc.get("root") != doc.get("root"):
            skipped.append({"date": d, "reason": "schema_version/root mismatch with selected sidecar"})
            continue
        dated_docs.append((d, other_doc))
    dated_docs.sort(key=lambda t: t[0])

    synth, synth_err = load_synthesis(out_dir, sel_date)
    if synth_err is not None:
        skipped.append({"date": sel_date, "reason": synth_err})

    models = {
        "context_weight": build_contextweight_model(doc),
        "bipartite": build_bipartite_model(doc),
        "trend": build_trend_model(dated_docs),
        "dupweb": build_dupweb_model(doc),
        "civc": build_civc_model(synth),
        "drag": build_dragcandidate_model(synth),
    }
    node_index = build_node_index(models)

    if streams is None or no_friction:
        streams = {"decisions": None, "metrics": None, "interventions": None, "codex": None}
    else:
        streams = _contain_default_interventions(streams, doc.get("root"))
    friction = build_friction_overlay(doc, streams, node_index, sel_date, no_friction)

    html_text = render_html(sel_date, models, friction, {"doc": doc, "skipped": skipped},
                            generation=generation)

    return RenderContext(
        date=sel_date,
        doc=doc,
        models=models,
        node_index=node_index,
        friction=friction,
        streams=streams,
        friction_disabled=no_friction,
        skipped=skipped,
        html_text=html_text,
        html_bytes=html_text.encode("utf-8", "backslashreplace"),
    )


# ---------------------------------------------------------------------------------- main
def _load_sibling_collector():
    """Lazily loads the sibling `collector.py` by absolute path (mirrors serve.py's
    `_load_sibling`, so this works regardless of the invoking cwd or whether render_html
    itself was loaded via `spec_from_file_location`, as the test suite does) — collector.py
    never imports render_html.py, so no load-time cycle. Loaded on demand (not at module
    import time) so a non-compose invocation never pays the cost of importing collector.py
    at all. Callers should go through `_get_sibling_collector()` instead of calling this
    directly, to share the cached module rather than re-exec'ing it per call."""
    module_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location(
        "harness_map_render_html_collector", module_dir / "collector.py")
    # spec_from_file_location's return type is `ModuleSpec | None`; for a fixed sibling
    # path next to this file it is never None in practice. module_from_spec/spec.loader
    # narrow to Any once past this call, so this is the ONE ignore for the whole seam.
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]  # sibling loaded via importlib (render_html.py:3441)
    spec.loader.exec_module(module)  # type: ignore[union-attr]  # sibling loaded via importlib (render_html.py:3441)
    return module


_SIBLING_COLLECTOR_MODULE = None


def _get_sibling_collector():
    """Cached accessor for `_load_sibling_collector()` (P1-B): `write_html_safely` calls
    this on EVERY write, including the serve.py B2 cheap friction-only re-render path,
    so re-exec'ing collector.py's whole module body per call would defeat the entire
    point of that cheap path. Imported lazily on first use (never at module import
    time), then reused for the rest of the process."""
    global _SIBLING_COLLECTOR_MODULE
    if _SIBLING_COLLECTOR_MODULE is None:
        _SIBLING_COLLECTOR_MODULE = _load_sibling_collector()
    return _SIBLING_COLLECTOR_MODULE


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Render an interactive HTML map from harness-map sidecar(s).")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--date", default=None)
    ap.add_argument("--metrics-file", default=None)
    ap.add_argument("--decisions-file", default=None)
    ap.add_argument("--codex-file", default=None)
    ap.add_argument("--interventions-file", default=None)
    ap.add_argument("--no-friction", action="store_true")
    args = ap.parse_args(argv)

    if args.no_friction:
        streams = None
    else:
        streams = default_streams()
        if args.decisions_file:
            streams["decisions"] = Path(args.decisions_file)
        if args.metrics_file:
            streams["metrics"] = Path(args.metrics_file)
        if args.codex_file:
            streams["codex"] = Path(args.codex_file)
        if args.interventions_file:
            streams["interventions"] = Path(args.interventions_file)

    try:
        ctx = render_from_out_dir(
            Path(args.out_dir), date=args.date, streams=streams, no_friction=args.no_friction)
    except RenderError as e:
        print(str(e), file=sys.stderr)
        return 1

    date, html_text, doc = ctx.date, ctx.html_text, ctx.doc
    out_path = Path(args.out_dir) / f"harness-map-{date}.html"
    # T13 F2 / P1-B: a compose sidecar carries `inspected_roots.project_containment` --
    # when present, an `--out-dir` inside the composed PROJECT repo must be rejected too,
    # not only the operator root. Absent `inspected_roots` (non-compose or old-shape
    # sidecar, C15) leaves `guard_roots` holding only the operator root, so a non-compose
    # run's behavior/output is unchanged. This is now the ONLY write-time guard (no
    # separate up-front check that write_html_safely's own re-validation could grow stale
    # against by the time the write actually happens) — `write_html_safely` re-resolves
    # and re-checks `guard_roots` fresh, immediately before writing.
    inspected_roots = doc.get("inspected_roots")
    project_containment_root = inspected_roots.get("project_containment") if inspected_roots else None
    sidecar_root = doc.get("root")
    # A non-string root can't be validated against, and write_html_safely's docstring is
    # explicit that an EMPTY guard_roots skips containment validation entirely -- so
    # silently dropping an unparseable root here would convert "cannot verify containment"
    # into "do not check containment" (the fail-open this guard exists to prevent). Refuse
    # the write instead of guessing or falling back to some other root.
    if sidecar_root is not None and not isinstance(sidecar_root, str):
        print(f"fatal: sidecar root field is not a string: {sidecar_root!r}", file=sys.stderr)
        return 1
    # A26 fixed the wrong-type case above; it left the FALSY case open. `sidecar_root`
    # legitimately falls out of `guard_roots` below when it is "", None, or absent (T3.11
    # requires exactly that -- a genuinely unknown root must drop the DEFAULT-derived
    # interventions stream via `_contain_default_interventions`, not refuse the whole
    # render: stream-selection containment and write-time containment are different jobs).
    # On a non-compose run `project_containment_root` is also None, so both truthy roots
    # can be absent at once -- the truthiness filter below used to empty `guard_roots`
    # entirely in that case, reproducing on the FALSY path exactly the fail-open A26
    # closed on the WRONG-TYPE path: an empty `guard_roots` makes write_html_safely skip
    # containment validation altogether.
    #
    # Fix: fold in a permanent FLOOR root this process independently knows to be true --
    # "this run must never write inside its own harness" -- ADDITIVE to, never a
    # replacement for, the sidecar-derived roots above. Both are enforced whenever the
    # sidecar root is usable; the floor alone still guards when it is not. This does not
    # revive the fallback A26 rejected: A26 rejected substituting Path.home()/".claude"
    # for CONTAINMENT VERIFICATION -- treating an unverifiable sidecar-reported root AS IF
    # it were the scanned root, which fabricates the very fact the guard exists to check.
    # The floor asserts nothing about what the sidecar scanned; it only asserts a fact
    # independent of the sidecar entirely ("this process must never write inside
    # ~/.claude"), so it is additive, never a substitute for the sidecar root when that
    # root IS known.
    #
    # Residual (disclosed, not closed): a falsy sidecar root plus an `--out-dir` inside
    # some OTHER mapped harness root (not `~/.claude`) still writes -- the floor cannot
    # guard a root the sidecar failed to report. Closing that needs the sidecar to report
    # its root reliably, not a wider guess here.
    floor_root = str(Path.home() / ".claude")
    guard_roots = [r for r in (sidecar_root, project_containment_root, floor_root) if r]
    if not guard_roots:
        # Unreachable today (floor_root is always non-empty) -- kept so a future refactor
        # that drops the floor fails CLOSED (hard error) instead of silently reverting to
        # the fail-open above.
        print("fatal: no guard roots available to validate the write against", file=sys.stderr)
        return 1
    try:
        write_html_safely(out_path, html_text, guard_roots)
    except RenderError as e:
        print(str(e), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"fatal: could not write {out_path}: {e}", file=sys.stderr)
        return 1
    print(str(out_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
