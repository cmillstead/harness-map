#!/usr/bin/env python3
"""harness-map live server: a loopback-only HTTP server that holds one in-memory rendered
HTML document and serves it from memory on GET `/`. Imports `collector` and `render_html`
in-process (via sibling-path importlib loading, so this works regardless of the invoking
cwd), runs one collect+render at startup, and serves the resulting bytes.

The realtime path (B1): a daemon watcher thread polls a PROVEN SUPERSET of the collector's
filesystem input surface (collector.iter_input_paths — the single source of truth for the
watched set) every POLL_SECONDS, debounces bursts over DEBOUNCE_SECONDS, and on a settled
change re-runs `_rebuild` then broadcasts a `refresh` event to every connected SSE client.
GET `/events` returns `text/event-stream` and streams events from a bounded per-client
queue.Queue (coalesced to <=1 pending, so a stalled client cannot grow memory unbounded).
"""
import argparse
import contextlib
import dataclasses
import importlib.util
import io
import os
import queue
import stat
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, cast

_MODULE_DIR = Path(__file__).resolve().parent


def _load_sibling(name: str, filename: str) -> ModuleType:
    """Loads a sibling module by absolute path (never relying on the invoking cwd or
    sys.path), mirroring the spec_from_file_location pattern the test suite already
    uses for the same modules."""
    spec = importlib.util.spec_from_file_location(name, _MODULE_DIR / filename)
    # spec_from_file_location's return type is `ModuleSpec | None`; for a fixed sibling
    # path next to this file it is never None in practice. module_from_spec/spec.loader
    # narrow to Any once past this call, so this is the ONE ignore for the whole seam
    # (collector/render_html below are then seen as dynamic module objects by mypy).
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]  # sibling loaded via importlib (serve.py:41)
    spec.loader.exec_module(module)  # type: ignore[union-attr]  # sibling loaded via importlib (serve.py:41)
    return module


collector = _load_sibling("harness_map_serve_collector", "collector.py")
render_html = _load_sibling("harness_map_serve_render_html", "render_html.py")

# Loopback allowlist (single-value, never a blocklist): the ONLY host this server may bind.
_ALLOWED_HOSTS = {"127.0.0.1"}

# Realtime-watcher timings. POLL: base sweep interval. DEBOUNCE: quiet window a burst must clear
# before ONE settled rebuild fires. HEARTBEAT: SSE keepalive interval, which keeps an otherwise
# silent stream's bytes flowing. HEARTBEAT is INDEPENDENT of RequestHandler.timeout — an earlier
# version of this comment claimed it MUST stay below that timeout or a healthy stream would be
# reaped; that was measured false in both directions, and the measurement plus the mechanism are
# recorded at RequestHandler.timeout. Tests shrink all three via build_server kwargs; these
# DEFAULTS are production.
POLL_SECONDS = 2.0
DEBOUNCE_SECONDS = 1.0
HEARTBEAT_SECONDS = 3.0

# Ceiling on simultaneously registered SSE (`/events`) streams. A loopback dashboard is 1-5
# browser tabs, so 16 is far above any human workflow. It is not a comfort limit: a stream whose
# peer stops reading is NOT reapable (see `_serve_events`' accepted-residual note), so this is what
# bounds how many ThreadingHTTPServer threads a stalled or hostile co-resident process can pin.
MAX_SSE_CLIENTS = 16

# Reconnect interval, in seconds. ONE home, so the wire surfaces that carry it cannot drift apart.
# Task 1 wires the first: the `Retry-After` header on a cap refusal, which is ADVISORY and reaches
# non-browser clients only -- per the WHATWG EventSource processing model a non-200 response FAILS
# the connection (fire `error`, readyState = CLOSED, no reconnect), and `Retry-After` is not an
# EventSource input.
# Task 2 wires the second: the `retry:` field written into the SSE stream itself, before every
# connect-time `event: sync`, on every ACCEPTED (200) stream. The client reads it in
# MILLISECONDS (hence the *1000 at the write site). What the tests VERIFY is narrower than what
# the field DOES: they pin that it reaches the wire, in that position, carrying no `data:`. No
# browser reconnect was measured. Its effect lands on a stream that was ESTABLISHED and then
# DROPPED -- a server restart -- where it replaces a user-agent-defined
# reconnection interval with an explicit, server-chosen one. It does NOT reach a client refused
# by MAX_SSE_CLIENTS: that stream never opened (503, not 200), so the field never arrived --
# that client is stuck with the `Retry-After` header above, or nothing at all if non-browser.
SSE_RETRY_SECONDS = 5

# B2/D5 degrade switch: when True, EVERY sweep that would otherwise take the cheap
# friction-only path instead runs a full `_rebuild`. The tool stays fully correct with
# this flipped True (just slower, no incremental optimization) -- it exists so the cheap
# path can be disabled wholesale without touching the classification logic itself.
_FORCE_FULL_RECOLLECT = False


class CollectorError(Exception):
    """Raised when collector.main() fails to produce a fresh sidecar (P30 guard)."""


def _validate_host(host: str) -> str:
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"host must be one of {sorted(_ALLOWED_HOSTS)}: {host!r}")
    return host


def _stat_identity(path):
    """(st_ino, st_mtime_ns) for `path`, or None if it does not exist. A fresh atomic
    os.replace() write always yields a new inode, so this tuple reliably distinguishes
    a real write from a skipped one (P30 guard)."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_mtime_ns)


def _run_collector(root, project_root, sidecar_path, compose=False):
    """Runs collector.main() in-process, suppressing its always-on JSON stdout print.

    P30 SIDECAR-FRESHNESS GUARD: collector.main() returns 0 even when the `--out`
    write was skipped or failed (it swallows the write OSError and also skips the
    write entirely if `--root` is inaccessible) — a transiently-unreadable root
    yields return 0 with a STALE sidecar on disk. Trusting the return code alone
    would serve a stale render, so freshness is verified via inode identity: snapshot
    the sidecar's (st_ino, st_mtime_ns) before the call, require it EXISTS and DIFFERS
    after the call.

    `compose` (T8, default False): propagates `--compose` to collector.main() so the
    written sidecar carries the composed operator ⊕ project tiers.
    """
    pre = _stat_identity(sidecar_path)
    argv = ["--root", str(root), "--project-root", str(project_root), "--out", str(sidecar_path)]
    if compose:
        argv.append("--compose")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = collector.main(argv)
    if rc != 0:
        raise CollectorError(f"collector.main exited {rc}")
    post = _stat_identity(sidecar_path)
    if post is None or post == pre:
        raise CollectorError(f"sidecar not freshly written: {sidecar_path}")


class _State:
    """Guards the single served RenderContext + rebuild bookkeeping under one lock.

    `ctx` is the SINGLE source of served state (html_bytes/doc/models/etc. all read
    from it, never re-derived). `collect_count` proves whether a FULL re-collect ran.
    `streams`/`no_friction` are carried so every `_rebuild` call uses the identical
    friction configuration the server was started with. `stream_offsets` (B2/T5) is
    {str(stream_path): size-or-None}, the trigger heuristic the watcher uses to tell a pure
    telemetry append (cheap friction-only re-render) apart from a truncation/rotation/deletion
    (forces a full re-collect) -- it is NEVER used to slice a partial tail, the cheap path
    always re-reads each stream file in FULL (C18 parity). It is seeded from the size observed
    BEFORE a render consumes the streams (FIX 1) and carries an existence bit -- None marks an
    absent stream, distinct from an empty one (size 0) (FIX 2). `stream_inodes` (TRK-022 finding
    3) is its parallel {str(stream_path): st_ino-or-None} identity dict, seeded in lockstep at
    the same two publish sites, catching a same-size rotation the size heuristic alone misses.
    `generation` is the monotonic publish counter (FIX 4) baked into the served page + reported
    to SSE clients on connect.
    """

    def __init__(self, streams: dict[str, Any], no_friction: bool) -> None:
        self.lock = threading.Lock()
        # `ctx` holds a render_html.RenderContext once populated; render_html is loaded
        # dynamically via `_load_sibling` (serve.py:41), so mypy sees it only as Any.
        self.ctx: Any = None
        self.collect_count = 0
        self.streams = streams
        self.no_friction = no_friction
        # Monotonic publish counter (FIX 4): bumped under `lock` on EVERY publish (full
        # `_rebuild` AND cheap `_rebuild_friction_only`). The generation is baked into the
        # rendered page (a <meta>) and sent to each /events client on (re)connect, so a
        # client that missed a refresh while disconnected catches up by comparing generations.
        self.generation = 0
        # {str(stream_path): size-or-None}, seeded from the PRE-render size of every
        # configured stream (FIX 1) and carrying the existence bit (None == absent, FIX 2):
        # the trigger heuristic `_classify_stream_sweep` reads to tell a pure telemetry append
        # (cheap friction-only re-render) apart from a truncation/rotation/deletion (full
        # re-collect) -- NEVER used to slice a partial tail (the cheap path always re-reads
        # each stream file in FULL, C18 parity).
        self.stream_offsets: dict[str, Any] = {}
        # {str(stream_path): st_ino-or-None}, seeded in LOCKSTEP with `stream_offsets` at BOTH
        # publish sites (`_rebuild` and `_rebuild_friction_only`). It is a PARALLEL dict rather
        # than a widened `stream_offsets` value on purpose: six pre-existing assertions compare
        # `stream_offsets` values to plain integers (tests/test_serve.py:694, 715, 739, 910, 928,
        # 934), and widening that value would break every one of them. `_classify_stream_sweep`
        # reads this to catch a SAME-SIZE rotation -- the live file renamed aside and replaced by
        # a new file of identical length -- which the size ladder alone reads as "no change"
        # (TRK-022 finding 3, measured).
        self.stream_inodes: dict[str, Any] = {}
        # FIX 4 (Codex challenge): stream keys whose last-observed truncation/rotation/deletion
        # has NOT yet been consumed by a successful full `_rebuild`. Touched ONLY by the single
        # watcher thread (no lock needed). Set when `_classify_stream_sweep` reports "truncated";
        # forces the full-recollect path every sweep until a rebuild SUCCEEDS, then cleared. This
        # replaces the old "zero state.stream_offsets[key] before the rebuild" step, which made a
        # truncate-to-empty stream compare 0==0 next sweep -> "none" -> the required re-collect was
        # lost forever if that rebuild failed transiently (stale state served permanently).
        self.pending_truncation: set[str] = set()
        # SSE fan-out: bounded per-client queues, mutated ONLY under clients_lock (never
        # the ctx lock, so a slow /events writer cannot block a GET `/` read). The watcher
        # compares fresh snapshots against watch_snapshot to decide when a rebuild is due.
        self.clients_lock = threading.Lock()
        self.clients: list["queue.Queue[str]"] = []
        self.watch_snapshot: dict[Any, Any] = {}
        # Codex r3 FIX 1: the synthesis sidecar (out_dir/harness-synthesis-<date>.json) is a
        # render INPUT that feeds the Coverage Matrix + drag MODELS, but it lives under out_dir
        # -- OUTSIDE the collector-input surface `watch_snapshot` covers AND distinct from the
        # friction streams. It is tracked as its OWN parallel `_path_value` snapshot (existence/
        # mtime/size), keyed by the served render's date, so a (re)write of the synthesis while
        # the server runs forces a FULL `_rebuild` (never the cheap friction-only path, which
        # reuses cached models). Seeded in build_server BEFORE the initial `_rebuild`, mirroring
        # watch_snapshot's pre-rebuild ordering; advanced only after a successful settled rebuild.
        self.synth_snapshot: Any = None

    def register_client(self) -> "queue.Queue[str] | None":
        """Add and return a fresh bounded (maxsize=1) queue for one SSE connection, or None when
        MAX_SSE_CLIENTS streams are already registered. The bound + Full-coalescing broadcast means
        a stalled client holds at most ONE pending refresh — a single reload subsumes every refresh
        it missed.

        The ceiling test and the append happen under ONE clients_lock acquisition. Splitting them
        across two acquisitions is a TOCTOU: N concurrent connects can all observe
        len(self.clients) < MAX_SSE_CLIENTS before any of them appends, and the list overshoots the
        ceiling by up to N-1 (TRK-022 slice A's "parallel state must be atomic per key, not merely
        adjacent"). Refusal returns None rather than raising, so `_serve_events` answers 503 on
        ordinary control flow and never has to unregister a queue that was never created — which is
        why the queue is constructed INSIDE the lock, after the check, rather than before it."""
        with self.clients_lock:
            if len(self.clients) >= MAX_SSE_CLIENTS:
                return None
            client_queue: "queue.Queue[str]" = queue.Queue(maxsize=1)
            self.clients.append(client_queue)
        return client_queue

    def unregister_client(self, client_queue: "queue.Queue[str]") -> None:
        """Remove a client's queue (idempotent — removing an already-absent queue must not
        raise, so the /events finally can call this unconditionally)."""
        with self.clients_lock:
            with contextlib.suppress(ValueError):
                self.clients.remove(client_queue)


def _broadcast_refresh(state):
    """Push a single `refresh` token to every connected SSE client, under clients_lock.

    Each client queue is bounded to maxsize=1: on queue.Full the client already has a
    pending refresh, so we drop THIS token silently — a single reload subsumes all missed
    refreshes (correct coalescing, NOT a lost update). queue.Queue.put_nowait raises only
    queue.Full for a bounded queue, so no other failure mode is reachable here; the real
    client-drop-on-broken-socket happens in the /events writer loop on BrokenPipeError."""
    with state.clients_lock:
        for client_queue in state.clients:
            try:
                client_queue.put_nowait("refresh")
            except queue.Full:
                pass


def _write_guard_roots(ctx):
    """P1-B (Codex challenge): the guard roots for the write-time re-validation every
    HTML write sink (`_rebuild`, `_rebuild_friction_only`) passes to
    `render_html.write_html_safely` — read fresh from the JUST-PRODUCED `ctx.doc` on
    EVERY call, never cached, so a retargeted `--out-dir` symlink is always checked
    against the CURRENT operator root and (in compose mode) the CURRENT project-
    containment root. `ctx.doc["inspected_roots"]["project_containment"]` is present
    only when the sidecar was collected with `--compose` (T8); a non-compose ctx has no
    `inspected_roots` key at all, so this degrades to `[operator_root]` — unchanged
    single-root behavior for the non-compose case.

    A non-string `ctx.doc["root"]` raises `render_html.RenderError` instead of silently
    dropping it: `write_html_safely` skips containment validation entirely on an EMPTY
    guard_roots, so dropping an unparseable root here would convert "cannot verify
    containment" into "do not check containment" -- the fail-open this guard exists to
    close. Every caller (`_rebuild`, `_rebuild_friction_only`) already treats a
    `RenderError` from this write-time re-validation as a normal, catchable render
    failure (see their docstrings) -- it degrades to keep-last-good / fall back to a
    full rebuild, same as any other write-time guard rejection, and never crashes the
    watcher thread or the server.

    Also folds in a permanent FLOOR root (`Path.home() / ".claude"`), ADDITIVE to the
    sidecar-derived roots above -- closes the FALSY-root half of the same fail-open (a
    falsy/absent `ctx.doc["root"]`, the ordinary shape on a non-compose ctx, used to
    empty this list entirely, and `write_html_safely` skips validation on an empty
    guard list). This does not reopen A26, which rejected using this same expression as
    a fallback for VERIFYING an unparseable root -- it never asserts anything about what
    the sidecar scanned, only a fact independent of the sidecar entirely ("this process
    must never write inside ~/.claude"). Full reasoning, kept in lockstep rather than
    duplicated at length here: render_html.py::main()'s matching comment. Residual: a
    falsy `root` plus a write inside some OTHER mapped harness root the sidecar failed
    to report is still unguarded -- only `~/.claude` is covered by the floor."""
    root = ctx.doc.get("root")
    if root is not None and not isinstance(root, str):
        raise render_html.RenderError(f"refusing to write: sidecar root field is not a string: {root!r}")
    inspected_roots = ctx.doc.get("inspected_roots") or {}
    floor_root = str(Path.home() / ".claude")
    roots = [r for r in (root, inspected_roots.get("project_containment"), floor_root) if r]
    if not roots:
        # Unreachable today (floor_root always non-empty); fails CLOSED if the floor is
        # ever refactored away, per render_html.py::main()'s matching comment.
        raise render_html.RenderError("refusing to write: no guard roots available to validate against")
    return roots


def _rebuild(state, out_dir, root, project_root, compose=False):
    """Write-then-publish (publish only AFTER the fallible on-disk write succeeds):
    0. snapshot each stream's PRE-render size and inode (FIX 1; TRK-022 finding 3) and the
       next publish generation (FIX 4)
    1. run the collector (P30-guarded) to produce today's sidecar
    2. render in memory from that sidecar (baking in the generation as a <meta>)
    3. write the HTML artifact to disk (the operation that can raise OSError)
    4. ONLY on write success: atomically publish state.ctx + bump collect_count + generation
       + seed stream_offsets and stream_inodes from the step-0 PRE-render snapshots
    5. broadcast a `refresh` to every connected SSE client

    Any exception raised before step 4 propagates to the caller (the watcher loop catches
    it); the last-good `state.ctx` is left untouched (no swap, no counter increment, no
    broadcast) so a mid-serve collect/render/write fault never publishes a broken document.
    This now ALSO covers write_html_safely's own guard rejection (P1-B, Codex challenge;
    raised as a catchable `render_html.RenderError`, NOT `SystemExit` -- a `SystemExit`
    here would be a `BaseException` that escapes this loop's `except Exception`/
    `except (..., RenderError, ...)` degrade handlers and silently kill the watcher
    thread): the startup `--out-dir` guard in `build_server` only ever validated ONCE,
    before this function has run even a single time -- a `--out-dir` symlink retargeted
    into a guarded root AFTER that startup check must still be caught at EVERY
    subsequent write, never just the first, and the watcher must SURVIVE that
    rejection rather than dying.

    `compose` (T8, default False): propagated straight through to `_run_collector`.
    """
    out_dir = Path(out_dir)
    # FIX 1: snapshot each stream's size BEFORE the render consumes it, so a lower bound of
    # what was actually consumed seeds the offsets. Any append that lands during the render
    # then leaves size>offset next sweep (a safe redundant re-render), never recorded as
    # already-consumed (which would drop the last event of a burst until another append).
    pre_sizes, pre_inodes = _snapshot_stream_state(state)  # ONE stat per path: the pair cannot disagree
    next_gen = state.generation + 1  # FIX 4: the generation this render is published at
    today = datetime.now().strftime("%Y-%m-%d")
    sidecar_path = out_dir / f"harness-map-{today}.json"
    _run_collector(root, project_root, sidecar_path, compose=compose)
    # P30 ACCEPTED TOCTOU WINDOW: _run_collector's freshness check reads the sidecar's
    # inode identity, then render_from_out_dir below re-opens and re-reads the same
    # sidecar path itself — a co-resident process could swap the file in that narrow gap.
    # Same accepted-risk class collector.py documents for its own write path (single-user
    # loopback tool; not fully closed, deliberately not hardened further here).
    ctx = render_html.render_from_out_dir(
        out_dir, date=today, streams=state.streams, no_friction=state.no_friction,
        generation=next_gen)
    html_path = out_dir / f"harness-map-{today}.html"
    render_html.write_html_safely(html_path, ctx.html_text, _write_guard_roots(ctx))
    with state.lock:
        state.ctx = ctx
        state.collect_count += 1
        state.generation = next_gen
        state.stream_offsets = pre_sizes  # FIX 1: seed from PRE-read sizes, not post-render
        state.stream_inodes = pre_inodes  # TRK-022 finding 3: seeded in lockstep with the sizes
    _broadcast_refresh(state)


def _path_value(path, tier="operator", project_root=None):
    """Snapshot value for ONE watched path. `tier`/`project_root` (T8) decide the symlink-
    follow POLICY: `tier="operator"` (the default, unchanged) always stats FOLLOWING symlinks
    (os.stat/os.path.realpath, never lstat) so a change to a deploy-symlink TARGET living
    OUTSIDE --root is still observed (skill dirs are deploy symlinks — that missed-target
    case is why iter_input_paths, not a hand-kept list, is the source of truth).

    `tier="project"` is CONTAINMENT-GATED instead of unconditional (T8 mirrors T3 exactly,
    reusing `collector._project_tier_gate`): a project-tier path whose realpath resolves
    INSIDE `project_root` is STILL followed (identical to the operator branch below — a
    target-content change must still trigger a refresh, matching T3's own read policy for a
    contained project symlink); a project-tier path whose realpath ESCAPES `project_root`
    uses lstat/readlink ONLY — no target stat/read, never diverging from what the collector
    itself would follow (a missing path degrades to the same missing-tuple result either way).

    Returns a small comparable tuple:
      * missing:            (False, None, None, None)
      * directory:          (True, "dir", sorted-listdir tuple | None, target identity | None)
      * regular file:       (True, "file", (mtime_ns, size), target identity | None)
      * escaping symlink:   (True, "symlink-escaping", readlink() target | None, None)
    Element 3 is the entry's OWN policy-permitted content identity: listdir membership for a
    dir, (mtime_ns, size) for a file, and -- for an ESCAPING project-tier symlink, which this
    function may never follow -- the readlink() target. Element 4 is what FOLLOWING the entry
    landed on: (realpath target, target mtime_ns) for a symlink on the follow branch, None for
    a non-symlink, and None on the escaping branch (which never follows, by policy). The two
    slots answer different questions and are not competing homes for one fact.
    A container dir's sorted-listdir membership flips when a skill/hook/rule/agent/project is
    added or removed (even an EMPTY dir appearing). An unreadable dir's listdir OSError degrades
    to None membership — the existence bit still records the flip."""
    if tier == "project" and project_root is not None:
        contained = False
        try:
            containment_stat = os.stat(project_root)
        except OSError:
            containment_stat = None
        if containment_stat is not None:
            contained, _identity = collector._project_tier_gate(
                Path(path), Path(project_root), containment_stat)
        if not contained:
            # T3 mirror: escaping (or stat-inaccessible/missing) -> lstat/readlink only, NEVER
            # follow into the target. A genuinely-missing path's lstat also raises ENOENT, so
            # this converges on the same (False, None, None, None) the follow branch below
            # would have produced for a missing path -- no behavioral gap for that case.
            try:
                lst = os.lstat(path)
            except OSError:
                return (False, None, None, None)
            if stat.S_ISLNK(lst.st_mode):
                try:
                    target = os.readlink(path)
                except OSError:
                    target = None
                return (True, "symlink-escaping", target, None)
            if stat.S_ISDIR(lst.st_mode):
                try:
                    members = tuple(sorted(os.listdir(path)))
                except OSError:
                    members = None
                return (True, "dir", members, None)
            return (True, "file", (lst.st_mtime_ns, lst.st_size), None)
    # operator tier (default) OR a CONTAINED project-tier entry: unchanged follow-symlinks
    # behavior.
    link_target_identity = None
    if os.path.islink(path):
        # os.stat below already follows the link, but fold the target's IDENTITY in explicitly
        # so intent is unmistakable and a multi-hop link is covered. The identity is
        # (resolved-target, target-mtime_ns), NOT the mtime alone: a symlink RETARGETED to a
        # different file that happens to carry the SAME size and the SAME mtime_ns produced a
        # byte-identical value under the mtime-only form, so the content flipped while the
        # watcher's value did not -- no re-render fired and the dashboard served stale bytes
        # (TRK-022 finding 6, measured). `os.path.realpath` is the expression this branch
        # already follows, is non-strict (it never raises OSError, unlike os.readlink), and
        # resolves a multi-hop chain -- so it also catches a retargeted INTERMEDIATE directory
        # symlink, which readlink() on the final component alone would miss.
        resolved = os.path.realpath(path)
        try:
            target_mtime = os.stat(resolved).st_mtime_ns
        except OSError:
            target_mtime = None
        link_target_identity = (resolved, target_mtime)
    try:
        st = os.stat(path)  # follows symlinks
    except OSError:
        return (False, None, None, None)
    if stat.S_ISDIR(st.st_mode):
        try:
            members = tuple(sorted(os.listdir(path)))
        except OSError:
            members = None
        return (True, "dir", members, link_target_identity)
    return (True, "file", (st.st_mtime_ns, st.st_size), link_target_identity)


def _classify_watch_tier(path, project_root):
    """Lexical-only classification (T8, no stat): True if `path` structurally lives under
    `project_root`'s directory tree -- i.e. this watched entry is one of the project-tier
    additions `collector.iter_input_paths(..., compose=True)` added under the project-
    containment-root. Purely a `Path.parents` check, independent of whatever the path
    CURRENTLY resolves to -- the resolve-time follow/no-follow POLICY decision belongs to
    `_path_value` (T3's realpath containment gate via `_project_tier_gate`), not here."""
    path = Path(path)
    project_root = Path(project_root)
    return path == project_root or project_root in path.parents


def _watched_snapshot(root, project_root=None, compose=False):
    """Point-in-time snapshot {Path: (tier, value)} of the ENTIRE collector input surface,
    keyed by exactly the paths collector.iter_input_paths yields — so the watched set can
    never drift from what the collector reads (a new collector input added THERE is watched
    automatically). A snapshot inequality (any file mtime/size, any container membership/
    existence, or any symlink-target mtime changed) is the watcher's re-render signal.

    T8: each entry is TIER-TAGGED ("operator"/"project") by lexical containment against
    `project_root` (compose mode only — non-compose stays all-"operator", unchanged), and
    that tier drives `_path_value`'s containment-gated follow policy for project-tier
    entries. Keys stay bare Paths (unchanged contract); values become `(tier, path_value)`."""
    snapshot = {}
    for path in collector.iter_input_paths(root, project_root, compose=compose):
        tier = "operator"
        if compose and project_root is not None and _classify_watch_tier(path, project_root):
            tier = "project"
        snapshot[path] = (tier, _path_value(path, tier, project_root if compose else None))
    return snapshot


def _synthesis_path(out_dir, date):
    """The synthesis sidecar render_from_out_dir consumes for `date`
    (harness-synthesis-<date>.json). It is the ONE render input that lives under out_dir
    instead of root, so it is tracked separately from the collector-input surface (Codex r3)."""
    return Path(out_dir) / f"harness-synthesis-{date}.json"


def _synthesis_value(out_dir, date):
    """`_path_value` change signal (existence / mtime / size) for the CURRENT-date synthesis
    sidecar -- the same tuple form the collector-input snapshot uses, so a create/modify/delete
    of the synthesis is detected identically. Keyed by the served render's `date` so a local
    midnight rollover re-keys it consistently with the C18 date-mismatch guard (they use the
    SAME `today`, so synthesis-tracking never fights the rollover logic)."""
    return _path_value(_synthesis_path(out_dir, date))


def _rebuild_friction_only(state, out_dir):
    """B2/T5 cheap path: re-renders ONLY the friction overlay from the CACHED collector
    doc/models/node_index already in `state.ctx` -- no collector run, no re-selection of
    sidecars. Used when a sweep sees ONLY a friction-telemetry JSONL stream grow (a pure
    append), never a collector-input change. `build_friction_overlay` and `render_html`
    always re-read/re-render over the FULL current file contents (never a partial tail),
    so this is byte-parity-equivalent to what a full `_rebuild` would produce for the
    same on-disk state (C18).

    Follows the identical write-then-publish ordering as `_rebuild`: write the on-disk
    HTML artifact FIRST, and ONLY on success swap in the new context (via
    dataclasses.replace, so date/doc/models/node_index/streams/friction_disabled/skipped
    are carried over UNCHANGED) under the lock, then broadcast. Any exception here
    propagates to the caller UNPUBLISHED (no swap, no broadcast, `ctx` untouched) -- the
    caller is responsible for the D5 degrade-to-full-recollect.

    `collect_count` is deliberately NEVER touched here (the T5 counter contract): only a
    full `_rebuild` proves a collector run happened.

    Shares `_rebuild`'s P1-B write-time re-validation via `_write_guard_roots`/
    `write_html_safely`: since this cheap path runs on every settled append-only sweep
    (far more often than a full `_rebuild`), it is EXACTLY the path a `--out-dir`
    symlink retargeted after startup would most likely hit first.
    """
    with state.lock:
        ctx = state.ctx
    # FIX 1: pre-read stream sizes (before build_friction_overlay consumes them) seed the
    # offsets, so an append during this cheap render is never marked already-consumed. TRK-022
    # finding 3: the inode is captured in the same pre-read pass, seeding stream_inodes too.
    pre_sizes, pre_inodes = _snapshot_stream_state(state)  # ONE stat per path: the pair cannot disagree
    next_gen = state.generation + 1  # FIX 4: the cheap re-render also advances the generation
    out_dir = Path(out_dir)
    new_friction = render_html.build_friction_overlay(
        ctx.doc, ctx.streams, ctx.node_index, ctx.date, ctx.friction_disabled)
    html_text = render_html.render_html(
        ctx.date, ctx.models, new_friction, {"doc": ctx.doc, "skipped": ctx.skipped},
        generation=next_gen)
    html_bytes = html_text.encode("utf-8", "backslashreplace")
    html_path = out_dir / f"harness-map-{ctx.date}.html"
    render_html.write_html_safely(html_path, html_text, _write_guard_roots(ctx))
    with state.lock:
        state.ctx = dataclasses.replace(
            state.ctx, friction=new_friction, html_text=html_text, html_bytes=html_bytes)
        state.generation = next_gen
        state.stream_offsets = pre_sizes  # FIX 1: seed from PRE-read sizes, not post-render
        state.stream_inodes = pre_inodes  # TRK-022 finding 3: seeded in lockstep with the sizes
    _broadcast_refresh(state)


def _stream_paths_list(streams):
    """[str(path), ...] (deduplicated, insertion order) for every CONFIGURED, non-None
    friction stream path -- the surface the B2 incremental path stats for append-only
    growth. `collector.iter_input_paths` deliberately excludes these four telemetry
    streams (they are friction inputs, not collector inputs), so this is a SEPARATE,
    parallel per-sweep check the watcher runs alongside `_watched_snapshot`. `stream_path`
    (not the friction-stream name like "decisions") is `state.stream_offsets`'s key, per
    the B2 design. A `no_friction` run's all-None streams dict, or a bare `None`, yields
    [].

    Why they are excluded is no longer obvious: since S6a the interventions stream lives
    INSIDE the scanned root (projects/<slug>/memory/), unlike the other three. The
    collector reaches that directory only as `paths.add(mem_dir)` plus
    `mem_dir.glob("*.md")` -- never `*.jsonl` -- so an append to the stream is not a
    collector-input change and still takes the cheap friction-only rebuild. Pinned by
    tests/test_collector.py::test_iter_input_paths_excludes_jsonl_telemetry_streams."""
    if not streams:
        return []
    seen = []
    for path in streams.values():
        if path is None:
            continue
        key = str(path)
        if key not in seen:
            seen.append(key)
    return seen


def _stream_size(path):
    """Current byte size of one friction-stream path, or None if it does not exist or is
    not a regular file (an absent/inaccessible stream has nothing to grow from).

    APPEND-ONLY ASSUMPTION: all four telemetry JSONL streams are written append-only, so a
    size delta is a sufficient change signal here -- an in-place same-size rewrite would be
    missed, but cannot occur under an append-only writer."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st.st_size


def _stream_inode(path):
    """The st_ino of one friction-stream path, or None if it does not exist.

    Mirrors `_stream_size`'s stat shape exactly (same try/except, same S_ISREG gate) and reads a
    different field. It deliberately does NOT reuse `_stat_identity` (serve.py:78), for two
    independent reasons -- both of which would be bugs if ignored:

    1. `_stat_identity` returns (st_ino, st_mtime_ns), and mtime changes on EVERY append. Comparing
       the full tuple would classify every ordinary append as a rotation and force a full
       re-collect each sweep -- destroying the B2 cheap path `_classify_stream_sweep` exists to
       protect. Inode ALONE is the file-IDENTITY signal: it changes on the rename-aside-and-replace
       a log rotation performs, and does not change on an append.
    2. `_stat_identity` has NO S_ISREG gate -- it returns an identity for a directory or a FIFO --
       whereas `_stream_size` returns None for anything that is not a regular file. See the
       presence paragraph below.

    PRESENCE MUST MATCH `_stream_size`'s NOTION OF PRESENCE. `stream_offsets` and `stream_inodes`
    are read in the SAME pass by `_classify_stream_sweep`, so if the two helpers disagree about
    whether a path counts as present, one input produces two contradictory answers. A path that
    is a directory would be ABSENT to the size ladder and PRESENT to the identity check, and the
    rotation arm would fire on a stream the size ladder considers gone. The S_ISREG gate above
    makes the two dicts agree by construction rather than by coincidence.

    RESIDUAL, disclosed and not fixed: a filesystem that REUSES inode numbers can hand the
    replacement file the same st_ino as the file it replaced, in which case a same-size rotation
    stays invisible exactly as it is today. This narrows the blind spot; it does not close it."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return st.st_ino


def _stream_identity(path):
    """`(size, st_ino)` for one friction-stream path from a SINGLE stat, or `(None, None)`
    if it is absent or not a regular file.

    ONE stat, not two, is the whole point. `stream_offsets` and `stream_inodes` were previously
    captured in two separate passes, so a rotation landing BETWEEN them published an inconsistent
    pair -- a known size against a None inode. That pair is not a transient: `_classify_stream_sweep`
    reads size and prev as both-present, `_stream_rotated` returns False on the None inode, neither
    size arm fires, and the sweep classifies "none". Nothing reports a change, so no rebuild runs,
    so the inode is never reseeded -- the rotation check stays OFF for that stream indefinitely,
    reopening the blind spot TRK-022 closes (measured, with a positive control proving the same
    fixture and the same rotation classify "truncated" once the inode is seeded).

    Both halves come from one `os.stat`, so presence agrees by construction: either both are None
    or both are set. There is no interleaving that can produce a mixed pair."""
    try:
        st = os.stat(path)
    except OSError:
        return (None, None)
    if not stat.S_ISREG(st.st_mode):
        return (None, None)
    return (st.st_size, st.st_ino)


def _snapshot_stream_state(state):
    """`({key: size-or-None}, {key: st_ino-or-None})` for every configured stream, captured from
    ONE stat per path and used to seed `stream_offsets` and `stream_inodes` together.

    Replaces the former `_snapshot_stream_sizes` + `_snapshot_stream_inodes` pair, which were two
    passes and could publish a mixed pair (see `_stream_identity`). The sizes are still read BEFORE
    a render consumes the streams (FIX 1): seeding from a PRE-render lower bound means bytes
    appended during the render leave size>offset next sweep -- a safe redundant re-render -- rather
    than being recorded as already-consumed. The size value still carries the existence bit: None
    marks an absent/non-regular stream, kept DISTINCT from an empty one (size 0) so a create/delete
    transition stays detectable (FIX 2)."""
    sizes, inodes = {}, {}
    for key in _stream_paths_list(state.streams):
        size, ino = _stream_identity(key)
        sizes[key] = size
        inodes[key] = ino
    return sizes, inodes


def _stream_rotated(state, key):
    """True iff `key`'s CURRENT inode differs from the one seeded at the last publish -- i.e. the
    path still exists but now names a DIFFERENT file, the rename-aside-and-replace of a log
    rotation. Consulted by `_classify_stream_sweep` only on the both-present branch, where
    `_stream_size` has already proved the stream was a regular file at seed time and is one now,
    so the two inodes are directly comparable.

    An UNKNOWN inode on either side -- never seeded, or the file vanished between this sweep's
    size stat and this one -- returns False rather than FABRICATING a rotation: the size ladder
    still governs, and a genuinely vanished stream is caught next sweep by the present->absent
    branch. Failing this way keeps an unavailable signal from forcing an endless full re-collect."""
    prev_inode = state.stream_inodes.get(key)
    current_inode = _stream_inode(key)
    if prev_inode is None or current_inode is None:
        return False
    return current_inode != prev_inode


def _classify_stream_sweep(state):
    """(classification, changed_keys) for the CURRENT friction-stream state against
    `state.stream_offsets`. Classification tracks EXISTENCE, not just size (FIX 2):
      * a stream that flipped present->absent (DELETION) -> "truncated" (force a FULL
        re-collect so the collector/friction drop the removed file's records)
      * a stream that flipped absent->present (CREATION, even empty) -> "grown" (the cheap
        path re-reads every stream in full, so it correctly picks up a newly-created file)
      * a stream that shrank (rotation/truncation) -> "truncated"
      * a stream that grew (pure append) -> "grown"
      * a stream whose path still exists but now names a DIFFERENT FILE (st_ino changed -- the
        rename-aside-and-replace of a log rotation) -> "truncated", checked BEFORE the size
        comparison so it fires even when the replacement file is the SAME SIZE (which the size
        ladder alone reads as "no change") and even when it is LARGER (records still disappeared,
        so the cheap append-only path would serve stale friction data). It is an `elif` in the
        same chain, so a rotation that ALSO changed size appends the key exactly ONCE
        (TRK-022 finding 3, measured).
    "truncated" takes PRIORITY over "grown" (a shrink/deletion must force the full path,
    never a cheap re-render), which takes priority over "none" (nothing moved). A shrunk
    stream is NEVER treated as growth -- that offset-drift failure mode would read a
    negative-length tail if the cheap path ever sliced instead of re-reading in full."""
    truncated, grown = [], []
    for key in _stream_paths_list(state.streams):
        size = _stream_size(key)                 # None == absent now
        prev = state.stream_offsets.get(key)     # None == absent at seed (or never seeded)
        if size is None and prev is None:
            continue                             # still absent: no change
        if size is None:                         # present -> absent: DELETION
            truncated.append(key)
        elif prev is None:                       # absent -> present: CREATION (even empty)
            grown.append(key)
        elif _stream_rotated(state, key):         # same path, DIFFERENT file: ROTATION
            truncated.append(key)
        elif size < prev:                        # shrank: rotation/truncation
            truncated.append(key)
        elif size > prev:                        # pure append
            grown.append(key)
    if truncated:
        return "truncated", truncated
    if grown:
        return "grown", grown
    return "none", []


def _try_full_rebuild(state, out_dir, root, project_root, label, compose=False):
    """Runs one full `_rebuild`, containing a failure (CollectorError / RenderError /
    OSError) HERE -- logs "{label} failed, keeping last-good render" to stderr and returns
    False -- instead of letting it propagate. Extracted so every full-rebuild call site in
    `_watcher_loop` (the collector-input-change path, the truncated/force-full path, the
    C18 date-rollover degrade, and the cheap-path failure degrade) shares the identical
    keep-last-good/don't-advance-snapshot semantics behind one implementation."""
    try:
        _rebuild(state, out_dir, root, project_root, compose=compose)
    except (CollectorError, render_html.RenderError, OSError) as exc:
        print(f"harness-map watcher: {label} failed, keeping last-good render: {exc}",
              file=sys.stderr)
        return False
    return True


def _watcher_loop(state, out_dir, root, project_root, stop_event, poll_seconds, debounce_seconds,
                   compose=False):
    """Daemon poll loop: every `poll_seconds` build a fresh collector-input snapshot; on any
    difference enter a debounce that keeps re-sweeping until `debounce_seconds` pass with NO
    further change, then run ONE `_rebuild` (coalescing a burst of N writes into one refresh).
    A rebuild failure (CollectorError / RenderError / OSError) is contained HERE — logged,
    last-good ctx kept, no broadcast, loop continues — so a transient collect/render/write
    fault never kills the thread or serves a broken document. The stored snapshot advances
    only after a successful settled rebuild, so a persistent fault is retried on the next
    sweep rather than swallowed. An outer catch-all backstop (see the inline comment)
    additionally guarantees the thread survives any UNENUMERATED exception, so no unexpected
    fault can silently freeze the dashboard.

    B2/T5: when a sweep sees NO collector-input change, it separately classifies the four
    friction-telemetry JSONL streams (`_classify_stream_sweep`, since `_watched_snapshot`
    deliberately does not cover them — they are friction inputs, not collector inputs):
    a pure append ("grown") takes the cheap `_rebuild_friction_only` path (no debounce —
    it is cheap enough to run every settled sweep, and `collect_count` is NEVER bumped by
    it); a shrink/rotation ("truncated") resets that stream's offset to 0 and forces a FULL
    `_rebuild` instead (D5) so the offset never drifts negative. Any exception from the
    cheap path degrades to a full `_rebuild` (D5) rather than propagating.

    C18 PARITY-OR-DEGRADE: the cheap path re-renders using the CACHED `state.ctx.date` (set
    at the last full rebuild), but `build_friction_overlay` filters telemetry records
    against the CURRENT date -- across a local midnight, a cached yesterday's-date would
    exclude a new-day-dated record that a full recollect (today's date) would include. So
    BEFORE taking the "grown" cheap path, the loop compares today's date to `ctx.date`; on a
    mismatch it forces a full `_rebuild` instead, exactly like the "truncated" case."""
    while not stop_event.is_set():
        if stop_event.wait(poll_seconds):
            return
        # OUTER BACKSTOP: the enumerated (CollectorError/RenderError/OSError) tuples below
        # cannot cover every fault -- _watched_snapshot runs before them, and render_from_out_dir
        # can raise UNENUMERATED types (e.g. TypeError/KeyError/ValueError from an unexpected
        # sidecar/synthesis shape). An escape would kill this daemon thread and freeze the
        # dashboard on last-good FOREVER while SSE keeps heartbeating (browser looks healthy,
        # never refreshes). So the WHOLE per-iteration body is wrapped to log repr(e) and
        # continue WITHOUT advancing the snapshot. The stop-Event `return`s are not exceptions,
        # so the clean-shutdown path passes straight through this backstop untouched.
        try:
            # `today` is computed ONCE per sweep and reused for BOTH the synthesis-sidecar key
            # (Codex r3 FIX 1) and the C18 date-rollover check below, so the two never disagree
            # on the date within a single iteration.
            today = datetime.now().strftime("%Y-%m-%d")
            current = _watched_snapshot(root, project_root, compose)
            synth_current = _synthesis_value(out_dir, today)  # Codex r3 FIX 1
            if current != state.watch_snapshot or synth_current != state.synth_snapshot:
                # Change detected (a collector input OR the synthesis sidecar moved): debounce
                # until BOTH surfaces hold steady for one full window, then run ONE full rebuild.
                while not stop_event.is_set():
                    if stop_event.wait(debounce_seconds):
                        return
                    settled = _watched_snapshot(root, project_root, compose)
                    settled_synth = _synthesis_value(out_dir, today)
                    if settled == current and settled_synth == synth_current:
                        break
                    current = settled
                    synth_current = settled_synth
                if stop_event.is_set():
                    return
                # A synthesis change MUST take the full-rebuild path (it feeds the Coverage
                # Matrix + drag MODELS, which the cheap friction-only path reuses from cache and
                # would leave stale) -- `_try_full_rebuild` is the only rebuild called here.
                if not _try_full_rebuild(state, out_dir, root, project_root, label="rebuild",
                                          compose=compose):
                    continue  # do NOT advance either snapshot -> the change is retried next sweep
                state.watch_snapshot = current
                state.synth_snapshot = synth_current  # Codex r3 FIX 1: advance PRE-read value
                # `_rebuild` already re-seeded stream_offsets from the sizes it saw at its
                # start (FIX 1) -- no separate post-render re-seed here (that was the bug).
                continue

            # No collector-input change this sweep: B2 friction-stream classification.
            classification, changed = _classify_stream_sweep(state)
            if classification == "truncated":
                # FIX 4 (Codex challenge): REMEMBER the truncation as pending instead of zeroing
                # state.stream_offsets[key] before the fallible rebuild. Zeroing a truncate-to-
                # empty stream's offset made it compare 0==0 next sweep -> "none" -> the required
                # full recollect was lost forever if THIS rebuild then failed transiently. The
                # saved offset is left UNTOUCHED (a successful `_rebuild` re-seeds it from its own
                # PRE-read snapshot); the pending flag forces the full path until that succeeds.
                state.pending_truncation.update(changed)
            force_full = bool(state.pending_truncation) or _FORCE_FULL_RECOLLECT
            if classification == "none" and not force_full:
                continue
            if force_full:
                if not _try_full_rebuild(state, out_dir, root, project_root,
                                          label="full recollect", compose=compose):
                    continue  # keep pending_truncation set -> the truncation is retried next sweep
                state.pending_truncation.clear()  # a successful full rebuild consumed the truncation
                continue

            # classification == "grown": C18 date-rollover check BEFORE taking the cheap
            # path -- the cheap path reuses the CACHED ctx.date, which diverges from a full
            # recollect's `today` across a local midnight (see the docstring above). On a
            # mismatch, force a full rebuild instead of the cheap path.
            with state.lock:
                ctx_date = state.ctx.date
            # `today` was computed once at the top of this sweep (shared with the synthesis
            # key), so the rollover check here reads the SAME date the synthesis-tracking used.
            if ctx_date != today:
                if not _try_full_rebuild(state, out_dir, root, project_root,
                                          label="date-rollover recollect", compose=compose):
                    continue
                continue

            # classification == "grown", dates match: B2 cheap path -- degrades to a full
            # recollect (D5) on ANY failure, never publishing a partial/broken cheap render.
            try:
                _rebuild_friction_only(state, out_dir)
            except Exception as exc:  # noqa: BLE001 - deliberate degrade-to-full-rebuild fallback, NOT a swallow: an unenumerated cheap-render fault (e.g. TypeError/KeyError/ValueError from an unexpected friction-stream shape) falls back to the strictly-more-complete full rebuild path below; any failure THERE is handled by _try_full_rebuild's own error handling plus the outer per-iteration backstop.
                print(f"harness-map watcher: cheap friction re-render failed ({exc}), "
                      f"degrading to full recollect", file=sys.stderr)
                if not _try_full_rebuild(state, out_dir, root, project_root,
                                          label="full recollect fallback", compose=compose):
                    continue
            # Both the cheap `_rebuild_friction_only` and its full-rebuild fallback re-seed
            # stream_offsets from their own PRE-read snapshots (FIX 1) -- no re-seed here.
        except Exception as e:  # noqa: BLE001 - deliberate daemon-thread backstop: catches the UNENUMERATED types on purpose; does NOT swallow (logs repr) and does NOT advance the snapshot (retried next sweep).
            print(f"harness-map watcher: unexpected error, keeping last-good render: {e!r}",
                  file=sys.stderr)
            continue  # do NOT advance the snapshot -> the change is retried next sweep


class RequestHandler(BaseHTTPRequestHandler):
    # Narrows the inherited `server: BaseServer` to this server's actual concrete type
    # (`_Server`, defined below) so `self.server.state`/etc. type-check without a cast at
    # every call site — `_Server` IS-A `BaseServer`, so this is a type-safe narrowing.
    server: "_Server"

    # Idle-connection read timeout: reaps a connect-but-never-sends-a-request-line socket after
    # 10s, so a co-resident process cannot pin ThreadingHTTPServer threads forever by opening
    # sockets and going silent (local DoS). That is a socket-operation timeout, NOT an
    # application-layer idle timer: it fires only while a socket op is actually in flight and
    # blocked. On the read side that is the case above. On the write side it can fire too, but
    # only once the send genuinely blocks — see the buffer arithmetic below.
    #
    # What it does NOT do is act as an SSE idle timer. Measured: it does not fire during the queue
    # wait between keepalives, and it did not reap any of the 40 observed non-reading streams within
    # 25s. It can still surface on an established stream once a WRITE genuinely blocks -- which the
    # buffer arithmetic below puts at roughly 26 hours. An earlier version of this comment
    # claimed the opposite: that an SSE heartbeat MUST stay below this timeout or an open, healthy
    # stream would be misclassified as idle and closed. MEASURED FALSE in both directions on
    # Python 3.11.14 / Darwin. (a) A server built with
    # heartbeat=14.0, above this timeout, with a
    # continuously-reading client: still streaming at t+16s, keepalive delivered, no close; the
    # same probe at heartbeat=0.4 saw 8 keepalives in 3s, so the probe was working. (b) 40 streams
    # whose peer read the headers and then stopped reading were ALL still registered at t+25s --
    # measured BEFORE MAX_SSE_CLIENTS existed, so 40 is no longer reproducible against this file:
    # the cap now refuses past 16, and re-running that probe here registers 16, not 40. What the
    # measurement establishes is unchanged by the cap -- the timeout does not reach an established
    # stream -- and 16 is the number the cap bounds it to.
    #
    # The mechanism, so the claim is not reintroduced: between writes `_serve_events` blocks in
    # `queue.get(timeout=heartbeat)` — a Python queue wait, NOT a socket operation. No socket op is
    # in flight, so no socket timeout can fire. `settimeout` surfaces on a WRITE only when the send
    # actually blocks, which requires a full kernel buffer: a 13-byte keepalive every 3s against a
    # measured 408,128-byte client receive buffer is roughly 26 hours, and that is a lower bound.
    # The heartbeat interval and this timeout are independent; do not couple them. What bounds the
    # unreapable case is MAX_SSE_CLIENTS — see `_serve_events`' accepted-residual note.
    timeout = 10

    # Expected client-side end states on ANY write to the connection socket: a hard disconnect
    # (BrokenPipeError/ConnectionResetError), the 10s idle-read timeout surfacing as TimeoutError
    # (socket.timeout is now its alias), or a generic socket fault (bare OSError). The first three
    # are OSError subclasses; this is a specific, minimal tuple, NOT a broad `except Exception`.
    # Shared by BOTH the `GET /` body write (FIX 8) and the `/events` stream so a client that
    # disconnects mid-response exits quietly instead of letting socketserver print a traceback.
    _CLIENT_GONE = (BrokenPipeError, ConnectionResetError, TimeoutError, OSError)

    def _send_security_headers(self):
        """Anti-framing headers on EVERY response (200/400/404/503): this server holds the
        user's PRIVATE harness dashboard. Loopback binding alone does not stop a
        malicious external page from framing it in a hidden <iframe> if the user's
        browser can also reach 127.0.0.1 (clickjacking)."""
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")

    def do_GET(self):
        # DNS-rebinding guard, checked BEFORE any path dispatch: loopback binding does
        # not stop a malicious page the user visits from pointing an attacker-controlled
        # hostname at 127.0.0.1 via DNS, then issuing requests that carry that hostname
        # in Host. A legitimate client's Host is always the literal "127.0.0.1[:port]".
        host_header = self.headers.get("Host", "")
        hostname = host_header.split(":", 1)[0]
        if hostname != "127.0.0.1":
            self.send_response(400)
            self._send_security_headers()
            self.end_headers()
            return
        if self.path == "/":
            state = self.server.state
            with state.lock:
                body = state.ctx.html_bytes
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self._send_security_headers()
                self.end_headers()
                self.wfile.write(body)
            except self._CLIENT_GONE:
                # FIX 8: a client that RSTs/closes during the header or (large) body write raises
                # BrokenPipeError/ConnectionResetError/OSError from the socket write. Unlike
                # /events, this path did not catch it, so socketserver printed a traceback to
                # stderr (server survived, but noisy). Mirror _serve_events's `_CLIENT_GONE`
                # handling and exit quietly — a normal client disconnect is not a server error.
                return
        elif self.path == "/events":
            self._serve_events()
        else:
            self.send_response(404)
            self._send_security_headers()
            self.end_headers()

    def _serve_events(self):
        """Server-Sent Events stream: register a fresh bounded queue, then block on it and forward
        each `refresh` token as an SSE event. On an empty get (no refresh within one HEARTBEAT)
        write a `: keepalive` comment so the connection stays established. The heartbeat interval is
        INDEPENDENT of RequestHandler.timeout — an earlier version of this docstring said it must
        stay below it; see that attribute's comment for the measurement showing the coupling does
        not exist. A broken/reset/timed-out socket (on either the header write or a streaming write)
        unregisters the queue and returns; the finally guarantees unregistration on every exit path
        AFTER a successful registration. A refused registration returns before the try/finally and
        has nothing to unregister.

        Registration CAN be refused: at MAX_SSE_CLIENTS `register_client` returns None, and this
        handler answers 503 + Retry-After and returns before the stream loop, having registered
        nothing.

        Connect-time preamble: a `retry:` field (SSE_RETRY_SECONDS, in milliseconds) sets the
        reconnect interval of a stream that was ESTABLISHED and then DROPPED -- a server restart --
        explicitly, replacing an interval the WHATWG standard leaves user-agent-defined. It does
        nothing for a REFUSED client, whose stream never
        opened and so never received it. It carries no `data:`, so per the SSE spec it dispatches no
        event and never triggers a reload.

        FIX 4 reconnect resync: on connect the CURRENT build generation is sent immediately as an
        `event: sync` (before the blocking loop). A client compares it to the generation its page
        was rendered from and reloads only when the server is AHEAD -- so a refresh that was
        broadcast to a since-dead queue while the client was disconnected is caught up on reconnect,
        while a fresh page (equal generations) never reloads (no reload loop).

        ACCEPTED RESIDUAL -- a stalled stream is not reaped. There is no application-layer signal
        that an SSE peer has stopped reading: keepalive writes keep succeeding into the kernel send
        buffer until that buffer fills (roughly 26h at this keepalive size and interval -- see
        RequestHandler.timeout), and SSE has no acknowledgement, so for any practical horizon a peer
        that reads its headers and then stops is
        indistinguishable here from a healthy idle one (MEASURED: 40 such streams still registered
        at t+25s, 2.5x RequestHandler.timeout -- taken BEFORE MAX_SSE_CLIENTS existed; re-running
        that probe against this file registers 16, since the cap refuses the rest. The
        indistinguishability is what the measurement shows, and the cap does not change it).
        The portable levers all cost more than the gap --
        shrinking SO_SNDBUF moves ~26h to ~15min without closing it, TCP_USER_TIMEOUT is Linux-only
        while this module is stdlib-only and macOS-tested, and a total-lifetime cap would close the
        healthy long-lived dashboards this server exists to serve. MAX_SSE_CLIENTS is the mitigation
        rather than the cure: with registrations bounded at 16, stalled streams pin at most 16
        threads, which is the local-DoS an idle reap would have prevented. The bound has its own
        cost, stated rather than glossed: 16 stalled streams refuse a legitimate 17th tab, and that
        tab most likely stays dead until the user reloads the page -- per the WHATWG EventSource
        processing model a non-200 response FAILS the connection (fire `error`, readyState = CLOSED,
        no reconnect), Retry-After is not an EventSource input, and this stream's `retry:` never
        arrives because the stream never opened. render_html.py's `error` handler is empty and
        relies entirely on auto-reconnect. That browser consequence is reasoned from the spec and
        the shipped client, NOT driven in a browser. This is disclosed, not closed."""
        state = self.server.state
        heartbeat = getattr(self.server, "heartbeat_seconds", HEARTBEAT_SECONDS)
        # Register the client queue BEFORE capturing the generation (Codex r2 residual-race
        # fix). register_client() uses clients_lock; the generation read below uses state.lock;
        # a publish takes state.lock (bump generation) THEN clients_lock (broadcast). Registering
        # first makes registration+generation-capture atomic w.r.t. publication: a publish fully
        # BEFORE registration also bumped the generation before this read -> the connect-time
        # sync reports it (client reloads iff ahead); a publish AFTER registration reaches this
        # queue via broadcast (client reloads). Reversing the order (read generation, then
        # register) leaves a gap where an interleaved publish misses the not-yet-registered queue
        # AND advances past the already-read generation -> the page stays stale until the next
        # rebuild. An equal generation on a fresh connect still never reloads (no loop).
        client_queue = state.register_client()
        if client_queue is None:
            # MAX_SSE_CLIENTS streams are already registered. 503 + Retry-After is the honest answer
            # for a temporary capacity limit, and a non-browser client can act on it. A BROWSER tab
            # most likely will not: per the WHATWG EventSource processing model a non-200 response
            # fails the connection (error fires, readyState = CLOSED, no reconnect) and Retry-After
            # is not an EventSource input -- so a refused tab most likely stays dead until the user
            # reloads. That is reasoned from the spec plus render_html.py's empty `error` handler,
            # NOT driven in a browser. Refusal is signalled by returning None rather than raising,
            # so the refusal is ordinary control flow -- and that makes the `is None` branch this
            # comment sits inside MANDATORY, not optional: every queue operation below it (the
            # client_queue.get in the stream loop, the unregister_client in the finally) assumes a
            # real queue. This returns BEFORE that try/finally because there is no stream to serve.
            # `unregister_client(None)` also happening not to raise (its
            # contextlib.suppress(ValueError) swallows list.remove(None)) is SEPARATE defensive
            # behavior, covering the idempotent double-unregister; it is not the reason this branch
            # is safe and it does not remove the need for this guard. Response shape matches the
            # 404 in do_GET,
            # with the same _CLIENT_GONE guard the 200 header write below already uses.
            try:
                self.send_response(503)
                self.send_header("Retry-After", str(SSE_RETRY_SECONDS))
                self._send_security_headers()
                self.end_headers()
            except self._CLIENT_GONE:
                pass  # client vanished during the refusal write -> nothing left to tell it
            return
        # The try/finally opens IMMEDIATELY after a successful registration -- before the
        # generation read below -- so the docstring's "every exit path" is literal rather than
        # nearly-true. Registration has already mutated state.clients at this point; anything
        # raising between here and the stream loop would otherwise leak that slot PERMANENTLY,
        # and with a hard MAX_SSE_CLIENTS ceiling a leaked slot is capacity lost for the life of
        # the process, not a transient. Nothing in the generation read can raise in ordinary
        # operation (an uncontended threading.Lock acquire does not, and state.generation is a
        # plain int attribute), so this closes a window that is structural rather than observed.
        try:
            with state.lock:
                current_gen = state.generation
            # A stuck/slow-reading or disconnected client makes wfile.write/flush raise on the
            # connection socket (TimeoutError after the 10s idle timeout, BrokenPipeError/
            # ConnectionResetError on a hard disconnect, bare OSError otherwise). All are expected
            # client-side end states, NOT server bugs -- catch them via the shared `_CLIENT_GONE`
            # tuple (identical to the GET `/` handler, FIX 8) and return cleanly (the finally
            # unregisters) so socketserver never prints a traceback to stderr.
            _client_gone = self._CLIENT_GONE
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self._send_security_headers()
                self.end_headers()
            except _client_gone:
                return  # client vanished during the header write -> nothing to stream
            try:
                # FIX 4: report the current generation on (re)connect so a client that missed
                # a refresh while disconnected can catch up (reload iff serverGen > pageGen).
                # `retry:` sets the client's reconnect interval (MILLISECONDS -- hence the *1000)
                # explicitly, replacing a value the WHATWG standard leaves user-agent-defined.
                # What that actually buys: a stream that was ESTABLISHED and
                # then DROPPED -- a server restart -- comes back at an interval THIS SERVER chose,
                # rather than one it does not control.
                # It does NOT reach a client refused by MAX_SSE_CLIENTS: that
                # stream never opened, so this field never arrived (see SSE_RETRY_SECONDS). It
                # carries no `data:`, so per the SSE spec it dispatches no event and can never
                # trigger a reload. Written in the SAME single write+flush as the sync below, so it
                # shares that write's existing _CLIENT_GONE boundary and adds no new failure point.
                # That is NOT a claim of atomic delivery: a write is not transactional at the socket
                # layer, so a prefix can still reach the peer before an error.
                # The parentheses are load-bearing for the READER, not the parser: implicit
                # string-literal concatenation already binds before `.encode`, so both literals are
                # encoded either way -- but unparenthesized, `.encode` reads as if it applied to the
                # second literal alone. It misled this change's own author once already.
                self.wfile.write(
                    (f"retry: {SSE_RETRY_SECONDS * 1000}\n\n"
                     f"event: sync\ndata: {current_gen}\n\n").encode("ascii"))
                self.wfile.flush()
            except _client_gone:
                return
            while True:
                try:
                    client_queue.get(timeout=heartbeat)
                    payload = b"event: refresh\ndata: 1\n\n"
                except queue.Empty:
                    payload = b": keepalive\n\n"
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except _client_gone:
                    return
        finally:
            state.unregister_client(client_queue)

    def log_message(self, format_str, *args):
        """Silences the default per-request stderr logging so serve's stdout/stderr
        stays clean."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    # Set post-construction by build_server (never in __init__, so ThreadingHTTPServer's
    # own __init__ signature/behavior stays untouched) — declared here only so
    # RequestHandler/_watcher_loop/main() read them through a known attribute type
    # instead of an implicit Any per call site.
    state: "_State"
    out_dir: Path
    root: Path
    project_root: Path
    heartbeat_seconds: float
    _poll_seconds: float
    _debounce_seconds: float
    _watcher_stop: threading.Event
    _watcher_thread: threading.Thread | None


def _build_streams(no_friction, root):
    """Mirrors render_html.main's --no-friction branching exactly: delegates to the
    shared render_html.default_streams() helper (real ~/.claude JSONL paths) unless
    friction is disabled, in which case None (render_from_out_dir treats a None streams
    value as the all-None/disabled dict).

    `root` is FORWARDED (§4.5, finding #3): default_streams offers the interventions log
    only when the selected root IS the harness root. Dropping it here would leave serve
    mode uncontained while the CLI path is fixed -- the worst of both."""
    if no_friction:
        return None
    return render_html.default_streams(root=root)


def build_server(
    out_dir: Path,
    root: Path,
    project_root: Path,
    host: str = "127.0.0.1",
    port: int = 0,
    no_friction: bool = False,
    streams: dict[str, Any] | None = None,
    watch: bool = False,
    poll_seconds: float = POLL_SECONDS,
    debounce_seconds: float = DEBOUNCE_SECONDS,
    heartbeat: float = HEARTBEAT_SECONDS,
    compose: bool = False,
) -> _Server:
    """Validates `host`, builds the friction `streams` dict (unless one is supplied),
    runs one `_rebuild`, then constructs the threading server bound to shared state.
    Lets any collect/render/write exception from the initial `_rebuild` propagate, so
    a bad out-dir fails fast at startup rather than serving with no ctx.

    When `watch=True` (main() always passes it; tests opt in and shrink the timings) a
    daemon watcher thread is started and stored as `server._watcher_thread`, with a
    `server._watcher_stop` Event that main()'s shutdown path signals + joins. The watch
    snapshot is seeded from the same source of truth the watcher polls, BEFORE the initial
    `_rebuild` reads the inputs (so a change concurrent with the first rebuild stays visible
    as a diff on the first sweep) and BEFORE the thread starts. On a CLEAN startup the
    baseline equals what the first sweep computes (out_dir is outside root, so the rebuild
    mutates no watched path), so the first sweep only fires on a genuine post-startup change.

    `compose` (T8, default False): propagates `--compose` to every collector run (initial +
    every watcher rebuild) and switches the watched-set/out-dir guard to two-tier mode."""
    host = _validate_host(host)
    out_dir = Path(out_dir)
    root = Path(root)
    project_root = Path(project_root)
    # T8 P1-6b: both-root out-dir/write guard, routed through T2's SHARED
    # collector.validate_write_target (the same guard collector.py --out and
    # render_html.write_html_safely reuse) -- a startup fail-fast BEFORE any collect/render/
    # write is attempted. Non-compose guards ONLY the operator root (write_html_safely's own
    # single-root check already covers this at render time; this adds an earlier, cleaner
    # failure). Compose mode ALSO guards the project-containment-root and rejects a target
    # equal to any compose-mode collector input (e.g. `~/.claude.json`), reusing T2's own
    # `input_paths=` clause rather than re-implementing it.
    guard_roots = [root]
    guard_input_paths = []
    if compose:
        guard_roots.append(project_root)
        guard_input_paths = collector.iter_input_paths(root, project_root, compose=True)
    ok, _resolved = collector.validate_write_target(out_dir, guard_roots, guard_input_paths)
    if not ok:
        raise ValueError(f"--out-dir must be outside the guarded root(s): {out_dir}")
    if streams is None:
        streams = _build_streams(no_friction, root)
    state = _State(streams=streams, no_friction=no_friction)
    # Capture the watch baseline BEFORE the initial `_rebuild` reads the inputs (Codex r2
    # residual-race fix), mirroring the stream-offset PRE-read seeding below: a watched harness
    # file mutated concurrently with the first rebuild then stays VISIBLE as a diff on the first
    # watcher sweep (safe direction -- at worst one extra early re-render, never a stale serve).
    # Seeding AFTER the rebuild would record the mutated fs state as baseline while state.ctx
    # still holds the pre-mutation render -> the first sweep sees no diff and serves stale until
    # the next edit. The collector enforces out_dir OUTSIDE root, so the rebuild never mutates a
    # watched path -> on a CLEAN startup this pre-rebuild baseline still equals what the first
    # sweep computes (no spurious re-render).
    state.watch_snapshot = _watched_snapshot(root, project_root, compose)
    # Codex r3 FIX 1: seed the synthesis-sidecar baseline BEFORE the initial `_rebuild`, using
    # the same `today` the rebuild renders at, so a synthesis write concurrent with startup
    # stays visible as a diff on the first sweep (safe direction) and a CLEAN startup (synthesis
    # unchanged by the rebuild -- it lives in out_dir and is READ, never written, by `_rebuild`)
    # produces no spurious first-sweep re-render.
    state.synth_snapshot = _synthesis_value(out_dir, datetime.now().strftime("%Y-%m-%d"))
    # The initial `_rebuild` seeds stream_offsets from the sizes it captured at its START
    # (FIX 1) -- consistent with every subsequent rebuild, no separate post-render re-seed.
    # Codex r3 FIX 2: an UNENUMERATED render fault at startup (e.g. a TypeError from a malformed
    # synthesis sidecar deep in render_from_out_dir) is a TERMINAL startup failure -- normalize
    # it to RenderError so main()'s existing (CollectorError/RenderError/OSError/SystemExit)
    # catch prints a clean "fatal: could not start server" instead of a bare traceback. The
    # enumerated startup exceptions propagate UNCHANGED (re-raised) so their type/message are
    # preserved; this normalizes ONLY the unexpected ones and is NOT a swallow (always re-raised).
    try:
        _rebuild(state, out_dir, root, project_root, compose=compose)
    except (CollectorError, render_html.RenderError, OSError, SystemExit):
        raise
    except Exception as exc:  # noqa: BLE001 - startup-failure normalizer (Codex r3 FIX 2): re-raises, never swallows
        raise render_html.RenderError(f"startup render failed: {exc}") from exc
    server = _Server((host, port), RequestHandler)
    server.state = state
    server.out_dir = out_dir
    server.root = root
    server.project_root = project_root
    server.heartbeat_seconds = heartbeat
    server._poll_seconds = poll_seconds
    server._debounce_seconds = debounce_seconds
    stop_event = threading.Event()
    server._watcher_stop = stop_event
    if watch:
        watcher = threading.Thread(
            target=_watcher_loop,
            args=(state, out_dir, root, project_root, stop_event, poll_seconds, debounce_seconds,
                  compose),
            name="harness-map-watcher", daemon=True)
        watcher.start()
        server._watcher_thread = watcher
    else:
        server._watcher_thread = None
    return server


def _warn_if_synthesis_missing(out_dir, date):
    """One-time startup advisory (stderr only, NEVER blocks startup): warn when the synthesis
    sidecar the initial render SELECTED for `date` is absent, so the CIVC coverage matrix +
    drag-candidate table render EMPTY. serve.py is stdlib-only with NO model access -- it can
    NEVER generate the synthesis sidecar (that is the skill's opus Step B); this only reports
    the gap. `date` MUST be the date the render actually selected (server.state.ctx.date ==
    render_from_out_dir's sel_date), reusing `_synthesis_path` so the checked file is EXACTLY
    the one render tried to load -- never a recomputed today(). The entire body is wrapped in a
    bare except: a startup ADVISORY must NEVER break serving, so any fault here (a stat error on
    the synthesis path, a BrokenPipeError/OSError writing to a closed stderr) is swallowed and
    the server still binds + serves the deterministic dashboard."""
    try:
        synth_path = _synthesis_path(out_dir, date)
        if synth_path.exists():
            return
        print(
            f"harness-map: no synthesis sidecar for {date} at {synth_path} -- the CIVC coverage "
            f"matrix and drag-candidate table will render EMPTY until you run the skill's Step B "
            f"synthesis (writes {synth_path.name}). The deterministic dashboard "
            f"(headline/treemap/friction) is unaffected.",
            file=sys.stderr, flush=True)
    except Exception:
        # Non-blocking by construction: an advisory must never suppress the "Serving..." line or
        # prevent serve_forever(). Swallow every fault (stat error, closed-stderr BrokenPipeError).
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Serve a live harness-map dashboard over loopback HTTP.")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--root", default=str(Path.home() / ".claude"))
    ap.add_argument("--project-root", default=str(Path.home() / ".claude"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-friction", action="store_true")
    ap.add_argument("--compose", action="store_true",
                     help="Compose operator ⊕ project tiers (see collector.py --compose): "
                          "propagated to every collector run and to the two-tier watched-set/"
                          "out-dir guard. Default (unset) behavior is unchanged (operator-only).")
    args = ap.parse_args(argv)

    try:
        host = _validate_host(args.host)
    except ValueError as e:
        ap.error(str(e))

    try:
        server = build_server(
            out_dir=Path(args.out_dir), root=Path(args.root), project_root=Path(args.project_root),
            host=host, port=args.port, no_friction=args.no_friction, watch=True,
            compose=args.compose)
    except (CollectorError, render_html.RenderError, OSError, SystemExit, ValueError) as e:
        # RenderError here now ALSO covers write_html_safely's inside-root guard rejection
        # inside build_server's startup _rebuild call -- it used to raise a bare SystemExit
        # (P1-B, Codex challenge; changed because a live serve run's watcher-loop degrade
        # handlers, which only catch Exception, could not catch a BaseException escaping
        # mid-run). SystemExit is kept in this tuple as defense-in-depth for any other
        # startup fault that might still raise it (argparse's own --host SystemExit already
        # happened above, before this try, and is deliberately NOT caught here) -- treat any
        # of these as a clean startup failure, not a bare traceback. ValueError (T8) is
        # build_server's own both-root out-dir guard rejecting `--out-dir` up front.
        print(f"fatal: could not start server: {e}", file=sys.stderr)
        return 1
    # server_address's typeshed stub allows a bytes host (AF_UNIX sockets); this server
    # only ever binds AF_INET to a validated str host (_validate_host above), so this is a
    # pure type narrowing with zero runtime effect.
    bound_host = cast(str, server.server_address[0])
    bound_port = server.server_address[1]
    # Startup advisory (stderr, non-blocking): the synthesis sidecar for the date the initial
    # render selected feeds the CIVC coverage matrix + drag table. serve.py has NO model and
    # cannot generate it (that is the skill's opus Step B) -- if it is absent, warn ONCE here so
    # the operator knows the matrix will be empty until Step B runs. Uses the render's ACTUAL
    # selected date (state.ctx.date == sel_date), never a recomputed today(); emitted BEFORE the
    # flushed "Serving..." line so a piped reader observing that line already has this warning.
    _warn_if_synthesis_missing(server.out_dir, server.state.ctx.date)
    # FIX 5: flush explicitly -- when the server is backgrounded with stdout piped (block-
    # buffered, not a TTY), an unflushed line sits in the buffer and never reaches the reader
    # before serve_forever() blocks, breaking the "background it and read stdout for the
    # OS-assigned port" workflow.
    print(f"Serving http://{bound_host}:{bound_port}/ (Ctrl-C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        stop_event = getattr(server, "_watcher_stop", None)
        if stop_event is not None:
            stop_event.set()
        watcher = getattr(server, "_watcher_thread", None)
        if watcher is not None:
            watcher.join(timeout=max(server._poll_seconds, server._debounce_seconds) + 5)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
