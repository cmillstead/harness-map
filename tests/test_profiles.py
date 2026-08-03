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
