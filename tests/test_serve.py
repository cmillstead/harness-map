import http.client
import importlib.util
import os
import threading
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
