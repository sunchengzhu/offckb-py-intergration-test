"""停止、继续同一通道实验，以及四种清理范围。所有状态来自真实 CLI/RPC。"""

from __future__ import annotations

import hashlib
import os
import signal
from pathlib import Path

import pytest

from tests.harness import (
    DEVNET_PORTS, _command_references_path, _process_alive, _process_command, is_port_open, wait_until,
)
from .environment import ALL_PORTS, FIBER_MODES, FNN_PORTS, FiberEnvironment
from .flows import (
    assert_close_amounts, channel_pair, close_channel, list_channels,
    open_ckb_channel, pay_invoice, settled,
)
from .support_configuration import (
    assert_chain_advances, assert_failure, assert_processes_unchanged, messages, result_json,
)


pytestmark = pytest.mark.fiber


def digest_files(paths) -> dict[Path, str]:
    # Only hashes enter assertions; encrypted keys and passwords never enter logs.
    return {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def stable_files(env: FiberEnvironment) -> dict[Path, str]:
    paths = [env.config_path / "fiber" / "nodes.yml", env.config_path / "ckb.toml",
             env.config_path / "ckb-miner.toml",
             *sorted(p for p in (env.config_path / "specs").rglob("*") if p.is_file())]
    for node in (1, 2):
        directory = env.config_path / "fiber" / "nodes" / str(node)
        paths.extend(directory / name for name in ("config.yml", "password", "ckb/key", "fiber/sk"))
    assert all(path.is_file() for path in paths), "configuration and identity files must exist"
    return digest_files(paths)


def assert_files_preserved(before: dict[Path, str]) -> None:
    assert all(path.is_file() for path in before), "a preserved file was deleted"
    assert digest_files(before) == before


def stores(env: FiberEnvironment) -> tuple[Path, Path]:
    return tuple(env.config_path / "fiber" / "nodes" / str(i) / "fiber" / "store" for i in (1, 2))


def assert_data_preserved(env: FiberEnvironment, before: dict[Path, str]) -> None:
    assert_files_preserved(before)
    database = env.config_path / "data" / "db"
    assert database.is_dir() and any(database.iterdir()), "the CKB database must be retained"
    assert all(path.is_dir() and any(path.iterdir()) for path in stores(env))


def identities(env: FiberEnvironment) -> list[dict]:
    return [{key: info[key] for key in ("pubkey", "default_funding_lock_script", "chain_hash")}
            for info in (rpc.call("node_info") for rpc in env.fnn)]


def assert_all_stopped(env: FiberEnvironment, before: dict[int, str]) -> None:
    wait_until(lambda: env._stopped(), timeout_s=20, description="all owned managers, children and ports to stop")
    assert before and all(not _process_alive(pid) for pid in before)
    assert not any(is_port_open(port) for port in ALL_PORTS)
    assert not env.runtime_path.exists() and not any(path.exists() for path in env.pid_files)


def paid_channel(env: FiberEnvironment):
    channel = open_ckb_channel(env)
    pay_invoice(env, channel)
    return channel


def closed_experiment(env: FiberEnvironment):
    channel = paid_channel(env)
    closing = close_channel(env, channel)
    assert_close_amounts(env.rpc, channel, closing, (env.accounts[3], env.accounts[4]))
    assert all(not list_channels(rpc) for rpc in env.fnn)
    return channel, closing["transaction"]["hash"]


# TEST-MAP: FIB-LIFE-11
@pytest.mark.parametrize("mode", ["combined-foreground", "attached-foreground"])
def test_unexpected_fnn_exit_respects_manager_scope_and_can_restart(fiber_environment, mode):
    """单个 FNN 意外退出后按管理范围回收，原目录仍可重新实验。"""
    with fiber_environment(mode) as env:
        assert all(not list_channels(rpc) for rpc in env.fnn)
        identity, files, processes = identities(env), stable_files(env), env.process_commands()
        node_path = env.config_path / "fiber" / "nodes" / "1"
        candidates = [pid for pid, command in processes.items()
                      if _command_references_path(command, env.managed_fnn)
                      and _command_references_path(command, node_path)]
        assert len(candidates) == 1, "expected exactly one FNN belonging to this fixture's node 1"
        pid = candidates[0]
        assert _process_alive(pid) and _process_command(pid) == processes[pid]
        os.kill(pid, signal.SIGKILL)
        assert env.foreground is not None
        wait_until(lambda: env.foreground.poll() is not None, timeout_s=20,
                   description="the manager to report the unexpected FNN exit")
        assert env.foreground.returncode != 0, "the manager reported a successful exit after losing an FNN"
        diagnostic = env.foreground_stdout.read_text() + env.foreground_stderr.read_text()
        assert "FNN node 1 exited unexpectedly" in diagnostic, env.runner._redact_text(diagnostic)

        if mode.startswith("combined"):
            assert_all_stopped(env, processes)
        else:
            chain = env.attached_snapshot
            assert chain is not None
            fnn_pids = set(processes) - set(chain["commands"])
            assert fnn_pids
            wait_until(lambda: all(not _process_alive(pid) for pid in fnn_pids)
                       and not any(is_port_open(port) for port in FNN_PORTS), timeout_s=20,
                       description="the attached manager and all its FNNs to stop")
            assert not env.runtime_path.exists() and not env.pid_files[1].exists()
            assert_processes_unchanged(env, chain["commands"])
            assert_chain_advances(env)
            assert env.rpc.call("get_transaction", [chain["tx_hash"]]) == chain["transaction"]
        assert_data_preserved(env, files)

        env.start()
        assert identities(env) == identity
        assert_files_preserved(files)
        paid_channel(env)


# TEST-MAP: FIB-LIFE-01
def test_ctrl_c_stops_combined_environment_without_erasing_data(fiber_environment):
    with fiber_environment("combined-foreground") as env:
        paid_channel(env)
        files, processes = stable_files(env), env.process_commands()
        env.stop_fiber()
        assert_all_stopped(env, processes)
        assert_data_preserved(env, files)


# TEST-MAP: FIB-LIFE-02
@pytest.mark.parametrize("command", [("node", "stop"), ("fiber", "stop")], ids=["node-stop", "fiber-stop"])
def test_either_daemon_stop_entry_stops_combined_environment(fiber_environment, command):
    with fiber_environment("combined-daemon") as env:
        paid_channel(env)
        files, processes = stable_files(env), env.process_commands()
        result = env.runner.run(*command)
        data = result_json(result, ".".join(command))
        assert data["stopped"] is True
        if command[0] == "fiber":
            assert data["includedCkb"] is True
            assert "CKB" in messages(result)
        assert_all_stopped(env, processes)
        assert_data_preserved(env, files)


# TEST-MAP: FIB-LIFE-03
@pytest.mark.parametrize("mode", ["attached-foreground", "attached-daemon"])
def test_stopping_attached_fiber_preserves_the_original_chain(fiber_environment, mode):
    with fiber_environment(mode) as env:
        chain = env.attached_snapshot
        before = env.process_commands()
        if mode.endswith("foreground"):
            result = env.runner.run("fiber", "stop")
            data = result_json(result, "fiber.stop")
            assert data["stopped"] is False and data["reason"] == "foreground-manager"
            assert "Ctrl+C" in messages(result)
            assert_processes_unchanged(env, before)
            env.assert_ready_snapshot(env.public_snapshot())
        env.stop_fiber()
        assert chain is not None
        assert_processes_unchanged(env, chain["commands"])
        fnn_pids = set(before) - set(chain["commands"])
        assert fnn_pids and all(not _process_alive(pid) for pid in fnn_pids)
        assert_chain_advances(env)
        assert env.rpc.call("get_transaction", [chain["tx_hash"]])["tx_status"]["status"] == "committed"


# TEST-MAP: FIB-LIFE-04
@pytest.mark.parametrize("mode", ["combined-daemon", "attached-daemon"])
def test_restart_restores_the_same_settled_channel_and_can_pay_again(fiber_environment, mode):
    with fiber_environment(mode) as env:
        channel = paid_channel(env)
        before = channel_pair(env, channel.channel_id)
        identity, files = identities(env), stable_files(env)
        env.stop_fiber()
        assert_data_preserved(env, files)
        env.start()
        assert identities(env) == identity
        def restored():
            pair = channel_pair(env, channel.channel_id)
            return pair is not None and all(c["state"]["state_name"] == "ChannelReady" and settled(c) for c in pair)
        wait_until(restored, timeout_s=env.timeout_s, description="original channel to restore ChannelReady")
        after = channel_pair(env, channel.channel_id)
        assert [[c[key] for key in ("channel_id", "local_balance", "remote_balance", "channel_outpoint")]
                for c in after] == [[c[key] for key in ("channel_id", "local_balance", "remote_balance", "channel_outpoint")]
                                    for c in before]
        pay_invoice(env, channel)
        close_channel(env, channel)


# TEST-MAP: FIB-LIFE-05
@pytest.mark.parametrize("mode", ["attached-foreground", "attached-daemon"])
def test_node_stop_requires_force_when_fiber_has_another_manager(fiber_environment, mode):
    with fiber_environment(mode) as env:
        channel = paid_channel(env)
        files, before = stable_files(env), env.process_commands()
        assert_failure(env, ("node", "stop"), "(stop.*first|Ctrl\\+C)", "--force")
        assert_processes_unchanged(env, before)
        pay_invoice(env, channel)
        result = env.runner.run("node", "stop", "--force")
        assert result_json(result, "node.stop")["stopped"] is True
        assert "keep running on a stopped chain" in messages(result)
        chain = env.attached_snapshot
        wait_until(lambda: not any(is_port_open(p) for p in DEVNET_PORTS), timeout_s=20,
                   description="forced CKB stop to release only CKB ports")
        assert all(not _process_alive(pid) for pid in chain["commands"])
        assert_processes_unchanged(env, {pid: cmd for pid, cmd in before.items() if pid not in chain["commands"]})
        assert_data_preserved(env, files)
        assert all(rpc.call("node_info")["version"] == "0.9.0" for rpc in env.fnn)


def reset_experiment(fiber_environment, *, fiber_only: bool, data_only: bool):
    mode = "attached-daemon" if fiber_only else "combined-daemon"
    with fiber_environment(mode) as env:
        channel, old_tx = closed_experiment(env)
        identity, files = identities(env), stable_files(env)
        old_genesis = env.rpc.call("get_block_hash", ["0x0"])
        tools = digest_files([env.managed_ckb, env.managed_fnn,
                              env.managed_fnn.parent / "config" / "testnet" / "config.yml"])
        env.stop_fiber() if fiber_only else env.stop()
        # Stable log bytes are observable only after the services have stopped.
        log_files = digest_files(env.node_logs)
        command = ("fiber", "clean", *(('--data',) if data_only else ()), "--yes") if fiber_only else (
            "clean", *(("-d",) if data_only else ()))
        env.runner.run(*command)
        assert_files_preserved(tools)
        if data_only:
            assert_files_preserved(files)
            assert all(not path.exists() for path in stores(env))
            if fiber_only:
                assert_files_preserved(log_files)
            else:
                assert not (env.config_path / "data").exists()
        else:
            assert not (env.config_path / "fiber" if fiber_only else env.config_path).exists()
        if fiber_only:
            assert_processes_unchanged(env, env.attached_snapshot["commands"])
            assert_chain_advances(env)
            assert env.rpc.call("get_transaction", [old_tx])["tx_status"]["status"] == "committed"
            assert_files_preserved({p: h for p, h in files.items() if "fiber" not in p.relative_to(env.config_path).parts})
        env.start()
        after = identities(env)
        if data_only:
            assert after == identity
        else:
            assert all(a["pubkey"] != b["pubkey"] for a, b in zip(after, identity))
            assert [i["default_funding_lock_script"] for i in after] == [i["default_funding_lock_script"] for i in identity]
        assert env.rpc.call("get_block_hash", ["0x0"]) == old_genesis
        assert all(not list_channels(rpc, include_closed=True) for rpc in env.fnn)
        if not fiber_only:
            missing = env.rpc.call("get_transaction", [old_tx])
            assert missing is None or (missing["transaction"] is None and missing["tx_status"]["status"] == "unknown"), (
                "the previous experimental transaction survived chain reset")
        assert channel.last_payment_hash is not None
        with pytest.raises(RuntimeError, match="(?i)(payment.*not found|not found.*payment)"):
            env.fnn[0].call("get_payment", [{"payment_hash": channel.last_payment_hash}])
        new_channel = paid_channel(env)
        close_channel(env, new_channel)


# TEST-MAP: FIB-LIFE-06
def test_fiber_data_clean_preserves_chain_identity_configuration_and_logs(fiber_environment):
    reset_experiment(fiber_environment, fiber_only=True, data_only=True)


# TEST-MAP: FIB-LIFE-07
def test_fiber_full_clean_recreates_identity_without_resetting_ckb(fiber_environment):
    reset_experiment(fiber_environment, fiber_only=True, data_only=False)


# TEST-MAP: FIB-LIFE-08
def test_devnet_data_clean_resets_chain_and_fiber_stores_together(fiber_environment):
    reset_experiment(fiber_environment, fiber_only=False, data_only=True)


# TEST-MAP: FIB-LIFE-09
def test_full_clean_recreates_the_environment_and_keeps_downloaded_tools(fiber_environment):
    reset_experiment(fiber_environment, fiber_only=False, data_only=False)


# TEST-MAP: FIB-LIFE-10
@pytest.mark.parametrize("mode", FIBER_MODES)
@pytest.mark.parametrize("command", [("fiber", "clean", "--data", "--yes"), ("fiber", "clean", "--yes"),
                                      ("clean", "-d"), ("clean",)], ids=["fiber-data", "fiber-all", "chain-data", "all"])
def test_clean_refuses_a_live_environment_and_allows_clean_after_stop(fiber_environment, mode, command):
    with fiber_environment(mode) as env:
        channel = paid_channel(env)
        files, processes, identity = stable_files(env), env.process_commands(), identities(env)
        assert_failure(env, command, "stop|Ctrl\\+C")
        assert_data_preserved(env, files)
        assert_processes_unchanged(env, processes)
        assert identities(env) == identity
        pay_invoice(env, channel)
        closing = close_channel(env, channel)
        assert_close_amounts(env.rpc, channel, closing, (env.accounts[3], env.accounts[4]))
        env.stop()
        env.runner.run(*command)
        if command[0] == "fiber":
            if "--data" in command:
                assert all(not path.exists() for path in stores(env))
                assert_files_preserved(files)
            else:
                assert not (env.config_path / "fiber").exists()
        elif "-d" in command:
            assert not (env.config_path / "data").exists()
            assert all(not path.exists() for path in stores(env))
        else:
            assert not env.config_path.exists()
