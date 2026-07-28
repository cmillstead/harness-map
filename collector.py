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
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, cast

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
# ^/name  or  ^/ns:name  or  ^/ns:sub:...:name  (/base:orientation:tasks:deep-why is live)
_SLASH_COMMAND_RE = re.compile(r"^/[a-z0-9][a-z0-9-]*(?::[a-z0-9][a-z0-9-]*)*$")
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
    """Read `path` as utf-8 text (errors="replace"). Returns `(text, "VERIFIED")` on
    success or `(None, "INACCESSIBLE")` on failure. The `is_file()` guard matches
    `_read_head`'s "no blocking open() on a FIFO" invariant: an in-root, registered
    `*-dispatcher.py` (or any of the other `_read_checked` call sites) that is actually
    a FIFO/socket/dir must not block the collector. `is_file()` follows symlinks (True
    for a regular file or a symlink to one; False for FIFO/dir/socket/broken symlink),
    so regular-file behavior is unchanged — no false negatives."""
    if not path.is_file():
        return None, "INACCESSIBLE"
    try:
        return path.read_text(encoding="utf-8", errors="replace"), "VERIFIED"
    except OSError:
        return None, "INACCESSIBLE"


_DESC_MAX_BYTES = 65536      # ONE byte-bounded read; serves BOTH the header/comment scan
                             # and the ast-docstring parse (no second read to drop status)
_DESCRIPTION_MAX = 120


def _read_head(path, max_bytes):
    """Read at most `max_bytes` BYTES of `path`, decoded utf-8 (errors='replace'); None on
    OSError or if `path` is not a regular file. BYTE-bounded (binary read then decode — a
    text-mode `read(n)` caps CHARACTERS, not bytes). The `is_file()` guard rejects a FIFO/
    dir/socket/broken-symlink named `*.py` so a blocking `open()` on a FIFO is not reached.
    Best-effort: the is_file()->open() window is NOT race-proof against concurrent path
    replacement, which is acceptable for a read-only mapper over the self-owned harness dir
    (TOCTOU here is not a threat; no O_NONBLOCK needed)."""
    try:
        if not path.is_file():          # follows symlinks; False for FIFO/dir/socket/broken
            return None
        with open(path, "rb") as fh:
            data = fh.read(max_bytes)
    except OSError:
        return None
    return data.decode("utf-8", errors="replace")


def _script_description(path, skip_read=False):
    """Read-only, best-effort one-line description for the 'Scripts on disk' list. Returns
    `(description, status)`, status in {"OK", "SKIPPED", "INACCESSIBLE"}:
      * SKIPPED  — `skip_read=True` (out-of-root / unresolvable target): "", no read; the
                   caller records a blind-spot, NOT an inaccessible entry.
      * INACCESSIBLE — the read failed (perm/FIFO/etc.): ""; the CALLER records the
                   inaccessible entry ONCE (deduped) — this fn never mutates it.
      * OK       — read succeeded (description may still be "" for a headerless file).
    Precedence: `# summary: X` marker > .py module docstring first line
    (ast.get_docstring(ast.parse) — PARSES only, never imports/executes) > first non-shebang
    leading `# ...` comment > "". Does a SINGLE bounded `_read_head` — the same buffer feeds
    the comment scan AND the ast parse, so there is no second read whose failure could drop
    the status. Scanned bytes are untrusted DATA: extracted verbatim, esc_html'd at render,
    never executed."""
    if skip_read:
        return "", "SKIPPED"
    raw = _read_head(path, _DESC_MAX_BYTES)
    if raw is None:
        return "", "INACCESSIBLE"
    lines = raw.splitlines()[:60]
    # 1. explicit `# summary:` marker
    for line in lines:
        s = line.strip()
        if s.lower().startswith("# summary:"):
            return s.split(":", 1)[1].strip()[:_DESCRIPTION_MAX], "OK"
    # 2. .py module docstring first line (parse the SAME bounded buffer; never execute).
    #    A >64KB file yields a truncated prefix -> SyntaxError -> comment fallback below.
    if path.suffix == ".py":
        try:
            doc = ast.get_docstring(ast.parse(raw))
        except (SyntaxError, ValueError, RecursionError):
            doc = None
        if doc:
            first = doc.strip().splitlines()[0].strip()
            if first:
                return first[:_DESCRIPTION_MAX], "OK"
    # 3. first non-shebang leading comment line
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#!"):
            continue
        if s.startswith("#"):
            return s.lstrip("#").strip()[:_DESCRIPTION_MAX], "OK"
        break   # first real code/content line ends the leading-comment scan
    return "", "OK"


def _append_inaccessible_once(inaccessible, rel):
    """Record an unreadable path exactly once (schema.md:92 requires reporting every
    attempted-but-unreadable path; dedupe so a file read twice isn't double-listed).
    `_rel`/`_rel_safe` produce identical strings for an in-root path, so this dedupes
    against an entry a dispatcher read may already have produced for the same path."""
    if not any(e.get("path") == rel for e in inaccessible):
        inaccessible.append({"path": rel, "reason": "unreadable"})


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


def validate_write_target(raw_path, roots, input_paths=()):
    """SINGLE shared write-guard called at each write sink's CALLER entry point:
    collector.main's `--out` guard, serve.py `build_server`'s `--out-dir` startup guard,
    and render_html.main's `--out-dir` compose-mode guard (T13 F2) — every caller that
    can see BOTH roots reuses this rather than re-implementing the containment check.
    `render_html.write_html_safely` ALSO routes its write-time root guard through this
    helper: it calls `validate_write_target` immediately on entry AND again immediately
    before its `mkstemp` (a pre-mkstemp re-check mirroring collector.main's, narrowing the
    validate-then-write TOCTOU window), raising `render_html.RenderError` on rejection.
    So this helper is the single shared containment check for every write sink — the
    collector `--out`/serve `--out-dir` startup guards AND the render/serve per-write sinks.

    A candidate is REJECTED if it resolves inside ANY of `roots` (segment-safe via
    `_resolves_inside_root` — Path.parents + inode compare, NEVER str.startswith),
    tested both LEXICALLY (normpath, catches a textual '..' that still exits a root)
    and RESOLVED (catches a symlink alias into a root — mirrors main()'s prior FIX 2/3).
    It is ALSO rejected if it equals any path in `input_paths` (the collector's own
    read surface, e.g. `iter_input_paths()`) — that clause is what stops a target like
    `~/.claude.json` (a T5 MCP input that sits OUTSIDE every dir-root, so containment
    alone would wrongly allow overwriting it). That `input_paths` check (P2-B hardened)
    compares LITERALLY (lexical/resolved string equality — defense-in-depth, kept as-is)
    PLUS by resolved realpath/inode identity: an input path that is ITSELF a symlink
    (e.g. `~/.claude.json` aliasing `/reports/result.json`) is resolved before comparing,
    and — where both the input and the candidate target exist — an `os.path.samestat`
    inode check catches an alias the string comparison alone would miss (a literal
    `Path(p) in (lexical, resolved)` never resolves `p`, so it could not see that
    `~/.claude.json` and `--out /reports/result.json` name the SAME file).

    Returns `(ok, resolved_path)`: `resolved_path` is the Path to write through when
    `ok` is True, `None` when `ok` is False. A root that cannot be stat()'d is SKIPPED
    (nothing safe to compare against) rather than treated as a rejection."""
    expanded = Path(raw_path).expanduser()
    lexical = Path(os.path.normpath(str(expanded)))
    try:
        resolved = expanded.resolve()
    except OSError:
        resolved = expanded
    for root in roots:
        root = Path(root)
        try:
            root_stat = os.stat(root)
        except OSError:
            continue
        for cand in (lexical, resolved):
            if _resolves_inside_root(cand, root, root_stat):
                return False, None
    for p in input_paths:
        p_path = Path(p)
        if p_path in (lexical, resolved):
            return False, None
        try:
            p_resolved = p_path.resolve()
        except OSError:
            p_resolved = p_path
        if p_resolved == resolved:
            return False, None
        try:
            if os.path.samestat(os.stat(p_resolved), os.stat(resolved)):
                return False, None
        except OSError:
            pass
    return True, resolved


# --- T3: project-tier read gate (H2 containment) + TOCTOU-closed read ---
# The operator tier keeps its existing trusted symlink-following UNCHANGED (it deploys
# via symlinks by design). Every project-tier read/traverse/excerpt sink below routes
# through THIS gate instead of re-implementing containment per call site.

def _project_tier_gate(candidate, containment_root, containment_root_stat):
    """Single project-tier read/traverse gate (H2). Returns `(contained, identity_stat)`:
    `contained=True` only when `candidate` exists and its REALPATH lies inside
    `containment_root` — segment-safe via `_resolves_inside_root` (`Path.parents` +
    inode compare, NEVER `str.startswith`). `identity_stat` is the `os.stat(candidate)`
    result captured AT THIS CHECK (follows symlinks) — a subsequent TOCTOU-closed read
    (`_read_project_file`) MUST re-validate an opened fd's `fstat()` against this SAME
    identity before trusting the bytes (T3 P1-5). `contained=False` (identity_stat=None)
    for a broken symlink, a stat()-inaccessible path, or a realpath that escapes
    `containment_root` — the caller must record an `out_of_root_ref` and skip; never
    read, traverse, or excerpt."""
    try:
        identity_stat = os.stat(candidate)   # follows symlinks -- the resolved identity
    except OSError:
        return False, None
    real = Path(_physical_key(candidate))
    if not _resolves_inside_root(real, containment_root, containment_root_stat):
        return False, None
    return True, identity_stat


def _record_out_of_root_ref(out_of_root_refs, seen, rel_root, candidate):
    """Record an escaping project-tier path (H2) as a structured, untrusted
    `out_of_root_ref`: `name` (harness-relative, via `_rel_safe`) + `target` (the raw
    `readlink()` string for a symlink, else a best-effort realpath) + `trusted: False`.
    NEVER reads `candidate`'s contents. Deduped by `name` in `seen` so a dir-level skip
    and a file-level skip for the same entry are not double-recorded."""
    name = _rel_safe(rel_root, candidate)
    if name in seen:
        return
    seen.add(name)
    try:
        is_link = candidate.is_symlink()
    except OSError:
        is_link = False
    if is_link:
        try:
            target = os.readlink(candidate)
        except OSError:
            target = _physical_key(candidate)
    else:
        target = _physical_key(candidate)
    out_of_root_refs.append({"name": name, "target": target, "trusted": False})


def _read_project_file(path, containment_root, containment_root_stat):
    """TOCTOU-closed project-tier file read (T3 P1-5, hardened P1-A). The `is_file()`-
    then-`open()` window accepted elsewhere in this file for the self-owned operator tier
    (see `_read_text`/`_read_head`) is CLOSED here for untrusted project-tier reads.

    Codex reproduced an ABA symlink race against the OLD design (`_project_tier_gate`
    captured an identity `stat()` FIRST, then a SEPARATE `realpath()` call decided
    containment, and only THEN was the path re-opened by pathname): an attacker who can
    retime a symlink swap makes the path resolve OUTSIDE the containment root during the
    identity stat, INSIDE during the containment realpath (so H2 wrongly "passes"), and
    back OUTSIDE before this open — the reopened OUTSIDE inode matched the STALE identity
    captured at the first (also outside) resolution, so external bytes returned as
    VERIFIED with no `out_of_root_ref`. FIX: bind the containment decision to the OPENED
    fd, not to a pathname resolved before or after it. Open first (`O_NONBLOCK` so a FIFO
    swapped in after a caller's `_project_tier_gate` pre-check can't hang forever on a
    writer-less FIFO), `fstat()` the opened fd for its immutable identity, THEN re-derive
    the realpath and require ALL of: (a) it resolves inside `containment_root`
    (`_resolves_inside_root`, segment-safe), (b) a FRESH `os.stat()` of that realpath —
    taken now, not earlier — `samestat()`s the opened fd's fstat (so the containment
    check and the bytes about to be read are provably the SAME inode, closing the ABA
    window in both directions), and (c) the fd is a regular file. Any mismatch is
    INACCESSIBLE; the bytes behind a swapped-out fd are never surfaced as VERIFIED.
    `containment_root`/`containment_root_stat` are the SAME pair every project-tier
    containment check uses (H2) — no `identity_stat` param anymore; the gate's pre-open
    stat is no longer trusted for the read decision. Returns `(text | None, evidence)`,
    evidence in `{"VERIFIED", "INACCESSIBLE"}`."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return None, "INACCESSIBLE"
    try:
        post_stat = os.fstat(fd)
        if not stat.S_ISREG(post_stat.st_mode):
            return None, "INACCESSIBLE"
        real = Path(_physical_key(path))
        if not _resolves_inside_root(real, containment_root, containment_root_stat):
            return None, "INACCESSIBLE"
        try:
            real_stat = os.stat(real)
        except OSError:
            return None, "INACCESSIBLE"
        if not os.path.samestat(real_stat, post_stat):
            return None, "INACCESSIBLE"
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read()
    except OSError:
        return None, "INACCESSIBLE"
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
    return data.decode("utf-8", errors="replace"), "VERIFIED"


def _project_file_entry(rel_root, path, category, containment_root_stat, inaccessible):
    """Project-tier analog of `_file_entry`: identical output shape, but reads through
    the TOCTOU-closed `_read_project_file` (T3, hardened P1-A) instead of `_read_text`.
    The caller MUST have already passed `path` through `_project_tier_gate` as a cheap
    pre-filter (skip an obviously-escaping path without even attempting an open) — the
    read below is the actual security decision, bound to the opened fd, independent of
    the gate's outcome; `rel_root` doubles as `_read_project_file`'s containment root."""
    text, evidence = _read_project_file(path, rel_root, containment_root_stat)
    if evidence == "INACCESSIBLE":
        _append_inaccessible_once(inaccessible, _rel_safe(rel_root, path))
        return None
    words, lines, tokens_est = _metrics(text)
    return {
        "path": _rel(rel_root, path),
        "category": category,
        "words": words,
        "lines": lines,
        "tokens_est": tokens_est,
        "evidence": "VERIFIED",
    }


def _walk_contained_dirs(start, containment_root, containment_root_stat, out_of_root_refs, seen_refs):
    """Manual, cycle-safe directory walk under `start` (H2) — yields each directory
    (including `start`) whose realpath lies inside `containment_root`. Deliberately NOT
    `Path.rglob`, which follows symlinks unconditionally: a directory entry that is a
    symlink resolving OUTSIDE `containment_root` is recorded as an `out_of_root_ref` and
    NOT descended into — no listing of its children, no reads, no further traversal.
    Cycle-safe: a directory whose physical identity was already visited is skipped
    without re-descending, so a project-internal symlink loop cannot hang the walk.

    P1-A hardened: the OLD design ran `_project_tier_gate` (a pathname `stat()` for
    identity, then a SEPARATE pathname `realpath()` for containment) and THEN listed the
    SAME pathname again (`d.iterdir()`) — an attacker retiming a symlink swap could make
    the containment check see one inode and the subsequent listing see another, the same
    ABA class as the file-read race `_read_project_file` closes. FIX: open the directory
    FIRST (`O_DIRECTORY | O_NONBLOCK`), `fstat()` the opened fd for its immutable
    identity, THEN re-derive the realpath and require BOTH that it resolves inside
    `containment_root` AND that a FRESH `os.stat()` of that realpath `samestat()`s the
    OPENED fd — closing the ABA window exactly like `_read_project_file` — and enumerate
    children via `os.scandir(fd)` (an int fd, not the pathname again) so the listing is
    of the PROVEN inode, never a re-resolved path."""
    visited = set()
    stack = [Path(start)]
    while stack:
        d = stack.pop()
        key = _physical_key(d)
        if key in visited:
            continue
        visited.add(key)
        try:
            fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK)
        except OSError:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, containment_root, d)
            continue
        try:
            post_stat = os.fstat(fd)
            real = Path(_physical_key(d))
            if not _resolves_inside_root(real, containment_root, containment_root_stat):
                _record_out_of_root_ref(out_of_root_refs, seen_refs, containment_root, d)
                continue
            try:
                real_stat = os.stat(real)
            except OSError:
                _record_out_of_root_ref(out_of_root_refs, seen_refs, containment_root, d)
                continue
            if not os.path.samestat(real_stat, post_stat):
                _record_out_of_root_ref(out_of_root_refs, seen_refs, containment_root, d)
                continue
            yield d
            try:
                entries = sorted(os.scandir(fd), key=lambda e: e.name)
            except OSError:
                continue
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                if is_dir:
                    stack.append(d / entry.name)
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def _metrics(text):
    words = len(text.split())
    lines = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
    return words, lines, round(words * 1.3)


def _read_checked(root, path, inaccessible, rel_root=None):
    """Read text; on INACCESSIBLE record to inaccessible[] and return None. Preserves the
    exact inaccessible-append behavior the call sites previously inlined.

    Codex #6 (S2 gate fix): routed through _append_inaccessible_once rather than a bare
    append — _staleness_corpus (one of this function's callers) globs the SAME rules/*.md
    files _deduped_instruction_files now also records inaccessible entries for, so an
    unreadable rule file reached through both paths must be recorded once, not twice."""
    text, evidence = _read_text(path)
    if evidence == "INACCESSIBLE":
        _append_inaccessible_once(inaccessible, _rel(rel_root or root, path))
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


def _walk_project_tier(project_root, inaccessible, errors, out_of_root_refs):
    """Compose-mode project-tier walk (P1-1): project CLAUDE files under the
    project-containment-root (`<repo>/CLAUDE.md`, `<repo>/CLAUDE.local.md`, and nested
    `<repo>/**/CLAUDE.md` — including `<repo>/.claude/CLAUDE.md`, a valid load form per
    T1) plus project-harness-root rules (`<repo>/.claude/rules/*.md`). Every entry is
    tagged tier="project". Unconditional on any operator-root registration (H1:
    composed mode replaces the legacy single-file, registration-gated
    project_claude_md branch in walk_always_loaded).

    T3/H2: EVERY read and directory descent here is gated by `_project_tier_gate`
    (containment) and, for file bodies, read via the TOCTOU-closed `_read_project_file`
    instead of `_file_entry`/`_read_text`. A directory (the `<repo>/**` walk that finds
    nested CLAUDE.md, or `.claude/rules` itself) whose realpath escapes the project
    containment root is NOT descended into; a file whose realpath escapes is NOT read.
    Both are recorded as `out_of_root_refs` (name + target, untrusted) instead."""
    files: list[dict[str, Any]] = []
    seen: set[Any] = set()
    seen_refs: set[str] = set()
    project_root = Path(project_root)
    harness_root = project_root / ".claude"

    try:
        containment_stat = os.stat(project_root)
    except OSError as e:
        errors.append(f"project containment root not accessible: {project_root}: {e}")
        return files

    claude_files = []
    try:
        for d in _walk_contained_dirs(project_root, project_root, containment_stat,
                                       out_of_root_refs, seen_refs):
            for fname in ("CLAUDE.md", "CLAUDE.local.md"):
                f = d / fname
                present, ok = _safe_exists(f)
                if ok and present:
                    claude_files.append(f)
    except OSError as e:
        errors.append(f"project CLAUDE.md walk failed for {project_root}: {e}")

    for f in sorted(claude_files):
        key = _physical_key(f)
        if key in seen:
            continue
        contained, _identity = _project_tier_gate(f, project_root, containment_stat)
        if not contained:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, f)
            continue
        if f.name == "CLAUDE.local.md":
            category = "project_claude_local_md"
        elif f.parent == project_root:
            category = "project_claude_md"
        else:
            category = "project_claude_md_nested"
        entry = _project_file_entry(project_root, f, category, containment_stat, inaccessible)
        if entry:
            entry["tier"] = "project"
            files.append(entry)
            seen.add(key)

    rules_dir = harness_root / "rules"
    rule_files = []
    try:
        is_rules_dir = rules_dir.is_dir()
    except OSError as e:
        errors.append(f"project rules is_dir failed for {rules_dir}: {e}")
        is_rules_dir = False
    if is_rules_dir:
        contained, _identity = _project_tier_gate(rules_dir, project_root, containment_stat)
        if not contained:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, rules_dir)
        else:
            try:
                rule_files = sorted(rules_dir.glob("*.md"))
            except OSError as e:
                errors.append(f"project rules glob failed for {rules_dir}: {e}")
                rule_files = []
    for f in rule_files:
        key = _physical_key(f)
        if key in seen:
            continue
        contained, _identity = _project_tier_gate(f, project_root, containment_stat)
        if not contained:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, f)
            continue
        entry = _project_file_entry(project_root, f, "project_rule", containment_stat, inaccessible)
        if entry:
            entry["tier"] = "project"
            files.append(entry)
            seen.add(key)

    return files


# --- T4: node model + collision-keyed shadow resolver (compose mode only) ---
# Canonical tier-tagged nodes for the 4 collision-keyed surfaces (skill/agent/command/
# rule), each carrying a collision key (surface, name) plus tier. Feeds
# _resolve_tier_composition below, which marks the effective/shadowed winner per surface
# and classifies each project-tier node as an add/override/dark entry. Project-tier
# skill/agent/command discovery here is a NEW read/traverse surface (T4) — every path is
# routed through T3's `_project_tier_gate` (H2 containment), same as `_walk_project_tier`.

def _walk_operator_tier_nodes(root):
    """Operator-tier skill/agent/command nodes (T4). A lean single-level existence walk
    (no body read — the node model needs only the collision key + path for the shadow
    resolver; word/line metrics stay owned by collect_descriptions/collect_on_demand).
    Commands get their FIRST node collection here — no prior section inventoried
    commands/*.md as nodes at all. Operator tier keeps its existing trusted
    symlink-following (unchanged, matches every other operator-tier walk)."""
    nodes: list[dict[str, Any]] = []
    skills_dir = root / "skills"
    if skills_dir.is_dir():
        try:
            skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
        except OSError:
            skill_dirs = []
        for skill_dir in skill_dirs:
            skill_md = skill_dir / "SKILL.md"
            present, ok = _safe_exists(skill_md)
            if ok and present:
                nodes.append({"surface": "skill", "name": skill_dir.name,
                              "tier": "operator", "path": _rel(root, skill_md)})
    for surface, dirname in (("agent", "agents"), ("command", "commands")):
        d = root / dirname
        if not d.is_dir():
            continue
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            files = []
        for f in files:
            nodes.append({"surface": surface, "name": f.stem, "tier": "operator",
                          "path": _rel(root, f)})
    return nodes


def _walk_project_tier_nodes(project_root, out_of_root_refs):
    """Project-tier skill/agent/command nodes (T4): `<repo>/.claude/{skills,agents,
    commands}/`. Existence + identity ONLY (same rationale as
    `_walk_operator_tier_nodes` — no body read needed for the collision-key model).
    EVERY path (surface dir, skill dir, leaf file) is routed through T3's
    `_project_tier_gate` (H2) — an escaping symlink at any level is recorded as an
    `out_of_root_ref` and excluded from the node list, mirroring `_walk_project_tier`'s
    rules-dir handling exactly (reused, not reimplemented)."""
    project_root = Path(project_root)
    harness_root = project_root / ".claude"
    nodes: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    try:
        containment_stat = os.stat(project_root)
    except OSError:
        return nodes

    skills_dir = harness_root / "skills"
    try:
        is_skills_dir = skills_dir.is_dir()
    except OSError:
        is_skills_dir = False
    if is_skills_dir:
        contained, _identity = _project_tier_gate(skills_dir, project_root, containment_stat)
        if not contained:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, skills_dir)
        else:
            try:
                skill_dirs = sorted(p for p in skills_dir.iterdir() if p.is_dir())
            except OSError:
                skill_dirs = []
            for skill_dir in skill_dirs:
                sd_contained, _identity = _project_tier_gate(skill_dir, project_root, containment_stat)
                if not sd_contained:
                    _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, skill_dir)
                    continue
                skill_md = skill_dir / "SKILL.md"
                present, ok = _safe_exists(skill_md)
                if not (ok and present):
                    continue
                f_contained, _identity = _project_tier_gate(skill_md, project_root, containment_stat)
                if not f_contained:
                    _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, skill_md)
                    continue
                nodes.append({"surface": "skill", "name": skill_dir.name, "tier": "project",
                              "path": _rel(project_root, skill_md)})

    for surface, dirname in (("agent", "agents"), ("command", "commands")):
        d = harness_root / dirname
        try:
            is_dir = d.is_dir()
        except OSError:
            is_dir = False
        if not is_dir:
            continue
        contained, _identity = _project_tier_gate(d, project_root, containment_stat)
        if not contained:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, d)
            continue
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            files = []
        for f in files:
            f_contained, _identity = _project_tier_gate(f, project_root, containment_stat)
            if not f_contained:
                _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, f)
                continue
            nodes.append({"surface": surface, "name": f.stem, "tier": "project",
                          "path": _rel(project_root, f)})
    return nodes


_RULE_NODE_CATEGORIES = {"rule", "coding_team_rule", "skill_rule", "project_rule"}


def _rule_nodes_from_files(files):
    """'rule' surface nodes (T4), derived from the already-deduped, already-tier-tagged
    `always_loaded.files` list (T2/T3) instead of re-walking/re-reading disk. Rules are a
    UNION surface (both tiers load, no shadow winner) so the node model only needs the
    collision key (name + tier + path), which `files[]` already carries — avoids a
    second read of every rule file."""
    return [{"surface": "rule", "name": Path(f["path"]).stem, "tier": f.get("tier", "operator"),
             "path": f["path"]}
            for f in files if f["category"] in _RULE_NODE_CATEGORIES]


_CLAUDE_MD_NODE_CATEGORIES = {"claude_md", "project_claude_md", "project_claude_md_nested",
                               "project_claude_local_md"}


def _claude_md_nodes_from_files(files):
    """'claude_md' surface nodes (T4/P2-A), derived from the already-deduped,
    already-tier-tagged `always_loaded.files` list — same reuse pattern as
    `_rule_nodes_from_files`. A UNION surface (both an operator CLAUDE.md and a project
    CLAUDE.md/CLAUDE.local.md/nested CLAUDE.md load, no shadow winner) so composition
    only needs the collision key `files[]` already carries. Fixes P2-A: without this, a
    project whose ONLY always-loaded surface is CLAUDE.md renders "project adds 0" even
    though a CLAUDE.md was actually added."""
    return [{"surface": "claude_md", "name": Path(f["path"]).stem, "tier": f.get("tier", "operator"),
             "path": f["path"]}
            for f in files if f["category"] in _CLAUDE_MD_NODE_CATEGORIES]


# Normalizes `composed_hooks`' THREE-tier settings vocabulary (`user`/`project`/`local`
# — correct for `composed_settings.hooks`, T5 §3) down to the tier_composition node
# model's BINARY `operator`/`project` vocabulary (P2, cross-model review): `local` is
# part of the project's OWN local config, so it is project-side in the binary model, not
# a third bucket. Only `_hook_nodes_from_composed` consumes this — `composed_hooks`
# itself (and `doc["composed_settings"]["hooks"]`) keeps the 3-way vocabulary untouched.
_HOOK_NODE_TIER = {"user": "operator", "project": "project", "local": "project"}


def _hook_nodes_from_composed(composed_hooks):
    """'hook' surface nodes (T4/P2-A), derived from the already-tier-tagged,
    already-precedence-merged `composed_settings.hooks` records (T5's `_compose_hooks`)
    — same reuse pattern as `_rule_nodes_from_files`/`_claude_md_nodes_from_files`. A
    UNION surface: every tier's hook fires regardless of collision (T5 §3), so
    composition only needs the collision key + path the composed record already carries.
    `composed_hooks` natively carries settings' THREE-tier vocabulary (`user`/`project`/
    `local`); the node model here is BINARY like every other surface, so each node's tier
    is normalized via `_HOOK_NODE_TIER` (P2 fix — previously the 3-way tier leaked
    through unchanged, so a Local-tier hook was never counted toward the project "adds"
    total `_resolve_tier_composition` derives from `tier=="project"`, and `"local"`/
    `"user"` wrongly appeared in a node model documented as operator|project only).
    `name`/`path` prefer the resolved script path (the concrete on-disk artifact); a hook
    whose command didn't resolve to a script token falls back to the raw command string
    so it is still represented rather than silently dropped (never-silent, matches this
    file's `_script_from_command`/`note` posture elsewhere)."""
    nodes = []
    for h in composed_hooks:
        script = h.get("script")
        path = script if script else h["command"]
        name = Path(script).stem if script else h["command"][:60]
        nodes.append({"surface": "hook", "name": name, "tier": _HOOK_NODE_TIER[h["tier"]],
                      "path": path})
    return nodes


# tier-precedence: CC-docs (HIGH confidence), live-verify deferred 2026-07-22 (T1
# RESOLUTION). Skills/commands: operator SHADOWS project (operator wins a name
# collision). Agents: project SHADOWS user — the INVERSE of skills. Rules/CLAUDE files/
# hooks: UNION (both tiers load, no winner). This resolver keys the collision winner OFF
# THE SURFACE — it is not one global rule; getting a surface backwards inverts the
# "overrides M" headline count.
_SURFACE_MERGE: dict[str, dict[str, Any]] = {
    "skill": {"merge": "shadow", "winner_tier": "operator"},
    "command": {"merge": "shadow", "winner_tier": "operator"},
    "agent": {"merge": "shadow", "winner_tier": "project"},
    "rule": {"merge": "union", "winner_tier": None},
    "claude_md": {"merge": "union", "winner_tier": None},
    "hook": {"merge": "union", "winner_tier": None},
}


def _resolve_tier_composition(
    raw_nodes: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """Per-surface shadow resolver (T4, M4/R5-A). Groups `raw_nodes` by the collision
    key (surface, name); for a SHADOW surface with both tiers present, marks the
    `_SURFACE_MERGE` winner "effective" and the loser "shadowed" (with `shadowed_by`
    naming the winner); for a UNION surface every node stays "effective" (both load, no
    winner). Classifies each surface's PROJECT-tier nodes into adds/overrides/dark
    (R5-A): a union project entry is always an add (loads alongside, never shadowed); a
    shadow project entry is an add (no operator collision), an override (project WON the
    collision — only possible on agents), or dark (project LOST the collision — a
    defined-but-never-runs project skill/command the operator should see). Returns
    `(resolved_nodes, surfaces_summary, participating_surfaces)`; `resolved_nodes` is
    sorted by the composite determinism key `(path, tier)` (L1)."""
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for n in raw_nodes:
        by_key.setdefault((n["surface"], n["name"]), []).append(n)

    resolved: list[dict[str, Any]] = []
    surfaces_summary: dict[str, dict[str, Any]] = {
        surface: {"merge": cfg["merge"], "winner_tier": cfg["winner_tier"],
                  "adds": 0, "overrides": 0, "dark": 0}
        for surface, cfg in _SURFACE_MERGE.items()
    }

    for (surface, _name), group in by_key.items():
        cfg = _SURFACE_MERGE[surface]
        if cfg["merge"] == "union":
            for n in group:
                resolved.append({**n, "status": "effective", "shadowed_by": None})
                if n["tier"] == "project":
                    surfaces_summary[surface]["adds"] += 1
            continue
        by_tier: dict[str, dict[str, Any]] = {n["tier"]: n for n in group}
        operator_n = by_tier.get("operator")
        project_n = by_tier.get("project")
        if operator_n and project_n:
            winner = operator_n if cfg["winner_tier"] == "operator" else project_n
            loser = project_n if winner is operator_n else operator_n
            resolved.append({**winner, "status": "effective", "shadowed_by": None})
            resolved.append({**loser, "status": "shadowed",
                             "shadowed_by": {"tier": winner["tier"], "path": winner["path"]}})
            if cfg["winner_tier"] == "project":
                surfaces_summary[surface]["overrides"] += 1
            else:
                surfaces_summary[surface]["dark"] += 1
        else:
            # group is non-empty (by_key.setdefault guarantees at least one member) and
            # by_tier's keys are only "operator"/"project", so at least one of these two
            # .get() lookups is non-None — cast (not assert) so this stays a pure type
            # narrowing with zero runtime effect, matching the M1 typing-only scope.
            only = cast(dict[str, Any], operator_n or project_n)
            resolved.append({**only, "status": "effective", "shadowed_by": None})
            if only["tier"] == "project":
                surfaces_summary[surface]["adds"] += 1

    resolved.sort(key=lambda n: (n["path"], n["tier"]))
    return resolved, surfaces_summary, sorted(_SURFACE_MERGE)


def walk_always_loaded(
    root: Path,
    project_root: Path | None,
    inaccessible: list[dict[str, Any]],
    errors: list[str],
    compose: bool = False,
    out_of_root_refs: list[Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect always-loaded surfaces: harness CLAUDE.md, the active project's memory
    index only (other projects' indexes go to conditional_variants), the active
    project's own CLAUDE.md (outside --root), rules/*.md, and coding-team rules.
    `compose=True` (P1-1): tags every operator-tier entry with an additive
    tier="operator" field, suppresses the legacy registration-gated project_claude_md
    branch below (H1 — the project's CLAUDE.md is instead emitted, unconditionally and
    tier="project", by the broader _walk_project_tier three-root walk appended at the
    end), and appends that project-tier walk's own entries to `files`. Default
    (compose=False) behavior is UNCHANGED byte-for-byte. `out_of_root_refs` (T3/H2) is
    mutated in place with any project-tier path that escaped containment; only consulted
    when `compose=True` and `project_root` is set."""
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
                if compose:
                    entry["tier"] = "operator"
                files.append(entry)
                seen.add(key)

    active_slug = None
    if project_root is not None:
        active_slug = _project_slug(project_root)
        # Only count this project's CLAUDE.md via THIS legacy branch if the project is
        # registered under this harness root's projects/<slug>/memory/ (unregistered
        # --project-root defaulting to an unrelated cwd must not leak an unrelated
        # CLAUDE.md), AND compose is off — compose mode emits the project CLAUDE.md via
        # _walk_project_tier below instead, unconditionally on registration (H1: the two
        # paths must never BOTH fire for the same physical file, or it double-counts).
        if not compose and (root / "projects" / active_slug / "memory").is_dir():
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
                        if compose:
                            entry["tier"] = "operator"
                        files.append(entry)
                        seen.add(key)
            else:
                text = _read_checked(root, idx, inaccessible)
                if text is None:
                    continue
                words, lines, tokens_est = _metrics(text)
                variant = {
                    "path": _rel(root, idx),
                    "project_slug": slug,
                    "words": words,
                    "lines": lines,
                    "tokens_est": tokens_est,
                    "evidence": "VERIFIED",
                }
                if compose:
                    variant["tier"] = "operator"
                conditional_variants.append(variant)

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
                if compose:
                    entry["tier"] = "operator"
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
                if compose:
                    entry["tier"] = "operator"
                files.append(entry)
                seen.add(key)

    if compose and project_root is not None:
        files.extend(_walk_project_tier(project_root, inaccessible, errors,
                                         out_of_root_refs if out_of_root_refs is not None else []))

    return files, conditional_variants


def collect_descriptions(
    root: Path, inaccessible: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
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


def collect_on_demand(
    root: Path, project_root: Path | None, inaccessible: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
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


def parse_settings(
    root: Path, errors: list[str], blind_spots: list[str]
) -> tuple[dict[str, Any], bool]:
    """Read + parse root/settings.json. Three distinct outcomes, all NON-fatal —
    build_document always continues and populates every settings-INDEPENDENT section
    (always_loaded, hooks, duplication, phantom_refs, ...), because `headline` is the
    run-to-run diff unit and a one-file settings problem must never fabricate a false
    "everything vanished" diff:
    - Genuinely ABSENT (settings_path does not exist, and is NOT a symlink): the
      common, expected case — nothing wrong. Silent: ({}, False) plus a blind_spot note,
      no errors[] entry.
    - PRESENT but NOT a regular file (a FIFO, socket, directory, or a symlink to one of
      those): an is_file() gate — matching `_read_text`/`_read_head`'s "no blocking
      open() on a FIFO" invariant — rejects it BEFORE any read_text() call, so a FIFO at
      this path can never block the collector forever (open()-for-read on a FIFO with no
      writer waits indefinitely; it never raises). Distinguished from genuine absence via
      a guarded exists() (True for a symlink-to-non-regular; OSError on stat is treated
      conservatively as present). LOUD: a descriptive errors[] entry, return ({}, False).
    - PRESENT but unreadable-as-a-regular-file (any OSError post-gate — PermissionError,
      ELOOP — or JSONDecodeError; OR a FileNotFoundError where settings_path IS a
      symlink, i.e. a PRESENT-but-BROKEN symlink whose target does not exist —
      is_symlink() is True even for a dangling target, so this is distinguished from
      genuine absence): a real anomaly, symmetric handling across all these cases —
      record a descriptive errors[] entry and return ({}, False) so the run continues
      with config evidence INACCESSIBLE, same shape as the absent case, just LOUD
      instead of silent. (main()'s top-level `except Exception` guard remains a
      defense-in-depth backstop for anything unanticipated; it no longer has an organic
      trigger via settings.json specifically — the intended, more robust outcome.)
      Returns (settings_dict, parsed_ok)."""
    settings_path = root / "settings.json"
    if not settings_path.is_file():   # follows symlinks; False for FIFO/socket/dir/broken-symlink/absent
        try:
            present = settings_path.exists()   # follows symlinks; True for a symlink to a FIFO/socket/dir
        except OSError:
            present = True   # stat failed unexpectedly; treat conservatively as present-anomaly
        if present:
            errors.append("settings.json exists but is not a regular file (FIFO/socket/directory); "
                           "refusing to open it to avoid blocking on a special file.")
            return {}, False
        if settings_path.is_symlink():   # exists() False + is_symlink() True == a broken (dangling) symlink
            errors.append("settings.json is a broken symlink (target does not exist)")
            return {}, False
        blind_spots.append("settings.json not found; permissions/config/hooks reflect defaults.")
        return {}, False
    try:
        text = settings_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        errors.append(f"settings.json unreadable: {e!r}")
        return {}, False
    return _parse_json_object_guarded(text, "settings.json", errors)


def _parse_json_object_guarded(text, label, errors):
    """Shared JSON-object shape guard (T3 C22), reused by `parse_settings` (operator) and
    `parse_project_settings` (project tier): malformed JSON, OR any well-formed JSON value
    that is not a top-level object (a number, a string, an array, `null`) degrades to
    `({}, False)` plus a descriptive `errors[]` entry — never a crash, never surfaced as a
    partial/garbage settings dict. `label` names the source in the error text (e.g.
    `"settings.json"`, `"project settings.json"`)."""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        errors.append(f"failed to parse {label}: {e}")
        return {}, False
    if not isinstance(parsed, dict):
        errors.append(f"{label} is not a JSON object; treated as empty.")
        return {}, False
    return parsed, True


def parse_project_settings(project_root, containment_root, containment_root_stat, errors,
                            blind_spots, out_of_root_refs, filename="settings.json"):
    """Project-tier analog of `parse_settings` (T3 foundation for T5's full Local >
    Project > User settings merge across `<repo>/.claude/settings.local.json`,
    `<repo>/.claude/settings.json`, and `~/.claude/settings.json`). Reads
    `<repo>/.claude/<filename>` through the SAME project-tier gate (H2 containment) +
    TOCTOU-closed read (`_project_tier_gate`/`_read_project_file`) as every other
    project-tier sink, then the SAME JSON-object shape guard
    (`_parse_json_object_guarded`) as the operator settings.json. `filename` (T5) lets the
    Local tier (`settings.local.json`) reuse this exact function instead of a duplicate —
    the default `"settings.json"` is the Project tier and keeps every T3 test's error/
    blind-spot string byte-identical. T5 owns the precedence chain across the three
    settings sources and calls this (or `parse_settings` for the User tier) per source
    file. Returns `(settings_dict, parsed_ok)`, same shape as `parse_settings`."""
    settings_path = Path(project_root) / ".claude" / filename
    present, ok = _safe_exists(settings_path)
    if not ok:
        errors.append(f"project {filename} existence check failed for {settings_path}")
        return {}, False
    if not present:
        blind_spots.append(f"project {filename} not found; project-tier settings reflect defaults.")
        return {}, False
    contained, _identity = _project_tier_gate(settings_path, containment_root, containment_root_stat)
    if not contained:
        _record_out_of_root_ref(out_of_root_refs, set(), containment_root, settings_path)
        errors.append(f"project {filename} resolves outside the project containment root; not read.")
        return {}, False
    text, evidence = _read_project_file(settings_path, containment_root, containment_root_stat)
    if evidence == "INACCESSIBLE":
        errors.append(f"project {filename} unreadable or not a regular file: {settings_path}")
        return {}, False
    return _parse_json_object_guarded(text, f"project {filename}", errors)


def collect_permissions(settings: dict[str, Any], parsed_ok: bool) -> dict[str, Any]:
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


def collect_config(
    root: Path, settings: dict[str, Any], parsed_ok: bool, blind_spots: list[str]
) -> dict[str, Any]:
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


def reconcile_hooks(
    root: Path,
    settings: dict[str, Any],
    inaccessible: list[dict[str, Any]],
    blind_spots: list[str],
) -> dict[str, Any]:
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

    disk_files = _hook_disk_files(root)
    disk_names = {p.name for p in disk_files}

    # Containment decision per disk file, computed ONCE. A file whose real path escapes root
    # (leaf symlink, symlinked ANCESTOR dir e.g. `hooks/` itself, or a symlink loop) must not
    # have its bytes read — NOT for dispatcher reachability analysis NOR for description
    # extraction. Catches OSError (broken/perm) and RuntimeError (symlink loop). Records the
    # outside-root blind-spot here so the two downstream loops don't duplicate it.
    try:
        root_stat = os.stat(root)
    except OSError:
        root_stat = None
    fp_inside = {}
    for fp in disk_files:
        try:
            fp_inside[fp] = (root_stat is not None
                             and _resolves_inside_root(fp.resolve(), root, root_stat))
        except (OSError, RuntimeError):
            fp_inside[fp] = False
        if not fp_inside[fp]:
            blind_spots.append(f"hook {fp.name} resolves outside the harness root — not read")

    dispatcher_reached_names = set()
    for disp in (p for p in disk_files if p.name.endswith("-dispatcher.py")):
        if disp.name not in direct_registered_names:
            continue  # a dispatcher confers reachability only if it is itself registered
        if not fp_inside[disp]:
            continue  # out-of-root dispatcher: never read for reachability (bypass closed)
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
                target = os.readlink(fp)        # keep the raw link string for the `target` field
            except OSError:
                _append_inaccessible_once(inaccessible, _rel_safe(root, fp))

        if name in direct_registered_names:
            registered_via, evidence = "direct", "VERIFIED"
        elif name in dispatcher_reached_names:
            registered_via, evidence = "dispatcher", "INFERRED"
        else:
            registered_via, evidence = "none", "INFERRED"

        desc, desc_status = _script_description(fp, skip_read=not fp_inside[fp])
        if desc_status == "INACCESSIBLE":
            _append_inaccessible_once(inaccessible, _rel_safe(root, fp))

        scripts_on_disk.append({
            "name": name, "is_symlink": is_link, "target": target,
            "registered_via": registered_via, "evidence": evidence,
            "description": desc,
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


# --- T5: settings / hooks / MCP full-chain merge (compose mode only) ---
# tier-precedence: CC-docs (HIGH confidence), live-verify deferred 2026-07-22 (T1
# RESOLUTION). Three settings SOURCES — User (`~/.claude/settings.json`, the operator's
# own `parse_settings` result), Project (`<repo>/.claude/settings.json`), Local
# (`<repo>/.claude/settings.local.json`) — precedence Local > Project > User for every
# key EXCEPT `permissions`, which instead MERGES (union, deny wins a same-rule conflict —
# §3 merge table). Hooks are a separate merge rule again: UNION, every tier's matching
# hooks fire, no precedence winner. Every function below is additive/compose-only; the
# operator-only `parse_settings`/`collect_permissions`/`reconcile_hooks`/`collect_config`
# above are UNCHANGED so non-compose output stays byte-identical.

def _iter_hook_entries(settings):
    """Like `_iter_hook_commands`, but yields `(event, matcher, command)` instead of just
    `command` — a composed hook record (T5 R5-B) needs the event key and matcher string
    per source, which the plain command-only iterator (used by `build_document`'s
    operator-only hooks section and by `iter_input_paths`) does not carry. Duplicates
    `_iter_hook_commands`'s exact input-shape guards (T3 C22: a malformed `entries`/
    `entry`/`entry_hooks` shape is skipped, never crashes) rather than refactoring that
    already-shipped, already-tested function mid-task."""
    hooks_cfg = settings.get("hooks", {})
    if not isinstance(hooks_cfg, dict):
        return
    for event, entries in hooks_cfg.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            matcher = entry.get("matcher")
            entry_hooks = entry.get("hooks", [])
            if not isinstance(entry_hooks, list):
                continue
            for h in entry_hooks:
                if isinstance(h, dict) and h.get("type") == "command" and isinstance(h.get("command"), str):
                    yield event, matcher, h["command"]


def _merge_permissions_union_deny_wins(settings_stack_with_ok):
    """T5 §3: `permissions` is the ONE settings.json key that MERGES across tiers rather
    than overriding by precedence — union of allow/deny/ask rule strings from every tier,
    deny wins a same-rule conflict (a rule denied by ANY tier is denied, even if another
    tier allows/asks it). `settings_stack_with_ok` is `[(tier, settings_dict, parsed_ok),
    ...]` — order does not matter for a union+deny-wins merge, but callers pass the shared
    Local>Project>User triple every T5 function consumes for consistency. `evidence` is
    `"VERIFIED"` when at least one tier's settings actually parsed as a real JSON object,
    `"INACCESSIBLE"` only when every tier is absent/malformed (mirrors
    `collect_permissions`'s single-source evidence convention, generalized to "at least
    one real source" for a union).

    P3 hardened: "parsed" used to be re-derived from `bool(settings)` — a legitimately
    PRESENT, VALID, but EMPTY `{}` settings.json (parses fine, just has no keys) is
    falsy, so it was silently treated as absent/malformed and could flip `evidence` to
    INACCESSIBLE even though every tier parsed successfully. FIX: track parse success via
    the explicit `parsed_ok` flag each tier's own `parse_settings`/`parse_project_settings`
    call already returns (and `collect_permissions`, the non-compose single-tier sibling,
    already uses correctly) — NOT dict truthiness — so "present and empty" is VERIFIED,
    never conflated with "absent or malformed"."""
    deny: set[str] = set()
    allow: set[str] = set()
    ask: set[str] = set()
    any_parsed = False
    for _tier, settings, parsed_ok in settings_stack_with_ok:
        if not parsed_ok:
            continue
        any_parsed = True
        perms = settings.get("permissions", {})
        if not isinstance(perms, dict):
            continue
        for bucket, target in (("allow", allow), ("deny", deny), ("ask", ask)):
            rules = perms.get(bucket, [])
            if isinstance(rules, list):
                target.update(r for r in rules if isinstance(r, str))
    allow -= deny
    ask -= deny
    return {
        "allow_count": len(allow), "deny_count": len(deny), "ask_count": len(ask),
        "evidence": "VERIFIED" if any_parsed else "INACCESSIBLE",
    }


def _compose_hooks(sources, project_root, out_of_root_refs):
    """T5 R5-B: source-aware hook UNION across User/Project/Local (§3: hooks merge by
    union — every matching hook fires regardless of tier, unlike settings scalars/MCP
    which pick one precedence winner). Each record retains `event`, `matcher`, `tier`, and
    `source_file` alongside `command`/`script`/`exists`. `sources` is `[(tier,
    settings_dict, source_file_str_or_None, resolve_root_path_or_None), ...]`. A
    project/local hook's `command`/script path resolves against the REPO ROOT
    (`resolve_root` passed per-source via `_script_from_command`), never the operator
    root — a project-tier `./hooks/x.py` command means `<repo>/hooks/x.py`. The user
    (operator) tier keeps its existing trusted symlink-following (plain `.exists()`); a
    project/local tier script path is routed through T3's `_project_tier_gate` (H2
    containment) before its existence is reported — an escaping symlink target is
    recorded as an `out_of_root_ref`, `exists` reported as `None` (unknown/untrusted),
    never followed."""
    containment_stat = None
    if project_root is not None:
        try:
            containment_stat = os.stat(project_root)
        except OSError:
            containment_stat = None
    seen_refs: set[str] = set()
    records: list[dict[str, Any]] = []
    for tier, settings, source_file, resolve_root in sources:
        if not settings or resolve_root is None:
            continue
        for event, matcher, command in _iter_hook_entries(settings):
            script_path, _note = _script_from_command(command, resolve_root)
            script_rel = _rel_safe(resolve_root, script_path) if script_path is not None else None
            if script_path is None:
                exists = None
            elif tier == "user":
                try:
                    exists = script_path.exists()
                except OSError:
                    exists = False
            elif containment_stat is None:
                exists = None
            else:
                contained, _identity = _project_tier_gate(script_path, project_root, containment_stat)
                if not contained:
                    _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, script_path)
                    exists = None
                else:
                    exists = True
            records.append({"event": event, "matcher": matcher, "command": command,
                            "script": script_rel, "exists": exists,
                            "tier": tier, "source_file": source_file})
    return records


# The ONLY non-permission settings.json keys this readout ever surfaces (M5/P2-9) — the
# SAME small set `collect_config` already treats as safe/non-secret-shaped. A key outside
# this allowlist is NEVER surfaced here even if a fixture defines it at multiple tiers:
# an arbitrary/unknown settings key could hold anything, and "allowlist, don't guess" is
# the same secret-safety posture `collect_config.env_keys` uses for `env`.
_SETTINGS_OVERRIDE_ALLOWLIST = ("model", "cleanupPeriodDays", "sandbox", "enabledPlugins")


def _settings_scalar_value(key, settings):
    """Best-effort safe scalar readout for `key` from `settings` (T5 P2-9) — mirrors
    `collect_config`'s own coercions for its two non-trivially-shaped keys (`sandbox`'s
    nested-object form, `enabledPlugins`'s name->bool map) so `settings_overrides`
    reports the SAME winning value a human would see in `config`, never a raw container
    that could carry something unexpected."""
    if key == "sandbox":
        raw = settings.get("sandbox")
        return bool(raw.get("enabled", False)) if isinstance(raw, dict) else bool(raw)
    if key == "enabledPlugins":
        raw = settings.get("enabledPlugins", {})
        return {k: bool(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    return settings.get(key)


_SETTINGS_OVERRIDE_VALUE_MAX_LEN = 200


def _settings_override_safe_value(value):
    """P1-C: type-gate `winning_value` — `_SETTINGS_OVERRIDE_ALLOWLIST` only restricts
    which KEY NAMES are surfaced; on its own it does nothing to stop an attacker from
    stuffing an arbitrary nested object under an allowlisted key (project settings
    `{"model": {"token": "SECRET"}}` — "model" IS allowlisted, so the raw dict would
    otherwise be emitted verbatim). Returns `(winning_value, value_kind)`: a safe SCALAR
    (`str`/`int`/`float`/`bool`/`None`) passes through as `(value, None)`; an
    over-`_SETTINGS_OVERRIDE_VALUE_MAX_LEN` str is NEVER emitted whole, returning
    `(None, "redacted")` rather than risking a leaked secret-bearing string; any other
    non-scalar (`dict`/`list`) is NEVER emitted, returning `(None, "complex")`."""
    if value is None or isinstance(value, (bool, int, float)):
        return value, None
    if isinstance(value, str):
        if len(value) > _SETTINGS_OVERRIDE_VALUE_MAX_LEN:
            return None, "redacted"
        return value, None
    return None, "complex"


def _settings_overrides(settings_stack):
    """M5/P2-9: non-permission settings-key overrides across the full Local > Project >
    User chain, SECRET-SAFE — routed through `_SETTINGS_OVERRIDE_ALLOWLIST`, the same
    small allowlist posture as `collect_config`, PLUS a value-TYPE gate
    (`_settings_override_safe_value`, P1-C) so an allowlisted key cannot smuggle an
    arbitrary nested object/oversized string past the key-name allowlist. `settings_stack`
    MUST already be in precedence order `[("local", ...), ("project", ...), ("user",
    ...)]` — the first tier in the list that defines a key is the winner. `env` is
    special-cased: only the CHANGED key NAMES are reported (never a value), mirroring
    `collect_config.env_keys`. NOT folded into `tier_composition`'s node "overrides" count
    (T4) — that count is node-surface shadowing (skills/commands/agents); this is
    scalar-key overriding."""
    overrides = []
    present_tiers = [(tier, s) for tier, s in settings_stack if s]
    for key in _SETTINGS_OVERRIDE_ALLOWLIST:
        defining = [(tier, _settings_scalar_value(key, s)) for tier, s in present_tiers if key in s]
        if len(defining) < 2:
            continue
        winner_tier, winner_raw = defining[0]
        winning_value, value_kind = _settings_override_safe_value(winner_raw)
        entry = {"key": key, "winning_tier": winner_tier, "winning_value": winning_value,
                "overridden_tiers": [t for t, _ in defining[1:]]}
        if value_kind is not None:
            entry["value_kind"] = value_kind
        overrides.append(entry)
    # P2 fix (cross-model review): winner selection is by KEY PRESENCE (`isinstance(...,
    # dict)`), not by truthiness of the value — `and s["env"]` used to treat a tier that
    # explicitly sets an EMPTY env (`{}`) as "does not define env", so a higher-precedence
    # tier's deliberate empty env lost to a lower tier's non-empty one instead of winning.
    env_by_tier = [(tier, sorted(s["env"].keys())) for tier, s in present_tiers
                   if isinstance(s.get("env"), dict)]
    if env_by_tier:
        winner_tier, winner_keys = env_by_tier[0]
        overridden = [t for t, keys in env_by_tier[1:] if keys != winner_keys]
        if overridden:
            overrides.append({"key": "env", "winning_tier": winner_tier,
                              "winning_value": winner_keys, "overridden_tiers": overridden})
    return overrides


def _mcp_servers_from(obj):
    """`obj["mcpServers"]` if `obj` is a dict and that key is itself a dict; else `{}`.
    Guards every static MCP projection the same way (T3 C22 input-shape discipline: a
    malformed shape degrades to empty, never crashes)."""
    if not isinstance(obj, dict):
        return {}
    servers = obj.get("mcpServers")
    return servers if isinstance(servers, dict) else {}


def _redact_mcp_server(name, tier, source_file, raw):
    """One MCP server registration -> the SECRET-SAFE emitted record (R4). `raw` (the
    server's config dict as found in the source JSON) is NEVER stored or returned
    verbatim — only a fixed allowlist of non-secret fields plus `env`/`headers` KEY NAMES
    survive (mirrors `collect_config.env_keys`: names only, never values). `command`/
    `url`/`args` are deliberately OMITTED — unlike `env`/`headers`, the plan does not
    document those as secret-safe, and a CLI arg list can legally carry an inline
    `--api-key=...` flag; omitting is the conservative default until a real need to
    surface them is confirmed. `enabled` reflects the server's OWN `disabled` field
    verbatim (`disabled: true` -> `enabled: False`) — the collector does not invent a
    cross-reference against an unconfirmed settings.json approval-list schema."""
    if not isinstance(raw, dict):
        raw = {}
    env = raw.get("env")
    headers = raw.get("headers")
    server_type = raw.get("type") if isinstance(raw.get("type"), str) else None
    return {
        "name": name, "tier": tier, "source_file": source_file, "type": server_type,
        "enabled": not bool(raw.get("disabled", False)),
        "env_keys": sorted(env.keys()) if isinstance(env, dict) else [],
        "header_keys": sorted(headers.keys()) if isinstance(headers, dict) else [],
    }


def collect_composed_mcp(project_root, errors, blind_spots, out_of_root_refs):
    """T5 R4: MCP server registrations from the three EXACT static projections, Local >
    Project > User precedence, secret-safe (see `_redact_mcp_server`). Sources:
      User:    ~/.claude.json    -> mcpServers
      Local:   ~/.claude.json    -> projects[<project_containment_root abspath>].mcpServers
      Project: <repo>/.mcp.json  -> mcpServers  (untrusted -> T3 `_project_tier_gate`)
    `~/.claude.json` is the operator's OWN file — TRUSTED, no containment gate — and is
    ALWAYS the real `$HOME/.claude.json`, never `--root`-relative: `CLAUDE_CONFIG_DIR`
    redirects only the harness/skills tree (`_default_operator_root`), never this
    per-user registry, per CC's documented behavior. Returns a name-sorted list; when a
    name is registered at more than one tier, the HIGHEST-precedence tier's registration
    wins (Local, then Project, then User) — matching every other T5 precedence merge."""
    user_claude_json_path = Path.home() / ".claude.json"
    user_json = {}
    text, evidence = _read_text(user_claude_json_path)
    if evidence == "VERIFIED":
        user_json, _ok = _parse_json_object_guarded(text, "~/.claude.json", errors)
    else:
        blind_spots.append("~/.claude.json not found or unreadable; MCP User/Local tier "
                            "registrations reflect defaults.")

    user_servers = _mcp_servers_from(user_json)

    local_servers = {}
    if project_root is not None:
        projects_key = str(Path(project_root).expanduser().resolve())
        projects_map = user_json.get("projects") if isinstance(user_json, dict) else None
        project_entry = projects_map.get(projects_key) if isinstance(projects_map, dict) else None
        local_servers = _mcp_servers_from(project_entry)

    project_servers = {}
    if project_root is not None:
        mcp_json_path = Path(project_root) / ".mcp.json"
        present, ok = _safe_exists(mcp_json_path)
        if not ok:
            errors.append(f".mcp.json existence check failed for {mcp_json_path}")
        elif present:
            try:
                containment_stat = os.stat(project_root)
            except OSError:
                containment_stat = None
            if containment_stat is None:
                errors.append(f"project containment root not accessible for {mcp_json_path}")
            else:
                contained, _identity = _project_tier_gate(mcp_json_path, Path(project_root), containment_stat)
                if not contained:
                    _record_out_of_root_ref(out_of_root_refs, set(), Path(project_root), mcp_json_path)
                else:
                    text, evidence = _read_project_file(mcp_json_path, Path(project_root), containment_stat)
                    if evidence == "VERIFIED":
                        proj_json, _ok = _parse_json_object_guarded(text, ".mcp.json", errors)
                        project_servers = _mcp_servers_from(proj_json)
                    else:
                        errors.append(f".mcp.json unreadable or not a regular file: {mcp_json_path}")

    project_source = str(Path(project_root) / ".mcp.json") if project_root is not None else None
    tiered = (
        ("local", local_servers, str(user_claude_json_path)),
        ("project", project_servers, project_source),
        ("user", user_servers, str(user_claude_json_path)),
    )
    resolved = {}
    for tier, servers, source_file in tiered:
        for name, raw in servers.items():
            if name in resolved:
                continue  # a higher-precedence tier (earlier in `tiered`) already won
            resolved[name] = _redact_mcp_server(name, tier, source_file, raw)
    return sorted(resolved.values(), key=lambda s: s["name"])


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


def _deduped_instruction_files(root: Path, inaccessible: list[dict[str, Any]],
                               blind_spots: list[str]) -> list[Path]:
    """Shared glob-walk + dedup for the instruction-file corpus (S2.M3): the SINGLE
    definition of "the deduped instruction-file set" consumed by BOTH
    flag_long_instructions (line-count flags) and collect_git_age's caller (staleness
    signal) -- add a new instruction-file glob to _INSTRUCTION_GLOBS, never inline a
    second glob loop here or elsewhere.

    `seen` dedupes a file reachable via multiple glob paths (a deploy symlink under
    agents/ + its canonical submodule source under skills/*/agents/). The glob order
    in _INSTRUCTION_GLOBS lists skills/*/agents/*.md BEFORE agents/*.md, so the canonical
    path (the one you'd actually edit to shorten the file) is seen first and returned;
    the deploy-symlink duplicate is skipped.

    skills/*/rules/*.md generalizes the coding-team-only rules scan (release portability,
    matching walk_always_loaded/scan_duplication/_staleness_corpus); listed right after
    rules/*.md so a physically-symlinked rule is seen/returned under its rules/*.md path
    first, same precedence as those three scans.

    Filtered to files _read_text can actually read (mirrors flag_long_instructions'
    INACCESSIBLE skip) so an unreadable instruction file is excluded from both the
    length-flag scan and the git-age signal, consistently.

    Codex #6 (S2 gate fix): an unreadable instruction file used to be dropped with a bare
    `continue` -- absent from the length-flag scan, absent from staleness.last_commit_ts,
    and therefore un-nameable by staleness_null_reasons' closed enum (which only describes
    keys that EXIST in last_commit_ts). It is now recorded in `inaccessible[]` via
    _append_inaccessible_once, which dedupes across this function's TWO callers
    (flag_long_instructions and build_document's staleness path) and against any entry a
    dispatcher read already produced for the same path. A file whose real path escapes
    root is refused before the read and recorded as a blind spot instead, matching the
    hook-script walk's containment gate."""
    seen = set()
    result: list[Path] = []
    try:
        root_stat = os.stat(root)
    except OSError:
        root_stat = None
    if root_stat is None:
        # QA exit gate (MEDIUM 4), the class T9 round 3 fixed in build_git_repo_index (F9):
        # an unstat-able root is an UNKNOWN, not a determined "resolves outside the root"
        # fact -- _resolves_inside_root is never even called. Every file used to fall
        # through the gate below and emit the per-file message, asserting containment the
        # collector never evaluated (binding rule 6). ONE aggregate note instead, worded
        # DISTINCTLY from that message, and emitted unconditionally: with the root
        # unstat-able, "no instruction files exist" and "we could not look" are
        # indistinguishable, so a glob-count gate would suppress the only honest signal.
        msg = ("instruction files: the harness root could not be stat'd — none was read "
               "(containment is undecidable, not confirmed outside the root)")
        if msg not in blind_spots:      # called twice per run (two callers)
            blind_spots.append(msg)
        return result
    for pattern in _INSTRUCTION_GLOBS:
        # Codex #4 (S2 gate fix): sort BEFORE dedup. root.glob() yields in filesystem
        # order, so both the `seen` winner and the returned order were filesystem
        # dependent. D4's budget-exhaustion truncation silences a SUFFIX of this list, so
        # an unsorted order would make "which files went unmeasured" nondeterministic.
        # Sorted by the string form, matching build_document's rel-path sort (F11).
        for fp in sorted(root.glob(pattern), key=str):
            key = _physical_key(fp)
            if key in seen:
                continue
            seen.add(key)
            # Codex R2-F7: containment gate BEFORE the read -- `key` is already the
            # realpath, so this costs no second resolve. The hook walk 450 lines up
            # refuses the identical case and SAYS SO; this walk now does both too.
            try:
                # `root_stat` is non-None here (the undecidable case returned above), so a
                # False verdict now always comes from a containment check that actually RAN.
                inside = _resolves_inside_root(Path(key), root, root_stat)
            except (OSError, RuntimeError):
                inside = False
            if not inside:
                msg = (f"instruction file {_rel_safe(root, fp)} resolves outside "
                       f"the harness root — not read")
                if msg not in blind_spots:      # called twice per run (two callers)
                    blind_spots.append(msg)
                continue
            text, _evidence = _read_text(fp)
            if text is None:
                _append_inaccessible_once(inaccessible, _rel_safe(root, fp))
                continue
            result.append(fp)
    return result


def flag_long_instructions(root: Path, inaccessible: list[dict[str, Any]],
                           blind_spots: list[str]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for fp in _deduped_instruction_files(root, inaccessible, blind_spots):
        text, evidence = _read_text(fp)
        if text is None:
            # Codex R2-F11: this is the SECOND read of fp this run. _deduped_
            # instruction_files read it successfully moments ago, so text=None here
            # means the file's readability CHANGED mid-run -- still a silent-drop
            # unless recorded. _append_inaccessible_once dedupes against the first
            # walk's entry, so a file unreadable in BOTH walks appears exactly once.
            _append_inaccessible_once(inaccessible, _rel_safe(root, fp))
            continue
        n = len(text.splitlines())
        if n > INSTRUCTION_LINE_LIMIT:
            flags.append({"path": _rel(root, fp), "lines": n,
                          "threshold": INSTRUCTION_LINE_LIMIT, "evidence": evidence})
    return flags


# S2 gate fix: per-git-invocation timeout for SINGLE-PATH calls (rev-parse, per-file log).
_GIT_SUBPROCESS_TIMEOUT = 2
# Batched calls scale with total tracked-file count / history depth, not one path, so a 2s
# cap risks spurious nulls on a legitimately larger repo. Measured 0.015-0.234s on the live
# corpus -- 20-300x headroom at 5s.
_GIT_BATCH_TIMEOUT = 5

# D5 (S2 gate fix): the CLOSED enum. Free text is forbidden -- the in-repo precedent is
# decisive (build_civc_model documents a P1 class-injection finding fixed by allowlisting
# `verdict`, so a crafted value "can never ride through as an extra CSS class"), and
# reasons naturally want to become CSS classes and data- attrs. Additionally, git error
# text carries absolute paths and .gitmodules/.git/config values, so surfacing stderr
# would move credential-bearing text into a published HTML document. Any variable text
# goes to errors[] / inaccessible[], which already route through esc_html.
#
# TEN values, not nine (F4): `no_commits` exists because mapping a tracked-but-never-
# committed path to `unparseable` ("stdout was not an integer") would be a MISLEADING
# reason. Sized so D3 and D4 land without a second schema change.
_GIT_NULL_REASONS = (
    "git_unavailable", "no_repo", "outside_root", "untracked", "submodule_unavailable",
    "timeout", "budget_exhausted", "git_error", "unparseable", "no_commits",
)


def _checked_git_reason(reason: str) -> str:
    """QA exit gate (LOW 5): the ONE place a null reason enters the sidecar, so the enum
    above is closed by the EMIT PATH rather than by a test that only reads it afterwards
    (its former only consumer).

    Fail-CLOSED, not fatal: an off-enum value is replaced with `git_error` -- a wrong-but-
    bounded reason costs the operator one imprecise label, whereas raising would take down
    a whole run's git-age data over a labelling bug, and passing it through would put
    unvetted text (git's own, which carries absolute paths and .gitmodules/.git/config
    values) into a document that renders reasons as CSS classes and data- attrs. The
    discard is announced on stderr -- ephemeral, operator-local, never the sidecar -- so a
    future drift is loud without being publishable."""
    if reason in _GIT_NULL_REASONS:
        return reason
    print(f"warning: internal: discarding off-enum git-age null reason {reason[:80]!r}",
          file=sys.stderr)
    return "git_error"

# Total wall budget for the git-age subsystem, with ONE named exemption (Codex F5): the
# deadline is computed in build_document BEFORE build_git_repo_index and threaded into
# BOTH discovery and the per-file loop. Covered: every per-root ls-files load, every
# non-root toplevel discovery (including its provenance probe), every per-file log.
# Exempt: the scanned root's OWN availability probe, which defines `available` -- a value
# exhaustion must never flip (a budget running out is not evidence the root is not a work
# tree). That probe is TWO subprocesses, not one (T10 audit, LOW): `_git_toplevel`, then
# `_git_common_dir` via _toplevel_refusal, each capped at _GIT_SUBPROCESS_TIMEOUT, so the
# exempt window is up to 4s. Hard ceiling therefore = 4s exempt probes + this budget + one
# in-flight subprocess timeout (<=5s) ~= 19s, replacing today's UNBOUNDED 230-260s worst
# case.
# 4.5x the measured 2.24s typical; a backstop for a degenerate case (huge-history
# submodule, network-mounted .git, hung git), not a perf target. DELIBERATELY not tied
# to --check's <=5s: that budget covers a different, intentionally-thin path.
_GIT_TOTAL_BUDGET = 10.0

# Harden-audit fix (T9 round 2, HIGH): command-line -c OUTRANKS repo config, so these
# neutralize every command-valued key a discovered repository could carry. VERIFIED
# EMPIRICALLY (git 2.50.1): `ls-files -s -z` EXECUTES core.fsmonitor -- twice -- in the
# repo it runs in, while rev-parse and log do not. The gitlink fence accepts a submodule
# toplevel on PATH evidence, and an attacker who can write inside that subtree controls
# its .git/config (or replaces .git with a gitfile pointing at their own repo), so
# without this the batched index read is arbitrary command execution. The extra keys are
# defense in depth for subcommands added to this wrapper later.
_GIT_SAFE_CONFIG = [
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "diff.external=",
    "-c", "core.pager=cat",
    "-c", "core.sshCommand=",
]


def _git(args: list[str], cwd: Path, timeout: int
         ) -> tuple[subprocess.CompletedProcess[bytes] | None, str | None]:
    """Control 4 (S2 gate fix): the SINGLE entry point for every git invocation in this
    module. Returns (proc, None) or (None, closed-enum reason). NEVER raises.

    `--literal-pathspecs` (Codex #2): a pathspec is never glob- or magic-interpreted.
    Verified sufficient for `*`, `?`, `[...]`, backslash escapes AND the magic forms
    `:(glob)`, `:!`, `:/`; leading `-` is already handled by the existing `--`. The FLAG,
    not GIT_LITERAL_PATHSPECS: passing a bare `env=` dict to subprocess.run REPLACES the
    inherited environment (no PATH, no HOME) -- a footgun with a silent failure mode. The
    flag is also assertable in a test.

    `GIT_OPTIONAL_LOCKS=0` keeps binding rule 4's read-only posture belt-and-braces: git
    never takes an index lock on our behalf. Set on a COPY of os.environ for the same
    reason as above.

    BYTES, never text=True (Security S14): `ls-files -z` emits PATHS, and a non-UTF-8
    filename raises UnicodeDecodeError -- a ValueError, NOT in the (OSError,
    TimeoutExpired) tuple every existing call site catches -- so it would escape uncaught
    and violate the envelope rule from INSIDE the collector. Callers decode via
    _decode_git.

    `timeout` and `git_error` are returned DISTINCTLY rather than collapsed to one None:
    guessing which failure occurred is the exact defect class this batch eliminates.

    SECRET SAFETY (binding rule 11): `proc.stderr` is captured and MUST NEVER be read by
    any caller. Git error text carries absolute paths, and submodule failures carry
    .gitmodules / .git/config values -- this repo's .gitmodules already holds a remote URL
    (`git@github.com:...`), and an HTTPS-with-token submodule would put
    `https://user:token@host/...` there. Surfacing it would move credential-bearing text
    into a published HTML document. Closed-enum reasons only.

    COMMAND-VALUED CONFIG (T9 harden round 2, HIGH): every invocation carries
    `_GIT_SAFE_CONFIG`. Measured on git 2.50.1 that `ls-files -s -z` EXECUTES
    core.fsmonitor -- twice -- in whatever repo it runs in, while `rev-parse
    --show-toplevel`, `rev-parse --git-common-dir` and `log -1 --format=%ct` execute
    nothing. The command line is the right layer because `-c` OUTRANKS repo config
    unconditionally: it holds no matter which repository a call site is pointed at,
    whereas auditing each discovered .git/config would have to be redone for every
    subcommand added later. Verified: `git -c core.fsmonitor= ls-files -s -z` returns the
    index with NO payload run."""
    env = dict(os.environ)
    env["GIT_OPTIONAL_LOCKS"] = "0"
    # Harden-audit fix (T8 round 2): these four vars silently redirect git away from
    # `cwd` to a different repo/index/object store -- inherited from the invoking
    # process they would produce plausible-but-wrong timestamps for the WRONG repo,
    # the exact defect class this batch eliminates. cwd= is the single source of
    # repo-targeting truth for this wrapper.
    for redirect_var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                        "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(redirect_var, None)
    try:
        return subprocess.run(
            ["git", *_GIT_SAFE_CONFIG, "--literal-pathspecs", *args],
            cwd=cwd, capture_output=True, timeout=timeout, env=env,
        ), None
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError:
        return None, "git_error"


def _decode_git(raw: bytes) -> str:
    """Decode git stdout with surrogateescape (Security S14). Verified: `-z` ignores
    core.quotePath (forcing `-c core.quotePath=true` still emitted raw bytes), so `-z` +
    surrogateescape round-trips a non-UTF-8 filename EXACTLY. render_html.esc_html already
    neutralizes lone surrogates on the render side."""
    return raw.decode("utf-8", errors="surrogateescape")


def _git_marker_root(start: Path, stop_at: str) -> tuple[Path | None, bool]:
    """Nearest ancestor of `start` STRICTLY BELOW `stop_at` containing a `.git` entry ->
    (dir, ok). ZERO subprocesses.

    `.git` is a DIRECTORY in a normal clone and a FILE in a submodule or a linked
    worktree, so this tests for either via the existing tri-state _safe_exists.

    `stop_at` is the scanned root's realpath and is EXCLUSIVE -- the walk answers only
    "is there a NESTED work tree between this dir and the scanned root?". Two reasons it
    is bounded rather than climbing to the filesystem root:
      (1) The scanned root's own toplevel is ALREADY known (one `rev-parse` in
          build_git_repo_index, which climbs for us), so probing root/.git and everything
          above it re-derives a known answer from extra filesystem reads -- including reads
          ABOVE the scanned root, which the containment posture of this whole batch exists
          to avoid.
      (2) `.git` sits in _PRUNED_WALK_DIRS, so iter_input_paths cannot watch `root/.git`;
          probing it would break the standing invariant (asserted by
          test_iter_input_paths_is_superset_of_real_build_document_reads) that every path
          build_document reads under root is watched. Every dir this walk DOES probe lies
          under an instruction-file dir that iter_input_paths already yields.

    TRI-STATE, not two-state (F5): `ok=False` means an ancestor's `.git` could not be read
    (Path.exists() RAISES PermissionError -- it only swallows ENOENT/ENOTDIR), so the walk
    ABORTS rather than silently attributing the file to an outer repo. The caller maps that
    to `git_error`, NEVER to `no_repo`: "there is no enclosing work tree" is a positive
    assertion of absence, and asserting absence over a state we could not read is the exact
    defect class this batch exists to eliminate. `(None, True)` means the honest "no nested
    work tree found before the boundary" -- the caller then falls back to the scanned root's
    own toplevel, never to a guess. The filesystem-root termination is a backstop for the
    exotic case where containment passed by inode identity through a directory hard-link /
    bind-mount alias, so the lexical climb never meets `stop_at`."""
    cur = start
    while str(cur) != stop_at:
        present, ok = _safe_exists(cur / ".git")
        if not ok:
            return None, False
        if present:
            return cur, True
        if cur.parent == cur:
            return None, True
        cur = cur.parent
    return None, True


def _git_toplevel(dir_path: Path) -> tuple[str | None, str | None]:
    """`git -C <dir> rev-parse --show-toplevel` -> (physical toplevel, None) or
    (None, closed-enum reason).

    SUBSUMES the DELETED _git_work_tree_available and is strictly stricter: verified that
    in a BARE repo --show-toplevel exits 128 while --is-inside-work-tree exits 0 printing
    "false". Returns a physical path even from a symlinked cwd, so it compares directly
    against os.path.realpath -- which is what makes the realpath-first ordering work.

    THE REASON IS DISCRIMINATED, not collapsed (correction C-f, amending design §13 F2).
    An OSError from the wrapper means the git BINARY could not be executed -- the ONLY
    honest producer of `git_unavailable`. A CLEAN non-zero exit means git ran perfectly and
    reported that this is not a work tree (or is bare): that is `no_repo`. Verified live
    that conftest.py's fake_harness has no `git init`, so EVERY fixture run takes the
    clean-non-zero branch with git fully installed; labelling it `git_unavailable` would
    tell the operator "git could not run at all" when git ran fine."""
    proc, err = _git(["rev-parse", "--show-toplevel"], dir_path, _GIT_SUBPROCESS_TIMEOUT)
    if proc is None:
        return None, "git_unavailable" if err == "git_error" else "timeout"
    if proc.returncode != 0:
        return None, "no_repo"
    out = _decode_git(proc.stdout).strip()
    return (out, None) if out else (None, "no_repo")


def _git_common_dir(dir_path: Path) -> str | None:
    """Absolute git-common-dir for the repo at `dir_path`, or None.

    Harden-audit fix (T9 round 2, HIGH): the gitlink fence proves a PATH is named as a
    submodule by an accepted parent's index; it cannot prove the `.git` at that path
    BELONGS to that parent. An attacker who can write inside the named subtree replaces
    `.git` with a gitfile (`gitdir: <their repo>`) -- --show-toplevel still reports the
    containing directory, so the path still matches -- and the subsequent batched
    `ls-files` binds to a FOREIGN repository (demonstrated end to end). Requiring the
    git-common-dir to resolve INSIDE the harness root closes that: the attacked submodule
    reports `.../evil/.git` where an honest one reports a dir under the root.

    VERIFIED INERT: this rev-parse form executes no command-valued config."""
    proc, _err = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                      dir_path, _GIT_SUBPROCESS_TIMEOUT)
    if proc is None or proc.returncode != 0:
        return None
    out = _decode_git(proc.stdout).strip()
    return out or None


def _toplevel_refusal(top: str, root: Path,
                      root_stat: os.stat_result | None) -> str | None:
    """None when the work tree at `top` may be probed, else a phrase saying why not.

    BOTH halves are required and neither alone is sufficient (T9 harden round 2): the
    work-tree PATH must lie inside the harness root (a `git -C` outside it binds to a
    foreign repository -- S17), AND the git-common-dir must too (a gitfile inside an
    accepted path can point the SAME path at someone else's repository). The phrase is
    returned rather than a bare bool because the caller publishes it as a blind spot:
    a silent refusal is what Finding 4 of the same audit round was about."""
    if root_stat is None:
        return "the harness root could not be stat'd, so containment is undecidable"
    if not _resolves_inside_root(Path(top), root, root_stat):
        return f"its work tree ({top}) resolves outside the harness root"
    common_dir = _git_common_dir(Path(top))
    if common_dir is None:
        return f"the git directory backing {top} could not be resolved"
    if not _resolves_inside_root(Path(common_dir), root, root_stat):
        return (f"the git directory backing {top} ({common_dir}) resolves outside the "
                f"harness root")
    return None


def _git_tracked_and_gitlinks(top: Path) -> tuple[frozenset[str], frozenset[str]] | None:
    """One batched `git ls-files -s -z` per confirmed repo root -> (tracked, gitlinks).

    NO PATHSPEC is passed: the whole index is cheaper (measured 0.013-0.015s, 3465 paths,
    262,464 bytes, ~577 KB as a Python set -- ~5.9 MB estimated at 10x scale) AND
    structurally immune to the pathspec-glob class, because there is no pathspec to
    interpret. `-s` costs nothing measurable and buys the mode-160000 gitlink set that the
    S17 containment fence needs HERE and that `submodule_unavailable` needs at T10.

    Returns None -- NOT an empty frozenset -- when the index could not be determined. T10
    maps None to `git_error`; an unknown index must NEVER masquerade as the definitive
    negative `untracked` (S15, P1).

    S15, restated because it is the reason this is per-ROOT: `git ls-files` does NOT
    descend into submodules -- the parent's index reports only the gitlink (verified). A
    parent-only set would mark 46 submodule files `untracked`, replacing an honest "we
    don't know" with a specific WRONG answer. That is worse than shipping the blind spot."""
    proc, _err = _git(["ls-files", "-s", "-z"], top, _GIT_BATCH_TIMEOUT)
    if proc is None or proc.returncode != 0:
        return None
    tracked: set[str] = set()
    gitlinks: set[str] = set()
    for chunk in proc.stdout.split(b"\0"):
        if not chunk:
            continue
        head, _tab, raw_path = chunk.partition(b"\t")
        if not raw_path:
            continue
        rel = _decode_git(raw_path)
        tracked.add(rel)
        if head.startswith(b"160000"):
            gitlinks.add(rel)
    return frozenset(tracked), frozenset(gitlinks)


class _GitRepoIndex(NamedTuple):
    """One-shot git topology snapshot for a run (S2 gate fix: R1/N1/#1/#5/S15/S17).

    Built ONCE by build_git_repo_index and threaded into collect_git_age -- never
    re-probed mid-run, so staleness.git_age_available can no longer disagree with the
    timestamps it labels (Codex #5, dissolved STRUCTURALLY: _git_work_tree_available is
    deleted, not kept)."""
    available: bool
    # Closed-enum reason when `available` is False, else None (C-f). A SCALAR paired with
    # `available` at construction, so the two cannot drift apart.
    root_reason: str | None
    # realpath'd parent dir -> (confirmed toplevel, None) | (None, closed-enum reason).
    # One map, not two: a parallel "why" dict could disagree with the toplevel it explains.
    toplevel_by_dir: dict[str, tuple[str | None, str | None]]
    tracked_by_toplevel: dict[str, frozenset[str] | None]   # None == index UNKNOWN
    gitlinks_by_toplevel: dict[str, frozenset[str]]
    # Roots whose per-root load was SKIPPED because the total budget expired during
    # discovery (Codex F5). Distinct from tracked_by_toplevel[top] is None, which means
    # the load RAN and failed -- conflating them would map exhaustion to git_error.
    # TRAILING DEFAULT: every pre-D4 constructor stays valid unchanged.
    exhausted_roots: frozenset[str] = frozenset()


def _accept_via_gitlink(top: str, accepted: set[str],
                        gitlinks_by_toplevel: dict[str, frozenset[str]]) -> bool:
    """True when `top` is named as a mode-160000 gitlink by an ALREADY-ACCEPTED root
    (8.6 clause 2). Candidate dirs are processed shallowest-first, so a nested submodule
    is reached transitively without recursion ONLY when an instruction file also lives in
    the intervening (outer) submodule -- a toplevel enters `accepted` when one of its own
    dirs is processed, not merely by being an ancestor. When corpus files exist only
    inside the INNER submodule, the outer is never accepted and the inner is refused as
    `outside_root` (a null, not a wrong number -- the safe direction, but not the
    transitive reach this docstring used to promise)."""
    for parent in accepted:
        try:
            rel = str(Path(top).relative_to(parent))
        except ValueError:
            continue
        if rel in gitlinks_by_toplevel.get(parent, frozenset()):
            return True
    return False


def build_git_repo_index(root: Path, files: list[Path], blind_spots: list[str],
                         deadline: float | None = None) -> _GitRepoIndex:
    """THE only git-topology discovery in a run. O(repo roots), not O(files): the live
    114-file corpus clusters into 40 distinct physical parent dirs but only 3 repo roots,
    so discovery is 0 walk subprocesses + 3 rev-parse + 3 git-common-dir + 3 ls-files = 9.

    CONTAINMENT -- ALL THREE fences (design §8.6 + T9 harden round 2, BINDING):
      (1) every candidate cwd passes _resolves_inside_root against a ONCE-computed
          root_stat, the SAME mechanism the hook walk already uses. That INCLUDES
          `root_top`, which was exempt until the T9 harden round found it (an enclosing
          outer repository would otherwise supply every timestamp, undisclosed).
          NOT Path.is_relative_to: that compares a RESOLVED path against an UNRESOLVED
          root and is False for every file in every macOS temp fixture (/var ->
          /private/var), silently nulling the whole corpus (F3, reproduced).
      (2) a submodule toplevel is accepted only when the PARENT index's mode-160000
          gitlink set names it. Filesystem resolution is attacker-influenced; the index is
          not. Without this, an out-of-root symlink would bind `git -C` to a FOREIGN
          repository whose .git/config holds command-valued keys -- core.fsmonitor,
          core.hooksPath, diff.external (S17, P1).
      (3) that toplevel's GIT-COMMON-DIR must resolve inside the root too. Clause 2
          proves a PATH is named as a submodule; it cannot prove the `.git` there belongs
          to that parent, and an attacker who can write in the named subtree swaps it for
          a gitfile pointing at their own repository (demonstrated end to end). Every
          refusal under any clause is published as a blind spot -- a fence that refuses
          silently teaches the operator nothing.
    Fences 1 and 3 are checked BEFORE _load_root for each candidate, because `ls-files`
    is the call that binds to the repository (and, per _GIT_SAFE_CONFIG, the one that
    would execute its command-valued config).

    Alternatives disqualified by measurement, recorded so they are not re-proposed:
      - parsing .gitmodules: N2 -- this repo has 3 gitlinks and only 1 is declared.
      - `git submodule status --recursive`: N3 -- it EXITS NON-ZERO on this very repo
        ("no submodule mapping found in .gitmodules for path
        'skills-archive/business-team'") and reads the same incomplete file.
      - per-file/per-dir `rev-parse --show-toplevel`: correct but +114 (or +40)
        subprocesses; the .git-marker walk is 0.

    S16 note (R5-3): the containment READ-GATE for _deduped_instruction_files lands in
    T6 (C-q), BEFORE this function exists -- by the time paths reach here they have
    already passed it. This function's own containment fences (above) close the
    DOWNSTREAM half of the same class: without them, an out-of-root path would choose a
    subprocess working directory (S17) rather than merely leak a read.

    `deadline` (a time.monotonic() instant, D4/Codex F5) makes DISCOVERY part of the same
    total budget the per-file loop obeys -- a budget that covered only the `git log` calls
    would not be total, since discovery is itself unbounded subprocess work. The scanned
    root's OWN availability probe is the single named exemption (see _GIT_TOTAL_BUDGET):
    exhaustion must never flip `available`, because a budget running out is not evidence
    that the root is not a work tree.
    """
    try:
        root_stat: os.stat_result | None = os.stat(root)
    except OSError:
        root_stat = None

    toplevel_by_dir: dict[str, tuple[str | None, str | None]] = {}
    tracked_by_toplevel: dict[str, frozenset[str] | None] = {}
    gitlinks_by_toplevel: dict[str, frozenset[str]] = {}

    exhausted: set[str] = set()

    def _load_root(top: str) -> None:
        if top in tracked_by_toplevel:
            return
        if deadline is not None and time.monotonic() >= deadline:
            # The batched index read is the expensive half of discovery, so the budget is
            # checked HERE rather than after it. Recorded in `exhausted` (not just as an
            # unknown index) so _git_age_for_file can say "never probed" instead of
            # "probed and failed".
            tracked_by_toplevel[top] = None
            gitlinks_by_toplevel[top] = frozenset()
            exhausted.add(top)
            return
        pair = _git_tracked_and_gitlinks(Path(top))
        if pair is None:
            tracked_by_toplevel[top] = None
            gitlinks_by_toplevel[top] = frozenset()
        else:
            tracked_by_toplevel[top], gitlinks_by_toplevel[top] = pair

    # --- the scanned root itself, first: it is the authority every other root is
    # --- validated against, and it defines `available` (computed exactly ONCE).
    root_real = os.path.realpath(root)
    root_top: str | None
    root_reason: str | None
    if root_stat is None:
        root_top, root_reason = None, "git_error"
    else:
        root_top, root_reason = _git_toplevel(Path(root_real))
        if root_top is not None:
            # `root_top` used to be the ONE candidate cwd exempt from the containment
            # fence this docstring calls BINDING (T9 harden round 2, MEDIUM). When the
            # scanned root is not itself a repo but sits inside an outer one,
            # --show-toplevel returns the ENCLOSING work tree and every file falls
            # through the no-marker branch below to it -- so `ls-files` and every `git
            # log` would run with cwd outside the harness root, silently attributing
            # timestamps from a repository the operator never asked about.
            refusal = _toplevel_refusal(root_top, root, root_stat)
            if refusal is not None:
                blind_spots.append(
                    f"git-age: the scanned root's work tree was not probed — {refusal} "
                    f"(a `git -C` there would bind to a foreign repository)")
                root_top, root_reason = None, "outside_root"
    available = root_top is not None
    accepted: set[str] = set()
    if root_top is not None:
        _load_root(root_top)
        accepted.add(root_top)

    # --- distinct physical parent dirs, SORTED (determinism across PYTHONHASHSEED) and
    # --- shallowest-first, so an outer root is always accepted before an inner one and a
    # --- nested submodule can be validated against its already-accepted parent in one pass.
    dirs = sorted({str(Path(_physical_key(fp)).parent) for fp in files},
                  key=lambda d: (d.count(os.sep), d))
    if root_stat is None and dirs:
        # F9: an unstat-able root is an UNKNOWN, not a determined "resolves outside the
        # root" fact -- _resolves_inside_root was never even called for these dirs. ONE
        # aggregate blind spot for the whole run, not one per dir, since none of them was
        # individually evaluated (the per-dir `outside_root` message below stays exact:
        # that path DOES call _resolves_inside_root and reports a fact it determined).
        blind_spots.append(
            "git-age: the harness root could not be stat'd — no directory was probed "
            "(git topology is unknown, not confirmed outside the root)")
    for dir_key in dirs:
        dir_path = Path(dir_key)
        if root_stat is None:
            toplevel_by_dir[dir_key] = (None, root_reason or "git_error")
            continue
        if not _resolves_inside_root(dir_path, root, root_stat):
            toplevel_by_dir[dir_key] = (None, "outside_root")
            blind_spots.append(
                f"git-age: {dir_key} resolves outside the harness root — not probed "
                f"(a `git -C` there would bind to a foreign repository)")
            continue
        marker, ok = _git_marker_root(dir_path, root_real)
        if not ok:
            toplevel_by_dir[dir_key] = (None, "git_error")   # F5: unreadable != absent
            continue
        if marker is None:
            # No NESTED work tree between this dir and the scanned root, so the file
            # belongs to the scanned root's own -- whose toplevel was resolved above and
            # needs no re-probe. When the root has none, its ALREADY-DISCRIMINATED reason
            # is reused rather than re-asserting `no_repo` over a `git_unavailable` /
            # `timeout` state we never re-examined (C-f/H1).
            if root_top is None:
                toplevel_by_dir[dir_key] = (None, root_reason or "no_repo")
            else:
                toplevel_by_dir[dir_key] = (root_top, None)
            continue
        # R3-3: guard keyed on the CANDIDATE DIR, before _git_toplevel runs -- `top`
        # does not exist yet. The (None, "budget_exhausted") mapping rides the existing
        # `top is None -> why` return in _git_age_for_file; no new branch needed there.
        if deadline is not None and time.monotonic() >= deadline:
            toplevel_by_dir[dir_key] = (None, "budget_exhausted")
            continue
        top, why = _git_toplevel(marker)
        if top is None:
            toplevel_by_dir[dir_key] = (None, why or "no_repo")
            continue
        if top not in accepted and not _accept_via_gitlink(top, accepted,
                                                           gitlinks_by_toplevel):
            # A work tree that neither IS the scanned root nor is named as a gitlink by an
            # already-accepted root. Refuse rather than guess (8.6 clause 2) -- and SAY SO
            # (T9 harden round 2, LOW): this is the exact case the fence exists to catch,
            # and it used to leave the operator a bare null with no trace.
            toplevel_by_dir[dir_key] = (None, "outside_root")
            blind_spots.append(
                f"git-age: {dir_key} was not probed — its work tree ({top}) is neither "
                f"the scanned root nor named as a gitlink by an accepted root")
            continue
        if top not in accepted:
            # T10 audit (MEDIUM): _toplevel_refusal runs a SECOND subprocess of its own
            # (`rev-parse --git-common-dir`, 2s-capped) for every distinct new gitlink
            # toplevel. It sat between two gated checkpoints -- the pre-_git_toplevel guard
            # above and _load_root's own -- so K submodule roots spent up to K x 2s past
            # the deadline invisibly, falsifying the TOTAL budget this docstring promises.
            # Same mapping as that guard: an unspent budget is not a containment refusal.
            if deadline is not None and time.monotonic() >= deadline:
                toplevel_by_dir[dir_key] = (None, "budget_exhausted")
                continue
            # PROVENANCE, not just path (T9 harden round 2, HIGH). The gitlink clause
            # above proved only that an accepted parent's index names this PATH; the
            # `.git` sitting there can still belong to someone else's repository. Checked
            # BEFORE _load_root, because `ls-files` is the call that would bind to it.
            refusal = _toplevel_refusal(top, root, root_stat)
            if refusal is not None:
                toplevel_by_dir[dir_key] = (None, "outside_root")
                blind_spots.append(f"git-age: {dir_key} was not probed — {refusal}")
                continue
        _load_root(top)
        accepted.add(top)
        toplevel_by_dir[dir_key] = (top, None)

    return _GitRepoIndex(available=available, root_reason=None if available else root_reason,
                         toplevel_by_dir=toplevel_by_dir,
                         tracked_by_toplevel=tracked_by_toplevel,
                         gitlinks_by_toplevel=gitlinks_by_toplevel,
                         exhausted_roots=frozenset(exhausted))


def _git_last_commit_ts(top: Path, sub_path: str, timeout: int) -> tuple[int | None, str]:
    """(timestamp, "") or (None, closed-enum reason). NEVER falls back to filesystem mtime
    -- mtime lies after a copy or checkout, and an honest null beats plausible noise.

    Four distinct failures that all used to collapse into a bare None:
      timeout      -- this file's `git log` exceeded the per-call cap
      git_error    -- OSError, or a non-zero exit (verified: a zero-commit repo exits 128)
      no_commits   -- exit 0 with EMPTY stdout: tracked but never committed (staged only).
                      Verified live. Mapping this to `unparseable` ("stdout was not an
                      integer") would emit a MISLEADING reason (§13 F4).
      unparseable  -- exit 0, stdout present but not an integer."""
    proc, err = _git(["log", "-1", "--format=%ct", "--", sub_path], top, timeout)
    if proc is None:
        return None, err or "git_error"
    if proc.returncode != 0:
        return None, "git_error"
    out = _decode_git(proc.stdout).strip()
    if not out:
        return None, "no_commits"
    try:
        return int(out), ""
    except ValueError:
        return None, "unparseable"


def _relative_to_toplevel(real: Path, top: str) -> str | None:
    """`real` expressed relative to the work tree at `top`, or None when it is not inside
    it. ZERO subprocesses -- _git_age_for_file stays pure with respect to the index.

    NOT a bare Path.relative_to (T9 harden round 2, LOW). `os.path.realpath` does NOT
    canonicalize case on APFS (`/Users/cevin/.CLAUDE` comes back unchanged) but git's
    `--show-toplevel` DOES, so with a case-variant --root every single file raised
    ValueError -- the whole git-age signal vanished behind reason `git_error` while
    `git_age_available` still reported True, blaming git for something git got right.

    The fallback walks `real`'s ancestors comparing st_dev/st_ino against `top` via
    os.path.samestat -- the SAME identity mechanism _resolves_inside_root already uses,
    so it also covers directory hard-link and bind-mount aliases, not just case. The
    lexical fast path stays first: when it matches, the prefix is character-identical, so
    it cannot be a false positive, and the common case costs no stat calls at all."""
    try:
        return str(real.relative_to(top))
    except ValueError:
        pass
    try:
        top_stat = os.stat(top)
    except OSError:
        return None
    parts: list[str] = []
    current = real
    while True:
        parts.append(current.name)
        parent = current.parent
        if parent == current:
            return None                  # reached the filesystem root without a match
        try:
            parent_stat = os.stat(parent)
        except OSError:
            return None
        if os.path.samestat(parent_stat, top_stat):
            return str(Path(*reversed(parts)))
        current = parent


def _git_age_for_file(root: Path, fp: Path, index: _GitRepoIndex) -> tuple[int | None, str]:
    """Resolve ONE file to (timestamp, "") or (None, reason). PURE with respect to git
    topology: every repo root comes from `index`.

    ORDERING IS THE WHOLE DECISION and it is settled by measurement. Repo-root-first
    (today) gives: submodule contents -> null, beyond-symlinked-dir -> null, leaf symlink
    -> a WRONG value skewed up to +/-73 days. Realpath-first gives the real content
    timestamp in all three shapes. No amount of reason-field effort can fix a non-null
    wrong number, which is why the ORDERING flips rather than the reporting.

    Reported dict keys are UNCHANGED -- the logical root-relative path stays the key; only
    the QUERIED path is physical. This preserves the schema contract and
    _deduped_instruction_files' deliberate canonical-path preference, which needs NO change:
    under realpath-first the two designs align, because whichever path wins dedup resolves
    to the same physical file.

    THE TRACKED-STATE GATE (D3, Codex #1) sits between the toplevel lookup and the `git
    log`, because `git log` answers from HISTORY, not from tracked state: a file deleted
    in one commit and then recreated UNTRACKED still has a commit, so the unguarded call
    reports a real timestamp for a path git no longer tracks -- a STALE LIE, and a wrong
    number no reason field can ever describe. Index membership converts it to an honest
    null. A path missing from the index but named by (or living under) a mode-160000
    gitlink is `submodule_unavailable`, not `untracked`: the parent's `ls-files` never
    descends into a submodule, so absence there says nothing about the file itself."""
    real = Path(_physical_key(fp))                       # symlink resolved FIRST (N1)
    # F10b: .get with a default, never a bare subscript -- a KeyError waiting on any
    # divergence between the file list the index was built from and this one.
    # Codex F9: the default is git_error, NOT no_repo. A missing index entry means the
    # queried list diverged from the list the index was built from -- an UNKNOWN, not
    # evidence that no repository exists. `no_repo` here would be the same
    # unknown-as-definitive-negative overclaim as S15 (untracked) and D2 (phantom refs):
    # a positive assertion of absence over a state that was never examined.
    top, why = index.toplevel_by_dir.get(str(real.parent), (None, "git_error"))
    if top is None:
        return None, why or "git_error"
    if top in index.exhausted_roots:
        # ORDERING MATTERS: this precedes the unknown-index branch below, because an
        # exhausted root ALSO has tracked_by_toplevel[top] is None -- checking that first
        # would mislabel every budget decision as a git failure (Codex F5).
        return None, "budget_exhausted"    # discovery never ran -- NOT a git failure
    sub = _relative_to_toplevel(real, top)
    if sub is None:
        return None, "git_error"
    tracked = index.tracked_by_toplevel.get(top)
    if tracked is None:
        return None, "git_error"          # unknown index != untracked (S15, P1)
    if sub not in tracked:
        gitlinks = index.gitlinks_by_toplevel.get(top, frozenset())
        if any(sub == g or sub.startswith(g + "/") for g in gitlinks):
            return None, "submodule_unavailable"
        return None, "untracked"
    return _git_last_commit_ts(Path(top), sub, _GIT_SUBPROCESS_TIMEOUT)


def collect_git_age_with_reasons(
    root: Path,
    files: list[Path],
    index: _GitRepoIndex,
    deadline: float | None = None,
) -> tuple[dict[str, int | None], dict[str, str]]:
    """Per-instruction-file git-age SIGNAL (S2.M3; S2-gate hardening R1/N1/#1/#5/#7).
    Never a "stale"/"dead" judgment (binding rule 6).

    APPROVED DEVIATION from the plan (binding rule 3, recorded here because a docstring is
    where the next reader looks): the plan specified widening `collect_git_age` ITSELF to
    return the pair. A later hardening round added two assertions that pin that function's
    dict return by exact equality:
        test_scanned_root_inside_an_outer_repo_is_refused_and_disclosed
        test_case_variant_root_still_reports_real_timestamps
    Binding rule 7 forbids editing an existing assertion -- editing one is a named kill
    signal. So THIS is the widened function and `collect_git_age` remains a thin
    timestamps-only view over it, which preserves the property the plan's "no second pass"
    clause was actually protecting: still exactly one `git log` per file.

    Returns (timestamps, null_reasons). Returning the reason map from the SAME call is what
    keeps this at one `git log` per file; a separate collect_git_age_reasons would double
    the cost. Reasons come from the closed _GIT_NULL_REASONS enum -- never git's own text,
    which carries absolute paths and .gitmodules/.git/config values (binding rule 11). That
    is now ENFORCED here rather than merely tested: both emit sites pass through
    _checked_git_reason (QA exit gate, LOW 5).

    PURE with respect to git topology: everything comes from `index`, built once by
    build_git_repo_index, so staleness.git_age_available can never disagree with the
    timestamps it labels (Codex #5).

    Graceful degradation (never crashes): when the scanned root has no confirmed toplevel,
    EVERY value is None and the per-file loop is skipped entirely. Per-file failures
    (timeout, non-zero exit, untracked, unparseable output) independently degrade to None
    for that path only -- one bad file never poisons the rest.

    `deadline` is a time.monotonic() instant. Files not yet probed when it passes get None +
    "budget_exhausted" and are NOT probed. Exhaustion does NOT flip git_age_available (that
    field means "--root has a confirmed toplevel", a different fact). Iteration is over the
    sorted REPORTED keys (F11: sorted(files) would compare Path parts tuples, diverging for
    prefix-sibling dirs like skills/scan vs skills/scan-code, while schema.md documents
    lexicographic key order), so the skipped set is a deterministic suffix."""
    by_key: dict[str, Path] = {_rel(root, fp): fp for fp in files}
    ordered = sorted(by_key)
    if not index.available:
        # Design §13 F2 as CORRECTED by C-f: the blanket reason is whatever the ROOT probe
        # actually found. `git_unavailable` means the git binary could not be executed;
        # a root that simply is not a work tree (git working fine) is `no_repo`.
        blanket = _checked_git_reason(index.root_reason or "no_repo")
        return ({k: None for k in ordered}, {k: blanket for k in ordered})

    timestamps: dict[str, int | None] = {}
    reasons: dict[str, str] = {}
    for key in ordered:
        if deadline is not None and time.monotonic() >= deadline:
            timestamps[key] = None
            reasons[key] = "budget_exhausted"
            continue
        ts, reason = _git_age_for_file(root, by_key[key], index)
        timestamps[key] = ts
        if ts is None:
            reasons[key] = _checked_git_reason(reason or "git_error")
    return timestamps, reasons


def collect_git_age(root: Path, files: list[Path], index: _GitRepoIndex,
                    deadline: float | None = None) -> dict[str, int | None]:
    """Timestamps-only VIEW over collect_git_age_with_reasons.

    NOT a second pass: it discards the reason map produced by the SAME per-file `git log`,
    so the cost is identical. build_document, which publishes staleness_null_reasons, calls
    the two-map form directly.

    WHY IT EXISTS, honestly (T10 audit, LOW): it has ZERO production callers. Its only two
    call sites are the assertions that compare its return to a dict by exact equality --
    test_scanned_root_inside_an_outer_repo_is_refused_and_disclosed and
    test_case_variant_root_still_reports_real_timestamps -- and binding rule 7 forbids
    editing an existing assertion, so this signature must survive. NAMED, not alluded to:
    without the names a future reader deleting this view has no way to find what breaks.
    If rule 7 is ever lifted for those two tests, this function goes with them."""
    return collect_git_age_with_reasons(root, files, index, deadline)[0]


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


# T4/M4: project-tier surfaces fed into the CROSS-TIER duplication corpus — mirrors
# _DUP_GLOBS's operator-tier surface set as closely as the project `.claude/` layout
# allows (project skills' SKILL.md bodies are gathered separately, below, since they
# live one level deeper than a flat glob reaches).
_PROJECT_DUP_SURFACE_DIRS = ((".claude/rules", "*.md"), (".claude/agents", "*.md"),
                             (".claude/commands", "*.md"))


def _project_tier_duplication_corpus(project_root, blind_spots, out_of_root_refs):
    """Project-tier half of the M4 cross-tier duplication corpus (T4): `.claude/rules`,
    `.claude/agents`, `.claude/commands` bodies plus each project skill's SKILL.md.
    EVERY read routes through T3's `_project_tier_gate` + `_read_project_file` (H2) — an
    escaping symlink is recorded as an `out_of_root_ref` and excluded, never body-read or
    excerpted. Feeds `duplication.pairs[].shared_sample`, one of T3's three named
    excerpt sinks."""
    project_root = Path(project_root)
    harness_root = project_root / ".claude"
    seen_refs: set[str] = set()
    seen_physical: set[Any] = set()
    corpus: list[tuple[str, str, set[str]]] = []  # [(rel_path, "project", shingle_set), ...]
    try:
        containment_stat = os.stat(project_root)
    except OSError:
        return corpus

    candidates = []
    for rel_dir, pattern in _PROJECT_DUP_SURFACE_DIRS:
        d = project_root / rel_dir
        try:
            if d.is_dir():
                candidates.extend(sorted(d.glob(pattern)))
        except OSError:
            continue
    skills_dir = harness_root / "skills"
    try:
        if skills_dir.is_dir():
            for skill_dir in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
                skill_md = skill_dir / "SKILL.md"
                present, ok = _safe_exists(skill_md)
                if ok and present:
                    candidates.append(skill_md)
    except OSError:
        pass

    for fp in candidates:
        key = _physical_key(fp)
        if key in seen_physical:
            continue
        seen_physical.add(key)
        contained, _identity = _project_tier_gate(fp, project_root, containment_stat)
        if not contained:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, fp)
            continue
        try:
            size = fp.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_BYTES:
            blind_spots.append(
                f"{_rel(project_root, fp)} exceeds {MAX_FILE_BYTES} bytes; skipped in duplication scan.")
            continue
        text, _evidence = _read_project_file(fp, project_root, containment_stat)
        if text is None:
            continue
        words = _normalize_words(text)
        shingles = _ordered_capped_shingles(words)
        if not shingles:
            blind_spots.append(
                f"{_rel(project_root, fp)} has fewer than {SHINGLE_K} normalized words; "
                "skipped in duplication scan.")
            continue
        corpus.append((_rel(project_root, fp), "project", shingles))
    return corpus


def scan_duplication(
    root: Path,
    blind_spots: list[str],
    project_root: Path | None = None,
    compose: bool = False,
    out_of_root_refs: list[Any] | None = None,
) -> dict[str, Any]:
    """Candidate near-duplicate pairs by containment coefficient (|A∩B| / min(|A|,|B|))
    over k=8 word shingles — chosen over Jaccard because it correctly flags a short file
    fully subsumed by a longer one (schema.md Note 2). SIGNALS only: this is a candidate
    list. Deciding "one declared home + callers" for a pair is a synthesis-pass JUDGMENT,
    not something this collector decides. `compose=True` (T4/M4) adds the project-tier
    corpus so duplication runs ACROSS BOTH TIERS COMBINED — an operator rule duplicated
    by a project file is a signal ("this repo re-implements an operator rule"). Additive:
    `compose=False` behavior (corpus, pairs shape, output) is byte-for-byte unchanged."""
    # Generalized skills/coding-team/rules -> skills/*/rules for release portability; the
    # seen_physical dedup below still collapses a rule reachable via multiple glob paths.
    seen_physical = set()
    corpus = []  # [(rel_path, tier, shingle_set), ...]
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
            corpus.append((_rel(root, fp), "operator", shingles))

    if compose and project_root is not None:
        corpus.extend(_project_tier_duplication_corpus(
            project_root, blind_spots, out_of_root_refs if out_of_root_refs is not None else []))

    pairs = []
    for i in range(len(corpus)):
        path_a, tier_a, set_a = corpus[i]
        for j in range(i + 1, len(corpus)):
            path_b, tier_b, set_b = corpus[j]
            score = _containment(set_a, set_b)
            if score < DUP_THRESHOLD:
                continue
            shared = set_a & set_b
            sample = min(shared) if shared else ""
            (a, tier_of_a), (b, tier_of_b) = sorted(((path_a, tier_a), (path_b, tier_b)))
            pair = {"a": a, "b": b, "score": score, "shared_sample": sample,
                    "evidence": "INFERRED"}
            if compose:
                pair["a_tier"] = tier_of_a
                pair["b_tier"] = tier_of_b
            pairs.append(pair)

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


def check_phantom_refs(
    root: Path,
    corpus_files: list[tuple[str, str]],
    inaccessible: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Backtick-quoted path and env-flag tokens that don't resolve to anything real. A
    path OUTSIDE --root is reported as kind="external" (INFERRED, resolved: null) — the
    collector never claims a file outside its scanned scope is phantom; it genuinely
    cannot see it either way, so it only classifies, never asserts absence."""
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
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
                    if _SLASH_COMMAND_RE.fullmatch(norm):
                        segments = norm[1:].split(":")
                        # Homes under --root, in check order. A BARE /foo has
                        # segments[:-1] == [] and yields EXACTLY today's two homes, so
                        # nothing changes for it. A namespaced /paul:apply adds the
                        # commands/<ns>/<name>.md home (verified live for commands/paul/,
                        # commands/base/, commands/aegis/ and the nested
                        # commands/base/orientation/tasks/deep-why.md).
                        homes = [root.joinpath("commands", *segments[:-1], f"{segments[-1]}.md"),
                                 root / "skills" / segments[0] / "SKILL.md"]
                        exists = False
                        blocked = False
                        for home in homes:
                            present, ok = _safe_exists(home)
                            if not ok:
                                # QA exit gate (MEDIUM 3): the `blocked` branch below
                                # `continue`s BEFORE `seen` is consulted, so the same token
                                # mentioned N times probes -- and reported -- the same home
                                # N times. _append_inaccessible_once is T6's answer to
                                # exactly that, and the badge counts these rows.
                                _append_inaccessible_once(inaccessible, _rel_safe(root, home))
                                blocked = True
                                break
                            if present:
                                exists = True
                                break
                        if exists or blocked:
                            continue
                        key = (rel_path, norm, "slash_command")
                        if key not in seen:
                            seen.add(key)
                            # R2/F1: INFERRED + resolved null, NOT VERIFIED + false. A
                            # /token's real resolution space is at least six homes:
                            # commands/<name>.md, commands/<ns>/<name>.md,
                            # skills/<name>/SKILL.md, a Claude Code BUILT-IN, a plugin
                            # command, and a project-tier command. The last three are
                            # structurally unenumerable from --root. Asserting absence
                            # over a space we cannot see is a VERDICT wearing a signal's
                            # clothes (binding rule 6). This matches the doctrine this
                            # same function already applies to `external` in its own
                            # docstring. The claim becomes "no home under the scanned
                            # root" (true and verifiable) instead of "this command no
                            # longer exists" (false for /simplify).
                            refs.append({"source": rel_path, "ref": norm, "kind": "slash_command",
                                         "resolved": None, "evidence": "INFERRED"})
                        continue
                    key = (rel_path, norm, "external")
                    if key not in seen:
                        seen.add(key)
                        refs.append({"source": rel_path, "ref": norm, "kind": "external",
                                     "resolved": None, "evidence": "INFERRED"})
                    continue
                src_dir = Path(rel_path).parent
                candidates = [root / norm]
                if str(src_dir) not in (".", ""):
                    candidates.append(root / src_dir / norm)
                handled = False
                for candidate in candidates:
                    present, ok = _safe_exists(candidate)
                    if not ok:
                        # Same shape, same fix as the slash-command probe above: `handled`
                        # short-circuits past the `seen` guard, so a repeated ref would
                        # re-report one unreadable candidate once per mention.
                        _append_inaccessible_once(inaccessible, _rel_safe(root, candidate))
                        handled = True
                        break
                    if present:
                        handled = True
                        break
                if handled:
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


def collect_promotion_candidates(
    root: Path,
    corpus_files: list[tuple[str, str]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Prose in an instruction file that reads like a hard rule (NEVER/ALWAYS/must, a
    numeric cap, a required-file assertion) but may have no corresponding hook enforcing
    it. Advisory SIGNALS only — synthesis proposes extending an EXISTING covered hook
    before creating a new one; this collector never makes that judgment itself."""
    candidates: list[dict[str, Any]] = []
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


def detect_test_coverage(
    root: Path, on_demand: dict[str, Any], errors: list[str]
) -> dict[str, Any]:
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


def build_headline(
    always_loaded: dict[str, Any],
    hooks_section: dict[str, Any],
    instruction_length_flags: list[dict[str, Any]],
    duplication_section: dict[str, Any],
) -> dict[str, Any]:
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
    target is NOT chased INTO. This matches the collector's own reads -- pathlib rglob does not
    follow nested directory symlinks either -- so the walked set stays a SUPERSET of what
    build_document reads while avoiding heavy I/O (and any symlink cycle) on nested external
    trees. Well-known generated subtrees (_PRUNED_WALK_DIRS) are pruned before descending.

    Directory symlinks (Codex r5 FIX 1): os.walk(followlinks=False) LISTS a nested directory
    symlink in `dirnames` but never revisits it as its own `dirpath`, so it would never be
    yielded — yet _skill_has_test_asset FOLLOWS such a link (its `(skill_dir/"tests").is_dir()`
    and `d.glob(...)` both chase the target), making has_test depend on the link TARGET. So
    each retained directory symlink is yielded HERE by its own path (membership-watchable) but
    NOT traversed into, mirroring _skill_has_test_asset's is_dir()/glob follow. Only the
    symlinked children are yielded from `dirnames` — a plain subdir is already yielded when
    os.walk descends into it as `dirpath`, so this avoids a double-yield."""
    try:
        if not base.is_dir():
            return
    except OSError:
        return
    for dirpath, dirnames, _ in os.walk(base, followlinks=False):
        # Prune generated/non-input subtrees IN PLACE so os.walk never descends into them.
        dirnames[:] = [d for d in dirnames if d not in _PRUNED_WALK_DIRS]
        yield Path(dirpath)
        # Yield retained directory symlinks WITHOUT following them: os.walk(followlinks=False)
        # never revisits them as `dirpath`, but the collector's has_test check reads through
        # them, so the watcher must snapshot the link path for membership (keeps the superset).
        for name in dirnames:
            child = Path(dirpath) / name
            try:
                if child.is_symlink() and child.is_dir():
                    yield child
            except OSError:
                continue


# T8: project-tier explicit membership-watched surface dirs (compose mode only) --
# `<repo>/.claude/{rules,agents,commands,skills}` mirror `_PROJECT_DUP_SURFACE_DIRS` +
# `_walk_project_tier_nodes`'s surfaces; `.claude/hooks` is included too even though no
# collector read currently descends into it -- the Three Roots doc names it as part of
# the project-harness-root, and watching an absent/unread dir is a harmless superset
# (its future creation is still observable, matching the operator-tier `for d in (...)`
# unconditional-membership block below).
_PROJECT_HARNESS_SURFACE_DIRS = ("rules", "agents", "commands", "skills", "hooks")


def _compose_project_input_paths(project_root):
    """Compose-mode project-tier watch surface (T8): a SUPERSET of every project-tier read
    `_walk_project_tier` (T2 -- repo-root/nested CLAUDE.md + CLAUDE.local.md via the
    containment-root walk), `_walk_project_tier_nodes`/`_project_tier_duplication_corpus`
    (T4/M4 -- `.claude/{rules,agents,commands,skills}`), and `_compose_hooks` (T5 -- a
    project/local settings.json hook command's resolved script existence) add to the
    collector output. Returns a `set` of ABSOLUTE `Path`s, every one lexically under
    `project_root` -- serve.py's watcher relies on that lexical-containment invariant to
    tier-tag the watched set (T8's `(path, tier)` contract) without a second stat pass."""
    project_root = Path(project_root)
    paths: set[Path] = set()
    try:
        containment_stat = os.stat(project_root)
    except OSError:
        return paths

    # -- repo-root + every CONTAINED nested dir, membership-watched, PLUS each dir's
    #    CLAUDE.md/CLAUDE.local.md (content) -- the SAME `_walk_contained_dirs` walk
    #    `_walk_project_tier` uses (H2 containment gate included), so this naturally
    #    covers `.claude/`, `.claude/rules/`, `.claude/skills/`, `.claude/skills/<name>/`,
    #    `.claude/agents/`, `.claude/commands/` as membership-watched dirs for free -- an
    #    escaping symlinked dir is not yielded (mirrors: the collector never descends into
    #    it either). Scratch out_of_root_refs/seen are discarded -- the watcher needs only
    #    the path set, not the bookkeeping build_document records for the real doc. --
    scratch_refs: list[Any] = []
    scratch_seen: set[str] = set()
    for d in _walk_contained_dirs(project_root, project_root, containment_stat,
                                   scratch_refs, scratch_seen):
        paths.add(d)
        for fname in ("CLAUDE.md", "CLAUDE.local.md"):
            f = d / fname
            present, ok = _safe_exists(f)
            if ok and present:
                paths.add(f)

    # -- explicit project-harness-root surface dirs (membership), unconditional (even if
    #    absent today) -- mirrors the operator-tier `for d in (...)` block above. --
    harness_root = project_root / ".claude"
    paths.add(harness_root)
    for d in _PROJECT_HARNESS_SURFACE_DIRS:
        paths.add(harness_root / d)

    # -- content globs: the SAME dir+pattern tuples `_project_tier_duplication_corpus` (M4)
    #    consumes for `.claude/{rules,agents,commands}`, plus each project skill's SKILL.md
    #    (the T4 node model + M4 corpus both read exactly this file per skill dir). --
    for rel_dir, pattern in _PROJECT_DUP_SURFACE_DIRS:
        try:
            paths.update((project_root / rel_dir).glob(pattern))
        except OSError:
            pass
    try:
        skill_dirs = sorted(p for p in (harness_root / "skills").iterdir() if p.is_dir())
    except OSError:
        skill_dirs = []
    for skill_dir in skill_dirs:
        paths.add(skill_dir / "SKILL.md")

    # -- T5: a project/local settings.json hook command's resolved script existence
    #    (`_compose_hooks`'s `.exists()` check) -- mirrors the operator-tier resolved-hook-
    #    script loop below EXACTLY (same lexical-containment logic), reading project/local
    #    settings via the SAME `parse_project_settings` T5's own merge uses. --
    proj_settings, _ok1 = parse_project_settings(project_root, project_root, containment_stat,
                                                  [], [], [])
    local_settings, _ok2 = parse_project_settings(project_root, project_root, containment_stat,
                                                   [], [], [], filename="settings.local.json")
    project_root_resolved = project_root.resolve()
    for settings in (proj_settings, local_settings):
        for command in _iter_hook_commands(settings):
            script_path, _note = _script_from_command(command, project_root)
            if script_path is None:
                continue
            try:
                lexical = script_path.parent.resolve() / script_path.name
                lexical.relative_to(project_root_resolved)
            except (ValueError, OSError):
                continue  # genuinely outside the project root -- un-watchable via this walk
            paths.add(script_path)

    return paths


def iter_input_paths(
    root: Path, project_root: Path | None = None, compose: bool = False
) -> list[Path]:
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
        hooks/); the instruction-file EDIT that introduces the ref itself IS watched.
      * collect_git_age reads `git log` history for each instruction file — git history is
        not a watchable filesystem input; `.git` sits in _PRUNED_WALK_DIRS, so this walk
        structurally cannot see commits. A new commit to an already-unchanged instruction
        file changes its git-age (staleness.last_commit_ts) with NO watched filesystem
        signal of its own — but the instruction-file content EDIT that precedes the commit
        IS watched, so a re-render still fires on the edit itself.

    `compose` (T8, default False): when True AND `project_root` is given, ALSO yields the
    project-tier reads T2/T3/T4/T5 added under the project-containment-root/project-harness-
    root in compose mode (`_compose_project_input_paths`) — nested CLAUDE.md/CLAUDE.local.md,
    `.claude/{rules,agents,commands,skills,hooks}`, and project/local hook-script existence.
    Gated on `compose`, NOT merely on `project_root` (which already has a legacy non-compose
    meaning above — the active operator-project's CLAUDE.md), so a non-compose caller's
    watched/read surface for the SAME `project_root` argument stays byte-identical."""
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

    # -- T5 settings/hooks/MCP full-chain inputs (compose mode). `~/.claude.json` is the
    #    User+Local MCP source (collect_composed_mcp) — it sits OUTSIDE every dir-root, so
    #    it is yielded here PRECISELY so validate_write_target's `input_paths=` clause can
    #    reject a `--out ~/.claude.json` that containment alone would wrongly permit.
    #    Project settings.json/settings.local.json/.mcp.json are yielded unconditionally
    #    when project_root is given (harmless superset, matches the CLAUDE.md pattern
    #    above) even though they may not exist on disk. --
    paths.add(Path.home() / ".claude.json")
    if project_root is not None:
        project_root_p = Path(project_root)
        paths.add(project_root_p / ".claude" / "settings.json")
        paths.add(project_root_p / ".claude" / "settings.local.json")
        paths.add(project_root_p / ".mcp.json")

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
        # Root-containment is decided LEXICALLY (Codex r5 FIX 2): resolve only the DIRECTORY
        # chain (so root-matching is correct through deploy-symlinked parents) and KEEP the
        # leaf name unresolved, so a leaf like ./scripts/x.py that LIVES under root but is a
        # symlink to an external target still counts as in-root. reconcile_hooks stat()s the
        # SAME lexical path (script_path.stat() follows the leaf symlink to that target), so the
        # watched path added below EQUALS what reconcile_hooks reads. A truly-external absolute
        # path still fails relative_to and stays un-watchable (documented blind spot, case c).
        try:
            lexical = script_path.parent.resolve() / script_path.name
            lexical.relative_to(root_resolved)
        except (ValueError, OSError):
            continue  # genuinely outside root (case c) — un-watchable via a root walk
        paths.add(script_path)

    # -- T8: compose-mode project-tier additions (gated on `compose`, not merely on
    #    `project_root`'s presence -- see the docstring note above). --
    if compose and project_root is not None:
        paths.update(_compose_project_input_paths(project_root))

    return sorted(paths, key=str)


def build_document(
    root: Path, project_root: Path | None, compose: bool = False
) -> dict[str, Any]:
    root = Path(root).resolve()
    inaccessible: list[dict[str, Any]] = []
    errors: list[str] = []
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

    out_of_root_refs: list[dict[str, Any]] = []
    # T5 P31/C18 (composed weight honesty): snapshot BEFORE walk_always_loaded so the
    # deltas below capture EXACTLY the out-of-root/inaccessible entries that would have
    # contributed to always_loaded weight — nothing from later scans (duplication, node
    # model, MCP) leaks into this count. Unused in non-compose mode.
    _weight_out_of_root_before = len(out_of_root_refs)
    _weight_inaccessible_before = len(inaccessible)
    files, conditional_variants = walk_always_loaded(root, project_root, inaccessible, errors,
                                                      compose=compose,
                                                      out_of_root_refs=out_of_root_refs)
    weight_excluded_count = ((len(out_of_root_refs) - _weight_out_of_root_before)
                              + (len(inaccessible) - _weight_inaccessible_before))
    skill_descriptions, agent_descriptions = collect_descriptions(root, inaccessible)
    skills, skill_internal_bodies, memory_bodies = collect_on_demand(root, project_root, inaccessible)

    settings, settings_parsed_ok = parse_settings(root, errors, blind_spots)
    hooks_section = reconcile_hooks(root, settings, inaccessible, blind_spots)
    permissions_section = collect_permissions(settings, settings_parsed_ok)
    config_section = collect_config(root, settings, settings_parsed_ok, blind_spots)
    instruction_length_flags = flag_long_instructions(root, inaccessible, blind_spots)
    duplication_section = scan_duplication(root, blind_spots, project_root=project_root,
                                            compose=compose, out_of_root_refs=out_of_root_refs)
    corpus_files = _staleness_corpus(root, inaccessible)
    phantom_refs = check_phantom_refs(root, corpus_files, inaccessible)
    promotion_candidates = collect_promotion_candidates(root, corpus_files, settings)
    # S2 gate fix: git-age SIGNAL only (never a "stale" verdict). Topology is discovered
    # ONCE and collect_git_age is pure with respect to it, so git_age_available can never
    # disagree with the timestamps it labels (Codex #5). `available` replaces the deleted
    # _git_work_tree_available and is NARROWER: it means "--root has a confirmed toplevel"
    # (rev-parse --show-toplevel), where the old probe accepted anything "inside a work
    # tree" -- verified that a BARE repo passed the old probe and fails this one.
    # ONE _deduped_instruction_files call here feeds BOTH the index build and the age
    # collection (it previously produced only rel-paths): it appends to `inaccessible`/
    # `blind_spots`, so a second call would re-walk the corpus at double the I/O for an
    # identical result. Sorting moved into collect_git_age_with_reasons, which keys the
    # same lexicographic order (F11).
    instruction_files = _deduped_instruction_files(root, inaccessible, blind_spots)
    # D4/Codex F5: the deadline is computed BEFORE discovery and threaded into BOTH, so
    # the cap is TOTAL for the subsystem rather than per-call.
    git_deadline = time.monotonic() + _GIT_TOTAL_BUDGET
    git_index = build_git_repo_index(root, instruction_files, blind_spots,
                                     deadline=git_deadline)
    git_age, git_age_null_reasons = collect_git_age_with_reasons(
        root, instruction_files, git_index, deadline=git_deadline)
    staleness_section = {
        "git_age_available": git_index.available,
        "last_commit_ts": git_age,
    }
    exhausted_count = sum(1 for r in git_age_null_reasons.values()
                          if r == "budget_exhausted")
    if exhausted_count:
        # D4: exhaustion must be DISCLOSED with a count, never silently truncated.
        blind_spots.append(
            f"git-age: the {_GIT_TOTAL_BUDGET:.0f}s total budget was exhausted before "
            f"{exhausted_count} instruction file(s) were probed — their last_commit_ts is "
            f"null with reason budget_exhausted, which is NOT a measurement. Re-run, or "
            f"raise the budget.")

    totals = {
        "words": sum(f["words"] for f in files),
        "tokens_est": sum(f["tokens_est"] for f in files),
        "file_count": len(files),
    }
    if compose:
        # P31/C18: weight is a sum over READABLE entries only (files[] above already
        # excludes out-of-root + inaccessible project entries) — this makes the exclusion
        # EXPLICIT so a dropped file reads as "N excluded," never as a silently-smaller
        # total. Additive/compose-only; absent in a non-compose doc distinguishes
        # "0 excluded (measured)" from "not measured at all."
        totals["excluded_count"] = weight_excluded_count

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
        "staleness": staleness_section,
        # D5 (S2 gate fix, ADDITIVE, no schema_version bump per 8.2). A SIBLING, not
        # nested: an existing test asserts doc["staleness"] by EXACT DICT EQUALITY with an
        # INLINE literal, so any nested key would force editing an existing assertion
        # (binding rule 7). Sibling costs only naming cohesion.
        # TOTAL INVARIANT: an entry for exactly those last_commit_ts keys whose value is
        # null -- never for a key with a timestamp, never for a key absent from
        # last_commit_ts.
        "staleness_null_reasons": git_age_null_reasons,
        "inaccessible": inaccessible,
        "blind_spots": blind_spots,
        "errors": errors,
    }
    if compose:
        # T11 (disclose-and-defer, operator-approved 2026-07-22): the per-file hygiene
        # analyses above (flag_long_instructions, _staleness_corpus, check_phantom_refs,
        # collect_promotion_candidates, detect_test_coverage, _hooks_body_corpus) take no
        # project_root and run OPERATOR-TIER-ONLY, even in --compose — a genuinely
        # oversized/stale/phantom/promotion-candidate/untested PROJECT-tier file is never
        # flagged by them. Full per-tier hygiene is deferred to v1.1 (see the plan); this
        # discloses the current limitation so "0 project flags" is never misread as
        # "project clean" ([[no-known-broken]]: a silent gap is a trap).
        blind_spots.append(
            "Compose mode: instruction_length_flags, staleness, phantom_refs, "
            "promotion_candidates, test_coverage, and the hooks-body duplication corpus "
            "(flag_long_instructions, _staleness_corpus, check_phantom_refs, "
            "collect_promotion_candidates, detect_test_coverage, _hooks_body_corpus) scan "
            "the OPERATOR tier only — project-tier files are NOT covered by these "
            "analyses in v1.")
        # R2-B: name BOTH roots walked (today's `doc["root"]` is operator-only) — additive,
        # so a non-compose run's schema is byte-identical to before this field existed.
        project_containment_root = Path(project_root).expanduser().resolve() if project_root else None
        doc["inspected_roots"] = {
            "operator": str(root),
            "project_containment": str(project_containment_root) if project_containment_root else None,
            "project_harness": (str(project_containment_root / ".claude")
                                 if project_containment_root else None),
        }
        # T3/H2: additive-in-compose-only, same pattern as inspected_roots above — a
        # non-compose run's schema stays byte-identical to before this field existed.
        doc["out_of_root_refs"] = out_of_root_refs
        # T5: settings/hooks/MCP full-chain merge (Local > Project > User) — additive,
        # same compose-only pattern as inspected_roots/out_of_root_refs above. `settings`
        # (User tier) was already parsed via `parse_settings` above for the
        # operator-only sections; Project/Local reuse T3's `parse_project_settings`. This
        # block moved AHEAD of T4 (P2-A) so `composed_hooks` exists before `raw_nodes` is
        # assembled — T4's 'hook' surface (below) is derived from this SAME merged list,
        # never a second independent hooks read.
        project_settings, local_settings = {}, {}
        project_ok, local_ok = False, False
        if project_containment_root is not None:
            try:
                proj_containment_stat = os.stat(project_containment_root)
            except OSError:
                proj_containment_stat = None
            if proj_containment_stat is not None:
                project_settings, project_ok = parse_project_settings(
                    project_containment_root, project_containment_root, proj_containment_stat,
                    errors, blind_spots, out_of_root_refs)
                local_settings, local_ok = parse_project_settings(
                    project_containment_root, project_containment_root, proj_containment_stat,
                    errors, blind_spots, out_of_root_refs, filename="settings.local.json")
        # Precedence order Local > Project > User — the shared stack every T5 merge
        # function below consumes (permissions union is order-independent; overrides/MCP
        # take the FIRST tier in this list that defines a given key/name as the winner).
        settings_stack = [("local", local_settings), ("project", project_settings), ("user", settings)]
        # P3: a SEPARATE stack carrying each tier's real `parsed_ok` flag (from
        # `parse_settings`/`parse_project_settings`) alongside the dict — `settings_stack`
        # above stays dict-only/unchanged for `_settings_overrides` (whose `key in s`
        # truthiness filter is functionally equivalent for an empty dict either way);
        # `_merge_permissions_union_deny_wins` needs the real flag, since `bool({})` would
        # wrongly read a present-but-legitimately-empty settings.json as "did not parse".
        settings_stack_with_ok = [("local", local_settings, local_ok),
                                   ("project", project_settings, project_ok),
                                   ("user", settings, settings_parsed_ok)]

        project_settings_source = (str(project_containment_root / ".claude" / "settings.json")
                                    if project_containment_root else None)
        local_settings_source = (str(project_containment_root / ".claude" / "settings.local.json")
                                  if project_containment_root else None)
        composed_hooks = _compose_hooks(
            [("user", settings, str(root / "settings.json"), root),
             ("project", project_settings, project_settings_source, project_containment_root),
             ("local", local_settings, local_settings_source, project_containment_root)],
            project_containment_root, out_of_root_refs)

        doc["composed_settings"] = {
            "permissions": _merge_permissions_union_deny_wins(settings_stack_with_ok),
            "hooks": composed_hooks,
            "overrides": _settings_overrides(settings_stack),
            "mcp": collect_composed_mcp(project_containment_root, errors, blind_spots, out_of_root_refs),
        }

        # T4: canonical tier-tagged node model + per-surface shadow resolver — additive,
        # same compose-only pattern as inspected_roots/out_of_root_refs above. 'claude_md'
        # and 'hook' (P2-A) reuse `files[]`/`composed_hooks` (both already
        # built/tier-tagged above) rather than re-walking or re-reading disk.
        raw_nodes = _walk_operator_tier_nodes(root)
        if project_root is not None:
            raw_nodes += _walk_project_tier_nodes(project_root, out_of_root_refs)
        raw_nodes += _rule_nodes_from_files(files)
        raw_nodes += _claude_md_nodes_from_files(files)
        raw_nodes += _hook_nodes_from_composed(composed_hooks)
        resolved_nodes, surfaces_summary, participating_surfaces = _resolve_tier_composition(raw_nodes)
        doc["tier_composition"] = {
            "nodes": resolved_nodes,
            "surfaces": surfaces_summary,
            "participating_surfaces": participating_surfaces,
        }

        # T11 (Bug 3, T9-found): `out_of_root_refs` is populated by several independent
        # recording sites above (_walk_project_tier, _project_tier_duplication_corpus,
        # _walk_project_tier_nodes, parse_project_settings x2, _compose_hooks,
        # collect_composed_mcp), each deduping ONLY against its own call-local `seen` set
        # inside `_record_out_of_root_ref` -- so the SAME escaping path is recorded once per
        # recording site that independently encounters it (e.g. a project rule symlink is
        # walked by both the always-loaded rules scan and the duplication-corpus scan).
        # Dedupe ONCE here by `name` -- the same identity `_record_out_of_root_ref` already
        # dedupes on intra-call -- first occurrence kept, stable order preserved.
        seen_ref_names = set()
        deduped_refs = []
        for ref in out_of_root_refs:
            if ref["name"] in seen_ref_names:
                continue
            seen_ref_names.add(ref["name"])
            deduped_refs.append(ref)
        doc["out_of_root_refs"] = deduped_refs
    return doc


def _empty_document(root: Path) -> dict[str, Any]:
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
        "staleness": {"git_age_available": False, "last_commit_ts": {}},
        "staleness_null_reasons": {},
        "inaccessible": [], "blind_spots": [], "errors": [],
    }


def _default_operator_root():
    """Operator-scan-root auto-resolution (P1-1, M6): `$CLAUDE_CONFIG_DIR` if set, else
    `$HOME/.claude` — NEVER hard-coded. Used only as the `--root` argparse default; an
    explicit `--root` always wins. When `CLAUDE_CONFIG_DIR` is unset (the common case),
    this resolves identically to the prior hard-coded default."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else (Path.home() / ".claude")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Read-only harness map collector.")
    ap.add_argument("--root", default=str(_default_operator_root()))
    ap.add_argument("--project-root", default=os.getcwd())
    ap.add_argument("--compose", action="store_true",
                     help="Compose operator ⊕ project tiers: three-root walk, every node "
                          "additive-tagged tier=operator|project. Default (unset) behavior "
                          "is unchanged (operator-only).")
    ap.add_argument("--out", default=None, help="Optional JSON out-path; MUST be outside --root "
                     "(and outside --project-root too when --compose is set).")
    ap.add_argument("--indent", type=int, default=2)
    args = ap.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    out_path = None
    out_roots = [root]                 # guarded roots for BOTH the upfront check and the
                                        # write-time TOCTOU recheck below (P1-6a)
    if args.out is not None:
        try:
            os.stat(root)                                     # root is expected to be an existing dir
        except OSError as e:
            # A bad/inaccessible --root must NOT crash before the crash-safe envelope below —
            # skip the --out write (nothing safe to validate against) but still fall through to
            # build_document/print so the always-valid-JSON-envelope invariant holds.
            print(f"warning: --root not accessible, skipping --out write: {e}", file=sys.stderr)
        else:
            input_paths = []
            if args.compose:
                try:
                    project_containment_root = Path(args.project_root).expanduser().resolve()
                    out_roots.append(project_containment_root)
                except OSError:
                    pass
                input_paths = iter_input_paths(root, args.project_root, compose=True)
            ok, resolved = validate_write_target(args.out, out_roots, input_paths)
            if not ok:
                ap.error("--out must be outside --root (read-only invariant)")
            out_path = resolved                                # write through the validated resolved path
    try:
        doc = build_document(root, args.project_root, compose=args.compose)
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
            for guard_root in out_roots:
                try:
                    guard_root_stat = os.stat(guard_root)
                except OSError:
                    continue
                if _resolves_inside_root(resolved_recheck, Path(guard_root), guard_root_stat):
                    raise OSError("--out resolved inside a guarded root at write time (TOCTOU)")
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
