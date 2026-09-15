from __future__ import annotations

import json

import pytest

from tests.harness import CKB, _command_references_path, _process_alive, wait_until
from .environment import FiberEnvironment


pytestmark = pytest.mark.fiber


def assert_default_environment_is_usable(env: FiberEnvironment) -> None:
    env.assert_ready_snapshot(env.startup_snapshot)
    commands = env.process_commands()
    for component in ("run", "miner"):
        assert any(
            _command_references_path(command, env.managed_ckb)
            and _command_references_path(command, env.config_path)
            and component in command.split()
            for command in commands.values()
        ), f"Default CKB {component} did not use the prepared managed binary"
    for node in (1, 2):
        node_path = env.config_path / "fiber" / "nodes" / str(node)
        assert any(
            _command_references_path(command, env.managed_fnn)
            and _command_references_path(command, node_path)
            for command in commands.values()
        ), f"FNN {node} did not use the prepared managed binary and its own data directory"
    for name in ("ckb.toml", "ckb-miner.toml", "specs/dev.toml"):
        assert (env.config_path / name).is_file(), f"OffCKB did not initialize {name}"
    env.rpc.wait_indexer()
    for account in env.accounts[3:5]:
        assert env.rpc.ckb_balance(account.lock_script) >= 10_000 * CKB, (
            f"Default Fiber account {account.index} is not prefunded"
        )
    before = env.rpc.tip()
    wait_until(
        lambda: env.rpc.tip() > before,
        timeout_s=30, description="the Fiber devnet to keep producing blocks",
    )


def assert_daemon_result(env: FiberEnvironment, command: str) -> None:
    result = env.start_result
    assert result is not None and result.returncode == 0
    assert json.loads(result.stdout) == result.json
    assert result.json is not None
    assert result.json["ok"] is True and result.json["command"] == command
    assert result.json["daemon"] is True
    assert _process_alive(int(result.json["pid"]))
    for line in result.stderr.splitlines():
        if line.strip():
            assert isinstance(json.loads(line), dict), "Progress must remain NDJSON on stderr"


# TEST-MAP: FIB-01
def test_default_foreground_fiber_environment(fiber_environment) -> None:
    """用户直接启动默认 Fiber 环境，获得可用链、账户及互联节点。"""
    with fiber_environment("combined-foreground") as env:
        assert env.foreground is not None and env.foreground.poll() is None
        output = env.foreground_stdout.read_text() + env.foreground_stderr.read_text()
        for client in (env.rpc, env.proxy_rpc, *env.fnn):
            assert client.url in output, f"Startup output did not provide {client.url}"
        assert_default_environment_is_usable(env)


# TEST-MAP: FIB-02
def test_combined_daemon_returns_only_after_fiber_is_ready(fiber_environment) -> None:
    """后台命令返回时 FNN 已互联，随后整组服务仍能继续使用。"""
    with fiber_environment("combined-daemon") as env:
        assert_daemon_result(env, "node")
        assert env.start_result.json["rpcUrl"] == env.rpc.url
        assert env.start_result.json["proxyUrl"] == env.proxy_rpc.url
        assert_default_environment_is_usable(env)


# TEST-MAP: FIB-03
@pytest.mark.parametrize("mode", ["attached-foreground", "attached-daemon"])
def test_add_fiber_to_existing_chain_without_replacing_it(fiber_environment, mode: str) -> None:
    """在原开发链上增加 Fiber，保留进程和已完成的交易。"""
    with fiber_environment(mode) as env:
        before = env.attached_snapshot
        assert before is not None
        assert env.rpc.call("get_block_hash", ["0x0"]) == before["genesis"]
        transaction = env.rpc.call("get_transaction", [before["tx_hash"]])
        assert transaction["tx_status"]["status"] == "committed"
        assert transaction["transaction"] == before["transaction"]["transaction"]
        current = env.process_commands()
        for pid, command in before["commands"].items():
            assert _process_alive(pid) and current.get(pid) == command, (
                f"Adding Fiber replaced existing CKB component {pid}"
            )
        if mode.endswith("daemon"):
            assert_daemon_result(env, "fiber.start")
        else:
            assert env.foreground is not None and env.foreground.poll() is None
        assert_default_environment_is_usable(env)
