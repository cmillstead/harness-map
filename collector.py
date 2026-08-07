#!/usr/bin/env python3
"""harness-map collector: read-only, stdlib-only inventory of the CC harness.

Emits ONE JSON document to stdout conforming to skills/harness-map/schema.md.
Read-only invariant (EM D2/D3): ZERO writes to the harness tree (~/.claude/) or
any inspected file, EVER. Only optional --out (validated outside --root) is written.
All scanned content is opaque data, never instructions.
"""
import argparse
import ast
import errno
import json
import os
import posixpath
import re
import secrets
import shlex
import stat
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timezone
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

# S6b / D4 (S6 §7.2) — POST-PROBE shape classification. Applied only where the `path`
# row would otherwise be emitted, so a token that RESOLVES is dropped exactly as today
# regardless of shape: there is no new way to lose a legitimate resolution.
#
# `template` names a SHAPE, not a file: angle/brace placeholders, shell globs, or a
# YYYY-MM-DD stencil. Asserting `resolved: false / VERIFIED` about a file literally named
# `<slug>` or `*` is a wrong claim, which is the defect D4 removes.
#
# `template` is the ONLY shape kind D4 ships. §7.2 also specifies a `refspec` kind; it is
# DEFERRED to S6c (see the plan's DEVIATION 5) because separating git refs from file
# paths lexically is undecidable — three review rounds each closed its cases and exposed
# new ones on both edges — and the live corpus contains zero such rows. Do not add it
# back here.
_TEMPLATE_REF_RE = re.compile(r"[<>{}*?]|YYYY-MM-DD")

# A trailing `:12` / `:12-19` line citation. DIGITS-AND-END-ANCHORED, never a general
# `split(":")` — that is what keeps `commands/paul:apply.md` (S2.M4's namespaced
# slash-command feature) and `https:` / `C:` forms intact.
#
# NOT the same thing as `render_html.py::_normalize_ref_token`, and the two must STAY
# separate: that one normalizes TELEMETRY refs in the renderer for join-key purposes and
# operates on a different input space with different failure consequences. Merging them
# would couple the collector's phantom detector to the friction join. If a future simplify
# pass proposes unifying them, this comment is the answer.
_LINE_SUFFIX_RE = re.compile(r":\d+(?:-\d+)?$")

# S6b §8.1 — DEFINITION VERSIONS, not values. A metric's integer changes when the code
# that COMPUTES it changes meaning, so a consumer comparing two sidecars can tell "the
# world changed" from "we changed how we measure". Bump a metric's integer IN THE SAME
# CHANGE as the detector edit, exactly as schema.md is updated in the same change as a
# field addition. Derived metrics inherit the collector version of their underlying data;
# a separate "renderer derivation version" was considered and DROPPED (it is identical
# across every sidecar in a window, so it has zero detection power -- do not reintroduce
# it). Changing any value here requires a spec change (S6 §8.1).
METRIC_DEFINITIONS: dict[str, int] = {
    "always_loaded_tokens_est": 1,
    "always_loaded_words": 1,
    "always_loaded_file_count": 1,
    "duplicate_pair_count": 1,
    "instruction_files_over_200": 1,
    "orphan_registration_count": 1,
    "orphan_script_count": 1,
    "unchecked_binary_count": 1,
    "promotion_candidate_count": 1,
    "memory_body_count": 1,
    "hooks_with_test_ratio": 1,
    "skills_with_test_ratio": 1,
    # v1 pre-S1.M0 · v2 S1.M0+S2.M4 · v3 S2-gate D2 · v4 S6 D4
    "phantom_ref_count": 4,
    "phantom_confirmed_count": 4,   # same detector; two views, one version (§6.5 C18)
}

# S6c §6.5a axis 3. WITHOUT THIS TABLE `partial` IS UNDECIDABLE -- the collector records
# inaccessible paths but not which scan recorded them, so nothing else in this module can
# say which metric an unreadable file taints. Each entry is a prefix predicate over
# `inaccessible[].path`; ANY match taints the metric.
#
# This is a MEASUREMENT-STATE fact, not a judgment (binding rule 6): it reports whether an
# input was read, exactly as `evidence: "INACCESSIBLE"` already does per row. It renders no
# verdict and no direction -- the renderer withholds the direction word, and the VALUE is
# displayed either way.
#
# Deliberately conservative: over-tainting ADDS doubt, which is the safe direction and the
# same asymmetry as `inaccessible != clean`. Changing this map requires a spec change
# (S6 §6.5a).
_METRIC_INPUT_PREFIXES: dict[str, tuple[str, ...]] = {
    "always_loaded_words":        ("CLAUDE.md", "memory/", "rules/", "skills/"),
    "always_loaded_tokens_est":   ("CLAUDE.md", "memory/", "rules/", "skills/"),
    "always_loaded_file_count":   ("CLAUDE.md", "memory/", "rules/", "skills/"),
    "duplicate_pair_count":       ("CLAUDE.md", "rules/", "skills/", "hooks/"),
    "instruction_files_over_200": ("CLAUDE.md", "rules/", "skills/", "commands/",
                                   "agents/", "hooks/"),
    "orphan_registration_count":  ("settings.json", "hooks/"),
    "orphan_script_count":        ("hooks/",),
    "promotion_candidate_count":  ("CLAUDE.md", "rules/", "skills/", "commands/",
                                   "agents/"),
    "memory_body_count":          ("memory/", "projects/"),
    "phantom_ref_count":          ("CLAUDE.md", "rules/", "skills/", "commands/",
                                   "agents/"),
    "phantom_confirmed_count":    ("CLAUDE.md", "rules/", "skills/", "commands/",
                                   "agents/"),
    "hooks_with_test_ratio":      ("hooks/", "tests/"),
    "skills_with_test_ratio":     ("skills/", "tests/"),
    "unchecked_binary_count":     (),
}

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


# --- version-independent existence probes ---
#
# Python 3.14 changed Path.is_dir() / is_file() / exists() / is_symlink() to suppress EVERY
# OSError and return False (CPython gh-144525). Python 3.11-3.13 suppress only this family
# and RE-RAISE EACCES from an unreadable ancestor. README.md advertises "Python 3.10+", so
# 3.14 is inside the supported range.
#
# That is load-bearing here in a way it is not in most modules: this collector's disclosure
# invariant is "inaccessible is NOT clean". On 3.14, a pathlib probe would make every
# `except OSError` disclosure branch below UNREACHABLE -- an unreadable directory would
# report as absent, and the collector would emit a confident-clean inventory over a tree it
# could not read, which is precisely the failure the guarded probe sites exist to prevent.
#
# os.stat raises on every version, so these probes keep the error distinction intact and the
# callers' existing handlers keep firing. The ignored set below is pathlib's own
# `_ignore_error` set: swallowing LESS would turn an ordinary absent path into a crash at
# every call site, so parity with 3.11 is the requirement, not merely "raise more".
_IGNORED_PROBE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.EBADF, errno.ELOOP})


def _probe_stat(path, follow_symlinks):
    """`os.stat` reduced to pathlib's ignored-error set: the stat result, or None for an
    error pathlib would have reported as False. Every other OSError PROPAGATES, which is
    the entire reason this helper exists.

    ValueError is caught for the same parity reason: pathlib's probes return False for a
    non-encodable path (an embedded NUL), where `os.stat` raises ValueError -- not an
    OSError, so no caller's handler would catch it. It is mapped to the same "not there"
    answer pathlib gives rather than swallowed silently: `_IGNORED_PROBE_ERRNOS` and this
    clause together are the exhaustive statement of what a probe is allowed to hide.

    POSIX-targeted, matching the rest of this module (O_DIRECTORY, dir_fd, geteuid):
    pathlib additionally ignores three Windows-only winerror codes, which are not mirrored.
    """
    try:
        return os.stat(path, follow_symlinks=follow_symlinks)
    except OSError as exc:
        if exc.errno in _IGNORED_PROBE_ERRNOS:
            return None
        raise
    except ValueError:
        return None


def _probe_is_dir(path):
    """`Path.is_dir()` that preserves the error distinction on every Python version."""
    st = _probe_stat(path, follow_symlinks=True)
    return st is not None and stat.S_ISDIR(st.st_mode)


def _probe_is_file(path):
    """`Path.is_file()` that preserves the error distinction on every Python version."""
    st = _probe_stat(path, follow_symlinks=True)
    return st is not None and stat.S_ISREG(st.st_mode)


def _probe_exists(path):
    """`Path.exists()` that preserves the error distinction on every Python version.
    FOLLOWS symlinks, so a broken link is False -- same as the pathlib call it replaces."""
    return _probe_stat(path, follow_symlinks=True) is not None


def _probe_is_symlink(path):
    """`Path.is_symlink()` that preserves the error distinction on every Python version.
    Does NOT follow symlinks (lstat semantics), same as the pathlib call it replaces."""
    st = _probe_stat(path, follow_symlinks=False)
    return st is not None and stat.S_ISLNK(st.st_mode)


def _read_text(path):
    """Read `path` as utf-8 text (errors="replace"). Returns `(text, "VERIFIED")` on
    success or `(None, "INACCESSIBLE")` on failure. The `is_file()` guard matches
    `_read_head`'s "no blocking open() on a FIFO" invariant: an in-root, registered
    `*-dispatcher.py` (or any of the other `_read_checked` call sites) that is actually
    a FIFO/socket/dir must not block the collector. `is_file()` follows symlinks (True
    for a regular file or a symlink to one; False for FIFO/dir/socket/broken symlink),
    so regular-file behavior is unchanged — no false negatives."""
    try:
        # The probe itself can raise OSError (EACCES) when an ancestor directory
        # is unsearchable, not just when the read fails — the probe is inside the
        # same try as the read so both fold into the one INACCESSIBLE outcome.
        if not _probe_is_file(path):
            return None, "INACCESSIBLE"
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
    """The probes can raise PermissionError etc. (they swallow only the ENOENT family).
    Treat any OSError as 'cannot determine' so one locked dir marks just that entry
    inaccessible instead of blanking the whole inventory. Returns (present, ok).

    The ORDERED PAIR is deliberate and must not be collapsed into a single `os.lstat`,
    however much "is there a directory entry at this path" reads like one question. For a
    symlink whose TARGET lives under an unreadable directory, `os.lstat` succeeds -- it
    never follows -- so a one-probe form answers (True, True), "present, and I am sure."
    The follow-symlink probe runs first, raises EACCES, and the answer is (False, False),
    "cannot determine," which is the entire purpose of the tri-state: confidently-present
    is exactly as false a claim as confidently-absent when the target cannot be reached.
    Pinned by test_safe_exists_keeps_two_probes_for_a_symlink_into_an_unreadable_tree.

    On 3.14 the pathlib spelling of this pair would report (False, True) for an unreadable
    path -- absent, and sure of it. See the `_probe_stat` block above."""
    try:
        return (_probe_exists(path) or _probe_is_symlink(path)), True
    except OSError:
        return False, False


def _disclose_unlistable_glob(base: Path, pattern: str, matches, sink: list[str],
                              label: str) -> None:
    """glob() cannot tell an empty directory from an unreadable one (spec AMENDMENTS
    A59/A60). MEASURED on CPython 3.11.14 against a real 0o000 dir with a genuine match
    inside it: `Path.glob()` raises nothing and returns `[]` -- indistinguishable from a
    directory that is merely empty or absent. `os.scandir` DOES raise, so this probes it
    -- but ONLY on the empty-result path (an empty glob is common and legitimate; the
    extra syscall must not land on every call site unconditionally).

    `pattern` is a glob pattern STRING, always forward-slash-separated regardless of
    platform (mirroring the profile's `hook_script_globs`/`container_dirs` convention
    elsewhere in this file) -- `posixpath.dirname`, not `pathlib`, is deliberate here:
    it parses the pattern's directory component as text, before any `Path` is built from
    it.

    A wildcard in the directory component (`skills/*/rules/*.md`) has no single
    directory to probe -- that half of the blind spot is covered separately by a
    `blind_spots` disclosure (TRK-082 T4), and this function returns early, silently, so
    every call site can hand its raw pattern here without first hand-classifying it.

    An ABSENT target directory returns with no record: a harness that simply lacks an
    optional directory is not a blind spot, and reporting one here would be the exact
    false-positive TRK-050's review caught and reversed. Only a PRESENT-but-unlistable
    directory is recorded, naming it by path.

    The probe consumes at most one entry off the scandir iterator, purely to force the
    syscall -- it never sorts, collects, or exposes entry names, so this stays
    deterministic across `PYTHONHASHSEED` like every other signal in this file. The
    residual TOCTOU gap (the directory could still change state between this probe and
    the `glob()` call it is disclosing for) is accepted, not engineered around -- the
    goal is to stop reporting a locked surface as clean, not to close a race."""
    if matches:
        return
    dirpart = posixpath.dirname(pattern)
    if any(c in dirpart for c in "*?["):
        return
    target = base / dirpart if dirpart else base
    try:
        with os.scandir(target) as it:
            next(iter(it), None)
    except FileNotFoundError:
        return
    except OSError as e:
        sink.append(f"{label} listing failed for {target}: {e}")


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


def _contained_or_disclosed(fp: Path, key: str, root: Path, root_stat, label: str,
                            blind_spots: list[str]) -> bool:
    """Shared containment gate for a profile-glob (or profile container-dir) consumer
    that is about to read `fp`'s CONTENT (M11 exit gate, Finding 2): True iff `key` (fp's
    resolved physical identity, from `_physical_key`) resolves inside `root`. On a False
    verdict, records a blind_spots entry naming `fp` and `label`, deduped -- so a profile
    glob that escapes containment (a symlinked directory matching an innocuous-looking
    glob/role name, which `load_profile`'s string-only validation cannot see -- Finding
    1's fix closes only the literal-'..'/absolute-string vector, not this one) is
    disclosed, never silently read NOR silently dropped ("inaccessible is NOT clean").
    Message format matches `_deduped_instruction_files`'s existing
    "<label> <path> resolves outside the harness root — not read" wording, so every
    containment-escape disclosure in this collector reads the same way.

    `root_stat is None` (an unstat'able root) makes containment undecidable, so it is
    treated as False -- "cannot determine" is never reported as "confirmed inside"."""
    try:
        inside = root_stat is not None and _resolves_inside_root(Path(key), root, root_stat)
    except (OSError, RuntimeError):
        inside = False
    if not inside:
        msg = f"{label} {_rel_safe(root, fp)} resolves outside the harness root — not read"
        if msg not in blind_spots:
            blind_spots.append(msg)
    return inside


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
    except (OSError, RuntimeError):
        # RuntimeError, not just OSError: on CPython Path.resolve() converts an ELOOP
        # into RuntimeError("Symlink loop from ...") rather than an OSError subclass, so
        # a --out path (or, below, a declared input path) that is a symlink loop would
        # otherwise escape this handler and abort the caller. Same defect and same fix as
        # the sibling write-time check in _reject_if_target_is_an_input_path (Codex
        # challenge finding F7); this is the caller-entry twin of that helper's ladder.
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
        except (OSError, RuntimeError):  # see the identical rationale above
            p_resolved = p_path
        if p_resolved == resolved:
            return False, None
        try:
            if os.path.samestat(os.stat(p_resolved), os.stat(resolved)):
                return False, None
        except OSError:
            pass
    return True, resolved


class WriteContainmentError(OSError):
    """The write target's PINNED parent directory is inside (or IS) a guarded root, or
    the directory that containment was decided against is not the directory actually
    held open. Subclasses OSError deliberately: collector.main's existing
    `except OSError` around its --out write keeps working unchanged, while
    render_html.write_html_safely can catch THIS type specifically and re-raise its own
    catchable RenderError.

    Callers DO still need a broader `except OSError` alongside it: the fd path raises a
    BARE OSError for a refused open (ELOOP from O_NOFOLLOW, ENOENT/EACCES on an
    unreadable parent), which is not a containment verdict and is not this type. See
    render_html.write_html_safely, which catches both. (An earlier draft of this docstring
    claimed no caller needed a broader clause; that was wrong and is corrected here — flagged
    by the Codex plan gate as P3-7.)"""


_TMP_NAME_ATTEMPTS = 8       # O_EXCL collision retries; token_hex(16) makes >1 collision
                             # astronomically unlikely, so a bounded loop is honest rather
                             # than optimistic — exhaustion raises instead of looping.


def _dir_fd_write_supported():
    """True when this platform can do the whole fd-pinned write: open/rename/unlink/stat
    all accepting dir_fd. Read from os.supports_dir_fd AT CALL TIME, never cached at
    import, so the fallback branch is reachable in a test by monkeypatching the capability
    set (F4) — a fallback nobody can execute is a dark path, not a fallback.

    PROBES os.rename, NOT os.replace. CPython does not register os.replace in
    os.supports_dir_fd even though it accepts src_dir_fd/dst_dir_fd — it is the same
    renameat syscall as os.rename, which IS registered. Verified 2026-08-02 on this
    platform: `os.replace in os.supports_dir_fd` is False while a real dir-fd os.replace
    succeeds. Gating on os.replace would return False forever, silently routing EVERY
    production write through the vulnerable fallback and shipping this entire hardening
    dark. Do not "correct" this back to os.replace."""
    return all(fn in os.supports_dir_fd
               for fn in (os.open, os.rename, os.unlink, os.stat))


def _search_only_dir_flag():
    """This platform's SEARCH-ONLY directory open flag, or None where there is no VERIFIED
    one. Read at call time, matching `_dir_fd_write_supported`'s posture above, so a test
    can simulate a platform without one instead of the absence branch going dark.

    O_SEARCH ONLY, DELIBERATELY NOT O_PATH. Both are nominally search-only, but they are
    not interchangeable and the difference is not something to guess at in the module's one
    physical write path. On darwin, O_SEARCH was MEASURED here (2026-08-02, CPython 3.11.14,
    os.O_SEARCH == 0x40100000) to support every dir_fd operation this write performs --
    os.open(O_CREAT|O_EXCL, dir_fd=), os.stat(dir_fd=), os.replace(src_dir_fd=, dst_dir_fd=),
    os.fstat -- while os.scandir(fd) fails EBADF, which is exactly the privilege being given
    up. Linux's O_PATH is documented for a RESTRICTED set of *at() uses (fchownat, fstatat,
    linkat, readlinkat); renameat is not named in it even though it is often observed to
    work, and "often works in practice" is not a basis for the containment guarantees this
    helper carries. No Linux host is available here to measure it on, so O_PATH is left out
    rather than adopted unverified: on a platform with no O_SEARCH the ladder simply stays on
    O_RDONLY, i.e. the pre-existing behaviour, and an unreadable output directory keeps
    failing EACCES there. Do not add O_PATH from the man page alone -- add it when a Linux
    host has run the four operations above against an O_PATH descriptor."""
    flag = getattr(os, "O_SEARCH", None)
    return flag if isinstance(flag, int) else None


def _open_dir_traverse_only(path, *, extra_flags=0, dir_fd=None):
    """Open a directory for TRAVERSAL ONLY -- `fstat` it, and anchor *at() calls at it.
    Never to list it. `os.scandir` on the returned descriptor may fail, and that is the
    point: a caller that needs entries must open its own read-capable descriptor.

    WHY THIS EXISTS (P2-1). The fd-pinned write replaced `tempfile.mkstemp`, which needs
    only write + execute on the output directory, with `os.open(dir, O_RDONLY)`, which also
    needs READ. That silently raised the permission floor of every write: a 0o333 drop-box
    stopped working, and so did any 0o755 output directory sitting under a single 0o111
    ancestor, because the `..` walk climbs to the namespace root. Neither caller ever lists
    the directories it opens, so read was privilege taken and not used.

    THE LADDER IS O_RDONLY FIRST, SEARCH-ONLY ONLY ON EACCES, and the order is the point.
    The common case keeps running on the descriptor type every other test in this module
    already exercises; the less-travelled one is confined to the case that would otherwise
    fail outright, where the alternative is not writing at all. `raise` on a missing
    search-only flag re-raises the ORIGINAL PermissionError, so a platform without one
    reports exactly what it reported before this change.

    RETRYING THE OPEN DOES NOT WIDEN ANY WINDOW. The first attempt failed, so it pinned
    nothing and no check was decided on it; every containment check in this module runs
    against the descriptor this function RETURNS.

    CLASS 1 OF THE WRITE-SIDE THREAT MODEL IS UNREACHABLE THROUGH THE RETRY, which is a
    stronger statement than "O_NOFOLLOW is carried on both rungs" (it is, `extra_flags` is
    applied identically to each). O_NOFOLLOW is evaluated while resolving the FINAL
    component, before the target's permission bits are consulted, so a symlinked parent
    fails rung 1 with ENOTDIR -- not EACCES -- and the retry is never entered for that case.
    Measured on darwin both ways: O_RDONLY|O_NOFOLLOW and O_SEARCH|O_NOFOLLOW refuse a
    symlinked directory identically."""
    flags = os.O_DIRECTORY | extra_flags
    try:
        return os.open(path, os.O_RDONLY | flags, dir_fd=dir_fd)
    except PermissionError:
        search_flag = _search_only_dir_flag()
        if search_flag is None:
            raise
        return os.open(path, search_flag | flags, dir_fd=dir_fd)


def _reject_if_parent_inside_guard_roots(parent_real, parent_fstat, guard_roots):
    """PATHNAME-based containment for a write's parent directory.

    FALLBACK BRANCH ONLY. The dir_fd branch must NOT call this — it calls
    `_reject_if_pinned_dir_inside_guard_roots` below, which decides about the opened
    descriptor instead. Deciding containment about a pathname and then writing through a
    descriptor is what made the class-2 grandparent attack land; this function is kept
    solely because `_write_text_contained_fallback` has no descriptor to anchor to, and
    that branch's documented limitation covers the resulting exposure.

    Reuses the SAME two mechanisms the read paths use — `_resolves_inside_root`
    (Path.parents + os.path.samestat, never str.startswith) and a direct inode compare —
    rather than introducing a second containment predicate. A second predicate is a
    drift source and was itself a finding in the previous stage.

    Two checks per guard root, both required:
      (a) inode identity: the pinned directory IS the guard root;
      (b) ancestry: the pinned directory's realpath lies inside the guard root.
    Neither subsumes the other — (a) catches a case-insensitive or hard-linked alias of
    the root itself, (b) catches a descendant.

    A guard root that cannot be stat'd is SKIPPED (nothing safe to compare against),
    matching validate_write_target's existing posture for the same situation. The dir_fd
    path deliberately DIVERGES from that posture — see the Y4 note in
    `_reject_if_pinned_dir_inside_guard_roots`."""
    for guard_root in guard_roots:
        guard_root_path = Path(guard_root)
        try:
            guard_root_stat = os.stat(guard_root_path)
        except OSError:
            continue
        if os.path.samestat(parent_fstat, guard_root_stat):
            raise WriteContainmentError(
                f"refusing to write: the target directory IS the guarded root {guard_root_path}")
        if _resolves_inside_root(parent_real, guard_root_path, guard_root_stat):
            raise WriteContainmentError(
                f"refusing to write: {parent_real} resolves inside the guarded root "
                f"{guard_root_path}")


def _reject_if_pinned_dir_inside_guard_roots(dir_fd, guard_roots):
    """Containment decided about the OPENED DESCRIPTOR, not about any pathname.

    Walks upward from the pinned directory using `..` resolved BY THE KERNEL relative to
    each successive descriptor, comparing every ancestor's (st_dev, st_ino) against each
    guard root. Because no pathname is ever re-resolved, a symlink or rename applied to
    any component — parent, grandparent, or higher — after the pin cannot change the
    result. This is what closes class 2 (the grandparent attack), which O_NOFOLLOW alone
    does not: O_NOFOLLOW constrains only the FINAL component.

    Scope of this check — see the six-class table in `write_text_contained`'s docstring.
    It closes classes 1–4 together with the rest of the helper; classes 5 and 6 are
    ACCEPTED residuals, not oversights, and are recorded at RISK_REGISTER R11 / AMENDMENTS
    A36. Do not describe this walk as making ancestry immutable — it does not.

    DENY ON UNCERTAINTY, BUT NOT ON ABSENCE (Y4). A guard root that EXISTS but cannot be
    stat'd (EACCES, ELOOP, or any other OSError) is unverifiable, and for a tool whose core
    invariant is "zero writes under the mapped root" that is exactly when writing is unsafe
    — so it RAISES rather than being skipped. This deliberately diverges from
    validate_write_target's older skip-on-unstattable posture.

    A guard root that simply DOES NOT EXIST (ENOENT / FileNotFoundError) is a different
    case and is PERMITTED. Both render paths add `Path.home() / ".claude"` as a permanent
    floor root (render_html.py:5163, serve.py:246), and in a public standalone install that
    directory legitimately may not exist — denying on absence would reject EVERY write for
    a user whose only sin is not having a ~/.claude. A nonexistent directory also cannot
    contain the target, so permitting it is sound rather than merely convenient: there is
    nothing to be inside of. Absent roots are still covered LEXICALLY by
    validate_write_target at caller entry, which compares configured pathnames and does not
    require the root to exist."""
    guard_stats = []
    for guard_root in guard_roots:
        try:
            guard_stats.append((guard_root, os.stat(guard_root)))
        except FileNotFoundError:
            continue          # absent != unverifiable; see docstring. Nothing to be inside of.
        except OSError as exc:
            raise WriteContainmentError(
                f"refusing to write: guarded root {guard_root} exists but cannot be stat'd "
                f"({exc.strerror}), so containment cannot be established") from exc

    # DESCRIPTOR OWNERSHIP (Y3). An earlier draft leaked one fd per SUCCESSFUL traversal:
    # at the filesystem root it returned while `parent_fd` was still open, and the outer
    # finally closed only `current_fd`. Codex executed that loop 20 times and the process
    # gained 20 descriptors. The shape below keeps EXACTLY ONE owned descriptor in
    # `current_fd` at all times, and `parent_fd` is owned by its own try/finally until
    # ownership is explicitly transferred -- so every exit path, including the root return
    # and a raising os.close, closes exactly what it owns and nothing twice.
    current_fd = os.dup(dir_fd)          # dup: never consume the caller's descriptor
    try:
        while True:
            current_stat = os.fstat(current_fd)
            for guard_root, guard_stat in guard_stats:
                if os.path.samestat(current_stat, guard_stat):
                    raise WriteContainmentError(
                        f"refusing to write: the target directory is {guard_root} or lies "
                        f"inside it (established from the opened descriptor)")
            # TRAVERSE-ONLY (P2-1): this walk `fstat`s each ancestor and anchors the next
            # `..` at it. It never lists one, so it must not demand read -- a single 0o111
            # ancestor anywhere above the output directory would otherwise fail the write.
            parent_fd = _open_dir_traverse_only("..", dir_fd=current_fd)
            try:
                at_root = os.path.samestat(os.fstat(parent_fd), current_stat)
            except OSError:
                os.close(parent_fd)
                raise
            if at_root:
                os.close(parent_fd)   # `..` is itself: namespace root, containment clean
                return                # current_fd closed by the outer finally
            # TRANSFER OWNERSHIP BEFORE CLOSING THE OLD FD. Rebinding first means the outer
            # finally always owns exactly one live descriptor, so even if this os.close
            # raises (POSIX leaves the fd state unspecified after a failed close) there is
            # no path on which the outer finally closes an fd a second time.
            stale_fd, current_fd = current_fd, parent_fd
            os.close(stale_fd)
    finally:
        os.close(current_fd)


def _reject_if_target_is_an_input_path(out_path, input_paths):
    """Write-time re-check of the `input_paths` dimension (F-P2, added at plan review).

    `validate_write_target` rejects a target that IS one of the collector's own read
    inputs; that is what stops `--out ~/.claude.json`, a file sitting OUTSIDE every
    directory root where guard-root containment alone would wrongly allow the overwrite.
    The pre-change code re-checked this immediately before mkstemp. Dropping it when the
    write moved into this helper would have silently narrowed the guarantee, so it is
    re-checked here at the latest possible moment.

    Mirrors validate_write_target's own comparison ladder EXACTLY rather than inventing a
    looser one: literal (lexical/resolved equality), then resolved-realpath equality, then
    an os.path.samestat inode compare where both sides exist — that last rung is what
    catches an alias a string compare cannot see (an input that is itself a symlink onto
    the target). A path that cannot be stat'd is skipped, same posture as elsewhere.

    RuntimeError IS CAUGHT ALONGSIDE OSError, and it is not defensive padding: on CPython
    `Path.resolve()` converts an ELOOP into `RuntimeError("Symlink loop from ...")`, which
    is NOT an OSError. REPRODUCED on 3.11.14 by the Codex challenge (finding F7) -- a
    declared input that is a symlink loop escaped every caller's `except OSError` as an
    unhandled RuntimeError. Pinned by
    `test_write_text_contained_reports_a_symlink_loop_as_an_oserror`. Falling back to the
    unresolved path is the same posture the OSError arm already takes: an unresolvable
    path is compared literally rather than silently dropped."""
    if not input_paths:
        return
    lexical = Path(os.path.normpath(str(out_path)))
    try:
        resolved = out_path.resolve()
    except (OSError, RuntimeError):
        resolved = out_path
    for candidate_input in input_paths:
        input_path = Path(candidate_input)
        if input_path in (lexical, resolved):
            raise WriteContainmentError(
                f"refusing to write: {out_path} is one of this tool's own read inputs")
        try:
            input_resolved = input_path.resolve()
        except (OSError, RuntimeError):
            input_resolved = input_path
        if input_resolved == resolved:
            raise WriteContainmentError(
                f"refusing to write: {out_path} resolves onto the read input {input_path}")
        # NOTE: WriteContainmentError SUBCLASSES OSError, so the raise must sit OUTSIDE
        # the try -- raising it inside would be swallowed by this same `except OSError`.
        # (validate_write_target has the identical shape and is safe only because its
        # raise-equivalent is a `return`. Do not "simplify" this back into the try.)
        try:
            same_file = os.path.samestat(os.stat(input_resolved), os.stat(resolved))
        except OSError:
            same_file = False
        if same_file:
            raise WriteContainmentError(
                f"refusing to write: {out_path} is the same file as the read input "
                f"{input_path}")


def _reject_if_pinned_target_is_an_input_path(dir_fd, out_name, input_paths):
    """Class-4 check RE-BOUND to the pinned directory (Y5). The pre-open rung above is
    kept for its better error messages on the common case; THIS is the one that is not
    defeatable by an intermediate-component swap.

    `_reject_if_target_is_an_input_path` decides about a PATHNAME before the parent is
    opened, so a redirect applied after it runs re-points what it cleared. Once `dir_fd`
    pins the parent inode, `os.stat(out_name, dir_fd=dir_fd)` names the file the write will
    actually land on — and that is what must be compared against the declared read inputs.
    Without this rung, a redirect can route the write onto one of the tool's own inputs
    sitting OUTSIDE every guard root, where guard-root containment alone would wrongly
    permit it. That is exactly why class 4 is listed as closed rather than subsumed by
    class 2: guard-root containment does not cover an input outside every root.

    EXCEPTION POLICY — ABSENCE PERMITS, AMBIGUITY DENIES. This is the same split Y4
    established for guard roots in `_reject_if_pinned_dir_inside_guard_roots`; the two must
    not be allowed to drift apart:
      * target FileNotFoundError -> return. A target that does not exist yet aliases
        nothing, and this is the COMMON case (every first write), so denying here would
        reject ordinary runs.
      * target OSError (EACCES, ELOOP, ...) -> RAISE. The name exists but its identity is
        unverifiable, and "cannot tell" is not "safe" for a tool whose core invariant is
        that it never writes over its own read surface.
      * input FileNotFoundError -> skip THAT input, continue with the rest. A declared
        input that is gone cannot be the file being written.
      * input OSError -> RAISE, same reasoning as the target.

    `follow_symlinks=False` on the TARGET is deliberate and load-bearing: `os.replace`
    retargets the NAME, unlinking whatever it currently denotes and linking the fresh temp
    inode in its place. It does NOT write through a symlink, so a symlinked target name
    leaves the pointed-at input's bytes untouched — the identity that matters here is the
    name's own. The INPUT side resolves normally, because an input declared as a symlink
    ONTO the target is precisely the alias this rung exists to catch.

    KNOWN SEAM, stated rather than papered over: a target NAME that is itself a declared
    input symlink is caught by the pre-open rung's literal/resolved comparison, not by the
    samestat here (lstat sees the link's own inode, while the INPUT side is resolved).
    Both rungs run, so the case is covered ON A STABLE FILESYSTEM. It is NOT covered when
    a redirect lands between the two rungs: the pre-open rung then cleared a different
    pathname, and this rung's lstat/stat asymmetry misses the match, so `os.replace` can
    retarget a declared input symlink. Raised by the Codex challenge (finding F1) and
    stated here rather than closed, for two reasons: reaching it requires write access to
    the output parent chain, which is the same privilege that makes accepted residual
    class 5 unclosable (see the six-class table in `write_text_contained`), and the fix
    would be a third stat mode — the accumulating-checks failure mode that got the st_dev
    tripwire declined. Do NOT add that third stat mode. Do not upgrade this paragraph into
    a claim that the seam is closed."""
    if not input_paths:
        return
    try:
        target_stat = os.stat(out_name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return          # nothing on disk under that name yet -> it cannot alias an input
    except OSError as exc:
        raise WriteContainmentError(
            f"refusing to write: {out_name} exists in the pinned directory but cannot be "
            f"stat'd ({exc.strerror}), so it cannot be cleared against this tool's own "
            f"read inputs") from exc
    for candidate_input in input_paths:
        input_path = Path(candidate_input)
        try:
            input_stat = os.stat(input_path)
        except FileNotFoundError:
            continue    # a declared input that no longer exists cannot be the write target
        except OSError as exc:
            raise WriteContainmentError(
                f"refusing to write: read input {input_path} cannot be stat'd "
                f"({exc.strerror}), so the pinned target cannot be cleared against it"
            ) from exc
        if os.path.samestat(target_stat, input_stat):
            raise WriteContainmentError(
                f"refusing to write: the pinned target {out_name} is the same file as the "
                f"read input {input_path}")


def write_text_contained(out_path, text, guard_roots, *, input_paths=(),
                         encoding_errors="strict"):
    """THE single physical write for every sink in this tool — collector.main's `--out`
    and render_html.write_html_safely both call exactly this. Neither grows its own copy
    of the mechanics (that seam produced four findings in the previous stage).

    WRITE-SIDE THREAT MODEL — SIX CLASSES CONSIDERED, FOUR CLOSED, TWO ACCEPTED.
    Settled 2026-08-02; authoritative record RISK_REGISTER R11 + AMENDMENTS A36. A later
    reviewer re-finding classes 5 or 6 is EXPECTED and pre-answered — point it there rather
    than re-deriving. This is the honest claim about this helper; there is no stronger one.

        1 Symlinked PARENT of the target ................. CLOSED  O_NOFOLLOW on the open
        2 Swapped GRANDPARENT / intermediate component ... CLOSED  fd-anchored `..` walk
        3 Hard-link truncation of a shared inode ......... CLOSED  O_CREAT|O_EXCL+replace
        4 Overwriting one of this tool's own read inputs . CLOSED  pinned-identity check
        5 Concurrent RENAME of the pinned directory ...... ACCEPTED
        6 Bind-mount alias of a guard-root descendant .... ACCEPTED

    An fd pins an INODE, not where that inode lives. Classes 5 and 6 were reproduced on a
    real filesystem and are not closable in portable POSIX. They are accepted because ANY
    attacker positioned to exploit them can already modify this tool's own source, so no
    in-process check can be load-bearing against them: class 5 needs write access to the
    output parent chain (`$HOME` for the default report directory) and class 6 needs mount
    privileges. Either principal can edit this file, swap a hook, or rewrite settings.json
    without racing anything. A guard cannot be a security boundary against a principal who
    owns the code implementing the guard. Classes 1–4 are exactly those reachable WITHOUT
    that privilege, which is where the real risk lives. (The weaker framing — "it's a
    single-user local tool" — is explicitly rejected: single-user machines run untrusted
    code all day.) What would VOID this acceptance is a THIRD class reachable without that
    privilege line; a further variant of 5 or 6 is the same accepted class.

    On the dir_fd path: the parent directory is opened ONCE with O_NOFOLLOW (a symlinked
    parent is refused outright, not followed) and the returned fd PINS that directory's
    inode for the rest of the call. Containment is then decided against the PINNED INODE
    via `_reject_if_pinned_dir_inside_guard_roots`, never against a pathname, and the temp
    file is created RELATIVE TO THAT FD. The old shape re-validated a PATHNAME and then
    handed that same pathname to tempfile.mkstemp; deciding about a pathname and writing
    through a descriptor is what made class 2 exploitable. This is the write-side mirror of
    the read-side closure `_read_project_file` and `_walk_contained_dirs` already implement.

    ABA binding: the realpath used for the ancestry check must `samestat` a FRESH stat
    taken now, against the OPENED fd. If they differ, the directory containment was
    decided about is not the directory being written into, and the write is refused —
    the same both-directions check the read paths carry. It is defence in depth, NOT the
    closure mechanism; the fd-anchored walk is.

    HARD-LINK DEFENSE (F5), preserved exactly: an outside-root hard link whose inode is
    also linked under a guarded root passes every resolve()-based path check, because
    hard links are invisible to path resolution. Writing in place would truncate that
    shared inode — a read-only-invariant bypass. So a FRESH inode is created in the same
    directory (O_CREAT|O_EXCL) and os.replace only ever retargets the out-path NAME at
    it; the under-root hard-linked inode keeps its original bytes.

    `input_paths` (class 4): the collector's own read surface. The caller-entry
    `validate_write_target` already rejects a target that IS one of these, which is what
    stops `--out ~/.claude.json` — a read input that sits OUTSIDE every directory root, so
    guard-root containment alone would wrongly allow overwriting it. That dimension is
    re-checked TWICE here: once pre-open against the pathname (better error messages), and
    again AFTER the pin against the target's identity in the pinned directory, which is the
    rung an intermediate-component swap cannot defeat. Threading it through matters even
    though no current caller passes a non-empty value: the previous shape re-checked
    `input_paths` immediately before mkstemp, and dropping it silently would have been a
    real (if currently inert) regression in a guarantee this tool advertises.

    `encoding_errors` is the text-encoding error mode ONLY (render_html needs
    "backslashreplace" so a lone UTF-16 surrogate degrades to a literal escape instead of
    aborting the write with no report produced). It changes nothing about path security.

    Raises WriteContainmentError (an OSError) on a containment rejection, OSError on any
    filesystem failure, UnicodeEncodeError on an unencodable payload under
    encoding_errors="strict". On every failure path the temp file is unlinked
    BEST-EFFORT: cleanup errors are swallowed so the original write failure is what
    propagates, so residue is possible if the unlink itself fails."""
    out_path = Path(out_path)
    parent = out_path.parent
    out_name = out_path.name
    if not out_name:
        raise WriteContainmentError(f"refusing to write: no file name in target {out_path}")
    guard_roots = [r for r in guard_roots if r]
    # MATERIALISE ONCE (Codex challenge, finding F2). `input_paths` is checked by TWO
    # rungs -- pre-open and post-pin -- so a one-shot iterable (a generator expression at
    # a call site) would be drained by the first and leave the AUTHORITATIVE second rung
    # iterating zero inputs. That fails OPEN and does so silently: a generator is truthy,
    # so the `if not input_paths` early-return does not catch it either. Today's callers
    # pass `iter_input_paths()`, which returns a list, so nothing currently trips this --
    # which is exactly why it needs pinning here rather than at a call site.
    input_paths = tuple(input_paths)

    _reject_if_target_is_an_input_path(out_path, input_paths)

    if not _dir_fd_write_supported():
        return _write_text_contained_fallback(
            out_path, parent, text, guard_roots, input_paths, encoding_errors)

    # TRAVERSE-ONLY (P2-1): this descriptor creates, stats and replaces entries in the
    # pinned directory; it never lists them. Requiring read would hold the write to a
    # HIGHER permission bar than the mkstemp shape it replaced, breaking a write-only
    # drop-box. O_NOFOLLOW rides on both rungs of the ladder -- see the helper.
    dir_fd = _open_dir_traverse_only(
        parent, extra_flags=os.O_NOFOLLOW | os.O_NONBLOCK)
    tmp_name = None
    try:
        parent_fstat = os.fstat(dir_fd)
        parent_real = Path(_physical_key(parent))
        try:
            parent_real_stat = os.stat(parent_real)
        except OSError as exc:
            raise WriteContainmentError(
                f"refusing to write: could not stat the resolved parent {parent_real}") from exc
        # TOCTOU: the path containment is decided about must BE the directory held open.
        # Defence in depth only -- the fd-anchored walk below is the actual mechanism.
        if not os.path.samestat(parent_real_stat, parent_fstat):
            raise WriteContainmentError(
                f"refusing to write: {parent_real} is no longer the opened directory (TOCTOU)")
        # CONTAINMENT IS DECIDED ABOUT THE DESCRIPTOR, NOT THE PATHNAME. Do NOT call
        # _reject_if_parent_inside_guard_roots here -- that is the FALLBACK branch's
        # pathname predicate, and deciding about a pathname while writing through a
        # descriptor is exactly what made class 2 (the grandparent swap) exploitable.
        _reject_if_pinned_dir_inside_guard_roots(dir_fd, guard_roots)
        # Class 4, re-bound to the pin. Must run BEFORE the temp file is created: once the
        # temp exists, a failure here would leave residue for the finally to clean up for
        # no reason, and the decision does not depend on the temp.
        _reject_if_pinned_target_is_an_input_path(dir_fd, out_name, input_paths)

        # tempfile.mkstemp does NOT accept dir_fd, so the name is generated here and
        # created with O_EXCL relative to the pinned fd. Bounded retry: exhaustion raises
        # rather than spinning.
        file_fd = None
        for _attempt in range(_TMP_NAME_ATTEMPTS):
            candidate = f".harness-map-{secrets.token_hex(16)}.tmp"
            try:
                file_fd = os.open(
                    candidate,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=dir_fd,
                )
            except FileExistsError:
                continue
            tmp_name = candidate
            break
        # BOTH are tested, not just file_fd: they are set together on the successful
        # iteration, so either being None means the loop never succeeded. Testing only
        # file_fd leaves tmp_name typed `str | None` at the os.replace below, which is
        # a genuine type error rather than a mypy quibble -- the plan's block tested
        # file_fd alone and did not type-check.
        if file_fd is None or tmp_name is None:
            raise OSError(
                f"could not create a unique temporary file in {parent} after "
                f"{_TMP_NAME_ATTEMPTS} attempts")

        # closefd=False + one explicit close (X5). os.fdopen can raise BEFORE the wrapper
        # takes ownership of the raw descriptor, which leaks it. With closefd=False the
        # wrapper NEVER closes file_fd, so this finally is the single owner on every path
        # -- including a raise from inside os.fdopen itself, which is why the try opens
        # before the call rather than around the with-body. There is no double-close
        # precisely because nothing else is permitted to close it.
        try:
            with os.fdopen(file_fd, "w", encoding="utf-8", errors=encoding_errors,
                           closefd=False) as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(file_fd)
        os.replace(tmp_name, out_name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass          # best-effort cleanup; the write failure is what propagates
        try:
            os.close(dir_fd)
        except OSError:
            pass
    return None


def _write_text_contained_fallback(out_path, parent, text, guard_roots, input_paths,
                                   encoding_errors):
    """EXPLICIT platform fallback (F4) for a system whose os.open/os.replace/os.unlink do
    not accept dir_fd. Behaviorally identical to the pre-S7 write: re-resolve and re-check
    the target immediately before mkstemp, then mkstemp/fsync/os.replace in the target's
    own directory.

    The hard-link defense (F5) is UNCHANGED here — mkstemp still creates a fresh inode in
    the same directory and os.replace still only retargets the name.

    LIMITATION, true on THIS branch only, and WIDER than the dir_fd path's. With no
    descriptor to anchor to, containment is decided about a pathname and the write then
    goes through that pathname, so classes 1 and 2 (symlinked parent, swapped grandparent)
    are narrowed but not fully closed here — the residual window sits between the re-check below
    and the mkstemp call, and a parent-directory symlink swapped into it can still redirect
    the write. Class 3 still holds on this branch unconditionally: mkstemp creates a fresh
    inode and os.replace only retargets the name. Class 4 holds only as far as
    `_reject_if_target_is_an_input_path` reaches, which is a pathname comparison with no
    pinned re-check behind it.

    TWO FAIL-OPEN POINTS specific to this branch, named rather than left implicit (Codex
    challenge, finding F4). `_reject_if_parent_inside_guard_roots` SKIPS a guard root that
    exists but cannot be stat'd, and `_reject_if_target_is_an_input_path` treats an
    unstattable input as "not the same file". The dir_fd branch denies on that same
    ambiguity. The divergence is deliberate — it matches `validate_write_target`'s
    long-standing posture, and this branch has no authoritative pinned rung to fall back on
    — but it is a weaker posture, not an equivalent one, and it should be revisited if this
    branch ever becomes reachable on a supported platform.

    This branch is reached only where the platform cannot do dir_fd writes at all, so the
    alternative is not writing. Do not describe it as equivalent to the dir_fd path, and do
    not justify it as "fine for a single-user local tool" — that framing is explicitly
    rejected (RISK_REGISTER R11). See `write_text_contained`'s six-class table for the
    posture that does apply, and note that classes 1 and 2 are listed CLOSED there on the
    strength of the dir_fd path, which is the one production actually takes."""
    resolved_parent = Path(_physical_key(parent))
    try:
        parent_stat = os.stat(resolved_parent)
    except OSError as exc:
        raise WriteContainmentError(
            f"refusing to write: could not stat the resolved parent {resolved_parent}") from exc
    _reject_if_parent_inside_guard_roots(resolved_parent, parent_stat, guard_roots)
    # CHECK THE PATH THIS BRANCH ACTUALLY WRITES THROUGH, not the caller's spelling
    # (Codex challenge, finding F3). The write below lands at
    # `resolved_parent / out_path.name`; clearing `out_path` instead compared a DIFFERENT
    # pathname snapshot, so on a concurrent redirect the branch could clear one file and
    # replace another. The two derivations are identical on a stable filesystem, which is
    # why no static test distinguishes them -- that is a reason to make them the same
    # expression, not a reason to leave them different.
    written_path = resolved_parent / out_path.name
    _reject_if_target_is_an_input_path(written_path, input_paths)
    tmp_name = None
    try:
        file_fd, tmp_name = tempfile.mkstemp(dir=str(resolved_parent), suffix=".tmp")
        # closefd=False + one explicit close, same ownership rule as the dir_fd branch:
        # os.fdopen can raise before taking ownership of the raw fd. Both branches must
        # carry this or the leak simply moves to whichever one was left alone.
        try:
            with os.fdopen(file_fd, "w", encoding="utf-8", errors=encoding_errors,
                           closefd=False) as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(file_fd)
        os.replace(tmp_name, written_path)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return None


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


def _walk_contained_dirs(start, containment_root, containment_root_stat, out_of_root_refs, seen_refs,
                          inaccessible=None, blind_spots=None):
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
    of the PROVEN inode, never a re-resolved path.

    TRK-044 (AMENDMENTS A46), category 1 of 5 — three swallows fixed, all "inaccessible
    is NOT clean" cases: (1) an `os.open` failure has NOT yet computed a realpath, so it
    is never a containment fact — it used to be recorded via `_record_out_of_root_ref`,
    mislabeling a real permission failure (EACCES) as "out of root"; it is now recorded
    to `inaccessible[]`, the correct channel for "could not open this path" regardless of
    cause. (2) an `os.scandir(fd)` failure used to be a bare `continue` with zero
    disclosure anywhere — the yielded directory's subtree silently vanished from the walk
    with no record. (3) a per-entry `is_dir()` failure used to silently treat the entry as
    a non-directory, dropping its subtree without a record. Both (2) and (3) are now
    disclosed to `blind_spots[]` as a structural gap (a listing the walk could not
    complete), distinct from `inaccessible[]`'s per-path read failures. `inaccessible`/
    `blind_spots` default to `None` (internally treated as throwaway local lists) so a
    caller with no report to disclose into — the compose-mode watch-surface scratch walk
    — can omit them exactly as it already omits real out_of_root_refs/seen bookkeeping."""
    if inaccessible is None:
        inaccessible = []
    if blind_spots is None:
        blind_spots = []
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
            # Not yet a containment fact (no realpath has been derived) — an open
            # failure (EACCES, ENOENT on a dangling symlink, etc.) means "could not
            # open," never "resolved outside the root."
            _append_inaccessible_once(inaccessible, _rel_safe(containment_root, d))
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
                msg = (f"directory listing failed for {_rel_safe(containment_root, d)} "
                       f"— its subtree was not scanned")
                if msg not in blind_spots:
                    blind_spots.append(msg)
                continue
            for entry in entries:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    msg = (f"entry type undetermined for "
                           f"{_rel_safe(containment_root, d / entry.name)} — a possible "
                           f"subtree was not scanned")
                    if msg not in blind_spots:
                        blind_spots.append(msg)
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


class _HookCommandResolution(NamedTuple):
    """Outcome of resolving one hook `command` string (TRK-025 T1). `kind` classifies
    every one of `_script_from_command`'s branches for coverage counting, since a bare
    `script_path is None` used to conflate two very different situations:

    - `"resolved"` — a script token was derived (`script_path` is not `None`); the
      caller's own `stat()` later decides whether that target exists, is missing, or is
      unreadable — this classification is about tokenization, not disk state.
    - `"no_script"` — the command tokenizes fine but demonstrably references no script at
      all (e.g. inline shell emitting a terminal escape sequence). Fully examined, zero
      orphan risk, so the caller must NOT treat it as a blind spot.
    - `"unparsed"` — `shlex` could not tokenize the command, or an unrecognized command
      form still APPEARS to reference a script. A real coverage gap: the caller keeps it
      as a blind spot."""
    script_path: Path | None
    note: str | None
    kind: str


# TRK-025 P1 crash fix: a real hook command's inline-shell token can be ~1500 characters
# (an embedded awk/shell program, measured on the live harness). `Path.is_file()` only
# swallows ENOENT/ENOTDIR/EBADF/ELOOP (pathlib's own `_ignore_error`) — ENAMETOOLONG is
# NOT in that set and RE-RAISES OSError, which escaped `reconcile_hooks` uncaught and hit
# main()'s catch-all, turning the whole run into an all-zero crash envelope. A token this
# long cannot name a real hook script, so it is rejected before the syscall ever runs.
_MAX_SCRIPT_TOKEN_LEN = 255  # practical NAME_MAX


def _looks_like_existing_hook_script(hooks_dir, token):
    """Guarded `is_file()` probe (TRK-025 P1) — see `_MAX_SCRIPT_TOKEN_LEN` above for why
    the length check exists. The `except OSError` is kept regardless of the length guard:
    a read-only classifier must never be able to take the whole document down over a
    single unusual token (a hostile/garbled path, a filesystem-specific limit other than
    NAME_MAX, ...) — the length guard avoids the KNOWN failure mode cheaply, the guard
    below closes the class."""
    name = Path(token).name
    if len(name) > _MAX_SCRIPT_TOKEN_LEN:
        return False
    try:
        return (hooks_dir / name).is_file()
    except OSError:
        return False


def _references_script_token(tokens, root, profile):
    """Conservative scan (TRK-025 T1, Trap 2) used for an unrecognized command form: does
    ANY token look like it names a script? Deliberately does NOT use the bare `"/" in p`
    rule the recognized-form branches use below — measured against real inline-shell hook
    commands, that rule false-positives on shell redirection/tty tokens (`2>/dev/null`,
    `__tty=/dev/tty`, `>/dev/null`), which would silently reclassify every one of them as
    "references a script". A token counts here only when it carries a `.py`/`.sh` suffix,
    or names a file that actually exists under the profile's hooks dir. This check runs
    FIRST (see `_script_from_command`'s unrecognized-form branch) and unconditionally
    wins: a command that appears to reference a script stays `unparsed` regardless of
    what `_has_shell_control_syntax` below says (this is what keeps the Trap 2 guard,
    `caffeinate -i hooks/mystery.py`, a blind spot).

    Residual gap, documented rather than solved: an extensionless script (e.g. a
    relative `bin/track`) invoked from an unrecognized compound form and absent from the
    hooks dir will still classify as `no_script` here. That is narrower than the
    previous silent drop, but not perfect."""
    hooks_name = profile["container_dirs"]["hooks"]
    hooks_dir = (root / hooks_name) if hooks_name is not None else None
    return any(
        p.endswith((".py", ".sh")) or (hooks_dir is not None and _looks_like_existing_hook_script(hooks_dir, p))
        for p in tokens)


# TRK-025 T1 (corrected per team-lead review — a NAME allowlist swept up an opaque
# program like `rtk hook claude`, which could plausibly dispatch to a script, into the
# same bucket as self-evidently inline shell). This is a SYNTAX-based discriminator
# instead: does the RAW command contain shell control syntax at all? A `[ ... ] && ... `
# compound, a pipeline, a subshell/substitution, or a redirection is unambiguously being
# interpreted BY a shell rather than exec'd as a single external program — a bare
# `prog arg arg` invocation is not, and gives no positive evidence about what `prog`
# does internally. Bounded and bug-for-bug reproducible (unlike a name allowlist, which
# would need to grow forever): measured against the real 8 commands, every one contains
# at least one of these; `rtk hook claude` and `caffeinate -i hooks/mystery.py` contain
# none. Checked against the RAW `command` string, not the shlex tokens — tokenizing loses
# quote structure entirely, and quote structure is exactly what decides whether an
# operator is evidence of shell interpretation (TRK-056, A62): a real `/bin/sh` hands a
# quoted operator to the exec'd program as a single literal argument, never as syntax —
# verified directly, `/bin/sh` passes `rtk hook "a && z"` the one literal argument
# `a && z` — so a hit found only INSIDE quotes must NOT count. The one exception is `$(`
# and a backtick, which still expand INSIDE DOUBLE quotes (command substitution runs
# there), so those two keep counting even when double-quoted. `_has_shell_control_syntax`
# implements this with a single left-to-right quote-tracking scan; see its docstring for
# the exact rule.
#
# This is a bounded detector rule, not a claim of POSIX completeness — per
# codex-learnings C10 the detector does not model the shell, and the following remain
# known residuals of the A45 heuristic, documented here rather than fixed: a bare `&`
# (background), a literal newline, bare `(`/`)` (a subshell with no `$`), `${...}`
# parameter expansion, `$((...))` arithmetic expansion, comment-only input (`# ...`), and
# assignment-only input (`FOO=bar cmd`) are not scanned for at all, quoted or not. Bare
# `{`/`}` are scanned for OUTSIDE quotes only — the quote-aware scan reaches them like any
# other token, so `rtk hook {foo}` hits while `rtk hook "{foo}"` and `rtk hook '{foo}'` do
# not (verified by direct call) — and can flag a literal `{`/`}` character that is not
# functioning as brace-expansion syntax — narrowing that would mean
# editing the `bracket_commands`/`long_single_token` shapes `pathological_harness` already
# pins, which CLAUDE.md's assertion rule forbids, so that residual stays open too.
_SHELL_CONTROL_SYNTAX = frozenset(("$(", "&&", "||", "|", ";", "<", ">", "{", "}", "`"))


def _has_shell_control_syntax(command):
    """True when a real shell would see one of `_SHELL_CONTROL_SYNTAX`'s tokens as an
    OPERATOR — not as literal text inside a quoted argument. See that constant's comment
    for the reasoning and the residual gaps this does NOT close (TRK-056, A62).

    A single left-to-right pass tracks quote state. This is a bounded heuristic, not a
    shell lexer — no backslash-newline, no `${...}` nesting, no `$'...'`, no here-docs;
    see the residual-gap comment above `_SHELL_CONTROL_SYNTAX`. The quoting rule:

    - Inside single quotes: everything is literal, backslash included. No operator
      counts, and a single quote cannot be escaped from within itself.
    - Inside double quotes: `$(` and a backtick still count (command substitution still
      expands there); every other operator does not.
    - Backslash escapes the very next character everywhere EXCEPT inside single quotes —
      that includes inside double quotes, so `"\\$(...)"` and `` "\\`...\\`" `` do NOT
      count. POSIX only special-cases backslash before `$`, backtick, `"`, `\\`, and
      newline inside double quotes; treating every character there as escapable is a
      deliberate simplification of POSIX, outcome-neutral for this operator set, since
      `$(` and backtick are the only two tokens that count inside double quotes anyway.
    - Unterminated quote: unreachable in production (`_script_from_command`'s
      `shlex.split` call raises `ValueError` and returns "unparsed" before this function
      is ever reached), but chosen deliberately rather than left an accident — the scan
      runs off the end of the string in whatever quote state it is in and returns False.
    """
    in_single = False
    in_double = False
    i = 0
    length = len(command)
    while i < length:
        ch = command[i]
        if in_single:
            if ch == "'":
                in_single = False
            i += 1
            continue
        if ch == "\\":
            i += 2  # escapes the next character everywhere except inside single quotes
            continue
        if in_double:
            if ch == '"':
                in_double = False
            elif ch == "`" or command.startswith("$(", i):
                return True
            i += 1
            continue
        if ch == "'":
            in_single = True
            i += 1
            continue
        if ch == '"':
            in_double = True
            i += 1
            continue
        if any(command.startswith(tok, i) for tok in _SHELL_CONTROL_SYNTAX):
            return True
        i += 1
    return False


def _script_from_command(command, root, *, profile: dict[str, Any] | None = None) -> _HookCommandResolution:
    """Resolve one hook `command` string to a `_HookCommandResolution`. `note` is set
    whenever the command is not a clean "resolved" (so the caller never has to infer a
    reason from a bare `None`); whether that note becomes a visible blind spot is the
    caller's call, keyed on `kind` (see `_HookCommandResolution`).

    M11 (SPEC_7 §2): the `~`-prefixed remap below tries each configured
    profile["hook_command_remaps"] pair in order, first match wins — reproducing the
    single "~/.claude/hooks" -> root/"hooks" remap byte-for-byte on the default profile."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    try:
        tokens = shlex.split(command)
    except ValueError:
        return _HookCommandResolution(None, f"unparseable hook command: {command[:80]}", "unparsed")
    if not tokens:
        # TRK-025 T1: this used to be a SILENT (None, None) — closing that silent path,
        # an empty command tokenizes fine and references nothing, so it is benign
        # (no_script), but the caller still gets a note explaining why.
        return _HookCommandResolution(None, "empty hook command", "no_script")
    first = Path(tokens[0]).name
    if first == "env":
        rest = tokens[2:]
    elif first in _SCRIPT_INTERPRETERS:
        rest = tokens[1:]
    elif "/" in tokens[0] or tokens[0].endswith((".py", ".sh")):
        rest = tokens
    else:
        # TRK-025 T1: an unrecognized first token is NOT automatically a coverage gap,
        # but a NAME-based judgment about it is a mistake (a real coding-team review
        # caught this: `rtk hook claude` is an opaque program that could plausibly
        # dispatch to a script, and must NOT be waved through as `no_script` just
        # because it looks like `printf`-style inline shell). Three-way, in order:
        if _references_script_token(tokens, root, profile):
            # 1. Genuinely appears to reference a script (Trap 2 guard) -> unparsed,
            #    regardless of shell syntax.
            return _HookCommandResolution(
                None, f"unsupported hook command form: {command[:80]}", "unparsed")
        if _has_shell_control_syntax(command):
            # 2. No script reference, but the RAW command is unambiguously being
            #    interpreted BY a shell (a `[ ... ] && ...` compound, a pipeline, a
            #    subshell/substitution, a redirection) -> no_script, fully examined.
            return _HookCommandResolution(None, None, "no_script")
        # 3. A bare `prog arg arg` invocation with no shell syntax and no script
        #    reference -- an opaque program we have no positive evidence about (e.g.
        #    `rtk hook claude`) -> unparsed, a real disclosed blind spot.
        return _HookCommandResolution(
            None, f"unsupported hook command form: {command[:80]}", "unparsed")
    token = next((p for p in rest if "/" in p or p.endswith((".py", ".sh"))), None)
    if token is None:
        return _HookCommandResolution(
            None, f"no script token in hook command: {command[:80]}", "no_script")
    raw = Path(token)
    if str(raw).startswith("~"):
        # Registered commands literally read "~/.claude/hooks/...": remap that literal
        # ~-path onto `root / "hooks"` (not the real home dir) so a non-default --root
        # (and every fixture in these tests) reconciles against the actual registered
        # hook path instead of the real, unrelated $HOME.
        expanded = raw.expanduser()
        for literal_prefix, rel_dir in profile["hook_command_remaps"]:
            try:
                return _HookCommandResolution(
                    (root / rel_dir) / expanded.relative_to(Path(literal_prefix).expanduser()),
                    None, "resolved")
            except ValueError:
                continue
        return _HookCommandResolution(expanded, None, "resolved")
    if raw.is_absolute():
        return _HookCommandResolution(raw, None, "resolved")
    # A relative directly-executable token (e.g. "./hooks/x.py") resolves against --root,
    # NEVER against the process's cwd (R6) — joining (not .resolve()) avoids symlink surprises.
    return _HookCommandResolution((root / raw), None, "resolved")


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


def _walk_project_tier(project_root, inaccessible, errors, out_of_root_refs, blind_spots=None):
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
    Both are recorded as `out_of_root_refs` (name + target, untrusted) instead.

    TRK-044 (AMENDMENTS A46): `inaccessible` (already a param) and the new optional
    `blind_spots` are threaded into `_walk_contained_dirs` so a permission failure or a
    listing gap encountered during the walk itself is disclosed rather than silently
    dropped or mislabeled as an out-of-root escape."""
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
                                       out_of_root_refs, seen_refs,
                                       inaccessible=inaccessible, blind_spots=blind_spots):
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
        is_rules_dir = _probe_is_dir(rules_dir)
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
            else:
                _disclose_unlistable_glob(rules_dir, "*.md", rule_files, errors,
                                           "project rules")
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

def _walk_operator_tier_nodes(root, inaccessible=None, *, profile: dict[str, Any] | None = None):
    """Operator-tier skill/agent/command nodes (T4). A lean single-level existence walk
    (no body read — the node model needs only the collision key + path for the shadow
    resolver; word/line metrics stay owned by collect_descriptions/collect_on_demand).
    Commands get their FIRST node collection here — no prior section inventoried
    commands/*.md as nodes at all. Operator tier keeps its existing trusted
    symlink-following (unchanged, matches every other operator-tier walk).

    `inaccessible` (S7): the shared build_document inaccessible[] list. `_probe_is_dir`
    re-raises EACCES from an unreadable ancestor, so a locked skills/ or agents/ dir used
    to abort this walk and — via build_document, which has no per-section handler — replace
    the whole document with a crash envelope. Each surface dir that cannot be probed is now
    recorded here and skipped, so an unreadable surface is DISCLOSED rather than silently
    absent from a report that would otherwise read as clean.

    M11 (SPEC_7 §2): dir names come from profile["container_dirs"], the manifest name from
    profile["skill_manifest_name"]; a None role means this layout has no such surface, and
    the corresponding block is skipped."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    container_dirs = profile["container_dirs"]
    nodes: list[dict[str, Any]] = []
    if inaccessible is None:
        inaccessible = []
    skills_dir_name = container_dirs["skills"]
    skill_manifest_name = profile["skill_manifest_name"]
    skills_dir_is_dir = False
    if skills_dir_name is not None:
        skills_dir = root / skills_dir_name
        try:
            skills_dir_is_dir = _probe_is_dir(skills_dir)
        except OSError:
            _append_inaccessible_once(inaccessible, _rel_safe(root, skills_dir))
            skills_dir_is_dir = False
    if skills_dir_is_dir and skill_manifest_name is not None:
        try:
            skill_entries = sorted(skills_dir.iterdir())
        except OSError:
            _append_inaccessible_once(inaccessible, _rel_safe(root, skills_dir))
            skill_entries = []
        skill_dirs = []
        for p in skill_entries:
            try:
                if p.is_dir():
                    skill_dirs.append(p)
            except OSError:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T1).
                _append_inaccessible_once(inaccessible, _rel_safe(root, p))
        for skill_dir in skill_dirs:
            skill_md = skill_dir / skill_manifest_name
            present, ok = _safe_exists(skill_md)
            if ok and present:
                nodes.append({"surface": "skill", "name": skill_dir.name,
                              "tier": "operator", "path": _rel(root, skill_md)})
    for surface, role in (("agent", "agents"), ("command", "commands")):
        dirname = container_dirs[role]
        if dirname is None:
            continue
        d = root / dirname
        try:
            d_is_dir = _probe_is_dir(d)
        except OSError:
            _append_inaccessible_once(inaccessible, _rel_safe(root, d))
            continue
        if not d_is_dir:
            continue
        try:
            files = sorted(d.glob("*.md"))
        except OSError:
            files = []
        else:
            glob_errors: list[str] = []
            _disclose_unlistable_glob(d, "*.md", files, glob_errors, "operator tier nodes")
            if glob_errors:
                _append_inaccessible_once(inaccessible, _rel_safe(root, d))
        for f in files:
            nodes.append({"surface": surface, "name": f.stem, "tier": "operator",
                          "path": _rel(root, f)})
    return nodes


def _walk_project_tier_nodes(project_root, out_of_root_refs, errors):
    """Project-tier skill/agent/command nodes (T4): `<repo>/.claude/{skills,agents,
    commands}/`. Existence + identity ONLY (same rationale as
    `_walk_operator_tier_nodes` — no body read needed for the collision-key model).
    EVERY path (surface dir, skill dir, leaf file) is routed through T3's
    `_project_tier_gate` (H2) — an escaping symlink at any level is recorded as an
    `out_of_root_ref` and excluded from the node list, mirroring `_walk_project_tier`'s
    rules-dir handling exactly (reused, not reimplemented).

    `errors` (S7): same disclosure channel `_walk_project_tier` already uses for the
    IDENTICAL `os.stat(project_root)`/`is_dir()` failures (its containment-root and
    rules-dir checks) — matched here rather than `_walk_operator_tier_nodes`'
    `inaccessible` so project-tier disclosure stays on one channel. An unreadable
    containment root or surface dir used to yield a silently truncated (or empty) node
    list with zero record anywhere; inaccessible is NOT clean."""
    project_root = Path(project_root)
    harness_root = project_root / ".claude"
    nodes: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    try:
        containment_stat = os.stat(project_root)
    except OSError as e:
        errors.append(f"project containment root not accessible: {project_root}: {e}")
        return nodes

    skills_dir = harness_root / "skills"
    try:
        is_skills_dir = _probe_is_dir(skills_dir)
    except OSError as e:
        errors.append(f"project skills is_dir failed for {skills_dir}: {e}")
        is_skills_dir = False
    if is_skills_dir:
        contained, _identity = _project_tier_gate(skills_dir, project_root, containment_stat)
        if not contained:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, skills_dir)
        else:
            try:
                skill_entries = sorted(skills_dir.iterdir())
            except OSError as e:
                errors.append(f"project skills listing failed for {skills_dir}: {e}")
                skill_entries = []
            skill_dirs = []
            for p in skill_entries:
                try:
                    if p.is_dir():
                        skill_dirs.append(p)
                except OSError as e:
                    # A single unlistable/unstat-able child must not abort the whole
                    # comprehension and discard every sibling with it (TRK-050 T1).
                    errors.append(f"project skills child is_dir failed for {p}: {e}")
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
            is_dir = _probe_is_dir(d)
        except OSError as e:
            errors.append(f"project {dirname} is_dir failed for {d}: {e}")
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
        else:
            _disclose_unlistable_glob(d, "*.md", files, errors, "project tier nodes")
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


# tier-precedence: live-verified 2026-08-07 against real `claude -p` sessions (CC
# 2.1.224, macOS) — all six surfaces matched this table, no code change resulted
# (AMENDMENTS A63). Skills/commands: operator SHADOWS project (operator wins a name
# collision). Agents: project SHADOWS user — the INVERSE of skills, now measured rather
# than inferred. Rules/CLAUDE files/hooks: UNION (both tiers load, no winner). This
# resolver keys the collision winner OFF THE SURFACE — it is not one global rule; getting
# a surface backwards inverts the "overrides M" headline count. Scope: this establishes
# what a real session resolved to on 2.1.224, not documented intent, and not stability
# across versions.
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
    *,
    profile: dict[str, Any] | None = None,
    blind_spots: list[str] | None = None,
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
    when `compose=True` and `project_root` is set.

    M11 (SPEC_7 §2): every fixed path below is sourced from `profile` (defaulting to
    PROFILE_CLAUDE_CODE); a None role means this layout has no such surface, and the
    corresponding block is skipped — never `root / None`.

    TRK-044 (AMENDMENTS A46): `blind_spots`, optional and defaulting to `None`, is passed
    through to `_walk_project_tier`'s own `_walk_contained_dirs` walk (compose mode only)
    so a listing gap encountered during that walk is disclosed; omitted by every existing
    caller that has no `blind_spots` list of its own, matching this function's existing
    `out_of_root_refs: list[Any] | None = None` convention."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    top_level_files = profile["top_level_files"]
    container_dirs = profile["container_dirs"]
    root_instructions_name = top_level_files["root_instructions"]
    files = []
    conditional_variants = []
    # A file reachable via multiple glob paths (a rules/ deploy symlink pointing at its
    # skills/coding-team/rules/ submodule source) is ONE physical file and must be counted
    # ONCE. `seen` covers every append to `files` below, in append order — so a symlinked
    # rule is counted under its deployed/always-loaded location (rules/, seen first) and the
    # submodule-source duplicate is skipped. `conditional_variants` is NOT deduped against
    # this set: those are different projects' distinct MEMORY.md files, not glob duplicates.
    seen = set()

    if root_instructions_name is not None:
        root_claude = root / root_instructions_name
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
    projects_dir_name = container_dirs["projects"]
    memory_dir_name = container_dirs["memory"]
    memory_index_name = profile["memory_index_name"]
    if project_root is not None:
        active_slug = _project_slug(project_root)
        # Only count this project's CLAUDE.md via THIS legacy branch if the project is
        # registered under this harness root's projects/<slug>/memory/ (unregistered
        # --project-root defaulting to an unrelated cwd must not leak an unrelated
        # CLAUDE.md), AND compose is off — compose mode emits the project CLAUDE.md via
        # _walk_project_tier below instead, unconditionally on registration (H1: the two
        # paths must never BOTH fire for the same physical file, or it double-counts).
        # `_probe_is_dir` re-raises EACCES from an unreadable ancestor (it swallows only the
        # ENOENT family) — an escape here aborts walk_always_loaded and, via build_document,
        # replaces the ENTIRE report with a crash envelope. Record and treat as absent.
        legacy_memory_present = False
        if projects_dir_name is not None and memory_dir_name is not None:
            legacy_memory_dir = root / projects_dir_name / active_slug / memory_dir_name
            try:
                legacy_memory_present = _probe_is_dir(legacy_memory_dir)
            except OSError as e:
                errors.append(f"projects memory is_dir failed for {legacy_memory_dir}: {e}")
                legacy_memory_present = False
        if not compose and legacy_memory_present and root_instructions_name is not None:
            proj_claude = Path(project_root) / root_instructions_name
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
    #
    # M11 exit gate, Codex round, Finding 3 (P2): `projects_glob` (defaulting to
    # "projects/*") is what actually GATES and NARROWS which projects/<slug> dirs count as
    # registered here -- `container_dirs["projects"]` alone (used above only to locate the
    # container dir and, unchanged, still recorded as an errors[]-worthy is_dir() probe)
    # used to be the only thing driving this walk, leaving `projects_glob` entirely inert.
    # `None` means "no project discovery in this layout" ([DECISION] SPEC_7 §2): slug_dirs
    # stays empty. A non-null glob NARROWS the existing dir-filtered candidate set to those
    # ALSO matching the pattern (`root.glob(pattern)`, intersected — never substituted for
    # the candidate set outright, since `.glob()` returns files too and the existing
    # is_dir()-filtered `iterdir()` enumeration must stay the single source of "is this a
    # directory" truth). The default "projects/*" intersects to the SAME set in the SAME
    # order as the pre-M11 walk, so default output stays byte-identical.
    projects_dir_is_dir = False
    projects_dir: Path | None = None
    if projects_dir_name is not None:
        projects_dir = root / projects_dir_name
        try:
            projects_dir_is_dir = _probe_is_dir(projects_dir)
        except OSError as e:
            errors.append(f"projects is_dir failed for {projects_dir}: {e}")
            projects_dir_is_dir = False
    projects_glob_pattern = profile["projects_glob"]
    slug_dirs: list[Path] = []
    if projects_dir_is_dir and projects_dir is not None and projects_glob_pattern is not None:
        try:
            project_entries = sorted(projects_dir.iterdir())
        except OSError as e:
            errors.append(f"projects listing failed for {projects_dir}: {e}")
            project_entries = []
        candidate_dirs = []
        for p in project_entries:
            try:
                if p.is_dir():
                    candidate_dirs.append(p)
            except OSError as e:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T1).
                errors.append(f"projects child is_dir failed for {p}: {e}")
        try:
            glob_matches = set(root.glob(projects_glob_pattern))
        except OSError:
            glob_matches = set()
        else:
            _disclose_unlistable_glob(root, projects_glob_pattern, glob_matches, errors,
                                       "always-loaded projects")
        slug_dirs = [p for p in candidate_dirs if p in glob_matches]
    for slug_dir in slug_dirs:
        if memory_dir_name is None or memory_index_name is None:
            continue
        idx = slug_dir / memory_dir_name / memory_index_name
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
    memory_index_rel = top_level_files["memory_index"]
    if memory_index_rel is not None:
        stub = root / memory_index_rel
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
    rules_dir_name = container_dirs["rules"]
    rule_dirs = [(root / rules_dir_name, "rule")] if rules_dir_name is not None else []
    skills_dir_name = container_dirs["skills"]
    # <skills>/*/<sub-rules>/*.md's middle segment names each sub-skill's own rules dir —
    # matched against `skills_dir_name` (container_dirs["skills"]), never the literal
    # "skills" (M11 exit gate, Codex round, Finding 4: an earlier version of this comment
    # claimed the match was "derived from rules_globs (never a second literal)", which was
    # false -- "skills" WAS a second literal, so a profile renaming container_dirs["skills"]
    # (e.g. to "abilities") silently lost every nested rules_globs entry shaped like
    # "<renamed>/*/<sub>/*.md" from this walk, with no disclosure at all). `skills_dir_name`
    # being None (no skills concept) makes this comparison never match any glob segment,
    # same as before.
    sub_rules_dir_name = None
    for g in profile["rules_globs"]:
        segs = g.split("/")
        if len(segs) == 4 and segs[0] == skills_dir_name and segs[1] == "*" and segs[3] == "*.md":
            sub_rules_dir_name = segs[2]
            break
    skills_root_is_dir = False
    skills_root: Path | None = None
    if skills_dir_name is not None:
        skills_root = root / skills_dir_name
        try:
            skills_root_is_dir = _probe_is_dir(skills_root)
        except OSError as e:
            errors.append(f"skills is_dir failed for {skills_root}: {e}")
            skills_root_is_dir = False
    if skills_root_is_dir and sub_rules_dir_name is not None and skills_root is not None:
        try:
            sub_skill_entries = sorted(skills_root.iterdir())
        except OSError as e:
            errors.append(f"skills iterdir failed for {skills_root}: {e}")
            sub_skill_entries = []
        sub_skill_dirs = []
        for p in sub_skill_entries:
            try:
                if p.is_dir():
                    sub_skill_dirs.append(p)
            except OSError as e:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T1).
                # TRK-050 T5 F2: scan-named prefix so this always-loaded sub-rules scan's
                # entry is distinguishable from the byte-identical messages
                # _hook_test_stems and _detect_skill_test_coverage independently emit for
                # the SAME skills/ dir -- one unreadable skill child used to produce three
                # indistinguishable errors[] entries.
                errors.append(f"always-loaded skills child is_dir failed for {p}: {e}")
        for skill_dir in sub_skill_dirs:
            sub_rules = skill_dir / sub_rules_dir_name
            try:
                is_rules_dir = _probe_is_dir(sub_rules)
            except OSError as e:
                errors.append(f"rules is_dir check failed for {sub_rules}: {e}")
                continue
            if is_rules_dir:
                category = "coding_team_rule" if skill_dir.name == "coding-team" else "skill_rule"
                rule_dirs.append((sub_rules, category))
    for rules_dir, category in rule_dirs:
        try:
            if not _probe_is_dir(rules_dir):
                continue
        except OSError as e:
            errors.append(f"rules is_dir failed for {rules_dir}: {e}")
            continue
        try:
            names = sorted(rules_dir.glob("*.md"))
        except OSError as e:
            errors.append(f"rules glob failed for {rules_dir}: {e}")
            continue
        else:
            _disclose_unlistable_glob(rules_dir, "*.md", names, errors, "always-loaded rules")
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
                                         out_of_root_refs if out_of_root_refs is not None else [],
                                         blind_spots=blind_spots))

    return files, conditional_variants


def collect_descriptions(
    root: Path, inaccessible: list[dict[str, Any]], *,
    profile: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect skill/agent front-matter `description` word counts.

    M11 (SPEC_7 §2): dir names come from profile["container_dirs"], the manifest name from
    profile["skill_manifest_name"] (defaulting to PROFILE_CLAUDE_CODE); a None role means
    this layout has no such surface, and the corresponding block is skipped."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    container_dirs = profile["container_dirs"]
    skill_manifest_name = profile["skill_manifest_name"]
    skill_descriptions = []
    agent_descriptions = []

    # Deliberately single-level: iterdir()/glob("*.md") only, no recursion — so there is no
    # walk to follow symlinks through. A symlinked skill DIR is followed and reported under
    # its harness-relative name by design.
    skills_dir_name = container_dirs["skills"]
    skills_dir_is_dir = False
    if skills_dir_name is not None:
        skills_dir = root / skills_dir_name
        try:
            skills_dir_is_dir = _probe_is_dir(skills_dir)
        except OSError:
            _append_inaccessible_once(inaccessible, _rel_safe(root, skills_dir))
            skills_dir_is_dir = False
    if skills_dir_is_dir and skill_manifest_name is not None:
        try:
            skill_entries = sorted(skills_dir.iterdir())
        except OSError:
            _append_inaccessible_once(inaccessible, _rel_safe(root, skills_dir))
            skill_entries = []
        skill_dirs = []
        for p in skill_entries:
            try:
                if p.is_dir():
                    skill_dirs.append(p)
            except OSError:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T1).
                _append_inaccessible_once(inaccessible, _rel_safe(root, p))
        for skill_dir in skill_dirs:
            skill_md = skill_dir / skill_manifest_name
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

    agents_dir_name = container_dirs["agents"]
    agents_dir_is_dir = False
    if agents_dir_name is not None:
        agents_dir = root / agents_dir_name
        try:
            agents_dir_is_dir = _probe_is_dir(agents_dir)
        except OSError:
            _append_inaccessible_once(inaccessible, _rel_safe(root, agents_dir))
            agents_dir_is_dir = False
    if agents_dir_is_dir:
        try:
            agent_files = sorted(agents_dir.glob("*.md"))
        except OSError:
            agent_files = []
        else:
            glob_errors: list[str] = []
            _disclose_unlistable_glob(agents_dir, "*.md", agent_files, glob_errors,
                                       "descriptions agents")
            if glob_errors:
                _append_inaccessible_once(inaccessible, _rel_safe(root, agents_dir))
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
    root: Path, project_root: Path | None, inaccessible: list[dict[str, Any]], *,
    profile: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect on-demand bodies: skill SKILL.md bodies, skill-internal phases/prompts/agents,
    and the active project's memory bodies (excluding the always-loaded MEMORY.md index).

    M11 (SPEC_7 §2): dir/file names come from `profile` (defaulting to PROFILE_CLAUDE_CODE);
    a None role means this layout has no such surface, and the corresponding block is
    skipped. The phases/prompts/agents skill-internal subdir tuple stays literal, deferred."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    container_dirs = profile["container_dirs"]
    skill_manifest_name = profile["skill_manifest_name"]
    skills = []
    skill_internal_bodies = []
    memory_bodies = []

    # Deliberately single-level: iterdir()/glob("*.md") only, no recursion — so there is no
    # walk to follow symlinks through. A symlinked skill DIR is followed and reported under
    # its harness-relative name by design.
    skills_dir_name = container_dirs["skills"]
    skills_dir_is_dir = False
    if skills_dir_name is not None:
        skills_dir = root / skills_dir_name
        try:
            skills_dir_is_dir = _probe_is_dir(skills_dir)
        except OSError:
            _append_inaccessible_once(inaccessible, _rel_safe(root, skills_dir))
            skills_dir_is_dir = False
    if skills_dir_is_dir and skill_manifest_name is not None:
        try:
            skill_entries = sorted(skills_dir.iterdir())
        except OSError:
            _append_inaccessible_once(inaccessible, _rel_safe(root, skills_dir))
            skill_entries = []
        skill_dirs = []
        for p in skill_entries:
            try:
                if p.is_dir():
                    skill_dirs.append(p)
            except OSError:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T1).
                _append_inaccessible_once(inaccessible, _rel_safe(root, p))
        for skill_dir in skill_dirs:
            name = skill_dir.name
            skill_md = skill_dir / skill_manifest_name
            present, ok = _safe_exists(skill_md)
            if not ok:
                inaccessible.append({"path": _rel(root, skill_md), "reason": "unreadable"})
                continue
            tests_dir = skill_dir / "tests"
            try:
                has_test = _probe_is_dir(tests_dir)
            except OSError:
                _append_inaccessible_once(inaccessible, _rel_safe(root, tests_dir))
                has_test = False
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
                try:
                    target_is_dir = _probe_is_dir(target)
                except OSError:
                    _append_inaccessible_once(inaccessible, _rel_safe(root, target))
                    continue
                if not target_is_dir:
                    continue
                try:
                    body_files = sorted(target.glob("*.md"))
                except OSError:
                    body_files = []
                else:
                    glob_errors: list[str] = []
                    _disclose_unlistable_glob(target, "*.md", body_files, glob_errors,
                                               "on-demand skill body")
                    if glob_errors:
                        _append_inaccessible_once(inaccessible, _rel_safe(root, target))
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
        projects_dir_name = container_dirs["projects"]
        memory_dir_name = container_dirs["memory"]
        mem_dir_is_dir = False
        if projects_dir_name is not None and memory_dir_name is not None:
            mem_dir = root / projects_dir_name / active_slug / memory_dir_name
            try:
                mem_dir_is_dir = _probe_is_dir(mem_dir)
            except OSError:
                _append_inaccessible_once(inaccessible, _rel_safe(root, mem_dir))
                mem_dir_is_dir = False
        if mem_dir_is_dir:
            try:
                mem_files = sorted(mem_dir.glob("*.md"))
            except OSError:
                mem_files = []
            else:
                glob_errors = []
                _disclose_unlistable_glob(mem_dir, "*.md", mem_files, glob_errors,
                                           "on-demand project memory")
                if glob_errors:
                    _append_inaccessible_once(inaccessible, _rel_safe(root, mem_dir))
            for f in mem_files:
                if f.name == profile["memory_index_name"]:
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
    root: Path, errors: list[str], blind_spots: list[str], *,
    profile: dict[str, Any] | None = None,
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

      M11 (SPEC_7 §2): `profile` (defaulting to PROFILE_CLAUDE_CODE) gates this entirely —
      settings_format == "none" or top_level_files["settings"] is None means this layout
      has no settings file this collector can parse, so the file is never even looked at.
      Short-circuits to ({}, False) with a blind_spots note, NOT an errors[] entry (a
      profile declaring no settings surface is not an anomaly).
      Returns (settings_dict, parsed_ok)."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    settings_name = profile["top_level_files"]["settings"]
    if profile["settings_format"] == "none" or settings_name is None:
        blind_spots.append(
            f"profile '{profile['name']}' declares settings_format=none; permissions, "
            "config and hook REGISTRATIONS are not collected (hook SCRIPTS on disk still "
            "are).")
        return {}, False
    settings_path = root / settings_name
    try:
        is_regular_file = _probe_is_file(settings_path)   # follows symlinks; False for FIFO/socket/dir/broken-symlink/absent
    except OSError as e:
        # The probe itself can raise EACCES from an unsearchable ancestor directory,
        # not just report False — LOUD per this function's own "unreadable-as-a-regular
        # file" branch, same shape as the read-failure case below.
        errors.append(f"settings.json is_file() check failed: {e!r}")
        return {}, False
    if not is_regular_file:
        try:
            present = _probe_exists(settings_path)   # follows symlinks; True for a symlink to a FIFO/socket/dir
        except OSError:
            present = True   # stat failed unexpectedly; treat conservatively as present-anomaly
        if present:
            errors.append("settings.json exists but is not a regular file (FIFO/socket/directory); "
                           "refusing to open it to avoid blocking on a special file.")
            return {}, False
        try:
            is_broken_symlink = _probe_is_symlink(settings_path)   # not-present + is-a-link == a broken (dangling) symlink
        except OSError as e:
            errors.append(f"settings.json is_symlink() check failed: {e!r}")
            return {}, False
        if is_broken_symlink:
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
    root: Path, settings: dict[str, Any], parsed_ok: bool, blind_spots: list[str], *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # M11 (SPEC_7 §2): settings_format == "none" mirrors parse_settings' own short-circuit
    # — a profile that declares no settings surface has no plugin registry to read either
    # (both live under the same operator-config concept this collector treats as one
    # surface), so this returns the SAME 12-key shape as the normal path, empty/zero
    # throughout, evidence INACCESSIBLE (parsed_ok would already be False here, but this
    # short-circuits BEFORE the plugin reads below rather than relying on that).
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    if profile["settings_format"] == "none":
        return {
            "env_keys": [], "env_key_count": 0, "model": None, "cleanup_period_days": 0,
            "sandbox": False, "enabled_plugins": [], "plugin_count": 0,
            "marketplaces": [], "marketplace_count": 0,
            "installed_plugins": [], "installed_plugin_count": 0,
            "evidence": "INACCESSIBLE",
        }

    # secret-leak guard: never serialize env values — env_keys is names ONLY.
    env = settings.get("env", {})
    env_keys = sorted(env.keys()) if isinstance(env, dict) else []

    enabled_plugins_raw = settings.get("enabledPlugins", {})
    enabled_plugins = ([{"name": k, "enabled": bool(v)} for k, v in enabled_plugins_raw.items()]
                        if isinstance(enabled_plugins_raw, dict) else [])

    # Orthogonal to the settings_format short-circuit above: a claude-code-format profile
    # may still declare a null plugin_marketplaces/plugin_installed role (no such surface
    # in this layout), independent of whether settings.json itself parsed.
    marketplaces_name = profile["top_level_files"]["plugin_marketplaces"]
    installed_name = profile["top_level_files"]["plugin_installed"]
    marketplaces, marketplace_count = (
        _read_json_name_list(root / marketplaces_name, "marketplaces", blind_spots)
        if marketplaces_name is not None else ([], 0))
    installed_plugins, installed_plugin_count = (
        _read_json_name_list(root / installed_name, "installed", blind_spots)
        if installed_name is not None else ([], 0))

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


def _hook_disk_files(root, *, profile: dict[str, Any] | None = None,
                      errors: list[str] | None = None):
    """hooks/*.py + hooks/*.sh on disk, name-sorted. MEASURED 2026-08-06 on CPython
    3.11.14 against a real 0o000 hooks dir: Path.glob() raises nothing and returns []
    — an unreadable hooks/ dir is indistinguishable here from an absent or genuinely
    empty one, so "never raising" is a blind spot, not a guarantee: a locked-out hooks
    dir is silently reported as a clean empty result to both downstream callers.
    Deliberately single-level: no recursion, so there is no walk to follow
    symlinks through — a symlinked hook FILE is included by name. Shared by
    reconcile_hooks and _detect_hook_test_coverage, which both need the identical
    guarded + sorted listing before diverging into their own downstream logic.

    M11 (SPEC_7 §2): the hooks dir and its script extensions come from `profile`
    (defaulting to PROFILE_CLAUDE_CODE) — `hook_script_globs` entries are glob patterns
    ROOTED at container_dirs["hooks"] (e.g. "hooks/*.py"), so only their basename
    ("*.py") is re-applied against the profile's hooks dir.

    `errors` (TRK-082 T2, optional): no build_document channel is in scope at this call
    depth (both callers -- reconcile_hooks and _detect_hook_test_coverage -- have their
    own errors[] but neither currently threads it in here), so this is a bare optional
    sink following the `_skill_has_test_asset(errors=None)` precedent -- defaulting to
    None keeps every existing call site byte-identical. Wiring it into a caller is a
    separate change (TRK-086), deliberately not done here."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    hooks_name = profile["container_dirs"]["hooks"]
    if hooks_name is None:
        return []
    hooks_dir = root / hooks_name
    try:
        disk_files = []
        for pattern in profile["hook_script_globs"]:
            glob_pattern = Path(pattern).name
            pattern_matches = list(hooks_dir.glob(glob_pattern))
            disk_files.extend(pattern_matches)
            if errors is not None:
                _disclose_unlistable_glob(hooks_dir, glob_pattern, pattern_matches, errors,
                                           "hook disk files")
        disk_files = sorted(disk_files, key=lambda p: p.name)
    except OSError:
        return []
    return disk_files


def reconcile_hooks(
    root: Path,
    settings: dict[str, Any],
    inaccessible: list[dict[str, Any]],
    blind_spots: list[str],
    *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatcher-aware reconciliation: resolve every hook `command` registered in
    settings.json against hooks/ on disk, then fan reachability through any registered
    *-dispatcher.py's string-literal CHECKS-style list. Registration evidence (the
    settings.json line was read) and target status (stat() of the resolved script) are
    always kept as distinct facts — see schema.md Note 3.

    M11 (SPEC_7 §2): `profile` (defaulting to PROFILE_CLAUDE_CODE) supplies the dispatcher
    filename suffix and is forwarded to _script_from_command/_hook_disk_files so a
    non-default profile's hooks dir and remaps are used consistently throughout."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    registered = []
    orphan_registrations = []
    direct_registered_names = set()
    commands_resolved = commands_no_script = commands_unparsed = 0

    for command in _iter_hook_commands(settings):
        script_path, note, kind = _script_from_command(command, root, profile=profile)
        if kind == "resolved":
            commands_resolved += 1
        elif kind == "no_script":
            commands_no_script += 1
        else:
            # TRK-025 [DECISION]: only a command we genuinely could not read reduces
            # coverage — a "no_script" note (e.g. an inline shell command that tokenizes
            # fine but references nothing) is fully examined and must never appear here.
            commands_unparsed += 1
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

    disk_files = _hook_disk_files(root, profile=profile)
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

    # M11 (SPEC_7 §2): profile["dispatcher_suffix"], defaulting to "-dispatcher.py"; None ->
    # this layout has no dispatcher concept, so no dispatcher fans out reachability at all.
    dispatcher_suffix = profile["dispatcher_suffix"]
    dispatcher_reached_names = set()
    for disp in (p for p in disk_files
                 if dispatcher_suffix is not None and p.name.endswith(dispatcher_suffix)):
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
        # TRK-025 T2: the coverage denominator behind the headline — a command lands in
        # EXACTLY one bucket, so commands_total always equals the other three summed.
        "commands_total": commands_resolved + commands_no_script + commands_unparsed,
        "commands_resolved": commands_resolved,
        "commands_no_script": commands_no_script,
        "commands_unparsed": commands_unparsed,
    }


# --- T5: settings / hooks / MCP full-chain merge (compose mode only) ---
# tier-precedence: live-verified 2026-08-07 against real `claude -p` sessions (CC
# 2.1.224, macOS) — all six surfaces matched this table, no code change resulted
# (AMENDMENTS A63); this establishes what a real session resolved to on 2.1.224, not
# documented intent, and not stability across versions. Three settings SOURCES — User
# (`~/.claude/settings.json`, the operator's own `parse_settings` result), Project
# (`<repo>/.claude/settings.json`), Local (`<repo>/.claude/settings.local.json`) —
# precedence Local > Project > User for every key EXCEPT `permissions`, which instead
# MERGES (union, deny wins a same-rule conflict — §3 merge table). Hooks are a separate
# merge rule again: UNION, every tier's matching hooks fire, no precedence winner. Every
# function below is additive/compose-only; the operator-only
# `parse_settings`/`collect_permissions`/`reconcile_hooks`/`collect_config` above are
# UNCHANGED so non-compose output stays byte-identical.

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


def _compose_hooks(sources, project_root, out_of_root_refs, *, profile: dict[str, Any] | None = None):
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
    never followed.

    M11 exit gate, Finding 3 (P2): `profile` is forwarded to `_script_from_command` ONLY
    for the `tier == "user"` source -- that is the single PROFILE-AWARE tier (it resolves
    against the OPERATOR root, the one --profile describes). Project and local tiers keep
    the Claude Code layout unconditionally (schema.md's deferred coupling #1) by passing
    `profile=None`, which defaults `_script_from_command` back to `PROFILE_CLAUDE_CODE`
    regardless of the active profile -- blanket-forwarding `profile` to every tier would
    remap a project/local `~/.claude/hooks/...` command under a non-default profile's
    `hook_command_remaps`, which is a REGRESSION, not a fix. This is the third instance of
    this class (the first two, `_hooks_body_corpus`'s callers and `_hook_disk_files` via
    `reconcile_hooks`, were fixed earlier in M11); every other `_script_from_command`
    caller in this module reads `root/--root`, the operator tree `--profile` describes,
    so this is the only site where "which tier" and "which profile" can disagree."""
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
            script_path, _note, _kind = _script_from_command(
                command, resolve_root, profile=profile if tier == "user" else None)
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


# --- Task 3B (superseded by M11, SPEC_7 §2): these tuples are now a PINNED HISTORICAL
# REFERENCE, not a live source of truth -- PROFILE_CLAUDE_CODE below is the runtime source
# of truth for every collector input glob. A new collector input glob goes in
# PROFILE_CLAUDE_CODE, never here. Their only remaining consumer is
# tests/test_profiles.py::test_default_profile_reproduces_the_shared_glob_constants, which
# is the tripwire proving the default profile still reproduces this pinned set byte-for-byte
# -- keep them in sync with PROFILE_CLAUDE_CODE; do not delete them. The watcher-sync
# property this block used to name directly (every glob a scan reads is also unioned by
# iter_input_paths()) still holds; it is now enforced through the profile.
_INSTRUCTION_GLOBS = ("rules/*.md", "skills/*/rules/*.md", "skills/*/SKILL.md",
                      "skills/*/*/SKILL.md", "skills/*/phases/*.md", "skills/*/prompts/*.md",
                      "skills/*/agents/*.md", "commands/*.md", "agents/*.md")  # flag_long_instructions
_DUP_GLOBS = ("rules/*.md", "skills/*/rules/*.md", "skills/*/SKILL.md",
              "skills/*/phases/*.md", "agents/*.md", "commands/*.md")  # scan_duplication
_STALENESS_RULE_GLOBS = ("rules/*.md", "skills/*/rules/*.md")  # _staleness_corpus (+ CLAUDE.md)
_HOOK_SCRIPT_GLOBS = ("hooks/*.py", "hooks/*.sh")  # mirrors _hook_disk_files / _hooks_body_corpus
# The same two extensions as _HOOK_SCRIPT_GLOBS, in the form _hooks_body_corpus needs:
# it lists hooks/ with os.scandir (a glob would swallow an unlistable dir) and filters
# by suffix itself. Kept adjacent so the pair cannot drift.
_HOOK_BODY_SUFFIXES = (".py", ".sh")
_HOOK_TEST_GLOBS = ("hooks/tests/*.py", "skills/*/hooks/tests/*.py")  # mirrors _hook_test_stems


# --- M11 (SPEC_7 §2): layout profiles. PROFILE_CLAUDE_CODE is AUTHORITATIVE at runtime;
# profiles/claude-code.json is its exported twin (documentation + a template for sharers),
# pinned equal by tests/test_profiles.py::test_profile_file_matches_embedded_constant.
# Tuples, not lists: a profile is read-only config and ORDER IS LOAD-BEARING
# (_deduped_instruction_files is first-match-wins -- see its docstring).
PROFILE_CLAUDE_CODE: dict[str, Any] = {
    "name": "claude-code",
    # Role -> root-relative path. None means "this harness has no such file".
    "top_level_files": {
        "root_instructions": "CLAUDE.md",
        "settings": "settings.json",
        "memory_index": "memory/MEMORY.md",
        "plugin_marketplaces": "plugins/known_marketplaces.json",
        "plugin_installed": "plugins/installed_plugins.json",
    },
    # Role -> root-relative dir name. None means absent. Reached by iterdir(), not glob.
    "container_dirs": {
        "skills": "skills", "rules": "rules", "commands": "commands",
        "agents": "agents", "hooks": "hooks", "hook_tests": "hooks/tests",
        "projects": "projects", "memory": "memory",
    },
    "projects_glob": "projects/*",
    "memory_index_name": "MEMORY.md",
    "skill_manifest_name": "SKILL.md",
    "rules_globs": ("rules/*.md", "skills/*/rules/*.md"),
    "skills_globs": ("skills/*/SKILL.md", "skills/*/*/SKILL.md", "skills/*/phases/*.md",
                     "skills/*/prompts/*.md", "skills/*/agents/*.md"),
    "commands_glob": "commands/*.md",
    "agents_glob": "agents/*.md",
    "hook_script_globs": ("hooks/*.py", "hooks/*.sh"),
    "hook_test_globs": ("hooks/tests/*.py", "skills/*/hooks/tests/*.py"),
    "dispatcher_suffix": "-dispatcher.py",
    # [[literal_prefix, root_relative_dir], ...] -- the "~/.claude/hooks" remap.
    "hook_command_remaps": (("~/.claude/hooks", "hooks"),),
    "duplication_globs": ("rules/*.md", "skills/*/rules/*.md", "skills/*/SKILL.md",
                          "skills/*/phases/*.md", "agents/*.md", "commands/*.md"),
    "settings_format": "claude-code",
}

# Scalar keys accept `str | None`; None means "this harness has no such surface".
_PROFILE_SCALAR_KEYS = ("name", "projects_glob", "memory_index_name", "skill_manifest_name",
                        "commands_glob", "agents_glob", "dispatcher_suffix", "settings_format")
_PROFILE_LIST_KEYS = ("rules_globs", "skills_globs", "hook_script_globs", "hook_test_globs",
                      "duplication_globs")
_PROFILE_MAP_KEYS = ("top_level_files", "container_dirs")
_PROFILE_PAIR_LIST_KEYS = ("hook_command_remaps",)
_PROFILE_SETTINGS_FORMATS = ("claude-code", "none")


def _is_default_layout(profile: dict[str, Any]) -> bool:
    """True iff `profile`'s LAYOUT -- every key except the free-text `name` label -- is
    identical to `PROFILE_CLAUDE_CODE` (M11 exit gate, Codex round, Finding 5): the
    compose-mode "project tier is not profile-aware" disclosure used to gate on
    `profile["name"] != PROFILE_CLAUDE_CODE["name"]`, but `name` is an UNCONSTRAINED
    free-text label with no uniqueness or provenance guarantee -- a user who copies
    `profiles/claude-code.json`, changes `container_dirs`/`rules_globs`/etc. to a genuinely
    different layout, and leaves `name: "claude-code"` (the common case: nothing prompts
    them to rename it) got NO disclosure at all that the project tier still assumes the
    Claude Code layout regardless.

    Chosen over threading extra CLI state (e.g. "was --profile passed at all") through
    build_document/walk_always_loaded: this is a pure function of the two dicts already in
    hand at the call site, needs no new parameter plumbed through every intermediate
    caller, and is correct for the "I want to see MY OWN copy of claude-code.json" case
    too -- an explicit `--profile profiles/claude-code.json` run must NOT trigger the
    disclosure (its layout genuinely IS the default), which a CLI-state-based "was
    --profile passed" check would get wrong.

    Every list-valued role is a tuple on BOTH sides (`load_profile` normalizes lists to
    tuples; `PROFILE_CLAUDE_CODE` is authored as tuples directly), and every map-valued
    role is a plain dict on both sides, so `==` compares like-for-like without any
    coercion here."""
    return all(profile[k] == PROFILE_CLAUDE_CODE[k] for k in PROFILE_CLAUDE_CODE if k != "name")


class ProfileError(ValueError):
    """A --profile file that is unreadable, malformed, or schema-invalid. Raised BEFORE
    any profile value is applied, so a bad profile can never HALF-apply (SPEC_7 §2)."""


def _check_profile_path_safe(value: str, key: str, profile_path: Path) -> None:
    """Reject an ABSOLUTE profile-path value or one with a literal '..' component,
    naming `key` (M11 exit gate, Finding 1 P1). Every such value is later joined onto
    `root` (directly, e.g. `root / value`, or via `root.glob(value)`) somewhere in the
    collector -- a bare `isinstance(value, str)` check alone lets an absolute string
    straight through `load_profile`, and pathlib's `/` SILENTLY REPLACES the left operand
    when the right is absolute (`root / "/etc/hosts" == Path("/etc/hosts")`), defeating
    containment before any read-time gate (`_resolves_inside_root`) ever runs -- the
    escaping join happens first, not a symlink or a "resolves outside" case that gate
    could catch. Reproduced live: a profile setting `top_level_files.root_instructions`
    to "/etc/hosts" crashes `walk_always_loaded` with a bare `ValueError` from `_rel`'s
    unguarded `relative_to`, which `main`'s outer handler turns into a full
    `_empty_document` reported at exit 0 -- total inventory loss read as a clean empty
    harness. This check closes that at the validation boundary, consistent with
    load_profile's existing "never half-apply" contract.

    Uses `pathlib.Path`, not `PurePosixPath`: this process only ever runs on POSIX
    (CLAUDE.md's stdlib-only, deterministic-per-platform posture), and every join this
    validates against (`root / value`, `root.glob(value)`) uses that SAME platform `Path`
    class at read time -- validating with a different path flavor (e.g. PurePosixPath)
    could disagree with what the actual join does. A Windows-style "C:\\x" or "\\\\host\\x"
    is not absolute under a POSIX `Path` and is therefore not rejected here, but it is
    also harmless there: POSIX `Path` treats a backslash as an ordinary filename
    character, so `root / "C:\\x"` stays a single relative component INSIDE root -- not a
    containment escape, just a filename that is unlikely to exist.

    M11 exit gate, Codex round, Finding 2 (P2): also rejects an EMPTY or whitespace-only
    value here, at the same validation boundary. Every value this function checks is later
    either joined onto `root` (`root / value`) OR passed to `root.glob(value)` -- an empty
    string passes both checks above (`Path("").parts == ()`, not absolute) and
    load_profile's earlier `isinstance(value, str)` type check, then reaches
    `root.glob("")`, which raises a bare `ValueError("Unacceptable pattern: ''")` that
    main()'s outer `except Exception` turns into a full `_empty_document` reported at EXIT
    0 -- the identical "total inventory loss read as a clean harness" shape the absolute-
    path fix above was written to close, now through a third vector: not absolute, not
    '..'-bearing, but unusable. A whitespace-only value never crashes `.glob()` (pathlib
    treats it as one ordinary path component) but can never usefully name or match a real
    file either, so it is rejected here for the same reason. (A bare `"."` and an invalid
    `"**"` placement are a FOURTH and FIFTH vector through the identical hole --
    `_check_profile_glob_parses`, below, closes those for the glob-consuming keys.)"""
    candidate = Path(value)
    if value.strip() == "":
        raise ProfileError(
            f"profile {profile_path.name}: {key} must not be empty or whitespace-only")
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ProfileError(
            f"profile {profile_path.name}: {key} must be a relative path with no "
            "'..' component")


def _check_profile_glob_parses(value: str, key: str, profile_path: Path) -> None:
    """Reject a glob-pattern value that Python's own pathlib glob parser cannot compile
    (M11 exit gate, Codex round, Finding 2 follow-up): `_check_profile_path_safe` closes
    the STRING-shape vectors (absolute, '..', empty/whitespace), but a syntactically odd
    pattern can still crash a later `root.glob(pattern)` call with a ValueError OR
    (measured, CPython 3.11.14: `Path(".").glob(".")` raises `IndexError: tuple index out
    of range`) an IndexError -- neither is caught anywhere between the collector's many
    `root.glob(pattern)` call sites and main()'s outer `except Exception`, so either one
    crashes an otherwise-valid-looking profile into a full `_empty_document` reported at
    EXIT 0, same shape as every other vector through this hole.

    Rather than enumerate every such string by hand -- a growing, version-dependent list
    (`"."`, `"**"` outside its own path component, and whatever else pathlib's parser
    rejects on a future Python) -- this validates by ACTUALLY calling `.glob(value)`
    against a placeholder path and forcing its first parse step with `next()`. The parse
    error is a pure function of the PATTERN STRING, independent of whether the base
    directory exists (measured: an existing and a nonexistent base raise the identical
    exception for the identical pattern). Iteration is bounded to one item (`next(...,
    None)`) since forcing the parse is all that is needed; a placeholder directory that
    happens to hold many matching entries must not make profile loading slow.

    Scoped to the keys actually passed to `.glob()` (the scalar globs and every
    `_PROFILE_LIST_KEYS` entry) -- NOT `top_level_files`/`container_dirs`/
    `hook_command_remaps[1]`, which are joined directly and never globbed, so a value that
    merely LOOKS like bad glob syntax is not a real hazard for those roles."""
    try:
        next(Path(".").glob(value), None)
    except (ValueError, IndexError, NotImplementedError) as e:
        raise ProfileError(
            f"profile {profile_path.name}: {key} is not a usable glob pattern ({e})") from e


def _check_profile_bare_name_safe(value: str, key: str, profile_path: Path) -> None:
    """Reject a profile bare-name value (`memory_index_name`, `skill_manifest_name`) that
    is not exactly ONE path component (M11 exit gate, Finding 1 P1). These are joined as
    a single filename segment onto an already-resolved directory (`skill_dir /
    skill_manifest_name`, `slug_dir / memory_dir_name / memory_index_name`) or compared
    directly against one (`f.name == profile["memory_index_name"]`) -- `_check_profile_
    path_safe`'s absolute/'..' check alone is not enough here: "a/b" is neither absolute
    nor contains '..', but `Path(x) / "a/b"` still joins TWO components where the schema
    promises one, and `Path.name` would silently take just "b" rather than reject the
    mismatch. `value != Path(value).name` catches any embedded separator (including a
    leading '/', which is also absolute and would be caught by the equality check itself
    since `Path("/x").name == "x" != "/x"`); the explicit `(".", "..")` exclusion catches
    the two literal single-token values whose `.name` degenerately survives (or -- for
    ".." -- equals) that comparison without being a real filename.

    M11 exit gate, Codex round, Finding 2 (P2): `value.strip() == ""` also rejects a
    whitespace-only bare name (e.g. `"   "`) -- `Path("   ").name == "   "` equals `value`,
    so the equality check alone lets it straight through, and it is not one of the three
    literal tokens the old `in (".", "..")` check named. It never crashes (joined as an
    ordinary, if useless, filename), but it can never name a real file either, so it is
    rejected here for the same "unusable" reason `_check_profile_path_safe` rejects it for
    glob/join roles."""
    if value != Path(value).name or value.strip() == "" or value in (".", ".."):
        raise ProfileError(
            f"profile {profile_path.name}: {key} must be a single path component "
            "(no separators, and not '.' or '..')")


def _instruction_globs(profile: dict[str, Any]) -> tuple[str, ...]:
    """The instruction-file corpus, in DEDUP-SIGNIFICANT order (never sorted)."""
    return (tuple(profile["rules_globs"]) + tuple(profile["skills_globs"])
            + tuple(g for g in (profile["commands_glob"], profile["agents_glob"]) if g))


def _hook_body_suffixes(profile: dict[str, Any]) -> tuple[str, ...]:
    """Suffixes for _hooks_body_corpus's scandir filter, derived from hook_script_globs
    so the two can never disagree. sorted() -> deterministic across PYTHONHASHSEED."""
    return tuple(sorted({Path(g).suffix for g in profile["hook_script_globs"] if Path(g).suffix}))


def load_profile(path: Path) -> dict[str, Any]:
    """Read + strictly validate a layout profile. READ-ONLY: this opens `path` for reading
    and nothing else -- it is a new READ path, never a write path (CLAUDE.md rule 4).

    Every check runs BEFORE the result dict is built, so a malformed profile never
    half-applies. Unknown key -> error naming the key(s). Missing required key -> error
    naming the key(s). Both name-lists are sorted, so the message is deterministic across
    PYTHONHASHSEED (CLAUDE.md rule 9).

    The is_file() gate mirrors parse_settings: open()-for-read on a FIFO with no writer
    blocks forever and never raises, so a special file at this path must be rejected
    BEFORE the read, not after."""
    try:
        is_regular = _probe_is_file(path)
    except OSError as e:
        raise ProfileError(f"profile {path.name}: cannot stat ({e!r})") from e
    if not is_regular:
        raise ProfileError(f"profile {path.name}: not a readable regular file")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ProfileError(f"profile {path.name}: unreadable ({e!r})") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProfileError(f"profile {path.name}: not valid JSON ({e})") from e
    if not isinstance(data, dict):
        raise ProfileError(f"profile {path.name}: top level must be a JSON object")

    unknown = sorted(set(data) - set(PROFILE_CLAUDE_CODE))
    if unknown:
        raise ProfileError(f"profile {path.name}: unknown key(s): {', '.join(unknown)}")
    missing = sorted(set(PROFILE_CLAUDE_CODE) - set(data))
    if missing:
        raise ProfileError(f"profile {path.name}: missing required key(s): {', '.join(missing)}")

    for key in _PROFILE_SCALAR_KEYS:
        if data[key] is not None and not isinstance(data[key], str):
            raise ProfileError(f"profile {path.name}: {key} must be a string or null")
    if data["name"] is None:
        raise ProfileError(f"profile {path.name}: name must not be null")
    if data["settings_format"] not in _PROFILE_SETTINGS_FORMATS:
        raise ProfileError(
            f"profile {path.name}: settings_format must be one of "
            f"{', '.join(_PROFILE_SETTINGS_FORMATS)} (v1 supports no other adapters)")
    for key in _PROFILE_LIST_KEYS:
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(g, str) for g in value):
            raise ProfileError(f"profile {path.name}: {key} must be a list of strings")
    for key in _PROFILE_MAP_KEYS:
        value = data[key]
        if not isinstance(value, dict):
            raise ProfileError(f"profile {path.name}: {key} must be a JSON object")
        unknown_roles = sorted(set(value) - set(PROFILE_CLAUDE_CODE[key]))
        if unknown_roles:
            raise ProfileError(
                f"profile {path.name}: {key} has unknown role(s): {', '.join(unknown_roles)}")
        missing_roles = sorted(set(PROFILE_CLAUDE_CODE[key]) - set(value))
        if missing_roles:
            raise ProfileError(
                f"profile {path.name}: {key} is missing role(s): {', '.join(missing_roles)}")
        for role, entry in value.items():
            if entry is not None and not isinstance(entry, str):
                raise ProfileError(
                    f"profile {path.name}: {key}.{role} must be a string or null")
    for key in _PROFILE_PAIR_LIST_KEYS:
        value = data[key]
        if not isinstance(value, list):
            raise ProfileError(f"profile {path.name}: {key} must be a list")
        for pair in value:
            if (not isinstance(pair, list) or len(pair) != 2
                    or not all(isinstance(s, str) for s in pair)):
                raise ProfileError(
                    f"profile {path.name}: {key} entries must be [prefix, dir] string pairs")

    # M11 exit gate, Finding 1 (P1): every value that is later joined onto `root` as a
    # filesystem path must be relative and '..'-free (_check_profile_path_safe), or --
    # for the two bare-filename roles -- exactly one path component
    # (_check_profile_bare_name_safe). Runs AFTER every type check above, still BEFORE
    # the result dict is built, so a profile with an unsafe path never half-applies.
    #
    # `name`, `settings_format`, and `dispatcher_suffix` are deliberately NOT checked
    # here: `name` and `settings_format` are labels/enum values, never joined onto a
    # path, and `dispatcher_suffix` is only ever compared with `str.endswith` (collector.py
    # reconcile_hooks) -- never joined -- so a '/' or '..' inside it is inert, not a
    # containment vector.
    for key in ("projects_glob", "commands_glob", "agents_glob"):
        value = data[key]
        if value is not None:
            _check_profile_path_safe(value, key, path)
            _check_profile_glob_parses(value, key, path)
    for key in ("memory_index_name", "skill_manifest_name"):
        value = data[key]
        if value is not None:
            _check_profile_bare_name_safe(value, key, path)
    for key in _PROFILE_LIST_KEYS:
        for entry in data[key]:
            _check_profile_path_safe(entry, key, path)
            _check_profile_glob_parses(entry, key, path)
    for key in _PROFILE_MAP_KEYS:
        for role, entry in data[key].items():
            if entry is not None:
                _check_profile_path_safe(entry, f"{key}.{role}", path)
    for pair in data["hook_command_remaps"]:
        # Only the SECOND element (rel_dir) is a root-relative path this collector joins
        # onto `root` -- the first is a literal '~'-prefixed prefix matched textually
        # against a registered command string (e.g. "~/.claude/hooks"); it is SUPPOSED
        # to look absolute-ish and is never joined onto `root`, so it is not checked.
        _check_profile_path_safe(pair[1], "hook_command_remaps[1]", path)

    # Built only after EVERY check passed. Lists -> tuples: order preserved, never sorted.
    result: dict[str, Any] = {}
    for key, value in data.items():
        if key in _PROFILE_LIST_KEYS:
            result[key] = tuple(value)
        elif key in _PROFILE_PAIR_LIST_KEYS:
            result[key] = tuple(tuple(pair) for pair in value)
        elif key in _PROFILE_MAP_KEYS:
            result[key] = dict(value)
        else:
            result[key] = value
    return result


def _deduped_instruction_files(root: Path, inaccessible: list[dict[str, Any]],
                               blind_spots: list[str], *,
                               profile: dict[str, Any] | None = None) -> list[Path]:
    """Shared glob-walk + dedup for the instruction-file corpus (S2.M3): the SINGLE
    definition of "the deduped instruction-file set" consumed by BOTH
    flag_long_instructions (line-count flags) and collect_git_age's caller (staleness
    signal) -- add a new instruction-file glob to PROFILE_CLAUDE_CODE's rules_globs /
    skills_globs, never inline a second glob loop here or elsewhere.

    M11 (SPEC_7 §2): `profile` (defaulting to PROFILE_CLAUDE_CODE) supplies the glob
    set via _instruction_globs(profile), reproducing _INSTRUCTION_GLOBS byte-for-byte
    on the default profile.

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
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
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
    for pattern in _instruction_globs(profile):
        # Codex #4 (S2 gate fix): sort BEFORE dedup. root.glob() yields in filesystem
        # order, so both the `seen` winner and the returned order were filesystem
        # dependent. D4's budget-exhaustion truncation silences a SUFFIX of this list, so
        # an unsorted order would make "which files went unmeasured" nondeterministic.
        # Sorted by the string form, matching build_document's rel-path sort (F11).
        pattern_matches = sorted(root.glob(pattern), key=str)
        # TRK-082 T3: an unlistable (but present) directory for this pattern is
        # indistinguishable from a genuinely empty one via glob() alone -- disclose it
        # the same way scan_duplication/_staleness_corpus do (blind_spots, since this
        # function has no per-pattern try/except OSError to route through).
        _disclose_unlistable_glob(root, pattern, pattern_matches, blind_spots,
                                   "instruction files")
        for fp in pattern_matches:
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
                           blind_spots: list[str], *,
                           profile: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    flags: list[dict[str, Any]] = []
    for fp in _deduped_instruction_files(root, inaccessible, blind_spots, profile=profile):
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
# in-flight subprocess timeout (<=5s) + the submodule provenance probe that can start just
# before it (`rev-parse --verify`, <=2s -- Codex gate finding 2) ~= 21s, replacing today's
# UNBOUNDED 230-260s worst case.
# 4.5x the measured 2.24s typical; a backstop for a degenerate case (huge-history
# submodule, network-mounted .git, hung git), not a perf target. DELIBERATELY not tied
# to --check's <=5s: --check runs the SAME build_document() path as default mode --
# there is no separate thin path -- and was measured at 3.17s against that budget
# (TRK-051 F4).
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

# Harden-audit fix (T8 round 2, EXTENDED by the pre-flight exit gate): every name here
# either RE-TARGETS git away from `cwd` or INJECTS config from the environment. Both
# defeat this wrapper's stated invariant that cwd is the single source of repo-targeting
# truth, and both arrive from the process that invoked the collector.
#   - redirect class: the original four plus GIT_OBJECT_DIRECTORY and GIT_COMMON_DIR
#     (same store/dir redirection), GIT_CEILING_DIRECTORIES (truncates `--show-toplevel`
#     discovery, so a real work tree reports as `no_repo` -- MEASURED) and GIT_NAMESPACE.
#   - config class: GIT_CONFIG_COUNT + the INDEXED GIT_CONFIG_KEY_<n>/GIT_CONFIG_VALUE_<n>
#     pairs (handled by prefix below -- they are unbounded, so no fixed list can cover
#     them), GIT_CONFIG_PARAMETERS (git's own internal `-c` transport, the same channel
#     under another name) and GIT_CONFIG/GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM/
#     GIT_CONFIG_NOSYSTEM, which repoint the config FILE layer at attacker-chosen files.
#
# MEASURED on git 2.50.1: command-line `-c` OUTRANKS every environment config form, so
# the five keys in _GIT_SAFE_CONFIG were never reinstatable this way. What the config
# class did reach was every OTHER key -- including command-valued ones (filter.*.clean,
# diff.*.textconv, ...) that _GIT_SAFE_CONFIG does not pin and a subcommand added to this
# wrapper later would execute. That is the "defense in depth for subcommands added later"
# _GIT_SAFE_CONFIG already claims, applied to the layer it was missing.
#
# STRIPPED, not overridden: pointing GIT_CONFIG_GLOBAL/SYSTEM at /dev/null (or forcing
# GIT_CONFIG_NOSYSTEM=1) would also discard the OPERATOR's real ~/.gitconfig and
# /etc/gitconfig -- where `safe.directory` lives. Suppressing those turns repositories
# the operator has legitimately allow-listed into exit-128 "dubious ownership" failures,
# i.e. it converts measured timestamps into nulls. Removing the env overrides restores
# the operator's own config exactly, and the dangerous keys are already outranked by
# `-c` at a strictly higher precedence layer, so the override buys nothing here.
_GIT_STRIPPED_ENV_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_CEILING_DIRECTORIES", "GIT_NAMESPACE",
    "GIT_CONFIG", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_NOSYSTEM",
    "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
)
_GIT_STRIPPED_ENV_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


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
    # See _GIT_STRIPPED_ENV_VARS for why each name is in the set. The PREFIX pass is not
    # a stylistic variant of the name pass: GIT_CONFIG_KEY_<n>/GIT_CONFIG_VALUE_<n> are
    # indexed and unbounded, so every matching name must be dropped, not a fixed list.
    # Iterate over a LIST copy -- mutating a dict during iteration raises RuntimeError.
    for stripped_var in _GIT_STRIPPED_ENV_VARS:
        env.pop(stripped_var, None)
    for name in list(env):
        if name.startswith(_GIT_STRIPPED_ENV_PREFIXES):
            del env[name]
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
    (`_probe_exists` RAISES PermissionError -- it swallows only the ENOENT family), so the walk
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
    tell the operator "git could not run at all" when git ran fine.

    Pre-flight exit gate: exit 0 with EMPTY stdout is an ANOMALY, not evidence. It used
    to fall through to `no_repo` -- a definitive negative ("this is not a work tree")
    asserted over a state nobody examined, which then became the blanket staleness reason
    for every file underneath. Real git never produces that shape, so reaching it means
    something about the invocation is not what this function assumed; `git_error` is the
    honest unknown, and it is the same enum value the unknown-index path already uses.
    The CLEAN non-zero exit above is untouched only where it really is a negative git
    reported (Codex gate finding 4, narrowing it further). Git ALSO exits non-zero when it
    REFUSES to read a repository that is plainly present: dubious ownership
    (`safe.directory`, realistic on a shared or mounted checkout) and corrupt/unreadable
    git metadata both land here. Reading those as `no_repo` asserts "this is not a work
    tree" over a state git declined to determine -- and that reason then becomes the
    blanket staleness label for every file underneath.

    The discriminator is `--resolve-git-dir`, and it is neither stderr nor a stat:
      * stderr is forbidden (binding rule 11 -- git's error text carries absolute paths and
        .gitmodules/.git/config values), and the exit code is 128 for BOTH readings.
      * a `Path.exists()` probe of `dir_path/".git"` would be a filesystem read of a path
        `iter_input_paths` deliberately prunes, breaking the standing superset invariant
        (test_iter_input_paths_is_superset_of_real_build_document_reads) -- `.git` is not a
        harness input, and a commit must not wake the `--watch` sweep.
    MEASURED (git 2.50.1): `rev-parse --resolve-git-dir <path>` answers from a cwd that is
    not a repository at all, so it runs BEFORE repository setup -- which is exactly the
    phase the ownership refusal happens in. Exit 0 means a valid git dir (or a gitfile
    pointing at one) is sitting right there while `--show-toplevel` refused it: an
    UNDETERMINED state, `git_error`. Exit non-zero means git found no usable repository
    there either, which is the negative `no_repo` really does describe.

    Only ONE extra subprocess, only on the already-failing branch."""
    proc, err = _git(["rev-parse", "--show-toplevel"], dir_path, _GIT_SUBPROCESS_TIMEOUT)
    if proc is None:
        return None, "git_unavailable" if err == "git_error" else "timeout"
    if proc.returncode != 0:
        marker, marker_err = _git(["rev-parse", "--resolve-git-dir", str(dir_path / ".git")],
                                  dir_path, _GIT_SUBPROCESS_TIMEOUT)
        if marker is None:
            return None, marker_err or "git_error"
        return None, "git_error" if marker.returncode == 0 else "no_repo"
    out = _decode_git(proc.stdout).strip()
    return (out, None) if out else (None, "git_error")


def _git_common_dir(dir_path: Path) -> tuple[str | None, str | None]:
    """(absolute git-common-dir, None) or (None, closed-enum reason).

    Codex gate finding 4: the reason used to be DISCARDED (a bare `str | None`), and the
    single caller published every failure -- including a TIMEOUT and an unreadable git
    directory -- as `outside_root`, a determined "this escaped the harness root" over a
    state nobody determined. Same discrimination the sibling `_git_toplevel` already does.

    Harden-audit fix (T9 round 2, HIGH): the gitlink fence proves a PATH is named as a
    submodule by an accepted parent's index; it cannot prove the `.git` at that path
    BELONGS to that parent. An attacker who can write inside the named subtree replaces
    `.git` with a gitfile (`gitdir: <their repo>`) -- --show-toplevel still reports the
    containing directory, so the path still matches -- and the subsequent batched
    `ls-files` binds to a FOREIGN repository (demonstrated end to end). Requiring the
    git-common-dir to resolve INSIDE the harness root closes that: the attacked submodule
    reports `.../evil/.git` where an honest one reports a dir under the root.

    VERIFIED INERT: this rev-parse form executes no command-valued config."""
    proc, err = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"],
                     dir_path, _GIT_SUBPROCESS_TIMEOUT)
    if proc is None:
        return None, err or "git_error"
    if proc.returncode != 0:
        return None, "git_error"
    out = _decode_git(proc.stdout).strip()
    return (out, None) if out else (None, "git_error")


class _GitlinkVoucher(NamedTuple):
    """What an ALREADY-ACCEPTED parent repository's index says about a candidate submodule
    toplevel. Every field comes from the parent's own `ls-files -s` output or from the
    parent's already-validated topology -- never from the candidate, whose filesystem
    state an attacker who can write in the subtree controls."""
    parent_top: str
    parent_common_dir: str | None
    rel: str
    sha: str                 # the mode-160000 commit the parent's index RECORDS for `rel`


_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}")


def _repo_contains_commit(top: str, sha: str) -> tuple[bool, str | None]:
    """(contains, None) or (False, closed-enum reason) — does the repository at `top` hold
    `sha` as a commit object?

    MEASURED (git 2.50.1): `rev-parse --verify --quiet <sha>^{commit}` exits 0 printing the
    sha when the object is present and exits 1 SILENTLY when it is not, so no stderr is
    ever read (binding rule 11). It is the same inert rev-parse family `_git`'s docstring
    records as executing no command-valued config, and it runs BEFORE `ls-files` -- the one
    call measured to execute `core.fsmonitor` in whatever repo it lands in.

    A malformed sha is refused without a subprocess: it can only come from a divergence
    between this parser and git's index format, which is an UNKNOWN, and a leading `-`
    would otherwise be read as an option."""
    if not _GIT_SHA_RE.fullmatch(sha):
        return False, "git_error"
    proc, err = _git(["rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}"],
                     Path(top), _GIT_SUBPROCESS_TIMEOUT)
    if proc is None:
        return False, err or "git_error"
    return proc.returncode == 0, None


def _gitlink_provenance_refusal(top: str, common_dir: str,
                                voucher: _GitlinkVoucher) -> tuple[str, str] | None:
    """None when the git directory at `top` is one the vouching parent accounts for, else
    (closed-enum reason, phrase).

    Codex gate finding 2 (HIGH). `_toplevel_refusal`'s two containment checks ask only
    WHERE things sit, and both are satisfied by an attack that never leaves the root:
    replace an accepted submodule's `.git` with `gitdir: <root>/evil/.git`, and
    --show-toplevel still reports the gitlinked directory (so the parent's gitlink entry
    still matches the PATH) while `ls-files`/`git log` bind to a foreign index and
    history. Reproduced: the file's timestamp came back as the ATTACKER's commit.

    TWO conjunctive tests, neither of them root-containment:
      (a) ANCHOR. The git dir must live inside the gitlinked path ITSELF (`<top>/.git`,
          the embedded layout -- measured live: all three of this harness's own submodules
          carry a real `.git` DIRECTORY) or inside the PARENT's own git dir
          (`<parent>/.git/modules/...`, the layout `git submodule add` creates -- the
          submodule_tree fixture). An in-root redirect to an unrelated repository is
          neither.
      (b) INDEX PROVENANCE. The repository must contain the exact commit the parent's
          index RECORDS for that gitlink. This is the only fact the parent's index vouches
          for; everything else here is filesystem state.

    Residual, stated rather than overclaimed: (b) alone is defeated by an attacker who
    already holds the genuine submodule's objects and can therefore build a repository
    containing the vouched commit -- which is why (a) is kept beside it rather than
    replaced by it."""
    try:
        top_stat = os.stat(top)
    except OSError:
        return "git_error", f"the gitlinked path {top} could not be stat'd"
    # (a) -- `_resolves_inside_root` is a generic containment predicate (candidate, container,
    # container stat); the parameter is named `root` only because that was its first caller.
    anchored = _resolves_inside_root(Path(common_dir), Path(top), top_stat)
    if not anchored and voucher.parent_common_dir is not None:
        try:
            parent_gitdir_stat = os.stat(voucher.parent_common_dir)
        except OSError:
            return "git_error", (f"the git directory of the repository vouching for {top} "
                                 f"could not be stat'd")
        anchored = _resolves_inside_root(Path(common_dir),
                                         Path(voucher.parent_common_dir), parent_gitdir_stat)
    if not anchored:
        return "outside_root", (
            f"the git directory backing {top} ({common_dir}) is neither inside that "
            f"gitlinked path nor inside the git directory of the repository whose index "
            f"names it as a submodule")
    # (b)
    contains, why = _repo_contains_commit(top, voucher.sha)
    if why is not None:
        return why, (f"the repository at {top} could not be checked against the commit "
                     f"its parent's index records for it")
    if not contains:
        return "outside_root", (
            f"the repository backing {top} does not contain the commit its parent's index "
            f"records for that submodule, so it is not the one the index vouches for")
    return None


def _toplevel_refusal(top: str, root: Path, root_stat: os.stat_result | None,
                      voucher: _GitlinkVoucher | None = None,
                      common_dir_result: tuple[str | None, str | None] | None = None
                      ) -> tuple[str, str] | None:
    """None when the work tree at `top` may be probed, else (closed-enum reason, phrase).

    THREE halves now, and no one of them is sufficient:
      1. the work-tree PATH must lie inside the harness root (a `git -C` outside it binds
         to a foreign repository -- S17, T9 harden round 2);
      2. the git-common-dir must too (a gitfile inside an accepted path can point the SAME
         path at someone else's repository);
      3. and, for a candidate accepted via the gitlink clause, that git dir must be one the
         vouching parent's INDEX accounts for (`_gitlink_provenance_refusal`) -- because 1
         and 2 are both satisfied by a redirect that stays inside the root (Codex gate
         finding 2). `voucher=None` (the scanned root's own probe) runs 1 and 2 only:
         the root is the authority every other toplevel is validated against, so there is
         no outer index to vouch for it.

    The REASON is returned beside the phrase rather than left to the caller (Codex gate
    finding 4): every refusal used to be published as `outside_root`, including the ones
    that mean "could not determine". The phrase is returned rather than a bare bool because
    the caller publishes it as a blind spot: a silent refusal is what Finding 4 of the T9
    harden round was about.

    `common_dir_result` lets a caller that already resolved (or is about to reuse) `top`'s
    git-common-dir pass it in, so the run's subprocess count is unchanged by the provenance
    fence. Omitted -> resolved here, which is what a direct caller wants."""
    if root_stat is None:
        return "git_error", "the harness root could not be stat'd, so containment is undecidable"
    if not _resolves_inside_root(Path(top), root, root_stat):
        return "outside_root", f"its work tree ({top}) resolves outside the harness root"
    if common_dir_result is None:
        common_dir_result = _git_common_dir(Path(top))
    common_dir, why = common_dir_result
    if common_dir is None:
        return why or "git_error", f"the git directory backing {top} could not be resolved"
    if not _resolves_inside_root(Path(common_dir), root, root_stat):
        return "outside_root", (f"the git directory backing {top} ({common_dir}) resolves "
                                f"outside the harness root")
    if voucher is not None:
        return _gitlink_provenance_refusal(top, common_dir, voucher)
    return None


def _git_index_snapshot(top: Path) -> tuple[frozenset[str], dict[str, str]] | None:
    """One batched `git ls-files -s -z` per confirmed repo root ->
    (tracked paths, {gitlink path: the mode-160000 commit the index RECORDS}).

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
    don't know" with a specific WRONG answer. That is worse than shipping the blind spot.

    The gitlink SHA is kept, not just the path (Codex gate finding 2): the recorded commit
    is the ONE thing the parent's index actually vouches for about a submodule, and
    `_gitlink_provenance_refusal` needs it to tell the vouched repository apart from a
    foreign one sitting at the same path. `<mode> <sha> <stage>\\t<path>` -- the head is
    split on ASCII spaces, which cannot appear in the fixed-width mode/sha/stage fields,
    while the PATH (which may contain spaces AND tabs) stays whole in `raw_path`."""
    proc, _err = _git(["ls-files", "-s", "-z"], top, _GIT_BATCH_TIMEOUT)
    if proc is None or proc.returncode != 0:
        return None
    tracked: set[str] = set()
    gitlink_shas: dict[str, str] = {}
    for chunk in proc.stdout.split(b"\0"):
        if not chunk:
            continue
        head, _tab, raw_path = chunk.partition(b"\t")
        if not raw_path:
            continue
        rel = _decode_git(raw_path)
        tracked.add(rel)
        if head.startswith(b"160000"):
            fields = _decode_git(head).split()
            gitlink_shas[rel] = fields[1] if len(fields) > 1 else ""
    return frozenset(tracked), gitlink_shas


def _git_tracked_and_gitlinks(top: Path) -> tuple[frozenset[str], frozenset[str]] | None:
    """The two-SET view of `_git_index_snapshot`, kept as the narrow parse-level surface an
    existing regression assertion reads (binding rule 7 pins its shape). Production reads
    the snapshot directly, because it needs the gitlink SHAs the set form cannot carry."""
    snapshot = _git_index_snapshot(top)
    if snapshot is None:
        return None
    tracked, gitlink_shas = snapshot
    return tracked, frozenset(gitlink_shas)


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
    # {toplevel: {gitlink path: the mode-160000 commit that index records}}. A MAP, not a
    # set: the recorded commit is the provenance datum _gitlink_provenance_refusal needs,
    # and holding it beside the path keeps the two from drifting into disagreement. Every
    # membership read (`rel in ...`) is unchanged by the widening.
    gitlinks_by_toplevel: dict[str, dict[str, str]]
    # Roots whose per-root load was SKIPPED because the total budget expired during
    # discovery (Codex F5). Distinct from tracked_by_toplevel[top] is None, which means
    # the load RAN and failed -- conflating them would map exhaustion to git_error.
    # TRAILING DEFAULT: every pre-D4 constructor stays valid unchanged.
    exhausted_roots: frozenset[str] = frozenset()


def _accept_via_gitlink(top: str, accepted: set[str],
                        gitlinks_by_toplevel: dict[str, dict[str, str]]
                        ) -> tuple[str, str, str] | None:
    """(vouching parent toplevel, index path, recorded commit) when `top` is named as a
    mode-160000 gitlink by an ALREADY-ACCEPTED root (8.6 clause 2), else None.

    Candidate dirs are processed shallowest-first, so a nested submodule is reached
    transitively without recursion ONLY when an instruction file also lives in the
    intervening (outer) submodule -- a toplevel enters `accepted` when one of its own dirs
    is processed, not merely by being an ancestor. When corpus files exist only inside the
    INNER submodule, the outer is never accepted and the inner is refused as `outside_root`
    (a null, not a wrong number -- the safe direction, but not the transitive reach this
    docstring used to promise).

    Returns the VOUCHER, not a bool (Codex gate finding 2): the caller must know WHICH
    index vouched and what commit it recorded, because path evidence alone cannot tell the
    vouched repository from a foreign one planted at the same path. `accepted` is iterated
    SORTED so the chosen parent -- and therefore every refusal phrase built from it -- is
    stable across PYTHONHASHSEED (binding rule 9)."""
    for parent in sorted(accepted):
        try:
            rel = str(Path(top).relative_to(parent))
        except ValueError:
            continue
        sha = gitlinks_by_toplevel.get(parent, {}).get(rel)
        if sha is not None:
            return parent, rel, sha
    return None


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
    gitlinks_by_toplevel: dict[str, dict[str, str]] = {}
    common_dir_by_toplevel: dict[str, tuple[str | None, str | None]] = {}

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
            gitlinks_by_toplevel[top] = {}
            exhausted.add(top)
            return
        snapshot = _git_index_snapshot(Path(top))
        if snapshot is None:
            tracked_by_toplevel[top] = None
            gitlinks_by_toplevel[top] = {}
        else:
            tracked_by_toplevel[top], gitlinks_by_toplevel[top] = snapshot

    def _common_dir(top: str) -> tuple[str | None, str | None]:
        """Memoized `_git_common_dir`. A toplevel's own common dir is resolved when IT is
        checked, and read again as the ANCHOR when it later vouches for a nested submodule
        (`_gitlink_provenance_refusal`) -- so in the common case, where the scanned root is
        the only vouching parent, provenance costs ZERO extra subprocesses."""
        if top not in common_dir_by_toplevel:
            common_dir_by_toplevel[top] = _git_common_dir(Path(top))
        return common_dir_by_toplevel[top]

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
            refusal = _toplevel_refusal(root_top, root, root_stat,
                                        common_dir_result=_common_dir(root_top))
            if refusal is not None:
                reason, phrase = refusal
                blind_spots.append(
                    f"git-age: the scanned root's work tree was not probed — {phrase} "
                    f"(a `git -C` there would bind to a foreign repository)")
                # Codex gate finding 4: the refusal's OWN reason, not a blanket
                # `outside_root` -- "the git dir could not be resolved" is an undetermined
                # state, and publishing it as a containment verdict asserts a fact about
                # the operator's harness root that nobody established.
                root_top, root_reason = None, reason
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
        if top not in accepted:
            vouched = _accept_via_gitlink(top, accepted, gitlinks_by_toplevel)
            if vouched is None:
                # A work tree that neither IS the scanned root nor is named as a gitlink by
                # an already-accepted root. Refuse rather than guess (8.6 clause 2) -- and
                # SAY SO (T9 harden round 2, LOW): this is the exact case the fence exists
                # to catch, and it used to leave the operator a bare null with no trace.
                toplevel_by_dir[dir_key] = (None, "outside_root")
                blind_spots.append(
                    f"git-age: {dir_key} was not probed — its work tree ({top}) is neither "
                    f"the scanned root nor named as a gitlink by an accepted root")
                continue
            # T10 audit (MEDIUM): _toplevel_refusal runs a SECOND subprocess of its own
            # (`rev-parse --git-common-dir`, 2s-capped) for every distinct new gitlink
            # toplevel. It sat between two gated checkpoints -- the pre-_git_toplevel guard
            # above and _load_root's own -- so K submodule roots spent up to K x 2s past
            # the deadline invisibly, falsifying the TOTAL budget this docstring promises.
            # Same mapping as that guard: an unspent budget is not a containment refusal.
            if deadline is not None and time.monotonic() >= deadline:
                toplevel_by_dir[dir_key] = (None, "budget_exhausted")
                continue
            # PROVENANCE, not just path (T9 harden round 2, HIGH; completed by Codex gate
            # finding 2). The gitlink clause above proved only that an accepted parent's
            # index names this PATH; the `.git` sitting there can still belong to someone
            # else's repository -- and the T9 containment checks alone are all satisfied by
            # a `gitdir:` redirect that stays INSIDE the root. The voucher carries what the
            # parent's index actually attests: the recorded commit, plus the parent's own
            # git dir as the anchor. Checked BEFORE _load_root, because `ls-files` is the
            # call that would bind to it (and the one measured to execute core.fsmonitor).
            parent_top, rel, sha = vouched
            parent_common_dir, _parent_why = _common_dir(parent_top)
            refusal = _toplevel_refusal(
                top, root, root_stat,
                voucher=_GitlinkVoucher(parent_top, parent_common_dir, rel, sha),
                common_dir_result=_common_dir(top))
            if refusal is not None:
                reason, phrase = refusal
                toplevel_by_dir[dir_key] = (None, reason)
                blind_spots.append(f"git-age: {dir_key} was not probed — {phrase}")
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
    canonicalize case on APFS (a path whose case differs from the on-disk name comes back
    unchanged) but git's `--show-toplevel` DOES, so with a case-variant --root every single
    file raised ValueError -- the whole git-age signal vanished behind reason `git_error`
    while `git_age_available` still reported True, blaming git for something git got right.

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
        gitlinks = index.gitlinks_by_toplevel.get(top, {})
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

# TRK-023 slice A: project-tier always-loaded instruction files fed into the hygiene
# corpus -- the project analog of the operator corpus's `root_instructions` file.
_PROJECT_HYGIENE_ROOT_FILES = ("CLAUDE.md", "CLAUDE.local.md")


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
            if _probe_is_dir(d):
                dir_matches = sorted(d.glob(pattern))
                candidates.extend(dir_matches)
                _disclose_unlistable_glob(d, pattern, dir_matches, blind_spots,
                                           "project duplication corpus")
        except OSError as e:
            # Inaccessible is NOT clean: an unreadable project-tier surface dir yields
            # zero candidates for it, which reads identically to "nothing there" unless
            # recorded. blind_spots is the existing recording channel for this function.
            blind_spots.append(f"project {rel_dir} not probed for duplication scan: {e}")
            continue
    skills_dir = harness_root / "skills"
    try:
        skills_dir_is_dir = _probe_is_dir(skills_dir)
    except OSError as e:
        # Same "inaccessible is NOT clean" invariant: an unreadable .claude/skills yields
        # zero skill SKILL.md candidates with no signal unless recorded here.
        # TRK-050 T5 F5: distinct text from the iterdir() failure just below -- the two
        # are different failure modes (mutually exclusive within one call: an ancestor
        # that fails is_dir() never reaches iterdir()), and a reader could not tell which
        # occurred when both shared one literal.
        blind_spots.append(f"project skills is_dir failed for duplication scan: {e}")
        skills_dir_is_dir = False
    if skills_dir_is_dir:
        try:
            skill_entries = sorted(skills_dir.iterdir())
        except OSError as e:
            blind_spots.append(f"project skills listing failed for duplication scan: {e}")
            skill_entries = []
        skill_dirs = []
        for p in skill_entries:
            try:
                if p.is_dir():
                    skill_dirs.append(p)
            except OSError as e:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T2).
                blind_spots.append(f"project skills child is_dir failed for {p}: {e}")
        for skill_dir in skill_dirs:
            skill_md = skill_dir / "SKILL.md"
            present, ok = _safe_exists(skill_md)
            if ok and present:
                candidates.append(skill_md)

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


def _project_tier_hygiene_corpus(project_root, inaccessible, blind_spots, out_of_root_refs):
    """Project-tier hygiene corpus feeding `collect_promotion_candidates`'s project half
    (TRK-023 T2). Three surface groups, read in this fixed order:
      1. `<repo>/CLAUDE.md` and `<repo>/CLAUDE.local.md` -- the project's own always-loaded
         instructions, the closest analog of the operator corpus's `root_instructions` file.
      2. Each `(rel_dir, pattern)` in `_PROJECT_DUP_SURFACE_DIRS` (`.claude/rules/*.md`,
         `.claude/agents/*.md`, `.claude/commands/*.md`).
      3. Each project skill's `<repo>/.claude/skills/<name>/SKILL.md`.

    EVERY read routes through `_project_tier_gate` + `_read_project_file` (H2) -- an
    escaping symlink is recorded to `out_of_root_refs` and NEVER read. A surface
    directory may still be ENUMERATED before its containment is decided (a pre-existing,
    measured gap shared with `_project_tier_duplication_corpus`, recorded in spec
    AMENDMENTS A64) -- no byte outside `project_root` ever crosses the gate. The skill
    DIRECTORY is gated before its `SKILL.md` is even probed for existence, unlike the
    duplication-corpus template this function is modelled on: that template probes
    `SKILL.md` before the per-candidate gate runs, making the file's presence an
    existence oracle for an escaping skill directory (spec AMENDMENTS A64); this new
    code does not copy that shape.

    Unlike `_project_tier_duplication_corpus`, `_read_project_file` returning
    `text is None` is recorded to `inaccessible` here -- a deliberate divergence: the
    template drops a read failure silently, a known `inaccessible != clean` gap this new
    corpus must not inherit.

    Returns `(corpus, scan_complete)`, mirroring `_hooks_body_corpus`'s established
    `(corpus, complete)` two-tuple shape -- not a new invention. `scan_complete` is
    `False` if ANY surface could not be fully examined: an unstattable containment root,
    a gate refusal, an unlistable or locked surface directory, an oversize file, a
    vanished/unstat-able candidate, or an unreadable file -- so a genuinely-empty scan is
    structurally distinguishable from one that silently lost data (spec Design section
    F3): `project is not None` alone cannot tell "scanned, found nothing" from "could not
    read", and `scan_complete` is the channel that makes the difference visible."""
    project_root = Path(project_root)
    scan_complete = True
    corpus: list[tuple[str, str]] = []
    seen_refs: set[str] = set()
    seen_physical: set[Any] = set()
    try:
        containment_stat = os.stat(project_root)
    except OSError as e:
        # F3 path 3, the silent one in the template: a bare `return corpus` records
        # nothing anywhere. This corpus must not inherit that -- disclose and degrade.
        blind_spots.append(f"project root not probed for hygiene scan: {e}")
        return corpus, False

    candidates: list[Path] = []

    # 1. Repo-root always-loaded instructions. Absent is normal (never a blind spot);
    #    an undeterminable presence (a locked ancestor) is recorded to `inaccessible`,
    #    mirroring `_hooks_body_corpus`'s `_safe_exists` + not-ok handling.
    for fname in _PROJECT_HYGIENE_ROOT_FILES:
        fp = project_root / fname
        present, ok = _safe_exists(fp)
        if not ok:
            _append_inaccessible_once(inaccessible, _rel_safe(project_root, fp))
            scan_complete = False
            continue
        if present:
            candidates.append(fp)

    # 2. `.claude/{rules,agents,commands}` -- same enumeration shape
    #    `_project_tier_duplication_corpus` uses (spec Design F5/R3-6: keeping the
    #    template's shape here rather than diverging from its sibling).
    for rel_dir, pattern in _PROJECT_DUP_SURFACE_DIRS:
        d = project_root / rel_dir
        try:
            if _probe_is_dir(d):
                dir_matches = sorted(d.glob(pattern))
                candidates.extend(dir_matches)
                # _disclose_unlistable_glob returns None; its only effect is an append to
                # blind_spots when the directory is present but unlistable -- detect that
                # by length delta rather than reimplementing its scandir probe here.
                before = len(blind_spots)
                _disclose_unlistable_glob(d, pattern, dir_matches, blind_spots,
                                           "project hygiene corpus")
                if len(blind_spots) > before:
                    scan_complete = False
        except OSError as e:
            blind_spots.append(f"project {rel_dir} not probed for hygiene scan: {e}")
            scan_complete = False
            continue

    # 3. Each project skill's SKILL.md -- the skill DIRECTORY is gated BEFORE its
    #    SKILL.md is even probed for existence (see docstring: the addendum fix this
    #    new code must apply that the duplication-corpus template does not).
    skills_dir = project_root / ".claude" / "skills"
    try:
        skills_dir_is_dir = _probe_is_dir(skills_dir)
    except OSError as e:
        blind_spots.append(f"project skills is_dir failed for hygiene scan: {e}")
        scan_complete = False
        skills_dir_is_dir = False
    if skills_dir_is_dir:
        try:
            skill_entries = sorted(skills_dir.iterdir())
        except OSError as e:
            blind_spots.append(f"project skills listing failed for hygiene scan: {e}")
            scan_complete = False
            skill_entries = []
        skill_dirs = []
        for p in skill_entries:
            try:
                if p.is_dir():
                    skill_dirs.append(p)
            except OSError as e:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T2).
                blind_spots.append(f"project skills child is_dir failed for {p}: {e}")
                scan_complete = False
        for skill_dir in skill_dirs:
            contained, _identity = _project_tier_gate(skill_dir, project_root, containment_stat)
            if not contained:
                _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, skill_dir)
                scan_complete = False
                continue                       # never probe SKILL.md beneath an escaping dir
            skill_md = skill_dir / "SKILL.md"
            present, ok = _safe_exists(skill_md)
            if not ok:
                _append_inaccessible_once(inaccessible, _rel_safe(project_root, skill_md))
                scan_complete = False
                continue
            if present:
                candidates.append(skill_md)

    for fp in candidates:
        key = _physical_key(fp)
        if key in seen_physical:
            continue
        seen_physical.add(key)
        contained, _identity = _project_tier_gate(fp, project_root, containment_stat)
        if not contained:
            _record_out_of_root_ref(out_of_root_refs, seen_refs, project_root, fp)
            scan_complete = False
            continue
        try:
            size = fp.stat().st_size
        except OSError:
            # F3 path 3b, the other silent one: a bare `continue` here records nothing --
            # a file removed or locked between the gate and this stat vanishes with no
            # trace. Record it instead.
            _append_inaccessible_once(inaccessible, _rel_safe(project_root, fp))
            scan_complete = False
            continue
        if size > MAX_FILE_BYTES:
            blind_spots.append(
                f"{_rel(project_root, fp)} exceeds {MAX_FILE_BYTES} bytes; skipped in hygiene corpus.")
            scan_complete = False
            continue
        text, _evidence = _read_project_file(fp, project_root, containment_stat)
        if text is None:
            # Deliberate divergence from `_project_tier_duplication_corpus`, which drops
            # a read failure silently -- reproducing that here would ship a known
            # `inaccessible != clean` gap in new code.
            _append_inaccessible_once(inaccessible, _rel_safe(project_root, fp))
            scan_complete = False
            continue
        corpus.append((_rel(project_root, fp), text))
    return corpus, scan_complete


def scan_duplication(
    root: Path,
    blind_spots: list[str],
    project_root: Path | None = None,
    compose: bool = False,
    out_of_root_refs: list[Any] | None = None,
    *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Candidate near-duplicate pairs by containment coefficient (|A∩B| / min(|A|,|B|))
    over k=8 word shingles — chosen over Jaccard because it correctly flags a short file
    fully subsumed by a longer one (schema.md Note 2). SIGNALS only: this is a candidate
    list. Deciding "one declared home + callers" for a pair is a synthesis-pass JUDGMENT,
    not something this collector decides. `compose=True` (T4/M4) adds the project-tier
    corpus so duplication runs ACROSS BOTH TIERS COMBINED — an operator rule duplicated
    by a project file is a signal ("this repo re-implements an operator rule"). Additive:
    `compose=False` behavior (corpus, pairs shape, output) is byte-for-byte unchanged.

    M11 (SPEC_7 §2): the operator-tier corpus globs come from `profile["duplication_globs"]`
    (defaulting to PROFILE_CLAUDE_CODE, which reproduces _DUP_GLOBS).

    M11 exit gate, Finding 2 (P2): each glob candidate's containment is checked via
    `_contained_or_disclosed` BEFORE its bytes are read (Codex R2-F7's "containment
    refusal precedes the read", generalized here from `_deduped_instruction_files` to
    this profile-glob consumer) -- a `duplication_globs` entry like "rules/*.md" where
    `rules` is a symlink pointing outside `--root` used to be read (and its content
    SAMPLED into a duplication pair's `shared_sample`) with no containment check at all;
    it is now refused and disclosed instead."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    # Generalized skills/coding-team/rules -> skills/*/rules for release portability; the
    # seen_physical dedup below still collapses a rule reachable via multiple glob paths.
    seen_physical = set()
    corpus = []  # [(rel_path, tier, shingle_set), ...]
    try:
        root_stat = os.stat(root)
    except OSError:
        root_stat = None
    for pattern in profile["duplication_globs"]:
        try:
            candidates = sorted(root.glob(pattern))
        except OSError:
            candidates = []
        # TRK-082 T3: an unlistable (but present) directory yields the same empty
        # `candidates` as a genuinely empty one -- disclose the gap.
        _disclose_unlistable_glob(root, pattern, candidates, blind_spots, "duplication scan")
        for fp in candidates:
            # A file reachable via multiple glob paths (a rules/ deploy symlink pointing at
            # its skills/coding-team/rules/ submodule source) is ONE physical file — it must
            # never be compared against itself as a false-positive duplicate pair.
            key = _physical_key(fp)
            if key in seen_physical:
                continue
            seen_physical.add(key)
            if not _contained_or_disclosed(fp, key, root, root_stat,
                                           "duplication corpus file", blind_spots):
                continue
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


def _hooks_body_corpus(root, inaccessible=None, blind_spots=None, *,
                       profile: dict[str, Any] | None = None):
    """Concatenated hooks/*.py + hooks/*.sh bodies, ORIGINAL case, for literal env-flag
    grep and the promotion-candidate hook_covered cross-reference. Returns
    `(corpus, complete)`.

    M11 (SPEC_7 §2): `profile` (defaulting to PROFILE_CLAUDE_CODE) supplies both the
    hooks dir (container_dirs["hooks"]) and the scandir suffix filter
    (_hook_body_suffixes(profile), derived from hook_script_globs). A profile with no
    hooks concept (container_dirs["hooks"] is None) is treated exactly like an ABSENT
    hooks dir below: known-empty, complete=True.
    Caveat: a hook that reads the flag name from a variable rather than a literal string
    (os.environ[SOME_VAR] indirection) is invisible to this substring check — a
    false-positive "phantom" env flag is possible. Best-effort only.

    `complete` (pre-flight exit gate) is the second half of that caveat, and the half
    that was missing: this corpus is the NEGATIVE evidence for env-flag phantom refs
    (`name not in hooks_corpus`), so a hook that could not be READ made live flags look
    unreferenced and they were emitted as the confident `resolved: False` the renderer
    counts as CONFIRMED. `complete=False` means "some hook body is unseen", and
    check_phantom_refs downgrades every env-flag row to the resolved=null / INFERRED
    treatment D2 established. Confidence is a property of the CORPUS, not of one row:
    once any body is unseen, no flag's absence from the blob is provable.

    os.scandir, not Path.glob: glob SWALLOWS a PermissionError on the directory itself
    and yields nothing, which is indistinguishable from an empty hooks dir — the whole
    corpus silently becoming "" is the same defect at maximum blast radius. scandir
    raises, so an unlistable dir is recorded and disclosed like an unreadable file.

    An ABSENT hooks dir is `complete=True`: a harness with no hooks has a known-empty
    corpus, which is a fact, not a blind spot.

    M11 exit gate, Finding 2 (P2, assessed): unlike `scan_duplication`/`_staleness_
    corpus`, `container_dirs["hooks"]` is a single dir role, not a glob list -- but it
    reads file CONTENT the same way, so the same escape applies: `hooks/` matching a
    symlink pointing outside `--root` (Finding 1's fix rejects only a literal absolute/
    '..' role STRING, not a symlink on disk). Each candidate file's containment is now
    checked via `_resolves_inside_root` before its body is read -- same `fp_inside`
    pattern `reconcile_hooks` already uses for the identical hooks/ walk, reused here
    rather than invented twice, INCLUDING that function's choice of sink:
    `test_out_of_root_registered_dispatcher_does_not_drive_reachability`
    (test_collector.py) pins "an out-of-root target is a blind-spot, NOT an inaccessible
    entry (never opened)" for that identical hooks/-symlink shape, so a containment
    REFUSAL is disclosed via `blind_spots` here too, same message text reconcile_hooks
    already uses -- `inaccessible[]` stays reserved for a genuine post-containment read
    failure (the branch immediately below this one)."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    hooks_name = profile["container_dirs"]["hooks"]
    if hooks_name is None:
        return "", True
    parts = []
    hooks_dir = root / hooks_name
    present, ok = _safe_exists(hooks_dir)
    if not ok:
        if inaccessible is not None:
            _append_inaccessible_once(inaccessible, _rel_safe(root, hooks_dir))
        return "", False
    if not present:
        return "", True
    try:
        with os.scandir(hooks_dir) as entries:
            names = sorted(entry.name for entry in entries)
    except OSError:
        if inaccessible is not None:
            _append_inaccessible_once(inaccessible, _rel_safe(root, hooks_dir))
        return "", False
    try:
        root_stat = os.stat(root)
    except OSError:
        root_stat = None
    complete = True
    body_suffixes = _hook_body_suffixes(profile)
    for name in names:
        if not name.endswith(body_suffixes):
            continue
        fp = hooks_dir / name
        key = _physical_key(fp)
        try:
            fp_inside = root_stat is not None and _resolves_inside_root(Path(key), root, root_stat)
        except (OSError, RuntimeError):
            fp_inside = False
        if not fp_inside:
            # Same posture as an unreadable body below: unseen is unseen, whether the
            # cause is a permission error or a resolved path outside --root. Disclosed
            # via blind_spots, not inaccessible -- see the docstring's cross-reference to
            # reconcile_hooks' identical containment refusal for this same hooks/ shape.
            complete = False
            if blind_spots is not None:
                msg = f"hook {name} resolves outside the harness root — not read"
                if msg not in blind_spots:
                    blind_spots.append(msg)
            continue
        text, _status = _read_text(fp)
        if text is None:
            # Covers both a genuinely unreadable regular file and a non-file the glob
            # would have matched (a dir/FIFO named `x.py`). Either way its body is
            # unseen, and over-reporting an unseen body costs one INFERRED downgrade
            # while under-reporting one costs a false confirmed verdict.
            complete = False
            if inaccessible is not None:
                _append_inaccessible_once(inaccessible, _rel_safe(root, fp))
            continue
        parts.append(text)
    return "\n".join(parts), complete


def _staleness_corpus(root, inaccessible, blind_spots, *, profile: dict[str, Any] | None = None):
    """Corpus for phantom-ref + promotion-candidate scanning: rules/*.md,
    skills/coding-team/rules/*.md, and the harness CLAUDE.md — deduped by physical
    identity so a symlinked rule (deploy path + submodule source) is scanned once.

    M11 (SPEC_7 §2): the rule globs come from `profile["rules_globs"]` (defaulting to
    PROFILE_CLAUDE_CODE, which reproduces _STALENESS_RULE_GLOBS) and the root
    instructions file from `profile["top_level_files"]["root_instructions"]` — skipped
    entirely when that role is None (a harness with no root instructions file).

    M11 exit gate, Finding 2 (P2): every `rules_globs` candidate's containment is
    checked via `_contained_or_disclosed` before its bytes are read -- same fix and same
    rationale as `scan_duplication`'s. (The single `root_instructions` file below is
    NOT globbed and is out of this finding's scope -- see Finding 2's writeup: it names
    only the profile GLOB consumers.)"""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    seen = set()
    corpus = []
    paths = []
    try:
        root_stat = os.stat(root)
    except OSError:
        root_stat = None
    # Generalized skills/coding-team/rules -> skills/*/rules for release portability; deduped by
    # physical identity so a symlinked rule (deploy path + sub-skill source) is scanned once.
    for pattern in profile["rules_globs"]:
        try:
            candidates = sorted(root.glob(pattern))
        except OSError:
            candidates = []
        # TRK-082 T3: an unlistable (but present) directory yields the same empty
        # `candidates` as a genuinely empty one -- disclose the gap, matching the
        # adjacent `_contained_or_disclosed` handler's blind_spots channel.
        _disclose_unlistable_glob(root, pattern, candidates, blind_spots, "staleness corpus")
        for fp in candidates:
            key = _physical_key(fp)
            if not _contained_or_disclosed(fp, key, root, root_stat,
                                           "staleness corpus file", blind_spots):
                continue
            paths.append(fp)
    root_instructions_name = profile["top_level_files"]["root_instructions"]
    if root_instructions_name is not None:
        claude = root / root_instructions_name
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
    blind_spots: list[str] | None = None,
    *,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Backtick-quoted path and env-flag tokens that don't resolve to anything real. A
    path OUTSIDE --root is reported as kind="external" (INFERRED, resolved: null) — the
    collector never claims a file outside its scanned scope is phantom; it genuinely
    cannot see it either way, so it only classifies, never asserts absence.

    M11 (SPEC_7 §2): `profile` is forwarded to _hooks_body_corpus so a non-default profile's
    hooks corpus is read from ITS hooks dir, not PROFILE_CLAUDE_CODE's. This function's own
    literal homes (the `~/.claude/` prefix, the commands/ + skills/ slash-command resolution
    paths below) are deliberately deferred — generalizing THEM is out of scope for this
    milestone.

    M11 exit gate, Finding 2 (P2): `blind_spots` (additive, defaults to None for a caller
    with no such list handy) is forwarded to `_hooks_body_corpus` so a hooks/ symlink
    escaping --root is disclosed there, not silently dropped -- see that function's own
    docstring for why it is `blind_spots`, not `inaccessible`, for this specific case."""
    refs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    hooks_corpus, hooks_corpus_complete = _hooks_body_corpus(
        root, inaccessible, blind_spots, profile=profile)
    # S6b.M2.1: computed ONCE per call, not per token -- reused by BOTH candidate loops
    # below to filter out-of-root candidates before any stat. A `root` that cannot be
    # stat'd makes containment undecidable for every candidate, so `_in_root` skips them
    # all (same "cannot determine -> not proven inside" posture as the fp_inside table
    # in reconcile_hooks, collector.py:1599-1607).
    try:
        root_stat = os.stat(root)
    except OSError:
        root_stat = None

    def _in_root(candidate: Path) -> bool:
        # `_resolves_inside_root`'s fast lexical `root in candidate.parents` check
        # compares RAW path components without collapsing "..". A joined-but-unresolved
        # candidate like `root / "../active-repo/x.md"` therefore walks back to a literal
        # ancestor equal to `root` after popping one trailing component per ".." segment
        # -- and returns True -- even though the path actually RESOLVES outside root
        # (verified: `Path('/r/claude') in (Path('/r/claude') / '../x').parents` is
        # True). Calling `_resolves_inside_root` on the raw join would silently defeat
        # this fix. `validate_write_target` (collector.py:330) already carries the same
        # lexical defense for its own containment check ("tested both LEXICALLY
        # (normpath, catches a textual '..' that still exits a root)"); reused here
        # rather than inventing a second predicate.
        normalized = Path(os.path.normpath(str(candidate)))
        return root_stat is not None and _resolves_inside_root(normalized, root, root_stat)

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
                # S6b.M2.1: a relative token (no leading `/` or `~`, so it skipped the
                # `external` branch above) can still escape --root through a `../`
                # segment. Filter to in-root candidates BEFORE any `_safe_exists` stat --
                # stat-ing an out-of-root candidate at all is what turned row-presence
                # into a filesystem existence oracle (a `../etc/passwd`-shaped token could
                # probe anything readable by the process). If NONE of a token's candidates
                # resolve inside root, the docstring's own promise applies: classify as
                # `external`, never assert absence about a path this scan cannot see.
                in_root_candidates = [c for c in candidates if _in_root(c)]
                if not in_root_candidates:
                    key = (rel_path, norm, "external")
                    if key not in seen:
                        seen.add(key)
                        refs.append({"source": rel_path, "ref": norm, "kind": "external",
                                     "resolved": None, "evidence": "INFERRED"})
                    continue
                handled = False
                for candidate in in_root_candidates:
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
                # D4 (S6 §7.2), POST-probe. Everything below runs ONLY on tokens the
                # probe above already failed to resolve, so no resolution can be lost.
                #
                # (1) The `:<line>` strip, applied to the PROBE TARGET ONLY — the
                # reported `ref` keeps the operator's original citation so they can find
                # the text they wrote.
                #
                # THE STRIPPED TARGET IS A PATH THAT WAS NEVER PROBED (Codex gate P2-3).
                # The candidate loop above probed the UNSTRIPPED citation
                # `rules/x.md:12`; `rules/x.md` is a DIFFERENT path, so nothing upstream
                # established its accessibility. It therefore gets the same tri-state
                # treatment every other probe in this function gets: `_safe_exists`, NOT a
                # bare `is_file()`. A bare `is_file()` returns False for an unreadable
                # target -- a symlink whose target cannot be read is the demonstrated case
                # -- and we would emit `resolved: False, evidence: VERIFIED` about
                # something we never verified. That is the binding invariant of this whole
                # skill: INACCESSIBLE IS NOT CLEAN; never report a surface clean because
                # we could not see it.
                #
                # `is_file()` is still required, but only AFTER `_safe_exists` returns
                # ok=True -- at that point the stat has already succeeded, so is_file()
                # is only distinguishing file from directory and cannot swallow an error.
                # That keeps S-9 closed (a real DIRECTORY can never make an
                # extension-bearing token "resolve") without collapsing the tri-state.
                # The narrowing stays scoped INSIDE this branch: applying it to every
                # slash-bearing ref would turn legitimate directory references
                # (`skills/harness-map/`, `.claude/hooks`) into false phantom rows,
                # INVERTING the defect D4 exists to fix (finding #14).
                #
                # LIMITATION, disclosed not hidden: stripping `:999999` and probing only
                # the FILE makes a stale LINE reference disappear from the table. The row
                # dropping is correct — the file does exist, and existence is what
                # phantom_refs measures — but line-range validity is UNKNOWN and is never
                # implied as checked. See the blind spot emitted in build_document and the
                # tile-drawer note in render_html. A line-range validator is separate
                # scope and is not built here.
                if _LINE_SUFFIX_RE.search(norm):
                    stripped = _LINE_SUFFIX_RE.sub("", norm)
                    stripped_candidates = [root / stripped]
                    if str(src_dir) not in (".", ""):
                        stripped_candidates.append(root / src_dir / stripped)
                    # S6b.M2.1: `stripped` is a PATH NEVER PROBED by the candidate loop
                    # above (that loop probed the unstripped citation) -- so it needs its
                    # own containment filter, same reasoning as the loop above. Without
                    # this, a `../x.md:1` token bypassed the unstripped loop's oracle
                    # fix (the unstripped candidate never matches a real filename, so
                    # `handled` stays False) and reached an unguarded stat here instead.
                    in_root_stripped_candidates = [c for c in stripped_candidates if _in_root(c)]
                    if not in_root_stripped_candidates:
                        key = (rel_path, norm, "external")
                        if key not in seen:
                            seen.add(key)
                            refs.append({"source": rel_path, "ref": norm, "kind": "external",
                                         "resolved": None, "evidence": "INFERRED"})
                        continue
                    stripped_handled = False
                    stripped_escaped = False
                    for candidate in in_root_stripped_candidates:
                        present, ok = _safe_exists(candidate)
                        if not ok:
                            # Unreadable: record it and DROP the token. Same policy as the
                            # candidate loop above -- inaccessible is not retired, and it
                            # is certainly not "confirmed missing".
                            _append_inaccessible_once(inaccessible, _rel_safe(root, candidate))
                            stripped_handled = True
                            break
                        if not present:
                            continue
                        # S6b.M7 SECURITY FIX: `candidate` is lexically inside root (that
                        # is how it reached `in_root_stripped_candidates`), but if it is a
                        # symlink -- or sits beneath one -- its TARGET can still resolve
                        # OUTSIDE root; `_in_root`'s normpath-based check is lexical and
                        # cannot see through a symlink. The old code went straight to a
                        # bare `candidate.is_file()`, which FOLLOWS the link and turns the
                        # outside target's existence into an oracle: present-but-dangling
                        # and present-but-real are indistinguishable from `_safe_exists`
                        # alone, but `is_file()` told them apart by touching a path this
                        # scan has no standing to probe. Resolve ONCE here and reuse that
                        # single result for both the containment re-check and the file-type
                        # check below -- resolving again after this would reopen the same
                        # TOCTOU window the tri-state helpers elsewhere in this module are
                        # built to avoid.
                        try:
                            resolved_target = candidate.resolve()
                        except OSError:
                            _append_inaccessible_once(inaccessible, _rel_safe(root, candidate))
                            stripped_handled = True
                            break
                        if not _resolves_inside_root(resolved_target, root, root_stat):
                            # The symlink chain exits root. Whether the outside target
                            # exists or not must produce the SAME row (that equality IS
                            # the anti-oracle property), so this candidate is treated
                            # exactly like a lexically out-of-root one: never probed for
                            # file-ness, never asserted resolved OR missing. Another
                            # candidate for this same token may still resolve normally, so
                            # this falls through to the next candidate rather than
                            # immediately reporting.
                            stripped_escaped = True
                            continue
                        # Resolved and CONTAINED: safe to ask whether it names a file.
                        # `is_file()` on the already-resolved path keeps S-9 closed (a
                        # real DIRECTORY still does not make an extension-bearing token
                        # "resolve") without re-touching the original symlink chain.
                        # `resolved_target` sits under a validated in-root ancestor chain
                        # for `candidate`, but is a DIFFERENT path post-`resolve()` and can
                        # itself sit beneath an unreadable in-root ancestor -- reachable
                        # WITHOUT a race. An OSError here must not become a distinguishable
                        # outcome (that would reopen the existence oracle the block above
                        # closes), so this candidate is recorded inaccessible and handled
                        # exactly like the resolve() failure above: never asserted resolved
                        # OR missing.
                        try:
                            is_file = _probe_is_file(resolved_target)
                        except OSError:
                            _append_inaccessible_once(inaccessible, _rel_safe(root, candidate))
                            stripped_handled = True
                            break
                        if is_file:
                            stripped_handled = True
                            break
                    if stripped_handled:
                        continue
                    if stripped_escaped:
                        # Every candidate that had a chance to resolve escaped root; none
                        # resolved inside it. Same footing as any other out-of-root
                        # candidate (collector.py:3536-3542): kind="external",
                        # resolved=None, evidence="INFERRED" -- never resolved: False,
                        # evidence: VERIFIED, which would assert a confirmed negative
                        # about a target this scan cannot see.
                        key = (rel_path, norm, "external")
                        if key not in seen:
                            seen.add(key)
                            refs.append({"source": rel_path, "ref": norm, "kind": "external",
                                         "resolved": None, "evidence": "INFERRED"})
                        continue
                # (2) Shape classification, replacing the bare `path` emission. The
                # `template` branch must not `continue` past its append — zero rows may
                # disappear except by the genuine resolution above.
                #
                # `path` is the default and the honest one: it reports what was actually
                # probed. A shape kind may only pre-empt it when the token PROVABLY names
                # no target — which is true of a stencil and was NOT true of the deferred
                # `refspec` arm (S6c, DEVIATION 5).
                kind: str
                resolved: bool | None
                evidence: str
                if _TEMPLATE_REF_RE.search(norm):
                    kind, resolved, evidence = "template", None, "INFERRED"
                else:
                    kind, resolved, evidence = "path", False, "VERIFIED"
                key = (rel_path, norm, kind)
                if key not in seen:
                    seen.add(key)
                    refs.append({"source": rel_path, "ref": norm, "kind": kind,
                                 "resolved": resolved, "evidence": evidence})
                continue
            env_match = _ENV_FLAG_NAME_RE.match(token)
            if env_match:
                name = env_match.group(1)
                if _ENV_FLAG_SHAPE_RE.search(name) and name not in hooks_corpus:
                    key = (rel_path, name, "env_flag")
                    if key not in seen:
                        seen.add(key)
                        # Pre-flight exit gate: `resolved: False` here is a CONFIRMED
                        # negative ("no hook reads this flag"), and the ONLY evidence for
                        # it is the hooks corpus. When that corpus is incomplete the
                        # negative is unprovable, so the row takes the resolved=null /
                        # INFERRED treatment D2 established for slash commands: still
                        # surfaced for review, never asserted broken, and never counted
                        # as confirmed by the renderer's BROKEN band. The unseen hook is
                        # in inaccessible[] alongside it (recorded by _hooks_body_corpus).
                        refs.append({"source": rel_path, "ref": name, "kind": "env_flag",
                                     "resolved": False if hooks_corpus_complete else None,
                                     "evidence": "INFERRED"})
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
    *,
    profile: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Prose in an instruction file that reads like a hard rule (NEVER/ALWAYS/must, a
    numeric cap, a required-file assertion) but may have no corresponding hook enforcing
    it. Advisory SIGNALS only — synthesis proposes extending an EXISTING covered hook
    before creating a new one; this collector never makes that judgment itself.

    M11 (SPEC_7 §2): `profile` is forwarded to _hooks_body_corpus so a non-default profile's
    hooks corpus is read from ITS hooks dir, not PROFILE_CLAUDE_CODE's; corpus_files itself
    already arrives pre-built by this function's caller, so nothing else here needs to read
    the profile directly."""
    candidates: list[dict[str, Any]] = []
    # `complete` is unused HERE on purpose: every promotion candidate already ships as
    # evidence=INFERRED and `hook_covered` is an advisory hint, not a verdict, so there is
    # no confident negative to downgrade. `inaccessible` is not threaded in either — the
    # phantom-ref pass records the very same unreadable hooks, and _append_inaccessible_once
    # dedupes across callers, so passing it would only duplicate that work.
    hooks_corpus_lower = _hooks_body_corpus(root, profile=profile)[0].lower()
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


def _hook_test_stems(root, errors, *, profile: dict[str, Any] | None = None):
    """Normalized (snake_case) stems named by test files under hooks/tests/ and
    skills/*/hooks/tests/ (generalized from the coding-team-only scope for release
    portability) — "test_guard.py" and "guard_test.py" both yield "guard". Read-only,
    single-level glob per dir (no recursion needed: hook tests live directly in these
    known locations). `errors` is the shared build_document errors[] list — an
    inaccessible ancestor is disclosed there rather than silently swallowed.

    M11 (SPEC_7 §2): the root-level test dir and the skills container come from
    `profile["container_dirs"]` (defaulting to PROFILE_CLAUDE_CODE); either role may be
    None (no such surface), in which case that source is skipped entirely.

    M11 exit gate, Finding 2 (P2, assessed -- NOT gated): this function never reads a
    file's CONTENT (no `_read_text`/`_read_checked` call anywhere below) -- only
    `Path.stem`, `.is_dir()`, and `.iterdir()`/`.glob("*.py")` names. Its return value
    (`stems`, a set of normalized basenames) never reaches the report as a path or as
    text either: the sole consumer, `_detect_hook_test_coverage`, folds it into a
    per-IN-ROOT-hook-script boolean (`has_test`) and discards the stems themselves. A
    `hooks/tests/` symlinked outside `--root` could at most flip an in-root hook's
    `has_test` flag via a same-named outside test file -- no outside path or outside
    content is ever disclosed or read -- so this does not meet the "a read or a name
    reaching output can escape" bar the sibling functions above were gated for, and is
    intentionally left ungated."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    stems = set()
    # Generalized skills/coding-team/hooks/tests -> skills/*/hooks/tests for release portability.
    # `stems` is a set, so union order is irrelevant; baseline-stable because coding-team is the
    # only sub-skill with a hooks/tests dir on this harness.
    test_dirs = []
    hook_tests_name = profile["container_dirs"]["hook_tests"]
    if hook_tests_name is not None:
        test_dirs.append(root / hook_tests_name)
    skills_name = profile["container_dirs"]["skills"]
    if skills_name is not None:
        skills_root = root / skills_name
        try:
            skills_root_is_dir = _probe_is_dir(skills_root)
        except OSError as e:
            errors.append(f"skills is_dir failed for {skills_root}: {e}")
            skills_root_is_dir = False
        if skills_root_is_dir:
            try:
                skill_entries = sorted(skills_root.iterdir())
            except OSError as e:
                # TRK-050 T5 F2: scan-named prefix -- see the always-loaded sub-rules scan
                # comment above for why this must be distinguishable from the byte-
                # identical message _detect_skill_test_coverage emits for the same dir.
                errors.append(f"hook test stems skills listing failed for {skills_root}: {e}")
                skill_entries = []
            skill_dirs = []
            for p in skill_entries:
                try:
                    if p.is_dir():
                        skill_dirs.append(p)
                except OSError as e:
                    # A single unlistable/unstat-able child must not abort the whole
                    # comprehension and discard every sibling with it (TRK-050 T1).
                    errors.append(f"hook test stems skills child is_dir failed for {p}: {e}")
            for skill_dir in skill_dirs:
                # M11 (SPEC_7 §2): the per-skill hooks/tests join stays a literal --
                # re-deriving it from hook_test_globs's "skills/*/..." entry is
                # deliberately deferred (out of scope for this task).
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
            test_files = sorted(test_dir.glob("*.py"))
        except OSError:
            test_files = []
        else:
            _disclose_unlistable_glob(test_dir, "*.py", test_files, errors, "hook test stems")
        for f in test_files:
            stem = f.stem
            if stem.startswith("test_"):
                stems.add(stem[len("test_"):])
            elif stem.endswith("_test"):
                stems.add(stem[:-len("_test")])
    return stems


def _detect_hook_test_coverage(root, errors, *, profile: dict[str, Any] | None = None):
    """PRESENCE-only signal: does a hook script have a matching test file? NOT adequacy —
    a hooks/tests/test_x.py with a single trivial assertion counts as covered, same as a
    thorough suite (the "6 of 66" reality). Symlinked hooks are deduped by physical
    identity so one script counts once even if reachable via multiple glob paths.

    M11 (SPEC_7 §2): `profile` (defaulting to PROFILE_CLAUDE_CODE) is threaded into
    both helpers below."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    disk_files = _hook_disk_files(root, profile=profile)
    test_stems = _hook_test_stems(root, errors, profile=profile)

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


def _skill_has_test_asset(skill_dir, errors=None):
    """PRESENCE-only signal (see _detect_hook_test_coverage docstring): a tests/ dir, an
    evals/ dir, or any test_*.py / *_eval.* file anywhere under the skill dir. MEASURED
    on CPython 3.11.14 against a real EACCES tree: unlike _safe_exists, Path.is_dir()
    does NOT swallow PermissionError (only ENOENT-family errors) — a permission-denied
    skill dir raises here rather than reading False, so it is already surfaced as
    inaccessible by collect_descriptions()/collect_on_demand(); this function must only
    avoid crashing the whole run, not duplicate that reporting. This is version-
    dependent and NOT re-verified on every interpreter: on 3.14, Path.is_dir() is
    documented/expected to swallow PermissionError too (not measured here — only
    3.11.14 is installed on this machine). If that holds, the "already surfaced
    elsewhere" justification above stops applying on 3.14 — a permission-denied skill
    dir would read as a silent False here, duplicating no report, because there would be
    no report to duplicate.

    The recursive test_*.py / *_eval.* search walks _iter_descendant_dirs(skill_dir) — the
    SAME pruned descendant walk the watcher uses (Codex r4 fix) — rather than
    Path.rglob(), which would descend into generated subtrees like node_modules/.venv that
    the watcher does not observe. This keeps the two walks equal BY CONSTRUCTION: a
    test/eval file this function can see is always inside a directory the watcher also
    yields, and a test/eval file planted under a pruned dir (e.g. node_modules) is
    intentionally excluded from BOTH signals.

    `errors` (S7.M3c, optional): os.walk's default onerror silently discards a
    per-directory listing failure partway through the descendant walk, which would
    otherwise make an unreadable nested subtree return a DETERMINED has_test: False
    rather than surfacing the gap — this is NOT the same swallow the tests/evals check
    above documents (that one is a top-level check already reported elsewhere; this one
    is unreported and partway through an unbounded recursive walk), so it is recorded
    here instead when a caller supplies a list. Defaults to None (discarded) so this
    function's pre-existing single-argument call shape keeps working."""
    try:
        if (skill_dir / "tests").is_dir() or (skill_dir / "evals").is_dir():
            return True
    except OSError:
        pass

    def _record_walk_error(exc):
        if errors is not None:
            errors.append(f"skill descendant walk failed under {skill_dir}: {exc}")

    for d in _iter_descendant_dirs(skill_dir, onerror=_record_walk_error):
        try:
            found = next(d.glob("test_*.py"), None)
        except OSError:
            pass
        else:
            if found is not None:
                return True
            if errors is not None:
                _disclose_unlistable_glob(d, "test_*.py", [], errors, "skill test coverage")
        try:
            found = next(d.glob("*_eval.*"), None)
        except OSError:
            pass
        else:
            if found is not None:
                return True
            if errors is not None:
                _disclose_unlistable_glob(d, "*_eval.*", [], errors, "skill test coverage")
    return False


def _detect_skill_test_coverage(root, errors, *, profile: dict[str, Any] | None = None):
    """M11 (SPEC_7 §2): the skills container comes from `profile["container_dirs"]["skills"]`
    (defaulting to PROFILE_CLAUDE_CODE); a profile with no skills concept (None) yields []."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    skills_name = profile["container_dirs"]["skills"]
    if skills_name is None:
        return []
    skills_dir = root / skills_name
    try:
        skills_dir_is_dir = _probe_is_dir(skills_dir)
    except OSError as e:
        # is_dir() re-raises EACCES from an unreadable ancestor; an escape here aborts
        # detect_test_coverage and, via build_document, the entire report.
        errors.append(f"skills is_dir failed for {skills_dir}: {e}")
        return []
    if not skills_dir_is_dir:
        return []
    try:
        skill_entries = sorted(skills_dir.iterdir())
    except OSError as e:
        # TRK-050 T5 F2: scan-named prefix -- see the always-loaded sub-rules scan
        # comment above for why this must be distinguishable from the byte-identical
        # message _hook_test_stems emits for the same dir.
        errors.append(f"skill test coverage skills listing failed for {skills_dir}: {e}")
        skill_entries = []
    skill_dirs = []
    for p in skill_entries:
        try:
            if p.is_dir():
                skill_dirs.append(p)
        except OSError as e:
            # A single unlistable/unstat-able child must not abort the whole
            # comprehension and discard every sibling with it (TRK-050 T1).
            errors.append(f"skill test coverage skills child is_dir failed for {p}: {e}")
    return [{"name": d.name, "has_test": _skill_has_test_asset(d, errors)} for d in skill_dirs]


def detect_test_coverage(
    root: Path, on_demand: dict[str, Any], errors: list[str], *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Whether each hook script and each skill has an associated test ASSET — a
    PRESENCE check, not an adequacy check (the "6 of 66" reality: a tests/ dir holding
    one trivial assertion counts as covered exactly like a thorough suite). Cross-links
    the same per-skill has_test verdict onto on_demand["skills"] (mutated in place) by
    skill name, so both sections agree instead of on_demand carrying its own narrower
    (tests/-dir-only) check.

    M11 (SPEC_7 §2): `profile` (defaulting to PROFILE_CLAUDE_CODE) is threaded into
    both helpers below."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    hooks_result = _detect_hook_test_coverage(root, errors, profile=profile)
    skills_result = _detect_skill_test_coverage(root, errors, profile=profile)

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
        # TRK-025 T3: the coverage denominator behind the two orphan counts above — an
        # "examined" hook command is one _script_from_command could actually classify
        # ("resolved" or "no_script"); "hook_commands_total" is every command registered
        # in settings.json, including any "unparsed" one a future parser limit leaves
        # out of the orphan counts entirely. Additive fields, not part of the original
        # eight-metric headline diff unit (schema.md Note, HEADLINE_KEYS) — a future
        # unparsed command now shows up as a shrinking numerator instead of vanishing
        # silently.
        "hook_commands_examined": (hooks_section["commands_resolved"]
                                    + hooks_section["commands_no_script"]),
        "hook_commands_total": hooks_section["commands_total"],
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


def _iter_descendant_dirs(base, onerror=None):
    """Yield `base` and every non-pruned directory beneath it (each membership-watchable).
    _skill_has_test_asset (Codex r4 fix) now SHARES this exact walk for its recursive
    test_*.py / *_eval.* glob search instead of Path.rglob() — the two are equal BY
    CONSTRUCTION, not by a duplicated constant that could drift: a test/eval file added at
    ANY non-pruned depth flips a skill's has_test AND is watched, while one planted under a
    pruned dir (node_modules, .venv, caches, ...) is intentionally invisible to BOTH.

    `onerror` (optional): os.walk's default `onerror=None` SILENTLY DISCARDS a
    per-directory listing failure partway through the walk, making an unreadable
    subtree indistinguishable from a genuinely empty one to every caller. Pass a
    callback here to record that failure (S7.M3c) instead of losing it; the
    watcher call site (iter_input_paths, consumed by serve.py) omits it and keeps
    its prior silent-on-walk-error behavior unchanged.

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
    for dirpath, dirnames, _ in os.walk(base, followlinks=False, onerror=onerror):
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


def _compose_project_input_paths(project_root, errors=None):
    """Compose-mode project-tier watch surface (T8): a SUPERSET of every project-tier read
    `_walk_project_tier` (T2 -- repo-root/nested CLAUDE.md + CLAUDE.local.md via the
    containment-root walk), `_walk_project_tier_nodes`/`_project_tier_duplication_corpus`
    (T4/M4 -- `.claude/{rules,agents,commands,skills}`), and `_compose_hooks` (T5 -- a
    project/local settings.json hook command's resolved script existence) add to the
    collector output. Returns a `set` of ABSOLUTE `Path`s, every one lexically under
    `project_root` -- serve.py's watcher relies on that lexical-containment invariant to
    tier-tag the watched set (T8's `(path, tier)` contract) without a second stat pass.

    `errors` (TRK-050 T2, optional): the skills-dir listing below used to swallow an
    OSError into `skill_dirs = []` with no signal at all. Recorded here instead when a
    caller supplies a list. Defaults to None (discarded) so this function's pre-existing
    single-argument call shape keeps working."""
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
        d = project_root / rel_dir
        try:
            dir_matches = list(d.glob(pattern))
        except OSError:
            continue
        paths.update(dir_matches)
        if errors is not None:
            _disclose_unlistable_glob(d, pattern, dir_matches, errors,
                                       "compose project duplication")
    # TRK-050 T5 F1: an ABSENT skills dir is a normal, valid project layout -- iterdir()
    # raising ENOENT for a never-created dir must record NOTHING, only a PRESENT-but-
    # unlistable dir is a real disclosure-worthy failure. `_probe_is_dir` swallows the
    # ENOENT family and returns False for "absent", but re-raises EACCES from an
    # unreadable ANCESTOR -- that escape is itself recorded, matching the house pattern
    # walk_always_loaded/_hook_test_stems/_detect_skill_test_coverage already use.
    compose_skills_dir = harness_root / "skills"
    skill_entries: list[Path] = []
    try:
        compose_skills_dir_is_dir = _probe_is_dir(compose_skills_dir)
    except OSError as e:
        if errors is not None:
            errors.append(f"compose project skills is_dir failed for {compose_skills_dir}: {e}")
        compose_skills_dir_is_dir = False
    if compose_skills_dir_is_dir:
        try:
            skill_entries = sorted(compose_skills_dir.iterdir())
        except OSError as e:
            if errors is not None:
                errors.append(f"compose project skills listing failed for {compose_skills_dir}: {e}")
            skill_entries = []
    skill_dirs = []
    for p in skill_entries:
        try:
            if p.is_dir():
                skill_dirs.append(p)
        except OSError as e:
            # A single unlistable/unstat-able child must not abort the whole
            # comprehension and discard every sibling with it (TRK-050 T2).
            if errors is not None:
                errors.append(f"compose project skills child is_dir failed for {p}: {e}")
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
            script_path, _note, _kind = _script_from_command(command, project_root)
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
    root: Path, project_root: Path | None = None, compose: bool = False, *,
    profile: dict[str, Any] | None = None, errors: list[str] | None = None,
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
    watched/read surface for the SAME `project_root` argument stays byte-identical.

    `errors` (TRK-050 T2, optional): the projects/*/memory and skills listings below used
    to swallow an OSError into an empty dir list with no signal at all; threaded through to
    `_compose_project_input_paths` in compose mode too. Recorded here instead when a caller
    supplies a list. Defaults to None (discarded) so this function's pre-existing call shape
    (positional root/project_root/compose, keyword-only profile) keeps working.

    TRK-050 T5 F3, disclosed honestly: no PRODUCTION caller supplies `errors` yet. `main()`
    calls this during argument validation, before a document (or an errors[] list) exists;
    serve.py's watcher snapshot function returns a bare dict of Paths with no error channel
    of its own. Both are exercised only via `errors=None`, so every per-child/listing
    failure this parameter can now report is disclosed exclusively in tests today — a real
    unlistable projects/skills dir during a live watch is still silently treated as absent
    by every current caller. Wiring a disclosure surface into the watcher is a design change
    tracked separately, not done by this fix."""
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
    root = Path(root)
    paths = set()

    # -- concrete top-level files (content matters) --
    #   CLAUDE.md              walk_always_loaded + _staleness_corpus
    #   settings.json          parse_settings -> permissions, config, hook registrations
    #   memory/MEMORY.md       walk_always_loaded (root stub index)
    #   plugins/*.json         collect_config._read_json_name_list (two fixed names)
    # M11 (SPEC_7 §2): sourced from profile["top_level_files"]; a None role is skipped.
    top_level_files = profile["top_level_files"]
    for role in ("root_instructions", "settings", "memory_index",
                 "plugin_marketplaces", "plugin_installed"):
        if top_level_files[role] is not None:
            paths.add(root / top_level_files[role])

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
    #   agents/hooks/hooks-tests/rules/commands/memory : globbed membership below
    # M11 (SPEC_7 §2): sourced from profile["container_dirs"] -- sorted() over the dict's
    # VALUES (never its insertion order) so the watched set stays deterministic across
    # PYTHONHASHSEED (CLAUDE.md rule 9); a None role (no such surface) is skipped.
    for d in sorted(v for v in profile["container_dirs"].values() if v):
        paths.add(root / d)

    # -- glob-based content files: the SAME pattern tuples the collector scans consume, so the
    #    read surface and the watched surface are one definition (see _*_GLOBS above / the
    #    profile's glob keys). Covers rules, skills/*/rules, skills SKILL.md (top + nested),
    #    phases/prompts/agents md, commands, agents, hooks/*.py|*.sh, and hooks/tests +
    #    skills/*/hooks/tests scripts. --
    # sorted() (CLAUDE.md rule 9): a bare set() of pattern STRINGS iterates in
    # PYTHONHASHSEED-dependent order. `paths` is sorted at return regardless of this
    # loop's order, but TRK-082 T3's glob-listability disclosure now appends to `errors`
    # INSIDE this loop -- that record order must not depend on hash seed either, even
    # though no production caller supplies `errors` today (TRK-086 is the open ticket to
    # wire one in, and must not inherit this order silently).
    for pattern in sorted(set(
            _instruction_globs(profile) + tuple(profile["duplication_globs"])
            + tuple(profile["rules_globs"]) + tuple(profile["hook_script_globs"])
            + tuple(profile["hook_test_globs"]))):
        try:
            pattern_matches = list(root.glob(pattern))
        except OSError:
            continue
        paths.update(pattern_matches)
        # TRK-082 T3: an unlistable (but present) directory for this pattern is
        # indistinguishable from a genuinely empty one via glob() alone -- disclose it,
        # matching the `errors` channel this function already uses for every other
        # listing failure below (optional: no production caller supplies `errors` yet,
        # per the docstring's TRK-050 T5 F3 note).
        if errors is not None:
            _disclose_unlistable_glob(root, pattern, pattern_matches, errors,
                                       "watcher inputs")

    # -- projects/*/memory: MEMORY.md index (walk_always_loaded / conditional_variants) plus,
    #    for the active project, memory bodies (collect_on_demand). Yield each memory dir
    #    (membership) + every *.md (content); MEMORY.md matches the *.md glob. --
    # M11 (SPEC_7 §2): container_dirs["projects"], defaulting to "projects"; None -> no
    # projects concept, so this whole block is a no-op.
    projects_name = profile["container_dirs"]["projects"]
    slug_dirs = []
    if projects_name is not None:
        projects_dir = root / projects_name
        # TRK-050 T5 F1: an ABSENT projects dir is a normal, valid harness (no projects
        # registered yet) -- iterdir() raising ENOENT for a never-created dir must record
        # NOTHING; only a PRESENT-but-unlistable dir is a real disclosure-worthy failure.
        # `_probe_is_dir` swallows the ENOENT family (absent -> False) but re-raises EACCES
        # from an unreadable ANCESTOR, which is itself recorded here.
        slug_entries: list[Path] = []
        try:
            projects_dir_is_dir = _probe_is_dir(projects_dir)
        except OSError as e:
            if errors is not None:
                errors.append(f"watcher projects is_dir failed for {projects_dir}: {e}")
            projects_dir_is_dir = False
        if projects_dir_is_dir:
            try:
                slug_entries = sorted(projects_dir.iterdir())
            except OSError as e:
                if errors is not None:
                    errors.append(f"watcher projects listing failed for {projects_dir}: {e}")
                slug_entries = []
        for p in slug_entries:
            try:
                if p.is_dir():
                    slug_dirs.append(p)
            except OSError as e:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T2).
                if errors is not None:
                    errors.append(f"watcher projects child is_dir failed for {p}: {e}")
    for slug_dir in slug_dirs:
        mem_dir = slug_dir / "memory"
        paths.add(mem_dir)
        try:
            mem_matches = list(mem_dir.glob("*.md"))
        except OSError:
            continue
        paths.update(mem_matches)
        if errors is not None:
            _disclose_unlistable_glob(mem_dir, "*.md", mem_matches, errors,
                                       "watcher projects memory")

    # -- per-skill dirs: each skill dir + ALL descendant dirs (membership) so a test_*.py /
    #    *_eval.* added at any depth flips has_test (_skill_has_test_asset rglob). The skill's
    #    concrete CONTENT files are already covered by the _*_GLOBS union above. --
    # M11 (SPEC_7 §2): container_dirs["skills"], defaulting to "skills"; None -> no skills
    # concept, so this whole block is a no-op.
    skills_name = profile["container_dirs"]["skills"]
    skill_dirs = []
    if skills_name is not None:
        watcher_skills_dir = root / skills_name
        # TRK-050 T5 F1: an ABSENT skills dir is a normal, valid harness -- see the
        # matching comment on the projects listing above for the full rationale.
        skill_entries: list[Path] = []
        try:
            watcher_skills_dir_is_dir = _probe_is_dir(watcher_skills_dir)
        except OSError as e:
            if errors is not None:
                errors.append(f"watcher skills is_dir failed for {watcher_skills_dir}: {e}")
            watcher_skills_dir_is_dir = False
        if watcher_skills_dir_is_dir:
            try:
                skill_entries = sorted(watcher_skills_dir.iterdir())
            except OSError as e:
                if errors is not None:
                    errors.append(f"watcher skills listing failed for {watcher_skills_dir}: {e}")
                skill_entries = []
        for p in skill_entries:
            try:
                if p.is_dir():
                    skill_dirs.append(p)
            except OSError as e:
                # A single unlistable/unstat-able child must not abort the whole
                # comprehension and discard every sibling with it (TRK-050 T2).
                if errors is not None:
                    errors.append(f"watcher skills child is_dir failed for {p}: {e}")
    for skill_dir in skill_dirs:
        for sub in _iter_descendant_dirs(skill_dir):
            paths.add(sub)

    # -- hooks/ dir + ALL descendant dirs (membership): reconcile_hooks stat()s the resolved
    #    script for each registered command, and _script_from_command can resolve to a script
    #    NESTED under hooks/<subdir>/. The shallow hooks/*.py|*.sh globs above miss that depth,
    #    so watch hooks/ recursively — the same _iter_descendant_dirs mechanism used for skills. --
    # M11 (SPEC_7 §2): container_dirs["hooks"], defaulting to "hooks"; None -> no hooks
    # concept, so nothing to watch recursively.
    hooks_name = profile["container_dirs"]["hooks"]
    if hooks_name is not None:
        for sub in _iter_descendant_dirs(root / hooks_name):
            paths.add(sub)

    # -- resolved hook-script paths from REGISTERED settings.json commands: reconcile_hooks
    #    stat()s exactly these. Reuse _script_from_command (its resolution logic is the single
    #    source of truth) and yield each script that resolves UNDER root — a command may point
    #    OUTSIDE hooks/ (e.g. "./scripts/x.py"). A command resolving to an ABSOLUTE path outside
    #    root is un-watchable via a root walk (disclosed in the docstring's blind-spot list); the
    #    settings.json edit that registers it IS watched (settings.json is yielded above). --
    settings, _parsed_ok = parse_settings(root, [], [], profile=profile)
    root_resolved = root.resolve()
    for command in _iter_hook_commands(settings):
        script_path, _note, _kind = _script_from_command(command, root, profile=profile)
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
        paths.update(_compose_project_input_paths(project_root, errors=errors))

    return sorted(paths, key=str)


def _metric_quality(inaccessible: list[dict[str, Any]],
                     duplication_section: dict[str, Any]) -> dict[str, str]:
    """Per-metric measurement-state (S6c §6.5a axis 3): `complete | partial | saturated`
    for every key in METRIC_DEFINITIONS, iterating `sorted(METRIC_DEFINITIONS)` for
    cross-`PYTHONHASHSEED` determinism (`unmeasured` is set only by `_empty_document`, on
    the crash/profile-rejection envelope paths -- this function never emits it).

    `saturated` wins over `partial` for `duplicate_pair_count` at `len(pairs) >= MAX_PAIRS`
    -- the count is capped, not merely large, so a `partial` label there would hide a
    STRUCTURAL ceiling behind an accessibility caveat. A metric with no entry in
    `_METRIC_INPUT_PREFIXES` (or an empty prefix tuple, e.g. `unchecked_binary_count`) can
    never be tainted -- it is always `complete`, never inspected means never partially
    inspected.

    Registered in the T5.1 totality guard (`_TOTALITY_TARGETS`,
    tests/test_render_html.py): unlike its S6b neighbors, both arguments here are
    structures THIS SAME RUN just built in-process, never values parsed back out of a
    past run's sidecar -- but shape-guarded regardless (`isinstance` checks below) so a
    future caller passing a malformed structure degrades to `complete` rather than
    raising, the same total-function property every registered entry shares."""
    paths = [entry.get("path", "") for entry in inaccessible if isinstance(entry, dict)]
    pairs = duplication_section.get("pairs", []) if isinstance(duplication_section, dict) else []
    pairs_len = len(pairs) if isinstance(pairs, (list, tuple)) else 0
    quality: dict[str, str] = {}
    for metric in sorted(METRIC_DEFINITIONS):
        if metric == "duplicate_pair_count" and pairs_len >= MAX_PAIRS:
            quality[metric] = "saturated"
            continue
        prefixes = _METRIC_INPUT_PREFIXES.get(metric, ())
        if prefixes and any(isinstance(path, str) and path.startswith(prefix)
                             for path in paths for prefix in prefixes):
            quality[metric] = "partial"
        else:
            quality[metric] = "complete"
    return quality


def build_document(
    root: Path, project_root: Path | None, compose: bool = False, *,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    profile = PROFILE_CLAUDE_CODE if profile is None else profile
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
        # TRK-082 T4 (spec AMENDMENTS A59 requirement 4): STANDING and UNCONDITIONAL, same
        # as every other entry in this literal list. A wildcard in the directory position
        # (skills/*/...) has no single directory for _disclose_unlistable_glob to probe, so
        # a locked skills/<name>/ is undetectable at this layer and would otherwise read as
        # an all-clear -- the disclosure exists precisely because the failure it names
        # cannot be conditioned on.
        "Glob patterns whose wildcard sits in the directory position — the skills/*/... "
        "family (SKILL.md, phases, prompts, agents, rules, and hook-test globs) — cannot "
        "tell an empty match from an unreadable intermediate skills/<name>/ directory, so a "
        "locked skill directory's matches drop out with no disclosure. Single-directory "
        "globs are unaffected: their own unlistable target is named when locked.",
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
                                                      out_of_root_refs=out_of_root_refs,
                                                      profile=profile,
                                                      blind_spots=blind_spots)
    weight_excluded_count = ((len(out_of_root_refs) - _weight_out_of_root_before)
                              + (len(inaccessible) - _weight_inaccessible_before))
    skill_descriptions, agent_descriptions = collect_descriptions(root, inaccessible, profile=profile)
    skills, skill_internal_bodies, memory_bodies = collect_on_demand(
        root, project_root, inaccessible, profile=profile)

    settings, settings_parsed_ok = parse_settings(root, errors, blind_spots, profile=profile)
    hooks_section = reconcile_hooks(root, settings, inaccessible, blind_spots, profile=profile)
    permissions_section = collect_permissions(settings, settings_parsed_ok)
    config_section = collect_config(root, settings, settings_parsed_ok, blind_spots, profile=profile)
    instruction_length_flags = flag_long_instructions(root, inaccessible, blind_spots,
                                                       profile=profile)
    duplication_section = scan_duplication(root, blind_spots, project_root=project_root,
                                            compose=compose, out_of_root_refs=out_of_root_refs,
                                            profile=profile)
    corpus_files = _staleness_corpus(root, inaccessible, blind_spots, profile=profile)
    phantom_refs = check_phantom_refs(root, corpus_files, inaccessible, blind_spots, profile=profile)
    promotion_candidates = collect_promotion_candidates(root, corpus_files, settings,
                                                         profile=profile)
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
    instruction_files = _deduped_instruction_files(root, inaccessible, blind_spots,
                                                    profile=profile)
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

    # S6b / D4 requirement 12, binding disclosure.
    #
    # POSITION IS DELIBERATE and both directions are constrained. UNCONDITIONAL, and
    # placed after the last conditional append a NON-compose run can reach (the
    # `if exhausted_count:` block above) but BEFORE the first `if compose:` below.
    #   - Earlier (inside the static list) shifts every conditional entry's index, which
    #     shows up as an unexplained CHANGED path in the golden's structural diff.
    #   - Inside or after `if compose:` it would never run on the DEFAULT path, so the
    #     disclosure would silently not exist in an ordinary run.
    # In a non-compose run it therefore lands LAST and adds an index rather than moving
    # one. Compose-mode appends land after it, which is fine — nothing keys off its
    # index, only off the fact that it exists and shifts nothing.
    blind_spots.append(
        "Line-range citations (`path.md:12-19`) are checked for the FILE only — the line "
        "range itself is never validated, so a stale range in an otherwise-valid citation "
        "is invisible to this scan.")
    # Codex gate P2-1: "we classify templates" over-promises without this. Backtick
    # tokens are only detected as refs when they carry a `/` or a [\w./~-] extension, so
    # a BARE placeholder or glob never becomes a row and never reaches the template
    # classifier. No row means no claim, so this is a scope limit rather than a defect —
    # but an operator reading a `template` kind would otherwise assume the detector sees
    # every placeholder in their instruction files. It does not.
    blind_spots.append(
        "Placeholder and glob tokens are recognized only when they are PATH-SHAPED — a "
        "backticked `<slug>.md`, `{session}.md` or `*.md` with no directory separator is "
        "not detected as a reference at all, so it is neither resolved nor reported.")
    # M11 (SPEC_7 §2): same deliberate-position reasoning as the two appends above — the
    # project tier is not profile-aware (it always scans the Claude Code project layout,
    # .claude/ + CLAUDE.local.md + .mcp.json), so a --compose run under a non-default
    # profile must disclose that gap rather than let it read as silently covered.
    #
    # M11 exit gate, Codex round, Finding 5 (P2): gated on `_is_default_layout(profile)`,
    # not `profile["name"] != PROFILE_CLAUDE_CODE["name"]` -- `name` is an unconstrained
    # free-text label a foreign profile could leave (or spoof) as "claude-code" while its
    # actual layout differs, which would silently suppress this disclosure under the old
    # name-based check.
    if compose and not _is_default_layout(profile):
        blind_spots.append(
            "The project tier (--compose) is scanned with the Claude Code layout "
            "(.claude/, CLAUDE.local.md, .mcp.json) regardless of --profile; layout "
            "profiles cover the operator tier only in v1.")

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

    test_coverage_section = detect_test_coverage(root, on_demand, errors, profile=profile)

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
        # S6b §8.1 (ADDITIVE, no schema_version bump per binding rule 10). A copy, not the
        # module constant, so a consumer mutating the emitted doc cannot corrupt the next
        # run in the same process.
        "metric_definitions": dict(METRIC_DEFINITIONS),
        # S6c §6.5a axis 2 (ADDITIVE, no schema_version bump per binding rule 10). The
        # run's IDENTITY: two adjacent trend points differing in ANY field are not
        # comparable. `project_root` is null (not omitted) when unset -- a null scope is a
        # DISTINCT scope, never "same as whatever ran last".
        "collection_scope": {
            "root": str(root),
            "project_root": str(Path(project_root).expanduser().resolve())
                            if project_root is not None else None,
            "compose": bool(compose),
        },
        "metric_quality": _metric_quality(inaccessible, duplication_section),
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
        # M11 exit gate, Finding 4 (P3): sourced from the profile's own settings role,
        # not the hardcoded literal "settings.json" -- the user tier IS profile-aware
        # (parse_settings above already reads `profile["top_level_files"]["settings"]`),
        # so a profile naming a custom settings file must label composed hooks with the
        # file that was actually read, not one that never was. None-guarded: a profile
        # with no settings surface at all (settings_format == "none" or the role is
        # null) has no file to name.
        user_settings_name = profile["top_level_files"]["settings"]
        user_settings_source = str(root / user_settings_name) if user_settings_name is not None else None
        composed_hooks = _compose_hooks(
            [("user", settings, user_settings_source, root),
             ("project", project_settings, project_settings_source, project_containment_root),
             ("local", local_settings, local_settings_source, project_containment_root)],
            project_containment_root, out_of_root_refs, profile=profile)

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
        raw_nodes = _walk_operator_tier_nodes(root, inaccessible, profile=profile)
        if project_root is not None:
            raw_nodes += _walk_project_tier_nodes(project_root, out_of_root_refs, errors)
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


# The marker `main()` writes into the crash envelope's errors[]. A NAMED constant because
# it is a cross-module contract, not a log line: _empty_document zeroes all eight headline
# keys, and main() writes that envelope to --out as an ordinary dated sidecar, so the
# renderer needs a reliable way to tell "this run measured zero" from "this run measured
# nothing". render_html.CRASH_ERROR_PREFIX is the reading end, pinned equal to this one by
# test_crash_marker_prefix_matches_the_collector_producer.
_CRASH_ERROR_PREFIX = "collector crashed: "

# M11 (SPEC_7 §2): companion to _CRASH_ERROR_PREFIX above -- tags an errors[] entry so a
# rejected --profile is distinguishable from a build_document crash by anyone reading the
# document. render_html.PROFILE_ERROR_PREFIX is the reading end, pinned equal to this one
# by test_profile_marker_prefix_matches_the_renderer_reader. (Through M11 there was no
# renderer mirror -- an M11 SCOPING decision, not a design property. It was a live defect:
# main()'s --out block writes this envelope to disk as an ordinary dated sidecar, and the
# renderer read its eight fabricated zeros as a measurement. Fixed in TRK-051.)
_PROFILE_ERROR_PREFIX = "layout profile rejected: "


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
            "instruction_files_over_200", "orphan_registration_count", "orphan_script_count",
            "hook_commands_examined", "hook_commands_total")},
        "always_loaded": {"files": [], "conditional_variants": [], "skill_descriptions": [],
                          "agent_descriptions": [],
                          "totals": {"words": 0, "tokens_est": 0, "file_count": 0}},
        "on_demand": {"skills": [], "skill_internal_bodies": [], "memory_bodies": []},
        "enforcement": {"hooks": {"registered": [], "orphan_registrations": [],
            "scripts_on_disk": [], "orphan_scripts": [], "commands_total": 0,
            "commands_resolved": 0, "commands_no_script": 0, "commands_unparsed": 0},
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
        # Envelope rule: present-and-empty on the crash path. A crashed run measured
        # nothing, so it defines nothing -- inheriting METRIC_DEFINITIONS here would let a
        # crash envelope claim a definition it never computed.
        "metric_definitions": {},
        # Envelope rule. A crashed run measured nothing, so its scope is the bare root and
        # every metric is `unmeasured` -- inheriting `complete` here would let a crash
        # envelope claim a measurement it never made.
        "collection_scope": {"root": str(root), "project_root": None, "compose": False},
        "metric_quality": {k: "unmeasured" for k in sorted(METRIC_DEFINITIONS)},
        "inaccessible": [], "blind_spots": [], "errors": [],
    }


def _default_operator_root():
    """Operator-scan-root auto-resolution (P1-1, M6): `$CLAUDE_CONFIG_DIR` if set, else
    `$HOME/.claude` — NEVER hard-coded. Used only as the `--root` argparse default; an
    explicit `--root` always wins. When `CLAUDE_CONFIG_DIR` is unset (the common case),
    this resolves identically to the prior hard-coded default."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(cfg) if cfg else (Path.home() / ".claude")


# ------------------------------------------------------------- TRK-047 / M10: --check (SPEC_7 §1)
# A regression GATE, not a report: --check compares this run's signals against the prior
# MEASURED run in an OUT_DIR and prints findings/exit codes only -- never the JSON document
# (Envelope rule §5 governs the DEFAULT mode; this is the one documented carve-out).

# CHECK_BANDS encodes report-template.md's "Fixed band thresholds" row for
# always_loaded_tokens_est (5,000 / 12,000) -- deliberately its OWN constant, not
# render_html.GAUGE_BANDS (6,000/15,000): the template/gauge divergence is a known,
# out-of-scope inconsistency (AMENDMENTS A3). NOT a uniform upper_inclusive ladder: per
# report-template.md:23 (`<5,000 LOW / 5,000-12,000 MODERATE / >12,000 HIGH`) the LOW cut
# is EXCLUSIVE (5,000 itself is MODERATE) while the MODERATE cut is INCLUSIVE (12,000
# itself is MODERATE, not HIGH) -- see _check_band, which reads these two numbers with
# that asymmetry made explicit rather than folding them into one `<=` walk.
# Changing bands requires editing report-template.md and CHECK_BANDS together (SPEC_7 §1).
CHECK_BANDS = ((5000, "LOW"), (12000, "MODERATE"), (None, "HIGH"))
_CHECK_BAND_ORDER = tuple(label for _, label in CHECK_BANDS)

# D-4 (AMENDMENTS A48): collector.py does not import render_html.py (and must not start).
# These three tuples are the CIVC allowlists render_html.py declares at module scope
# (VERBS/SURFACES/VERDICTS) -- re-declared locally here and pinned equal to render_html's
# by test_check_enums_match_render_html (drift test, the same cure CHECK_BANDS uses above).
_CHECK_VERBS = ("Afford", "Inform", "Constrain", "Verify", "Correct", "Evolve")
_CHECK_SURFACES = ("context", "tools", "memory", "permissions", "orchestration", "observability")
_CHECK_VERDICTS = ("covered", "thin", "empty")
# A cell whose verdict pair matches one of these is a CIVC coverage regression.
_CHECK_CIVC_REGRESSIONS = {("covered", "thin"), ("covered", "empty"), ("thin", "empty")}

_CHECK_SIDECAR_RE = re.compile(r"^harness-map-(\d{4}-\d{2}-\d{2})\.json$")
_CHECK_SYNTHESIS_RE = re.compile(r"^harness-synthesis-(\d{4}-\d{2}-\d{2})\.json$")

_CHECK_BASELINE_NO_PRIOR = "First run — no prior map (baseline)."
# SKILL.md D7's mandated wording, unchanged by TRK-051 (Rule 7: never edit an existing
# assertion's subject). "every prior run crashed" is the UMBRELLA term here for "every
# prior run was unmeasured" -- it now also covers a profile-rejection envelope, which
# measured nothing for a different reason than a crash but is skipped identically.
_CHECK_BASELINE_ALL_CRASHED = "No comparison baseline available — every prior run crashed."


def _check_band(value: Any) -> str:
    """Which CHECK_BANDS label `value` falls in. A non-numeric value sorts into the LAST
    band rather than raising, matching this module's crash-safe posture elsewhere;
    --check has no unit for that case anyway since it only compares like-typed headline
    ints written by build_headline/`_empty_document`.

    Deliberately three explicit branches, NOT a uniform `num <= upper` walk (that shape
    is what render_html._gauge_band uses, and it is WRONG here): report-template.md:23
    puts BOTH boundary values in MODERATE -- 5,000 tokens is MODERATE, not LOW, and
    12,000 tokens is MODERATE, not HIGH. A single ascending `<=` ladder cannot express an
    exclusive-then-inclusive pair of cuts without silently mis-banding the 5,000 edge (a
    harness moving 4,999 -> 5,000 tokens would read LOW->LOW and the gate would stay
    silent at exactly the threshold it exists to watch). Do not "simplify" this back to
    a `for upper, label in CHECK_BANDS: if num <= upper` loop."""
    try:
        num = float(value)
    except (TypeError, ValueError):
        num = float("inf")
    low_upper = CHECK_BANDS[0][0]       # 5,000 -- EXCLUSIVE upper bound for LOW
    moderate_upper = CHECK_BANDS[1][0]  # 12,000 -- INCLUSIVE upper bound for MODERATE
    if num < low_upper:
        return CHECK_BANDS[0][1]
    if num <= moderate_upper:
        return CHECK_BANDS[1][1]
    return CHECK_BANDS[2][1]


def _check_is_crash_envelope(doc: dict[str, Any]) -> bool:
    """True when `doc["errors"]` carries EITHER unmeasured-run marker _empty_document
    ships with: _CRASH_ERROR_PREFIX (build_document raised) or _PROFILE_ERROR_PREFIX (the
    --profile was rejected, so nothing was inventoried). Both produce the SAME all-zero
    headline, and both mean "this run measured nothing" -- so both must be skipped as
    baselines. Matching only the crash marker (the pre-TRK-051 behavior) let a
    profile-rejection envelope's fabricated zeros turn every real current number into a
    manufactured increase.

    Mirrors render_html._run_was_measured's detector (D-4: reuses this module's OWN
    prefixes rather than importing the renderer's copies; the two are pinned equal by
    test_crash_marker_prefix_matches_the_collector_producer and
    test_profile_marker_prefix_matches_the_renderer_reader). Defensive on shape: a
    non-list `errors` is wrapped, non-string entries are skipped rather than raising."""
    errors = doc.get("errors") or []
    entries = errors if isinstance(errors, list) else [errors]
    markers = (_CRASH_ERROR_PREFIX, _PROFILE_ERROR_PREFIX)
    return any(isinstance(e, str) and e.startswith(markers) for e in entries)


# The four headline keys --check actually compares. Fixed order, matching
# _check_headline_regressions' emission order (deterministic output, CLAUDE.md rule 9).
_CHECK_COMPARED_HEADLINE_KEYS = ("always_loaded_tokens_est", "instruction_files_over_200",
                                 "orphan_registration_count", "orphan_script_count")


def _check_headline_is_comparable(headline: Any) -> str | None:
    """None when `headline` can be compared, else a reason string naming the offending
    key. Structural validation, run at SELECTION time (A48 D-2: a D7-selected sidecar
    failing structural validation is FATAL, exit 2, with no fallback to an older file).

    Pre-TRK-051 the `>` comparisons in _check_headline_regressions had no guard at all --
    only _check_band's `try: float(value)` did -- so a prior headline value of "bad"
    raised an uncaught TypeError and --check exited 1, the code that means REGRESSION
    FOUND, printing no REGRESSION: line at all. A hook branching on the exit code could
    not tell a degraded harness from an unreadable file.

    ABSENT keys are fine and must stay fine: partial headlines are normal (older schemas),
    and _check_headline_regressions defaults them with .get(key, 0). Only a PRESENT
    non-numeric value is malformed.

    Bools are rejected explicitly. `True == 1` in Python, so a boolean count would compare
    as a real measurement of 1 -- the same trap schema.md:167 documents for definition
    versions."""
    if not isinstance(headline, dict):
        return f"headline is {type(headline).__name__}, not an object"
    for key in _CHECK_COMPARED_HEADLINE_KEYS:
        if key not in headline:
            continue
        value = headline[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"headline.{key} is not a number ({value!r})"
    return None


def _check_select_prior_sidecar(out_dir: Path, today: str) -> tuple[str, dict[str, Any] | None, str | None]:
    """The D7 selection rule (SKILL.md's Diff-vs-Previous-Run section), scoped to --check
    (AMENDMENTS A48): among harness-map-YYYY-MM-DD.json sidecars in `out_dir` strictly
    before `today`, walk NEWEST FIRST. D-2: a structurally malformed candidate is FATAL --
    returns ("malformed", ...) with NO fallback to an older file. A well-formed UNMEASURED
    ENVELOPE (crash or profile rejection) is not malformed -- it is SKIPPED, continuing to
    the next-older candidate.

    Returns (status, doc, detail):
      ("found", doc, date_str)     -- doc is the measured baseline, detail is its date
      ("no_prior", None, None)     -- no sidecar at all strictly before `today`
      ("all_crashed", None, None)  -- sidecars exist, every one is an unmeasured envelope
                                       (crash or profile rejection)
      ("malformed", None, msg)     -- the D7-selected candidate failed structural validation
      ("unreadable", None, msg)    -- `out_dir` itself could not be listed
    """
    try:
        entries = list(out_dir.iterdir())
    except OSError as exc:
        return "unreadable", None, f"could not read --check out-dir {out_dir}: {exc}"
    candidates = []
    for p in entries:
        m = _CHECK_SIDECAR_RE.match(p.name)
        if not m:
            continue
        # F8 (TRK-051): _CHECK_SIDECAR_RE is STRUCTURAL only (\d{4}-\d{2}-\d{2}), so
        # harness-map-2026-02-31.json matches -- and since selection sorts on the
        # captured STRING, an impossible date can sort as newer than every real one and
        # become the comparison baseline. date.fromisoformat is the calendar gate, the
        # same shape render_html._date_prefix's fix took (AMENDMENTS A27); a match that
        # fails it is treated as not-a-sidecar, never as a candidate.
        try:
            date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if m.group(1) < today:
            candidates.append((m.group(1), p))
    candidates.sort(key=lambda t: t[0], reverse=True)  # newest first (fixed order, F4.4)
    if not candidates:
        return "no_prior", None, None
    for date_str, path in candidates:
        try:
            candidate_doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return "malformed", None, f"malformed prior sidecar {path.name}: {exc}"
        if not isinstance(candidate_doc, dict) or "schema_version" not in candidate_doc:
            return "malformed", None, f"malformed prior sidecar {path.name}: not a valid sidecar document"
        # Comparability is structural validation too (A48 D-2), and it runs BEFORE the
        # unmeasured-envelope skip below: an unmeasured envelope's headline is all-zero
        # ints and always passes this, so the ordering is only load-bearing for a
        # DOCTORED envelope -- where "malformed" is the more accurate verdict anyway.
        headline_problem = _check_headline_is_comparable(candidate_doc.get("headline") or {})
        if headline_problem is not None:
            return "malformed", None, f"malformed prior sidecar {path.name}: {headline_problem}"
        if _check_is_crash_envelope(candidate_doc):
            continue
        return "found", candidate_doc, date_str
    return "all_crashed", None, None


def _check_select_synthesis_pair(
    out_dir: Path, today: str,
) -> tuple[tuple[dict[str, Any], dict[str, Any]] | None, list[str]]:
    """(pair, notices). `pair` is the two most recent harness-synthesis-YYYY-MM-DD.json
    sidecars strictly before `today`, oldest-then-newest, or None when the CIVC comparison
    could not be made (the comparison is best-effort/optional -- SPEC_7 §1 gates it on "if
    >= 2 exist", unlike the collector-sidecar D7 selection above, which is mandatory and
    FATAL on malformed input -- so a `pair is None` return is NEVER by itself grounds for
    exit 2).

    F5 (TRK-051): a None `pair` used to be silent regardless of WHY -- indistinguishable
    from the ordinary "fewer than two sidecars exist yet" case that fires on every run
    against a fresh OUT_DIR. That ordinary case still gets no notice (a notice there would
    fire constantly and teach the reader to ignore the channel); an out-dir that could not
    be listed, or a candidate sidecar that could not be read or parsed as a document, is
    reported by name so "CIVC compared and found nothing" is never confused with "CIVC
    never ran"."""
    try:
        entries = list(out_dir.iterdir())
    except OSError as exc:
        return None, [f"notice: CIVC comparison skipped, could not read --check out-dir {out_dir}: {exc}"]
    candidates = []
    for p in entries:
        m = _CHECK_SYNTHESIS_RE.match(p.name)
        if not m:
            continue
        # F8 (TRK-051): same calendar gate as _check_select_prior_sidecar above -- the
        # regex is structural only, so an impossible date would otherwise sort as newer
        # than every real one.
        try:
            date.fromisoformat(m.group(1))
        except ValueError:
            continue
        if m.group(1) < today:
            candidates.append((m.group(1), p))
    candidates.sort(key=lambda t: t[0], reverse=True)  # newest first
    if len(candidates) < 2:
        return None, []
    (_newest_date, newest_path), (_prior_date, prior_path) = candidates[0], candidates[1]
    try:
        newest_doc = json.loads(newest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"notice: CIVC comparison skipped, unreadable synthesis sidecar {newest_path.name}: {exc}"]
    try:
        prior_doc = json.loads(prior_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, [f"notice: CIVC comparison skipped, unreadable synthesis sidecar {prior_path.name}: {exc}"]
    # Codex P2 (TRK-051 T5): a dict lacking "schema_version" is not a valid synthesis
    # document either -- load_sidecar's D7 selection and schema.md's synthesis contract
    # both require the marker, and a bare {} previously slipped through this check. Reuses
    # the SAME "is not a valid document" notice text as the isinstance failure above: one
    # notice vocabulary, not two.
    if not isinstance(newest_doc, dict) or "schema_version" not in newest_doc:
        return None, [f"notice: CIVC comparison skipped, synthesis sidecar {newest_path.name} is not a valid document"]
    if not isinstance(prior_doc, dict) or "schema_version" not in prior_doc:
        return None, [f"notice: CIVC comparison skipped, synthesis sidecar {prior_path.name} is not a valid document"]
    return (prior_doc, newest_doc), []


def _check_civc_cells(synth_doc: dict[str, Any], label: str) -> tuple[dict[tuple[str, str], str], list[str]]:
    """(cells, notices). D-3 (AMENDMENTS A48): a cell whose verb/surface/verdict falls
    outside the allowlists is IGNORED, never coerced, and never notice-worthy -- unlike
    render_html.build_civc_model, which coerces an unallowlisted verdict to 'empty'
    because it is filling a fixed 6x6 render grid. Coercing here would turn a synthesis
    typo into a manufactured covered->empty finding, so an unmatched cell is simply
    absent from the returned map; per-cell noise across a 36-cell grid would drown any
    real signal, so that case stays silent (this is the load-bearing NO half of F5 at
    this call site).

    `label` (e.g. "prior"/"current") names WHICH of the two compared sidecars an
    unusable `civc` came from, since this function only sees the already-parsed dict,
    never the file it came from."""
    cells: dict[tuple[str, str], str] = {}
    raw = synth_doc.get("civc")
    # A non-list `civc` (an int, a bare dict) is not iterable into cells -- pre-TRK-051
    # `for c in raw` raised a TypeError inside run_check, AFTER headline findings had been
    # accumulated, so a real regression was lost to a traceback. Unusable means "no cells":
    # the synthesis comparison is best-effort by design (SPEC_7 §1 gates it on ">= 2
    # exist", and _check_select_synthesis_pair already returns (None, ...) on unreadable
    # input), unlike the collector-sidecar path above, which is mandatory and FATAL.
    # F5 (TRK-051): silently returning "no cells" here used to be indistinguishable from a
    # clean CIVC comparison that genuinely found nothing -- fixed by reporting a notice
    # (never an exit-2; that widening was considered and declined, see Codex F5 above)
    # whenever `civc` is PRESENT but the wrong shape. An ABSENT key is the ordinary case
    # (a synthesis document predating the civc field) and stays silent for the same
    # every-run-fires-a-notice reason as the fewer-than-two-sidecars case above.
    if "civc" in synth_doc and not isinstance(raw, list):
        return cells, [f"notice: CIVC comparison skipped, {label} synthesis civc is not a list"]
    if not isinstance(raw, list):
        return cells, []
    for c in raw:
        if not isinstance(c, dict):
            continue
        verb, surface, verdict = c.get("verb"), c.get("surface"), c.get("verdict")
        if verb not in _CHECK_VERBS or surface not in _CHECK_SURFACES or verdict not in _CHECK_VERDICTS:
            continue
        cells[(verb, surface)] = verdict
    return cells, []


def _check_valid_definition_version(value: Any) -> bool:
    """schema.md:167's stated contract for a metric definition version: a positive int and
    NOT a bool. `True == 1` in Python, so a stray boolean would silently resolve as
    version 1 and report a series comparable when it is not. Anything failing this is
    UNKNOWN, never a default.

    Mirrors render_html._valid_definition_version. NOT imported from it (A48 D-4:
    collector.py does not import render_html, and must not start) -- instead the two are
    pinned BEHAVIORALLY equal by test_definition_version_validator_matches_render_html.
    Two independent implementations of one prose contract is the two-home shape, and A49's
    lesson is that the unpinned half of a pinned pair is where the off-by-one ships."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _check_definition_skips(prior: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(skipped_metric_names, notice_lines) for metrics whose DEFINITION VERSION differs
    between the prior sidecar and this run (S6b §8.1 / AMENDMENTS A50).

    METRIC_DEFINITIONS is the current run's version map by construction -- build_document
    copies this same constant into doc["metric_definitions"], so reading the constant and
    reading the current document give the same value for any real run. Reading the constant
    keeps run_check callable with a hand-built current doc (the in-process boundary test).

    ABSENT, EMPTY, non-dict, or INVALID prior versions mean COMPARE, never skip. Reading
    "unknown" as "changed" would skip every metric and print a clean-looking verdict for a
    run that compared nothing -- the identical silent-green failure as a --check whose
    --root did not exist, relocated. `_empty_document` sets metric_definitions to {}
    deliberately, and a legacy sidecar predating S6b carries no map at all; both of those
    are ordinary inputs, not signals.

    CIVC is deliberately untouched: METRIC_DEFINITIONS declares no CIVC metric, and the
    synthesis comparison is not a headline metric. Do not wire one in without a spec change.

    Fixed key order (CLAUDE.md rule 9: deterministic output across PYTHONHASHSEED)."""
    declared = prior.get("metric_definitions")
    if not isinstance(declared, dict):
        return [], []
    skipped: list[str] = []
    notices: list[str] = []
    for key in _CHECK_COMPARED_HEADLINE_KEYS:
        prior_version = declared.get(key)
        current_version = METRIC_DEFINITIONS.get(key)
        if not _check_valid_definition_version(prior_version):
            continue
        if not _check_valid_definition_version(current_version):
            continue
        if prior_version == current_version:
            continue
        skipped.append(key)
        notices.append(f"notice: {key} skipped (definition v{prior_version} -> v{current_version})")
    return skipped, notices


def _check_headline_regressions(current: dict[str, Any], prior: dict[str, Any],
                                skip: tuple[str, ...] = ()) -> list[str]:
    """The three headline regression signals (SPEC_7 §1), checked and emitted in this
    FIXED order (deterministic, F4.4) so findings are stable across runs regardless of
    dict iteration order.

    `skip` names metrics whose DEFINITION VERSION changed between the two runs (A50): the
    numbers are no longer measuring the same thing, so comparing them would manufacture a
    REGRESSION out of a detector bump. The default () preserves the pre-A50 behavior for
    any caller that does not supply it."""
    findings = []
    if "always_loaded_tokens_est" not in skip:
        cur_tokens = current.get("always_loaded_tokens_est", 0)
        prior_tokens = prior.get("always_loaded_tokens_est", 0)
        cur_band, prior_band = _check_band(cur_tokens), _check_band(prior_tokens)
        if _CHECK_BAND_ORDER.index(cur_band) > _CHECK_BAND_ORDER.index(prior_band):
            findings.append(f"REGRESSION: always_loaded_tokens_est crossed {prior_band} -> {cur_band} "
                             f"({prior_tokens} -> {cur_tokens} tokens)")
    if "instruction_files_over_200" not in skip:
        cur_over200 = current.get("instruction_files_over_200", 0)
        prior_over200 = prior.get("instruction_files_over_200", 0)
        if cur_over200 > prior_over200:
            findings.append(f"REGRESSION: instruction_files_over_200 increased "
                             f"({prior_over200} -> {cur_over200})")
    for key in ("orphan_registration_count", "orphan_script_count"):
        if key in skip:
            continue
        cur_v, prior_v = current.get(key, 0), prior.get(key, 0)
        if cur_v > prior_v:
            findings.append(f"REGRESSION: {key} increased ({prior_v} -> {cur_v})")
    return findings


def _check_civc_regressions(prior_synth: dict[str, Any], newest_synth: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(findings, notices). CIVC cell regressions between the two most recent synthesis
    sidecars, iterated in the FIXED _CHECK_VERBS x _CHECK_SURFACES order (never a
    sorted(set(...)), F4.4)."""
    prior_cells, prior_notices = _check_civc_cells(prior_synth, "prior")
    newest_cells, newest_notices = _check_civc_cells(newest_synth, "current")
    findings = []
    for verb in _CHECK_VERBS:
        for surface in _CHECK_SURFACES:
            old, new = prior_cells.get((verb, surface)), newest_cells.get((verb, surface))
            if old is not None and new is not None and (old, new) in _CHECK_CIVC_REGRESSIONS:
                findings.append(f"REGRESSION: CIVC {verb}/{surface} regressed {old} -> {new}")
    return findings, prior_notices + newest_notices


def check_load_baseline(out_dir: str) -> tuple[tuple[str, dict[str, Any] | None, str | None],
                                               tuple[tuple[dict[str, Any], dict[str, Any]] | None,
                                                     list[str]]]:
    """Every disk read run_check needs, performed in one place so main() can do it BEFORE
    build_document and therefore before BOTH --out blocks -- the validation block
    (`if args.out is not None:`) and the write block (`if out_path is not None:`) (F1).
    Pre-fix, the --out WRITE ran first and could overwrite the very sidecar the comparison
    was about to select, so `--check DIR --out DIR/<the baseline>` compared the run against
    itself and reported CLEAN. Reading first makes the comparison independent of what --out
    later does to the directory; it does NOT narrow the --out-alongside---check combination,
    which SPEC_7 §1 explicitly permits.

    Returns (prior_selection, (synthesis_pair, synthesis_notices)) -- exactly the two
    top-level values run_check consumes. No write, no mutation: --check writes nothing
    (CLAUDE.md rule 4)."""
    today = datetime.now(timezone.utc).date().isoformat()  # D-1: one clock frame per module
    out_path = Path(out_dir)
    return (_check_select_prior_sidecar(out_path, today),
            _check_select_synthesis_pair(out_path, today))


def run_check(doc: dict[str, Any], out_dir: str,
              baseline: tuple[tuple[str, dict[str, Any] | None, str | None],
                              tuple[tuple[dict[str, Any], dict[str, Any]] | None,
                                    list[str]]] | None = None,
              ) -> tuple[int, str]:
    """SPEC_7 §1 / AMENDMENTS A48 entry point. `doc` is the CURRENT run's already-built
    document -- main() intercepts a current-run crash/profile-rejection BEFORE calling
    this (comparing a crash envelope would emit fabricated regressions, never done here).

    `baseline` is the preloaded (prior_selection, (synthesis_pair, synthesis_notices))
    pair from check_load_baseline. main() always passes it, read before build_document and
    before both --out blocks (F1); the default None re-reads here, which keeps this
    function callable standalone with just (doc, out_dir) -- the shape
    test_check_exit_one_on_band_crossing_at_the_5000_boundary drives.

    Returns (exit_code, text): text is what --check prints to stdout INSTEAD OF the JSON
    document (SPEC_7 §1's one carve-out to the always-emit-JSON invariant, which governs
    the default mode only)."""
    if baseline is None:
        baseline = check_load_baseline(out_dir)
    (status, prior_doc, detail), (synth_pair, synth_notices) = baseline
    # Codex P2 round 2 (TRK-051 T6): computed EXACTLY ONCE here, before `status` is even
    # inspected, so the fatal early return below and the normal path downstream read the
    # SAME `all_synth_notices` -- one gather step, not two hand-synced call sites. T5
    # closed the ONE instance it found (a fatal headline run discarding `synth_notices`,
    # the SELECTION-failure notices from check_load_baseline); it left a second instance
    # of the identical class standing -- the SHAPE notices _check_civc_cells can raise once
    # a pair IS selected (e.g. a present-but-non-list `civc`) were still only folded in
    # further down, past the fatal return. Gathering both notice sources up front, before
    # the branch, closes the CLASS: a third notice source added later is included by
    # construction, not by whoever adds it remembering to patch the fatal branch too.
    # `_check_civc_regressions` is called EXACTLY ONCE per run_check invocation (here) --
    # never a second time downstream -- so `civc_notices` cannot appear twice in the text.
    civc_findings: list[str] = []
    civc_notices: list[str] = []
    if synth_pair is not None:
        prior_synth, newest_synth = synth_pair
        civc_findings, civc_notices = _check_civc_regressions(prior_synth, newest_synth)
    all_synth_notices = synth_notices + civc_notices
    if status in ("malformed", "unreadable"):
        # Exit 2 means the gate could NOT run: civc_findings are deliberately DISCARDED on
        # this path (never returned to the caller) -- a notice is unconditional disclosure
        # ("here is why this run does not carry the usual signal"), a finding is a
        # comparison RESULT, and a fatal run performed no comparison. Surfacing a finding
        # here would be the F6 shape again (a real comparison outcome reached the operator
        # from a run that officially compared nothing) -- not reopened.
        #
        # Ordering: "error: {detail}" stays FIRST, not the usual notices-precede-everything
        # shape (A50) -- test_check_unreadable_out_dir_still_exits_two pins
        # `out.startswith("error:")` and is a shipped assertion (never edited, T5). An
        # unreadable OUT_DIR fails BOTH selectors identically (same directory, same
        # OSError), so that exact test now also carries a non-empty synth_notices; putting
        # the error line first keeps it green while still surfacing every notice, appended
        # after, rather than discarding any of them as before.
        return 2, "\n".join([f"error: {detail}"] + all_synth_notices)
    findings: list[str] = []
    notices: list[str] = []
    if status == "found":
        assert prior_doc is not None
        skipped_metrics, notices = _check_definition_skips(prior_doc)
        findings.extend(_check_headline_regressions(
            doc.get("headline") or {}, prior_doc.get("headline") or {},
            skip=tuple(skipped_metrics)))
    # F5 (TRK-051): synth_notices covers selection failures (an unreadable out-dir or an
    # unreadable/malformed candidate sidecar -- synth_pair is None in both cases); the
    # civc-shape notices cover a selected PAIR whose civc field is unusable. The two are
    # mutually exclusive per run (civc notices only fire once a pair was found), so
    # simply concatenating preserves the fixed selection-then-comparison order (F4.4).
    notices = notices + all_synth_notices
    findings.extend(civc_findings)
    # A50: notices precede findings and the verdict line, and never change the exit code --
    # a skipped metric is neither a regression nor a clean result, it is a metric nobody
    # compared, and the operator is told which one by name.
    if findings:
        return 1, "\n".join(notices + findings)
    if status == "no_prior":
        return 0, "\n".join(notices + [_CHECK_BASELINE_NO_PRIOR])
    if status == "all_crashed":
        return 0, "\n".join(notices + [_CHECK_BASELINE_ALL_CRASHED])
    return 0, "\n".join(notices + [f"No regression detected (baseline: harness-map-{detail}.json)."])


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
    ap.add_argument("--profile", default=None,
                     help="Optional layout-profile JSON describing where always-loaded "
                          "context, skills, rules and hooks live (see "
                          "profiles/claude-code.json). Omitted: the embedded Claude Code "
                          "default. A malformed profile exits 2.")
    ap.add_argument("--indent", type=int, default=2)
    ap.add_argument("--check", default=None, metavar="OUT_DIR",
                     help="Regression gate (SPEC_7 §1): compare this run's headline + "
                          "latest CIVC synthesis against the most recent prior MEASURED "
                          "run in OUT_DIR; PRINTS FINDINGS, NOT THE JSON DOCUMENT (the one "
                          "carve-out to the always-emit-JSON invariant, default mode "
                          "only). Exit 0 no regression (or no baseline yet), 1 a "
                          "REGRESSION:-prefixed finding, 2 a collection/comparison error. "
                          "A metric whose definition version differs between the two runs "
                          "is skipped, with a notice: line naming it.")
    args = ap.parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    out_path = None
    input_paths: list[Path] = []       # bound unconditionally: the write block below passes
                                        # it to write_text_contained, and must never depend
                                        # on which --out branch happened to run
    out_roots = [root]                 # guarded roots for BOTH the upfront check and the
                                        # write-time TOCTOU recheck below (P1-6a)
    # M10 / TRK-051 pre-flight (F2 + F1). --check has no JSON envelope to fall back on --
    # it prints findings -- so an input it cannot measure must EXIT 2, never reach the
    # comparison. Pre-fix, build_document() did not raise on a missing root, so the
    # current document came back empty, every real prior value read as an improvement,
    # and a typo'd --root printed "No regression detected" with exit 0. SPEC_7 §1 line 23:
    # exit 2 is for collection errors; errors must not masquerade as clean.
    #
    # Deliberately --check ONLY. The default mode's contract is the opposite (always emit
    # a valid JSON envelope, CLAUDE.md rule 5), and the --out VALIDATION block below keeps
    # its own softer os.stat() warning unchanged.
    #
    # THREE probes, not one: os.stat() succeeds on a regular file, and is_dir() is True
    # for a directory whose contents cannot be listed -- both of those measure nothing and
    # would report CLEAN.
    check_baseline = None
    if args.check is not None:
        try:
            if not root.is_dir():
                print(f"error: --check requires --root to be a directory: {root}", file=sys.stderr)
                return 2
            os.listdir(root)
        except OSError as exc:
            print(f"error: --check requires an accessible --root: {exc}", file=sys.stderr)
            return 2
        # F1: read the baseline BEFORE build_document and before the --out WRITE block
        # (`if out_path is not None:`), which may target a path inside OUT_DIR (SPEC_7 §1
        # line 24 permits --out alongside --check). Reading first is what stops the run
        # from comparing against itself.
        check_baseline = check_load_baseline(args.check)
    # M11: resolve the layout profile BEFORE anything reads the tree. On failure we do not
    # collect at all -- a half-applied profile would silently inventory the wrong surfaces.
    # The --out block below still runs, and the envelope is still emitted and written
    # (Invariant 2 / SPEC_7 §2).
    profile: dict[str, Any] = PROFILE_CLAUDE_CODE
    profile_error: str | None = None
    # M11 exit gate, Codex round, Finding 1 (P1): the RESOLVED --profile path, tracked
    # unconditionally (success or ProfileError -- the file was READ either way, so it is a
    # read input either way) so the --out guard below can never let a write target name the
    # file the collector just read for --profile. Resolved defensively, same ladder as
    # validate_write_target's own candidate/input-path resolution: a symlink loop raises
    # RuntimeError (not OSError) on CPython, so both are caught and the expanded-but-
    # unresolved path is used as a fallback rather than escaping uncaught.
    profile_arg_path: Path | None = None
    if args.profile is not None:
        profile_arg_path = Path(args.profile).expanduser()
        try:
            profile = load_profile(profile_arg_path)
        except ProfileError as exc:
            profile_error = str(exc)
            profile = PROFILE_CLAUDE_CODE
            print(f"error: {exc}", file=sys.stderr)
    if args.out is not None:
        try:
            os.stat(root)                                     # root is expected to be an existing dir
        except OSError as e:
            # A bad/inaccessible --root must NOT crash before the crash-safe envelope below —
            # skip the --out write (nothing safe to validate against) but still fall through to
            # build_document/print so the always-valid-JSON-envelope invariant holds.
            print(f"warning: --root not accessible, skipping --out write: {e}", file=sys.stderr)
        else:
            if args.compose:
                try:
                    project_containment_root = Path(args.project_root).expanduser().resolve()
                    out_roots.append(project_containment_root)
                except OSError:
                    pass
                # Deliberately the DEFAULT profile: this call feeds validate_write_target's
                # input_paths guard, and a wider input set is the SAFE direction. On the
                # profile-error path there is no valid profile to use anyway.
                # REPLACES input_paths (still [] here), so the --profile path below is
                # appended AFTER this, never lost to the replacement.
                input_paths = iter_input_paths(root, args.project_root, compose=True)
            if profile_arg_path is not None:
                try:
                    resolved_profile_path = profile_arg_path.resolve()
                except (OSError, RuntimeError):  # symlink loop -- see comment above
                    resolved_profile_path = profile_arg_path
                input_paths = list(input_paths) + [resolved_profile_path]
            ok, resolved = validate_write_target(args.out, out_roots, input_paths)
            if not ok:
                ap.error("--out must be outside --root and away from every file the "
                          "collector reads, including --profile if given (read-only "
                          "invariant)")
            out_path = resolved                                # write through the validated resolved path
    # M10 (SPEC_7 §1 / AMENDMENTS A48): tracked unconditionally so the --check branch below
    # can tell "current run measured nothing" from a real document without re-parsing
    # errors[] for a marker string -- comparing a crash/profile-rejection envelope would
    # emit fabricated regressions (every real headline number reads as "new").
    current_run_crash_reason: str | None = None
    if profile_error is not None:
        doc = _empty_document(root)
        doc["errors"].append(f"{_PROFILE_ERROR_PREFIX}{profile_error}")
        current_run_crash_reason = f"layout profile rejected: {profile_error}"
    else:
        try:
            doc = build_document(root, args.project_root, compose=args.compose, profile=profile)
        except Exception as exc:  # noqa: BLE001 — collector must always emit a FULL-key valid envelope
            doc = _empty_document(root)
            doc["errors"].append(f"{_CRASH_ERROR_PREFIX}{exc!r}")
            current_run_crash_reason = f"collector crashed: {exc!r}"
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
        # Re-validate IMMEDIATELY before writing (the earlier --out guard ran before
        # build_document; this catches a target retargeted since). The physical write then
        # goes through the SHARED write_text_contained helper — the same helper
        # render_html.write_html_safely uses, so the two sinks cannot drift.
        #
        # TOCTOU: the helper's write-side threat model is SIX CLASSES CONSIDERED, FOUR
        # CLOSED, TWO ACCEPTED — see write_text_contained's docstring for the table and
        # RISK_REGISTER R11 for the settled rationale. On the dir_fd path the helper opens
        # this file's parent directory once with O_NOFOLLOW, pins that inode with the
        # returned fd, decides containment against the PINNED inode, and creates the temp
        # file relative to that fd — so a parent path COMPONENT swapped in afterward cannot
        # redirect the write. It does NOT defeat a concurrent rename of the pinned
        # directory itself, or a bind-mount alias; those are the two accepted residuals,
        # and any principal who can reach them can already edit this file. On a platform
        # without dir_fd support the helper takes its documented fallback branch, whose
        # exposure is wider; that limitation is stated there.
        #
        # Hard-link safety is unchanged and still load-bearing: an outside-root HARD LINK
        # whose inode is also linked under --root passes resolve()-based path checks
        # (hard links are invisible to path resolution), so a naive write_text() would
        # truncate that shared inode — a read-only bypass. The helper still creates a
        # fresh inode in the target's directory and os.replace()s it onto the name, so
        # any under-root hard-linked inode keeps its original, untouched content.
        try:
            resolved_recheck = out_path.resolve()
            for guard_root in out_roots:
                try:
                    guard_root_stat = os.stat(guard_root)
                except OSError:
                    continue
                if _resolves_inside_root(resolved_recheck, Path(guard_root), guard_root_stat):
                    raise OSError("--out resolved inside a guarded root at write time (TOCTOU)")
            write_text_contained(resolved_recheck, text, out_roots,
                                 input_paths=input_paths)
        except OSError as exc:
            print(f"warning: could not write --out: {exc}", file=sys.stderr)
    if args.check is not None:
        # M10 (SPEC_7 §1): the one carve-out to "stdout is the primary contract" below --
        # --check prints findings, never the JSON document. --out (if also given) already
        # wrote above, unaffected; this branch only changes what goes to stdout/exit code.
        if current_run_crash_reason is not None:
            print(f"error: current run did not measure anything -- {current_run_crash_reason}")
            return 2
        check_exit, check_text = run_check(doc, args.check, baseline=check_baseline)
        print(check_text)
        return check_exit
    print(text)  # stdout is the primary contract — always emit the built document, write-or-not
    return 2 if profile_error is not None else 0


if __name__ == "__main__":
    sys.exit(main())
