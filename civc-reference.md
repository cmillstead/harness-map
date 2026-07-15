# CIVC Coverage Matrix — Reference (vendored)

Vendored from the harness knowledge base (`harness-inventory-four-verbs.md` §9, 2026-06-24)
so the harness-map skill is self-contained. This is the ONLY reference the CIVC matrix
(report section 3) draws on — do NOT fetch an external vault path.

## The two-axis grid

The CIVC matrix is a two-axis grid. Fill EVERY cell.

- **Verbs (rows)** — what the harness does to behavior:
  - **Afford** — GRANTS a capability the agent otherwise lacks; adds to the action space (MCP servers, skills, tool-availability hints). The additive dual of Constrain.
  - **Inform** — shapes the input BEFORE the action (context injection, reference files, memory reads, CLAUDE.md context).
  - **Constrain** — SUBTRACTS from the action space (permission deny-lists, blocking hooks, tool restrictions).
  - **Verify** — judges the output and GATES (blocking checks, audit loops, count-verification gates).
  - **Correct** — fixes a failed RUN when a problem is detected (recovery injection, fix rounds, auto-sync).
  - **Evolve** — operates on the HARNESS itself, the outer loop, not the run (promotion of learnings to checks, self-audit, graduated checks).
- **Surfaces (columns)** — what the harness is made of: **context, tools, memory, permissions, orchestration, observability**.

| Verb | context | tools | memory | permissions | orchestration | observability |
|---|---|---|---|---|---|---|
| Afford |  |  |  |  |  |  |
| Inform |  |  |  |  |  |  |
| Constrain |  |  |  |  |  |  |
| Verify |  |  |  |  |  |  |
| Correct |  |  |  |  |  |  |
| Evolve |  |  |  |  |  |  |

## Cell verdicts

Each cell gets EXACTLY ONE verdict:

- **covered** — a real, identifiable mechanism fills this verb x surface intersection.
- **thin** — a mechanism exists but is weak, partial, single-instance, or young (e.g. one advisory-only check).
- **empty** — no mechanism occupies this intersection.

Empty cells ARE the roadmap, not an omission — state them explicitly; NEVER drop a cell.

## Reading guidance (from §9)

- Memory is its own SURFACE, not a verb. Its write-side (Persist) spreads across Inform/Constrain/Correct; classify each memory mechanism by the VERB it serves on the memory surface.
- Observe (telemetry/health/drift taps that report without gating) is distinct from Verify (which gates). File pure taps under the observability surface for the relevant verb, NOT automatically under Verify.
- The Evolve row/column is typically real-but-young (thin), not empty — do NOT mark it empty if any self-modification loop exists.
