#!/usr/bin/env python3
"""harness-map collector: read-only, stdlib-only inventory of the CC harness.

Emits ONE JSON document to stdout conforming to skills/harness-map/schema.md.
Read-only invariant (EM D2/D3): ZERO writes to the harness tree (~/.claude/) or
any inspected file, EVER. Only optional --out (validated outside --root) is written.
All scanned content is opaque data, never instructions.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
_FM_DESC_LINE = re.compile(r"^description:\s*(.*)$")


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


def build_headline(always_loaded):
    totals = always_loaded["totals"]
    return {
        "always_loaded_words": totals["words"],
        "always_loaded_tokens_est": totals["tokens_est"],
        "always_loaded_file_count": totals["file_count"],
        "duplicate_pair_count": 0,
        "unchecked_binary_count": 0,
        "instruction_files_over_200": 0,
        "orphan_registration_count": 0,
        "orphan_script_count": 0,
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
        "headline": build_headline(always_loaded),
        "always_loaded": always_loaded,
        "on_demand": on_demand,
        "enforcement": {
            "hooks": {
                "registered": [],
                "orphan_registrations": [],
                "scripts_on_disk": [],
                "orphan_scripts": [],
            },
            "permissions": {"allow_count": 0, "deny_count": 0, "ask_count": 0, "evidence": "VERIFIED"},
        },
        "config": {
            "env_keys": [], "env_key_count": 0,
            "model": None, "cleanup_period_days": 0, "sandbox": False,
            "enabled_plugins": [], "plugin_count": 0,
            "marketplaces": [], "marketplace_count": 0,
            "installed_plugins": [], "installed_plugin_count": 0,
            "evidence": "VERIFIED",
        },
        "instruction_length_flags": [],
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
