import datetime
import http.client
import importlib.util
import os
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


def _await_refresh(resp, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = resp.fp.readline()
        except (socket.timeout, TimeoutError, OSError):
            break
        if not line:
            break
        if b"event: refresh" in line:
            return True
    return False


def _drain_refresh_count(resp, quiet_for):
    """Read the stream for `quiet_for` seconds, counting `event: refresh` lines
    (heartbeat comment lines are ignored)."""
    deadline = time.monotonic() + quiet_for
    count = 0
    while time.monotonic() < deadline:
        try:
            line = resp.fp.readline()
        except (socket.timeout, TimeoutError, OSError):
            break
        if not line:
            break
        if b"event: refresh" in line:
            count += 1
    return count


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
