"""Bounded, serialized on-demand bridge startup for macOS and Linux.

Never stop a port owner during startup. Health checks require the service marker,
checkout id and process identity, not just an HTTP 200. No login service is installed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import queue
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parent
CHECKOUT_ID = hashlib.sha256(str(ROOT).encode()).hexdigest()
DEFAULT_PORT = 18787
START_TIMEOUT = 10.0


class BridgeError(RuntimeError):
    pass


def state_dir():
    path = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "figmosha"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def paths(port):
    base = state_dir()
    return base / f"bridge-{port}.lock", base / f"bridge-{port}.json", base / f"bridge-{port}.log"


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise BridgeError("bridge startup timed out (including the concurrent-start lock wait).")
    return value


def validate_target(host, port):
    if host not in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        raise BridgeError("automatic startup only supports loopback hosts; use figmosha.py directly for a remote bridge.")
    if not 1 <= port <= 65535:
        raise BridgeError("port must be between 1 and 65535.")


@contextmanager
def locked(port, deadline):
    lock, _, _ = paths(port)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        while True:
            remaining(deadline)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(0.05, remaining(deadline)))
        yield
    finally:
        os.close(fd)  # releases the kernel lock, including after a crash


def _read_json(conn, sockets, host, port, path):
    try:
        conn.connect()
        sockets.append(conn.sock)
        conn.request("GET", path)
        response = conn.getresponse()
        data = response.read(131073)
        if response.status != 200 or len(data) > 131072:
            raise BridgeError(f"port {port} is occupied by an incompatible service ({host}{path}). No process was stopped.")
        try:
            value = json.loads(data)
        except (ValueError, UnicodeError) as exc:
            raise BridgeError(f"port {port} did not return Figmosha JSON. No process was stopped.") from exc
        if not isinstance(value, dict):
            raise BridgeError(f"port {port} did not return a Figmosha object. No process was stopped.")
        return value
    except OSError as exc:
        if exc.errno in {errno.ECONNREFUSED, errno.EADDRNOTAVAIL, errno.ENETUNREACH, errno.EAFNOSUPPORT}:
            return None
        raise BridgeError(f"port {port} is occupied or unresponsive ({host}); refusing to start another bridge.") from exc
    except http.client.HTTPException as exc:
        raise BridgeError(f"port {port} has an invalid HTTP service; refusing to start another bridge.") from exc
    finally:
        conn.close()


def read_json(host, port, path, deadline):
    # Socket inactivity timeouts alone do not bound a server that drip-feeds
    # headers/body. A daemon reader also enforces an absolute request budget.
    budget = min(0.4, remaining(deadline))
    conn = http.client.HTTPConnection(host, port, timeout=budget)
    results = queue.Queue(maxsize=1)
    sockets = []

    def read():
        try:
            results.put((True, _read_json(conn, sockets, host, port, path)))
        except Exception as exc:
            results.put((False, exc))

    threading.Thread(target=read, daemon=True).start()
    try:
        ok, result = results.get(timeout=budget)
    except queue.Empty as exc:
        for sock in sockets:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        raise BridgeError(f"port {port} did not answer within the health-check deadline; no process was stopped.") from exc
    if not ok:
        raise result
    return result


def probe(port, deadline, allow_outdated=False):
    """Check both loopback families; reject any occupied, incompatible endpoint."""
    found = None
    for host in ("127.0.0.1", "::1"):
        root = read_json(host, port, "/", deadline)
        if root is None:
            continue
        status = read_json(host, port, "/status", deadline)
        if (root.get("service") != "figmosha-bridge" or not status
                or status.get("service") != "figmosha-bridge"
                or status.get("checkout_id") != CHECKOUT_ID
                or type(status.get("pid")) is not int or status["pid"] <= 0
                or not isinstance(status.get("instance_id"), str)
                or len(status["instance_id"]) != 32
                or type(status.get("plugin_connected")) is not bool):
            raise BridgeError(f"port {port} is not a verified bridge from this checkout (or is an older build). No process was stopped.")
        if status.get("bridge_outdated") and not allow_outdated:
            raise BridgeError(f"bridge on {port} runs stale code; restart that verified bridge explicitly.")
        if found and (status["pid"], status["instance_id"]) != (found["pid"], found["instance_id"]):
            raise BridgeError(f"IPv4 and IPv6 on port {port} belong to different bridge processes.")
        found = status
    return found


def ensure_bridge(host="127.0.0.1", port=DEFAULT_PORT, timeout=START_TIMEOUT):
    validate_target(host, port)
    deadline = time.monotonic() + timeout
    with locked(port, deadline):
        status = probe(port, deadline)
        if status:
            return status
        python = ROOT / "venv/bin/python"
        if not python.is_file():
            raise BridgeError(f"missing {python}; create the project venv and install requirements.txt first.")
        _, record, logfile = paths(port)
        # Append: previous failure diagnostics stay available. The child inherits
        # no lock/terminal descriptors and has a separate session, not a login job.
        fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            child = subprocess.Popen(
                [str(python), "-u", str(ROOT / "bridge.py"), "--host", "127.0.0.1", "--port", str(port)],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=fd, stderr=subprocess.STDOUT,
                start_new_session=True, close_fds=True,
            )
        finally:
            os.close(fd)
        try:
            while True:
                remaining(deadline)
                if child.poll() is not None:
                    raise BridgeError(f"bridge exited with code {child.returncode}; see {logfile}")
                status = probe(port, deadline)
                if status:
                    if status["pid"] != child.pid:
                        raise BridgeError(f"port {port} changed owners during startup; refusing to use it.")
                    temp = record.with_name(record.name + "." + uuid.uuid4().hex + ".tmp")
                    try:
                        with open(temp, "x") as out:
                            json.dump({key: status[key] for key in ("pid", "instance_id", "checkout_id")}, out)
                        temp.replace(record)
                    finally:
                        temp.unlink(missing_ok=True)
                    print(f"figmosha: started bridge on 127.0.0.1:{port} (pid {child.pid}); log: {logfile}", file=sys.stderr)
                    return status
                time.sleep(min(0.05, remaining(deadline)))
        except BaseException:
            # Only clean up the exact child we created, never a process discovered
            # via a port or stale PID file. Cleanup adds at most 2 s to the budget.
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=1)
            raise


def stop_bridge(port=DEFAULT_PORT):
    """Stop only a healthy instance whose identity matches our startup record."""
    validate_target("127.0.0.1", port)
    deadline = time.monotonic() + START_TIMEOUT
    with locked(port, deadline):
        _, record, _ = paths(port)
        status = probe(port, deadline, allow_outdated=True)
        if status is None:
            record.unlink(missing_ok=True)
            print(f"figmosha: no bridge listening on {port}")
            return
        try:
            saved = json.loads(record.read_text())
        except (OSError, ValueError) as exc:
            raise BridgeError("no startup record for this bridge; refusing to stop a manually started process.") from exc
        if any(saved.get(key) != status.get(key) for key in ("pid", "instance_id", "checkout_id")):
            raise BridgeError("bridge identity differs from the startup record; no process was stopped.")
        os.kill(status["pid"], signal.SIGTERM)
        while True:
            try:
                current = probe(port, deadline, allow_outdated=True)
                if current is None:
                    break
                if current["instance_id"] != status["instance_id"]:
                    raise BridgeError("port changed owners while stopping; no further signal was sent.")
            except BridgeError as exc:
                # A server closing its HTTP sockets after SIGTERM may reset an
                # in-flight health request. Retry only those shutdown races.
                if not isinstance(exc.__cause__, (ConnectionResetError, ConnectionAbortedError,
                                                  BrokenPipeError, http.client.RemoteDisconnected)):
                    raise
            time.sleep(min(0.05, remaining(deadline)))
        record.unlink(missing_ok=True)
        print(f"figmosha: stopped bridge on {port}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    options = parser.parse_args()
    try:
        if options.action == "stop":
            stop_bridge(options.port)
        else:
            ensure_bridge(port=options.port)
    except (BridgeError, OSError) as exc:
        parser.exit(2, f"figmosha: {exc}\n")
