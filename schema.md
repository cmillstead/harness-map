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
    "files": [{"path": "rel", "category": "claude_md|project_claude_md|project_claude_local_md|project_claude_md_nested|memory|rule|project_rule|coding_team_rule|skill_rule",
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
                           "registered_via": "direct|dispatcher|none",
                           "description": "", "evidence": "VERIFIED|INFERRED"}],
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
  "phantom_refs": [{"source": "rel", "ref": "", "kind": "path|env_flag|external|slash_command", "resolved": false /* |null */, "evidence": "VERIFIED|INFERRED"}],
  "promotion_candidates": [{"source": "rel", "pattern": "NEVER|ALWAYS|must|numeric_cap|required_file",
                            "excerpt": "", "hook_covered": false, "evidence": "INFERRED"}],
  "test_coverage": {"hooks": [{"name": "", "has_test": false}], "skills": [{"name": "", "has_test": false}],
                    "summary": {"hooks_with_test": 0, "hooks_total": 0, "skills_with_test": 0, "skills_total": 0}},
  "staleness": {"git_age_available": false, "last_commit_ts": {"<rel path>": 0 /* |null */}},
  "staleness_null_reasons": {"<rel path>": "git_unavailable|no_repo|outside_root|untracked|submodule_unavailable|timeout|budget_exhausted|git_error|unparseable|no_commits"},
  "inaccessible": [{"path": "rel", "reason": ""}],
  "blind_spots": ["<string>"],
  "errors": ["<string>"]
}
```

### Compose-mode-only fields (`--compose`, additive)

When `--compose` is set, the collector emits these ADDITIONAL fields on top of everything in (A) above. They are ABSENT (not present-and-empty) on a non-compose run, so a consumer can tell "not measured" from "measured, zero." All are additive — `schema_version` stays `1`, no existing consumer shape changed.

```jsonc
{
  "inspected_roots": {"operator": "<abs path>", "project_containment": "<abs path|null>",
                       "project_harness": "<abs path|null>"},
  "out_of_root_refs": [{"name": "rel", "target": "<raw readlink() string, else best-effort realpath>",
                        "trusted": false}],
  "tier_composition": {
    "nodes": [{"surface": "skill|command|agent|rule|claude_md|hook", "name": "", "tier": "operator|project",
               "path": "rel" /* repo-relative for file-backed surfaces; an inline hook with no
                    script file carries its raw command string here instead */, "status": "effective|shadowed",
               "shadowed_by": {"tier": "operator|project", "path": "rel"} /* | null */}],
    /* shadow surfaces: skill, command (operator wins), agent (project wins).
       union surfaces (never shadowed/dark): rule, claude_md, hook — every project entry is an add.
       node `tier` is ALWAYS the binary operator|project (settings-derived nodes normalize
       the 3-way user/project/local tier: user->operator, project/local->project). */
    "surfaces": {"<surface>": {"merge": "union|shadow", "winner_tier": "operator|project|null",
                                "adds": 0, "overrides": 0, "dark": 0}},
    "participating_surfaces": ["agent", "claude_md", "command", "hook", "rule", "skill"]
  },
  "composed_settings": {
    "permissions": {"allow_count": 0, "deny_count": 0, "ask_count": 0, "evidence": "VERIFIED|INACCESSIBLE"},
    "hooks": [{"event": "", "matcher": "<raw settings value — typically a str, null when the hook
                    omits its matcher; reported verbatim since this is a faithful read-only mirror
                    of settings.json, not a normalizer>", "command": "", "script": "rel|null",
               "exists": true /* |false|null */, "tier": "user|project|local", "source_file": "<abs path|null>"}],
    "overrides": [{"key": "", "winning_tier": "user|project|local",
                   "winning_value": "<scalar | [env-key names] | null>" /* null when the winning value is
                       non-scalar or an oversized string — the raw value is NEVER emitted (secret-safe) */,
                   "value_kind": "complex|redacted" /* present ONLY when winning_value is null:
                       "complex" = non-scalar (dict/list) value hidden; "redacted" = oversized string hidden */,
                   "overridden_tiers": ["user|project|local", "..."]}],
    "mcp": [{"name": "", "tier": "user|project|local", "source_file": "<abs path|null>", "type": "<str|null>",
             "enabled": true, "env_keys": ["<NAME only>"], "header_keys": ["<NAME only>"]}]
  }
}
```

These compose-only additions live INSIDE existing (A) arrays rather than as new top-level keys: every `always_loaded.files[]` entry AND every `conditional_variants[]` entry gains `tier` (`"operator"|"project"`), and every `duplication.pairs[]` entry gains `a_tier`/`b_tier` (`"operator"|"project"`) once the corpus runs cross-tier.

- **`inspected_roots`** — names the three roots a compose run walked (the plan's "Three roots": operator-scan-root, project-containment-root, project-harness-root = `project_containment/.claude`) — today's plain `doc["root"]` is operator-scan-root only, so this is what makes a compose run's full input surface explicit.
- **`out_of_root_refs`** — every project-tier path (file OR dir) whose symlink realpath escapes `project_containment` — NOT read, traversed, or excerpted; `trusted` is always `false` (the field exists so a future trusted-target case wouldn't need a shape change). Consumed by weight-honesty accounting (below) and by render's containment note.
- **`tier_composition.nodes`** — the canonical tier-tagged node model for all six composed surfaces (skills/commands/agents/rules **plus** CLAUDE-files (`claude_md`) and hooks (`hook`)), one entry per (surface, tier) after per-surface shadow resolution; sorted `(path, tier)` for determinism. `tier` is ALWAYS the binary `"operator"|"project"` — settings-derived nodes (hooks) normalize the 3-way settings tier (`user`→`operator`, `project`/`local`→`project`) so a Local-tier project hook still counts as a project entry. `path` is repo-relative for file-backed surfaces; for a hook node with NO backing script file (an inline `command:` like `"echo local-only"`) `path` carries the command string instead. `status:"shadowed"` + a non-null `shadowed_by` marks a node that lost its surface's collision — a `tier:"project"` shadowed node is a **dark project skill/command** (only the shadow surfaces skills/commands can go dark; union surfaces never do), defined in the repo but never runs while the operator's (or, for agents, the user's) version wins.
- **`tier_composition.surfaces`** — one rollup per participating surface: `merge` distinguishes **UNION** (CLAUDE.md/rules/hooks — both tiers load, `winner_tier: null`, every project entry is an `adds`, never `overrides`/`dark`) from **SHADOW** (skills/commands/agents — one winner per collision; `adds` = project entry with no operator collision, `overrides` = project won the collision, `dark` = project lost it). `winner_tier` names which tier wins a SHADOW surface's collision (`"operator"` for skills/commands, `"project"` for agents).
- **`tier_composition.participating_surfaces`** — the alphabetically-sorted surface list the resolver covers (`agent`, `claude_md`, `command`, `hook`, `rule`, `skill`); render iterates this list rather than guessing which surfaces exist.
- **`composed_settings`** — the settings/hooks/MCP compose chain, precedence **Local > Project > User** throughout. `permissions` UNIONS allow/deny/ask across all three tiers with deny winning any same-rule conflict (the one settings.json key that merges rather than overrides). `hooks` is a UNION (every matching hook fires regardless of tier) — each record keeps its own `tier` + `source_file` so a duplicate-looking hook is traceable to the file that registered it; `exists` is `null` when a project/local script's containment could not be verified (never silently treated as present or absent). `overrides` reports non-permission `settings.json` scalar-key winners through a small non-secret allowlist (`model`, `cleanupPeriodDays`, `sandbox`, `enabledPlugins`) plus an `env`-key-name-only special case — NEVER folded into `tier_composition`'s node adds/overrides counts, since this is scalar-key overriding, not node-surface shadowing. `mcp` lists server registrations from the three exact static projections (user `~/.claude.json:mcpServers`, local `~/.claude.json:projects[<repo>].mcpServers`, project `<repo>/.mcp.json:mcpServers`), SECRET-SAFE: `env_keys`/`header_keys` are names only, `command`/`url`/`args` are omitted entirely (a CLI arg list can legally carry an inline secret flag).
- **`always_loaded.totals.excluded_count`** — compose-only weight-honesty count: out-of-root + inaccessible entries that would otherwise have contributed to always-loaded weight, captured as a delta around `walk_always_loaded` so nothing from later scans (duplication, node model, MCP) leaks in. Its presence (vs. absence) distinguishes "0 excluded, measured" from "not measured at all" — do not read an absent field as zero.
- **`duplication.pairs[].a_tier` / `.b_tier`** — present only when `--compose` is set (duplication then runs across both tiers combined, per M4 — an operator rule duplicated by a project file is itself a signal). Tag order matches `a`/`b`, which are already sorted lexicographically as a pair, independent of tier.
- **Two DISTINCT tier vocabularies — do not conflate them.** Node-level tags (`tier_composition.nodes[].tier`, `always_loaded.files[].tier`, `duplication.pairs[].a_tier`/`.b_tier`, `out_of_root_refs` implicitly) are the BINARY `"operator"|"project"` model tag. `composed_settings`'s `tier` fields (hooks, overrides, mcp) use a SEPARATE 3-way `"user"|"project"|"local"` vocabulary, because settings/MCP have a real Local-vs-Project distinction (`settings.local.json` vs `settings.json`, and `.claude.json`'s per-project `Local` entry vs `.mcp.json`'s `Project` entry) that skills/agents/commands don't — this is intentional, not an inconsistency.

### Field notes

- **`headline`** — an eight-number rollup used as the diff unit between runs (see Note 3 below). Every field is a plain count, computed from the other sections.
- **`always_loaded`** — everything paid for on every conversation turn regardless of whether the skill/rule is invoked: root and project `CLAUDE.md` files, memory files, `rules/*.md`, coding-team rules, plus the *description* text of every skill and agent (their bodies are NOT always-loaded — only the frontmatter description shown in the picker). `conditional_variants` covers per-project `CLAUDE.md` variants that load only when that project is the cwd. The rule scan is generalized to `skills/*/rules/*.md` (any sub-skill's rules dir), scanned AFTER `rules/*.md` so a rule reachable via both a `rules/` deploy symlink and a sub-skill source is deduped by physical identity and counted once under `rules/` (category `rule`). A sub-skill's own rule files carry category `coding_team_rule` when the sub-skill is `coding-team` (retained for baseline continuity) and `skill_rule` for every other sub-skill. Hook test detection is likewise generalized to `hooks/tests` + `skills/*/hooks/tests`.
- **`on_demand`** — content that loads only when a skill/agent is actually invoked: skill `SKILL.md` files, their internal `phases/`, `prompts/`, `agents/` bodies, and memory file bodies (as opposed to the memory index entry, which is always-loaded).
- **`enforcement.hooks`** — see Note 3 (registration vs target status) below; this is the section that distinction governs.
- **`enforcement.hooks.scripts_on_disk[].description`** — a one-line, read-only summary auto-extracted from the script header (precedence: `# summary:` marker > `.py` module docstring first line > first leading `#` comment > `""`). Extracted verbatim as DATA (never executed/followed); empty when no header is present. Additive optional field — `schema_version` stays `1`; readers tolerate its absence via `.get('description', '')`.
- **`config`** — a snapshot of `settings.json` / `.claude.json`-level configuration. `env_keys` is names only (see Note 2). `evidence` at the `config` level covers whether `settings.json` itself was readable.
- **`instruction_length_flags`** — any instruction file (SKILL.md, phase, prompt, agent, rule) whose line count exceeds `threshold` (200).
- **`duplication`** — near-duplicate content pairs across instruction files. See Note 2 for the metric and Note 3 for determinism of the output ordering.
- **`phantom_refs`** — a reference (file path, env-flag name, external URL/tool, or slash command) named in an instruction file that does not resolve to a real, checkable target. `kind: "slash_command"` (S2.M4, additive — no `schema_version` bump) is a RECLASSIFICATION of the existing `external` branch for a `/token` matching `^/[a-z0-9][a-z0-9-]*(?::[a-z0-9][a-z0-9-]*)*$` (bare `/name` or namespaced `/ns:name`, `/ns:sub:name`). The collector checks the homes it can see under `--root` — `commands/<ns>/…/<name>.md` (a bare `/name` yields `commands/<name>.md`) and `skills/<seg0>/SKILL.md` — via the same tri-state `_safe_exists` used elsewhere: present at either home means the token is dropped silently; inaccessible at either home is recorded in `inaccessible` and the token is dropped (inaccessible is not retired); absent-and-readable at every home emits the row with **`resolved: null`, `evidence: "INFERRED"`**. It is NOT `VERIFIED`/`false`: a `/token`'s real resolution space also includes Claude Code BUILT-INS, plugin commands, and project-tier commands, all of which are structurally unenumerable from `--root`. The row therefore means "no home under the scanned root", never "this command no longer exists". A multi-segment or dotted `/path` (e.g. `/usr/bin/python3`) stays `external` unchanged.
- **`promotion_candidates`** — prose in an instruction file that reads like a hard rule (`NEVER`, `ALWAYS`, `must`, a numeric cap, a required-file assertion) but has no corresponding hook enforcing it. `hook_covered` is `true` only when the collector matched it to an entry in `enforcement.hooks.registered`.
- **`test_coverage`** — whether each hook script and each skill has an associated test, and the aggregate summary counts.
- **`staleness`** (S2.M3, additive — no `schema_version` bump) — a raw git-age SIGNAL only; the collector never classifies a file as "stale." `git_age_available` comes from the run's single git-topology discovery (`build_git_repo_index`, S2 gate fix): `true` only when `--root` has a confirmed work-tree toplevel (`git rev-parse --show-toplevel`, run per repo root) that is itself contained by `--root`. That probe is STRICTER than the `--is-inside-work-tree` check it replaces, and the narrowing is deliberate: a BARE repo exits 0 printing `false` for `--is-inside-work-tree` but exits non-zero for `--show-toplevel`, so a bare repo now reads as unavailable where it previously read as available. The flag is therefore `false` when git is absent, `--root` is not a work tree, `--root` is a BARE repo, the probe errors, or the reported toplevel lies OUTSIDE `--root` (the case where `--root` is not a repo but sits inside an enclosing one — the enclosing repository is never probed, and the refusal is recorded as a blind spot) — in that case every `last_commit_ts` value is `null` and the per-file `git log` loop is skipped entirely. The topology is discovered once and the per-file lookups are pure with respect to it, so this flag can never disagree with the timestamps it labels. `last_commit_ts` maps every deduped instruction-file rel-path (the same corpus `instruction_length_flags` walks — `rules/*.md`, `skills/*/rules/*.md`, `skills/*/SKILL.md`, `skills/*/*/SKILL.md`, `skills/*/phases/*.md`, `skills/*/prompts/*.md`, `skills/*/agents/*.md`, `commands/*.md`, `agents/*.md`) to its last commit's unix timestamp (`git log -1 --format=%ct -- <path>`), or `null` when it cannot be honestly determined. A `null` is never a guess and never an implied "very old": every one of them carries its cause in the sibling `staleness_null_reasons` map, whose ten closed values are listed in the next bullet. A path is looked up in that work tree's INDEX (`git ls-files`) before its history is read, because `git log` answers from history rather than tracked state — a file deleted in one commit and recreated untracked still has a commit, and reporting it would be a stale lie rather than a null. The whole git-age subsystem also runs under ONE total wall-clock budget of 10s (typical measured run: 2.24s) covering discovery and every per-file read; files not yet probed when it expires are `null` with reason `budget_exhausted` and are never probed, and a blind spot names how many. The scanned root's own availability probe is the single exemption from that budget, so exhaustion can never flip `git_age_available` — a budget running out is not evidence that the root is not a work tree. That probe is itself two subprocesses (`_git_toplevel`, then `_git_common_dir` via `_toplevel_refusal`), each capped at 2s, so its exempt window is up to 4s; add the 10s total budget and one already-in-flight per-file subprocess left to finish its own cap (up to 5s, the batched `git ls-files` timeout) once the deadline fires, and the documented worst case for the whole git-age subsystem is **~19s** — replacing the previous UNBOUNDED 230-260s worst case. 10s is a budget on new work, not a hard ceiling on the subsystem's total wall time. Each `git log` is run against the file's PHYSICAL path (symlinks resolved first) in the work tree that actually owns it, so a file reached through a deploy symlink reports its TARGET's commit and a file inside a submodule reports the submodule's own commit rather than `null`; a submodule work tree is trusted only when the parent index names it as a mode-160000 gitlink AND the git directory backing it (`git rev-parse --git-common-dir`) also resolves inside `--root` — naming the path is not proof that the `.git` sitting there belongs to that parent, since a gitfile in the named subtree can point at an unrelated repository — and a path resolving outside `--root` is never probed. Every refusal, under any of those clauses, is recorded as a blind spot naming the directory and the reason rather than emitting a silent `null`. Keys are unchanged by that resolution — the logical root-relative path stays the key — and are sorted lexicographically for deterministic output. NEVER derived from filesystem mtime — mtime lies after a copy or checkout. Consuming the watcher blind spot: `.git` is a pruned walk dir, so a new commit alone (with no accompanying file edit) changes this field with no watched filesystem signal — see `iter_input_paths`'s docstring.
- **`staleness_null_reasons`** (S2 gate fix, additive — no `schema_version` bump) — why each `null` in `staleness.last_commit_ts` is null. A SIBLING of `staleness`, not nested inside it, so a consumer pinning `staleness`'s exact shape is unaffected. TOTAL INVARIANT: this map has an entry for EXACTLY those `last_commit_ts` keys whose value is `null` — never for a key carrying a timestamp, never for a key absent from `last_commit_ts`. Keys are sorted lexicographically, same order as `last_commit_ts`. Values are a CLOSED enum of ten strings — git's own error text is never surfaced here, because it carries absolute paths and `.gitmodules`/`.git/config` values (an HTTPS-with-token submodule URL would put credentials in a published HTML document), and because a free-text value reaching the renderer is the class-injection class already closed elsewhere. Variable text goes to `errors[]`/`inaccessible[]` instead. The ten values:
  - `git_unavailable` — the git **binary could not be executed** (absent, or not runnable). This is NOT "not a repo": it means no git command ran at all.
  - `no_repo` — git ran fine and reported no enclosing work tree. Includes a bare repo, and a `--root` that is simply not a work tree.
  - `outside_root` — the work tree that would answer for this path resolves outside `--root` (or its backing git directory does), so it was refused rather than probed. Also recorded as a blind spot.
  - `untracked` — the file is not in its work tree's index. Its history may still hold commits (deleted-then-recreated); reporting them would be a stale lie.
  - `submodule_unavailable` — the path is, or lives under, a mode-160000 gitlink whose work tree could not be read (typically a deinitialized submodule). The parent's `ls-files` never descends into a submodule, so absence from the parent index says nothing about the file itself — this is why it is not `untracked`.
  - `timeout` — this path's git call exceeded the per-call cap (2s single-path, 5s batched).
  - `budget_exhausted` — the 10s TOTAL budget expired before this file was reached. The file was never measured; re-run, or raise the budget. NOT a statement about the file.
  - `git_error` — git could not answer: a non-zero exit (a zero-commit repo exits 128), an OS error, or an index/topology state that could not be determined. An unknown is reported here rather than as the definitive negative `untracked` or `no_repo`.
  - `unparseable` — git exited 0 with stdout that was not an integer.
  - `no_commits` — the path is tracked but has no commit yet (staged only): `git log` exits 0 with empty stdout. Distinct from `unparseable` on purpose, since "stdout was not an integer" would be a misleading description of an empty one.
- **`inaccessible`** — every path anywhere in the run that the collector attempted to read and could not, with the OS-level `reason`.
- **`blind_spots`** — free-text notes on categories of content the collector structurally cannot see (e.g., runtime-only MCP server instructions, plugin-marketplace content not vendored locally). In `--compose` mode one additional entry discloses that the per-file hygiene analyses (`instruction_length_flags`, staleness, `phantom_refs`, `promotion_candidates`, `test_coverage`, and the hooks-body duplication corpus) scan the **operator tier only** — project-tier files are NOT covered by them in v1, so a "0 project length flags" reading means "not scanned," not "clean." Full per-tier hygiene is deferred to v1.1.
- **`errors`** — any collector-internal error encountered mid-run; a non-fatal error is recorded here and the run continues.

## (B) Synthesis report structures

The synthesis pass is a model pass over (A)'s output. It produces judgments — the collector never does.

- **CIVC matrix cell** — a table, rows = verbs `[Afford, Inform, Constrain, Verify, Correct, Evolve]`, columns = surfaces `[context, tools, memory, permissions, orchestration, observability]`, each cell ∈ `{covered, thin, empty}`. Produced by the model from `always_loaded`, `enforcement`, `on_demand`, and `test_coverage`.
- **Drag-candidate record** — `{ n, surface, evidence (V/I/IA), outcome ∈ {keep, give it one home, load it later, turn it into a check, probation, retire safely}, what_must_survive, risk_if_wrong }`. Per D8, `retire safely` is disallowed in v1 (no-usage) runs — cap the outcome at `probation`.
- **Diff snapshot** — the `headline` block IS the diff unit. Synthesis compares the current run's `headline` against the most-recent prior sidecar's `headline`, field by field.

## (C) Synthesis sidecar file contract

The synthesis pass (B) is not just a report — it also writes a machine-readable sidecar, `harness-synthesis-<date>.json`, into `OUT_DIR` (the report directory, OUTSIDE `--root`) alongside that run's `.md` report. The `<date>` must match the report's own date so `render_html.py` can pair them. `render_html.py` (`load_synthesis`/`load_sidecar`) simply `json.loads()`s this file: absent file degrades gracefully to an empty-state (`available: False` in the CIVC and drag-candidate view models); a present-but-invalid file is an explicit "unavailable" state — it is never silently substituted with defaults.

### Shape

```jsonc
{
  "schema_version": 1,
  "civc": [
    {"verb": "<VERB>", "surface": "<surface>", "verdict": "<verdict>",
     "evidence": "VERIFIED|INFERRED|INACCESSIBLE", "note": "<prose>"}
    // ... all 36 verb×surface cells ...
  ],
  "drag_candidates": [
    {"n": 0, "surface": "<surface>", "evidence": "V|I|IA",
     "outcome": "keep|give it one home|load it later|turn it into a check|probation",
     "what_must_survive": "<prose>", "risk_if_wrong": "<prose>"}
  ]
}
```

`schema_version` is REQUIRED — `render_html.py`'s `load_sidecar` (the same loader used for the collector sidecar) rejects a synthesis sidecar missing it (`"missing schema_version"` → Coverage matrix renders unavailable); keep it in sync with the collector's `schema_version` in (A).

### Enums (verbatim from `render_html.py:55-57`)

- `VERBS = ("Afford", "Inform", "Constrain", "Verify", "Correct", "Evolve")` — **TitleCase**
- `SURFACES = ("context", "tools", "memory", "permissions", "orchestration", "observability")` — **lowercase**
- `VERDICTS = ("covered", "thin", "empty")` — **lowercase**

**CASING WARNING:** `build_civc_model` (`render_html.py:413-436`) allowlists a `civc` cell on EXACT membership — `c["verb"] in VERBS and c["surface"] in SURFACES` — before it is ever placed in the grid; `verdict` is separately re-checked against `VERDICTS` and falls back to `"empty"` if not a match. This is the ONE normalization point in the render path. A casing mismatch (`"Context"`, `"afford"`, `"Covered"`) does not error and does not get coerced — the cell is silently dropped, and the corresponding grid cell renders as the same visual "empty" as a genuinely-absent judgment. There is no warning surfaced to the operator when this happens; get the casing right at write time.

### Completeness

`civc` should carry all 36 verb×surface cells (6 verbs × 6 surfaces). A missing cell degrades to `verdict: "empty"` with no error — the same failure mode as a casing mismatch — so the sidecar-writer must emit the full skeleton to make gaps in coverage *intentional* (a real "empty" judgment) rather than *accidental* (an omitted cell that merely looks like one). `synthesis-template.json` in this skill directory is that full 36-cell skeleton, ready to fill in.

### D8 constraint

Per Note (B) above, `retire safely` is disallowed in v1 (no-usage) runs — cap `outcome` at `probation` for any drag candidate that would otherwise warrant retirement.

## Notes

1. **Signals vs. judgments.** The collector emits SIGNALS (A) — it counts, reads, and classifies mechanically (file categories, evidence labels, line thresholds). The model produces JUDGMENTS (B) — CIVC classification, drag outcomes, "give it one home" decisions. The collector never classifies a verb coverage or condemns a duplicate pair as dead weight; it only reports that the pair exists above threshold.

2. **Secret safety and the duplication metric.** `config.env_keys` lists env KEY NAMES only — the collector NEVER emits env VALUES (the real `env` holds `GITHUB_TOKEN` and other secrets). `duplication.metric` is `"containment"` (`|A∩B| / min(|A|,|B|)`), not Jaccard — containment is chosen because it correctly flags a short file fully subsumed by a longer one, which Jaccard would under-score. The shingle size constant `SHINGLE_K = 8` (also `duplication.shingle_k` in the output) sets the k-gram window used to build each file's shingle set before comparison. **Scope boundary:** this guard applies to `env` VALUES specifically — hook `command` strings ARE surfaced verbatim in `enforcement.hooks.registered[].command` and (truncated) in `blind_spots`, by design, since reconciliation requires showing the registered command for a human to audit an orphan. The harness convention is that secrets live in `env`, not inline in hook commands; this boundary is accepted, not a gap.

3. **Registration vs. target status — no exception.** A hook registration finding SEPARATES two facts and never collapses them. `registration_evidence: "VERIFIED"` means the collector READ the registration line in `settings.json` — that fact alone is always knowable. The registered TARGET's status is reported independently, by `stat()`-ing the path: a `FileNotFoundError` on the target routes the entry to `enforcement.hooks.orphan_registrations[]` with `target_status: "missing"` — a real orphan. A `PermissionError` on the target routes the entry to `inaccessible[]` with `evidence: "INACCESSIBLE"` — a permission-denied target is NOT an orphan and must never be condemned as one, because the collector could not actually see it. Registration evidence and target status are always distinct fields; nothing sets one from the other. `duplication` output is deterministic across runs: pairs are sorted by `(-score, a, b)`, and `shared_sample` is the lexicographically-smallest shared shingle between the pair.

4. **`headline.unchecked_binary_count` is reserved, always `0` in v1.** No binary scan is performed — the walk reads only `.md`/`.py`/`.sh` via `errors='replace'` (text mode). This `0` reflects "not inspected," never "no binaries found" — do NOT read it as a clean bill of health.
