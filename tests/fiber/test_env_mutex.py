"""并发操作与环境互斥：第二个进程被拒、残留锁回收、数据目录被占用时拒绝清理。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.harness import CommandResult, is_port_open, wait_until

from .flows import opened_ckb_channel, pay_invoice
from .support_configuration import assert_failure, nodes_path, result_json
from .support_mutex import (
    assert_rejected_second_command, env_lock_path, exited_process_id, hold_store_lock,
    start_background, store_lock_files, wait_for_environment_reservation,
    write_residual_lock,
)

pytestmark = pytest.mark.fiber

START_PURPOSE = "offckb fiber start"


def _lock_record(path: Path) -> dict | None:
    """The environment record a command in progress leaves behind, if any."""
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _lock_owner(path: Path) -> int | None:
    record = _lock_record(path)
    try:
        return int(record["pid"]) if record else None
    except (KeyError, TypeError, ValueError):
        return None


def _await_runner(env, command, timeout_s: float = 600.0):
    """Wait for one background OffCKB command and describe its result like the shared runner does."""
    code = command.wait(timeout_s=timeout_s)
    stdout = command.stdout_path.read_text()
    stderr = command.stderr_path.read_text()
    assert code == 0, f"the running command failed ({code}):\n{env.runner._redact_text(stdout + stderr)}"
    return CommandResult(command.argv, env.runner.cwd, code, stdout, stderr, 0.0, None)


# TEST-MAP: FIB-LOCK-01
def test_concurrent_environment_change_is_rejected_without_harming_the_running_command(fiber_environment) -> None:
    """第二个终端修改同一环境时被拒绝，先发起的启动不受影响。"""
    with fiber_environment("attached-daemon") as env:
        env.stop_fiber()
        processes_before = env.process_commands()
        nodes_before = nodes_path(env).read_bytes()
        fnn_ports = (21714, 21715, 8344, 8345)
        assert not any(is_port_open(port) for port in fnn_ports), "the case needs an idle Fiber environment"
        assert env.rpc.ready(), "the attached case keeps the original CKB chain running"

        # The daemon launcher reserves the environment for the whole startup. The second
        # terminal acts only while that reservation is confirmed, so the race is deterministic.
        manager = start_background(env, "--json", "fiber", "start", "--daemon")
        try:
            wait_for_environment_reservation(env, env.startup_timeout_s)

            concurrent = start_background(env, "--json", "fiber", "clean", "--yes")
            assert_rejected_second_command(env, concurrent, START_PURPOSE)
            # The rejected command must not have added processes of its own: at this moment the
            # environment still holds exactly the state captured before the first command ran,
            # because the running start has not brought its FNNs up yet.
            assert env.process_commands() == processes_before, "the rejected command changed the process set"
            assert nodes_path(env).read_bytes() == nodes_before, "the rejected command rewrote the node list"

            # The command in progress must still finish its work and be usable.
            wait_until(lambda: env._snapshot_ready(env._public_snapshot()), timeout_s=env.startup_timeout_s,
                       description="the running Fiber start to connect its nodes")
            env.assert_ready_snapshot(env._public_snapshot())
            started = result_json(_await_runner(env, manager), "fiber.start")
            assert started["daemon"] is True
            with opened_ckb_channel(env) as channel:
                pay_invoice(env, channel)

            # Once the environment is stopped and free, the same command can proceed.
            env.stop_fiber()
            wait_until(lambda: not env_lock_path(env).exists(), timeout_s=30,
                       description="the environment to become free again")
            assert env.rpc.ready(), "the original CKB chain must survive the interrupted Fiber start"
            retry = result_json(env.runner.run("fiber", "start", "--daemon"), "fiber.start")
            assert retry["daemon"] is True
            env.assert_ready_snapshot(env.public_snapshot())
            env.stop_fiber()
        finally:
            if manager.process.poll() is None:
                manager.interrupt()


# TEST-MAP: FIB-LOCK-02
def test_residual_lock_from_a_dead_process_is_reclaimed(fiber_environment) -> None:
    """上一次中断留下的、持有者已退出的锁会被回收，启动继续。"""
    with fiber_environment("combined-daemon", start=False) as env:
        dead_pid = exited_process_id()
        lock = write_residual_lock(env, dead_pid)
        assert _lock_owner(lock) == dead_pid

        result = env.runner.run("node", "--fiber", "--daemon")
        started = result_json(result, "node")
        assert started["daemon"] is True and int(started["pid"]) > 0
        env.assert_ready_snapshot(env.public_snapshot())

        # The dead process must not be reported as an environment owner afterwards.
        assert _lock_owner(lock) != dead_pid, "a dead process must not still own the environment"
        with opened_ckb_channel(env) as channel:
            pay_invoice(env, channel)


# TEST-MAP: FIB-LOCK-03
def test_clean_refuses_while_a_live_process_holds_the_store_lock(fiber_environment) -> None:
    """数据目录仍被存活进程持有时拒绝清理，释放后同一命令可完成。"""
    with fiber_environment("combined-daemon") as env:
        locks = store_lock_files(env)
        assert all(path.is_file() for path in locks), "a started FNN must have created its store lock"
        env.stop_fiber()
        with hold_store_lock(env, locks[0]):
            assert_failure(
                env, ("fiber", "clean", "--data", "--yes"),
                r"cannot confirm all fiber stores are closed", r"lock\(s\) still held", r"stop every fnn",
            )
        result = env.runner.run("fiber", "clean", "--data", "--yes")
        cleaned = result_json(result, "fiber.clean")
        assert cleaned["cleaned"] is True
