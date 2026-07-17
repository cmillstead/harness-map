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

_MODULE_DIR = Path(__file__).resolve().parent


def _load_sibling(name, filename):
    """Loads a sibling module by absolute path (never relying on the invoking cwd or
    sys.path), mirroring the spec_from_file_location pattern the test suite already
    uses for the same modules."""
    spec = importlib.util.spec_from_file_location(name, _MODULE_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collector = _load_sibling("harness_map_serve_collector", "collector.py")
render_html = _load_sibling("harness_map_serve_render_html", "render_html.py")

# Loopback allowlist (single-value, never a blocklist): the ONLY host this server may bind.
_ALLOWED_HOSTS = {"127.0.0.1"}

# Realtime-watcher timings. POLL: base sweep interval. DEBOUNCE: quiet window a burst must
# clear before ONE settled rebuild fires. HEARTBEAT: SSE keepalive interval — MUST stay
# below RequestHandler.timeout (10s) or an established, healthy stream is misread as idle
# and reaped. Tests shrink all three via build_server kwargs; these DEFAULTS are production.
POLL_SECONDS = 2.0
DEBOUNCE_SECONDS = 1.0
HEARTBEAT_SECONDS = 3.0


class CollectorError(Exception):
    """Raised when collector.main() fails to produce a fresh sidecar (P30 guard)."""


def _validate_host(host):
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


def _run_collector(root, project_root, sidecar_path):
    """Runs collector.main() in-process, suppressing its always-on JSON stdout print.

    P30 SIDECAR-FRESHNESS GUARD: collector.main() returns 0 even when the `--out`
    write was skipped or failed (it swallows the write OSError and also skips the
    write entirely if `--root` is inaccessible) — a transiently-unreadable root
    yields return 0 with a STALE sidecar on disk. Trusting the return code alone
    would serve a stale render, so freshness is verified via inode identity: snapshot
    the sidecar's (st_ino, st_mtime_ns) before the call, require it EXISTS and DIFFERS
    after the call.
    """
    pre = _stat_identity(sidecar_path)
    argv = ["--root", str(root), "--project-root", str(project_root), "--out", str(sidecar_path)]
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
    friction configuration the server was started with.
    """

    def __init__(self, streams, no_friction):
        self.lock = threading.Lock()
        self.ctx = None
        self.collect_count = 0
        self.streams = streams
        self.no_friction = no_friction
        # SSE fan-out: bounded per-client queues, mutated ONLY under clients_lock (never
        # the ctx lock, so a slow /events writer cannot block a GET `/` read). The watcher
        # compares fresh snapshots against watch_snapshot to decide when a rebuild is due.
        self.clients_lock = threading.Lock()
        self.clients = []
        self.watch_snapshot = {}

    def register_client(self):
        """Add and return a fresh bounded (maxsize=1) queue for one SSE connection. The
        bound + Full-coalescing broadcast means a stalled client holds at most ONE pending
        refresh — a single reload subsumes every refresh it missed."""
        client_queue = queue.Queue(maxsize=1)
        with self.clients_lock:
            self.clients.append(client_queue)
        return client_queue

    def unregister_client(self, client_queue):
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


def _rebuild(state, out_dir, root, project_root):
    """Write-then-publish (publish only AFTER the fallible on-disk write succeeds):
    1. run the collector (P30-guarded) to produce today's sidecar
    2. render in memory from that sidecar
    3. write the HTML artifact to disk (the operation that can raise OSError)
    4. ONLY on write success: atomically publish state.ctx + bump collect_count
    5. broadcast a `refresh` to every connected SSE client

    Any exception raised before step 4 propagates to the caller (the watcher loop catches
    it); the last-good `state.ctx` is left untouched (no swap, no counter increment, no
    broadcast) so a mid-serve collect/render/write fault never publishes a broken document.
    """
    out_dir = Path(out_dir)
    today = datetime.now().strftime("%Y-%m-%d")
    sidecar_path = out_dir / f"harness-map-{today}.json"
    _run_collector(root, project_root, sidecar_path)
    # P30 ACCEPTED TOCTOU WINDOW: _run_collector's freshness check reads the sidecar's
    # inode identity, then render_from_out_dir below re-opens and re-reads the same
    # sidecar path itself — a co-resident process could swap the file in that narrow gap.
    # Same accepted-risk class collector.py documents for its own write path (single-user
    # loopback tool; not fully closed, deliberately not hardened further here).
    ctx = render_html.render_from_out_dir(
        out_dir, date=today, streams=state.streams, no_friction=state.no_friction)
    html_path = out_dir / f"harness-map-{today}.html"
    render_html.write_html_safely(html_path, ctx.html_text, ctx.doc.get("root"))
    with state.lock:
        state.ctx = ctx
        state.collect_count += 1
    _broadcast_refresh(state)


def _path_value(path):
    """Snapshot value for ONE watched path, stat FOLLOWING symlinks (os.stat/os.path.realpath,
    never lstat) so a change to a deploy-symlink TARGET living OUTSIDE --root is still observed
    (skill dirs are deploy symlinks — that missed-target case is why iter_input_paths, not a
    hand-kept list, is the source of truth). Returns a small comparable tuple:
      * missing:      (False, None, None, None)
      * directory:    (True, "dir", sorted-listdir tuple | None, symlink-target mtime | None)
      * regular file: (True, "file", (mtime_ns, size), symlink-target mtime | None)
    A container dir's sorted-listdir membership flips when a skill/hook/rule/agent/project is
    added or removed (even an EMPTY dir appearing). An unreadable dir's listdir OSError degrades
    to None membership — the existence bit still records the flip."""
    link_target_mtime = None
    if os.path.islink(path):
        # os.stat below already follows the link, but fold the realpath target's mtime in
        # explicitly so intent is unmistakable and a multi-hop link is covered.
        try:
            link_target_mtime = os.stat(os.path.realpath(path)).st_mtime_ns
        except OSError:
            link_target_mtime = None
    try:
        st = os.stat(path)  # follows symlinks
    except OSError:
        return (False, None, None, None)
    if stat.S_ISDIR(st.st_mode):
        try:
            members = tuple(sorted(os.listdir(path)))
        except OSError:
            members = None
        return (True, "dir", members, link_target_mtime)
    return (True, "file", (st.st_mtime_ns, st.st_size), link_target_mtime)


def _watched_snapshot(root, project_root=None):
    """Point-in-time snapshot {Path: value} of the ENTIRE collector input surface, keyed by
    exactly the paths collector.iter_input_paths yields — so the watched set can never drift
    from what the collector reads (a new collector input added THERE is watched automatically).
    A snapshot inequality (any file mtime/size, any container membership/existence, or any
    symlink-target mtime changed) is the watcher's re-render signal."""
    return {path: _path_value(path)
            for path in collector.iter_input_paths(root, project_root)}


def _watcher_loop(state, out_dir, root, project_root, stop_event, poll_seconds, debounce_seconds):
    """Daemon poll loop: every `poll_seconds` build a fresh snapshot; on any difference enter a
    debounce that keeps re-sweeping until `debounce_seconds` pass with NO further change, then
    run ONE `_rebuild` (coalescing a burst of N writes into one refresh). A rebuild failure
    (CollectorError / RenderError / OSError) is contained HERE — logged, last-good ctx kept,
    no broadcast, loop continues — so a transient collect/render/write fault never kills the
    thread or serves a broken document. The stored snapshot advances only after a successful
    settled rebuild, so a persistent fault is retried on the next sweep rather than swallowed."""
    while not stop_event.is_set():
        if stop_event.wait(poll_seconds):
            return
        current = _watched_snapshot(root, project_root)
        if current == state.watch_snapshot:
            continue
        # Change detected: debounce until the snapshot holds steady for one full window.
        while not stop_event.is_set():
            if stop_event.wait(debounce_seconds):
                return
            settled = _watched_snapshot(root, project_root)
            if settled == current:
                break
            current = settled
        if stop_event.is_set():
            return
        try:
            _rebuild(state, out_dir, root, project_root)
        except (CollectorError, render_html.RenderError, OSError) as exc:
            print(f"harness-map watcher: rebuild failed, keeping last-good render: {exc}",
                  file=sys.stderr)
            continue  # do NOT advance the snapshot -> the change is retried next sweep
        state.watch_snapshot = current


class RequestHandler(BaseHTTPRequestHandler):
    # Idle-connection read timeout: reaps a connect-but-never-sends-a-request-line socket
    # after 10s, so a co-resident process cannot pin ThreadingHTTPServer threads forever by
    # opening sockets and going silent (local DoS). NOTE for the forthcoming SSE /events
    # handler (a later task): an established SSE stream WRITES rather than reads after its
    # initial request, so its heartbeat interval MUST stay below this timeout, or an
    # open, healthy stream will be misclassified as idle and closed.
    timeout = 10

    def _send_security_headers(self):
        """Anti-framing headers on EVERY response (200/400/404): this server holds the
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
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_security_headers()
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self._serve_events()
        else:
            self.send_response(404)
            self._send_security_headers()
            self.end_headers()

    def _serve_events(self):
        """Server-Sent Events stream: register a fresh bounded queue, then block on it and
        forward each `refresh` token as an SSE event. On an empty get (no refresh within one
        HEARTBEAT) write a `: keepalive` comment so the connection stays established — the
        heartbeat interval is < RequestHandler.timeout (10s), otherwise a healthy stream is
        misclassified as idle and reaped. A broken/reset socket unregisters the queue and
        returns; the finally guarantees unregistration on every exit path."""
        state = self.server.state
        heartbeat = getattr(self.server, "heartbeat_seconds", HEARTBEAT_SECONDS)
        client_queue = state.register_client()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._send_security_headers()
            self.end_headers()
            while True:
                try:
                    client_queue.get(timeout=heartbeat)
                    payload = b"event: refresh\ndata: 1\n\n"
                except queue.Empty:
                    payload = b": keepalive\n\n"
                try:
                    self.wfile.write(payload)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
        finally:
            state.unregister_client(client_queue)

    def log_message(self, format_str, *args):
        """Silences the default per-request stderr logging so serve's stdout/stderr
        stays clean."""


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def _build_streams(no_friction):
    """Mirrors render_html.main's --no-friction branching exactly: delegates to the
    shared render_html.default_streams() helper (real ~/.claude JSONL paths) unless
    friction is disabled, in which case None (render_from_out_dir treats a None streams
    value as the all-None/disabled dict)."""
    if no_friction:
        return None
    return render_html.default_streams()


def build_server(out_dir, root, project_root, host="127.0.0.1", port=0,
                  no_friction=False, streams=None, watch=False,
                  poll_seconds=POLL_SECONDS, debounce_seconds=DEBOUNCE_SECONDS,
                  heartbeat=HEARTBEAT_SECONDS):
    """Validates `host`, builds the friction `streams` dict (unless one is supplied),
    runs one `_rebuild`, then constructs the threading server bound to shared state.
    Lets any collect/render/write exception from the initial `_rebuild` propagate, so
    a bad out-dir fails fast at startup rather than serving with no ctx.

    When `watch=True` (main() always passes it; tests opt in and shrink the timings) a
    daemon watcher thread is started and stored as `server._watcher_thread`, with a
    `server._watcher_stop` Event that main()'s shutdown path signals + joins. The watch
    snapshot is seeded BEFORE the thread starts, so the very first sweep only fires on a
    genuine post-startup change, never on startup state."""
    host = _validate_host(host)
    out_dir = Path(out_dir)
    root = Path(root)
    project_root = Path(project_root)
    if streams is None:
        streams = _build_streams(no_friction)
    state = _State(streams=streams, no_friction=no_friction)
    _rebuild(state, out_dir, root, project_root)
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
    # Seed the snapshot from the same source of truth the watcher polls, BEFORE starting it.
    state.watch_snapshot = _watched_snapshot(root, project_root)
    if watch:
        watcher = threading.Thread(
            target=_watcher_loop,
            args=(state, out_dir, root, project_root, stop_event, poll_seconds, debounce_seconds),
            name="harness-map-watcher", daemon=True)
        watcher.start()
        server._watcher_thread = watcher
    else:
        server._watcher_thread = None
    return server


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Serve a live harness-map dashboard over loopback HTTP.")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--root", default=str(Path.home() / ".claude"))
    ap.add_argument("--project-root", default=str(Path.home() / ".claude"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--no-friction", action="store_true")
    args = ap.parse_args(argv)

    try:
        host = _validate_host(args.host)
    except ValueError as e:
        ap.error(str(e))

    try:
        server = build_server(
            out_dir=Path(args.out_dir), root=Path(args.root), project_root=Path(args.project_root),
            host=host, port=args.port, no_friction=args.no_friction, watch=True)
    except (CollectorError, render_html.RenderError, OSError, SystemExit) as e:
        # SystemExit here can ONLY come from write_html_safely's inside-root guard inside
        # build_server's startup _rebuild call (argparse's own --host SystemExit already
        # happened above, before this try, and is deliberately NOT caught here) — treat it
        # as a clean startup failure like the other three, not a bare traceback.
        print(f"fatal: could not start server: {e}", file=sys.stderr)
        return 1
    bound_host, bound_port = server.server_address[0], server.server_address[1]
    print(f"Serving http://{bound_host}:{bound_port}/ (Ctrl-C to stop)")
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
