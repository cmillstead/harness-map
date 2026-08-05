# Harness Map — {YYYY-MM-DD}

Fill-in skeleton for the synthesis pass. Every `{placeholder}` is replaced with a value read from the collector's JSON (stdout / sidecar) — never invented. Every row/cell carries an evidence label; every gap is stated explicitly, never omitted.

## 1. Headline Numbers

The collector's original 8 headline fields, each with an always-visible benchmark band and a footnote gloss. Bands come from the FIXED thresholds in the table below — never invented per-run, so successive runs are comparable. A 9th line (TRK-025) follows: an additive coverage DENOMINATOR, not a defect count — it carries neither a band nor an "informational"/"reserved" marker, unlike every one of the 8 above.

- Always-loaded words: {always_loaded_words} — **{weight_band}**[^words]
- Always-loaded estimated tokens: {always_loaded_tokens_est} — **{weight_band}**[^tokens]
- Always-loaded file count: {always_loaded_file_count} — informational[^filecount]
- Duplicate-pair count: {duplicate_pair_count} — **{dup_band}**[^dup]
- Unchecked-binary count: {unchecked_binary_count} — reserved (not inspected)[^binary]
- Instruction-files-over-200 count: {instruction_files_over_200} — **{over200_band}**[^over200]
- Orphan-registration count: {orphan_registration_count} — **{orphanreg_band}**[^orphanreg]
- Orphan-script count: {orphan_script_count} — **{orphanscript_band}**[^orphanscript]
- Hook commands examined: {hook_commands_examined} / {hook_commands_total}[^hookcoverage] — state this denominator EVERY time the two orphan counts above are reported, so "0 orphans" never reads as "0 orphans out of everything" when it is really "0 orphans out of a partial scan"

### Fixed band thresholds (apply verbatim every run)

| Metric | Bands |
|---|---|
| always_loaded_tokens_est (the weight band) | <5,000 LOW / 5,000–12,000 MODERATE / >12,000 HIGH |
| always_loaded_words | weight band shown = the tokens band, computed once from always_loaded_tokens_est (no separate word threshold) |
| always_loaded_file_count | informational — no severity band |
| duplicate_pair_count | 0 CLEAN / ≥1 REVIEW |
| unchecked_binary_count | reserved — always renders "reserved (not inspected)", NEVER CLEAN |
| instruction_files_over_200 | 0 CLEAN / ≥1 → that many compliance-risk files |
| orphan_registration_count | 0 CLEAN / >0 ACT |
| orphan_script_count | 0 CLEAN / >0 ACT |

[^words]: The weight verdict is derived ONCE from `always_loaded_tokens_est` (the canonical cost signal) and shown identically on BOTH weight lines, so the words and tokens verdicts can never disagree at a threshold boundary. The words count itself remains the raw figure.
[^tokens]: Bands <5k LOW / 5–12k MODERATE / >12k HIGH. The cost of always-loaded weight is ATTENTION DILUTION and per-turn COMPOUNDING, not context-window exhaustion — 8k tokens is <1% of a 1M window. Anchor: CLAUDE.md alone ≈2.5k tokens, so ~5k is two CLAUDE.md-equivalents and ~12k is where dilution compounds materially across turns.
[^filecount]: Informational, no severity band — file count is a weak proxy; 16 small files can weigh less than 3 large ones. Read `always_loaded_tokens_est` as the real cost signal.
[^dup]: 0 CLEAN / ≥1 REVIEW (not ACT) — duplication is a candidate signal, not a defect. Command↔skill wrapper pairs and symlinked-rule pairs are expected-benign; review whether each pair is one declared home with callers (benign) or genuine two-home duplication.
[^binary]: Reserved; always 0 in v1 — no binary scan is performed (the walk reads only `.md`/`.py`/`.sh` via `errors='replace'`). This 0 means "not inspected," NEVER "no binaries found" — do not read it as a clean bill.
[^over200]: 0 CLEAN / ≥1 → that many compliance-risk files. The harness's operative threshold: context saturation degrades instruction compliance beyond ~200 lines per file, so each flagged file is one unit of compliance risk.
[^orphanreg]: 0 CLEAN / >0 ACT — a registration pointing at a missing script is dead enforcement (a hook that will never fire). Any >0 is structural breakage to act on.
[^orphanscript]: 0 CLEAN / >0 ACT — a script on disk reached by no registration and no dispatcher is dead code (best-effort static; a dynamic-dispatch caveat applies). Any >0 warrants action.
[^hookcoverage]: Not a defect count — a coverage denominator (`enforcement.hooks.commands_total`/`.commands_resolved`/`.commands_no_script`/`.commands_unparsed`). `hook_commands_examined` is `commands_resolved + commands_no_script`: every registered hook command the collector could actually classify, whether or not it names a script. The gap between the two numbers is `commands_unparsed` — a real coverage gap the collector could not read, disclosed in `blind_spots`. A "0 orphans" reading with `hook_commands_examined` below `hook_commands_total` means "0 orphans found among what was examined," never "0 orphans, full stop."

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
- Staleness now has two v2 signals (git-age + retired slash-commands) — see "Staleness Signals" below.
- SessionStart runtime emissions + MCP instruction text not statically collectable.

### Inaccessible Paths

| Path | Reason |
|---|---|
| {path} | {reason} |

### Staleness Signals

Two staleness signals, both review candidates — **stale ≠ dead**: the model flags these for a human to look at, it never condemns a file outright from the signal alone.

- **Git age** (`staleness.last_commit_ts`) — an instruction file with no recent commit is a review candidate: still-correct-and-stable and genuinely-forgotten look identical from a timestamp alone. A `null` here is NOT "very old" — read its cause from `staleness_null_reasons` (`untracked` → commit it or ignore it; `submodule_unavailable` → initialize the submodule; `budget_exhausted` → re-run, the file was never measured; `no_repo` → the scanned root is not a git work tree; `git_unavailable` → the git binary could not be run at all). The remaining five closed-enum values — `outside_root`, `timeout`, `git_error`, `unparseable`, `no_commits` — cover the rarer edge cases (work tree resolves outside `--root`, a per-file git call exceeded its cap, git exited non-zero or with unparseable output, or the path is tracked but has no commit yet); see `schema.md`'s full ten-value enum for exact meaning. Never treat a null as a staleness signal.
- **Retired slash commands** (`phantom_refs[]` where `kind == "slash_command"`) — a rule cites a `/command` for which no home exists **under the scanned root** (`commands/<ns>/…/<name>.md` at any nesting depth, a bare `/name` yielding `commands/<name>.md`; and `skills/<seg0>/SKILL.md`, keyed on the command's first segment/namespace, not its own name). This is `evidence: INFERRED`, `resolved: null` — Claude Code built-ins and plugin commands live outside the root and cannot be checked from here, so treat as "verify, then update or remove," never "auto-delete."

#### Git-Age Null Reasons

| Path | Null Reason |
|---|---|
| {path} | {null_reason} |

### Collector Errors

Runtime anomalies the collector itself hit (malformed/unreadable settings.json, glob failures, crash fallback) — render the full list, or "none":

- {errors[]} (or "none")

## 6. Diff vs Previous Run

Selection rule: most recent `harness-map-*.json` in OUT_DIR strictly before today.

Either:

Compared against `harness-map-{prior-date}.json`:

| Metric | Prior | Current | Δ |
|---|---|---|---|
| Always-loaded words | {prior} | {current} | {delta} |
| Always-loaded tokens (est) | {prior} | {current} | {delta} |
| File count | {prior} | {current} | {delta} |
| Duplicate-pair count | {prior} | {current} | {delta} |
| Instruction-files-over-200 | {prior} | {current} | {delta} |
| Orphan-registration count | {prior} | {current} | {delta} |
| Orphan-script count | {prior} | {current} | {delta} |

Or, if no prior sidecar exists, exactly:

First run — no prior map (baseline).

## Sidecar Note

This run also wrote `harness-map-YYYY-MM-DD.json` (machine-readable snapshot) into the same report directory (the operator-chosen `OUT_DIR`, default `$HOME/harness-map-reports/`) as this report; the next run diffs against it.
