from __future__ import annotations

import json
import hashlib
import re
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from tests.harness import DEVNET_PORTS, _process_alive, is_port_open, transaction_hash_from, wait_until
from .environment import FiberEnvironment
from .flows import _key_file, channel_pair, open_ckb_channel, opened_ckb_channel, pay_invoice, settled
from .support_configuration import (
    assert_chain_advances, assert_failure, assert_failed_start_is_clean,
    assert_processes_unchanged, events, nodes_path, occupied_port, paused_miner,
)
from .support_mutex import env_lock_path, start_background
from .installation_support import file_manifest
from .test_lifecycle import assert_data_preserved, identities, stable_files, stores


pytestmark = pytest.mark.fiber


def start_args(mode: str) -> tuple[str, ...]:
    return ("node", "--fiber", "--daemon") if mode.startswith("combined") else ("fiber", "start", "--daemon")


def prepare_existing_chain(env) -> None:
    if env.mode.startswith("attached"):
        env.start_existing_chain()


def assert_recovered(env) -> None:
    env.assert_ready_snapshot(env.public_snapshot())
    assert_chain_advances(env)
    if env.attached_snapshot is not None:
        proof = env.attached_snapshot
        assert_processes_unchanged(env, proof["commands"])
        assert env.rpc.call("get_block_hash", ["0x0"]) == proof["genesis"]
        tx = env.rpc.call("get_transaction", [proof["tx_hash"]])
        assert tx["tx_status"]["status"] == "committed"
        assert tx["transaction"] == proof["transaction"]["transaction"]


# TEST-MAP: FIB-ERR-10
@pytest.mark.parametrize("mode", ["combined-daemon", "attached-daemon"])
@pytest.mark.parametrize("material", ["ckb/key", "password"], ids=["missing-key", "missing-password"])
def test_incomplete_key_material_is_preserved_and_original_files_restore_startup(
    fiber_environment, mode: str, material: str,
) -> None:
    """只缺配对材料中的一个文件时拒绝重建，恢复原文件即可继续。"""
    with fiber_environment(mode) as env:
        identity = identities(env)
        env.stop_fiber()
        assert_failed_start_is_clean(env, attached=mode.startswith("attached"))
        files = stable_files(env)
        store_manifests = {path: file_manifest(path) for path in stores(env)}
        assert all(store_manifests.values()), "the original FNN stores must contain data"
        missing = env.config_path / "fiber" / "nodes" / "1" / material
        backup = env.root / "secrets" / f"node-1-{material.replace('/', '-')}.original"
        missing.rename(backup)
        backup.chmod(0o600)
        try:
            assert_failure(env, start_args(mode), r"node 1", r"incomplete key material",
                           re.escape(str(missing)), r"missing", r"restore")
            assert not missing.exists(), "the missing key/password was silently re-created"
            assert_failed_start_is_clean(env, attached=mode.startswith("attached"))
            assert_data_preserved(env, {path: digest for path, digest in files.items() if path != missing})
            assert {path: file_manifest(path) for path in stores(env)} == store_manifests, (
                "failed startup changed the stopped FNN stores"
            )
        finally:
            # Original material remains private and is restored even if an assertion fails.
            backup.replace(missing)
        assert_data_preserved(env, files)
        env.start(start_ckb=not mode.startswith("attached"))
        assert identities(env) == identity
        assert_recovered(env)


# TEST-MAP: FIB-ERR-01
@pytest.mark.parametrize("mode", ["combined-daemon", "attached-daemon"])
@pytest.mark.parametrize("fault", ["missing", "not-executable"])
def test_bad_fnn_path_can_be_corrected_without_resetting_environment(fiber_environment, mode: str, fault: str) -> None:
    """错误 FNN 路径不留下半套服务，修正路径即可继续。"""
    with fiber_environment(mode, start=False) as env:
        prepare_existing_chain(env)
        binary = env.root / f"{fault}-fnn"
        if fault == "not-executable":
            binary.write_text("This isolated file intentionally has no executable permission.\n")
            binary.chmod(0o600)
        option = "--fnn-binary-path" if mode.startswith("combined") else "--binary-path"
        assert_failure(env, (*start_args(mode), option, str(binary)), re.escape(str(binary)), r"not found|EACCES|permission|exited")
        assert_failed_start_is_clean(env, attached=mode.startswith("attached"))
        env.start(extra_args=(option, str(env.managed_fnn)), start_ckb=not mode.startswith("attached"))
        assert_recovered(env)


# TEST-MAP: FIB-ERR-02
@pytest.mark.parametrize("mode", ["combined-daemon", "attached-daemon"])
@pytest.mark.parametrize("port", [21714, 8344], ids=["rpc-port", "p2p-port"])
def test_port_conflict_preserves_its_owner_and_retry_succeeds(fiber_environment, mode: str, port: int) -> None:
    """冲突端口的进程不被终止，释放后原环境可重试。"""
    with fiber_environment(mode, start=False) as env:
        prepare_existing_chain(env)
        with occupied_port(env, port) as owner:
            assert_failure(env, start_args(mode), r"node 1", str(port), r"conflict|already in use")
            assert owner.poll() is None and is_port_open(port)
            assert_failed_start_is_clean(env, attached=mode.startswith("attached"), reserved_port=port)
        env.start(start_ckb=not mode.startswith("attached"))
        assert_recovered(env)


# TEST-MAP: FIB-ERR-03
def test_duplicate_start_preserves_live_channel_and_guides_correct_entry(fiber_environment) -> None:
    """重复启动不替换已有进程，原通道仍能支付。"""
    with fiber_environment("attached-daemon") as env:
        with opened_ckb_channel(env) as channel:
            before = env.process_commands()
            identities = [client.call("node_info")["pubkey"] for client in env.fnn]
            runtime = env.runtime_path.read_bytes()
            original_nodes = nodes_path(env).read_bytes()
            for args in (("fiber", "start"), ("fiber", "start", "--daemon")):
                assert_failure(env, args, r"already", r"stop.*fiber|fiber stop")
                assert_processes_unchanged(env, before)
                assert env.runtime_path.read_bytes() == runtime
                assert nodes_path(env).read_bytes() == original_nodes
                assert [client.call("node_info")["pubkey"] for client in env.fnn] == identities
                pay_invoice(env, channel)
            assert_failure(env, ("node", "--fiber"), r"CKB.*already", r"offckb fiber start")
            assert_processes_unchanged(env, before)
            pay_invoice(env, channel)
            env.stop_fiber()
            env.start(start_ckb=False)
            assert [client.call("node_info")["pubkey"] for client in env.fnn] == identities
            def channel_restored() -> bool:
                pair = channel_pair(env, channel.channel_id)
                return pair is not None and all(node["state"]["state_name"] == "ChannelReady" and settled(node)
                                                for node in pair)
            wait_until(channel_restored, timeout_s=env.timeout_s,
                       description="the existing channel to be ready after the guided restart")
            pay_invoice(env, channel)
            assert_recovered(env)


# TEST-MAP: FIB-ERR-04
@pytest.mark.parametrize("condition", ["no-ckb", "miner-paused"])
def test_missing_or_nonmining_ckb_is_actionable_and_recovers(fiber_environment, condition: str) -> None:
    """缺少链或产块时明确拒绝，修复原链后添加 Fiber。"""
    with fiber_environment("attached-daemon", start=False) as env:
        if condition == "no-ckb":
            assert_failure(env, ("fiber", "start"), r"CKB.*not answering", r"offckb node")
            assert not any(is_port_open(port) for port in (*DEVNET_PORTS, 21714, 21715, 8344, 8345))
            assert not (env.config_path / "ckb.toml").exists()
            assert not env.runtime_path.exists()
            env.start_existing_chain()
        else:
            env.start_existing_chain()
            before = env.process_commands()
            with paused_miner(env) as miner:
                assert_failure(env, ("fiber", "start"), r"not producing blocks", r"miner", r"offckb logs")
                assert _process_alive(miner)
                assert_processes_unchanged(env, before)
                assert not env.runtime_path.exists()
                assert not any(is_port_open(port) for port in (21714, 21715, 8344, 8345))
        assert_chain_advances(env)
        env.start(start_ckb=False)
        assert_recovered(env)


# TEST-MAP: FIB-ERR-05
def test_empty_funding_account_can_be_refunded_and_open_channel(fiber_environment) -> None:
    """资金耗尽时启动明确失败，充值上链后可开通真实通道。"""
    with fiber_environment("attached-daemon", start=False) as env:
        env.start_existing_chain()
        account, destination = env.accounts[3], env.accounts[8]
        env.rpc.wait_indexer()
        assert env.rpc.ckb_balance(account.lock_script) > 0
        drained = env.runner.run("transfer-all", destination.address, "--network", "devnet",
                                 "--privkey-file", _key_file(env, account))
        transaction = env.rpc.wait_transaction(transaction_hash_from(drained), timeout_s=env.timeout_s)
        env.rpc.wait_indexer(transaction["tx_status"]["block_number"], timeout_s=env.timeout_s)
        assert env.rpc.ckb_balance(account.lock_script) == 0
        assert env.rpc.live_cells(account.lock_script) == []
        assert_failure(env, ("fiber", "start"), r"node 1", r"account #3", r"no available CKB", r"Fund the account")
        assert_failed_start_is_clean(env, attached=True)
        credited = env.runner.run("transfer", account.address, "20000", "--network", "devnet",
                                  "--privkey-file", _key_file(env, destination))
        refill = env.rpc.wait_transaction(transaction_hash_from(credited), timeout_s=env.timeout_s)
        env.rpc.wait_indexer(refill["tx_status"]["block_number"], timeout_s=env.timeout_s)
        env.start(start_ckb=False)
        assert_recovered(env)
        with opened_ckb_channel(env) as channel:
            assert channel.funding_transaction["tx_status"]["status"] == "committed"


# TEST-MAP: FIB-ERR-06
@pytest.mark.parametrize("unsupported", ["testnet", "mainnet", "fork"])
def test_nonlocal_or_forked_chain_is_preserved_and_new_local_environment_works(fiber_environment, unsupported: str) -> None:
    """公共网络与 fork 入口被拒绝，保留原数据并另建普通链。"""
    with fiber_environment("combined-daemon", start=False) as env:
        if unsupported == "fork":
            # This is only the documented local guard; no public-chain database is downloaded.
            env.config_path.mkdir(parents=True, exist_ok=True)
            marker = env.config_path / "fork.json"
            marker.write_text(json.dumps({"source": "testnet", "firstRunPending": True}) + "\n")
            data = env.config_path / "data" / "fork-preserved.bin"
            data.parent.mkdir(parents=True)
            data.write_bytes(b"isolated fork data must remain byte-for-byte unchanged\n")
            originals = {path: path.read_bytes() for path in (marker, data)}
            fork_files = {str(path.relative_to(env.config_path)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in env.config_path.rglob("*") if path.is_file()}
            entries = [("node", "--fiber"), ("fiber", "start")]
            pattern = r"forked devnet|plain local chain"
        else:
            originals = {}
            entries = [("node", "--fiber", "--network", unsupported)]
            pattern = r"plain local devnet"
        for entry in entries:
            for daemon in ((), ("--daemon",)):
                assert_failure(env, (*entry, *daemon), pattern)
                assert not any(is_port_open(port) for port in (*DEVNET_PORTS, 21714, 21715, 8344, 8345))
                assert not env.runtime_path.exists()
                assert not nodes_path(env).exists()
                assert all(path.read_bytes() == original for path, original in originals.items())
                if unsupported == "fork":
                    assert {str(path.relative_to(env.config_path)): hashlib.sha256(path.read_bytes()).hexdigest()
                            for path in env.config_path.rglob("*") if path.is_file()} == fork_files
        preserved_root = env.config_path
    # A separate HOME follows the guidance; never erase a fork marker to relabel that chain.
    with fiber_environment("combined-daemon") as recovered:
        assert recovered.config_path != preserved_root
        assert_recovered(recovered)


# TEST-MAP: FIB-ERR-07
@pytest.mark.parametrize("mode", ["combined-daemon", "attached-daemon"])
def test_invalid_node_count_preserves_configuration_and_valid_retry_works(fiber_environment, mode: str) -> None:
    """错误节点数量在参数校验时失败，修正后保留原节点配置。"""
    with fiber_environment(mode) as env:
        identities = [client.call("node_info")["pubkey"] for client in env.fnn]
        if mode.startswith("combined"):
            env.stop()
        else:
            env.stop_fiber()
        original = nodes_path(env).read_bytes()
        option = "--fiber-nodes" if mode.startswith("combined") else "--nodes"
        for invalid in ("0", "17", "1.5"):
            result = assert_failure(env, (*start_args(mode), option, invalid), r"integer between 1 and 16")
            assert "argument" in result.stderr.lower() or "invalid" in result.stderr.lower()
            assert nodes_path(env).read_bytes() == original
            assert_failed_start_is_clean(env, attached=mode.startswith("attached"))
        env.start(extra_args=(option, "2"), start_ckb=mode.startswith("combined"))
        assert [client.call("node_info")["pubkey"] for client in env.fnn] == identities
        assert_recovered(env)


FNN_PORTS = (21714, 21715, 8344, 8345)


def _pid_record(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _await_background(env: FiberEnvironment, command) -> dict:
    """Wait for one background command to finish and return its JSON result."""
    code = command.wait(timeout_s=env.startup_timeout_s)
    text = command.output()
    assert code == 0, f"the running command failed ({code}):\n{env.runner._redact_text(text)}"
    result = json.loads(command.stdout_path.read_text(encoding="utf-8"))
    assert result.get("ok") is True, env.runner._redact_text(text)
    return result


def _snapshot_node_list(env: FiberEnvironment) -> bytes | None:
    """The node list does not exist until a start writes it; snapshot its current state either way."""
    path = nodes_path(env)
    return path.read_bytes() if path.exists() else None


def _stop_daemon_start(env: FiberEnvironment, command) -> None:
    """Stop a background daemon start without orphaning a half-written PID record.

    The launcher owns the readiness handshake: it flips the record to `running` once the
    detached manager reports ready. Interrupting it earlier leaves that manager running under a
    record that stays `starting`, which later commands refuse to touch, so only give up on a
    launcher that exceeds the startup budget.
    """
    if command.process.poll() is None:
        try:
            command.process.wait(timeout=env.startup_timeout_s)
        except subprocess.TimeoutExpired:
            command.interrupt()
    if env.pid_files[1].exists() or env.runtime_path.exists():
        env.stop_fiber()


def _ports_in_use() -> list[int]:
    return [port for port in FNN_PORTS if is_port_open(port)]


def _assert_no_fnn_started(env: FiberEnvironment) -> None:
    assert not any(is_port_open(port) for port in FNN_PORTS), "a refused start occupied a Fiber port"
    assert not env.runtime_path.exists(), "a refused start left a runtime record"


def _await_channel_ready(env: FiberEnvironment, channel) -> None:
    def ready() -> bool:
        pair = channel_pair(env, channel.channel_id)
        return pair is not None and all(node["state"]["state_name"] == "ChannelReady" and settled(node)
                                        for node in pair)
    wait_until(ready, timeout_s=env.timeout_s, description="the existing channel to be ready again")


# TEST-MAP: FIB-ERR-08
def test_pid_record_pointing_at_an_unrelated_process_is_never_signalled(fiber_environment) -> None:
    """PID 记录指向存活但身份不符的进程：拒绝替换元数据、不发信号，处理后无需清空环境。

    产品以「refuses to replace live pid metadata whose identity cannot be verified」锁定了这条
    fail-closed 行为（tests/fiber-lifecycle.test.ts），这里按同一判定固定。
    """
    with fiber_environment("attached-daemon", start=False) as env:
        prepare_existing_chain(env)
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            cwd=env.runner.cwd, env=env.runner.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            assert unrelated.poll() is None
            env.pid_files[1].parent.mkdir(parents=True, exist_ok=True)
            record = json.dumps({
                "pid": unrelated.pid, "scriptPath": str(env.runner.cli_entry),
                "startedAt": datetime.now(timezone.utc).isoformat(), "status": "running",
            }).encode("utf-8")
            env.pid_files[1].write_bytes(record)
            result = env.runner.run("fiber", "start", "--daemon", check=False, timeout_s=env.startup_timeout_s)
            assert result.returncode != 0, "a live foreign PID record did not stop the startup"
            assert unrelated.poll() is None, "the command signalled the unrelated process"
            assert not result.stdout.strip(), "a refused start wrote a success result to stdout"
            failures = [event for event in events(result.stderr) if event.get("ok") is False]
            assert len(failures) == 1 and failures[0].get("code"), result.stderr
            message = failures[0]["message"].lower()
            assert str(unrelated.pid) in message, message
            assert any(word in message for word in
                       ("does not look like", "refusing", "not appear", "another process")), message
            assert str(env.pid_files[1]).lower() in message, "the refusal must name the record to clean up"
            assert env.pid_files[1].read_bytes() == record, "the refused start replaced foreign metadata"
            _assert_no_fnn_started(env)
        finally:
            # The injected record belongs to this test; teardown must never claim the unrelated process.
            env.pid_files[1].unlink(missing_ok=True)
            if unrelated.poll() is None:
                unrelated.kill()
            unrelated.wait(timeout=5)
        env.start(start_ckb=False)
        assert_recovered(env)
        with opened_ckb_channel(env) as channel:
            pay_invoice(env, channel)


# TEST-MAP: FIB-ERR-09
def test_start_during_startup_is_refused_with_a_locatable_reason(fiber_environment) -> None:
    """启动窗口内重复启动被拒且原因可定位；前台占用时 daemon 入口同样给出真实原因。"""
    with fiber_environment("attached-daemon", start=False) as env:
        prepare_existing_chain(env)
        manager = start_background(env, "--json", "fiber", "start", "--daemon")
        try:
            # 环境锁由分离的子进程持有：它出现后进程集合才稳定，此时 PID 记录仍是 starting。
            wait_until(lambda: env_lock_path(env).exists()
                       and _pid_record(env.pid_files[1]).get("status") == "starting",
                       timeout_s=env.startup_timeout_s, description="the daemon startup to hold the environment")
            record_before = env.pid_files[1].read_bytes()
            nodes_before = _snapshot_node_list(env)
            ports_before = _ports_in_use()
            # 启动器先读 PID 记录、环境锁由子进程稍后才取，所以窗口内的第二次启动命中「启动中」分支。
            assert_failure(env, ("fiber", "start", "--daemon"), r"startup is already in progress", r"\d+")
            assert env.pid_files[1].read_bytes() == record_before, "the refused start replaced the running reservation"
            assert _snapshot_node_list(env) == nodes_before, "the refused start rewrote the node list"
            assert _ports_in_use() == ports_before, "the refused start occupied a Fiber port"
            wait_until(lambda: env._snapshot_ready(env._public_snapshot()), timeout_s=env.startup_timeout_s,
                       description="the first Fiber start to connect its nodes")
            env.assert_ready_snapshot(env._public_snapshot())
            assert _await_background(env, manager)["daemon"] is True
            channel = open_ckb_channel(env)
            pay_invoice(env, channel)
        finally:
            _stop_daemon_start(env, manager)

        # 前台启动一直占用环境时，daemon 入口的失败必须能定位到真正原因，而不是只报通用未就绪。
        env.stop_fiber()
        foreground = start_background(env, "--json", "fiber", "start")
        try:
            wait_until(lambda: env_lock_path(env).exists(), timeout_s=env.startup_timeout_s,
                       description="the foreground Fiber start to reserve the environment")
            ports_before = _ports_in_use()
            assert_failure(env, ("fiber", "start", "--daemon"), r"modifying this devnet environment")
            assert _ports_in_use() == ports_before, "the refused start occupied a Fiber port"
            wait_until(lambda: env._snapshot_ready(env._public_snapshot()), timeout_s=env.startup_timeout_s,
                       description="the reserved Fiber start to connect its nodes")
            env.assert_ready_snapshot(env._public_snapshot())
            _await_channel_ready(env, channel)
            pay_invoice(env, channel)
        finally:
            if foreground.process.poll() is None:
                foreground.interrupt()

        # 前台启动按终端方式打断后环境仍可用：daemon 入口可以接手（陈旧锁由产品回收）。
        env.start(start_ckb=False)
        assert_recovered(env)
        env.stop_fiber()
