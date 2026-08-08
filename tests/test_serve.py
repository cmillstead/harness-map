import contextlib
import dataclasses
import datetime
import http.client
import importlib.util
import json
import os
import re
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest

from test_collector import _build_two_tier_maximal_fixture, _SECRET_SENTINELS
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


def _start_watching_server(out_dir, root, project_root, compose=False):
    server = srv.build_server(
        out_dir=out_dir, root=root, project_root=project_root,
        host="127.0.0.1", port=0, no_friction=True, watch=True,
        poll_seconds=_POLL, debounce_seconds=_DEBOUNCE, heartbeat=_HEARTBEAT, compose=compose)
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


@pytest.fixture
def live_server_watching_compose(tmp_path):
    # T8: a two-tier fixture -- operator root + a project-containment-root carrying its own
    # `.claude/{rules,commands}`. HOME is sandboxed (restored in the finally below) so
    # collect_composed_mcp reads a controlled ~/.claude.json, never the real dev machine's --
    # same real-env-var pattern test_collector.py uses via subprocess `env=`, adapted for an
    # in-process call (serve.py never shells out to collector.py).
    home = tmp_path / "home"
    home.mkdir()
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    (root / "CLAUDE.md").write_text("# claude\n")
    proj = tmp_path / "projroot"
    (proj / ".claude" / "rules").mkdir(parents=True)
    (proj / ".claude" / "commands").mkdir(parents=True)
    (proj / "CLAUDE.md").write_text("# proj claude\n" + "word " * 20)
    server = _start_watching_server(out_dir, root, proj, compose=True)
    try:
        yield server, out_dir, root, proj
    finally:
        _teardown_watching_server(server)
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


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


def test_path_value_detects_metadata_identical_symlink_retarget(tmp_path):
    # TRK-022 finding 6 (measured): a symlink retargeted to a DIFFERENT file carrying the SAME
    # size and the SAME mtime_ns produced a byte-identical _path_value under the mtime-only
    # element 4 -- the content flipped A->B while the watcher's value did not, so no re-render
    # fired and the dashboard served stale bytes. The followed target must be part of the value.
    alpha = tmp_path / "alpha.md"
    beta = tmp_path / "beta.md"
    alpha.write_text("AAAAAAAAAA")   # 10 bytes
    beta.write_text("BBBBBBBBBB")    # 10 bytes: identical size, different content
    fixed_ns = 1_700_000_000_000_000_000
    os.utime(alpha, ns=(fixed_ns, fixed_ns))
    os.utime(beta, ns=(fixed_ns, fixed_ns))
    link = tmp_path / "watched.md"
    link.symlink_to(alpha)
    before = srv._path_value(str(link))
    link.unlink()
    link.symlink_to(beta)
    after = srv._path_value(str(link))
    assert link.read_text() == "BBBBBBBBBB", "fixture precondition: the content really did change"
    assert alpha.stat().st_size == beta.stat().st_size, "fixture precondition: sizes really are equal"
    assert alpha.stat().st_mtime_ns == beta.stat().st_mtime_ns, \
        "fixture precondition: mtimes really are equal"
    assert before != after, \
        "a metadata-identical symlink retarget must change the watched value (TRK-022 finding 6)"


def test_path_value_unchanged_symlink_compares_equal(tmp_path):
    # Positive control for TRK-022 finding 6: recording the followed target must not make a
    # STABLE symlink look changed on every sweep -- that would re-render on every poll.
    target = tmp_path / "target.md"
    target.write_text("stable\n")
    link = tmp_path / "watched.md"
    link.symlink_to(target)
    assert srv._path_value(str(link)) == srv._path_value(str(link))


def test_path_value_still_detects_same_size_symlink_target_rewrite(tmp_path):
    # Positive control: element 4 keeps the target mtime_ns ALONGSIDE the resolved path, so a
    # same-size content rewrite through an UNCHANGED link is still seen. The pre-existing mtime
    # signal must not be traded away for the new retarget signal.
    target = tmp_path / "target.md"
    target.write_text("AAAA")
    os.utime(target, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    link = tmp_path / "watched.md"
    link.symlink_to(target)
    before = srv._path_value(str(link))
    target.write_text("BBBB")   # same size, different content
    os.utime(target, ns=(1_700_000_001_000_000_000, 1_700_000_001_000_000_000))
    assert srv._path_value(str(link)) != before


def test_path_value_dangling_symlink_returns_missing_tuple(tmp_path):
    # A symlink whose target does not exist returns the MISSING tuple, and element 4 is never
    # reached at all. os.path.realpath is non-strict so it returns the unresolved path rather
    # than raising, and the target stat's OSError would degrade the mtime half to None -- but
    # the OUTER os.stat(path) at serve.py:373 follows the link and raises for the same reason,
    # so the function returns at serve.py:375 before any identity is assembled. The existence
    # bit alone carries the change, which is the correct and safe direction.
    #
    # DISCLOSED CONSEQUENCE: retargeting one dangling link to a DIFFERENT dangling target is
    # therefore invisible -- both states are (False, None, None, None). Finding 6 is about
    # retargets between targets that EXIST; this residual is not in its scope and is not
    # narrowed by it.
    link = tmp_path / "dangling.md"
    link.symlink_to(tmp_path / "never_created.md")
    assert srv._path_value(str(link)) == (False, None, None, None)


def test_path_value_escaping_symlink_shape_unchanged(tmp_path):
    # The escaping project-tier branch must KEEP its (True, "symlink-escaping", readlink, None)
    # shape: it never follows, so element 4 -- "what following it landed on" -- stays None. This
    # pins that the finding-6 fix did not leak target resolution into the branch that is
    # forbidden by the T8/T3 containment policy from resolving the target at all.
    proj = tmp_path / "proj"
    (proj / ".claude" / "rules").mkdir(parents=True)
    external = tmp_path / "outside.md"
    external.write_text("external\n")
    link = proj / ".claude" / "rules" / "escaping.md"
    link.symlink_to(external)
    value = srv._path_value(str(link), "project", str(proj))
    assert value[1] == "symlink-escaping"
    assert value[2] == str(external)
    assert value[3] is None


def test_path_value_plain_file_has_no_target_identity(tmp_path):
    # A non-symlink has nothing to follow, so element 4 stays None -- unchanged from before.
    plain = tmp_path / "plain.md"
    plain.write_text("x\n")
    assert srv._path_value(str(plain))[3] is None


def test_metadata_identical_skill_symlink_retarget_triggers_recollect(
        live_server_watching_proj, tmp_path):
    # End-to-end for TRK-022 finding 6. Mirrors test_symlinked_input_target_change_triggers_
    # recollect (tests/test_serve.py:429) but flips the TARGET instead of the target's bytes.
    # VACUITY TRAP: every mtime on both candidate trees is pinned to the same value, so the ONLY
    # thing that differs pre-fix vs post-fix is the resolved target path. Without the pinning
    # this test would pass pre-fix on the two dirs' differing creation mtimes and prove nothing.
    # The `skills/linked/SKILL.md` entry is NOT itself a symlink, so its element 4 is None on
    # both sides -- the `skills/linked` DIR entry is what carries the signal.
    server, out_dir, root, proj = live_server_watching_proj
    fixed_ns = 1_700_000_000_000_000_000
    first = tmp_path / "skill_one"
    second = tmp_path / "skill_two"
    for skill_dir, body in ((first, "# one\n"), (second, "# two\n")):
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(body)   # both 6 bytes
        os.utime(skill_dir / "SKILL.md", ns=(fixed_ns, fixed_ns))
        os.utime(skill_dir, ns=(fixed_ns, fixed_ns))
    link = root / "skills" / "linked"
    link.symlink_to(first)
    _wait_settle(server)   # settle the symlink-creation rebuild
    before = server.state.collect_count
    # ATOMIC retarget, staged OUTSIDE the watched tree. Two windows have to stay closed here and
    # they pull in opposite directions:
    #   (a) a plain link.unlink() + link.symlink_to(second) leaves root/skills one member SHORT;
    #   (b) staging the replacement as root/skills/linked.swap leaves it one member LONG,
    #       flipping listdir membership from ("linked",) to ("linked", "linked.swap").
    # Either one is a MEMBERSHIP change that makes the watcher recollect on its own, so the test
    # would pass PRE-FIX and the Step 9 stash-proof would report a spurious PASS -- sending you
    # hunting a fixture bug that does not exist. Stage in tmp_path, which is OUTSIDE --root (so
    # it is not in the watched set) and on the SAME filesystem (so os.replace is a real atomic
    # rename, not a copy). os.replace then swaps the symlink itself without ever removing it and
    # without root/skills ever holding a different member set.
    staging = tmp_path / "linked.swap"
    os.symlink(second, staging)
    os.replace(staging, link)
    _wait_settle(server)
    assert server.state.collect_count > before, \
        "a metadata-identical deploy-symlink retarget must force a re-collect (TRK-022 finding 6)"


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


def test_same_size_rotation_classifies_truncated(streams_server_no_watch):
    # TRK-022 finding 3 (measured): a telemetry stream renamed aside and replaced by a NEW file
    # of IDENTICAL byte length yielded size 16 -> 16 and classified "none", so the watcher never
    # re-rendered and the dashboard served stale friction data. The inode changes across the
    # rename-and-replace, so file identity is a sufficient signal.
    server, out_dir, root, stream_path = streams_server_no_watch
    key = str(stream_path)
    stream_path.write_text('{"a":1}\n{"a":2}\n')
    srv._rebuild(server.state, out_dir, root, root)   # seeds BOTH the size and the inode
    size_before = stream_path.stat().st_size
    inode_before = server.state.stream_inodes[key]

    rotated = stream_path.parent / (stream_path.name + ".1")
    stream_path.rename(rotated)
    stream_path.write_text('{"z":9}\n{"z":8}\n')      # same length, different records

    assert stream_path.stat().st_size == size_before, \
        "fixture precondition: the size really is unchanged"
    assert stream_path.stat().st_ino != inode_before, \
        "fixture precondition: the file identity really did change"
    classification, changed = srv._classify_stream_sweep(server.state)
    assert classification == "truncated", \
        "a same-size rotation must force a FULL re-collect, not read as no-change (finding 3)"
    assert changed == [key]


def test_append_still_classifies_grown_after_rotation_check(streams_server_no_watch):
    # Positive control for TRK-022 finding 3: the rotation branch sits BEFORE the size ladder, so
    # it must not swallow ordinary growth. An append leaves the inode alone. Had the check
    # compared the full `_stat_identity` tuple (which carries st_mtime_ns, and mtime moves on
    # every append), every append would classify "truncated" and force a full re-collect --
    # destroying the B2 cheap path.
    server, out_dir, root, stream_path = streams_server_no_watch
    key = str(stream_path)
    srv._rebuild(server.state, out_dir, root, root)
    _append_decision_record(stream_path)
    classification, changed = srv._classify_stream_sweep(server.state)
    assert classification == "grown"
    assert changed == [key]


def test_unchanged_stream_still_classifies_none(streams_server_no_watch):
    # Positive control: the extra per-sweep inode stat must not manufacture a change on a stream
    # nobody touched -- that would re-render on every single poll.
    server, out_dir, root, stream_path = streams_server_no_watch
    srv._rebuild(server.state, out_dir, root, root)
    assert srv._classify_stream_sweep(server.state) == ("none", [])


def test_rotation_with_growth_classifies_truncated_exactly_once(streams_server_no_watch):
    # A rotation whose replacement file is LARGER must still classify "truncated" -- records
    # disappeared, so the cheap append-only path would serve stale data -- and the key must be
    # recorded exactly ONCE. The rotation branch and the size ladder are `elif` arms of one
    # chain, never two appends.
    server, out_dir, root, stream_path = streams_server_no_watch
    key = str(stream_path)
    stream_path.write_text('{"a":1}\n')
    srv._rebuild(server.state, out_dir, root, root)
    rotated = stream_path.parent / (stream_path.name + ".1")
    stream_path.rename(rotated)
    stream_path.write_text('{"z":9}\n{"z":8}\n{"z":7}\n')   # NEW file, strictly larger
    classification, changed = srv._classify_stream_sweep(server.state)
    assert classification == "truncated"
    assert changed == [key], "the key must be recorded once, not once per matching arm"


def test_unseeded_inode_does_not_fabricate_a_rotation(streams_server_no_watch):
    # An UNKNOWN inode on either side must fall through to the size ladder rather than report a
    # rotation -- otherwise an unavailable signal would force an endless full re-collect. This is
    # reachable in production for a stream configured but not yet seeded at either publish site.
    server, out_dir, root, stream_path = streams_server_no_watch
    srv._rebuild(server.state, out_dir, root, root)
    server.state.stream_inodes = {}   # a real assignment on a real object: the never-seeded case
    assert srv._classify_stream_sweep(server.state) == ("none", [])


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


def test_startup_warns_when_synthesis_sidecar_absent(tmp_path):
    # serve.py has NO model and cannot generate the synthesis sidecar (the skill's opus Step B
    # does). When the same-date synthesis sidecar is ABSENT at startup, main() must print ONE
    # stderr warning naming the missing file, and STILL start serving (non-blocking advisory).
    # HERMETIC BY CONSTRUCTION (no wall-clock dependency): this test writes NO synthesis sidecar
    # for ANY date, so whatever date the server picks at startup (it computes datetime.now() itself
    # in _rebuild, serve.py:248), the sidecar is absent and the warning fires. We do NOT compare
    # the warned date to a parent-computed today() -- that would race a local-midnight rollover
    # between the parent and the subprocess; instead we assert the warning names SOME same-date
    # `harness-synthesis-<YYYY-MM-DD>.json` (regex, date parsed from the warning itself). The
    # "guard reuses ctx.date, never a fresh today()" invariant is pinned by the STATIC
    # eval-criterion grep (no datetime.now()/date.today() in the guard body), which is the reliable
    # enforcement; a no-mocks subprocess test cannot distinguish the two without freezing time.
    import re
    import subprocess
    import sys
    import threading
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    (root / "CLAUDE.md").write_text("# claude\n")
    # deliberately DO NOT write ANY harness-synthesis-<date>.json
    # Merge stderr INTO stdout (stderr=STDOUT) so the two flushed lines land in ONE ordered pipe:
    # this lets us assert the warning line appears BEFORE the "Serving..." line (proving the
    # flush-before-serving ordering behaviorally, not just by code placement) AND that it fires
    # exactly once (count of warning LINES, not filename substrings -- the warning names the file
    # twice, via {synth_path} and {synth_path.name}, so a substring count would be 2 for ONE line).
    proc = subprocess.Popen(
        [sys.executable, str(SERVE), "--out-dir", str(out_dir), "--root", str(root),
         "--project-root", str(root), "--no-friction"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    # Watchdog: proc.stdout.readline() blocks with NO native timeout, so a server that never
    # prints would hang this test forever. A Timer that kills the process closes the pipe,
    # unblocking readline() (it then returns "") so the assertion fails cleanly instead of hanging.
    watchdog = threading.Timer(20.0, proc.kill)
    watchdog.start()
    lines = []
    try:
        while True:
            line = proc.stdout.readline()
            if line == "":  # EOF: process exited or was killed by the watchdog
                break
            lines.append(line)
            if line.startswith("Serving http://127.0.0.1:"):
                break
        assert lines and lines[-1].startswith("Serving http://127.0.0.1:"), \
            f"server must still start when synthesis is absent; got {lines!r}"
        assert proc.poll() is None, "the missing-synthesis warning must NOT block startup"
    finally:
        watchdog.cancel()
        proc.terminate()
        proc.wait(timeout=10)
    warn_idxs = [i for i, ln in enumerate(lines) if "no synthesis sidecar" in ln]
    assert len(warn_idxs) == 1, \
        f"the missing-synthesis warning must fire EXACTLY ONCE; got lines {lines!r}"
    assert warn_idxs[0] < len(lines) - 1, \
        f"the warning must be flushed BEFORE the 'Serving...' line; got lines {lines!r}"
    warn_line = lines[warn_idxs[0]]
    assert re.search(r"harness-synthesis-\d{4}-\d{2}-\d{2}\.json", warn_line), \
        f"warning must name the missing same-date synthesis sidecar; got {warn_line!r}"
    assert "coverage matrix" in warn_line.lower(), \
        f"warning must say the coverage matrix will be empty; got {warn_line!r}"


def test_startup_silent_when_synthesis_sidecar_present(tmp_path):
    # When the same-date synthesis sidecar IS present at startup, main() must NOT print the
    # missing-synthesis warning (the coverage matrix will render populated).
    # HERMETIC BY CONSTRUCTION (no wall-clock race): the server picks its render date from
    # datetime.now() itself at startup (serve.py:248); to guarantee the sidecar is present for
    # WHATEVER date it lands on -- even across a local-midnight rollover between this setup and the
    # subprocess -- we write valid same-date sidecars for yesterday, today, AND tomorrow. The
    # server's date is necessarily one of {today, today+1} (it runs milliseconds later, at most one
    # midnight can pass), all of which are covered, so the run is silent regardless of clock timing.
    import subprocess
    import sys
    import threading
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    root = tmp_path / "root"
    root.mkdir()
    (root / "rules").mkdir()
    (root / "rules" / "a.md").write_text("# rule a\n")
    (root / "CLAUDE.md").write_text("# claude\n")
    base = datetime.date.today()
    for delta in (-1, 0, 1):  # yesterday / today / tomorrow -- covers any rollover the server hits
        d = (base + datetime.timedelta(days=delta)).strftime("%Y-%m-%d")
        _write_synthesis(out_dir, d, [("Afford", "context")])  # present, valid, same-date
    proc = subprocess.Popen(
        [sys.executable, str(SERVE), "--out-dir", str(out_dir), "--root", str(root),
         "--project-root", str(root), "--no-friction"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    # Watchdog (same rationale as the absent-sidecar test): kill a never-printing server so
    # readline() unblocks and the assertion fails cleanly instead of hanging forever.
    watchdog = threading.Timer(20.0, proc.kill)
    watchdog.start()
    try:
        line = ""
        while True:
            line = proc.stdout.readline()
            if line == "":  # EOF: process exited or was killed by the watchdog
                break
            if line.startswith("Serving http://127.0.0.1:"):
                break
        assert line.startswith("Serving http://127.0.0.1:"), f"server did not start; got {line!r}"
    finally:
        watchdog.cancel()
        proc.terminate()
        proc.wait(timeout=10)
    err = proc.stderr.read()
    assert "no synthesis sidecar" not in err, \
        f"no missing-synthesis warning expected when the sidecar is present; stderr was {err!r}"


# ================================================================= T8: compose propagation
# + compose-aware watched-set + tier-aware, containment-gated watcher + both-root guard

def test_compose_flag_reaches_collector_produces_composed_doc(live_server_watching_compose):
    # Real behavioral proof (no argv introspection/mocks): --compose reaching collector.main()
    # is what produces the compose-only `inspected_roots`/`tier_composition` sidecar fields.
    server, out_dir, root, proj = live_server_watching_compose
    today = datetime.date.today().strftime("%Y-%m-%d")
    sidecar = json.loads((out_dir / f"harness-map-{today}.json").read_text())
    assert "inspected_roots" in sidecar
    assert sidecar["inspected_roots"]["operator"] == str(root.resolve())
    assert sidecar["inspected_roots"]["project_containment"] == str(proj.resolve())
    assert "tier_composition" in sidecar


def test_compose_end_to_end_serves_composed_dashboard(live_server_watching_compose):
    server, out_dir, root, proj = live_server_watching_compose
    port = server.server_address[1]
    body = _get_root_body(port)
    assert "project adds" in body


def test_watched_set_covers_project_tier_additions_both_roots(live_server_watching_compose):
    # WS-B superset, audited across BOTH roots: a nested project command AND the project's
    # own CLAUDE.md must be in the watched set the running compose server seeded from.
    server, out_dir, root, proj = live_server_watching_compose
    snap_keys = set(map(str, server.state.watch_snapshot.keys()))
    assert str(proj / "CLAUDE.md") in snap_keys
    assert str(proj / ".claude" / "commands") in snap_keys
    assert str(root / "rules") in snap_keys                       # operator side still covered


def test_watched_set_tags_operator_and_project_entries(live_server_watching_compose):
    server, out_dir, root, proj = live_server_watching_compose
    snap = server.state.watch_snapshot
    proj_tier, _value = snap[proj / "CLAUDE.md"]
    op_tier, _value2 = snap[root / "CLAUDE.md"]
    assert proj_tier == "project"
    assert op_tier == "operator"


def test_nested_project_command_addition_triggers_recollect(live_server_watching_compose):
    server, out_dir, root, proj = live_server_watching_compose
    _wait_settle(server)
    before = server.state.collect_count
    (proj / ".claude" / "commands" / "brand_new.md").write_text(
        "---\nname: brand_new\ndescription: new.\n---\nBody.\n")
    _wait_settle(server)
    assert server.state.collect_count > before


def test_nested_project_claude_md_addition_triggers_recollect(live_server_watching_compose):
    server, out_dir, root, proj = live_server_watching_compose
    (proj / "sub").mkdir()
    _wait_settle(server)
    before = server.state.collect_count
    (proj / "sub" / "CLAUDE.md").write_text("# nested\n" + "word " * 20)
    _wait_settle(server)
    assert server.state.collect_count > before


def test_project_out_of_root_symlink_not_followed_by_watcher(live_server_watching_compose, tmp_path):
    # T8/R3: an ESCAPING project-tier symlink's target mutating must NOT trigger a recollect --
    # the watcher lstats/readlinks it, never follows into the target (mirrors T3's containment
    # gate exactly).
    server, out_dir, root, proj = live_server_watching_compose
    external = tmp_path / "external_rule.md"
    external.write_text("external v1\n")
    (proj / ".claude" / "rules" / "escaping.md").symlink_to(external)
    _wait_settle(server)   # settle the symlink-creation rebuild
    before = server.state.collect_count
    external.write_text("external v2 changed and longer\n")
    _wait_settle(server)
    assert server.state.collect_count == before, \
        "an escaping project symlink's target change must not trigger a recollect"


def test_project_contained_symlink_target_mutation_triggers_recollect(live_server_watching_compose):
    # T8/R3 regression pin: a CONTAINED project symlink (target lives INSIDE project_root) must
    # still trigger a recollect on target-content change -- the opposite of the escaping case
    # above, proving the gate is containment-based, not a blanket "never follow project tier."
    server, out_dir, root, proj = live_server_watching_compose
    target = proj / "internal_rule.md"
    target.write_text("internal v1\n")
    (proj / ".claude" / "rules" / "aliased.md").symlink_to(target)
    _wait_settle(server)   # settle the symlink-creation rebuild
    before = server.state.collect_count
    target.write_text("internal v2 changed and longer\n")
    _wait_settle(server)
    assert server.state.collect_count > before, \
        "a CONTAINED project symlink's target change must still trigger a recollect"


def test_out_dir_inside_operator_root_rejected_in_compose_mode(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# proj\n" + "word " * 20)
    bad_out = root / "leak-out"
    with pytest.raises(ValueError):
        srv.build_server(out_dir=bad_out, root=root, project_root=proj,
                         host="127.0.0.1", port=0, no_friction=True, compose=True)


def test_out_dir_inside_project_root_rejected_in_compose_mode(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# proj\n" + "word " * 20)
    bad_out = proj / "leak-out"
    with pytest.raises(ValueError):
        srv.build_server(out_dir=bad_out, root=root, project_root=proj,
                         host="127.0.0.1", port=0, no_friction=True, compose=True)


def test_out_dir_outside_both_roots_accepted_in_compose_mode(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# proj\n" + "word " * 20)
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    server = srv.build_server(out_dir=out_dir, root=root, project_root=proj,
                              host="127.0.0.1", port=0, no_friction=True, compose=True)
    server.server_close()


def test_non_compose_out_dir_guard_still_rejects_inside_operator_root(tmp_path):
    # Regression pin: the NEW startup guard must not regress the pre-existing (non-compose)
    # single-root protection write_html_safely already enforced at render time.
    root = tmp_path / "root"
    root.mkdir()
    bad_out = root / "leak-out"
    with pytest.raises((ValueError, srv.render_html.RenderError, srv.CollectorError, SystemExit)):
        srv.build_server(out_dir=bad_out, root=root, project_root=root,
                         host="127.0.0.1", port=0, no_friction=True)


# ==================================================== P1-B: write-time re-validation
# build_server's own startup guard (above) validates --out-dir ONCE, before the server
# starts running. These pin the SEPARATE, later, write-time guard: every full/cheap
# rebuild's HTML write must independently re-validate against BOTH roots at the moment
# it writes -- a --out-dir symlink (or a pre-existing symlink AT the html filename
# itself) safe at startup, retargeted afterward, must never let a write land inside the
# project-containment root.

def _minimal_compose_server(tmp_path, out_dir):
    root = tmp_path / "root"
    root.mkdir()
    proj = tmp_path / "projroot"
    proj.mkdir()
    (proj / "CLAUDE.md").write_text("# proj\n" + "word " * 20)
    server = srv.build_server(out_dir=out_dir, root=root, project_root=proj,
                              host="127.0.0.1", port=0, no_friction=True, compose=True)
    return server, root, proj


def test_rebuild_friction_only_rejects_html_path_symlinked_into_project_root(tmp_path):
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    server, root, proj = _minimal_compose_server(tmp_path, out_dir)
    try:
        ctx_date = server.state.ctx.date
        html_path = out_dir / f"harness-map-{ctx_date}.html"
        leak_target = proj / "leak-friction.html"
        html_path.unlink()
        html_path.symlink_to(leak_target)
        with pytest.raises(srv.render_html.RenderError):
            srv._rebuild_friction_only(server.state, out_dir)
        assert not leak_target.exists(), \
            "a pre-existing html-path symlink into the project root must never be written through"
    finally:
        server.server_close()


def test_rebuild_rejects_html_path_symlinked_into_project_root(tmp_path):
    out_dir = tmp_path / "served"
    out_dir.mkdir()
    server, root, proj = _minimal_compose_server(tmp_path, out_dir)
    try:
        today = datetime.date.today().strftime("%Y-%m-%d")
        html_path = out_dir / f"harness-map-{today}.html"
        leak_target = proj / "leak-full.html"
        html_path.unlink()
        html_path.symlink_to(leak_target)
        with pytest.raises(srv.render_html.RenderError):
            srv._rebuild(server.state, out_dir, root, proj, compose=True)
        assert not leak_target.exists(), \
            "a pre-existing html-path symlink into the project root must never be written through"
    finally:
        server.server_close()


def test_rebuild_friction_only_rejects_out_dir_symlink_retargeted_into_project_root(tmp_path):
    # The fuller production shape: --out-dir ITSELF is a symlink, safe at startup, then
    # retargeted into the project root before the next cheap re-render.
    safe_target = tmp_path / "safe_out"
    safe_target.mkdir()
    out_link = tmp_path / "out_link"
    out_link.symlink_to(safe_target)
    server, root, proj = _minimal_compose_server(tmp_path, out_link)
    try:
        out_link.unlink()
        out_link.symlink_to(proj)
        with pytest.raises(srv.render_html.RenderError):
            srv._rebuild_friction_only(server.state, out_link)
        assert not any(proj.glob("harness-map-*.html")), \
            "a retargeted --out-dir symlink must never let a write land inside the project root"
    finally:
        server.server_close()


def test_guard_rejection_survives_watcher_degrade_handler_not_bare_systemexit(tmp_path):
    """P2 regression pin (Codex challenge): `write_html_safely`'s guard rejection must be
    a catchable `Exception`, not a bare `SystemExit` (a `BaseException`) that would
    escape `_watcher_loop`'s degrade handlers and silently kill the daemon thread. This
    reproduces the EXACT shape of the watcher loop's own cheap-path handler
    (serve.py ~642-649: `try: _rebuild_friction_only(...) except Exception as exc:
    ...degrade to full recollect...`) against the realistic production attack (T8 P1-B):
    a compose `--out-dir` symlink, safe at startup, retargeted into the project root
    AFTER the server is already live and serving. Before the fix, the inner
    `except Exception` here does NOT catch the guard's `SystemExit`, so it propagates
    out of this test uncaught -- pytest reports the test itself as errored (RED). After
    the fix, `RenderError` is caught exactly like any other rebuild fault (GREEN)."""
    safe_target = tmp_path / "safe_out"
    safe_target.mkdir()
    out_link = tmp_path / "out_link"
    out_link.symlink_to(safe_target)
    server, root, proj = _minimal_compose_server(tmp_path, out_link)
    try:
        out_link.unlink()
        out_link.symlink_to(proj)
        caught = None
        try:
            srv._rebuild_friction_only(server.state, out_link)
        except Exception as exc:  # mirrors _watcher_loop's own cheap-path degrade handler verbatim
            caught = exc
        assert caught is not None, \
            "the guard rejection must raise something an `except Exception` handler can catch"
        assert isinstance(caught, Exception)
        assert not isinstance(caught, SystemExit), \
            "SystemExit is a BaseException -- it would escape every watcher degrade handler"
        assert isinstance(caught, srv.render_html.RenderError)
        assert not any(proj.glob("harness-map-*.html")), \
            "the guard rejection must still block the write, not just become catchable"
    finally:
        server.server_close()


def test_write_guard_roots_refuses_a_nonstring_sidecar_root(tmp_path):
    """A26 (S6a guard-fix audit, HIGH). `_write_guard_roots` used to silently DROP a
    non-string `ctx.doc["root"]` from the returned list instead of refusing -- exactly
    the render_html.py `main()` fail-open (see test_render_html.py's A26 tests), just
    reached from serve.py's write-time re-validation instead of the one-shot CLI. A
    dropped root leaves `guard_roots` EMPTY, and `write_html_safely` skips containment
    validation entirely on an empty `guard_roots`. `ctx` here is a REAL `RenderContext`
    built by the actual production `render_from_out_dir` (no mock, no hand-rolled
    dataclass) from an on-disk sidecar whose `root` field is `5` -- the same shape a
    corrupted or crafted sidecar could produce; the real collector itself never emits a
    non-string root, so this is the only no-mock way to reach the branch. Both callers
    (`_rebuild`, `_rebuild_friction_only`) already treat a `RenderError` from this call
    as a normal, catchable rebuild fault (see the P1-B tests above) -- it degrades to
    keep-last-good / falls back to a full rebuild and never crashes the server."""
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc["root"] = 5
    _write_sidecar(out_dir, "2026-07-15", doc)
    ctx = srv.render_html.render_from_out_dir(
        out_dir, date="2026-07-15", streams=None, no_friction=True)
    with pytest.raises(srv.render_html.RenderError, match="root field is not a string"):
        srv._write_guard_roots(ctx)


def test_write_guard_roots_folds_in_the_floor_root_for_falsy_sidecar_root(tmp_path):
    """S6a guard-fix v2 -- the FALSY half of the fail-open A26 (above) only closed for
    the wrong-type case. `_write_guard_roots` used to return an EMPTY list when
    `ctx.doc["root"]` was falsy (absent/null/"") on a non-compose ctx -- the ORDINARY
    shape, since `inspected_roots` is compose-only -- and `write_html_safely` skips
    containment validation entirely on an empty guard list. The fix folds in a permanent
    floor root (`Path.home() / ".claude"`), ADDITIVE to the sidecar-derived roots, so the
    returned list is never empty on a falsy root. Uses the REAL `_home` env-var swap
    (never a mock) so `Path.home()` inside `serve.py` resolves to the fixture during the
    block. Then proves the floor actually blocks a write inside it via the SAME
    `except Exception` shape `_watcher_loop`'s degrade handler uses verbatim (see
    `test_guard_rejection_survives_watcher_degrade_handler_not_bare_systemexit` above) --
    a catchable `RenderError`, never `SystemExit`, and no file written."""
    home = tmp_path / "home"
    home.mkdir()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    doc = _minimal_doc()
    doc.pop("root")
    _write_sidecar(out_dir, "2026-07-15", doc)
    with _home(home):
        ctx = srv.render_html.render_from_out_dir(
            out_dir, date="2026-07-15", streams=None, no_friction=True)
        roots = srv._write_guard_roots(ctx)
        assert roots == [str(home / ".claude")], \
            "the floor root must be the ONLY entry when both sidecar-derived roots are absent"
        floor_target = home / ".claude" / "harness-map-out"
        floor_target.mkdir(parents=True)
        html_path = floor_target / "harness-map-2026-07-15.html"
        caught = None
        try:
            srv.render_html.write_html_safely(html_path, ctx.html_text, roots)
        except Exception as exc:  # mirrors _watcher_loop's own degrade handler verbatim
            caught = exc
        assert caught is not None, \
            "the floor must raise something an `except Exception` handler can catch"
        assert isinstance(caught, srv.render_html.RenderError)
        assert not isinstance(caught, SystemExit), \
            "SystemExit is a BaseException -- it would escape every watcher degrade handler"
        assert not html_path.exists(), \
            "the floor must still block the write, not just become catchable"


# ================================================================= T9: integration test net
# The SAME maximal two-tier fixture test_collector.py/test_render_html.py exercise, served
# live via `build_server` (in-process, matching every other serve.py test -- serve.py never
# shells out to collector.py). HOME is sandboxed via os.environ save/restore, the same
# real-env-var pattern `live_server_watching_compose` (T8) already established.

def test_maximal_two_tier_fixture_serves_composed_dashboard(fake_harness, tmp_path):
    proj, home = _build_two_tier_maximal_fixture(fake_harness, tmp_path)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    out_dir = tmp_path / "served_maximal"
    out_dir.mkdir()
    try:
        server = srv.build_server(out_dir=out_dir, root=fake_harness, project_root=proj,
                                  host="127.0.0.1", port=0, no_friction=True, compose=True)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            port = server.server_address[1]
            body = _get_root_body(port)
            # tenant isolation + dark-skill callout, served live (not just rendered to disk)
            assert "project adds 8 / overrides 1 / 2 dark" in body
            assert "Dark project skills" in body
            assert "skill:demo" in body and "command:demo-cmd" in body
            # composed-settings section (T7b), all four cards
            assert "MCP servers (composed)" in body
            assert "Hooks (composed, all tiers)" in body
            assert "Permissions (composed, union)" in body
            assert "Settings overrides (composed)" in body
            # secret-safety end-to-end at the SERVED-BODY layer, on the SAME fixture
            for secret in _SECRET_SENTINELS:
                assert secret not in body, f"raw secret leaked into the served page: {secret}"
        finally:
            server.shutdown()
            server.server_close()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


def test_maximal_two_tier_fixture_serves_old_shape_dashboard_when_compose_unset(
        fake_harness, tmp_path):
    """C15 back-compat at the SERVE layer: the SAME fixture, served with `compose`
    unset (the default), must produce and serve a genuinely old-shape page -- no
    tier-summary band, no composed-settings cards -- proving serve.py's own default
    path (not just render_html.py in isolation) tolerates the absent-tier case."""
    proj, home = _build_two_tier_maximal_fixture(fake_harness, tmp_path)
    old_home = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    out_dir = tmp_path / "served_old_shape"
    out_dir.mkdir()
    try:
        server = srv.build_server(out_dir=out_dir, root=fake_harness, project_root=proj,
                                  host="127.0.0.1", port=0, no_friction=True)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            port = server.server_address[1]
            body = _get_root_body(port)
            assert "harness-map" in body and "<!DOCTYPE html>" in body
            assert 'id="tier-summary"' not in body
            assert "MCP servers (composed)" not in body
            assert "Hooks (composed, all tiers)" not in body
            assert "Permissions (composed, union)" not in body
            assert "Settings overrides (composed)" not in body
        finally:
            server.shutdown()
            server.server_close()
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home


@contextlib.contextmanager
def _home(path):
    """Point $HOME at a real temp tree for the duration of the block, then restore it.
    A REAL environment change, not a patched one -- the same save/restore shape the
    compose-mode test above already uses, kept as a helper so the two S6a tests below
    cannot leak $HOME into the rest of the module on an assertion failure."""
    old = os.environ.get("HOME")
    os.environ["HOME"] = str(path)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old


def test_build_streams_forwards_the_server_root(tmp_path):
    """T3.8 — §4.5, finding #3: `_build_streams` must forward the server's --root, or
    serve mode silently keeps the OLD uncontained default while the CLI path is fixed --
    the worst of both. serve.py is otherwise a verification task: the watch / snapshot /
    sweep machinery is genuinely unchanged."""
    home = tmp_path / "home"
    claude = home / ".claude"
    slug = re.sub(r"[/.]", "-", os.path.abspath(str(claude)))
    (claude / "projects" / slug / "memory").mkdir(parents=True)
    foreign = tmp_path / "some-other-repo"
    foreign.mkdir()
    with _home(home):
        assert srv._build_streams(False, claude)["interventions"] is not None
        assert srv._build_streams(False, foreign)["interventions"] is None


def test_stream_paths_list_includes_the_interventions_path(tmp_path):
    """T3.9 — the moment the default stops being None, watch coverage, size snapshotting
    and sweep classification engage with NO further edits, because all three iterate
    `_stream_paths_list(state.streams)`. This pins that the fourth path actually reaches
    that list."""
    home = tmp_path / "home"
    claude = home / ".claude"
    slug = re.sub(r"[/.]", "-", os.path.abspath(str(claude)))
    (claude / "projects" / slug / "memory").mkdir(parents=True)
    with _home(home):
        paths = srv._stream_paths_list(srv._build_streams(False, claude))
    assert any(p.endswith("memory/interventions.jsonl") for p in paths)
