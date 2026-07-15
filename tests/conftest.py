import json
import os
import re
import textwrap
import pytest

def project_slug(path):
    # CC per-project memory dir name: absolute path with every "/" and "." replaced by "-".
    # e.g. /Users/cevin/.claude -> -Users-cevin--claude   (matches the live projects/<slug>/ layout)
    return re.sub(r"[/.]", "-", os.path.abspath(str(path)))

@pytest.fixture
def fake_harness(tmp_path):
    root = tmp_path / "claude"
    proj = tmp_path / "active-repo"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "CLAUDE.md").write_text("# Active project instructions\n" + "pword " * 25)
    slug = project_slug(proj)
    for d in ("rules", "skills/coding-team/rules", "skills/demo/phases", "commands",
              "agents", "hooks", "memory", "plugins",
              f"projects/{slug}/memory", "projects/other-proj-slug/memory"):
        (root / d).mkdir(parents=True, exist_ok=True)
    (root / "CLAUDE.md").write_text("# Root\n" + "word " * 40)
    (root / "projects" / slug / "memory" / "MEMORY.md").write_text("# Active Memory Index\n- item one\n")
    (root / "projects" / slug / "memory" / "detail.md").write_text("Detail body " * 12)
    (root / "projects" / "other-proj-slug" / "memory" / "MEMORY.md").write_text("# Other Index\n- other\n")
    (root / "memory" / "MEMORY.md").write_text("# stub\n")
    (root / "skills" / "demo" / "phases" / "p1.md").write_text("Phase one body " * 8)
    (root / "rules" / "a.md").write_text("Rule A body " * 10)
    (root / "rules" / "b.md").write_text("Rule B body " * 10)
    (root / "skills" / "coding-team" / "rules" / "c.md").write_text("Rule C body " * 10)
    (root / "skills" / "demo" / "SKILL.md").write_text(textwrap.dedent("""\
        ---
        name: demo
        description: A demo skill used only in tests.
        ---
        # demo
        Body line.
    """))
    (root / "commands" / "demo-cmd.md").write_text("---\nname: demo-cmd\ndescription: demo command.\n---\nBody.\n")
    (root / "agents" / "demo-agent.md").write_text(textwrap.dedent("""\
        ---
        name: demo-agent
        description: A demo agent used only in tests.
        ---
        Agent body.
    """))
    (root / "settings.json").write_text(json.dumps({
        "hooks": {}, "permissions": {"allow": [], "deny": []},
        "env": {"FAKE_TOKEN": "s3cr3t-should-never-appear", "ENABLE_X": "1"},
        "model": "opus[1m]", "cleanupPeriodDays": 3650, "sandbox": {"enabled": True},
        "enabledPlugins": {"demo-plugin@official": True, "off-plugin@official": False}}))
    (root / "plugins" / "known_marketplaces.json").write_text(json.dumps(
        {"marketplaces": {"official": {"url": "x"}, "community": {"url": "y"}}}))
    (root / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"installed": {"demo-plugin@official": {"version": "1.0"}}}))
    return root
