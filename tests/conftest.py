import json
import os
import re
import textwrap
import pytest

def project_slug(path):
    # CC per-project memory dir name: absolute path with every "/" and "." replaced by "-".
    # e.g. /Users/<user>/.claude -> -Users-<user>--claude   (matches the live projects/<slug>/ layout)
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


@pytest.fixture
def pathological_harness(fake_harness):
    """TRK-049: layers a family of pathological hook/settings/corpus shapes onto
    `fake_harness`, each pinned to one of the two "suite green, collector broken"
    instances TRK-025 shipped without catching (see collector.py's
    `_script_from_command`, `_references_script_token`, `_has_shell_control_syntax`,
    `_looks_like_existing_hook_script`). Takes `fake_harness` as a parameter and mutates
    ONLY what it adds -- `fake_harness`'s own golden shape (settings.json's `"hooks": {}`
    included) is otherwise untouched, so a test that requests plain `fake_harness` (e.g.
    the A16 golden in test_collector.py) is unaffected: this fixture must be requested
    explicitly to see any of it.

    Six shapes, one settings.json, one corpus file:
      1. `long_single_token` -- a 2000-char command with no whitespace/"/" (instance 1's
         exact crash shape, scaled past the measured live 1948-char command): the WHOLE
         command tokenizes as ONE shlex token whose `.name` exceeds
         `_MAX_SCRIPT_TOKEN_LEN` (255) (a real ENAMETOOLONG risk on this filesystem --
         verified directly against a 2000-char path during T1). Scope note, so this
         docstring does not overclaim: `_looks_like_existing_hook_script` carries TWO
         independent defenses -- the length guard AND a wrapping `except OSError` -- and
         what the tests over this shape pin is the COMBINED pair, not the length guard in
         isolation. Deleting either one alone still returns False before a crash reaches
         the document; only a full revert of BOTH reddens them.
      2. `bracket_commands` -- 8 `[ ... ] && ...` compounds: instance 2's exact live
         shape, the `[` first token the shipped name-allowlist fix was inert against.
      3. `nested_quote_command` -- deeply nested single/double quoting, with the `&&`
         control operator OUTSIDE the quoted argument, so shlex genuinely splits it into
         a standalone token and the command IS actually shell-interpreted (a real `&&`
         compound with a heavily-quoted first operand). The complementary case -- `&&`
         embedded INSIDE the quotes -- classifies `unparsed`, not `no_script`, under A62's
         quote-aware rule: an operator only counts as shell control syntax OUTSIDE quotes
         (`$(` and a backtick are the one exception, and only inside double quotes). That
         case is pinned in test_collector.py's `test_shell_control_syntax_quote_awareness_*`
         tests, not here -- adding it to this fixture would move `commands_unparsed` from
         1 to 2 and break several counts this fixture's tests already pin.
      4. `unbalanced_quote_command` -- `shlex.split` raises ValueError: a genuine,
         disclosed coverage gap, kept singular so tests can assert it is the ONLY
         `commands_unparsed` contributor here.
      5. a rules/*.md file containing invalid UTF-8 bytes (read with errors="replace").
      6. an oversized single settings scalar (an `env` value) -- must never serialize
         (config.env_keys is names-only, CLAUDE.md binding rule 11).
    """
    root = fake_harness
    long_single_token = "{" + ("a" * 1998) + "}"  # 2000 chars total; no whitespace, no
                                                    # "/", not "env"/an interpreter name
    bracket_commands = [f"[ -f flag{i} ] && echo {i}" for i in range(8)]
    # The `&&` sits OUTSIDE the double-quoted argument, so shlex splits it into its own
    # standalone token -- this genuinely IS shell control syntax under A62's quote-aware
    # rule, unlike a quoted `&&` (which classifies `unparsed`; see the docstring above and
    # test_collector.py's `test_shell_control_syntax_quote_awareness_*` tests).
    nested_quote_command = """rtk hook "a 'b \\"c\\" b' a" && z"""
    unbalanced_quote_command = "echo 'unterminated"

    settings = json.loads((root / "settings.json").read_text())
    settings["hooks"] = {"PostToolUse": [{"hooks": [
        {"type": "command", "command": c} for c in [
            long_single_token, *bracket_commands, nested_quote_command, unbalanced_quote_command,
        ]
    ]}]}
    settings["env"]["PATHOLOGICAL_HUGE_VALUE"] = "v" * 200_000
    (root / "settings.json").write_text(json.dumps(settings))

    (root / "rules" / "nonutf8.md").write_bytes(
        b"# Rule\n" + bytes([0xff, 0xfe, 0x80, 0x81]) + b" not valid utf-8\n")

    return root
