"""M11 layout profiles (SPEC_7 §2). Real fixtures only -- no mocks (CLAUDE.md rule 9).
Reuses `fake_harness` from conftest.py:13. Binds the collector as `_collector`, matching
tests/test_collector.py:19-21."""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SKILL_DIR = Path(__file__).resolve().parents[1]
COLLECTOR = SKILL_DIR / "collector.py"

_spec = importlib.util.spec_from_file_location("harness_map_collector_profiles", COLLECTOR)
_collector = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_collector)


def run_collector_raw(root, *args, project_root=None, env=None):
    """Like test_collector.run_collector, but returns the CompletedProcess WITHOUT
    asserting returncode == 0 -- the exit-2 profile tests need the failing run."""
    cmd = [sys.executable, str(COLLECTOR), "--root", str(root)]
    if project_root is not None:
        cmd += ["--project-root", str(project_root)]
    cmd += list(args)
    run_env = dict(os.environ, **env) if env else None
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=run_env)


def _jsonable(profile):
    """PROFILE_CLAUDE_CODE holds tuples (immutability); JSON holds lists. Normalize
    tuples -> lists RECURSIVELY, preserving order (order is load-bearing, DECISION 4)."""
    if isinstance(profile, dict):
        return {k: _jsonable(v) for k, v in profile.items()}
    if isinstance(profile, tuple):
        return [_jsonable(v) for v in profile]
    return profile


def test_profile_file_matches_embedded_constant():
    """Drift check: profiles/claude-code.json is the exported twin of the embedded
    default. The CONSTANT is authoritative at runtime; the file is documentation and a
    template for sharers (SPEC_7 §2 [DECISION]).
    # Changing the profile schema requires a spec change (SPEC_7 §2)."""
    on_disk = json.loads((SKILL_DIR / "profiles" / "claude-code.json").read_text())
    assert on_disk == _jsonable(_collector.PROFILE_CLAUDE_CODE)


def test_default_profile_reproduces_the_shared_glob_constants():
    """The proof that the default path is unchanged: the derived tuples must equal the
    module constants the collector has always used, ORDER INCLUDED.
    # Changing this value requires a spec change (SPEC_7 §2)."""
    p = _collector.PROFILE_CLAUDE_CODE
    assert _collector._instruction_globs(p) == _collector._INSTRUCTION_GLOBS
    assert tuple(p["duplication_globs"]) == _collector._DUP_GLOBS
    assert tuple(p["rules_globs"]) == _collector._STALENESS_RULE_GLOBS
    assert tuple(p["hook_script_globs"]) == _collector._HOOK_SCRIPT_GLOBS
    assert tuple(p["hook_test_globs"]) == _collector._HOOK_TEST_GLOBS
    assert _collector._hook_body_suffixes(p) == _collector._HOOK_BODY_SUFFIXES


def test_load_profile_preserves_glob_order(tmp_path):
    """_deduped_instruction_files is FIRST-MATCH-WINS: skills/*/agents/*.md must stay
    ahead of agents/*.md or a deploy symlink resolves to the wrong canonical path
    (collector.py:2816 docstring). load_profile must never sort."""
    src = json.loads((SKILL_DIR / "profiles" / "claude-code.json").read_text())
    p = tmp_path / "ordered.json"
    p.write_text(json.dumps(src))
    loaded = _collector.load_profile(p)
    globs = _collector._instruction_globs(loaded)
    assert globs.index("skills/*/agents/*.md") < globs.index("agents/*.md")


def test_load_profile_rejects_non_object(tmp_path):
    p = tmp_path / "arr.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "JSON object" in str(exc.value)


def test_load_profile_rejects_malformed_json(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json")
    with pytest.raises(_collector.ProfileError):
        _collector.load_profile(p)


def test_load_profile_rejects_unknown_settings_format(tmp_path):
    src = json.loads((SKILL_DIR / "profiles" / "claude-code.json").read_text())
    src["settings_format"] = "cursor"
    p = tmp_path / "sf.json"
    p.write_text(json.dumps(src))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "settings_format" in str(exc.value)


def _write_profile(tmp_path, mutate):
    src = json.loads((SKILL_DIR / "profiles" / "claude-code.json").read_text())
    mutate(src)
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(src))
    return p


def test_profile_unknown_key_rejected_with_clear_error(fake_harness, tmp_path):
    p = _write_profile(tmp_path, lambda d: d.__setitem__("nonsense_key", "x"))
    proc = run_collector_raw(fake_harness, "--profile", str(p))
    assert proc.returncode == 2, proc.stderr
    assert "nonsense_key" in proc.stderr
    assert "unknown key" in proc.stderr


def test_profile_missing_required_key_names_the_key(fake_harness, tmp_path):
    p = _write_profile(tmp_path, lambda d: d.pop("rules_globs"))
    proc = run_collector_raw(fake_harness, "--profile", str(p))
    assert proc.returncode == 2, proc.stderr
    assert "rules_globs" in proc.stderr
    assert "missing required key" in proc.stderr


def test_bad_profile_still_emits_a_full_valid_envelope(fake_harness, tmp_path):
    """Invariant 2: main() emits a valid JSON envelope on ANY failure. A bad profile is a
    failure like any other -- exit 2, but stdout is still a full-key empty document, and
    it records the reason. Also proves the profile NEVER half-applied: every count is 0
    because build_document was not reached.
    # Changing this value requires a spec change (SPEC_7 §2)."""
    p = _write_profile(tmp_path, lambda d: d.pop("skills_globs"))
    proc = run_collector_raw(fake_harness, "--profile", str(p))
    assert proc.returncode == 2
    doc = json.loads(proc.stdout)
    for key in ("schema_version", "generated_at", "root", "headline", "always_loaded",
                "on_demand", "enforcement", "config", "instruction_length_flags",
                "duplication", "phantom_refs", "promotion_candidates", "test_coverage",
                "inaccessible", "blind_spots", "errors"):
        assert key in doc, f"crash envelope missing top-level key: {key}"
    assert doc["headline"]["always_loaded_file_count"] == 0
    assert any("skills_globs" in e for e in doc["errors"])


def test_bad_profile_still_writes_the_envelope_to_out(fake_harness, tmp_path):
    """SPEC_7 §2: 'exit 2; envelope still emitted if --out given, per Invariant 2'."""
    out = tmp_path / "reports" / "sidecar.json"
    out.parent.mkdir(parents=True)
    p = _write_profile(tmp_path, lambda d: d.pop("agents_glob"))
    proc = run_collector_raw(fake_harness, "--profile", str(p), "--out", str(out))
    assert proc.returncode == 2
    assert out.is_file(), "envelope was not written to --out on the profile-error path"
    assert json.loads(out.read_text())["schema_version"] == _collector.SCHEMA_VERSION


def test_missing_profile_file_exits_two(fake_harness, tmp_path):
    proc = run_collector_raw(fake_harness, "--profile", str(tmp_path / "nope.json"))
    assert proc.returncode == 2
    assert "nope.json" in proc.stderr


def test_profile_load_never_writes_inside_root(fake_harness, tmp_path):
    """Read-only posture (CLAUDE.md rule 4): load_profile adds a READ path, never a write
    path. Byte-and-mtime snapshot of --root before/after a profiled run."""
    def snap(root):
        return {str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
                for p in sorted(root.rglob("*")) if p.is_file()}
    before = snap(fake_harness)
    run_collector_raw(fake_harness, "--profile",
                      str(SKILL_DIR / "profiles" / "claude-code.json"))
    assert snap(fake_harness) == before


def test_default_profile_matches_claude_code_layout(fake_harness, tmp_path):
    """SPEC_7 §2 scope guard: an explicit --profile profiles/claude-code.json run must
    produce the SAME document as a no-flag run, modulo generated_at. This is the whole
    no-behavior-change contract in one assertion.
    # Changing this value requires a spec change (SPEC_7 §2)."""
    proj = fake_harness.parent / "active-repo"
    default_proc = run_collector_raw(fake_harness, project_root=proj)
    assert default_proc.returncode == 0, default_proc.stderr
    profiled_proc = run_collector_raw(
        fake_harness, "--profile", str(SKILL_DIR / "profiles" / "claude-code.json"),
        project_root=proj)
    assert profiled_proc.returncode == 0, profiled_proc.stderr
    a = json.loads(default_proc.stdout)
    b = json.loads(profiled_proc.stdout)
    a.pop("generated_at")
    b.pop("generated_at")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_profile_hook_command_remap_is_profile_driven(fake_harness, tmp_path):
    """The `~/.claude/hooks/...` literal remap comes from hook_command_remaps, not from a
    hard-coded string. A profile with an empty remap list leaves a ~-prefixed command
    unremapped, so it resolves to the REAL expanduser path and reports as an orphan
    registration rather than silently resolving under --root."""
    (fake_harness / "hooks" / "x.py").write_text("# hook\n")
    (fake_harness / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "python3 ~/.claude/hooks/x.py"}]}]}}))
    default_doc = json.loads(run_collector_raw(fake_harness).stdout)
    assert any(r["script"].endswith("hooks/x.py")
               for r in default_doc["enforcement"]["hooks"]["registered"])
    p = _write_profile(tmp_path, lambda d: d.__setitem__("hook_command_remaps", []))
    no_remap = json.loads(run_collector_raw(fake_harness, "--profile", str(p)).stdout)
    assert no_remap["enforcement"]["hooks"]["registered"] == []


def test_default_profile_matches_claude_code_layout_in_compose_mode(fake_harness):
    """Same guard with --compose on, so the project-tier path is exercised too."""
    proj = fake_harness.parent / "active-repo"
    a_proc = run_collector_raw(fake_harness, "--compose", project_root=proj)
    b_proc = run_collector_raw(
        fake_harness, "--compose", "--profile",
        str(SKILL_DIR / "profiles" / "claude-code.json"), project_root=proj)
    assert a_proc.returncode == 0 and b_proc.returncode == 0
    a, b = json.loads(a_proc.stdout), json.loads(b_proc.stdout)
    a.pop("generated_at")
    b.pop("generated_at")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


_FOREIGN_PROFILE = {
    "name": "bare-agents-md",
    "top_level_files": {"root_instructions": "AGENTS.md", "settings": None,
                        "memory_index": None, "plugin_marketplaces": None,
                        "plugin_installed": None},
    "container_dirs": {"skills": None, "rules": "rules", "commands": None, "agents": None,
                       "hooks": None, "hook_tests": None, "projects": None, "memory": None},
    "projects_glob": None, "memory_index_name": None, "skill_manifest_name": None,
    "rules_globs": ["rules/*.md"], "skills_globs": [],
    "commands_glob": None, "agents_glob": None,
    "hook_script_globs": [], "hook_test_globs": [],
    "dispatcher_suffix": None, "hook_command_remaps": [],
    "duplication_globs": ["rules/*.md"],
    "settings_format": "none",
}


@pytest.fixture
def bare_agents_repo(tmp_path):
    """A real, minimal NON-Claude-Code harness: an AGENTS.md plus a rules dir. No
    settings.json, no skills/, no hooks/ -- the layout the seam has to prove itself on."""
    repo = tmp_path / "foreign-harness"
    (repo / "rules").mkdir(parents=True)
    (repo / "AGENTS.md").write_text("# Agent instructions\n" + "policy word " * 40)
    (repo / "rules" / "style.md").write_text("Style rule body " * 30)
    (repo / "rules" / "safety.md").write_text("Safety rule body " * 30)
    return repo


def test_foreign_profile_maps_bare_agents_md_repo(bare_agents_repo, tmp_path):
    """SPEC_7 §2 stage gate: a synthetic non-CC layout genuinely mapped -- the instruction
    files are found and a valid FULL-envelope document is emitted.
    # Changing this value requires a spec change (SPEC_7 §2)."""
    p = tmp_path / "foreign.json"
    p.write_text(json.dumps(_FOREIGN_PROFILE))
    proc = run_collector_raw(bare_agents_repo, "--profile", str(p),
                             project_root=bare_agents_repo)
    assert proc.returncode == 0, proc.stderr
    doc = json.loads(proc.stdout)
    for key in ("schema_version", "generated_at", "root", "headline", "always_loaded",
                "on_demand", "enforcement", "config", "instruction_length_flags",
                "duplication", "phantom_refs", "promotion_candidates", "test_coverage",
                "staleness", "metric_definitions", "inaccessible", "blind_spots", "errors"):
        assert key in doc, f"foreign-profile document missing top-level key: {key}"
    paths = {f["path"] for f in doc["always_loaded"]["files"]}
    assert "AGENTS.md" in paths
    assert {"rules/style.md", "rules/safety.md"} <= paths
    assert doc["headline"]["always_loaded_file_count"] == 3
    assert doc["headline"]["always_loaded_words"] > 0
    # settings_format "none": no settings read at all, disclosed rather than silent.
    assert doc["config"]["evidence"] == "INACCESSIBLE"
    assert doc["enforcement"]["hooks"]["registered"] == []
    assert any("settings_format" in b for b in doc["blind_spots"])
    assert doc["errors"] == []


def test_foreign_profile_never_reads_settings_json(bare_agents_repo, tmp_path):
    """settings_format 'none' must SKIP the file, not merely fail to parse it: a
    settings.json placed in the foreign repo is ignored entirely and contributes no
    errors[] entry."""
    (bare_agents_repo / "settings.json").write_text("{ this is not json")
    p = tmp_path / "foreign.json"
    p.write_text(json.dumps(_FOREIGN_PROFILE))
    doc = json.loads(run_collector_raw(bare_agents_repo, "--profile", str(p),
                                       project_root=bare_agents_repo).stdout)
    assert not any("settings.json" in e for e in doc["errors"])


def test_foreign_profile_with_compose_discloses_project_tier_limitation(
        bare_agents_repo, tmp_path):
    """Flagged gap, disclosed not silent: the project tier still uses the Claude Code
    layout (.claude/, CLAUDE.local.md, .mcp.json) regardless of profile. --compose under a
    non-claude-code profile must SAY so in blind_spots.
    # Changing this value requires a spec change (SPEC_7 §2)."""
    p = tmp_path / "foreign.json"
    p.write_text(json.dumps(_FOREIGN_PROFILE))
    doc = json.loads(run_collector_raw(bare_agents_repo, "--compose", "--profile", str(p),
                                       project_root=bare_agents_repo).stdout)
    assert any("project tier" in b and "Claude Code" in b for b in doc["blind_spots"])


def test_foreign_profile_skips_plugin_reads_even_when_files_exist(bare_agents_repo, tmp_path):
    """Coverage gap check: _FOREIGN_PROFILE alone can't prove the settings_format
    short-circuit does anything, because it ALSO sets plugin_marketplaces/plugin_installed
    to None -- the orthogonal per-role guard in collect_config would produce the identical
    empty result even with the short-circuit deleted entirely (verified by disabling it:
    the naive version of this test still passed). This profile keeps settings_format
    "none" but points plugin_marketplaces/plugin_installed at REAL, well-formed files (the
    shape a claude-code profile WOULD read), isolating the short-circuit as the only thing
    that can be skipping them."""
    profile = dict(_FOREIGN_PROFILE)
    profile["top_level_files"] = dict(_FOREIGN_PROFILE["top_level_files"])
    profile["top_level_files"]["plugin_marketplaces"] = "plugins/known_marketplaces.json"
    profile["top_level_files"]["plugin_installed"] = "plugins/installed_plugins.json"
    (bare_agents_repo / "plugins").mkdir()
    (bare_agents_repo / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"marketplaces": {"acme-market": {"url": "https://example.invalid/acme"}}}))
    (bare_agents_repo / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"installed": {"acme-plugin": {"version": "1.0.0"}}}))
    p = tmp_path / "foreign.json"
    p.write_text(json.dumps(profile))
    doc = json.loads(run_collector_raw(bare_agents_repo, "--profile", str(p),
                                       project_root=bare_agents_repo).stdout)
    assert doc["config"]["marketplaces"] == []
    assert doc["config"]["marketplace_count"] == 0
    assert doc["config"]["installed_plugins"] == []
    assert doc["config"]["installed_plugin_count"] == 0
    assert not any("known_marketplaces.json" in b or "installed_plugins.json" in b
                   for b in doc["blind_spots"])


# --- M11 exit gate, Finding 1 (P1): load_profile must reject an absolute or '..'-bearing
# profile path VALUE before it is ever half-applied. ---

def test_load_profile_rejects_absolute_top_level_files_role(tmp_path):
    """Direct load_profile reproduction of the team-lead's Finding 1: an absolute
    top_level_files role used to validate as a bare string, then silently REPLACE the
    root when joined (`root / "/etc/hosts" == Path("/etc/hosts")`)."""
    p = _write_profile(tmp_path, lambda d: d["top_level_files"].__setitem__(
        "root_instructions", "/etc/hosts"))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "top_level_files.root_instructions" in str(exc.value)


def test_absolute_profile_path_rejected_at_exit_two_before_any_read(fake_harness, tmp_path):
    """Full-CLI reproduction of the team-lead's Finding 1 exactly: BASELINE (no profile)
    reads several always-loaded files with 0 errors; a profile identical to
    claude-code.json except for an absolute root_instructions used to crash
    walk_always_loaded (an unguarded `_rel`'s `relative_to` raising ValueError) with the
    whole document replaced by `_empty_document` and reported at EXIT 0 -- total
    inventory loss read as a clean empty harness. It must now be rejected by
    load_profile ITSELF, at exit 2, naming the offending key, stdout still a full-key
    envelope (Invariant 2), and no crash (`errors` carries only the profile-rejection
    message, never a traceback fragment).
    # Changing this value requires a spec change (SPEC_7 §2)."""
    baseline = json.loads(run_collector_raw(fake_harness).stdout)
    assert baseline["headline"]["always_loaded_file_count"] > 0
    assert baseline["errors"] == []

    p = _write_profile(tmp_path, lambda d: d["top_level_files"].__setitem__(
        "root_instructions", "/etc/hosts"))
    proc = run_collector_raw(fake_harness, "--profile", str(p))
    assert proc.returncode == 2, proc.stderr
    assert "top_level_files.root_instructions" in proc.stderr
    doc = json.loads(proc.stdout)
    for key in ("schema_version", "generated_at", "root", "headline", "always_loaded",
                "on_demand", "enforcement", "config", "instruction_length_flags",
                "duplication", "phantom_refs", "promotion_candidates", "test_coverage",
                "inaccessible", "blind_spots", "errors"):
        assert key in doc, f"crash envelope missing top-level key: {key}"
    assert doc["headline"]["always_loaded_file_count"] == 0
    assert len(doc["errors"]) == 1
    assert "top_level_files.root_instructions" in doc["errors"][0]
    assert "ValueError" not in doc["errors"][0]  # never the old crash-message shape


def test_load_profile_rejects_absolute_container_dirs_role(tmp_path):
    p = _write_profile(tmp_path, lambda d: d["container_dirs"].__setitem__("hooks", "/tmp"))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "container_dirs.hooks" in str(exc.value)


def test_load_profile_rejects_dotdot_in_a_list_glob(tmp_path):
    p = _write_profile(tmp_path, lambda d: d.__setitem__(
        "rules_globs", d["rules_globs"] + ["../outside.md"]))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "rules_globs" in str(exc.value)


def test_load_profile_rejects_absolute_entry_in_hook_script_globs(tmp_path):
    p = _write_profile(tmp_path, lambda d: d.__setitem__(
        "hook_script_globs", d["hook_script_globs"] + ["/etc/*.py"]))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "hook_script_globs" in str(exc.value)


def test_load_profile_rejects_dotdot_projects_glob(tmp_path):
    p = _write_profile(tmp_path, lambda d: d.__setitem__("projects_glob", "../projects/*"))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "projects_glob" in str(exc.value)


def test_load_profile_rejects_bare_name_with_embedded_separator(tmp_path):
    """memory_index_name/skill_manifest_name are joined as a SINGLE filename component
    (`skill_dir / skill_manifest_name`) -- neither absolute nor '..'-bearing, "sub/x.md"
    still names two components where the schema promises one."""
    p = _write_profile(tmp_path, lambda d: d.__setitem__("skill_manifest_name", "sub/SKILL.md"))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "skill_manifest_name" in str(exc.value)


def test_load_profile_rejects_bare_name_equal_to_dotdot(tmp_path):
    p = _write_profile(tmp_path, lambda d: d.__setitem__("memory_index_name", ".."))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "memory_index_name" in str(exc.value)


def test_load_profile_rejects_absolute_second_element_of_hook_command_remap(tmp_path):
    p = _write_profile(tmp_path, lambda d: d.__setitem__(
        "hook_command_remaps", [["~/.claude/hooks", "/etc/hooks"]]))
    with pytest.raises(_collector.ProfileError) as exc:
        _collector.load_profile(p)
    assert "hook_command_remaps" in str(exc.value)


def test_load_profile_does_not_check_the_first_hook_command_remap_element(tmp_path):
    """The FIRST element is a literal '~'-prefixed prefix matched textually against a
    registered command string -- it is SUPPOSED to look absolute-ish and is never joined
    onto `root`, so it must load even with a '..' inside it."""
    p = _write_profile(tmp_path, lambda d: d.__setitem__(
        "hook_command_remaps", [["~/../weird/prefix", "hooks"]]))
    loaded = _collector.load_profile(p)  # must not raise
    assert loaded["hook_command_remaps"] == (("~/../weird/prefix", "hooks"),)


def test_load_profile_does_not_check_dispatcher_suffix_name_or_settings_format():
    """dispatcher_suffix is only ever compared with str.endswith (reconcile_hooks) --
    never joined onto a path -- and `name`/`settings_format` are labels/enum values, not
    paths. A '..'/'/' inside dispatcher_suffix or name is inert here, so load_profile
    must not reject it (documents the deliberate exclusion from Finding 1's fix)."""
    def mutate(d):
        d["dispatcher_suffix"] = "../weird-suffix.py"
        d["name"] = "weird/name"
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        p = _write_profile(Path(td), mutate)
        loaded = _collector.load_profile(p)  # must not raise
        assert loaded["dispatcher_suffix"] == "../weird-suffix.py"
        assert loaded["name"] == "weird/name"


# --- M11 exit gate, Finding 2 (P2): a profile glob with NO '..' and NO absolute path in
# its TEXT can still escape containment if the directory it names is a symlink pointing
# outside --root -- Finding 1's validation only rejects the STRING, not what it resolves
# to on disk. scan_duplication and _staleness_corpus must gate the read the same way
# _deduped_instruction_files already does. ---

def test_duplication_glob_symlink_escape_is_gated_not_read(fake_harness, tmp_path):
    """Reproduces the team-lead's Finding 2 exactly: an in-root file and an out-of-root
    file share an 8-word shingle. Before this fix, the out-of-root file was read and its
    content sampled straight into a duplication pair's shared_sample with no containment
    check at all."""
    outside = tmp_path / "outside"
    outside.mkdir()
    shared_text = "alpha bravo charlie delta echo foxtrot golf hotel " * 4
    (outside / "outside_secret.md").write_text(shared_text)
    (fake_harness / "escaped").symlink_to(outside)
    (fake_harness / "inroot_dup.md").write_text(shared_text)
    p = _write_profile(tmp_path, lambda d: d.__setitem__(
        "duplication_globs", d["duplication_globs"] + ["*.md", "escaped/*.md"]))
    doc = json.loads(run_collector_raw(fake_harness, "--profile", str(p)).stdout)
    assert doc["errors"] == []
    assert not any("escaped/outside_secret.md" in (pair["a"], pair["b"])
                   for pair in doc["duplication"]["pairs"])
    assert any(
        "duplication corpus file escaped/outside_secret.md resolves outside the "
        "harness root — not read" in b
        for b in doc["blind_spots"])


def test_staleness_glob_symlink_escape_is_gated_not_read(fake_harness, tmp_path):
    """Same containment escape as the duplication test above, for _staleness_corpus's
    rules_globs consumer."""
    outside = tmp_path / "outside2"
    outside.mkdir()
    (outside / "outside_secret.md").write_text("Secret rule body " * 30)
    (fake_harness / "escaped2").symlink_to(outside)
    p = _write_profile(tmp_path, lambda d: d.__setitem__(
        "rules_globs", d["rules_globs"] + ["escaped2/*.md"]))
    doc = json.loads(run_collector_raw(fake_harness, "--profile", str(p)).stdout)
    assert doc["errors"] == []
    assert any(
        "staleness corpus file escaped2/outside_secret.md resolves outside the "
        "harness root — not read" in b
        for b in doc["blind_spots"])


def test_hooks_body_corpus_leaf_symlink_escape_is_gated_not_read(fake_harness, tmp_path):
    """M11 exit gate, Finding 2 (P2, assessed item 3): a hook SCRIPT that is itself a
    symlink pointing outside --root must not have its body read into the hooks corpus --
    same fp_inside pattern reconcile_hooks already applies to the identical hooks/ walk,
    now also applied to _hooks_body_corpus's separate, parallel enumeration of it.

    Proven via the env-flag phantom-ref cross-reference (check_phantom_refs,
    collector.py ~5029): `SUPER_GUARD_FLAG` is referenced ONLY by the escaped hook's
    body. BEFORE this fix, `_hooks_body_corpus` read that body anyway, so the flag
    looked referenced and NO phantom row was ever emitted for it -- a false negative
    (the corpus's one and only piece of evidence for the flag was itself read from
    outside --root). AFTER the fix, the flag is correctly flagged AND downgraded to
    resolved=None (not the confirmed-broken False) because the corpus is now known-
    incomplete -- exactly the D2 treatment `_hooks_body_corpus`'s own docstring
    describes for an unseen hook body. Disclosed via blind_spots, not inaccessible: a
    protected test_collector.py assertion
    (test_out_of_root_registered_dispatcher_does_not_drive_reachability) pins "an
    out-of-root target is a blind-spot, NOT an inaccessible entry" for this identical
    hooks/-symlink shape via reconcile_hooks' own pre-existing gate, so
    _hooks_body_corpus's new gate follows the same convention."""
    outside = tmp_path / "outside3"
    outside.mkdir()
    (outside / "real_hook.py").write_text("import os\nos.environ.get('SUPER_GUARD_FLAG')\n")
    (fake_harness / "hooks" / "escaped_hook.py").symlink_to(outside / "real_hook.py")
    (fake_harness / "rules" / "envflag.md").write_text(
        "Reads the `SUPER_GUARD_FLAG` env var to bypass writes. " + "pad word " * 10)
    doc = json.loads(run_collector_raw(fake_harness).stdout)
    assert doc["errors"] == []
    env_rows = [r for r in doc["phantom_refs"]
                if r.get("kind") == "env_flag" and r.get("ref") == "SUPER_GUARD_FLAG"]
    assert len(env_rows) == 1, doc["phantom_refs"]
    assert env_rows[0]["resolved"] is None
    assert not any("escaped_hook.py" in e.get("path", "") for e in doc["inaccessible"])
    assert any("escaped_hook.py" in b for b in doc["blind_spots"])


# --- M11 exit gate, Finding 3 (P2) + Finding 4 (P3): _compose_hooks must forward the
# active profile ONLY to the profile-aware user tier, and the user-tier hook record's
# source_file label must come from the profile's own settings role. ---

def test_compose_hooks_forwards_profile_only_to_user_tier(fake_harness, tmp_path):
    """Finding 3: _compose_hooks used to call `_script_from_command(command,
    resolve_root)` with NO profile argument at all -- every tier, including the
    profile-AWARE user tier, silently remapped a `~/.claude/hooks/...` command against
    PROFILE_CLAUDE_CODE's hook_command_remaps regardless of --profile. Uses a
    settings_format='claude-code' profile with settings PRESENT (unlike
    _FOREIGN_PROFILE, whose settings_format='none' makes `if not settings` skip the
    tier before this bug ever executes -- the gap the team-lead's brief names
    explicitly) and a DISTINCTIVE hook_command_remaps target, so the user-tier record's
    resolved `script` differs depending on whether the custom remap was actually used.
    The project tier must resolve to the DEFAULT Claude Code remap regardless (schema.md
    deferred coupling #1) -- proving `profile` is forwarded to user only, never
    blanket-forwarded."""
    proj = tmp_path / "active-repo"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "python3 ~/.claude/hooks/proj_hook.py"}]}]}}))
    (fake_harness / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "python3 ~/.claude/hooks/user_hook.py"}]}]}}))
    p = _write_profile(tmp_path, lambda d: d.__setitem__(
        "hook_command_remaps", [["~/.claude/hooks", "custom_hooks_dir"]]))
    doc = json.loads(run_collector_raw(fake_harness, "--compose", "--profile", str(p),
                                       project_root=proj).stdout)
    hooks = doc["composed_settings"]["hooks"]
    user_records = [h for h in hooks if h["tier"] == "user"]
    project_records = [h for h in hooks if h["tier"] == "project"]
    assert len(user_records) == 1, hooks
    assert len(project_records) == 1, hooks
    assert user_records[0]["script"] == "custom_hooks_dir/user_hook.py"
    assert project_records[0]["script"] == "hooks/proj_hook.py"


def test_compose_hooks_user_source_file_reflects_profile_settings_role(fake_harness, tmp_path):
    """Finding 4: the user-tier hook record's source_file used to be the hardcoded
    literal `str(root / "settings.json")` -- a profile naming a custom
    top_level_files.settings labeled composed hooks with a file that was never even
    read. It must now name the file the profile actually declares."""
    proj = fake_harness.parent / "active-repo"
    (fake_harness / "custom_settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "python3 hooks/x.py"}]}]}}))
    p = _write_profile(tmp_path, lambda d: d["top_level_files"].__setitem__(
        "settings", "custom_settings.json"))
    doc = json.loads(run_collector_raw(fake_harness, "--compose", "--profile", str(p),
                                       project_root=proj).stdout)
    user_records = [h for h in doc["composed_settings"]["hooks"] if h["tier"] == "user"]
    assert len(user_records) == 1, doc["composed_settings"]["hooks"]
    assert Path(user_records[0]["source_file"]).name == "custom_settings.json"


# --- M11 exit gate, Finding 7 + Finding 8 (test gaps): _FOREIGN_PROFILE nulls
# container_dirs["hooks"]/["skills"] alongside dispatcher_suffix/skills_globs, so neither
# null value is ever exercised against a POPULATED dir of real files. ---

def test_null_dispatcher_suffix_disables_fanout_but_still_inventories_scripts(
        fake_harness, tmp_path):
    """Finding 7: _FOREIGN_PROFILE also nulls container_dirs["hooks"], so
    `_hook_disk_files` short-circuits to `[]` (collector.py ~2454) before the
    `dispatcher_suffix is not None` guard in `reconcile_hooks` (~2557) ever runs against
    real files. This profile keeps container_dirs["hooks"] populated (the shipped
    "hooks") while nulling ONLY dispatcher_suffix, proving the two are independent: no
    file is treated as a dispatcher regardless of its name, so `CHECKS`-based fan-out
    never fires, while every hook script on disk is still inventoried."""
    (fake_harness / "hooks" / "leaf-dispatcher.py").write_text('CHECKS = ["other.py"]\n')
    (fake_harness / "hooks" / "other.py").write_text('# reachable only via dispatch fan-out\n')
    (fake_harness / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "python3 hooks/leaf-dispatcher.py"}]}]}}))
    p = _write_profile(tmp_path, lambda d: d.__setitem__("dispatcher_suffix", None))
    doc = json.loads(run_collector_raw(fake_harness, "--profile", str(p)).stdout)
    assert doc["errors"] == []
    by_name = {s["name"]: s for s in doc["enforcement"]["hooks"]["scripts_on_disk"]}
    assert {"leaf-dispatcher.py", "other.py"} <= set(by_name), by_name
    assert by_name["leaf-dispatcher.py"]["registered_via"] == "direct"
    assert by_name["other.py"]["registered_via"] == "none"  # never dispatcher-reached


def test_empty_skills_globs_excludes_skills_from_instruction_corpus_but_keeps_descriptions(
        fake_harness, tmp_path):
    """Finding 8: _FOREIGN_PROFILE also nulls container_dirs["skills"], so the
    independence of skill DESCRIPTIONS (`collect_descriptions`, driven by
    container_dirs["skills"] + skill_manifest_name) from skill CONTENT reaching the
    instruction corpus (`_instruction_globs`, driven separately by skills_globs) is never
    demonstrated. This profile keeps container_dirs["skills"] populated (the shipped
    "skills") while emptying ONLY skills_globs.

    Note: `duplication_globs` is a SEPARATE, independent profile key, untouched here --
    skill content still reaches the duplication scan through it (the finding names only
    skills_globs, and this test proves exactly what skills_globs itself controls). Also
    note `rules_globs` itself includes "skills/*/rules/*.md" (the coding-team-style
    generalized rules-in-a-sub-skill pattern) -- that path keeps a "skills/" PREFIX but is
    governed by rules_globs, not skills_globs, so it correctly stays in the corpus; the
    assertions below check the skills_globs-owned patterns by exact path, not by prefix."""
    default_files = _collector._deduped_instruction_files(fake_harness, [], [])
    default_rels = {str(f.relative_to(fake_harness)) for f in default_files}
    assert "skills/demo/SKILL.md" in default_rels
    assert "skills/demo/phases/p1.md" in default_rels

    p = _write_profile(tmp_path, lambda d: d.__setitem__("skills_globs", []))
    doc = json.loads(run_collector_raw(fake_harness, "--profile", str(p)).stdout)
    assert doc["errors"] == []
    # Descriptions still appear: container_dirs["skills"] is untouched by skills_globs.
    skill_names = {s["name"] for s in doc["always_loaded"]["skill_descriptions"]}
    assert "demo" in skill_names
    # But no skills/*/SKILL.md, skills/*/phases/*.md etc. entered the instruction corpus
    # this profile drives -- proven directly against the loaded profile.
    profile = _collector.load_profile(p)
    corpus_files = _collector._deduped_instruction_files(fake_harness, [], [], profile=profile)
    corpus_rels = {str(f.relative_to(fake_harness)) for f in corpus_files}
    assert "skills/demo/SKILL.md" not in corpus_rels
    assert "skills/demo/phases/p1.md" not in corpus_rels
    assert "skills/coding-team/rules/c.md" in corpus_rels  # rules_globs, unaffected
    assert any(r.startswith("rules/") for r in corpus_rels)  # rules_globs untouched
