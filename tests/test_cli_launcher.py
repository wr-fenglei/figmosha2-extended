"""Exercise the installed-style CLI against real local processes, never Figma."""
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

import pytest

fcntl = pytest.importorskip("fcntl", reason="on-demand launcher is POSIX-only")

import bridge_control as control

ROOT = Path(__file__).resolve().parents[1]
ENTRY = ROOT / "bin/figmosha"


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("FIGMOSHA_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("FIGMOSHA_PORT", raising=False)
    monkeypatch.delenv("FIGMOSHA_HOST", raising=False)
    return tmp_path


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def call(port, *args, cwd, executable=ENTRY):
    return subprocess.run([str(executable), "--port", str(port), *args], cwd=cwd,
                          capture_output=True, text=True, timeout=15)


def test_cold_start_concurrent_reuse_and_safe_stop(isolated):
    port = free_port()
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            runs = list(pool.map(lambda _: call(port, "status", cwd=isolated), range(4)))
        assert all(run.returncode == 0 for run in runs), [(r.stdout, r.stderr) for r in runs]
        identities = {(json.loads(r.stdout)["pid"], json.loads(r.stdout)["instance_id"]) for r in runs}
        assert len(identities) == 1
        assert sum("started bridge" in r.stderr for r in runs) == 1
        again = call(port, "status", cwd=isolated)
        assert again.returncode == 0, again.stderr
        assert "started bridge" not in again.stderr
        assert (json.loads(again.stdout)["pid"], json.loads(again.stdout)["instance_id"]) in identities
        assert json.loads(again.stdout)["plugin_connected"] is False
    finally:
        control.stop_bridge(port)
    assert control.probe(port, time.monotonic() + 1) is None


@pytest.mark.parametrize("args,code", [
    (["--help"], 0), (["--version"], 0), (["exec", "--help"], 0),
    (["get-selection", "--print-js"], 0), (["status", "--not-an-option"], 2),
])
def test_offline_commands_never_start_bridge(isolated, args, code):
    port = free_port()
    result = call(port, *args, cwd=isolated)
    assert result.returncode == code, result.stderr
    assert not (isolated / "state/figmosha").exists()
    assert control.probe(port, time.monotonic() + 1) is None


@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_conflicting_http_200_service_is_never_used_or_stopped(isolated, family):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Even a plausible old service marker / status is insufficient.
            value = {"service": "figmosha-bridge"} if self.path == "/" else {"plugin_connected": False}
            data = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        address_family = family

    try:
        server = Server(("127.0.0.1" if family == socket.AF_INET else "::1", 0), Handler)
    except OSError:
        if family == socket.AF_INET6:
            pytest.skip("IPv6 unavailable")
        raise
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    _, record, _ = control.paths(port)
    record.write_text(json.dumps({"pid": os.getpid()}))  # stale/untrusted record must not be killed
    try:
        result = call(port, "status", cwd=isolated)
        assert result.returncode == 2
        assert "not a verified bridge" in result.stderr
        assert "started bridge" not in result.stderr
        assert server.socket.fileno() >= 0
        assert control.read_json("127.0.0.1" if family == socket.AF_INET else "::1", port, "/", time.monotonic() + 1)
        assert not control.paths(port)[2].exists()
    finally:
        server.shutdown()
        server.server_close()


def test_lock_wait_is_bounded_and_crashed_holder_releases_lock(isolated):
    port = free_port()
    lock, _, _ = control.paths(port)
    with open(lock, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        started = time.monotonic()
        with pytest.raises(control.BridgeError, match="timed out"):
            control.ensure_bridge(port=port, timeout=0.15)
        assert time.monotonic() - started < 1
        assert not control.paths(port)[2].exists()
    # A leftover lock file is fine: only the kernel lock controls ownership.
    with control.locked(port, time.monotonic() + 1):
        pass


@pytest.mark.parametrize("script,error", [
    ("raise SystemExit(17)", "exited with code 17"),
    ("import time; time.sleep(30)", "timed out"),
])
def test_failed_or_hung_start_is_bounded_and_cleans_only_its_child(isolated, monkeypatch, script, error):
    fake = isolated / "checkout"
    (fake / "venv/bin").mkdir(parents=True)
    (fake / "venv/bin/python").symlink_to(sys.executable)
    (fake / "bridge.py").write_text(script)
    monkeypatch.setattr(control, "ROOT", fake)
    children = []
    original = subprocess.Popen

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(control.subprocess, "Popen", spawn)
    started = time.monotonic()
    with pytest.raises(control.BridgeError, match=error):
        control.ensure_bridge(port=free_port(), timeout=0.3)
    assert time.monotonic() - started < 3
    assert len(children) == 1 and children[0].poll() is not None


def test_stop_refuses_mismatched_instance_record(isolated):
    port = free_port()
    status = control.ensure_bridge(port=port)
    _, record, _ = control.paths(port)
    correct = record.read_text()
    try:
        record.write_text(json.dumps({"pid": status["pid"], "instance_id": "different", "checkout_id": control.CHECKOUT_ID}))
        with pytest.raises(control.BridgeError, match="identity differs"):
            control.stop_bridge(port)
        assert control.probe(port, time.monotonic() + 1)["pid"] == status["pid"]
    finally:
        record.write_text(correct)
        control.stop_bridge(port)


def test_user_install_any_directory_and_uninstall_without_overwriting(isolated):
    bindir = isolated / "user bin"
    env = dict(os.environ, PATH="/usr/bin:/bin")
    command = [sys.executable, str(ROOT / "install-cli.py"), "--bin-dir", str(bindir)]
    for _ in range(2):
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    entry = bindir / "figmosha"
    assert entry.is_symlink() and entry.resolve() == ENTRY
    port = free_port()
    try:
        result = call(port, "status", cwd=isolated, executable=entry)
        assert result.returncode == 0, result.stderr
    finally:
        control.stop_bridge(port)
    assert subprocess.run(command + ["--uninstall"], env=env, capture_output=True).returncode == 0
    assert not entry.exists()
    entry.write_text("another tool")
    for suffix in ([], ["--uninstall"]):
        result = subprocess.run(command + suffix, env=env, capture_output=True, text=True)
        assert result.returncode == 2
        assert entry.read_text() == "another tool"


def test_proxy_environment_cannot_redirect_local_cli_and_ipv6_works(isolated, monkeypatch):
    port = free_port()
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        result = call(port, "status", cwd=isolated)
        assert result.returncode == 0, result.stderr
        ipv6 = call(port, "--host", "::1", "status", cwd=isolated)
        assert ipv6.returncode == 0, ipv6.stderr
        assert json.loads(result.stdout)["instance_id"] == json.loads(ipv6.stdout)["instance_id"]
    finally:
        control.stop_bridge(port)


def test_drip_fed_http_headers_cannot_extend_startup_deadline(isolated):
    class SlowHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            try:
                self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Slow: ")
                for _ in range(200):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.02)
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    started = time.monotonic()
    try:
        with pytest.raises(control.BridgeError, match="deadline"):
            control.ensure_bridge(port=server.server_address[1], timeout=0.2)
        assert time.monotonic() - started < 1
    finally:
        server.shutdown()
        server.server_close()


def test_non_loopback_autostart_is_refused(isolated):
    with pytest.raises(control.BridgeError, match="only supports loopback"):
        control.ensure_bridge(host="192.0.2.1")
    assert not (isolated / "state/figmosha").exists()
