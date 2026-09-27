from __future__ import annotations

import json
import hashlib
import os
import shutil
import signal
import subprocess
from pathlib import Path

import pytest

from tests.harness import CKB, _command_references_path, hex_int, is_port_open, read_cli_settings, wait_until
from .flows import opened_ckb_channel
from .support_configuration import (
    assert_chain_advances, assert_failure, assert_failed_start_is_clean, assert_processes_unchanged,
    events, messages, nodes_path, read_yaml, result_json, write_nodes,
)


pytestmark = pytest.mark.fiber


# TEST-MAP: FIB-CFG-01
def test_status_matches_uncreated_running_and_stopped_fiber(fiber_environment) -> None:
    """状态报告与真实 RPC 一致，停止独立 Fiber 后原链仍在运行。"""
    with fiber_environment("attached-daemon", start=False) as env:
        initial = result_json(env.runner.run("fiber", "status"), "fiber.status")
        assert initial["nodes"] == [] and initial["ckb"]["status"] == "stopped"
        env.start()
        running = result_json(env.runner.run("fiber", "status"), "fiber.status")
        assert running["ckb"]["status"] == "running"
        assert running["ckb"]["rpcUrl"] == env.rpc.url
        assert running["ckb"]["proxyUrl"] == env.proxy_rpc.url
        assert [node["id"] for node in running["nodes"]] == [1, 2]
        for index, node in enumerate(running["nodes"]):
            info = env.fnn[index].call("node_info")
            assert node["status"] == "running" and node["offckbManaged"] == "yes"
            assert node["rpcUrl"] == env.fnn[index].url
            assert node["p2pAddr"] == f"/ip4/127.0.0.1/tcp/{8344 + index}"
            assert node["accountIndex"] == 3 + index and node["reasons"] == []
            for reported, rpc_field in (("version", "version"), ("commitHash", "commit_hash"),
                                        ("chainHash", "chain_hash"), ("pubkey", "pubkey")):
                assert node[reported] == info[rpc_field]
        original = env.attached_snapshot["commands"]
        env.stop_fiber()
        stopped = result_json(env.runner.run("fiber", "status"), "fiber.status")
        assert stopped["ckb"]["status"] == "running"
        assert [node["id"] for node in stopped["nodes"]] == [1, 2]
        assert all(node["status"] == "stopped" and node["offckbManaged"] == "no" for node in stopped["nodes"])
        assert_processes_unchanged(env, original)
        assert_chain_advances(env)


# TEST-MAP: FIB-CFG-02
def test_real_node_log_tail_and_follow_do_not_stop_services(fiber_environment) -> None:
    """查看真实 FNN 历史及新增日志，退出跟随不会停止节点。"""
    with fiber_environment("attached-daemon") as env:
        env.stop_fiber()
        source = env.node_logs[0].read_text().splitlines()
        assert len(source) >= 5
        tail = env.runner.run("fiber", "logs", "--node", "1", "--tail", "5")
        result_json(tail, "fiber.logs")
        assert [event["message"] for event in events(tail.stderr)] == source[-5:]
        env.start(start_ckb=False)
        commands = env.process_commands()
        stdout_path = env.root / "commands" / "follow.stdout.log"
        stderr_path = env.root / "commands" / "follow.stderr.log"
        history_end = env.node_logs[0].stat().st_size
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            follower = subprocess.Popen(
                (*env.runner.command, "--json", "fiber", "logs", "--node", "1", "--tail", "0", "--follow"),
                cwd=env.runner.cwd, env=env.runner.env, stdin=subprocess.DEVNULL,
                stdout=stdout, stderr=stderr, start_new_session=True,
            )
        env.track_command(follower)
        try:
            wait_until(lambda: follower.poll() is None and bool(stdout_path.read_text().strip()),
                       timeout_s=10, description="fiber logs follower to report its source")
            report = json.loads(stdout_path.read_text())
            assert report["command"] == "fiber.logs" and report["follow"] is True and report["node"] == 1
            assert Path(report["logFile"]) == env.node_logs[0]

            def followed_messages() -> list[str]:
                # A live writer may be between writes; only complete NDJSON records are observable.
                complete = stderr_path.read_text().rpartition("\n")[0]
                return [event["message"] for event in events(complete)]

            with opened_ckb_channel(env) as channel:
                markers = (channel.channel_id[2:], channel.temporary_id[2:])
                wait_until(lambda: any(marker in line for marker in markers for line in followed_messages()),
                           timeout_s=30, description="new channel activity in followed FNN log")
                lines = followed_messages()
                appended = [line for line in env.node_logs[0].read_bytes()[history_end:].decode().splitlines() if line]
                assert lines and any(
                    lines == appended[start:start + len(lines)]
                    for start in range(len(appended) - len(lines) + 1)
                )
        finally:
            if follower.poll() is None:
                os.killpg(follower.pid, signal.SIGINT)
            follower.wait(timeout=10)
        assert_processes_unchanged(env, commands)
        env.assert_ready_snapshot(env.public_snapshot())
        assert_chain_advances(env)
        assert_failure(env, ("fiber", "logs", "--node", "99"), r"node 99", r"does not exist")


# TEST-MAP: FIB-CFG-03
@pytest.mark.parametrize("mode", ["combined-daemon", "attached-daemon"])
def test_node_count_expands_persists_and_shrinks_without_deleting_data(fiber_environment, mode: str) -> None:
    """调整节点数保留存活节点的身份、配置及被移除节点的数据。"""
    with fiber_environment(mode) as env:
        count_option = "--fiber-nodes" if mode.startswith("combined") else "--nodes"
        original = [client.call("node_info")["pubkey"] for client in env.fnn]
        env.stop_fiber()
        document = read_yaml(env, nodes_path(env))
        document["nodes"][0]["config"] = {"fiber": {"announced_node_name": "persistent-node-one"}}
        write_nodes(env, document)
        env.start(extra_args=(count_option, "3"), node_count=3, start_ckb=False)
        three = [client.call("node_info") for client in env.fnn]
        assert len({info["pubkey"] for info in three}) == 3
        assert [info["pubkey"] for info in three[:2]] == original
        assert three[0]["node_name"] == "persistent-node-one"
        env.assert_ready_snapshot(env.public_snapshot())
        third = env.config_path / "fiber" / "nodes" / "3"
        env.stop_fiber()
        retained = {str(path.relative_to(third)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in third.rglob("*") if path.is_file()}
        assert retained and any(name.startswith("fiber/store/") for name in retained)
        env.start(node_count=3, start_ckb=False)
        assert [client.call("node_info")["pubkey"] for client in env.fnn] == [info["pubkey"] for info in three]
        env.stop_fiber()
        retained = {str(path.relative_to(third)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in third.rglob("*") if path.is_file()}
        env.start(extra_args=(count_option, "2"), node_count=2, start_ckb=False)
        assert [client.call("node_info")["pubkey"] for client in env.fnn] == original
        assert env.fnn[0].call("node_info")["node_name"] == "persistent-node-one"
        assert not is_port_open(21716) and not is_port_open(8346)
        assert read_yaml(env, nodes_path(env))["nodes"] == document["nodes"]
        assert {str(path.relative_to(third)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in third.rglob("*") if path.is_file()} == retained
        daemon_log = env.config_path / ("data" if mode.startswith("combined") else "fiber") / "logs" / "daemon.log"
        diagnostic = env.start_result.stderr + daemon_log.read_text()
        assert "Node 3 is removed" in diagnostic and "kept" in diagnostic


# TEST-MAP: FIB-CFG-04
def test_persistent_node_override_and_managed_field_rejection(fiber_environment) -> None:
    """节点名覆盖跨重启保留，托管 RPC 字段覆盖被拒绝且可直接修复。"""
    with fiber_environment("attached-daemon") as env:
        original_keys = [client.call("node_info")["pubkey"] for client in env.fnn]
        original_second_name = env.fnn[1].call("node_info")["node_name"]
        env.stop_fiber()
        document = read_yaml(env, nodes_path(env))
        document["nodes"][0]["config"] = {"fiber": {"announced_node_name": "my-local-fiber"}}
        write_nodes(env, document)
        for _ in range(2):
            env.start(start_ckb=False)
            infos = [client.call("node_info") for client in env.fnn]
            assert infos[0]["node_name"] == "my-local-fiber"
            assert infos[1]["node_name"] == original_second_name
            assert [info["pubkey"] for info in infos] == original_keys
            env.assert_ready_snapshot(env.public_snapshot())
            env.stop_fiber()
        document["nodes"][0]["config"]["ckb"] = {"rpc_url": "http://127.0.0.1:29999"}
        write_nodes(env, document)
        assert_failure(env, ("fiber", "start"), r"node 1", r"ckb\.rpc_url", r"managed.*cannot be overridden")
        assert_failed_start_is_clean(env, attached=True)
        del document["nodes"][0]["config"]["ckb"]
        write_nodes(env, document)
        env.start(start_ckb=False)
        assert [client.call("node_info")["pubkey"] for client in env.fnn] == original_keys
        env.assert_ready_snapshot(env.public_snapshot())


# TEST-MAP: FIB-CFG-05
def test_fnn_version_selection_has_explicit_precedence_and_preserves_setting(fiber_environment) -> None:
    """不支持的默认版本明确失败，显式版本覆盖但不改写持久设置。"""
    with fiber_environment("attached-daemon", start=False) as env:
        env.start_existing_chain()
        env.runner.run("config", "set", "fnn-version", "0.8.0")
        assert "0.8.0" in messages(env.runner.run("config", "get", "fnn-version"))
        settings = read_cli_settings(env.runner)
        assert settings["bins"]["defaultFnnVersion"] == "0.8.0"
        assert_failure(env, ("fiber", "start"), r"0\.8\.0.*not supported", r"Supported versions: 0\.9\.0")
        assert_failed_start_is_clean(env, attached=True)
        env.start(extra_args=("0.9.0",), start_ckb=False)
        assert all(client.call("node_info")["version"] == "0.9.0" for client in env.fnn)
        assert read_cli_settings(env.runner) == settings
        env.stop_fiber()
        env.runner.run("config", "set", "fnn-version", "0.9.0")
        env.start(start_ckb=False)
        assert read_cli_settings(env.runner)["bins"]["defaultFnnVersion"] == "0.9.0"
        env.assert_ready_snapshot(env.public_snapshot())


# TEST-MAP: FIB-CFG-06
@pytest.mark.parametrize("mode", ["combined-daemon", "attached-daemon"])
@pytest.mark.parametrize("sibling_template", [True, False], ids=["sibling-template", "bundled-template"])
def test_custom_fnn_binary_uses_real_path_and_available_template(fiber_environment, mode: str, sibling_template: bool) -> None:
    """本地真实 FNN 绕过托管版本选择，并选用相邻或随包模板。"""
    with fiber_environment(mode, start=False) as env:
        env.runner.run("config", "set", "fnn-version", "0.8.0")
        local = env.root / "local-fnn" / "fnn"
        local.parent.mkdir()
        shutil.copy2(env.managed_fnn.resolve(), local)
        template = local.parent / "config" / "testnet" / "config.yml"
        if sibling_template:
            template.parent.mkdir(parents=True)
            data = read_yaml(env, env.managed_fnn.parent / "config" / "testnet" / "config.yml")
            data["fiber"]["auto_accept_channel_ckb_funding_amount"] = 123 * CKB
            template.write_text(json.dumps(data) + "\n")
        # Keep only the supplied executable/layout in this isolated HOME. A silent
        # fallback to the preloaded managed executable or its template cannot pass.
        shutil.rmtree(env.managed_fnn.parent)
        env.allow_binary(local)
        option = "--fnn-binary-path" if mode.startswith("combined") else "--binary-path"
        env.start(extra_args=(option, str(local)))
        env.assert_ready_snapshot(env.public_snapshot())
        for node in (1, 2):
            directory = env.config_path / "fiber" / "nodes" / str(node)
            assert any(_command_references_path(command, local) and _command_references_path(command, directory)
                       for command in env.process_commands().values())
        daemon_log = env.config_path / ("data" if mode.startswith("combined") else "fiber") / "logs" / "daemon.log"
        diagnostic = daemon_log.read_text()
        assert not env.managed_fnn.parent.exists(), "custom binary selection populated the managed installation"
        assert not (env.managed_fnn.parent.parent / "0.8.0").exists()
        assert "downloading" not in diagnostic.lower()
        if sibling_template:
            assert str(template) in diagnostic
            assert all(hex_int(client.call("node_info")["auto_accept_channel_ckb_funding_amount"]) == 123 * CKB
                       for client in env.fnn)
        else:
            assert "using the testnet config shipped with offckb" in diagnostic
        assert read_cli_settings(env.runner)["bins"]["defaultFnnVersion"] == "0.8.0"


# TEST-MAP: FIB-CFG-07
@pytest.mark.parametrize("mode", ["combined-daemon", "attached-daemon"])
def test_single_node_is_ready_without_a_second_peer(fiber_environment, mode: str) -> None:
    """单节点可查看接口并持续出块，无需不存在的第二节点。"""
    with fiber_environment(mode, start=False) as env:
        option = "--fiber-nodes" if mode.startswith("combined") else "--nodes"
        env.start(extra_args=(option, "1"), node_count=1)
        assert len(env.fnn) == 1
        env.assert_ready_snapshot(env.public_snapshot())
        assert env.fnn[0].call("list_peers")["peers"] == []
        assert not is_port_open(21715) and not is_port_open(8345)
        assert [node["id"] for node in read_yaml(env, nodes_path(env))["nodes"]] == [1]
        assert not (env.config_path / "fiber" / "nodes" / "2").exists()
        assert_chain_advances(env)
