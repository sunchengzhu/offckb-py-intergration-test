from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from .harness import (
    DEVNET_PORTS, CommandResult, DevnetManager, _command_references_path,
    is_port_open, wait_until,
)


pytestmark = pytest.mark.core


def _owned_processes(devnet: DevnetManager) -> dict[int, str]:
    """The unique case directory occurs in daemon binary arguments and CKB -C arguments."""
    snapshot = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5, check=True,
    )
    owned = {}
    for line in snapshot.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) != 2:
            continue
        pid, command = fields
        if _command_references_path(command, devnet.runner.cwd.parent) and (
            _command_references_path(command, devnet.runner.cli_entry)
            or _command_references_path(command, devnet.ckb_bin)
        ):
            owned[int(pid)] = command
    return owned


def _cleanup_remaining_processes(devnet: DevnetManager) -> None:
    """Failure-only cleanup also finds components left behind after PID metadata was deleted."""
    for termination_signal in (signal.SIGTERM, signal.SIGKILL):
        for pid, command in _owned_processes(devnet).items():
            if _owned_processes(devnet).get(pid) != command:
                continue
            try:
                os.kill(pid, termination_signal)
            except ProcessLookupError:
                pass
        try:
            wait_until(
                lambda: not _owned_processes(devnet), timeout_s=5,
                description="this test's remaining OffCKB components to exit",
            )
            return
        except AssertionError:
            if termination_signal == signal.SIGKILL:
                raise


@pytest.fixture
def recovery_devnet(isolated_devnet: DevnetManager) -> Iterator[DevnetManager]:
    devnet = isolated_devnet
    # A real binary with a case-specific argument lets us find an orphan daemon
    # without assuming that the product retained its PID file after a failure.
    binary = devnet.runner.cwd / "ckb"
    binary.symlink_to(devnet.ckb_bin)
    devnet.ckb_bin = binary
    try:
        yield devnet
    finally:
        try:
            devnet.close()
        finally:
            _cleanup_remaining_processes(devnet)


def _assert_start_failed(result: CommandResult) -> str:
    assert result.returncode != 0, f"startup unexpectedly succeeded: {result.stdout}"
    assert not result.stdout.strip(), f"failed startup reported a result on stdout: {result.stdout}"
    records = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    failures = [record for record in records if record.get("ok") is False]
    assert len(failures) == 1, f"expected one structured startup error: {result.stderr}"
    error = failures[0]
    assert error.get("code") and isinstance(error.get("message"), str), error
    assert error["message"].strip(), error
    return error["message"]


def _assert_failed_start_clean(devnet: DevnetManager, *, occupied_port: int | None = None) -> None:
    try:
        wait_until(
            lambda: not _owned_processes(devnet)
            and not devnet.pid_file.exists()
            and not any(is_port_open(port) for port in DEVNET_PORTS if port != occupied_port),
            timeout_s=10,
            description="OffCKB itself to remove failed-start processes, PID metadata and listeners",
        )
    except AssertionError as error:
        error.add_note(f"remaining owned processes: {_owned_processes(devnet)}")
        error.add_note(f"PID metadata exists: {devnet.pid_file.exists()}")
        error.add_note(f"listening ports: {[port for port in DEVNET_PORTS if is_port_open(port)]}")
        raise


def _assert_chain_advances(devnet: DevnetManager) -> None:
    assert devnet.rpc.ready() and devnet.proxy_rpc.ready()
    before = devnet.rpc.tip()
    wait_until(
        lambda: devnet.rpc.tip() > before, timeout_s=30,
        description="the recovered developer chain to continue producing blocks",
    )


# TEST-MAP: NODE-03
def test_duplicate_start_keeps_the_original_chain_running(recovery_devnet: DevnetManager) -> None:
    """重复启动给出已有服务提示，原开发链和组件保持可用。"""
    devnet = recovery_devnet
    devnet.start()
    metadata = devnet.pid_file.read_bytes()
    original_processes = _owned_processes(devnet)
    assert devnet.pid in original_processes and len(original_processes) >= 3

    result = devnet.runner.run(
        "node", "--daemon", "--binary-path", devnet.ckb_bin,
        check=False, timeout_s=devnet.startup_timeout_s,
    )

    message = _assert_start_failed(result)
    assert re.search(r"already.*(?:running|answering|progress)", message, re.IGNORECASE), message
    assert devnet.pid_file.read_bytes() == metadata
    assert _owned_processes(devnet) == original_processes
    _assert_chain_advances(devnet)
    assert _owned_processes(devnet) == original_processes


# TEST-MAP: NODE-09
@pytest.mark.parametrize("invalid_binary", ["missing", "not-executable"])
def test_invalid_binary_can_be_corrected_and_retried(
    recovery_devnet: DevnetManager, invalid_binary: str,
) -> None:
    """错误路径或不可执行文件启动失败后，修正路径即可开始开发。"""
    devnet = recovery_devnet
    binary = devnet.runner.cwd / invalid_binary
    if invalid_binary == "not-executable":
        binary.write_text("This file is not an executable CKB binary.\n", encoding="utf-8")
        binary.chmod(0o600)

    result = devnet.runner.run(
        "node", "--daemon", "--binary-path", binary,
        check=False, timeout_s=devnet.startup_timeout_s,
    )

    message = _assert_start_failed(result)
    assert re.search(r"binary|path|log|verbose", message, re.IGNORECASE), message
    _assert_failed_start_clean(devnet)
    devnet.start()
    _assert_chain_advances(devnet)


_CONFLICT_SERVICE = """
import http.server
import socketserver
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    timeout = 2

    def do_GET(self):
        body = b'unrelated local development service'
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET

    def log_message(self, *args):
        pass

class Server(http.server.ThreadingHTTPServer):
    def server_bind(self):
        # Binding a numeric loopback address does not need a reverse DNS lookup.
        socketserver.TCPServer.server_bind(self)
        self.server_name = 'localhost'
        self.server_port = self.server_address[1]

server = Server(('127.0.0.1', int(sys.argv[1])), Handler)
print('listening', server.server_address, flush=True)
server.serve_forever()
"""


def _service_responds(process: subprocess.Popen, port: int) -> bool:
    assert process.poll() is None, f"unrelated local service exited with {process.returncode}"
    with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=1) as response:
        return response.read() == b"unrelated local development service"


@contextmanager
def _conflicting_service(devnet: DevnetManager, port: int) -> Iterator[subprocess.Popen]:
    assert not is_port_open(port), f"refusing to occupy a port already owned by another service: {port}"
    with (devnet.runner.records_dir / f"conflicting-service-{port}.log").open("wb") as output:
        process = subprocess.Popen(
            [sys.executable, "-u", "-c", _CONFLICT_SERVICE, str(port)],
            stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            wait_until(
                lambda: _service_responds(process, port), timeout_s=10,
                description=f"the unrelated local HTTP service on {port} to respond",
            )
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            wait_until(
                lambda: not is_port_open(port), timeout_s=5,
                description=f"the test's conflict service to release port {port}",
            )


# TEST-MAP: NODE-11
@pytest.mark.parametrize("port", [8114, 28114], ids=["direct-rpc", "proxy-rpc"])
def test_port_conflict_preserves_other_service_and_allows_retry(
    recovery_devnet: DevnetManager, port: int,
) -> None:
    """其他本地服务占用 RPC 端口时不被误伤，释放端口后正常启动。"""
    devnet = recovery_devnet
    with _conflicting_service(devnet, port) as service:
        result = devnet.runner.run(
            "node", "--daemon", "--binary-path", devnet.ckb_bin,
            check=False, timeout_s=devnet.startup_timeout_s,
        )

        _assert_start_failed(result)
        assert _service_responds(service, port)
        _assert_failed_start_clean(devnet, occupied_port=port)
        assert _service_responds(service, port)

    devnet.start()
    _assert_chain_advances(devnet)
