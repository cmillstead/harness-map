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
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_FM_DESC_LINE = re.compile(r"^description:\s*(.*)$")
_QUOTED_TOKEN = re.compile(r"""['"]([^'"]+)['"]""")
_NORM_RE = re.compile(r"[^a-z0-9]+")
_SCRIPT_INTERPRETERS = {"python", "python3", "bash", "sh", "node"}

# --- phantom-ref / promotion-candidate scanning constants ---
_GENERIC_BACKTICK_RE = re.compile(r"`([^`]+)`")
_PATH_EXT_RE = re.compile(r"[\w./~-]+\.(?:md|py|sh|json)")
_ENV_FLAG_NAME_RE = re.compile(r"^([A-Z][A-Z0-9_]{4,})(?:=.*)?$")
_ENV_FLAG_SHAPE_RE = re.compile(r"_ALLOW_|_SKIP_|GUARD|WRITE_")
_NEVER_RE = re.compile(r"\bNEVER\b")
_ALWAYS_RE = re.compile(r"\bALWAYS\b")
_MUST_RE = re.compile(r"\bmust\b")
_NUMERIC_CAP_RE = re.compile(r"≤\s*\d+|>\s*\d+\s*lines?|\bunder\s+\d+\b|\bat\s+most\s+\d+\b",
                              re.IGNORECASE)
_REQUIRED_FILE_RE = re.compile(r"requires?\s+`[^`]+`|\bmust\s+exist\b", re.IGNORECASE)
_PROMOTION_PATTERNS = (
    ("NEVER", _NEVER_RE),
    ("ALWAYS", _ALWAYS_RE),
    ("must", _MUST_RE),
    ("numeric_cap", _NUMERIC_CAP_RE),
    ("required_file", _REQUIRED_FILE_RE),
)
# Common English words long enough (>=4 chars) to spuriously "match" a hook body during the
# advisory hook_covered cross-reference — excluded so the heuristic isn't trivially noisy.
_HOOK_COVERED_STOPWORDS = {
    "never", "always", "must", "this", "that", "with", "from", "have", "will", "your",
    "into", "when", "than", "then", "files", "keep", "before", "after", "workflow",
    "details", "instruction", "instructions", "under", "lines", "line", "requires",
    "exist", "commit", "secrets", "tests", "committing",
}


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


def _physical_key(path):
    """Resolved physical identity, for deduping a file reachable via multiple glob paths
    (a deploy symlink in rules/ or agents/ pointing at the submodule source). Guarded so a
    broken symlink can't crash the walk."""
    try:
        return os.path.realpath(str(path))
    except OSError:
        return str(path)


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


def _resolves_inside_root(candidate, root, root_stat):
    """True if `candidate` is root or lies inside it. Robust beyond string comparison:
    (1) lexical/resolved parents check (case-sensitive, kept for defense in depth); PLUS
    (2) an st_dev/st_ino identity check (os.path.samestat) against root for `candidate` and
    each of its EXISTING ancestors — this catches a case-insensitive collision on APFS
    (e.g. `.CLAUDE` vs `.claude`) and directory hard-link / bind-mount aliases that the
    string checks miss, consistent with main()'s existing hard-link write defense."""
    if candidate == root or root in candidate.parents:
        return True
    for anc in (candidate, *candidate.parents):
        try:
            st = os.stat(anc)
        except OSError:
            continue  # a not-yet-existent ancestor (the out-file itself) — nothing to compare
        if os.path.samestat(st, root_stat):
            return True
    return False


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
    # A file reachable via multiple glob paths (a rules/ deploy symlink pointing at its
    # skills/coding-team/rules/ submodule source) is ONE physical file and must be counted
    # ONCE. `seen` covers every append to `files` below, in append order — so a symlinked
    # rule is counted under its deployed/always-loaded location (rules/, seen first) and the
    # submodule-source duplicate is skipped. `conditional_variants` is NOT deduped against
    # this set: those are different projects' distinct MEMORY.md files, not glob duplicates.
    seen = set()

    root_claude = root / "CLAUDE.md"
    present, ok = _safe_exists(root_claude)
    if not ok:
        inaccessible.append({"path": _rel(root, root_claude), "reason": "unreadable"})
    elif present:
        key = _physical_key(root_claude)
        if key not in seen:
            entry = _file_entry(root, root_claude, "claude_md", inaccessible)
            if entry:
                files.append(entry)
                seen.add(key)

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
                key = _physical_key(proj_claude)
                if key not in seen:
                    entry = _file_entry(root, proj_claude, "project_claude_md", inaccessible,
                                         rel_root=project_root)
                    if entry:
                        files.append(entry)
                        seen.add(key)

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
                key = _physical_key(idx)
                if key not in seen:
                    entry = _file_entry(root, idx, "memory", inaccessible)
                    if entry:
                        files.append(entry)
                        seen.add(key)
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
        key = _physical_key(stub)
        if key not in seen:
            entry = _file_entry(root, stub, "memory", inaccessible)
            if entry:
                files.append(entry)
                seen.add(key)

    # Deliberately single-level: glob("*.md") only, no recursion into subdirectories.
    # root/rules/*.md is scanned FIRST so a rule reachable via BOTH a rules/ deploy symlink and
    # a sub-skill's rules/ source is counted ONCE under rules/ (category "rule") — the physical
    # `seen` dedup below drops the later sub-skill duplicate. Generalized from coding-team-only to
    # any skills/*/rules/ for release portability; coding-team keeps its "coding_team_rule" label
    # (baseline continuity), every other sub-skill's rules get "skill_rule".
    rule_dirs = [(root / "rules", "rule")]
    skills_root = root / "skills"
    try:
        skills_root_is_dir = skills_root.is_dir()
    except OSError as e:
        errors.append(f"skills is_dir failed for {skills_root}: {e}")
        skills_root_is_dir = False
    if skills_root_is_dir:
        try:
            sub_skill_dirs = sorted(p for p in skills_root.iterdir() if p.is_dir())
        except OSError as e:
            errors.append(f"skills iterdir failed for {skills_root}: {e}")
            sub_skill_dirs = []
        for skill_dir in sub_skill_dirs:
            sub_rules = skill_dir / "rules"
            try:
                is_rules_dir = sub_rules.is_dir()
            except OSError as e:
                errors.append(f"rules is_dir check failed for {sub_rules}: {e}")
                continue
            if is_rules_dir:
                category = "coding_team_rule" if skill_dir.name == "coding-team" else "skill_rule"
                rule_dirs.append((sub_rules, category))
    for rules_dir, category in rule_dirs:
        try:
            if not rules_dir.is_dir():
                continue
        except OSError as e:
            errors.append(f"rules is_dir failed for {rules_dir}: {e}")
            continue
        try:
            names = sorted(rules_dir.glob("*.md"))
        except OSError as e:
            errors.append(f"rules glob failed for {rules_dir}: {e}")
            continue
        for f in names:
            key = _physical_key(f)
            if key in seen:
                continue
            entry = _file_entry(root, f, category, inaccessible)
            if entry:
                files.append(entry)
                seen.add(key)

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


def parse_settings(root, errors, blind_spots):
    """Read + parse root/settings.json. Two distinct outcomes, both NON-fatal —
    build_document always continues and populates every settings-INDEPENDENT section
    (always_loaded, hooks, duplication, phantom_refs, ...), because `headline` is the
    run-to-run diff unit and a one-file settings problem must never fabricate a false
    "everything vanished" diff:
    - Genuinely ABSENT (FileNotFoundError, and settings_path is NOT a symlink): the
      common, expected case — nothing wrong. Silent: ({}, False) plus a blind_spot note,
      no errors[] entry.
    - PRESENT but unreadable-as-a-file (any other OSError — IsADirectoryError,
      PermissionError, ELOOP — or JSONDecodeError; OR a FileNotFoundError where
      settings_path IS a symlink, i.e. a PRESENT-but-BROKEN symlink whose target does
      not exist — is_symlink() is True even for a dangling target, so this is
      distinguished from genuine absence): a real anomaly, symmetric handling across all
      these cases — record a descriptive errors[] entry and return ({}, False) so the
      run continues with config evidence INACCESSIBLE, same shape as the absent case,
      just LOUD instead of silent. (main()'s top-level `except Exception` guard remains
      a defense-in-depth backstop for anything unanticipated; it no longer has an
      organic trigger via settings.json specifically — the intended, more robust
      outcome.) Returns (settings_dict, parsed_ok)."""
    settings_path = root / "settings.json"
    try:
        text = settings_path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        if settings_path.is_symlink():
            errors.append("settings.json is a broken symlink (target does not exist)")
            return {}, False
        blind_spots.append("settings.json not found; permissions/config/hooks reflect defaults.")
        return {}, False
    except OSError as e:
        errors.append(f"settings.json unreadable: {e!r}")
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


def _hook_disk_files(root):
    """hooks/*.py + hooks/*.sh on disk, name-sorted, never raising (an unreadable hooks/
    dir yields []). Deliberately single-level: no recursion, so there is no walk to
    follow symlinks through — a symlinked hook FILE is included by name. Shared by
    reconcile_hooks and _detect_hook_test_coverage, which both need the identical
    guarded + sorted listing before diverging into their own downstream logic."""
    hooks_dir = root / "hooks"
    try:
        disk_files = sorted(list(hooks_dir.glob("*.py")) + list(hooks_dir.glob("*.sh")),
                             key=lambda p: p.name)
    except OSError:
        return []
    return disk_files


def reconcile_hooks(root, settings, inaccessible, blind_spots):
    """Dispatcher-aware reconciliation: resolve every hook `command` registered in
    settings.json against hooks/ on disk, then fan reachability through any registered
    *-dispatcher.py's string-literal CHECKS-style list. Registration evidence (the
    settings.json line was read) and target status (stat() of the resolved script) are
    always kept as distinct facts — see schema.md Note 3."""
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

    # See the outside-root symlink check below for the symlinked-hook-FILE handling note.
    disk_files = _hook_disk_files(root)
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


# --- Task 3B: watched-input glob sets — single source of truth shared between each
# collector scan and iter_input_paths(), so the live-dashboard filesystem watcher (T4)
# cannot drift out of sync with what the collector actually reads. Each tuple is consumed
# BOTH by the collector function named in its comment AND by iter_input_paths(); add a new
# collector input glob HERE (never inline it in a scan) so the watcher automatically sees it.
_INSTRUCTION_GLOBS = ("rules/*.md", "skills/*/rules/*.md", "skills/*/SKILL.md",
                      "skills/*/*/SKILL.md", "skills/*/phases/*.md", "skills/*/prompts/*.md",
                      "skills/*/agents/*.md", "commands/*.md", "agents/*.md")  # flag_long_instructions
_DUP_GLOBS = ("rules/*.md", "skills/*/rules/*.md", "skills/*/SKILL.md",
              "skills/*/phases/*.md", "agents/*.md", "commands/*.md")  # scan_duplication
_STALENESS_RULE_GLOBS = ("rules/*.md", "skills/*/rules/*.md")  # _staleness_corpus (+ CLAUDE.md)
_HOOK_SCRIPT_GLOBS = ("hooks/*.py", "hooks/*.sh")  # mirrors _hook_disk_files / _hooks_body_corpus
_HOOK_TEST_GLOBS = ("hooks/tests/*.py", "skills/*/hooks/tests/*.py")  # mirrors _hook_test_stems


def flag_long_instructions(root):
    flags = []
    # `seen` dedupes a file reachable via multiple glob paths (a deploy symlink under
    # agents/ + its canonical submodule source under skills/*/agents/). The glob order
    # below lists skills/*/agents/*.md BEFORE agents/*.md, so the canonical path (the one
    # you'd actually edit to shorten the file) is seen first and reported; the deploy-symlink
    # duplicate is skipped.
    seen = set()
    # skills/*/rules/*.md generalizes the coding-team-only rules scan (release portability,
    # matching walk_always_loaded/scan_duplication/_staleness_corpus); listed right after
    # rules/*.md so a physically-symlinked rule is seen/reported under its rules/*.md path
    # first, same precedence as those three scans.
    for pattern in _INSTRUCTION_GLOBS:
        for fp in root.glob(pattern):
            key = _physical_key(fp)
            if key in seen:
                continue
            text, evidence = _read_text(fp)
            if text is None:
                continue
            seen.add(key)
            n = len(text.splitlines())
            if n > INSTRUCTION_LINE_LIMIT:
                flags.append({"path": _rel(root, fp), "lines": n,
                              "threshold": INSTRUCTION_LINE_LIMIT, "evidence": evidence})
    return flags


SHINGLE_K = 8
DUP_THRESHOLD = 0.6
MAX_SHINGLES_PER_FILE = 4000
MAX_FILE_BYTES = 200_000
MAX_PAIRS = 50


def _containment(a_set, b_set):
    smaller = min(len(a_set), len(b_set))
    if smaller == 0:
        return 0.0
    return len(a_set & b_set) / smaller  # |A∩B| / min(|A|,|B|)


def _normalize_words(text):
    """Lowercase, then replace (never delete) markdown punctuation with a space so
    "a.b" tokenizes as two words "a", "b" rather than merging into "ab"."""
    return _NORM_RE.sub(" ", text.lower()).split()


def _ordered_capped_shingles(words, k=SHINGLE_K, cap=MAX_SHINGLES_PER_FILE):
    """Overlapping k-word shingles in document order, deduped and capped deterministically:
    the FIRST `cap` DISTINCT shingles by document order are retained — never a set(...)
    truncation, whose iteration order is unspecified and would make output non-deterministic
    across runs/interpreters for a file that exceeds the cap."""
    ordered = []
    seen = set()
    for i in range(len(words) - k + 1):
        sh = " ".join(words[i:i + k])
        if sh in seen:
            continue
        seen.add(sh)
        ordered.append(sh)
        if len(ordered) >= cap:
            break
    return set(ordered)


def scan_duplication(root, blind_spots):
    """Candidate near-duplicate pairs by containment coefficient (|A∩B| / min(|A|,|B|))
    over k=8 word shingles — chosen over Jaccard because it correctly flags a short file
    fully subsumed by a longer one (schema.md Note 2). SIGNALS only: this is a candidate
    list. Deciding "one declared home + callers" for a pair is a synthesis-pass JUDGMENT,
    not something this collector decides."""
    # Generalized skills/coding-team/rules -> skills/*/rules for release portability; the
    # seen_physical dedup below still collapses a rule reachable via multiple glob paths.
    seen_physical = set()
    corpus = []  # [(rel_path, shingle_set), ...]
    for pattern in _DUP_GLOBS:
        try:
            candidates = sorted(root.glob(pattern))
        except OSError:
            candidates = []
        for fp in candidates:
            # A file reachable via multiple glob paths (a rules/ deploy symlink pointing at
            # its skills/coding-team/rules/ submodule source) is ONE physical file — it must
            # never be compared against itself as a false-positive duplicate pair.
            key = _physical_key(fp)
            if key in seen_physical:
                continue
            seen_physical.add(key)
            try:
                size = fp.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                blind_spots.append(
                    f"{_rel(root, fp)} exceeds {MAX_FILE_BYTES} bytes; skipped in duplication scan.")
                continue
            text, _ = _read_text(fp)
            if text is None:
                continue
            words = _normalize_words(text)
            shingles = _ordered_capped_shingles(words)
            if not shingles:
                blind_spots.append(
                    f"{_rel(root, fp)} has fewer than {SHINGLE_K} normalized words; "
                    "skipped in duplication scan.")
                continue
            corpus.append((_rel(root, fp), shingles))

    pairs = []
    for i in range(len(corpus)):
        path_a, set_a = corpus[i]
        for j in range(i + 1, len(corpus)):
            path_b, set_b = corpus[j]
            score = _containment(set_a, set_b)
            if score < DUP_THRESHOLD:
                continue
            shared = set_a & set_b
            sample = min(shared) if shared else ""
            a, b = sorted((path_a, path_b))
            pairs.append({"a": a, "b": b, "score": score, "shared_sample": sample,
                          "evidence": "INFERRED"})

    # Deterministic across runs, including when a file exceeds the shingle cap: sort by
    # (-score, a, b), then cap to the top MAX_PAIRS.
    pairs.sort(key=lambda p: (-p["score"], p["a"], p["b"]))
    pairs = pairs[:MAX_PAIRS]

    return {
        "shingle_k": SHINGLE_K,
        "metric": "containment",
        "threshold": DUP_THRESHOLD,
        "pairs": pairs,
    }


def _hooks_body_corpus(root):
    """Concatenated hooks/*.py + hooks/*.sh bodies, ORIGINAL case, for literal env-flag
    grep and the promotion-candidate hook_covered cross-reference.
    Caveat: a hook that reads the flag name from a variable rather than a literal string
    (os.environ[SOME_VAR] indirection) is invisible to this substring check — a
    false-positive "phantom" env flag is possible. Best-effort only."""
    parts = []
    hooks_dir = root / "hooks"
    if hooks_dir.is_dir():
        for pattern in ("*.py", "*.sh"):
            try:
                candidates = sorted(hooks_dir.glob(pattern))
            except OSError:
                candidates = []
            for fp in candidates:
                text, _ = _read_text(fp)
                if text:
                    parts.append(text)
    return "\n".join(parts)


def _staleness_corpus(root, inaccessible):
    """Corpus for phantom-ref + promotion-candidate scanning: rules/*.md,
    skills/coding-team/rules/*.md, and the harness CLAUDE.md — deduped by physical
    identity so a symlinked rule (deploy path + submodule source) is scanned once."""
    seen = set()
    corpus = []
    paths = []
    # Generalized skills/coding-team/rules -> skills/*/rules for release portability; deduped by
    # physical identity so a symlinked rule (deploy path + sub-skill source) is scanned once.
    for pattern in _STALENESS_RULE_GLOBS:
        try:
            paths.extend(sorted(root.glob(pattern)))
        except OSError:
            pass
    claude = root / "CLAUDE.md"
    present, ok = _safe_exists(claude)
    if ok and present:
        paths.append(claude)
    for fp in paths:
        key = _physical_key(fp)
        if key in seen:
            continue
        seen.add(key)
        text = _read_checked(root, fp, inaccessible)
        if text is None:
            continue
        corpus.append((_rel(root, fp), text))
    return corpus


def _looks_like_path_token(token):
    return bool(_PATH_EXT_RE.fullmatch(token)) or "/" in token


def check_phantom_refs(root, corpus_files, inaccessible):
    """Backtick-quoted path and env-flag tokens that don't resolve to anything real. A
    path OUTSIDE --root is reported as kind="external" (INFERRED, resolved: null) — the
    collector never claims a file outside its scanned scope is phantom; it genuinely
    cannot see it either way, so it only classifies, never asserts absence."""
    refs = []
    seen = set()
    hooks_corpus = _hooks_body_corpus(root)

    for rel_path, text in corpus_files:
        for m in _GENERIC_BACKTICK_RE.finditer(text):
            token = m.group(1)
            if re.search(r"\s", token):
                # A legitimate single-line path/env-flag backtick token never contains
                # whitespace. A match containing whitespace (space OR newline) means the
                # regex paired mismatched backticks — across a fenced code block with no
                # internal backticks, a markdown table, or an unrelated stray backtick
                # elsewhere in the prose — never a real ref. Reject rather than surface a
                # garbage multi-word/multi-line "ref".
                continue
            if _looks_like_path_token(token):
                norm = re.sub(r"^~/\.claude/?", "", token)
                if norm.startswith("/") or norm.startswith("~"):
                    key = (rel_path, norm, "external")
                    if key not in seen:
                        seen.add(key)
                        refs.append({"source": rel_path, "ref": norm, "kind": "external",
                                     "resolved": None, "evidence": "INFERRED"})
                    continue
                candidate = root / norm
                present, ok = _safe_exists(candidate)
                if not ok:
                    inaccessible.append({"path": _rel_safe(root, candidate), "reason": "unreadable"})
                    continue
                if present:
                    continue
                key = (rel_path, norm, "path")
                if key not in seen:
                    seen.add(key)
                    refs.append({"source": rel_path, "ref": norm, "kind": "path",
                                 "resolved": False, "evidence": "VERIFIED"})
                continue
            env_match = _ENV_FLAG_NAME_RE.match(token)
            if env_match:
                name = env_match.group(1)
                if _ENV_FLAG_SHAPE_RE.search(name) and name not in hooks_corpus:
                    key = (rel_path, name, "env_flag")
                    if key not in seen:
                        seen.add(key)
                        refs.append({"source": rel_path, "ref": name, "kind": "env_flag",
                                     "resolved": False, "evidence": "INFERRED"})
    return refs


def _excerpt_around(text, start, end, radius=60):
    lo = max(0, start - radius)
    hi = min(len(text), end + radius)
    return text[lo:hi].strip().replace("\n", " ")


def _hook_covered(excerpt, trigger_text, hooks_corpus_lower):
    """Best-effort cross-reference: does any SPECIFIC token from the excerpt — a
    snake_case identifier (contains `_`), or a path/filename (contains `/` or `.`) —
    appear in the hooks corpus (hook script bodies + registered settings.json commands)?
    Plain English words never qualify, even if >=4 chars and absent from the stopword
    list: against a corpus the size of the whole harness, common words leak through and
    make the signal meaningless, so only tokens that plausibly NAME a real enforcement
    target (a symbol, path, or filename) are considered. A hit means synthesis should
    propose EXTENDING that existing hook rather than proposing a new one — this
    collector only surfaces the raw signal."""
    if not hooks_corpus_lower:
        return False
    trigger_lower = trigger_text.lower()
    tokens = set(re.findall(r"[a-zA-Z_]{4,}", excerpt))
    tokens.update(re.findall(r"[A-Za-z0-9_]+(?:[./][A-Za-z0-9_-]+)+", excerpt))
    for w in tokens:
        wl = w.lower()
        if wl == trigger_lower or wl in _HOOK_COVERED_STOPWORDS:
            continue
        if not ("_" in wl or "/" in wl or "." in wl):
            continue
        if wl in hooks_corpus_lower:
            return True
    return False


def collect_promotion_candidates(root, corpus_files, settings):
    """Prose in an instruction file that reads like a hard rule (NEVER/ALWAYS/must, a
    numeric cap, a required-file assertion) but may have no corresponding hook enforcing
    it. Advisory SIGNALS only — synthesis proposes extending an EXISTING covered hook
    before creating a new one; this collector never makes that judgment itself."""
    candidates = []
    hooks_corpus_lower = _hooks_body_corpus(root).lower()
    commands_lower = "\n".join(_iter_hook_commands(settings)).lower()
    combined_lower = hooks_corpus_lower + "\n" + commands_lower

    for rel_path, text in corpus_files:
        for pattern_name, regex in _PROMOTION_PATTERNS:
            for m in regex.finditer(text):
                excerpt = _excerpt_around(text, m.start(), m.end())
                hook_covered = _hook_covered(excerpt, m.group(0), combined_lower)
                candidates.append({
                    "source": rel_path,
                    "pattern": pattern_name,
                    "excerpt": excerpt,
                    "hook_covered": hook_covered,
                    "evidence": "INFERRED",
                })
    return candidates


def _hook_test_stems(root, errors):
    """Normalized (snake_case) stems named by test files under hooks/tests/ and
    skills/*/hooks/tests/ (generalized from the coding-team-only scope for release
    portability) — "test_guard.py" and "guard_test.py" both yield "guard". Read-only,
    single-level glob per dir (no recursion needed: hook tests live directly in these
    known locations). `errors` is the shared build_document errors[] list — an
    inaccessible ancestor is disclosed there rather than silently swallowed."""
    stems = set()
    # Generalized skills/coding-team/hooks/tests -> skills/*/hooks/tests for release portability.
    # `stems` is a set, so union order is irrelevant; baseline-stable because coding-team is the
    # only sub-skill with a hooks/tests dir on this harness.
    test_dirs = [root / "hooks" / "tests"]
    skills_root = root / "skills"
    try:
        skills_root_is_dir = skills_root.is_dir()
    except OSError as e:
        errors.append(f"skills is_dir failed for {skills_root}: {e}")
        skills_root_is_dir = False
    if skills_root_is_dir:
        try:
            skill_dirs = sorted(p for p in skills_root.iterdir() if p.is_dir())
        except OSError:
            skill_dirs = []
        for skill_dir in skill_dirs:
            candidate = skill_dir / "hooks" / "tests"
            try:
                is_candidate_dir = candidate.is_dir()
            except OSError:
                continue
            if is_candidate_dir:
                test_dirs.append(candidate)
    for test_dir in test_dirs:
        try:
            if not test_dir.is_dir():
                continue
        except OSError:
            continue
        try:
            test_files = test_dir.glob("*.py")
        except OSError:
            test_files = []
        for f in test_files:
            stem = f.stem
            if stem.startswith("test_"):
                stems.add(stem[len("test_"):])
            elif stem.endswith("_test"):
                stems.add(stem[:-len("_test")])
    return stems


def _detect_hook_test_coverage(root, errors):
    """PRESENCE-only signal: does a hook script have a matching test file? NOT adequacy —
    a hooks/tests/test_x.py with a single trivial assertion counts as covered, same as a
    thorough suite (the "6 of 66" reality). Symlinked hooks are deduped by physical
    identity so one script counts once even if reachable via multiple glob paths."""
    disk_files = _hook_disk_files(root)
    test_stems = _hook_test_stems(root, errors)

    result = []
    seen_keys = set()
    for fp in disk_files:
        key = _physical_key(fp)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        stem_norm = fp.stem.replace("-", "_")
        result.append({"name": fp.name, "has_test": stem_norm in test_stems})
    return result


def _skill_has_test_asset(skill_dir):
    """PRESENCE-only signal (see _detect_hook_test_coverage docstring): a tests/ dir, an
    evals/ dir, or any test_*.py / *_eval.* file anywhere under the skill dir. Unlike
    _safe_exists, Path.is_dir() does NOT swallow PermissionError (only ENOENT-family
    errors) — a permission-denied skill dir is already surfaced as inaccessible by
    collect_descriptions()/collect_on_demand(); this function must only avoid crashing
    the whole run, not duplicate that reporting.

    The recursive test_*.py / *_eval.* search walks _iter_descendant_dirs(skill_dir) — the
    SAME pruned descendant walk the watcher uses (Codex r4 fix) — rather than
    Path.rglob(), which would descend into generated subtrees like node_modules/.venv that
    the watcher does not observe. This keeps the two walks equal BY CONSTRUCTION: a
    test/eval file this function can see is always inside a directory the watcher also
    yields, and a test/eval file planted under a pruned dir (e.g. node_modules) is
    intentionally excluded from BOTH signals."""
    try:
        if (skill_dir / "tests").is_dir() or (skill_dir / "evals").is_dir():
            return True
    except OSError:
        pass
    for d in _iter_descendant_dirs(skill_dir):
        try:
            if next(d.glob("test_*.py"), None) is not None:
                return True
        except OSError:
            pass
        try:
            if next(d.glob("*_eval.*"), None) is not None:
                return True
        except OSError:
            pass
    return False


def _detect_skill_test_coverage(root):
    skills_dir = root / "skills"
    if not skills_dir.is_dir():
        return []
    try:
        skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
    except OSError:
        skill_dirs = []
    return [{"name": d.name, "has_test": _skill_has_test_asset(d)} for d in skill_dirs]


def detect_test_coverage(root, on_demand, errors):
    """Whether each hook script and each skill has an associated test ASSET — a
    PRESENCE check, not an adequacy check (the "6 of 66" reality: a tests/ dir holding
    one trivial assertion counts as covered exactly like a thorough suite). Cross-links
    the same per-skill has_test verdict onto on_demand["skills"] (mutated in place) by
    skill name, so both sections agree instead of on_demand carrying its own narrower
    (tests/-dir-only) check."""
    hooks_result = _detect_hook_test_coverage(root, errors)
    skills_result = _detect_skill_test_coverage(root)

    skills_has_test = {s["name"]: s["has_test"] for s in skills_result}
    for entry in on_demand.get("skills", []):
        name = entry.get("name")
        if name in skills_has_test:
            entry["has_test"] = skills_has_test[name]

    return {
        "hooks": hooks_result,
        "skills": skills_result,
        "summary": {
            "hooks_with_test": sum(1 for h in hooks_result if h["has_test"]),
            "hooks_total": len(hooks_result),
            "skills_with_test": sum(1 for s in skills_result if s["has_test"]),
            "skills_total": len(skills_result),
        },
    }


def build_headline(always_loaded, hooks_section, instruction_length_flags, duplication_section):
    totals = always_loaded["totals"]
    return {
        "always_loaded_words": totals["words"],
        "always_loaded_tokens_est": totals["tokens_est"],
        "always_loaded_file_count": totals["file_count"],
        "duplicate_pair_count": len(duplication_section["pairs"]),
        "unchecked_binary_count": 0,
        "instruction_files_over_200": len(instruction_length_flags),
        "orphan_registration_count": len(hooks_section["orphan_registrations"]),
        "orphan_script_count": len(hooks_section["orphan_scripts"]),
    }


# Codex r3 FIX 3: well-known generated / non-harness-input subtrees pruned from the per-sweep
# descendant walk. NONE of the collector's instruction/rule/skill globs ever ingest a
# *.md / SKILL.md / phases|prompts|agents md / *_eval.* from inside these as an INPUT (they
# match only fixed-depth paths like skills/*/rules/*.md, never skills/*/node_modules/**), so
# skipping their descendants cannot drop a real read -- the containing SKILL dir stays yielded,
# keeping the T3B iter_input_paths-is-a-superset invariant intact -- while sparing the watcher
# from re-enumerating thousands of generated files (node_modules, caches, .git objects) every
# ~2s sweep. Membership of the FIRST level (the pruned dir appearing/disappearing under a
# watched parent) is still caught by that parent's own listdir signal.
_PRUNED_WALK_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".pytest_cache",
    ".venv", ".mypy_cache", ".ruff_cache",
})


def _iter_descendant_dirs(base):
    """Yield `base` and every non-pruned directory beneath it (each membership-watchable).
    _skill_has_test_asset (Codex r4 fix) now SHARES this exact walk for its recursive
    test_*.py / *_eval.* glob search instead of Path.rglob() — the two are equal BY
    CONSTRUCTION, not by a duplicated constant that could drift: a test/eval file added at
    ANY non-pruned depth flips a skill's has_test AND is watched, while one planted under a
    pruned dir (node_modules, .venv, caches, ...) is intentionally invisible to BOTH.

    followlinks=False (Codex r3 FIX 3): os.walk still ENTERS `base` even when `base` itself is a
    deploy-symlinked skill dir (the first hop stays -- os.walk always descends into the walk
    root), so a symlinked skill's own contents remain watched; but a symlink NESTED inside the
    target is NOT chased into. This matches the collector's own reads -- pathlib rglob does not
    follow nested directory symlinks either -- so the walked set stays a SUPERSET of what
    build_document reads while avoiding heavy I/O (and any symlink cycle) on nested external
    trees. Well-known generated subtrees (_PRUNED_WALK_DIRS) are pruned before descending."""
    try:
        if not base.is_dir():
            return
    except OSError:
        return
    for dirpath, dirnames, _ in os.walk(base, followlinks=False):
        # Prune generated/non-input subtrees IN PLACE so os.walk never descends into them.
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_WALK_DIRS]
        yield Path(dirpath)


def iter_input_paths(root, project_root=None):
    """SINGLE SOURCE OF TRUTH for the complete filesystem input surface build_document reads
    — the set a live-dashboard filesystem watcher (T4) must observe to know when a re-render
    is due. Returns a deterministic, de-duplicated, string-sorted list of Path.

    Contract for the watcher: snapshot each yielded FILE by mtime (content change) and each
    yielded DIR by membership (a skill / hook / rule / agent / project added or removed).
    Entries are yielded by their root-relative path; the watcher stats them FOLLOWING symlinks,
    so a change to a deploy-symlink TARGET that lives OUTSIDE --root is still observed even
    though a plain os.walk(--root) would miss it — that missed-target case is the whole reason
    this function, not a hand-kept list in serve.py, is the source of truth.

    GUARANTEE: a SUPERSET of every STATICALLY-enumerable path build_document stats/opens/globs/
    iterdirs, PLUS every hook-script path resolvable UNDER root from a registered settings.json
    command (reconcile_hooks stat()s exactly those — mirrored here via _script_from_command, and
    hooks/ is watched RECURSIVELY so a nested hook script is covered by container membership).
    Each group below names the collector read it corresponds to. Add a future collector input
    HERE (or to a shared _*_GLOBS constant that both this and the scan consume) or the dashboard
    serves stale data. NOT covered are the two honest, content-derived residuals below.

    KNOWN watcher blind spots (documented for T4 — content-derived, NOT statically enumerable):
      * A registered hook command may resolve to an ABSOLUTE path OUTSIDE root (case c). A root
        walk cannot watch a file outside root, so its own create/delete is unobserved — but the
        settings.json EDIT that registers (or de-registers) such a command IS watched, so a
        re-render still fires on the registration change itself. Nested and relative-under-root
        hook scripts ARE now covered (recursive hooks/ + resolved-command yield above).
      * check_phantom_refs stats `root / <token>` for backtick path tokens parsed out of prose
        — an unbounded, content-derived set. Creating a referenced file OUTSIDE the dirs above
        can flip a phantom-ref verdict without a watched signal. In practice almost every
        referenced path already lives under a watched dir (rules/, skills/, agents/, commands/,
        hooks/); the instruction-file EDIT that introduces the ref itself IS watched."""
    root = Path(root)
    paths = set()

    # -- concrete top-level files (content matters) --
    #   CLAUDE.md              walk_always_loaded + _staleness_corpus
    #   settings.json          parse_settings -> permissions, config, hook registrations
    #   memory/MEMORY.md       walk_always_loaded (root stub index)
    #   plugins/*.json         collect_config._read_json_name_list (two fixed names)
    paths.add(root / "CLAUDE.md")
    paths.add(root / "settings.json")
    paths.add(root / "memory" / "MEMORY.md")
    paths.add(root / "plugins" / "known_marketplaces.json")
    paths.add(root / "plugins" / "installed_plugins.json")

    # -- active project's own CLAUDE.md (lives OUTSIDE --root); walk_always_loaded gates it on
    #    the projects/<slug>/memory dir. Yielded unconditionally when given: a harmless superset. --
    if project_root is not None:
        paths.add(Path(project_root) / "CLAUDE.md")

    # -- container dirs whose MEMBERSHIP changes collector output --
    #   skills   : new/removed skill -> descriptions, on_demand, rules, test coverage
    #   projects : new project      -> conditional_variants (each */memory/MEMORY.md)
    #   agents/hooks/hooks-tests/rules/commands : globbed membership below
    for d in ("skills", "projects", "agents", "hooks", "hooks/tests", "rules", "commands"):
        paths.add(root / d)

    # -- glob-based content files: the SAME pattern tuples the collector scans consume, so the
    #    read surface and the watched surface are one definition (see _*_GLOBS above). Covers
    #    rules, skills/*/rules, skills SKILL.md (top + nested), phases/prompts/agents md,
    #    commands, agents, hooks/*.py|*.sh, and hooks/tests + skills/*/hooks/tests scripts. --
    for pattern in set(_INSTRUCTION_GLOBS + _DUP_GLOBS + _STALENESS_RULE_GLOBS
                       + _HOOK_SCRIPT_GLOBS + _HOOK_TEST_GLOBS):
        try:
            paths.update(root.glob(pattern))
        except OSError:
            continue

    # -- projects/*/memory: MEMORY.md index (walk_always_loaded / conditional_variants) plus,
    #    for the active project, memory bodies (collect_on_demand). Yield each memory dir
    #    (membership) + every *.md (content); MEMORY.md matches the *.md glob. --
    try:
        slug_dirs = sorted(p for p in (root / "projects").iterdir() if p.is_dir())
    except OSError:
        slug_dirs = []
    for slug_dir in slug_dirs:
        mem_dir = slug_dir / "memory"
        paths.add(mem_dir)
        try:
            paths.update(mem_dir.glob("*.md"))
        except OSError:
            pass

    # -- per-skill dirs: each skill dir + ALL descendant dirs (membership) so a test_*.py /
    #    *_eval.* added at any depth flips has_test (_skill_has_test_asset rglob). The skill's
    #    concrete CONTENT files are already covered by the _*_GLOBS union above. --
    try:
        skill_dirs = sorted(p for p in (root / "skills").iterdir() if p.is_dir())
    except OSError:
        skill_dirs = []
    for skill_dir in skill_dirs:
        for sub in _iter_descendant_dirs(skill_dir):
            paths.add(sub)

    # -- hooks/ dir + ALL descendant dirs (membership): reconcile_hooks stat()s the resolved
    #    script for each registered command, and _script_from_command can resolve to a script
    #    NESTED under hooks/<subdir>/. The shallow hooks/*.py|*.sh globs above miss that depth,
    #    so watch hooks/ recursively — the same _iter_descendant_dirs mechanism used for skills. --
    for sub in _iter_descendant_dirs(root / "hooks"):
        paths.add(sub)

    # -- resolved hook-script paths from REGISTERED settings.json commands: reconcile_hooks
    #    stat()s exactly these. Reuse _script_from_command (its resolution logic is the single
    #    source of truth) and yield each script that resolves UNDER root — a command may point
    #    OUTSIDE hooks/ (e.g. "./scripts/x.py"). A command resolving to an ABSOLUTE path outside
    #    root is un-watchable via a root walk (disclosed in the docstring's blind-spot list); the
    #    settings.json edit that registers it IS watched (settings.json is yielded above). --
    settings, _parsed_ok = parse_settings(root, [], [])
    root_resolved = root.resolve()
    for command in _iter_hook_commands(settings):
        script_path, _note = _script_from_command(command, root)
        if script_path is None:
            continue
        try:
            script_path.resolve().relative_to(root_resolved)
        except (ValueError, OSError):
            continue  # resolves outside root (case c) — genuinely un-watchable via a root walk
        paths.add(script_path)

    return sorted(paths, key=str)


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
        "The always-loaded classification of skills/*/rules/*.md (each sub-skill's rules dir) "
        "reflects the design's assertion and cannot be statically verified — CC's actual "
        "session-start injection set is not introspectable from disk.",
    ]

    files, conditional_variants = walk_always_loaded(root, project_root, inaccessible, errors)
    skill_descriptions, agent_descriptions = collect_descriptions(root, inaccessible)
    skills, skill_internal_bodies, memory_bodies = collect_on_demand(root, project_root, inaccessible)

    settings, settings_parsed_ok = parse_settings(root, errors, blind_spots)
    hooks_section = reconcile_hooks(root, settings, inaccessible, blind_spots)
    permissions_section = collect_permissions(settings, settings_parsed_ok)
    config_section = collect_config(root, settings, settings_parsed_ok, blind_spots)
    instruction_length_flags = flag_long_instructions(root)
    duplication_section = scan_duplication(root, blind_spots)
    corpus_files = _staleness_corpus(root, inaccessible)
    phantom_refs = check_phantom_refs(root, corpus_files, inaccessible)
    promotion_candidates = collect_promotion_candidates(root, corpus_files, settings)

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

    test_coverage_section = detect_test_coverage(root, on_demand, errors)

    doc = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "headline": build_headline(always_loaded, hooks_section, instruction_length_flags,
                                    duplication_section),
        "always_loaded": always_loaded,
        "on_demand": on_demand,
        "enforcement": {
            "hooks": hooks_section,
            "permissions": permissions_section,
        },
        "config": config_section,
        "instruction_length_flags": instruction_length_flags,
        "duplication": duplication_section,
        "phantom_refs": phantom_refs,
        "promotion_candidates": promotion_candidates,
        "test_coverage": test_coverage_section,
        "inaccessible": inaccessible,
        "blind_spots": blind_spots,
        "errors": errors,
    }
    return doc


def _empty_document(root):
    """Full schema envelope, every top-level key present and empty (F8) — the crash-path
    fallback so main()'s top-level guard never emits a partial/silent stub. Mirrors
    build_document's REAL current shape exactly (including on_demand.memory_bodies and
    always_loaded.conditional_variants), not a trimmed subset."""
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(), "root": str(root),
        "headline": {k: 0 for k in ("always_loaded_words", "always_loaded_tokens_est",
            "always_loaded_file_count", "duplicate_pair_count", "unchecked_binary_count",
            "instruction_files_over_200", "orphan_registration_count", "orphan_script_count")},
        "always_loaded": {"files": [], "conditional_variants": [], "skill_descriptions": [],
                          "agent_descriptions": [],
                          "totals": {"words": 0, "tokens_est": 0, "file_count": 0}},
        "on_demand": {"skills": [], "skill_internal_bodies": [], "memory_bodies": []},
        "enforcement": {"hooks": {"registered": [], "orphan_registrations": [],
            "scripts_on_disk": [], "orphan_scripts": []},
            "permissions": {"allow_count": 0, "deny_count": 0, "ask_count": 0, "evidence": "INACCESSIBLE"}},
        "config": {"env_keys": [], "env_key_count": 0, "model": None, "cleanup_period_days": 0,
                   "sandbox": False, "enabled_plugins": [], "plugin_count": 0,
                   "marketplaces": [], "marketplace_count": 0,
                   "installed_plugins": [], "installed_plugin_count": 0, "evidence": "INACCESSIBLE"},
        "instruction_length_flags": [], "duplication": {"shingle_k": SHINGLE_K,
            "metric": "containment", "threshold": DUP_THRESHOLD, "pairs": []},
        "phantom_refs": [], "promotion_candidates": [],
        "test_coverage": {"hooks": [], "skills": [], "summary": {"hooks_with_test": 0,
            "hooks_total": 0, "skills_with_test": 0, "skills_total": 0}},
        "inaccessible": [], "blind_spots": [], "errors": [],
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only harness map collector.")
    ap.add_argument("--root", default=str(Path.home() / ".claude"))
    ap.add_argument("--project-root", default=os.getcwd())
    ap.add_argument("--out", default=None, help="Optional JSON out-path; MUST be outside --root.")
    ap.add_argument("--indent", type=int, default=2)
    args = ap.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    out_path = None
    if args.out is not None:
        try:
            root_stat = os.stat(root)                        # root is expected to be an existing dir
        except OSError as e:
            # A bad/inaccessible --root must NOT crash before the crash-safe envelope below —
            # skip the --out write (nothing safe to validate against) but still fall through to
            # build_document/print so the always-valid-JSON-envelope invariant holds.
            print(f"warning: --root not accessible, skipping --out write: {e}", file=sys.stderr)
            root_stat = None
        if root_stat is not None:
            # normpath collapses a root-EXITING '..' (e.g. <root>/../x.json -> <root-parent>/x.json)
            # so a path that only textually traverses root is not falsely rejected (FIX 3).
            lexical = Path(os.path.normpath(str(Path(args.out).expanduser())))
            resolved = Path(args.out).expanduser().resolve()  # resolves symlink aliases
            for cand in (lexical, resolved):
                if _resolves_inside_root(cand, root, root_stat):  # case/hardlink-robust (FIX 2)
                    ap.error("--out must be outside --root (read-only invariant)")
            out_path = resolved                                # write through the validated resolved path
    try:
        doc = build_document(root, args.project_root)
    except Exception as exc:  # noqa: BLE001 — collector must always emit a FULL-key valid envelope
        doc = _empty_document(root)
        doc["errors"].append(f"collector crashed: {exc!r}")
    # Serialize defensively: a lone UTF-16 surrogate (e.g. surviving json.loads out of a
    # crafted settings.json — Python allows lone surrogates in str) is unencodable as
    # UTF-8 under ensure_ascii=False. Force-detect it HERE (encode, discard the bytes) so
    # the always-valid-JSON-envelope invariant holds even at print()/write_text() time,
    # which sits OUTSIDE the build_document try/except above — a fallback to
    # ensure_ascii=True (which escapes the surrogate back to \ud800 safely) never fails.
    text = json.dumps(doc, indent=args.indent, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except (UnicodeEncodeError, TypeError):
        text = json.dumps(doc, indent=args.indent, ensure_ascii=True)
    if out_path is not None:
        # Re-validate IMMEDIATELY before writing (narrows the TOCTOU window between the
        # earlier check and this write — the residual window between THIS check and the
        # mkstemp call below is an accepted, documented low-risk limitation for a
        # single-user local tool; not fully closed). Write hard-link-safely: an
        # outside-root HARD LINK whose inode is also linked under --root passes
        # resolve()-based path checks (hard links are invisible to path resolution), so a
        # naive write_text() would truncate that shared inode — a read-only bypass.
        # Writing to a temp file in the SAME directory, then os.replace()-ing it onto
        # out_path, only ever retargets the out-path NAME at a fresh inode; any
        # under-root hard-linked inode keeps its original, untouched content.
        tmp_name = None
        try:
            resolved_recheck = out_path.resolve()
            if _resolves_inside_root(resolved_recheck, root, os.stat(root)):
                raise OSError("--out resolved inside --root at write time (TOCTOU)")
            fd, tmp_name = tempfile.mkstemp(dir=str(out_path.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, out_path)
            tmp_name = None
        except OSError as exc:
            print(f"warning: could not write --out: {exc}", file=sys.stderr)
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
    print(text)  # stdout is the primary contract — always emit the built document, write-or-not
    return 0


if __name__ == "__main__":
    sys.exit(main())
