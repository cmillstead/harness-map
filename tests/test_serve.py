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
