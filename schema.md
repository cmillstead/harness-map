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
  "metric_definitions": {"<metric name>": 1},
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

- **`headline`** — an eight-number rollup used as the diff unit between runs (see Note 3 below). Every field is a plain count, computed from the other sections. Every count is NON-NEGATIVE by domain, and readers must treat an ABSENT key as *not measured* rather than as `0`: `render_html.py` renders a missing key as the same `—` the trend table uses (never `0`, never a `CLEAN`/`COMPLIANT`/`LEAN` band), and a negative value is banded as no-verdict neutral rather than banding clean. A sidecar that is a collector CRASH ENVELOPE (all-zero headline plus a `collector crashed: …` entry in `errors[]`) is not a measurement at all: it is excluded from the trend series AND from sidecar selection, so it can never be rendered as the current run — with `--out-dir` alone the renderer falls back to the newest *measured* sidecar and discloses the skip in the provenance footer, and with an explicit `--date` naming one it fails rather than substituting a date the operator did not ask for.
- **`always_loaded`** — everything paid for on every conversation turn regardless of whether the skill/rule is invoked: root and project `CLAUDE.md` files, memory files, `rules/*.md`, coding-team rules, plus the *description* text of every skill and agent (their bodies are NOT always-loaded — only the frontmatter description shown in the picker). `conditional_variants` covers per-project `CLAUDE.md` variants that load only when that project is the cwd. The rule scan is generalized to `skills/*/rules/*.md` (any sub-skill's rules dir), scanned AFTER `rules/*.md` so a rule reachable via both a `rules/` deploy symlink and a sub-skill source is deduped by physical identity and counted once under `rules/` (category `rule`). A sub-skill's own rule files carry category `coding_team_rule` when the sub-skill is `coding-team` (retained for baseline continuity) and `skill_rule` for every other sub-skill. Hook test detection is likewise generalized to `hooks/tests` + `skills/*/hooks/tests`.
- **`on_demand`** — content that loads only when a skill/agent is actually invoked: skill `SKILL.md` files, their internal `phases/`, `prompts/`, `agents/` bodies, and memory file bodies (as opposed to the memory index entry, which is always-loaded).
- **`enforcement.hooks`** — see Note 3 (registration vs target status) below; this is the section that distinction governs.
- **`enforcement.hooks.scripts_on_disk[].description`** — a one-line, read-only summary auto-extracted from the script header (precedence: `# summary:` marker > `.py` module docstring first line > first leading `#` comment > `""`). Extracted verbatim as DATA (never executed/followed); empty when no header is present. Additive optional field — `schema_version` stays `1`; readers tolerate its absence via `.get('description', '')`.
- **`config`** — a snapshot of `settings.json` / `.claude.json`-level configuration. `env_keys` is names only (see Note 2). `evidence` at the `config` level covers whether `settings.json` itself was readable.
- **`instruction_length_flags`** — any instruction file (SKILL.md, phase, prompt, agent, rule) whose line count exceeds `threshold` (200).
- **`duplication`** — near-duplicate content pairs across instruction files. See Note 2 for the metric and Note 3 for determinism of the output ordering.
- **`phantom_refs`** — a reference (file path, env-flag name, external URL/tool, or slash command) named in an instruction file that does not resolve to a real, checkable target. `kind: "slash_command"` (S2.M4, additive — no `schema_version` bump) is a RECLASSIFICATION of the existing `external` branch for a `/token` matching `^/[a-z0-9][a-z0-9-]*(?::[a-z0-9][a-z0-9-]*)*$` (bare `/name` or namespaced `/ns:name`, `/ns:sub:name`). The collector checks the homes it can see under `--root` — `commands/<ns>/…/<name>.md` (a bare `/name` yields `commands/<name>.md`) and `skills/<seg0>/SKILL.md` — via the same tri-state `_safe_exists` used elsewhere: present at either home means the token is dropped silently; inaccessible at either home is recorded in `inaccessible` and the token is dropped (inaccessible is not retired); absent-and-readable at every home emits the row with **`resolved: null`, `evidence: "INFERRED"`**. It is NOT `VERIFIED`/`false`: a `/token`'s real resolution space also includes Claude Code BUILT-INS, plugin commands, and project-tier commands, all of which are structurally unenumerable from `--root`. The row therefore means "no home under the scanned root", never "this command no longer exists". A multi-segment or dotted `/path` (e.g. `/usr/bin/python3`) stays `external` unchanged. A `kind: "env_flag"` row's `resolved: false` is a CONFIRMED negative — no hook body anywhere references the flag — and its only evidence is the concatenated `hooks/*.py` + `hooks/*.sh` corpus (`_hooks_body_corpus`, pre-flight exit gate). When that corpus is INCOMPLETE (a hook body could not be read, or the `hooks/` directory itself could not be listed), the negative is unprovable, so EVERY env_flag row for the run — not just the one whose file was unreadable — is downgraded to **`resolved: null`, `evidence: "INFERRED"`**: confidence is a property of the corpus, not of one row, since once any body is unseen no flag's absence from the concatenated blob is provable. An ABSENT `hooks/` directory does NOT trigger this downgrade: a harness with no hooks has a known-empty corpus, which is a fact, not a blind spot, so its env_flag rows still get a confirmed `resolved: false` when genuinely unreferenced.
- **`promotion_candidates`** — prose in an instruction file that reads like a hard rule (`NEVER`, `ALWAYS`, `must`, a numeric cap, a required-file assertion) but has no corresponding hook enforcing it. `hook_covered` is `true` only when the collector matched it to an entry in `enforcement.hooks.registered`.
- **`test_coverage`** — whether each hook script and each skill has an associated test, and the aggregate summary counts.
- **`staleness`** (S2.M3, additive — no `schema_version` bump) — a raw git-age SIGNAL only; the collector never classifies a file as "stale." `git_age_available` comes from the run's single git-topology discovery (`build_git_repo_index`, S2 gate fix): `true` only when `--root` has a confirmed work-tree toplevel (`git rev-parse --show-toplevel`, run per repo root) that is itself contained by `--root`. That probe is STRICTER than the `--is-inside-work-tree` check it replaces, and the narrowing is deliberate: a BARE repo exits 0 printing `false` for `--is-inside-work-tree` but exits non-zero for `--show-toplevel`, so a bare repo now reads as unavailable where it previously read as available. The flag is therefore `false` when git is absent, `--root` is not a work tree, `--root` is a BARE repo, the probe errors, or the reported toplevel lies OUTSIDE `--root` (the case where `--root` is not a repo but sits inside an enclosing one — the enclosing repository is never probed, and the refusal is recorded as a blind spot) — in that case every `last_commit_ts` value is `null` and the per-file `git log` loop is skipped entirely. The topology is discovered once and the per-file lookups are pure with respect to it, so this flag can never disagree with the timestamps it labels. `last_commit_ts` maps every deduped instruction-file rel-path (the same corpus `instruction_length_flags` walks — `rules/*.md`, `skills/*/rules/*.md`, `skills/*/SKILL.md`, `skills/*/*/SKILL.md`, `skills/*/phases/*.md`, `skills/*/prompts/*.md`, `skills/*/agents/*.md`, `commands/*.md`, `agents/*.md`) to its last commit's unix timestamp (`git log -1 --format=%ct -- <path>`), or `null` when it cannot be honestly determined. A `null` is never a guess and never an implied "very old": every one of them carries its cause in the sibling `staleness_null_reasons` map, whose ten closed values are listed in the next bullet. A path is looked up in that work tree's INDEX (`git ls-files`) before its history is read, because `git log` answers from history rather than tracked state — a file deleted in one commit and recreated untracked still has a commit, and reporting it would be a stale lie rather than a null. The whole git-age subsystem also runs under ONE total wall-clock budget of 10s (typical measured run: 2.24s) covering discovery and every per-file read; files not yet probed when it expires are `null` with reason `budget_exhausted` and are never probed, and a blind spot names how many. The scanned root's own availability probe is the single exemption from that budget, so exhaustion can never flip `git_age_available` — a budget running out is not evidence that the root is not a work tree. That probe is itself two subprocesses (`_git_toplevel`, then `_git_common_dir` via `_toplevel_refusal`), each capped at 2s, so its exempt window is up to 4s; add the 10s total budget, one already-in-flight per-file subprocess left to finish its own cap (up to 5s, the batched `git ls-files` timeout) once the deadline fires, and the submodule provenance probe that can start just before it (`git rev-parse --verify`, one more 2s cap), and the documented worst case for the whole git-age subsystem is **~21s** — replacing the previous UNBOUNDED 230-260s worst case. 10s is a budget on new work, not a hard ceiling on the subsystem's total wall time. Each `git log` is run against the file's PHYSICAL path (symlinks resolved first) in the work tree that actually owns it, so a file reached through a deploy symlink reports its TARGET's commit and a file inside a submodule reports the submodule's own commit rather than `null`; a submodule work tree is trusted only when the parent index names it as a mode-160000 gitlink AND the git directory backing it (`git rev-parse --git-common-dir`) also resolves inside `--root` AND that git directory is one the vouching parent accounts for — naming the path is not proof that the `.git` sitting there belongs to that parent, since a gitfile in the named subtree can point at an unrelated repository, and root-containment alone does not close that because the unrelated repository can sit INSIDE `--root` too. The third clause is the only one derived from the parent's INDEX rather than from filesystem state (which an attacker who can write in the subtree controls): the git directory must live either inside the gitlinked path itself or inside the parent's own git directory, AND the repository there must contain the exact commit the parent's mode-160000 entry records for that path. A path resolving outside `--root` is never probed. Every refusal, under any of those clauses, is recorded as a blind spot naming the directory and the reason rather than emitting a silent `null`. Keys are unchanged by that resolution — the logical root-relative path stays the key — and are sorted lexicographically for deterministic output. NEVER derived from filesystem mtime — mtime lies after a copy or checkout. Consuming the watcher blind spot: `.git` is a pruned walk dir, so a new commit alone (with no accompanying file edit) changes this field with no watched filesystem signal — see `iter_input_paths`'s docstring.
- **`staleness_null_reasons`** (S2 gate fix, additive — no `schema_version` bump) — why each `null` in `staleness.last_commit_ts` is null. A SIBLING of `staleness`, not nested inside it, so a consumer pinning `staleness`'s exact shape is unaffected. TOTAL INVARIANT: this map has an entry for EXACTLY those `last_commit_ts` keys whose value is `null` — never for a key carrying a timestamp, never for a key absent from `last_commit_ts`. Keys are sorted lexicographically, same order as `last_commit_ts`. Values are a CLOSED enum of ten strings — git's own error text is never surfaced here, because it carries absolute paths and `.gitmodules`/`.git/config` values (an HTTPS-with-token submodule URL would put credentials in a published HTML document), and because a free-text value reaching the renderer is the class-injection class already closed elsewhere. Variable text goes to `errors[]`/`inaccessible[]` instead. The ten values:
  - `git_unavailable` — the git **binary could not be executed** (absent, or not runnable). This is NOT "not a repo": it means no git command ran at all.
  - `no_repo` — git ran fine and reported no enclosing work tree, AND no usable repository is sitting there either (`git rev-parse --resolve-git-dir` also declines). Includes a bare repo, and a `--root` that is simply not a work tree. It does NOT include the case where git REFUSES a repository that is plainly present — dubious ownership (`safe.directory`) and unreadable git metadata both exit non-zero on `--show-toplevel` while the git directory resolves fine, and those are `git_error`, because "this is not a work tree" is a positive claim nobody established.
  - `outside_root` — the work tree that would answer for this path resolves outside `--root`, or its backing git directory does, or that git directory is not the one the parent repository's index vouches for (see the submodule clauses above), so it was refused rather than probed. Reserved for refusals that were DETERMINED: a git directory that could not be resolved at all is `git_error`. Also recorded as a blind spot.
  - `untracked` — the file is not in its work tree's index. Its history may still hold commits (deleted-then-recreated); reporting them would be a stale lie.
  - `submodule_unavailable` — the path is, or lives under, a mode-160000 gitlink whose work tree could not be read (typically a deinitialized submodule). The parent's `ls-files` never descends into a submodule, so absence from the parent index says nothing about the file itself — this is why it is not `untracked`.
  - `timeout` — this path's git call exceeded the per-call cap (2s single-path, 5s batched).
  - `budget_exhausted` — the 10s TOTAL budget expired before this file was reached. The file was never measured; re-run, or raise the budget. NOT a statement about the file.
  - `git_error` — git could not answer: a non-zero exit (a zero-commit repo exits 128), an OS error, or an index/topology state that could not be determined. This also covers git REFUSING a repository that is present (dubious ownership, unreadable git metadata), a work tree whose backing git directory could not be resolved, and a submodule whose provenance could not be checked. An unknown is reported here rather than as the definitive negative `untracked`, `no_repo`, or `outside_root`.
  - `unparseable` — git exited 0 with stdout that was not an integer.
  - `no_commits` — the path is tracked but has no commit yet (staged only): `git log` exits 0 with empty stdout. Distinct from `unparseable` on purpose, since "stdout was not an integer" would be a misleading description of an empty one.
- **`metric_definitions`** (S6b, additive — no `schema_version` bump) — a map from metric name to an integer DEFINITION VERSION. It carries no values; it says *how a metric was computed on this run*, so a consumer comparing two sidecars can distinguish "the world changed" from "we changed how we measure". A metric's integer is bumped in the SAME change as the edit to the code that computes it. 14 metrics are declared: the eight `headline` keys plus the six renderer-derived ones (`promotion_candidate_count`, `memory_body_count`, `hooks_with_test_ratio`, `skills_with_test_ratio`, `phantom_ref_count`, `phantom_confirmed_count`). `phantom_confirmed_count` is derived by the RENDERER and has no collector value, but it shares `phantom_ref_count`'s detector and therefore its version — two views, one definition. Derived metrics inherit the collector definition version of their underlying data; there is deliberately no separate renderer-derivation version, because it would be identical across every sidecar in a window and so could never detect anything. A consumer reading a version MUST validate it as `isinstance(v, int) and not isinstance(v, bool) and v > 0` — `True == 1` in Python, and a stray boolean silently resolving as version 1 would report a series comparable when it is not. Anything failing that check is UNKNOWN, never a default. On the crash-envelope path this field is present and EMPTY (`{}`): a run that measured nothing defines nothing.
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

## (D) Friction telemetry streams

`render_html.py` optionally reads four append-only JSONL streams and joins them onto the
rendered map nodes. These are RENDERER inputs, not collector inputs: `collector.iter_input_paths`
deliberately excludes them (it reaches the memory directory only via `*.md` globs, never
`*.jsonl`), so appending to a stream takes the cheap friction-only rebuild rather than a full
re-collect. The renderer never writes to any of them.

| Stream | Default path | Join |
|---|---|---|
| `decisions` | `~/.claude/harness-decisions.jsonl` | `component` → map node, via `_resolve_ref` |
| `metrics` | `~/.claude/harness-metrics.jsonl` | `phases_used` / `agents_dispatched` → coding-team phase/agent nodes |
| `interventions` | `~/.claude/projects/<slug>/memory/interventions.jsonl` | `memory_file` → map node, via `_resolve_ref` |
| `codex` | `~/.claude/harness-codex.jsonl` | none — aggregate only (`target` names a plan file, not a map node) |

`<slug>` is the CC per-project memory directory name: the harness root's absolute path with
every `/` and `.` replaced by `-`. It is DERIVED from `$HOME` at call time, never a literal.
The interventions default is offered **only when the selected scan root IS the harness root**,
and only when the memory DIRECTORY exists; any other root yields `null` for this stream, so a
foreign-root run can never ingest this harness's interventions log.

### Interventions record shape

```jsonc
{
  "timestamp": "2026-06-07T19:31:04.917406+00:00",  // ISO-8601; the ONLY date key this stream uses
  "session_id": "<uuid>",                            // never rendered
  "model": "<model id>",                             // optional, string — model id of the session that appended the row; "unknown" on backfilled rows and when not derivable (TRK-014, 2026-08-01)
  "memory_file": "feedback_proactive-solutions.md",  // BARE BASENAME, not a path
  "name": "<slug>", "description": "<prose>", "type": "feedback",
  "rule_summary": "<prose>", "rationale_snippet": "<prose>",
  "application_snippet": "<prose>",
  "related_memories": ["<wikilink target>", ...],    // NOT joined — see below
  "backfilled": true                                 // optional; present on reconstructed records
}
```

No record prose reaches the HTML: `joined[node_key]` is consumed only as `len(records)`.

**`related_memories` is not joined and is not part of the contract.** Measured: 79 wikilink
targets fan out to 67 additional events across 38 nodes; unioned with `memory_file` that is 115
events across 64 nodes from 48 records — 2.4× amplification. A one-to-many fan-out manufactures
events that never happened. It is an editorial cross-reference, not evidence of an occurrence.

**Attribution is INFERRED, never VERIFIED.** `memory_file` is a bare basename, which is not
stable identity over time: delete a file, let a different file with the same basename later
become the sole match, and every historical record silently reassigns to the new node. The
footer discloses this, and basename-attributed events must not enter consequential arithmetic
(they are excluded from M8's drag composite by construction — S6 §4.4 / AMENDMENTS A30).

### Which keys carry a record's date

`_record_date` reads, in this FIRST-MATCH-WINS order: `date`, `ts`, `verified_date`, `timestamp`.
`timestamp`'s TAIL position is load-bearing — any record resolving via an earlier key returns
before `timestamp` is consulted, which freezes the three pre-S6a streams by construction rather
than by their current key sets. The matched `YYYY-MM-DD` prefix is validated as a real calendar
date (`datetime.date.fromisoformat`); a structurally-shaped but calendar-invalid value such as
`2026-13-45` is treated as UNDATED and is never compared against the current date.

A record dated in the FUTURE is skipped entirely — no join, no heat — never merely excluded from
a count.

### Per-stream footer counters

Every stream's footer entry carries `stream`, `status`, `path_display`, and — when
`status == "loaded"` — `lines_nonblank`, `records_parsed`, `records_invalid`, and
`truncated_at_cap` (only if the read stopped at a cap).

**`records_invalid` and the byte-cap sentinel.** `records_invalid` counts genuinely
malformed lines (bad JSON, an oversized line, a non-dict value) as `read_jsonl` parses
them — but when the byte cap trips, `read_jsonl` ALSO counts the rejected overflow tail as
one additional synthetic malformed record: a parse-layer bookkeeping artifact marking
WHERE the read stopped, not a real invalid line. Left uncorrected, a stream whose every
line is syntactically valid but which merely exceeds `max_bytes` would render "≥1 invalid
lines" for zero real problems. The **metrics stream's rendered sentence** (the one surface
that narrates `records_invalid` as an English "N invalid lines" claim) subtracts that one
sentinel back out — `max(records_invalid - 1, 0)` when `"bytes"` is present in
`truncated_at_cap` — before display. This is a display-layer correction only: the raw
`records_invalid` counter in the footer dict, and the per-stream raw-counters `<details>`
dump on every stream (including metrics), still carries the uncorrected value, since that
surface is explicit raw internal bookkeeping rather than a narrated claim.

The date-provenance counters below are carried by the **three joined streams only**
(`decisions`, `metrics`, `interventions`). `codex` is aggregate-only: it never joins to a
node and reports `records_aggregate_only` instead. It applies the SAME date rules — a
calendar-invalid date is treated as undated and a future-dated record is skipped — it just
does not surface per-provenance counts.

<!-- `records_aggregate_only` is PRE-EXISTING, not introduced by S6a: set in the codex branch
     of `build_friction_overlay` and by `join_metrics` for the metrics stream; consumed by the
     friction sentence, the copy payload and the component table; pinned by existing
     assertions in tests/test_render_html.py. No S6a task implements it because none needs to.
     Verified 2026-07-31 — a plan reviewer read the sentence above as documenting an invented
     field, so this citation is here to stop the same conclusion being reached twice. -->

**`records_invalid_shape` (metrics stream only).** `join_metrics` consumes exactly two
attribution fields, `phases_used` (must be a list) and `agents_dispatched` (must be a
dict); either one present with the WRONG TYPE is silently skipped by the join with no
trace elsewhere (post-exec Codex finding, S6a) — a record can lose half or all of its
phase/agent attribution and nothing else in the footer says so. `records_invalid_shape`
counts, once per record, every eligible record where `phases_used` is present but not a
list OR `agents_dispatched` is present but not a dict (a record with both malformed still
counts once — the disclosure is "how many records lost attribution", not "how many fields
were malformed"). A field that is ABSENT is a legitimate older record shape and is never
counted; only present-with-wrong-type counts. It is accumulated only for records that
reach the attribution stage (after the eligibility and future-date filters), so it is
always a subset of `records_eligible` and the two reconcile. A record counted here
contributes NO phase/agent heat for its malformed field(s) — it still counts toward
`records_eligible`/`records_aggregate_only` as before, and toward `records_invalid_shape`
on top, so a reader can see the dashboard's attribution numbers are PARTIAL for that
record, not silently wrong. Surfaced in the metrics friction sentence only when non-zero:
`"; N of M records malformed (phase/agent attribution incomplete)"`, `N` and `M` both lower
bounds under truncation like every other count on that sentence.

| Counter | Meaning |
|---|---|
| `records_dated_as_of` | records carrying a valid, non-future date. **Deliberately NOT `..._in_window`**: the joins have no 30-day lower bound, they only exclude future dates. A real inclusive window counter belongs to M8, where a window exists. |
| `records_undated` | no recognised date key at all |
| `records_invalid_date` | the record's FIRST recognised date key (first-match-wins order) matched the structural date shape but is not a real calendar date. A later key carrying a valid date does not rescue the record — the malformed higher-priority key is never skipped in favor of it. |
| `records_conflicting_date` | `date` and `timestamp` both present with valid but DIFFERENT dates. Scoped to that one pair: `date` and `verified_date` legitimately differ on the decisions stream. First-match-wins still returns the `date` value; the disagreement is counted, not swallowed. |
| `records_skipped_future` | records skipped entirely because their date is after the render date |
| `events_backfilled` | interventions only — joined events whose record is `backfilled: true` |
| `attribution_evidence` | interventions only — always `"INFERRED"` (see above) |

And on every stream, joined or not:

| Counter | Meaning |
|---|---|
| `truncated_at_cap` | **present only when the read stopped at a cap**: `"bytes"`, `"lines"`, or `"bytes+lines"`. The two caps (`max_bytes=5_000_000`, `max_lines=20_000`) are independent — a stream of many compact records trips the line cap without approaching the byte cap. Every count derived from a truncated stream renders as a lower bound (`≥N`) **on every surface that displays it** — stream card, per-component join table, friction-gauge decomposition and its INFERRED-attribution note, footer sentence, and the clipboard copy payload — and its severity band is SUPPRESSED entirely: a partial read must never paint green. The footer's collapsed **raw-counters `<details>`** is a `json.dumps` of the whole counter dict, so it keeps its integer values and instead carries a lead-in line stating that every count below it is a lower bound. |

### Stream card counts

Each stream's headline card (`_stream_event_count`) shows that stream's own lead figure, and
the figure differs by stream: `decisions`/`metrics` show their ATTRIBUTED count (`segments_joined`
/ eligible-minus-aggregate-only); `interventions` shows its PARSED count (`records_parsed`),
because that is the figure the interventions sentence leads with and the one an operator
checking "did my log get read" wants first; `codex` shows its aggregate run count. Consequently
**the four card numerals do not sum to `friction_total`** — an interventions record can be
parsed but unmatched, undated, or future-skipped, contributing zero to the total while still
counted on the card. This is deliberate, disclosed in words by the sentence beneath each card,
and is not a defect.

## Notes

1. **Signals vs. judgments.** The collector emits SIGNALS (A) — it counts, reads, and classifies mechanically (file categories, evidence labels, line thresholds). The model produces JUDGMENTS (B) — CIVC classification, drag outcomes, "give it one home" decisions. The collector never classifies a verb coverage or condemns a duplicate pair as dead weight; it only reports that the pair exists above threshold.

2. **Secret safety and the duplication metric.** `config.env_keys` lists env KEY NAMES only — the collector NEVER emits env VALUES (the real `env` holds `GITHUB_TOKEN` and other secrets). `duplication.metric` is `"containment"` (`|A∩B| / min(|A|,|B|)`), not Jaccard — containment is chosen because it correctly flags a short file fully subsumed by a longer one, which Jaccard would under-score. The shingle size constant `SHINGLE_K = 8` (also `duplication.shingle_k` in the output) sets the k-gram window used to build each file's shingle set before comparison. **Scope boundary:** this guard applies to `env` VALUES specifically — hook `command` strings ARE surfaced verbatim in `enforcement.hooks.registered[].command` and (truncated) in `blind_spots`, by design, since reconciliation requires showing the registered command for a human to audit an orphan. The harness convention is that secrets live in `env`, not inline in hook commands; this boundary is accepted, not a gap.

3. **Registration vs. target status — no exception.** A hook registration finding SEPARATES two facts and never collapses them. `registration_evidence: "VERIFIED"` means the collector READ the registration line in `settings.json` — that fact alone is always knowable. The registered TARGET's status is reported independently, by `stat()`-ing the path: a `FileNotFoundError` on the target routes the entry to `enforcement.hooks.orphan_registrations[]` with `target_status: "missing"` — a real orphan. A `PermissionError` on the target routes the entry to `inaccessible[]` with `evidence: "INACCESSIBLE"` — a permission-denied target is NOT an orphan and must never be condemned as one, because the collector could not actually see it. Registration evidence and target status are always distinct fields; nothing sets one from the other. `duplication` output is deterministic across runs: pairs are sorted by `(-score, a, b)`, and `shared_sample` is the lexicographically-smallest shared shingle between the pair.

4. **`headline.unchecked_binary_count` is reserved, always `0` in v1.** No binary scan is performed — the walk reads only `.md`/`.py`/`.sh` via `errors='replace'` (text mode). This `0` reflects "not inspected," never "no binaries found" — do NOT read it as a clean bill of health.
