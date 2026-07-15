import json
import os
import re
import subprocess
import sys
import pytest
from pathlib import Path

COLLECTOR = Path(__file__).resolve().parents[1] / "collector.py"

def run_collector(root, *args, project_root=None):
    cmd = [sys.executable, str(COLLECTOR), "--root", str(root)]
    if project_root is not None:
        cmd += ["--project-root", str(project_root)]
    cmd += list(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)

def _active_slug(fake_harness):
    proj = fake_harness.parent / "active-repo"
    return proj, re.sub(r"[/.]", "-", os.path.abspath(str(proj)))

def test_schema_top_level_keys_present(fake_harness):
    doc = run_collector(fake_harness)
    for key in ("schema_version", "generated_at", "root", "headline", "always_loaded",
                "on_demand", "enforcement", "config", "instruction_length_flags", "duplication",
                "phantom_refs", "promotion_candidates", "test_coverage",
                "inaccessible", "blind_spots", "errors"):
        assert key in doc, f"missing top-level key: {key}"
    assert doc["schema_version"] == 1

def test_always_loaded_lists_global_surfaces(fake_harness):
    proj, slug = _active_slug(fake_harness)
    doc = run_collector(fake_harness, project_root=proj)
    paths = {f["path"] for f in doc["always_loaded"]["files"]}
    assert {"CLAUDE.md", f"projects/{slug}/memory/MEMORY.md",
            "rules/a.md", "skills/coding-team/rules/c.md"} <= paths
    claude = next(f for f in doc["always_loaded"]["files"] if f["path"] == "CLAUDE.md")
    assert claude["words"] >= 40 and claude["evidence"] == "VERIFIED"
    assert doc["headline"]["always_loaded_file_count"] >= 5

def test_only_active_project_memory_index_counted(fake_harness):
    proj, slug = _active_slug(fake_harness)
    doc = run_collector(fake_harness, project_root=proj)
    files = {f["path"] for f in doc["always_loaded"]["files"]}
    assert f"projects/{slug}/memory/MEMORY.md" in files
    assert "projects/other-proj-slug/memory/MEMORY.md" not in files
    variants = {v["path"] for v in doc["always_loaded"]["conditional_variants"]}
    assert "projects/other-proj-slug/memory/MEMORY.md" in variants
    assert f"projects/{slug}/memory/MEMORY.md" not in variants

def test_active_project_claude_md_counted(fake_harness):
    proj, slug = _active_slug(fake_harness)
    doc = run_collector(fake_harness, project_root=proj)
    proj_claude = [f for f in doc["always_loaded"]["files"] if f["category"] == "project_claude_md"]
    assert proj_claude and proj_claude[0]["words"] >= 25 and proj_claude[0]["evidence"] == "VERIFIED"

def test_active_project_memory_bodies_are_on_demand(fake_harness):
    proj, slug = _active_slug(fake_harness)
    doc = run_collector(fake_harness, project_root=proj)
    bodies = {b["path"] for b in doc["on_demand"]["memory_bodies"]}
    assert f"projects/{slug}/memory/detail.md" in bodies
    assert f"projects/{slug}/memory/MEMORY.md" not in bodies

def test_descriptions_counted(fake_harness):
    doc = run_collector(fake_harness)
    assert "demo" in {s["name"] for s in doc["always_loaded"]["skill_descriptions"]}
    assert "demo-agent" in {a["name"] for a in doc["always_loaded"]["agent_descriptions"]}

@pytest.mark.parametrize("description_block,expected_min_words", [
    ("description: A single-line demo skill description with six words.\n", 6),
    ("description: 'A single-quoted demo skill description with seven words.'\n", 7),
    ('description: "A double-quoted demo skill description with seven words."\n', 7),
    ("description: |\n  A block-scalar demo skill description\n  spanning two lines total.\n", 7),
])
def test_frontmatter_description_forms_counted(fake_harness, description_block, expected_min_words):
    skill_dir = fake_harness / "skills" / "descform"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: descform\n{description_block}---\nBody.\n")
    doc = run_collector(fake_harness)
    row = next(s for s in doc["always_loaded"]["skill_descriptions"] if s["name"] == "descform")
    assert row["words"] >= expected_min_words

def test_on_demand_lists_skill_bodies(fake_harness):
    doc = run_collector(fake_harness)
    skills = {s["name"]: s for s in doc["on_demand"]["skills"]}
    assert "demo" in skills and skills["demo"]["lines"] >= 1 and skills["demo"]["evidence"] == "VERIFIED"

def test_on_demand_counts_skill_internal_phase_bodies(fake_harness):
    doc = run_collector(fake_harness)
    internal = doc["on_demand"]["skill_internal_bodies"]
    match = [b for b in internal if b["skill"] == "demo" and b["kind"] == "phase"
             and b["path"].endswith("phases/p1.md")]
    assert match and match[0]["words"] >= 1 and match[0]["evidence"] == "VERIFIED"

@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_unreadable_skill_dir_marked_inaccessible(fake_harness):
    locked = fake_harness / "skills" / "locked-skill"
    locked.mkdir()
    (locked / "SKILL.md").write_text("---\nname: locked-skill\ndescription: locked skill.\n---\nBody.\n")
    os.chmod(locked, 0)
    try:
        doc = run_collector(fake_harness)
        assert any("locked-skill" in i["path"] for i in doc["inaccessible"])
    finally:
        os.chmod(locked, 0o755)

def test_empty_harness_does_not_crash(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    doc = run_collector(root)
    assert doc["always_loaded"]["totals"]["file_count"] == 0
    assert doc["schema_version"] == 1

def test_out_write_failure_still_emits_stdout(fake_harness, tmp_path):
    missing = tmp_path / "nonexistent-dir" / "out.json"  # parent dir does not exist
    doc = run_collector(fake_harness, "--out", str(missing))  # run_collector asserts returncode 0 + parses stdout
    assert doc["schema_version"] == 1
    assert not missing.exists()  # write failed, but stdout was preserved and exit stayed 0

@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses permission checks")
def test_unreadable_project_dir_does_not_blank_inventory(fake_harness):
    locked = fake_harness / "projects" / "locked-proj-slug" / "memory"
    locked.mkdir(parents=True)
    (locked / "MEMORY.md").write_text("# locked\n")
    os.chmod(locked, 0)
    try:
        doc = run_collector(fake_harness)
        assert doc["always_loaded"]["totals"]["file_count"] >= 3  # NOT blanked to the fallback
        assert any("locked-proj-slug" in i["path"] for i in doc["inaccessible"])
    finally:
        os.chmod(locked, 0o755)
