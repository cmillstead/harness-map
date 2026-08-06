# Completion Summary — TRK-021 dashboard UI polish

**Branch:** `feat/trk-021-dashboard-ui-polish` → merged to `main` via PR #13 (`92e3555`), 2026-08-05.
**Scope:** the five operator findings from the 2026-08-05 live dashboard review, all dispositioned Fix. 2 files (`render_html.py`, `tests/test_render_html.py`), 7 commits, +11 tests (1005→1016 passed / 3 skipped).

**Audit rounds:** 5 per-task rounds (3 auditors each) + feature QA + cross-model review — all converged in ≤2 rounds each.
**Exit reason:** clean audit (one Medium and one P2 fixed in dedicated follow-up commits; two Lows noted-and-declined with rationale).

## Recurring patterns

- **Representation-blind audit** (1 occurrence, P2, fixed in `17b2b62`): the Task 5 dark-mode audit scanned color *literals* and pinned them with an allowlist test — so a WCAG-failing pair composed entirely of `var()` references (pressed expand-all: accent-on-accent-soft, 3.65:1 in light) passed six same-model reviewers and was only caught by the cross-model (Codex) round. Fix resolves the rule's tokens against both parsed palettes and computes the ratio in a test. Captured as second-opinion learning entry C31: when auditing a resolved-value property, check the resolved pairs, not the spelling.
- **Plan test-coverage claims vs. delivered tests** (1 occurrence, Medium, fixed in `12ddd2c`): the plan's failure-modes table claimed the SVG fill-fallback fix was covered by the "stylesheet/markup literal scan," but the delivered test scans only `STATIC_STYLE` — Python f-string markup was invisible to it. The spec auditor caught the gap; a source-level pin test closed it.
- **Plan constants vs. measured reality** (2 occurrences, both resolved in-task): the plan assumed light-theme white-on-crit passed 4.5:1 (measured 4.45:1 — failed; black used instead) and specified a theme-sync test that would have permanently failed on the two deliberately theme-invariant tokens (`--r`, `--mono` — excluded with documented rationale). Both deviations were measurement-driven and validated by the spec reviewer.

## Unresolved (low severity, deferred)

- Copy-brief test asserts an attribute ordering (`<details open class=`) that the emitter cannot produce — redundant but harmless; removing it would delete a test assertion (rule 7 is tighten-only), so it stays.
- `_theme_block` overlaps `_css_decls` in extraction logic — kept separate deliberately: its `.index()` raises on a missing theme block, whereas consolidating on `_css_decls` (returns `""`) would let the sync test pass vacuously if every theme block vanished.

## Out-of-scope observations

- The codesight index for this repo returned stale/empty results for four separate agents; all fell back to grep/Read. Worth a re-index before the next task wave.
- Independently computed WCAG ratios differed slightly between reviewers (4.45 vs 4.36 for the same pair) — a known sRGB-threshold variant in the luminance formula (0.03928 vs 0.04045); conclusions agreed in every case.
- Accessibility trade-off shipped deliberately: while expand-all is active, no tab in the tablist is `aria-selected` (four panels visible). Transient by design — any tab interaction restores single-selection.

## Gates not run

- Codex `challenge` skipped per the Small-tier matrix — `/second-opinion challenge` runs it on this diff.
