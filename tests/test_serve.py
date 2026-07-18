import dataclasses
import datetime
import http.client
import importlib.util
import json
import os
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

from test_render_html import _minimal_doc, _write_sidecar  # reuse fixtures

SERVE = Path(__file__).resolve().parents[1] / "serve.py"
_spec = importlib.util.spec_from_file_location("harness_map_serve", SERVE)
srv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(srv)


def _get_root_body(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return body


@pytest.fixture
def live_server(tmp_path):
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    root = tmp_path / "fakeroot"
    root.mkdir()   # collector root (outside out_dir)
    server = srv.build_server(out_dir=out_dir, root=root, project_root=root,
                              host="127.0.0.1", port=0, no_friction=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield server, out_dir
    server.shutdown()
    server.server_close()


def test_bind_address_is_loopback(live_server):
    server, _ = live_server
    assert server.server_address[0] == "127.0.0.1"


def test_get_root_serves_html_from_memory(live_server):
    server, _ = live_server
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    assert resp.status == 200
    assert "harness-map" in body and "<!DOCTYPE html>" in body


def test_served_bytes_equal_on_disk_artifact(tmp_path):
    # N3: build_server collects for TODAY, so a pre-seeded past-dated sidecar is NEVER selected —
    # the surrogate MUST enter TODAY's fresh collect. Seed a REAL collector INPUT under --root with
    # a NON-UTF8 filename (byte 0xe9 -> lone surrogate) so today's walk ingests it. Assert (1)
    # served bytes == on-disk bytes AND (2) the surrogate-named input made it into BOTH.
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "fakeroot"
    root.mkdir()
    (root / "rules").mkdir()
    bad_name = b"\xe9bad.md"
    try:
        with open(os.path.join(os.fsencode(str(root / "rules")), bad_name), "wb") as f:
            f.write(b"rule body with a non-utf8-named path\n")
    except (OSError, ValueError):
        pytest.skip("filesystem rejects non-UTF8 filenames")
    server = srv.build_server(out_dir=out_dir, root=root, project_root=root,
                              host="127.0.0.1", port=0, no_friction=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        port = server.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/")
        served = conn.getresponse().read()
        on_disk = sorted(out_dir.glob("harness-map-*.html"))[-1].read_bytes()
        assert served == on_disk
        assert b"\\udce9bad.md" in served and b"\\udce9bad.md" in on_disk
    finally:
        server.shutdown()
        server.server_close()


def test_no_friction_renders_disabled_not_absent(live_server):
    # NEW BLOCKING 1: build_server(no_friction=True) must thread the flag so the friction footer
    # renders "disabled" (build_friction_overlay disabled=True), NOT "absent".
    server, _ = live_server
    port = server.server_address[1]
    body = _get_root_body(port)
    assert "disabled" in body, "no_friction did not render the disabled friction state"
    assert server.state.ctx.friction_disabled is True


@pytest.mark.parametrize("bad_host", ["0.0.0.0", "::", "::1", "localhost", "0", "10.0.0.5", "example.com"])
def test_non_loopback_host_rejected(bad_host):
    with pytest.raises(SystemExit):
        srv.main(["--out-dir", "/tmp", "--host", bad_host])


def test_only_127_0_0_1_allowed():
    assert srv._validate_host("127.0.0.1") == "127.0.0.1"


def test_default_host_binds_loopback(tmp_path):
    # Covers build_server's DEFAULT host= param specifically (the live_server fixture
    # passes host="127.0.0.1" explicitly, so it never exercises the default at all).
    out_dir = tmp_path / "defaulthost"
    out_dir.mkdir()
    _write_sidecar(out_dir, "2026-07-15", _minimal_doc())
    root = tmp_path / "fakeroot"
    root.mkdir()
    server = srv.build_server(out_dir=out_dir, root=root, project_root=root,
                              port=0, no_friction=True)  # host= omitted -> default
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.server_close()


def test_foreign_host_header_rejected(live_server):
    server, _ = live_server
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("GET", "/", skip_host=True)
    conn.putheader("Host", "evil.example.com")
    conn.endheaders()
    resp = conn.getresponse()
    body = resp.read().decode("utf-8", "replace")
    conn.close()
    assert resp.status == 400
    assert "harness-map" not in body and "<!DOCTYPE html>" not in body


def test_loopback_host_header_accepted(live_server):
    server, _ = live_server
    port = server.server_address[1]
    body = _get_root_body(port)
    assert "harness-map" in body and "<!DOCTYPE html>" in body


def test_antiframing_headers_present(live_server):
    server, _ = live_server
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.getheader("X-Frame-Options") == "DENY"
    assert "frame-ancestors 'none'" in resp.getheader("Content-Security-Policy", "")


def test_request_handler_has_idle_timeout():
    assert srv.RequestHandler.timeout is not None
    assert srv.RequestHandler.timeout <= 10


def test_main_reports_clean_error_on_bad_out_dir(tmp_path, capsys):
    # --root points at a path that does not exist, so collector.main() silently SKIPS its
    # --out write (see collector.py main()'s os.stat(root) OSError branch) -> the sidecar
    # is never freshly written -> _run_collector raises CollectorError from inside
    # build_server's startup _rebuild call. main() must catch this and report cleanly
    # (rv == 1, message on stderr) instead of letting the traceback escape.
    out_dir = tmp_path / "empty-out-dir"
    out_dir.mkdir()
    bad_root = tmp_path / "does-not-exist-root"
    rv = srv.main(["--out-dir", str(out_dir), "--root", str(bad_root),
                   "--project-root", str(bad_root), "--no-friction"])
    assert rv == 1
    captured = capsys.readouterr()
    assert captured.err.strip() != ""


# ============================================================= T4: SSE /events + watcher
# Shrunk poll/debounce/heartbeat keep the realtime tests fast; the DEFAULTS stay 2.0/1.0/<10.
_POLL = 0.2
_DEBOUNCE = 0.2
_HEARTBEAT = 1.0


def _start_watching_server(out_dir, root, project_root):
    server = srv.build_server(
        out_dir=out_dir, root=root, project_root=project_root,
        host="127.0.0.1", port=0, no_friction=True, watch=True,
        poll_seconds=_POLL, debounce_seconds=_DEBOUNCE, heartbeat=_HEARTBEAT)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def _teardown_watching_server(server):
    server.shutdown()
    server._watcher_stop.set()
    server._watcher_thread.join(timeout=5)
    server.server_close()


@pytest.fixture
def live_server_watching(tmp_path):
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    (root / "CLAUDE.md").write_text("# claude\n")
    server = _start_watching_server(out_dir, root, root)
    yield server, out_dir, root
    _teardown_watching_server(server)


@pytest.fixture
def live_server_watching_proj(tmp_path):
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    (root / "skills").mkdir()
    # projects/<slug>/memory so the collector actually counts project_root/CLAUDE.md
    slug_mem = root / "projects" / "proj-slug" / "memory"
    slug_mem.mkdir(parents=True)
    (slug_mem / "MEMORY.md").write_text("# mem\n")
    proj = tmp_path / "projroot"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# proj claude\n")
    server = _start_watching_server(out_dir, root, proj)
    yield server, out_dir, root, proj
    _teardown_watching_server(server)


def _wait_settle(server):
    """Sleep past one full poll + debounce + rebuild so a change either settles or is
    proven NOT to fire. Reads the server's configured timings so it tracks the fixture."""
    poll = getattr(server, "_poll_seconds", 2.0)
    debounce = getattr(server, "_debounce_seconds", 1.0)
    time.sleep((poll + debounce) * 2 + 0.8)


def _open_events(port):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/events")
    # getresponse() nulls conn.sock for a will-close HTTP/1.0 response, so capture the
    # socket first. timeout > heartbeat so each readline gets at least a heartbeat line
    # and never blocks past one heartbeat interval.
    sock = conn.sock
    sock.settimeout(_HEARTBEAT + 1.5)
    resp = conn.getresponse()
    return conn, resp


def _iter_refresh_lines(resp, deadline):
    """Shared SSE read loop: yield once per `event: refresh` line until `deadline`,
    stopping on a socket timeout or EOF (heartbeat comment lines are skipped). Both
    _await_refresh (first hit) and _drain_refresh_count (count) reuse this so the read
    loop + exception handling live in exactly one place."""
    while time.monotonic() < deadline:
        try:
            line = resp.fp.readline()
        except (socket.timeout, TimeoutError, OSError):
            break
        if not line:
            break
        if b"event: refresh" in line:
            yield line


def _await_refresh(resp, timeout):
    for _ in _iter_refresh_lines(resp, time.monotonic() + timeout):
        return True
    return False


def _drain_refresh_count(resp, quiet_for):
    """Read the stream for `quiet_for` seconds, counting `event: refresh` lines
    (heartbeat comment lines are ignored)."""
    return sum(1 for _ in _iter_refresh_lines(resp, time.monotonic() + quiet_for))


def test_sse_refresh_on_file_change(live_server_watching):
    server, out_dir, root = live_server_watching
    port = server.server_address[1]
    conn, resp = _open_events(port)
    try:
        (root / "rules" / "new.md").write_text("hi\n")
        assert _await_refresh(resp, timeout=8.0), "no refresh event arrived within 8s"
    finally:
        conn.close()


def test_debounce_burst_yields_single_refresh(live_server_watching):
    server, out_dir, root = live_server_watching
    port = server.server_address[1]
    conn, resp = _open_events(port)
    try:
        for i in range(6):
            (root / "rules" / f"b{i}.md").write_text("x\n")
            time.sleep(0.05)
        _wait_settle(server)
        assert _drain_refresh_count(resp, quiet_for=1.5) == 1
    finally:
        conn.close()


def test_slow_client_queue_bounded_to_one_pending(live_server_watching):
    server, out_dir, root = live_server_watching
    q = server.state.register_client()
    try:
        # Never drain q; drive 10 rebuilds -> each broadcasts. maxsize=1 + Full-coalesce
        # keeps at most one pending refresh, so a stalled client cannot grow unbounded.
        for _ in range(10):
            srv._rebuild(server.state, out_dir, root, root)
        assert q.qsize() <= 1
    finally:
        server.state.unregister_client(q)


def test_collector_failure_keeps_serving_last_good(live_server_watching):
    server, out_dir, root = live_server_watching
    port = server.server_address[1]
    before_body = _get_root_body(port)
    before_count = server.state.collect_count
    os.chmod(out_dir, 0o500)  # collector cannot write a fresh sidecar -> P30 CollectorError
    try:
        (root / "rules" / "trigger.md").write_text("x\n")
        _wait_settle(server)
        assert _get_root_body(port) == before_body
        assert server.state.collect_count == before_count
        assert server._watcher_thread.is_alive()
    finally:
        os.chmod(out_dir, 0o700)


def test_ondisk_write_failure_keeps_ctx_and_does_not_broadcast(live_server_watching):
    server, out_dir, root = live_server_watching
    port = server.server_address[1]
    today = datetime.date.today().strftime("%Y-%m-%d")
    blocker = out_dir / f"harness-map-{today}.html"
    before_body = _get_root_body(port)
    before_count = server.state.collect_count
    # collect + render SUCCEED, but the on-disk html write fails: a non-empty dir sitting
    # at the html path makes os.replace() in write_html_safely raise OSError.
    if blocker.exists():
        blocker.unlink()
    blocker.mkdir()
    (blocker / "keep").write_text("x")
    conn, resp = _open_events(port)
    try:
        (root / "rules" / "trigger5.md").write_text("x\n")
        _wait_settle(server)
        assert _get_root_body(port) == before_body
        assert server.state.collect_count == before_count
        assert _drain_refresh_count(resp, 2.0) == 0
        assert server._watcher_thread.is_alive()
    finally:
        conn.close()


@pytest.mark.parametrize("mutate", [
    pytest.param(lambda root, proj: (proj / "CLAUDE.md").write_text("# proj changed\n"),
                 id="project_claude_md"),
    pytest.param(lambda root, proj: (root / "skills" / "demo" / "tests").mkdir(),
                 id="skill_descendant_dir_existence"),
    pytest.param(lambda root, proj: (root / "skills" / "demo" / "cases_eval.txt").write_text("e\n"),
                 id="skill_eval_arbitrary_ext"),
    pytest.param(lambda root, proj: (root / "skills" / "brand_new_skill").mkdir(),
                 id="skills_container_membership"),
])
def test_each_collector_input_class_triggers_recollect(live_server_watching_proj, mutate):
    server, out_dir, root, proj = live_server_watching_proj
    (root / "skills" / "demo").mkdir(parents=True)  # pre-create per spec
    _wait_settle(server)  # settle the demo-creation rebuild first
    before = server.state.collect_count
    mutate(root, proj)
    _wait_settle(server)
    assert server.state.collect_count > before


def test_symlinked_input_target_change_triggers_recollect(live_server_watching_proj, tmp_path):
    server, out_dir, root, proj = live_server_watching_proj
    external = tmp_path / "external_skill"  # lives OUTSIDE --root
    external.mkdir()
    (external / "SKILL.md").write_text("# external v1\n")
    (root / "skills" / "linked").symlink_to(external)
    _wait_settle(server)  # registers the symlinked skill in the snapshot
    before = server.state.collect_count
    (external / "SKILL.md").write_text("# external v2 changed and longer\n")
    _wait_settle(server)
    assert server.state.collect_count > before


def test_watched_set_equals_collector_iter_input_paths(live_server_watching_proj):
    server, out_dir, root, proj = live_server_watching_proj
    snap = srv._watched_snapshot(root, proj)
    from_iter = srv.collector.iter_input_paths(root, proj)
    assert set(map(str, snap.keys())) == set(map(str, from_iter))


def test_watcher_survives_uncaught_exception(live_server_watching):
    # HIGH: the watcher must survive an exception that is NOT one of the enumerated
    # (CollectorError / RenderError / OSError) types, or the daemon thread dies and the
    # dashboard silently freezes on last-good forever. Real injection (no mocks): drop a
    # TODAY-dated synthesis sidecar whose JSON is VALID but whose shape makes render raise
    # deep inside render_from_out_dir — build_dragcandidate_model sorts drag_candidates by
    # r["n"], so mixed int/str "n" values raise a TypeError (confirmed: not RenderError,
    # not OSError). The collector regenerates the MAP sidecar on each rebuild but never the
    # synthesis file, so this poison persists across the triggered rebuild.
    server, out_dir, root = live_server_watching
    port = server.server_address[1]
    before_body = _get_root_body(port)
    before_count = server.state.collect_count
    today = datetime.date.today().strftime("%Y-%m-%d")
    synth = out_dir / f"harness-synthesis-{today}.json"
    # schema_version present so load_sidecar accepts the file (else it is skipped as
    # invalid and never reaches the model builders); the mixed-type "n" is what raises.
    synth.write_text('{"schema_version": 1, "drag_candidates": [{"n": 1}, {"n": "not-a-number"}]}')
    # Trigger a rebuild by touching a WATCHED input under --root (out_dir is not watched).
    (root / "rules" / "trigger_uncaught.md").write_text("x\n")
    _wait_settle(server)
    # Without the outer backstop the TypeError kills the thread; with it the thread survives,
    # last-good is still served, and no successful recollect happened (snapshot not advanced).
    assert server._watcher_thread.is_alive(), "watcher thread died on an unenumerated exception"
    assert _get_root_body(port) == before_body
    assert server.state.collect_count == before_count


# ================================================================= T5: B2 incremental tail
def _friction_section(html_text):
    """Slice the self-contained friction-panel block (`<aside id="friction-panel">` ..
    `</aside>`) out of a served document -- a pure function of joined/footer/codex_aggregate/
    friction_total_value only (never doc/headline fields), so it is the stable C18-parity
    comparison unit between a cheap-path render and a full-recollect render."""
    start = html_text.index('<aside class="card" id="friction-panel">')
    end = html_text.index("</aside>", start) + len("</aside>")
    return html_text[start:end]


def _start_streams_server(tmp_path, extra_root_files=0):
    # Friction ENABLED (not no_friction) with a real temp JSONL "decisions" stream, so a
    # pure append can be observed by the B2 cheap path. `root/rules/a.md` gives the stream's
    # "component": "rules/a.md" records a real map node to join onto. `extra_root_files`
    # (real, on-disk collector inputs present from the START, so they never register as a
    # mid-test collector-input CHANGE) widens collector.main()'s real wall-clock walk time
    # for tests that need a deterministic window between two rebuild attempts.
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    for i in range(extra_root_files):
        (root / "rules" / f"slow{i}.md").write_text(f"# slow rule {i}\n")
    stream_path = tmp_path / "harness-decisions.jsonl"
    stream_path.write_text("")
    streams = {"decisions": stream_path, "metrics": None, "codex": None, "interventions": None}
    server = srv.build_server(
        out_dir=out_dir, root=root, project_root=root, host="127.0.0.1", port=0,
        no_friction=False, streams=streams, watch=True,
        poll_seconds=_POLL, debounce_seconds=_DEBOUNCE, heartbeat=_HEARTBEAT)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, out_dir, root, stream_path


@pytest.fixture
def live_server_with_streams(tmp_path):
    server, out_dir, root, stream_path = _start_streams_server(tmp_path)
    yield server, out_dir, root, stream_path
    _teardown_watching_server(server)


@pytest.fixture
def live_server_with_streams_slow_root(tmp_path):
    server, out_dir, root, stream_path = _start_streams_server(tmp_path, extra_root_files=300)
    yield server, out_dir, root, stream_path
    _teardown_watching_server(server)


def _append_decision_record(stream_path, component="rules/a.md"):
    today = datetime.date.today().strftime("%Y-%m-%d")
    with open(stream_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"date": today, "component": component}) + "\n")


def test_jsonl_append_updates_friction_via_cheap_path(live_server_with_streams):
    server, out_dir, root, stream_path = live_server_with_streams
    port = server.server_address[1]
    before_count = server.state.collect_count
    _append_decision_record(stream_path)
    _wait_settle(server)
    assert server.state.collect_count == before_count, \
        "a pure JSONL append must NOT run a full re-collect (B2/T5 counter contract)"
    body = _get_root_body(port)
    assert "Friction events: 1" in body


def test_truncated_jsonl_resets_offset_and_full_recollects(live_server_with_streams):
    server, out_dir, root, stream_path = live_server_with_streams
    port = server.server_address[1]
    stream_path.write_text("x\n" * 50)
    _wait_settle(server)
    before_count = server.state.collect_count
    stream_path.write_text("y\n")
    _wait_settle(server)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 200
    assert server.state.stream_offsets[str(stream_path)] == stream_path.stat().st_size
    assert server.state.collect_count > before_count, \
        "a shrunk/rotated stream must force a FULL re-collect, never a cheap re-render"


def test_truncation_retries_full_recollect_after_transient_rebuild_failure(live_server_with_streams):
    # FIX 4 (Codex challenge): a truncate-to-EMPTY whose forced full re-collect FAILS transiently
    # must still retry on the next sweep. The old code zeroed state.stream_offsets[key] BEFORE the
    # rebuild, so after a truncate-to-0 + failed rebuild the next sweep compared size 0 == offset 0
    # -> "none" -> the required re-collect was lost forever (stale state served permanently). Real
    # (no-mock) fault injection: a non-empty dir at the html path makes write_html_safely's
    # os.replace() raise a real OSError, failing the first forced rebuild; removing it lets the
    # retry succeed. collect_count incrementing ONLY after the unblock proves the retry survived.
    server, out_dir, root, stream_path = live_server_with_streams
    today = datetime.date.today().strftime("%Y-%m-%d")
    html_path = out_dir / f"harness-map-{today}.html"

    # Grow + settle so the saved offset is > 0 (the precondition for the truncate-to-0 == 0 bug).
    _append_decision_record(stream_path)
    _append_decision_record(stream_path)
    _wait_settle(server)
    assert server.state.stream_offsets[str(stream_path)] > 0
    before_count = server.state.collect_count

    # Block the html write, then truncate the stream to empty: the forced full re-collect runs
    # the collector (succeeds) but fails at the on-disk html write (os.replace onto a non-empty
    # dir) -> _try_full_rebuild returns False -> the truncation stays pending.
    if html_path.exists():
        html_path.unlink()
    html_path.mkdir()
    (html_path / "keep").write_text("x")
    try:
        stream_path.write_text("")  # truncate to empty (size 0)
        _wait_settle(server)
        assert server.state.collect_count == before_count, \
            "the html-write block must have failed the forced re-collect (setup precondition)"
        assert server._watcher_thread.is_alive()
    finally:
        if html_path.is_dir():
            shutil.rmtree(html_path)

    # Unblocked: the pending truncation must force the full path again and now SUCCEED.
    _wait_settle(server)
    assert server.state.collect_count > before_count, \
        "a transient rebuild failure after truncation must still retry -> the re-collect is NOT lost"
    assert server.state.stream_offsets[str(stream_path)] == 0, \
        "the successful re-collect must re-seed the offset from its PRE-read size (empty stream -> 0)"


def test_get_root_swallows_client_disconnect_without_raising(live_server):
    # FIX 8 (Codex challenge): a client that RSTs/closes during the GET `/` body write raises
    # BrokenPipeError from the socket write; unlike /events, the old handler did not catch it, so
    # socketserver printed a traceback. Real (no-mock) severed transport: a socketpair whose read
    # end is closed makes the unbuffered body write raise a genuine BrokenPipeError. do_GET must
    # mirror _serve_events's _CLIENT_GONE handling and return cleanly instead of propagating.
    server, _out_dir = live_server
    assert BrokenPipeError in srv.RequestHandler._CLIENT_GONE
    assert ConnectionResetError in srv.RequestHandler._CLIENT_GONE

    sock_server, sock_client = socket.socketpair()
    handler = srv.RequestHandler.__new__(srv.RequestHandler)
    handler.server = server
    handler.request_version = "HTTP/1.1"
    handler.command = "GET"
    handler.requestline = "GET / HTTP/1.1"
    handler.path = "/"
    handler.close_connection = True
    headers = http.client.HTTPMessage()
    headers["Host"] = "127.0.0.1"
    handler.headers = headers
    handler.wfile = sock_server.makefile("wb", buffering=0)  # unbuffered -> write raises in-handler
    try:
        sock_client.close()  # sever the client end BEFORE the body write
        handler.do_GET()     # must NOT raise despite the broken transport
    finally:
        sock_server.close()


def test_date_rollover_forces_full_recollect(live_server_with_streams):
    # C18 PARITY-OR-DEGRADE: the cheap path re-renders friction using the CACHED ctx.date;
    # across a local midnight, `build_friction_overlay`'s `d > current_date` filter would
    # EXCLUDE a new-day-dated record under a stale cached date but INCLUDE it under a full
    # recollect's `today` -- a real divergence, not hypothetical. Force that mismatch with a
    # REAL dataclasses.replace on the REAL served ctx (no clock mock, no mock of any kind)
    # and confirm the watcher takes the FULL rebuild path (collect_count increments) rather
    # than the cheap path (which would leave collect_count unchanged).
    server, out_dir, root, stream_path = live_server_with_streams
    with server.state.lock:
        server.state.ctx = dataclasses.replace(server.state.ctx, date="2000-01-01")
    before_count = server.state.collect_count
    _append_decision_record(stream_path)
    _wait_settle(server)
    assert server.state.collect_count > before_count, \
        "a ctx.date/today mismatch must force a full recollect, not the cheap path " \
        "(C18 PARITY-OR-DEGRADE)"


def test_cheap_path_failure_degrades_to_full_recollect(live_server_with_streams_slow_root):
    # Real (no-mock) fault injection: swap the html artifact for a non-empty directory so
    # write_html_safely's os.replace() raises a real OSError -- but ONLY block the cheap
    # path's attempt. A watchdog thread self-heals the blocker the moment it observes the
    # SIDECAR's identity change, a real, state-based signal that the D5 full-recollect
    # fallback's _run_collector has just run (never a sleep-based timing guess), so the
    # fallback's OWN html write lands on a clear path. The fixture's 300 extra ROOT files
    # (present from server startup, so they are baseline state, never a mid-test
    # collector-input change) widen collector.main()'s real wall-clock walk time enough for
    # the watchdog to reliably win the removal race before the fallback's own write.
    # WALL-CLOCK MARGIN ASSUMPTION: this margin is a fault-injection TEST-ONLY dependency --
    # the product's own degrade-to-full-rebuild path is race-free regardless of timing; only
    # this test's watchdog-vs-fallback-write race could flake if a future CI-speed change
    # erodes the 300-file walk's margin over the rmtree.
    server, out_dir, root, stream_path = live_server_with_streams_slow_root
    port = server.server_address[1]
    today = datetime.date.today().strftime("%Y-%m-%d")
    html_path = out_dir / f"harness-map-{today}.html"
    sidecar_path = out_dir / f"harness-map-{today}.json"
    pre_sidecar_stat = sidecar_path.stat()
    pre_identity = (pre_sidecar_stat.st_ino, pre_sidecar_stat.st_mtime_ns)

    if html_path.exists():
        html_path.unlink()
    html_path.mkdir()
    (html_path / "keep").write_text("x")

    unblocked = threading.Event()

    def _watchdog():
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                st = sidecar_path.stat()
                identity = (st.st_ino, st.st_mtime_ns)
            except OSError:
                identity = None
            if identity is not None and identity != pre_identity:
                if html_path.is_dir():
                    shutil.rmtree(html_path)
                unblocked.set()
                return
            time.sleep(0.001)

    watchdog = threading.Thread(target=_watchdog, daemon=True)
    watchdog.start()
    try:
        before_count = server.state.collect_count
        _append_decision_record(stream_path)
        _wait_settle(server)
        assert unblocked.wait(timeout=1.0), \
            "watchdog never observed the full-recollect fallback's collector run"
        assert server.state.collect_count > before_count, \
            "a cheap-path write failure must degrade to a full re-collect (D5)"
        body = _get_root_body(port)
        assert "Friction events: 1" in body
    finally:
        if html_path.is_dir():
            shutil.rmtree(html_path)


def test_cheap_path_friction_byte_equals_full_recollect(live_server_with_streams):
    # C18 PARITY: compares the served friction section produced by the cheap incremental
    # path against the friction section produced by a subsequent full `_rebuild` over the
    # SAME (now-settled) stream file -- exact served-HTML parity is practical here because
    # the friction-panel block is a pure function of joined/footer/codex_aggregate/
    # friction_total_value only (never doc/headline fields the collector-input trigger
    # below also changes), see `_friction_section`'s docstring.
    server, out_dir, root, stream_path = live_server_with_streams
    port = server.server_address[1]
    _append_decision_record(stream_path)
    _wait_settle(server)
    cheap_section = _friction_section(_get_root_body(port))

    before_count = server.state.collect_count
    (root / "rules" / "force_full.md").write_text("# force full\n")
    _wait_settle(server)
    assert server.state.collect_count > before_count, \
        "a collector-input change must have triggered the FULL recollect path for this comparison"

    full_section = _friction_section(_get_root_body(port))
    assert cheap_section == full_section


# ================================================= Codex P2 fixes (append/existence/reconnect/flush)
@pytest.fixture
def streams_server_no_watch(tmp_path):
    """A friction-enabled streams server with NO watcher thread and NO serving thread, so
    the test thread is the SOLE mutator of state — offset-classification invariants can be
    asserted deterministically (no watcher racing on state.stream_offsets)."""
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    stream_path = tmp_path / "harness-decisions.jsonl"
    stream_path.write_text("")
    streams = {"decisions": stream_path, "metrics": None, "codex": None, "interventions": None}
    server = srv.build_server(
        out_dir=out_dir, root=root, project_root=root, host="127.0.0.1", port=0,
        no_friction=False, streams=streams, watch=False)
    yield server, out_dir, root, stream_path
    server.server_close()


def test_append_during_render_not_lost(streams_server_no_watch):
    # FIX 1 (Codex P2): offsets must be seeded from the size observed BEFORE the render
    # consumes the streams (a lower bound of what was actually consumed), NOT the post-render
    # size — otherwise an append that lands after the render read but before the seed is
    # recorded as already-consumed and its record is invisible until another append.
    # Racing the exact sub-millisecond window deterministically requires a mock (forbidden),
    # so the observable INVARIANT is asserted: after a rebuild the offset equals the stream
    # size at rebuild START, and any later append is classified "grown" (never dropped).
    server, out_dir, root, stream_path = streams_server_no_watch
    key = str(stream_path)
    _append_decision_record(stream_path)
    size_at_render = stream_path.stat().st_size
    srv._rebuild(server.state, out_dir, root, root)
    assert server.state.stream_offsets[key] == size_at_render, \
        "offset must reflect the PRE-read size captured at rebuild start (FIX 1)"
    # An append AFTER the render consumed the stream must be seen as growth next sweep —
    # the safe direction: it triggers a re-render, it is never silently lost.
    _append_decision_record(stream_path)
    classification, changed = srv._classify_stream_sweep(server.state)
    assert classification == "grown"
    assert key in changed


def test_empty_stream_creation_detected(streams_server_no_watch):
    # FIX 2 (Codex P2): classification must track EXISTENCE, not just size. A previously
    # ABSENT stream (offset None) that is CREATED — even empty (size 0, which collides with
    # the old default-0 offset) — must register as a change, and a subsequent append renders.
    server, out_dir, root, stream_path = streams_server_no_watch
    key = str(stream_path)
    stream_path.unlink()
    srv._rebuild(server.state, out_dir, root, root)  # re-seed with the stream ABSENT
    assert server.state.stream_offsets.get(key) is None, \
        "an absent stream must seed to None (existence tracked), not 0 (FIX 2)"
    stream_path.write_text("")  # create it empty: absent -> present is a tracked transition
    c1, changed1 = srv._classify_stream_sweep(server.state)
    assert c1 == "grown" and key in changed1, "empty-stream creation must be detected (FIX 2)"
    srv._rebuild_friction_only(server.state, out_dir)  # re-seed the offset to 0
    assert server.state.stream_offsets.get(key) == 0
    _append_decision_record(stream_path)  # a subsequent append is seen as growth
    c2, changed2 = srv._classify_stream_sweep(server.state)
    assert c2 == "grown" and key in changed2


def test_stream_deletion_forces_rerender(live_server_with_streams):
    # FIX 2 (Codex P2): a previously-loaded stream that is DELETED (size -> None) must force
    # a FULL re-collect so the collector/friction reflect the removed file — otherwise the
    # dashboard shows the deleted file's records forever.
    server, out_dir, root, stream_path = live_server_with_streams
    port = server.server_address[1]
    _append_decision_record(stream_path)
    _wait_settle(server)
    assert "Friction events: 1" in _get_root_body(port)
    before_count = server.state.collect_count
    stream_path.unlink()  # delete a loaded stream
    _wait_settle(server)
    assert server.state.collect_count > before_count, \
        "a deleted stream must force a FULL re-collect (FIX 2)"
    assert server._watcher_thread.is_alive()
    assert "Friction events: 1" not in _get_root_body(port), \
        "the deleted stream's records must no longer be served"


def _read_sync_generation(resp, timeout=6.0):
    """Read the SSE stream until the connect-time `event: sync` + its `data:` generation
    line, returning the int generation (or None on timeout/EOF). The server sends this as
    the FIRST event on every /events (re)connect so a reconnecting client can compare the
    server's current generation to the one its page was built from."""
    deadline = time.monotonic() + timeout
    saw_sync = False
    while time.monotonic() < deadline:
        try:
            line = resp.fp.readline()
        except (socket.timeout, TimeoutError, OSError):
            break
        if not line:
            break
        if b"event: sync" in line:
            saw_sync = True
            continue
        if saw_sync and line.startswith(b"data:"):
            return int(line.split(b":", 1)[1].strip())
    return None


def test_initial_connect_does_not_loop_reload(live_server_with_streams):
    # FIX 4 (Codex P2): on the INITIAL connect of a just-loaded page (page-gen == server-gen)
    # the server sends the current generation as an informational `sync` (NOT an unconditional
    # `refresh`), and the client reloads ONLY when serverGen > pageGen — so a fresh page never
    # loops. Server-side we assert the first event is a `sync` carrying the CURRENT generation.
    server, out_dir, root, stream_path = live_server_with_streams
    port = server.server_address[1]
    conn, resp = _open_events(port)
    try:
        with server.state.lock:
            current_gen = server.state.generation
        gen = _read_sync_generation(resp)
        assert gen == current_gen, "connect must report the CURRENT generation as sync"
        # equal generations must NOT reload -> no spurious refresh right after connect
        assert _drain_refresh_count(resp, quiet_for=1.0) == 0
    finally:
        conn.close()


def test_reconnect_after_missed_refresh_resyncs(live_server_with_streams):
    # FIX 4 (Codex P2): if a rebuild broadcasts while the SSE connection is down, the refresh
    # is lost; on reconnect the server must report its (now higher) generation so the client
    # catches up. A new /events connection after an intervening rebuild must report gen > the
    # generation the earlier connection saw.
    server, out_dir, root, stream_path = live_server_with_streams
    port = server.server_address[1]
    conn1, resp1 = _open_events(port)
    gen1 = _read_sync_generation(resp1)
    assert gen1 is not None
    conn1.close()  # "disconnect" while a rebuild happens
    _append_decision_record(stream_path)
    _wait_settle(server)  # a cheap re-render advances the generation
    conn2, resp2 = _open_events(port)
    try:
        gen2 = _read_sync_generation(resp2)
        assert gen2 is not None and gen2 > gen1, \
            "a reconnecting client must be told the advanced generation (FIX 4)"
    finally:
        conn2.close()


def test_reconnect_race_client_registered_before_gen(live_server):
    # FIX 3 (Codex r2): `_serve_events` must register the SSE client queue BEFORE reading the
    # generation. If it read the generation first and a rebuild published (bumped generation +
    # broadcast) in the gap before registration, the broadcast would miss the unregistered
    # queue AND the stale gen-read would report the OLD generation on `sync` -> a page at that
    # old generation gets neither the refresh nor an ahead-of-page sync and stays stale.
    #
    # Deterministic, mock-free proof via real lock semantics: the generation read happens under
    # `state.lock`, but `register_client()` uses the SEPARATE clients_lock. Hold `state.lock`
    # from the test thread so the handler BLOCKS at the generation read; register_client() does
    # not need state.lock, so with the fix it has already run (state.clients non-empty) while
    # the gen read is blocked. With the OLD ordering the handler would block at the gen read
    # BEFORE registering -> state.clients stays empty -> this assertion fails.
    server, _ = live_server
    port = server.server_address[1]
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        with server.state.lock:
            # send the request but do NOT getresponse() (that would block on the held lock):
            conn.request("GET", "/events")
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline and len(server.state.clients) == 0:
                time.sleep(0.01)
            assert len(server.state.clients) >= 1, \
                "the SSE client queue must be registered BEFORE the generation read " \
                "(which is blocked here on the held state.lock) — FIX 3 ordering"
        # lock released: the gen read proceeds, headers + connect-time sync are sent
        resp = conn.getresponse()
        gen = _read_sync_generation(resp)
        assert gen is not None, "the connect-time sync must still be delivered after the gen read"
    finally:
        conn.close()


def test_clean_startup_no_spurious_recollect(live_server_watching):
    # FIX 2 (Codex r2) SAFETY: seeding the watch snapshot BEFORE the initial _rebuild must NOT
    # make the first watcher sweep spuriously re-collect on a CLEAN startup (nothing changed).
    # The rebuild writes only to out_dir (disjoint from the watched surface), so the pre-rebuild
    # baseline equals what the first sweep re-computes -> collect_count stays stable.
    server, out_dir, root = live_server_watching
    before = server.state.collect_count
    _wait_settle(server)   # let several poll+debounce cycles run with NO change
    _wait_settle(server)
    assert server.state.collect_count == before, \
        "a clean startup must not spuriously re-collect on the first sweeps (FIX 2 safety)"
    # The seeded baseline must also EQUAL what the first sweep computes when nothing changed —
    # i.e. seeding before the rebuild does not diverge from the live filesystem (the collector
    # enforces out_dir OUTSIDE root, so the rebuild never mutates a watched path). If these
    # differed, the first sweep would spuriously re-render on a clean startup.
    assert server.state.watch_snapshot == srv._watched_snapshot(root, root), \
        "the pre-rebuild watch baseline must match the current watched surface on a clean startup"


def test_startup_url_is_flushed(tmp_path):
    # FIX 5 (Codex P2): main() prints the Serving URL then blocks in serve_forever(). When an
    # agent backgrounds the server with stdout piped (block-buffered, not a TTY), an unflushed
    # line never reaches the pipe before the blocking loop — breaking the T7 "background it and
    # read stdout for the OS-assigned port" workflow. The URL line must be flushed.
    import subprocess
    import sys
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    proc = subprocess.Popen(
        [sys.executable, str(SERVE), "--out-dir", str(out_dir), "--root", str(root),
         "--project-root", str(root), "--no-friction"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15.0
        line = ""
        while time.monotonic() < deadline:
            line = proc.stdout.readline()  # blocks until a flushed line or process exit
            if line:
                break
        assert line.startswith("Serving http://127.0.0.1:"), \
            f"startup URL not readable from piped stdout while server runs; got {line!r}"
        assert proc.poll() is None, "server exited instead of blocking in serve_forever()"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# =========================================================== Codex r3: synthesis sidecar
def _coverage_covered_count(html_text):
    """Count of 'covered'-verdict markers in the served page. A synthesis change that adds
    covered CIVC cells raises this count, so it is a stable signal that the served Coverage
    Matrix reflects the CURRENT synthesis sidecar (not a stale cached render)."""
    return html_text.count("verdict-covered")


def _write_synthesis(out_dir, date, covered_cells):
    """Write a valid today-dated synthesis sidecar whose CIVC grid marks `covered_cells`
    (list of (verb, surface)) as 'covered'; everything else defaults to empty."""
    synth = out_dir / f"harness-synthesis-{date}.json"
    civc = [{"verb": v, "surface": s, "verdict": "covered"} for (v, s) in covered_cells]
    synth.write_text(json.dumps({"schema_version": 1, "civc": civc, "drag_candidates": []}))
    return synth


def test_synthesis_sidecar_change_triggers_rerender(tmp_path):
    # Codex r3 FIX 1 (P1): render_from_out_dir reads harness-synthesis-<date>.json from
    # out_dir to build the Coverage Matrix + drag models, but that sidecar lives OUTSIDE the
    # collector-input surface the watcher tracks. A (re)write of the synthesis while the server
    # runs must still be observed and trigger a FULL rebuild (the synthesis feeds MODELS, so the
    # cheap friction-only path cannot pick it up), so the served Coverage Matrix stays live.
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    (root / "CLAUDE.md").write_text("# claude\n")
    today = datetime.date.today().strftime("%Y-%m-%d")
    _write_synthesis(out_dir, today, [("Afford", "context")])  # valid synthesis BEFORE startup
    server = _start_watching_server(out_dir, root, root)
    try:
        port = server.server_address[1]
        _wait_settle(server)  # settle the startup rebuild
        before_count = server.state.collect_count
        before_cov = _coverage_covered_count(_get_root_body(port))
        assert before_cov >= 1, "startup synthesis coverage not reflected in the served page"

        # a NON-synthesis sweep with no change must NOT spuriously rebuild
        _wait_settle(server)
        assert server.state.collect_count == before_count, \
            "a no-change sweep must not spuriously rebuild the synthesis-tracked surface"

        # rewrite the synthesis with MORE covered cells -> a full rebuild + a live page update
        _write_synthesis(out_dir, today,
                         [("Afford", "context"), ("Inform", "tools"), ("Constrain", "memory")])
        _wait_settle(server)
        assert server.state.collect_count > before_count, \
            "a synthesis-sidecar change must trigger a FULL rebuild (collect_count++)"
        after_cov = _coverage_covered_count(_get_root_body(port))
        assert after_cov > before_cov, \
            "the served Coverage Matrix must reflect the rewritten synthesis sidecar"
    finally:
        _teardown_watching_server(server)


def test_startup_malformed_synthesis_clean_fatal(tmp_path, capsys):
    # Codex r3 FIX 2 (P2): a today-dated synthesis sidecar that is valid JSON with the right
    # schema_version but a MALFORMED nested shape (mixed int/str drag_candidates[].n -> a
    # TypeError deep in render_from_out_dir, the exact class the watcher backstop test uses)
    # raised during build_server's STARTUP _rebuild must be contained: main() exits non-zero
    # with the clean "fatal: could not start server" message, NOT a bare traceback.
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    (root / "CLAUDE.md").write_text("# claude\n")
    today = datetime.date.today().strftime("%Y-%m-%d")
    synth = out_dir / f"harness-synthesis-{today}.json"
    synth.write_text('{"schema_version": 1, "drag_candidates": [{"n": 1}, {"n": "x"}]}')
    rv = srv.main(["--out-dir", str(out_dir), "--root", str(root),
                   "--project-root", str(root), "--no-friction"])
    assert rv == 1, "a malformed startup synthesis must yield a clean non-zero exit"
    captured = capsys.readouterr()
    assert "fatal: could not start server" in captured.err, \
        f"expected the clean fatal message on stderr, got {captured.err!r}"
