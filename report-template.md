# Harness Map — {YYYY-MM-DD}

Fill-in skeleton for the synthesis pass. Every `{placeholder}` is replaced with a value read from the collector's JSON (stdout / sidecar) — never invented. Every row/cell carries an evidence label; every gap is stated explicitly, never omitted.

## 1. Headline Numbers

The collector's 8 headline fields, verbatim:

- Always-loaded words: {always_loaded_words}
- Always-loaded estimated tokens: {always_loaded_tokens_est}
- Always-loaded file count: {always_loaded_file_count}
- Duplicate-pair count: {duplicate_pair_count}
- Unchecked-binary count: {unchecked_binary_count} — reserved; always 0 in v1 — no binary scan is performed (the walk reads only `.md`/`.py`/`.sh` via `errors='replace'`); do NOT read this 0 as "clean."
- Instruction-files-over-200 count: {instruction_files_over_200}
- Orphan-registration count: {orphan_registration_count}
- Orphan-script count: {orphan_script_count}

## 2. System Map

Each row carries an evidence label: **VERIFIED** (read the actual bytes) / **INFERRED** (deduced from a secondary source) / **INACCESSIBLE** (named but unreadable).

### Always-Loaded

| Path | Category | Words | Tokens (est) | Evidence |
|---|---|---|---|---|
| {path} | {category} | {words} | {tokens_est} | {evidence} |

### On-Demand

| Skill / Body | Words | Has Test | Evidence |
|---|---|---|---|
| {name} | {words} | {has_test} | {evidence} |

### Enforcement (Hooks / Permissions)

| Hook / Rule | Registered Via | Evidence |
|---|---|---|
| {name} | {registered_via} | {evidence} |

- Permissions: allow={allow_count}, deny={deny_count}, ask={ask_count} — evidence: {evidence}

## 3. CIVC Coverage Matrix

Two-axis grid: verbs (Afford, Inform, Constrain, Verify, Correct, Evolve) × surfaces (context, tools, memory, permissions, orchestration, observability). Every cell gets a verdict; empty cells ARE the roadmap, not an omission.

| Verb | Context | Tools | Memory | Permissions | Orchestration | Observability |
|---|---|---|---|---|---|---|
| Afford | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} |
| Inform | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} |
| Constrain | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} |
| Verify | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} |
| Correct | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} |
| Evolve | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} | {covered/thin/empty} |

## 4. Drag Candidates

`retire safely` is DISALLOWED in v1 (no usage data) — cap at `probation`.

Outcome legend (exactly one per row): `keep` / `give it one home` / `load it later` / `turn it into a check` / `probation` / `retire safely`.

| # | Surface | Evidence (V/I/IA) | Outcome | What must survive | Risk if wrong |
|---|---|---|---|---|---|
| {n} | {surface} | {V/I/IA} | {outcome} | {what must survive} | {risk if wrong} |

### Promotion Candidates (prose rules not hook-covered)

Separate from the headline block above — derived from `promotion_candidates` where `hook_covered=false`. `promotion_candidates` propose extending an EXISTING hook before proposing a new one.

- Prose rules NOT hook-covered: {count of promotion_candidates where hook_covered=false}

| Source | Pattern | Excerpt | Hook Covered |
|---|---|---|---|
| {source} | {NEVER/ALWAYS/must/numeric_cap/required_file} | {excerpt} | {hook_covered} |

## 5. Blind Spots

Full INACCESSIBLE list plus these standing v1 disclosures (verbatim, every report). The collector's `errors[]` MUST also render here — never omit it; a non-empty `errors[]` rendered nowhere produces a falsely-clean report, contradicting the "inaccessible ≠ clean" invariant:

- Per-project `CLAUDE.md` weight NOT collected — always-loaded weight is UNDERCOUNTED.
- Staleness = phantom-refs only; git-age correlation + retired-tool-rule detection deferred to v2.
- SessionStart runtime emissions + MCP instruction text not statically collectable.

| Path | Reason |
|---|---|
| {path} | {reason} |

### Collector Errors

Runtime anomalies the collector itself hit (malformed/unreadable settings.json, glob failures, crash fallback) — render the full list, or "none":

- {errors[]} (or "none")

## 6. Diff vs Previous Run

Selection rule: most recent `harness-map-*.json` strictly before today.

Either:

Compared against `harness-map-{prior-date}.json`:

| Metric | Prior | Current | Δ |
|---|---|---|---|
| Always-loaded words | {prior} | {current} | {delta} |
| Always-loaded tokens (est) | {prior} | {current} | {delta} |
| File count | {prior} | {current} | {delta} |
| Duplicate-pair count | {prior} | {current} | {delta} |
| Length-flag count | {prior} | {current} | {delta} |
| Orphan-registration count | {prior} | {current} | {delta} |
| Orphan-script count | {prior} | {current} | {delta} |

Or, if no prior sidecar exists, exactly:

First run — no prior map (baseline).

## Sidecar Note

This run also wrote `harness-map-YYYY-MM-DD.json` (machine-readable snapshot) alongside this report; the next run diffs against it.
