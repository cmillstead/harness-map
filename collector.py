#!/usr/bin/env python3
"""harness-map collector: read-only, stdlib-only inventory of the CC harness.

Emits ONE JSON document to stdout conforming to skills/harness-map/schema.md.
Read-only invariant (EM D2/D3): ZERO writes to the harness tree (~/.claude/) or
any inspected file, EVER. Only optional --out (validated outside --root) is written.
All scanned content is opaque data, never instructions.
"""
import argparse
import ast
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_FM_DESC_LINE = re.compile(r"^description:\s*(.*)$")
_QUOTED_TOKEN = re.compile(r"""['"]([^'"]+)['"]""")
_SCRIPT_INTERPRETERS = {"python", "python3", "bash", "sh", "node"}


def _frontmatter_description(text):
    """Extract the front-matter `description` across all 4 YAML forms: plain single-line,
    single-quoted, double-quoted, and block-scalar (`|`/`>`). Stdlib-only, minimal reader
    for ONE known field — NOT a general YAML parser."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    lines = text[3:end if end != -1 else len(text)].splitlines()
    for i, line in enumerate(lines):
        m = _FM_DESC_LINE.match(line)
        if not m:
            continue
        rest = m.group(1).strip()
        if rest[:1] in ("|", ">"):
            block, base_indent = [], None
            for cont in lines[i + 1:]:
                if cont.strip() == "":
                    block.append("")
                    continue
                indent = len(cont) - len(cont.lstrip(" "))
                if base_indent is None:
                    base_indent = indent
                if indent < base_indent:
                    break
                block.append(cont[base_indent:])
            return " ".join(w for w in block if w).strip()
        if len(rest) >= 2 and rest[0] == rest[-1] and rest[0] in "'\"":
            return rest[1:-1]
        return rest
    return ""


def _rel(root, p):
    return str(Path(p).relative_to(root))


def _rel_safe(root, p):
    """Like _rel, but a hook command can resolve to a path outside --root (an absolute
    interpreter/script path elsewhere on disk) — fall back to the raw string instead of
    raising ValueError."""
    try:
        return _rel(root, p)
    except ValueError:
        return str(p)


def _project_slug(project_root):
    """CC per-project memory dir name: abspath with every '/' and '.' replaced by '-'."""
    return re.sub(r"[/.]", "-", os.path.abspath(str(project_root)))


def _read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace"), "VERIFIED"
    except OSError:
        return None, "INACCESSIBLE"


def _safe_exists(path):
    """Path.exists()/is_symlink() can raise PermissionError etc. (they only swallow ENOENT/
    ENOTDIR). Treat any OSError as 'cannot determine' so one locked dir marks just that entry
    inaccessible instead of blanking the whole inventory. Returns (present, ok)."""
    try:
        return (path.exists() or path.is_symlink()), True
    except OSError:
        return False, False


def _metrics(text):
    words = len(text.split())
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return words, lines, round(words * 1.3)


def _read_checked(root, path, inaccessible, rel_root=None):
    """Read text; on INACCESSIBLE append to inaccessible[] and return None. Preserves the
    exact inaccessible-append behavior the call sites previously inlined."""
    text, evidence = _read_text(path)
    if evidence == "INACCESSIBLE":
        inaccessible.append({"path": _rel(rel_root or root, path), "reason": "unreadable"})
        return None
    return text


def _script_from_command(command, root):
    """Return (script_path | None, note | None). `note` is set when the command form is
    unsupported or yields no script token, so the caller SURFACES it (never silent)."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None, f"unparseable hook command: {command[:80]}"
    if not tokens:
        return None, None
    first = Path(tokens[0]).name
    if first == "env":
        rest = tokens[2:]
    elif first in _SCRIPT_INTERPRETERS:
        rest = tokens[1:]
    elif "/" in tokens[0] or tokens[0].endswith((".py", ".sh")):
        rest = tokens
    else:
        return None, f"unsupported hook command form: {command[:80]}"
    token = next((p for p in rest if "/" in p or p.endswith((".py", ".sh"))), None)
    if token is None:
        return None, f"no script token in hook command: {command[:80]}"
    raw = Path(token)
    if str(raw).startswith("~"):
        # Registered commands literally read "~/.claude/hooks/...": remap that literal
        # ~-path onto `root / "hooks"` (not the real home dir) so a non-default --root
        # (and every fixture in these tests) reconciles against the actual registered
        # hook path instead of the real, unrelated $HOME.
        expanded = raw.expanduser()
        try:
            return (root / "hooks") / expanded.relative_to(Path("~/.claude/hooks").expanduser()), None
        except ValueError:
            return expanded, None
    if raw.is_absolute():
        return raw, None
    # A relative directly-executable token (e.g. "./hooks/x.py") resolves against --root,
    # NEVER against the process's cwd (R6) — joining (not .resolve()) avoids symlink surprises.
    return (root / raw), None


def _dispatcher_string_literals(source):
    """String-literal constants in a dispatcher, EXCLUDING docstrings (F3)."""
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None:
                docstrings.add(doc)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value not in docstrings]


def _fallback_scan_dispatcher_literals(text, candidate_names):
    """Best-effort scan for a dispatcher that failed ast.parse (SyntaxError): look for a
    known hook script basename inside quotes on a non-comment line. NOT a general Python
    parser — used only when the source could not be parsed at all."""
    found = set()
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        for m in _QUOTED_TOKEN.finditer(code):
            base = Path(m.group(1)).name
            if base in candidate_names:
                found.add(base)
    return found


def _iter_hook_commands(settings):
    """Yield each hook `command` string registered anywhere in settings['hooks']."""
    hooks_cfg = settings.get("hooks", {})
    if not isinstance(hooks_cfg, dict):
        return
    for entries in hooks_cfg.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            # `entry.get("hooks", [])` only substitutes the default when the key is ABSENT —
            # an explicit "hooks": null yields None, and `for h in None` raises TypeError.
            entry_hooks = entry.get("hooks", [])
            if not isinstance(entry_hooks, list):
                continue
            for h in entry_hooks:
                if isinstance(h, dict) and h.get("type") == "command" and isinstance(h.get("command"), str):
                    yield h["command"]


def _file_entry(root, path, category, inaccessible, rel_root=None):
    """Read one file and build a schema `files[]`/`memory_bodies[]`-style entry.
    On OSError, append to `inaccessible` and return None."""
    text = _read_checked(root, path, inaccessible, rel_root=rel_root)
    if text is None:
        return None
    words, lines, tokens_est = _metrics(text)
    return {
        "path": _rel(rel_root or root, path),
        "category": category,
        "words": words,
        "lines": lines,
        "tokens_est": tokens_est,
        "evidence": "VERIFIED",
    }


def walk_always_loaded(root, project_root, inaccessible, errors):
    """Collect always-loaded surfaces: harness CLAUDE.md, the active project's memory
    index only (other projects' indexes go to conditional_variants), the active
    project's own CLAUDE.md (outside --root), rules/*.md, and coding-team rules."""
    files = []
    conditional_variants = []

    root_claude = root / "CLAUDE.md"
    present, ok = _safe_exists(root_claude)
    if not ok:
        inaccessible.append({"path": _rel(root, root_claude), "reason": "unreadable"})
    elif present:
        entry = _file_entry(root, root_claude, "claude_md", inaccessible)
        if entry:
            files.append(entry)

    active_slug = None
    if project_root is not None:
        active_slug = _project_slug(project_root)
        # Only count this project's CLAUDE.md if the project is registered under this
        # harness root's projects/<slug>/memory/ — otherwise --project-root defaulting to
        # an unrelated cwd would leak an unrelated CLAUDE.md into an unrelated --root's count.
        if (root / "projects" / active_slug / "memory").is_dir():
            proj_claude = Path(project_root) / "CLAUDE.md"
            present, ok = _safe_exists(proj_claude)
            if not ok:
                inaccessible.append({"path": _rel(project_root, proj_claude), "reason": "unreadable"})
            elif present:
                entry = _file_entry(root, proj_claude, "project_claude_md", inaccessible,
                                     rel_root=project_root)
                if entry:
                    files.append(entry)

    # Deliberately single-level: iterdir()/glob("*.md") only, no recursion — so there is no
    # walk to follow symlinks through. A symlinked skill/rule DIR is followed and reported
    # under its harness-relative name by design. Recursive, symlink-loop-prone trees (hooks/)
    # are handled in Task 3 with explicit name+target recording instead of a body read.
    projects_dir = root / "projects"
    if projects_dir.is_dir():
        try:
            slug_dirs = sorted(p for p in projects_dir.iterdir() if p.is_dir())
        except OSError:
            slug_dirs = []
        for slug_dir in slug_dirs:
            idx = slug_dir / "memory" / "MEMORY.md"
            present, ok = _safe_exists(idx)
            if not ok:
                # A single locked slug dir is marked inaccessible; the loop continues so one
                # bad project does not blank the rest of the inventory.
                inaccessible.append({"path": _rel(root, idx), "reason": "unreadable"})
                continue
            if not present:
                continue
            slug = slug_dir.name
            if slug == active_slug:
                entry = _file_entry(root, idx, "memory", inaccessible)
                if entry:
                    files.append(entry)
            else:
                text = _read_checked(root, idx, inaccessible)
                if text is None:
                    continue
                words, lines, tokens_est = _metrics(text)
                conditional_variants.append({
                    "path": _rel(root, idx),
                    "project_slug": slug,
                    "words": words,
                    "lines": lines,
                    "tokens_est": tokens_est,
                    "evidence": "VERIFIED",
                })

    # Note (comment per task spec): root ~/.claude/MEMORY.md does NOT exist in the live
    # harness — only the memory/ stub directory. We still count memory/MEMORY.md when present.
    stub = root / "memory" / "MEMORY.md"
    present, ok = _safe_exists(stub)
    if not ok:
        inaccessible.append({"path": _rel(root, stub), "reason": "unreadable"})
    elif present:
        entry = _file_entry(root, stub, "memory", inaccessible)
        if entry:
            files.append(entry)

    # Deliberately single-level: glob("*.md") only, no recursion into subdirectories.
    for pattern, category in ((root / "rules", "rule"), (root / "skills" / "coding-team" / "rules", "coding_team_rule")):
        if not pattern.is_dir():
            continue
        try:
            names = sorted(pattern.glob("*.md"))
        except OSError as e:
            errors.append(f"rules glob failed for {pattern}: {e}")
            continue
        for f in names:
            entry = _file_entry(root, f, category, inaccessible)
            if entry:
                files.append(entry)

    return files, conditional_variants


def collect_descriptions(root, inaccessible):
    """Collect skill/agent front-matter `description` word counts."""
    skill_descriptions = []
    agent_descriptions = []

    # Deliberately single-level: iterdir()/glob("*.md") only, no recursion — so there is no
    # walk to follow symlinks through. A symlinked skill DIR is followed and reported under
    # its harness-relative name by design.
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        try:
            skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
        except OSError:
            skill_dirs = []
        for skill_dir in skill_dirs:
            skill_md = skill_dir / "SKILL.md"
            present, ok = _safe_exists(skill_md)
            if not ok:
                inaccessible.append({"path": _rel(root, skill_md), "reason": "unreadable"})
                continue
            if not present:
                continue
            text = _read_checked(root, skill_md, inaccessible)
            if text is None:
                continue
            desc = _frontmatter_description(text)
            skill_descriptions.append({
                "name": skill_dir.name,
                "words": len(desc.split()),
                "evidence": "VERIFIED",
            })

    agents_dir = root / "agents"
    if agents_dir.is_dir():
        try:
            agent_files = sorted(agents_dir.glob("*.md"))
        except OSError:
            agent_files = []
        for f in agent_files:
            text = _read_checked(root, f, inaccessible)
            if text is None:
                continue
            desc = _frontmatter_description(text)
            agent_descriptions.append({
                "name": f.stem,
                "words": len(desc.split()),
                "evidence": "VERIFIED",
            })

    return skill_descriptions, agent_descriptions


def collect_on_demand(root, project_root, inaccessible):
    """Collect on-demand bodies: skill SKILL.md bodies, skill-internal phases/prompts/agents,
    and the active project's memory bodies (excluding the always-loaded MEMORY.md index)."""
    skills = []
    skill_internal_bodies = []
    memory_bodies = []

    # Deliberately single-level: iterdir()/glob("*.md") only, no recursion — so there is no
    # walk to follow symlinks through. A symlinked skill DIR is followed and reported under
    # its harness-relative name by design.
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        try:
            skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
        except OSError:
            skill_dirs = []
        for skill_dir in skill_dirs:
            name = skill_dir.name
            skill_md = skill_dir / "SKILL.md"
            present, ok = _safe_exists(skill_md)
            if not ok:
                inaccessible.append({"path": _rel(root, skill_md), "reason": "unreadable"})
                continue
            has_test = (skill_dir / "tests").is_dir()
            if present:
                text = _read_checked(root, skill_md, inaccessible)
                if text is not None:
                    words, lines, _ = _metrics(text)
                    skills.append({
                        "name": name,
                        "lines": lines,
                        "words": words,
                        "has_test": has_test,
                        "evidence": "VERIFIED",
                    })

            for subdir, kind in (("phases", "phase"), ("prompts", "prompt"), ("agents", "agent")):
                target = skill_dir / subdir
                if not target.is_dir():
                    continue
                try:
                    body_files = sorted(target.glob("*.md"))
                except OSError:
                    body_files = []
                for f in body_files:
                    text = _read_checked(root, f, inaccessible)
                    if text is None:
                        continue
                    words, lines, _ = _metrics(text)
                    skill_internal_bodies.append({
                        "skill": name,
                        "path": _rel(root, f),
                        "kind": kind,
                        "lines": lines,
                        "words": words,
                        "evidence": "VERIFIED",
                    })

    if project_root is not None:
        active_slug = _project_slug(project_root)
        mem_dir = root / "projects" / active_slug / "memory"
        if mem_dir.is_dir():
            try:
                mem_files = sorted(mem_dir.glob("*.md"))
            except OSError:
                mem_files = []
            for f in mem_files:
                if f.name == "MEMORY.md":
                    continue
                text = _read_checked(root, f, inaccessible)
                if text is None:
                    continue
                words, lines, _ = _metrics(text)
                memory_bodies.append({
                    "path": _rel(root, f),
                    "project_slug": active_slug,
                    "lines": lines,
                    "words": words,
                    "evidence": "VERIFIED",
                })

    return skills, skill_internal_bodies, memory_bodies


def parse_settings(root, errors):
    """Read + parse root/settings.json. On JSONDecodeError, append a message containing
    "settings.json" to errors[] and return ({}, False) so the run continues with defaults
    (absent file also returns ({}, False) silently — a missing settings.json is not an
    error). Returns (settings_dict, parsed_ok)."""
    text, evidence = _read_text(root / "settings.json")
    if evidence == "INACCESSIBLE" or text is None:
        return {}, False
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        errors.append(f"failed to parse settings.json: {e}")
        return {}, False
    if not isinstance(parsed, dict):
        errors.append("settings.json is not a JSON object; treated as empty.")
        return {}, False
    return parsed, True


def collect_permissions(settings, parsed_ok):
    perms = settings.get("permissions", {})
    if not isinstance(perms, dict):
        perms = {}
    allow = perms.get("allow", [])
    deny = perms.get("deny", [])
    ask = perms.get("ask", [])
    return {
        "allow_count": len(allow) if isinstance(allow, list) else 0,
        "deny_count": len(deny) if isinstance(deny, list) else 0,
        "ask_count": len(ask) if isinstance(ask, list) else 0,
        "evidence": "VERIFIED" if parsed_ok else "INACCESSIBLE",
    }


def _read_json_name_list(path, key, blind_spots):
    """Read a plugins/*.json file and return (sorted names, count). Absent file → ([], 0),
    NOT a gap — plugin/marketplace registries are optional. Malformed JSON → ([], 0) plus a
    blind_spots note, never a crash."""
    text, evidence = _read_text(path)
    if evidence == "INACCESSIBLE" or text is None:
        return [], 0
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        blind_spots.append(f"{path.name} exists but is not valid JSON; treated as empty.")
        return [], 0
    if not isinstance(data, dict):
        blind_spots.append(f"{path.name} is not a JSON object; ignored.")
        return [], 0
    entries = data.get(key, {})
    names = sorted(entries.keys()) if isinstance(entries, dict) else []
    return names, len(names)


def collect_config(root, settings, parsed_ok, blind_spots):
    # secret-leak guard: never serialize env values — env_keys is names ONLY.
    env = settings.get("env", {})
    env_keys = sorted(env.keys()) if isinstance(env, dict) else []

    enabled_plugins_raw = settings.get("enabledPlugins", {})
    enabled_plugins = ([{"name": k, "enabled": bool(v)} for k, v in enabled_plugins_raw.items()]
                        if isinstance(enabled_plugins_raw, dict) else [])

    marketplaces, marketplace_count = _read_json_name_list(
        root / "plugins" / "known_marketplaces.json", "marketplaces", blind_spots)
    installed_plugins, installed_plugin_count = _read_json_name_list(
        root / "plugins" / "installed_plugins.json", "installed", blind_spots)

    # The live settings.json shape is a nested object ({"sandbox": {"enabled": bool, ...}}),
    # NOT a bare bool — bool(non-empty dict) is always True, so a naive bool(settings.get(...))
    # reports sandboxing ON even when "enabled" is explicitly False. Read the nested flag.
    _sandbox_raw = settings.get("sandbox")
    sandbox = bool(_sandbox_raw.get("enabled", False)) if isinstance(_sandbox_raw, dict) else bool(_sandbox_raw)

    return {
        "env_keys": env_keys, "env_key_count": len(env_keys),
        "model": settings.get("model"),
        "cleanup_period_days": settings.get("cleanupPeriodDays", 0),
        "sandbox": sandbox,
        "enabled_plugins": enabled_plugins, "plugin_count": len(enabled_plugins),
        "marketplaces": marketplaces, "marketplace_count": marketplace_count,
        "installed_plugins": installed_plugins, "installed_plugin_count": installed_plugin_count,
        "evidence": "VERIFIED" if parsed_ok else "INACCESSIBLE",
    }


def reconcile_hooks(root, settings, inaccessible, blind_spots):
    """Dispatcher-aware reconciliation: resolve every hook `command` registered in
    settings.json against hooks/ on disk, then fan reachability through any registered
    *-dispatcher.py's string-literal CHECKS-style list. Registration evidence (the
    settings.json line was read) and target status (stat() of the resolved script) are
    always kept as distinct facts — see schema.md Note 3."""
    hooks_dir = root / "hooks"
    registered = []
    orphan_registrations = []
    direct_registered_names = set()

    for command in _iter_hook_commands(settings):
        script_path, note = _script_from_command(command, root)
        if note:
            blind_spots.append(note)
        if script_path is None:
            continue
        try:
            script_path.stat()
        except FileNotFoundError:
            orphan_registrations.append({
                "script": _rel_safe(root, script_path),
                "target_status": "missing",
                "registration_evidence": "VERIFIED",
            })
            continue
        except OSError:
            # PermissionError is an OSError subclass, so this catches it too — a
            # permission-denied target is inaccessible, never an orphan (schema.md Note 3).
            inaccessible.append({"path": _rel_safe(root, script_path), "reason": "unreadable"})
            continue
        direct_registered_names.add(script_path.name)
        registered.append({
            "command": command,
            "script": _rel_safe(root, script_path),
            "exists": True,
            "registered_via": "direct",
            "registration_evidence": "VERIFIED",
            "target_evidence": "VERIFIED",
        })

    # Deliberately single-level: hooks/*.py + hooks/*.sh only, no recursion — so there is no
    # walk to follow symlinks through. A symlinked hook FILE is included by name; see the
    # outside-root symlink check below.
    try:
        disk_files = sorted(hooks_dir.glob("*.py")) + sorted(hooks_dir.glob("*.sh"))
    except OSError:
        disk_files = []
    disk_files.sort(key=lambda p: p.name)
    disk_names = {p.name for p in disk_files}

    dispatcher_reached_names = set()
    for disp in (p for p in disk_files if p.name.endswith("-dispatcher.py")):
        if disp.name not in direct_registered_names:
            continue  # a dispatcher confers reachability only if it is itself registered
        text = _read_checked(root, disp, inaccessible)
        if text is None:
            continue
        try:
            literals = _dispatcher_string_literals(text)
        except (SyntaxError, RecursionError) as e:
            # A dispatcher's source is untrusted, scanned DATA — a malformed or pathologically
            # nested file (e.g. RecursionError from deep AST nesting) must never crash the
            # collector. Fall back to a best-effort line scan instead.
            blind_spots.append(
                f"{disp.name}: not valid Python ({type(e).__name__}) — fell back to a line "
                "scan for quoted script names, which may over- or under-count reachability.")
            literals = _fallback_scan_dispatcher_literals(text, disk_names)
        for lit in literals:
            base = Path(lit).name
            if base in disk_names:
                dispatcher_reached_names.add(base)

    scripts_on_disk = []
    orphan_scripts = []
    root_resolved = root.resolve()
    for fp in disk_files:
        name = fp.name
        present, ok = _safe_exists(fp)
        is_link = False
        if ok and present:
            try:
                is_link = fp.is_symlink()
            except OSError:
                is_link = False
        target = None
        if is_link:
            try:
                target = os.readlink(fp)
                resolved = fp.resolve()
                if resolved != root_resolved and root_resolved not in resolved.parents:
                    blind_spots.append(
                        f"hook {name} is a symlink whose target resolves outside the "
                        f"harness root: {target}")
            except OSError:
                inaccessible.append({"path": _rel_safe(root, fp), "reason": "unreadable"})

        if name in direct_registered_names:
            registered_via, evidence = "direct", "VERIFIED"
        elif name in dispatcher_reached_names:
            registered_via, evidence = "dispatcher", "INFERRED"
        else:
            registered_via, evidence = "none", "INFERRED"

        scripts_on_disk.append({
            "name": name, "is_symlink": is_link, "target": target,
            "registered_via": registered_via, "evidence": evidence,
        })
        if registered_via == "none":
            # A script may still be reached via dynamic dispatch we cannot statically see
            # (e.g. a runtime-built list rather than a string-literal CHECKS constant) — this
            # is a best-effort static classification, not proof of dead code.
            orphan_scripts.append({"name": name, "evidence": "INFERRED"})

    return {
        "registered": registered,
        "orphan_registrations": orphan_registrations,
        "scripts_on_disk": scripts_on_disk,
        "orphan_scripts": orphan_scripts,
    }


# Reused CONSTANT — canonical origin: skills/coding-team/hooks/hook-health-check.py:177-204
# check_instruction_file_lengths (threshold 200, "case study #24"). This collector
# REIMPLEMENTS the scan harness-wide (that function is coding-team-scoped). Keep the
# constant in sync; do NOT introduce a divergent threshold.
INSTRUCTION_LINE_LIMIT = 200


def flag_long_instructions(root):
    flags = []
    for pattern in ("rules/*.md", "skills/*/SKILL.md", "skills/*/*/SKILL.md",
                     "skills/*/phases/*.md", "skills/*/prompts/*.md", "skills/*/agents/*.md",
                     "commands/*.md", "agents/*.md"):
        for fp in root.glob(pattern):
            text, evidence = _read_text(fp)
            if text is None:
                continue
            n = len(text.splitlines())
            if n > INSTRUCTION_LINE_LIMIT:
                flags.append({"path": _rel(root, fp), "lines": n,
                              "threshold": INSTRUCTION_LINE_LIMIT, "evidence": evidence})
    return flags


def build_headline(always_loaded, hooks_section, instruction_length_flags):
    totals = always_loaded["totals"]
    return {
        "always_loaded_words": totals["words"],
        "always_loaded_tokens_est": totals["tokens_est"],
        "always_loaded_file_count": totals["file_count"],
        "duplicate_pair_count": 0,
        "unchecked_binary_count": 0,
        "instruction_files_over_200": len(instruction_length_flags),
        "orphan_registration_count": len(hooks_section["orphan_registrations"]),
        "orphan_script_count": len(hooks_section["orphan_scripts"]),
    }


def build_document(root, project_root):
    root = Path(root).resolve()
    inaccessible = []
    errors = []
    blind_spots = [
        "SessionStart hook emissions (runtime-only text injected at session start) are not "
        "statically collectable.",
        "MCP server runtime instructions (e.g. engram/firecrawl tool-use guidance) are not "
        "vendored as local files.",
        "Other projects' CLAUDE.md files (outside --project-root) are not read; only their "
        "memory/MEMORY.md index is inventoried as a conditional_variant.",
        "Knowledge-base/wiki documents cited by rules but hosted outside this repo are not "
        "fetched or verified.",
    ]

    files, conditional_variants = walk_always_loaded(root, project_root, inaccessible, errors)
    skill_descriptions, agent_descriptions = collect_descriptions(root, inaccessible)
    skills, skill_internal_bodies, memory_bodies = collect_on_demand(root, project_root, inaccessible)

    settings, settings_parsed_ok = parse_settings(root, errors)
    hooks_section = reconcile_hooks(root, settings, inaccessible, blind_spots)
    permissions_section = collect_permissions(settings, settings_parsed_ok)
    config_section = collect_config(root, settings, settings_parsed_ok, blind_spots)
    instruction_length_flags = flag_long_instructions(root)

    totals = {
        "words": sum(f["words"] for f in files),
        "tokens_est": sum(f["tokens_est"] for f in files),
        "file_count": len(files),
    }

    always_loaded = {
        "files": files,
        "conditional_variants": conditional_variants,
        "skill_descriptions": skill_descriptions,
        "agent_descriptions": agent_descriptions,
        "totals": totals,
    }

    on_demand = {
        "skills": skills,
        "skill_internal_bodies": skill_internal_bodies,
        "memory_bodies": memory_bodies,
    }

    doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "headline": build_headline(always_loaded, hooks_section, instruction_length_flags),
        "always_loaded": always_loaded,
        "on_demand": on_demand,
        "enforcement": {
            "hooks": hooks_section,
            "permissions": permissions_section,
        },
        "config": config_section,
        "instruction_length_flags": instruction_length_flags,
        "duplication": {"shingle_k": 8, "metric": "containment", "threshold": 0.6, "pairs": []},
        "phantom_refs": [],
        "promotion_candidates": [],
        "test_coverage": {
            "hooks": [], "skills": [],
            "summary": {"hooks_with_test": 0, "hooks_total": 0, "skills_with_test": 0, "skills_total": 0},
        },
        "inaccessible": inaccessible,
        "blind_spots": blind_spots,
        "errors": errors,
    }
    return doc


def main():
    parser = argparse.ArgumentParser(description="harness-map collector: read-only harness inventory.")
    parser.add_argument("--root", default=str(Path.home() / ".claude"))
    parser.add_argument("--project-root", default=os.getcwd())
    parser.add_argument("--out", default=None)
    parser.add_argument("--indent", type=int, default=2)
    args = parser.parse_args()

    try:
        doc = build_document(args.root, args.project_root)
    except OSError as e:
        doc = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "root": str(Path(args.root).resolve()) if Path(args.root).exists() else args.root,
            "headline": {
                "always_loaded_words": 0, "always_loaded_tokens_est": 0, "always_loaded_file_count": 0,
                "duplicate_pair_count": 0, "unchecked_binary_count": 0, "instruction_files_over_200": 0,
                "orphan_registration_count": 0, "orphan_script_count": 0,
            },
            "always_loaded": {"files": [], "conditional_variants": [], "skill_descriptions": [],
                               "agent_descriptions": [], "totals": {"words": 0, "tokens_est": 0, "file_count": 0}},
            "on_demand": {"skills": [], "skill_internal_bodies": [], "memory_bodies": []},
            "enforcement": {"hooks": {"registered": [], "orphan_registrations": [],
                                       "scripts_on_disk": [], "orphan_scripts": []},
                             "permissions": {"allow_count": 0, "deny_count": 0, "ask_count": 0,
                                              "evidence": "INACCESSIBLE"}},
            "config": {"env_keys": [], "env_key_count": 0, "model": None, "cleanup_period_days": 0,
                       "sandbox": False, "enabled_plugins": [], "plugin_count": 0, "marketplaces": [],
                       "marketplace_count": 0, "installed_plugins": [], "installed_plugin_count": 0,
                       "evidence": "INACCESSIBLE"},
            "instruction_length_flags": [], "duplication": {"shingle_k": 8, "metric": "containment",
                                                              "threshold": 0.6, "pairs": []},
            "phantom_refs": [], "promotion_candidates": [],
            "test_coverage": {"hooks": [], "skills": [], "summary": {"hooks_with_test": 0, "hooks_total": 0,
                                                                       "skills_with_test": 0, "skills_total": 0}},
            "inaccessible": [], "blind_spots": [], "errors": [f"fatal collector error: {e}"],
        }

    text = json.dumps(doc, indent=args.indent)
    print(text)  # stdout is the primary contract — always emit the built document first
    if args.out:
        try:
            out_path = Path(args.out).resolve()
            root_path = Path(args.root).resolve()
            if root_path in out_path.parents or out_path == root_path:
                print(json.dumps({"error": "--out must be outside --root (read-only invariant)"}), file=sys.stderr)
            else:
                out_path.write_text(text, encoding="utf-8")
        except (OSError, ValueError) as e:
            print(json.dumps({"error": f"--out write failed: {type(e).__name__}: {e}"}), file=sys.stderr)


if __name__ == "__main__":
    main()
