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


def test_default_host_binds_loopback(live_server):
    server, _ = live_server
    assert server.server_address[0] == "127.0.0.1"
