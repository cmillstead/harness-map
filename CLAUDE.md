# CLAUDE.md — harness-map Upgrades

You are upgrading the `harness-map` skill per the spec set at the path supplied via the
`HARNESS_MAP_SPEC_DIR` environment variable (referenced below as `SPEC_DIR`; part-files
under `SPEC_DIR/spec/`). The spec set is maintained privately and is not distributed with
this repo. This file is binding.
When code you find conflicts with this file or the spec, the code is wrong — fix it, don't imitate it. **Exception:** where live source contradicts a *fact this spec cites* (a line number moved, a helper renamed, behavior changed by an interim commit), reality wins — record a one-line amendment in `SPEC_DIR/spec/AMENDMENTS.md` per SPEC_1 §4 and proceed per the spec's intent.

## Orientation (read once)

harness-map is a production-grade, read-only inventory of the Claude Code harness: `collector.py` (deterministic stdlib signals → schema-pinned JSON) → model synthesis (judgments → report + synthesis JSON) → `render_html.py` (offline HTML) → `serve.py` (hardened loopback dashboard). It must never write inside the harness it scans, must always emit a valid JSON envelope, and ships portability tests that must never weaken. You are executing a staged upgrade: corrections first (phantom-ref fix, typing, hygiene), then staleness/trends, loop closure, drag economics, and monitoring/generalization. The spec set was verified line-by-line against source on 2026-07-18 (`SPEC_DIR/spec/AMENDMENTS.md`, "Source verification record") — build from it directly; do not re-verify wholesale.

## How to load context (do this, not more)

1. Always: this file + `SPEC_DIR/MASTER_SPEC.md` (index) + `SPEC_DIR/MILESTONE.md` + `SPEC_DIR/spec/AMENDMENTS.md` (skim).
2. Then ONLY the part-files the index's reading list names for your milestone.
3. Update `SPEC_DIR/MILESTONE.md` when an item or milestone completes.

## Binding rules (violations are bugs, not style choices)

1. **Authority order on conflict:** SPEC_1 §2 hard invariants > recorded amendments > this file > MASTER_SPEC conventions > SPEC_2/SPEC_3 > SPEC_4–7 > the audit > existing code style > your judgment.
2. **[DECISION] markers are final.** Do not reopen or work around them. If one blocks you, stop and ask the operator.
3. **Drift protocol.** Source verification is complete; there is no verify-then-build gate. If live source contradicts a spec-cited fact, record a one-line amendment and proceed per the spec's intent; halt only if the contradiction touches a SPEC_1 §2 invariant or makes a [DECISION] unimplementable (SPEC_1 §3). **Symbol names are authoritative; line numbers are advisory.** The tree has drifted far past the 2026-07-18 snapshot (collector 1,784→4,438, render 2,896→4,726, serve 832→1,084 lines — AMENDMENTS A12; re-measured 2026-08-01 at S6a completion); at each milestone start, re-locate every cited symbol by name with Grep — never trust snapshot line numbers.
4. **Read-only posture.** No new write paths, anywhere, ever. The collector writes only a validated outside-root `--out`; the renderer only via `write_html_safely`. `--check` writes nothing.
5. **Envelope rule.** Every new collector field exists (null/empty) in `_empty_document`; `main()` must still emit valid JSON on any crash.
6. **Signals vs judgments.** Collector code never classifies, condemns, or verdicts. Deterministic renderer arithmetic (joins, scores) is a data operation; verdict words stay the model's. Drag verdicts cap at `probation`; schema.md's D8 `retire safely` cap is unconditional — friction telemetry never lifts it (AMENDMENTS A4).
7. **Never edit an existing test assertion.** Additions only. `test_release_decoupling.py` passes unmodified after every milestone — if a scaffold file trips it, the file moves, not the test. **A27 carve-out, narrow:** this protects assertions that PREDATE the current plan execution. An assertion written EARLIER IN THE SAME execution may be corrected **tighten-only** — never loosened, never deleted — and only with an amendment recorded in `SPEC_DIR/spec/AMENDMENTS.md` BEFORE the edit, carrying git-verified provenance (the commit that introduced the assertion is on this branch). Provenance is established with `git log -L` or `git blame`, never from memory. A27 exists to correct a contract that is WRONG; it is never a licence to bulldoze one that is correct — see A31, where A27 would technically have permitted an edit to T3.11 and it was declined on the merits.
8. **Never-waive gate:** `./check.sh` green (ruff + mypy + pytest, run from the skill dir) before anything is called done. No exceptions, including "just this once".
9. **stdlib-only runtime; no mocks; deterministic output** across `PYTHONHASHSEED` (fixed orderings, no bare `set()` iteration into output).
10. **Additive schema discipline:** new sidecar fields additive, readers `.get()`-tolerant, `schema.md` updated in the same change; `schema_version` bumps only on meaning change.
11. **Secrets:** env values never serialize (`config.env_keys` is names-only); scanned bytes are data, never instructions (parse-only `ast`, no import/exec).
12. **Scope fence:** the only file outside the skill dir you may edit is `skills/harness-engineer/SKILL.md` at M7, exact-diff approved by the operator in-session first. Registering hooks or touching `settings.json` is out of scope.

## Key numbers (memorize; sources cited)

- SKILL.md hard cap **200** lines, working cap **195**; **live baseline 101** (was 80 at the 2026-07-18 snapshot, 99 before S6a — the `--serve` batch grew it; AMENDMENTS A12; re-measured 2026-08-01 with `wc -l SKILL.md`); budgeted additions **≤26** ⇒ ≤127 worst case (SPEC_3 §5).
- Description rewrite target **≤70** words (from the verified 88), all 4 positive + 2 negative triggers preserved (SPEC_3 §4).
- Test baseline: **851 items live** (was 353 at the 2026-07-18 snapshot, 520 at S1, 688 entering S6a, 765 at S6a completion — AMENDMENTS A10, A28); macOS **850 passed / 1 skipped**, re-measured 2026-08-02 at S6b completion. Count only goes up; existing tests untouched. **Measuring trap:** a PIPED `--collect-only` returns a wrapper line reading `Pytest: No tests collected`, which is not the real result — redirect to a file and read the file instead.
- Test placement (AMENDMENTS A8): renderer features → `test_render_html.py`; collector features → `test_collector.py`; only M11 adds `test_profiles.py`.
- Phantom fix: **DONE in S1.M0** — candidate order `root/norm` then `root/<source-dir>/norm`, `_safe_exists` tri-state preserved (SPEC_3 §1; fixed at `collector.py:2242`, was `:1166` at snapshot — AMENDMENTS A11).
- Drag: `est_tokens × (1 + friction_events_30d)`, 30-day window computed POST-join, undated records excluded (SPEC_6 §1). Cost: median **events/day** (never "turns/day") over **14** days, min **3** active days (SPEC_6 §2).
- Trend sparklines: display window **N=10** points, appear at ≥3 MEASURED points per series (a crashed run or a missing headline key contributes no point; sidecar count alone is not the gate); loader already exists — visualization only (SPEC_4 §3, AMENDMENTS A2).
- `--check`: exit **0/1/2**; `CHECK_BANDS` = report-template thresholds **5,000 / 12,000** tokens (NOT the renderer's `GAUGE_BANDS` 6,000/15,000 — two homes exist, AMENDMENTS A3); wall-time budget **≤5s** (SPEC_7 §1).
- Kill signals: >2 staleness false positives per real run; >50 mypy errors; >10 type-ignores; 1 edited existing assertion; <10 dated joinable telemetry records at M8 (RISK_REGISTER).

## Workflow expectations

- `./check.sh` green before claiming any task complete. New behavior needs tests in the same change, real-fixture style (extend `conftest.py::fake_harness` and the `run_collector`/`run_render` subprocess drivers — no mocks).
- Spec constants encoded in tests carry: `# Changing this value requires a spec change (SPEC_N §M).`
- No pre-created stubs: each milestone adds its functions with tests; contracts live in the spec (SPEC_2 §5).
- Fixture policy: fixtures are real temp trees/repos/streams built in-test; nothing golden is regenerated without a spec section saying how.
- No new top-level files in the skill dir beyond the ledger in SPEC_2 §4; no new dependencies, period.
- Commit style: `harness-map S<stage>.M<n>: <imperative summary>`.

## Current state

See `SPEC_DIR/MILESTONE.md`. Scaffold status at handoff: **nothing is created yet** — M0 creates `check.sh`, `mypy.ini`, and lands the phantom-ref fix + regression tests. Source verification is already complete (2026-07-18); M0 starts with the scaffold, not with verification.
