#!/usr/bin/env python3
"""harness-map live server (B1 read path): a loopback-only HTTP server that holds one
in-memory rendered HTML document and serves it from memory on GET `/`. Imports
`collector` and `render_html` in-process (via sibling-path importlib loading, so this
works regardless of the invoking cwd), runs one collect+render at startup, and serves
the resulting bytes. SSE broadcast and the filesystem watcher land in a later task —
this module implements ONLY the initial collect+render and the read path.
"""
import argparse
import contextlib
import importlib.util
import io
import os
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


def _broadcast_refresh(state):
    """No-op stub — SSE clients are wired to this in a later task."""


def _rebuild(state, out_dir, root, project_root):
    """Write-then-publish (publish only AFTER the fallible on-disk write succeeds):
    1. run the collector (P30-guarded) to produce today's sidecar
    2. render in memory from that sidecar
    3. write the HTML artifact to disk (the operation that can raise OSError)
    4. ONLY on write success: atomically publish state.ctx + bump collect_count
    5. broadcast (no-op stub for now)

    Any exception raised before step 4 propagates to the caller; the last-good
    `state.ctx` is left untouched (no swap, no counter increment).
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
        else:
            self.send_response(404)
            self._send_security_headers()
            self.end_headers()

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
                  no_friction=False, streams=None):
    """Validates `host`, builds the friction `streams` dict (unless one is supplied),
    runs one `_rebuild`, then constructs the threading server bound to shared state.
    Lets any collect/render/write exception from the initial `_rebuild` propagate, so
    a bad out-dir fails fast at startup rather than serving with no ctx."""
    host = _validate_host(host)
    out_dir = Path(out_dir)
    if streams is None:
        streams = _build_streams(no_friction)
    state = _State(streams=streams, no_friction=no_friction)
    _rebuild(state, out_dir, root, project_root)
    server = _Server((host, port), RequestHandler)
    server.state = state
    server.out_dir = out_dir
    server.root = Path(root)
    server.project_root = Path(project_root)
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
            host=host, port=args.port, no_friction=args.no_friction)
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
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
