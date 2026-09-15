from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest

from .harness import DEVNET_PORTS, DevnetManager, _process_alive, is_port_open, wait_until


pytestmark = pytest.mark.core


@contextmanager
def _unrelated_process(devnet: DevnetManager):
    """Own a responsive process that records caught termination signals before exiting."""
    report = devnet.runner.cwd / "unrelated-process.json"
    env = {**devnet.runner.env, "STOP_SAFETY_REPORT": str(report)}
    # Keep artifact paths out of argv: incidental 'node' in a test directory name
    # must not make this Python process resemble a Node.js daemon.
    script = """
import json, os, signal, sys
from pathlib import Path
report = Path(os.environ['STOP_SAFETY_REPORT'])
received = []
def record(number, frame):
    received.append(number)
for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(number, record)
report.write_text(json.dumps({'request': 'ready', 'signals': received}))
for line in sys.stdin:
    report.write_text(json.dumps({'request': line.strip(), 'signals': received}))
"""
    process = subprocess.Popen(
        [sys.executable, "-u", "-c", script], cwd=devnet.runner.cwd, env=env,
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True,
    )
    sequence = 0

    def assert_untouched() -> None:
        nonlocal sequence
        assert process.poll() is None, "node stop terminated the unrelated test process"
        sequence += 1
        request = str(sequence)
        assert process.stdin is not None
        process.stdin.write(request + "\n")
        process.stdin.flush()

        def responded() -> bool:
            assert process.poll() is None, "node stop terminated the unrelated test process"
            try:
                return json.loads(report.read_text(encoding="utf-8")).get("request") == request
            except (OSError, json.JSONDecodeError):
                return False

        wait_until(responded, timeout_s=5, description="the unrelated process to answer after node stop")
        assert json.loads(report.read_text(encoding="utf-8"))["signals"] == []

    try:
        wait_until(lambda: report.exists(), timeout_s=5, description="the unrelated test process to be ready")
        assert_untouched()
        yield process, assert_untouched
    finally:
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _pid_metadata(devnet: DevnetManager, pid: int) -> bytes:
    return json.dumps({
        "pid": pid, "scriptPath": str(devnet.runner.cli_entry),
        "startedAt": datetime.now(timezone.utc).isoformat(), "status": "running",
    }).encode("utf-8")


def _assert_no_daemon(devnet: DevnetManager, reason: str) -> None:
    result = devnet.runner.run("node", "stop", timeout_s=15)
    assert result.json is not None
    assert result.json["stopped"] is False
    assert result.json["reason"] == reason
    assert not devnet.pid_file.exists()
    assert not any(is_port_open(port) for port in DEVNET_PORTS)


# TEST-MAP: NODE-05
def test_repeated_stop_and_stale_metadata_are_safe_and_idempotent(isolated_devnet: DevnetManager) -> None:
    """未启动、正常停止及已退出 PID 三种情况再次停止均安全成功，不影响无关进程。"""
    devnet = isolated_devnet
    with _unrelated_process(devnet) as (_, assert_untouched):
        _assert_no_daemon(devnet, "not-running")
        assert_untouched()
        devnet.start()
        devnet.stop()
        _assert_no_daemon(devnet, "not-running")
        assert_untouched()

        # A reaped test-owned child supplies a real expired PID, never a guessed
        # host PID. Confirm it remains absent immediately before invoking stop.
        exited = subprocess.Popen([sys.executable, "-c", "pass"], env=devnet.runner.env)
        assert exited.wait(timeout=5) == 0
        assert not _process_alive(exited.pid)
        devnet.pid_file.parent.mkdir(parents=True, exist_ok=True)
        devnet.pid_file.write_bytes(_pid_metadata(devnet, exited.pid))
        try:
            assert not _process_alive(exited.pid)
            _assert_no_daemon(devnet, "stale-pid")
            assert_untouched()
        finally:
            devnet.pid_file.unlink(missing_ok=True)


# TEST-MAP: NODE-10
def test_stop_refuses_live_unrelated_process_and_preserves_metadata(isolated_devnet: DevnetManager) -> None:
    """PID 指向仍存活的无关进程时拒绝停止，原元数据保留且目标没有收到终止信号。"""
    devnet = isolated_devnet
    with _unrelated_process(devnet) as (process, assert_untouched):
        devnet.pid_file.parent.mkdir(parents=True, exist_ok=True)
        original = _pid_metadata(devnet, process.pid)
        devnet.pid_file.write_bytes(original)
        try:
            result = devnet.runner.run("node", "stop", check=False, timeout_s=15)
            assert result.returncode != 0
            assert not result.stdout
            errors = [event for line in result.stderr.splitlines() if (event := json.loads(line)).get("ok") is False]
            assert len(errors) == 1 and errors[0].get("code"), result.stderr
            message = errors[0]["message"].lower()
            assert str(process.pid) in message
            assert any(word in message for word in ("unrelated", "identity", "does not appear", "another process"))
            assert devnet.pid_file.read_bytes() == original
            assert_untouched()
            assert not any(is_port_open(port) for port in DEVNET_PORTS)
        finally:
            # The injected file belongs to this test; remove it before fixture
            # teardown, which must never claim the unrelated process as a daemon.
            devnet.pid_file.unlink(missing_ok=True)
