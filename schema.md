# harness-map: collector → synthesis JSON schema

This document is the pinned data contract for the harness-map skill. It has two halves:

- **(A) Collector JSON output** — the exact structure `collector.py` writes. Deterministic, machine-produced, no judgment calls.
- **(B) Synthesis report structures** — the structures a model pass derives *from* (A). Interpretive, not deterministic.

Both `collector.py` and the synthesis pass must conform to this document. If either needs to diverge, update this file first.

## Evidence labels

Every collected fact carries one of three evidence labels:

- **`VERIFIED`** — the collector read the actual bytes of a static file (opened it, read its content, computed real metrics from it).
- **`INFERRED`** — existence or content was deduced from a secondary source (a `settings.json` registration entry, a directory listing, a dispatcher's body, runtime MCP instructions) without reading the canonical static file itself.
- **`INACCESSIBLE`** — a reference names a target but the collector could not read it (missing file or permission denied). An inaccessible target is never treated as confirmed absent.

## (A) Collector JSON output

Top-level keys are ALL present on every run. When a category has no data, its array is empty (`[]`) and its counts are `0` — keys are never omitted.

```jsonc
{
  "schema_version": 1,
  "generated_at": "<ISO-8601 UTC>",
  "root": "<abs path>",
  "headline": {
    "always_loaded_words": 0, "always_loaded_tokens_est": 0, "always_loaded_file_count": 0,
    "duplicate_pair_count": 0, "unchecked_binary_count": 0, "instruction_files_over_200": 0,
    "orphan_registration_count": 0, "orphan_script_count": 0
  },
  "always_loaded": {
    "files": [{"path": "rel", "category": "claude_md|project_claude_md|memory|rule|coding_team_rule|skill_rule",
               "words": 0, "lines": 0, "tokens_est": 0, "evidence": "VERIFIED|INACCESSIBLE"}],
    "conditional_variants": [{"path": "rel", "project_slug": "", "words": 0, "lines": 0,
                              "tokens_est": 0, "evidence": "VERIFIED"}],
    "skill_descriptions": [{"name": "", "words": 0, "evidence": "VERIFIED"}],
    "agent_descriptions": [{"name": "", "words": 0, "evidence": "VERIFIED"}],
    "totals": {"words": 0, "tokens_est": 0, "file_count": 0}
  },
  "on_demand": {
    "skills": [{"name": "", "lines": 0, "words": 0, "has_test": false, "evidence": "VERIFIED"}],
    "skill_internal_bodies": [{"skill": "", "path": "rel", "kind": "phase|prompt|agent",
                               "lines": 0, "words": 0, "evidence": "VERIFIED"}],
    "memory_bodies": [{"path": "rel", "project_slug": "", "lines": 0, "words": 0, "evidence": "VERIFIED"}]
  },
  "enforcement": {
    "hooks": {
      "registered": [{"command": "", "script": "rel", "exists": true, "registered_via": "direct",
                      "registration_evidence": "VERIFIED", "target_evidence": "VERIFIED|INFERRED"}],
      "orphan_registrations": [{"script": "rel", "target_status": "missing", "registration_evidence": "VERIFIED"}],
      "scripts_on_disk": [{"name": "", "is_symlink": false, "target": null,
                           "registered_via": "direct|dispatcher|none", "evidence": "VERIFIED|INFERRED"}],
      "orphan_scripts": [{"name": "", "evidence": "INFERRED"}]
    },
    "permissions": {"allow_count": 0, "deny_count": 0, "ask_count": 0, "evidence": "VERIFIED|INACCESSIBLE"}
  },
  "config": {
    "env_keys": ["<KEY NAME ONLY — never values>"], "env_key_count": 0,
    "model": "<str|null>", "cleanup_period_days": 0, "sandbox": false,
    "enabled_plugins": [{"name": "", "enabled": true}], "plugin_count": 0,
    "marketplaces": ["<name>"], "marketplace_count": 0,
    "installed_plugins": ["<name>"], "installed_plugin_count": 0,
    "evidence": "VERIFIED|INACCESSIBLE"
  },
  "instruction_length_flags": [{"path": "rel", "lines": 0, "threshold": 200, "evidence": "VERIFIED"}],
  "duplication": {"shingle_k": 8, "metric": "containment", "threshold": 0.6,
                  "pairs": [{"a": "rel", "b": "rel", "score": 0.0, "shared_sample": "", "evidence": "INFERRED"}]},
  "phantom_refs": [{"source": "rel", "ref": "", "kind": "path|env_flag|external", "resolved": false, "evidence": "VERIFIED|INFERRED"}],
  "promotion_candidates": [{"source": "rel", "pattern": "NEVER|ALWAYS|must|numeric_cap|required_file",
                            "excerpt": "", "hook_covered": false, "evidence": "INFERRED"}],
  "test_coverage": {"hooks": [{"name": "", "has_test": false}], "skills": [{"name": "", "has_test": false}],
                    "summary": {"hooks_with_test": 0, "hooks_total": 0, "skills_with_test": 0, "skills_total": 0}},
  "inaccessible": [{"path": "rel", "reason": ""}],
  "blind_spots": ["<string>"],
  "errors": ["<string>"]
}
```

### Field notes

- **`headline`** — an eight-number rollup used as the diff unit between runs (see Note 3 below). Every field is a plain count, computed from the other sections.
- **`always_loaded`** — everything paid for on every conversation turn regardless of whether the skill/rule is invoked: root and project `CLAUDE.md` files, memory files, `rules/*.md`, coding-team rules, plus the *description* text of every skill and agent (their bodies are NOT always-loaded — only the frontmatter description shown in the picker). `conditional_variants` covers per-project `CLAUDE.md` variants that load only when that project is the cwd. The rule scan is generalized to `skills/*/rules/*.md` (any sub-skill's rules dir), scanned AFTER `rules/*.md` so a rule reachable via both a `rules/` deploy symlink and a sub-skill source is deduped by physical identity and counted once under `rules/` (category `rule`). A sub-skill's own rule files carry category `coding_team_rule` when the sub-skill is `coding-team` (retained for baseline continuity) and `skill_rule` for every other sub-skill. Hook test detection is likewise generalized to `hooks/tests` + `skills/*/hooks/tests`.
- **`on_demand`** — content that loads only when a skill/agent is actually invoked: skill `SKILL.md` files, their internal `phases/`, `prompts/`, `agents/` bodies, and memory file bodies (as opposed to the memory index entry, which is always-loaded).
- **`enforcement.hooks`** — see Note 3 (registration vs target status) below; this is the section that distinction governs.
- **`config`** — a snapshot of `settings.json` / `.claude.json`-level configuration. `env_keys` is names only (see Note 2). `evidence` at the `config` level covers whether `settings.json` itself was readable.
- **`instruction_length_flags`** — any instruction file (SKILL.md, phase, prompt, agent, rule) whose line count exceeds `threshold` (200).
- **`duplication`** — near-duplicate content pairs across instruction files. See Note 2 for the metric and Note 3 for determinism of the output ordering.
- **`phantom_refs`** — a reference (file path, env-flag name, or external URL/tool) named in an instruction file that does not resolve to a real, checkable target.
- **`promotion_candidates`** — prose in an instruction file that reads like a hard rule (`NEVER`, `ALWAYS`, `must`, a numeric cap, a required-file assertion) but has no corresponding hook enforcing it. `hook_covered` is `true` only when the collector matched it to an entry in `enforcement.hooks.registered`.
- **`test_coverage`** — whether each hook script and each skill has an associated test, and the aggregate summary counts.
- **`inaccessible`** — every path anywhere in the run that the collector attempted to read and could not, with the OS-level `reason`.
- **`blind_spots`** — free-text notes on categories of content the collector structurally cannot see (e.g., runtime-only MCP server instructions, plugin-marketplace content not vendored locally).
- **`errors`** — any collector-internal error encountered mid-run; a non-fatal error is recorded here and the run continues.

## (B) Synthesis report structures

The synthesis pass is a model pass over (A)'s output. It produces judgments — the collector never does.

- **CIVC matrix cell** — a table, rows = verbs `[Afford, Inform, Constrain, Verify, Correct, Evolve]`, columns = surfaces `[context, tools, memory, permissions, orchestration, observability]`, each cell ∈ `{covered, thin, empty}`. Produced by the model from `always_loaded`, `enforcement`, `on_demand`, and `test_coverage`.
- **Drag-candidate record** — `{ n, surface, evidence (V/I/IA), outcome ∈ {keep, give it one home, load it later, turn it into a check, probation, retire safely}, what_must_survive, risk_if_wrong }`. Per D8, `retire safely` is disallowed in v1 (no-usage) runs — cap the outcome at `probation`.
- **Diff snapshot** — the `headline` block IS the diff unit. Synthesis compares the current run's `headline` against the most-recent prior sidecar's `headline`, field by field.

## Notes

1. **Signals vs. judgments.** The collector emits SIGNALS (A) — it counts, reads, and classifies mechanically (file categories, evidence labels, line thresholds). The model produces JUDGMENTS (B) — CIVC classification, drag outcomes, "give it one home" decisions. The collector never classifies a verb coverage or condemns a duplicate pair as dead weight; it only reports that the pair exists above threshold.

2. **Secret safety and the duplication metric.** `config.env_keys` lists env KEY NAMES only — the collector NEVER emits env VALUES (the real `env` holds `GITHUB_TOKEN` and other secrets). `duplication.metric` is `"containment"` (`|A∩B| / min(|A|,|B|)`), not Jaccard — containment is chosen because it correctly flags a short file fully subsumed by a longer one, which Jaccard would under-score. The shingle size constant `SHINGLE_K = 8` (also `duplication.shingle_k` in the output) sets the k-gram window used to build each file's shingle set before comparison. **Scope boundary:** this guard applies to `env` VALUES specifically — hook `command` strings ARE surfaced verbatim in `enforcement.hooks.registered[].command` and (truncated) in `blind_spots`, by design, since reconciliation requires showing the registered command for a human to audit an orphan. The harness convention is that secrets live in `env`, not inline in hook commands; this boundary is accepted, not a gap.

3. **Registration vs. target status — no exception.** A hook registration finding SEPARATES two facts and never collapses them. `registration_evidence: "VERIFIED"` means the collector READ the registration line in `settings.json` — that fact alone is always knowable. The registered TARGET's status is reported independently, by `stat()`-ing the path: a `FileNotFoundError` on the target routes the entry to `enforcement.hooks.orphan_registrations[]` with `target_status: "missing"` — a real orphan. A `PermissionError` on the target routes the entry to `inaccessible[]` with `evidence: "INACCESSIBLE"` — a permission-denied target is NOT an orphan and must never be condemned as one, because the collector could not actually see it. Registration evidence and target status are always distinct fields; nothing sets one from the other. `duplication` output is deterministic across runs: pairs are sorted by `(-score, a, b)`, and `shared_sample` is the lexicographically-smallest shared shingle between the pair.

4. **`headline.unchecked_binary_count` is reserved, always `0` in v1.** No binary scan is performed — the walk reads only `.md`/`.py`/`.sh` via `errors='replace'` (text mode). This `0` reflects "not inspected," never "no binaries found" — do NOT read it as a clean bill of health.
