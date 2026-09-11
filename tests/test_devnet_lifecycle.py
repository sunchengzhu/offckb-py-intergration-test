from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from .harness import (
    DEVNET_PORTS, DevnetManager, OffckbRunner, RpcClient,
    _command_references_path, _process_group_commands, is_port_open, wait_until,
)


pytestmark = [pytest.mark.core]


def _process_group_members(process_group_id: int) -> set[int]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,pgid="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    members: set[int] = set()
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        pid, pgid = (int(value) for value in fields)
        if pgid == process_group_id:
            members.add(pid)
    return members


def _process_alive(process_id: int) -> bool:
    try:
        os.kill(process_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# TEST-MAP: NODE-01
def test_fresh_daemon_initializes_and_becomes_ready(uninitialized_devnet: DevnetManager) -> None:
    devnet = uninitialized_devnet
    assert not list(Path(devnet.runner.env["HOME"]).iterdir())
    assert "OFFCKB_CLI_PATH" not in devnet.runner.env
    result = devnet.start()
    assert result is not None
    assert result["ok"] is True
    assert result["command"] == "node"
    assert result["network"] == "devnet"
    assert result["daemon"] is True
    assert result["rpcUrl"] == "http://127.0.0.1:8114"
    assert result["proxyUrl"] == "http://127.0.0.1:28114"

    pid_file = Path(result["pidFile"])
    log_file = Path(result["logFile"])
    assert pid_file.is_file()
    assert log_file.is_file()
    metadata = json.loads(pid_file.read_text(encoding="utf-8"))
    assert metadata["pid"] == result["pid"]
    assert metadata["status"] == "running"

    config_path = devnet.config_path
    assert config_path is not None
    assert (config_path / "ckb.toml").is_file()
    assert (config_path / "ckb-miner.toml").is_file()
    assert (config_path / "specs" / "dev.toml").is_file()
    assert devnet.rpc.ready()
    assert all(is_port_open(port) for port in DEVNET_PORTS)
    assert devnet.pgid is not None
    commands = _process_group_commands(devnet.pgid)
    for component in ("run", "miner"):
        assert any(
            _command_references_path(command, devnet.ckb_bin)
            and _command_references_path(command, config_path)
            and component in command.split()
            for command in commands.values()
        ), f"{component} did not use the requested --binary-path: {commands}"


# TEST-MAP: NODE-02
def test_miner_advances_tip_and_indexer_catches_up(
    devnet: DevnetManager, offckb: OffckbRunner, rpc: RpcClient
) -> None:
    before = rpc.tip()
    wait_until(lambda: rpc.tip() > before, timeout_s=30.0, interval_s=0.5, description="miner to advance tip")
    target = rpc.tip()
    assert rpc.wait_indexer(target) >= target

    info: dict[str, object] = {}

    def reports_ready() -> bool:
        nonlocal info
        result = offckb.run("devnet", "info")
        assert result.json is not None
        info = result.json
        return (
            info.get("ready") is True
            and info.get("indexerReady") is True
            and info.get("indexerLag") == "0"
        )

    wait_until(reports_ready, timeout_s=30.0, interval_s=0.5, description="devnet info to report ready")
    assert info["command"] == "devnet.info"
    assert info["kind"] == "pure-devnet"
    assert info["indexerLag"] == "0"
    assert info["rpcUrl"] == "http://127.0.0.1:8114"


# TEST-MAP: NODE-04
def test_stop_terminates_the_owned_service_group(devnet: DevnetManager) -> None:
    owned_pid = devnet.pid
    assert owned_pid is not None
    owned_pgid = os.getpgid(owned_pid)
    assert owned_pgid == owned_pid, "the offckb daemon must own its detached process group"
    service_pids = _process_group_members(owned_pgid)
    assert owned_pid in service_pids
    assert len(service_pids) >= 3, (
        f"expected daemon, CKB node, and miner in process group {owned_pgid}: {service_pids}"
    )
    pid_file = devnet.pid_file
    assert pid_file is not None and pid_file.is_file()

    result = devnet.stop()
    assert result["ok"] is True
    assert result["command"] == "node.stop"
    assert result["stopped"] is True
    assert int(result["pid"]) == owned_pid
    assert all(not is_port_open(port) for port in DEVNET_PORTS)
    assert not pid_file.exists()
    wait_until(
        lambda: all(not _process_alive(pid) for pid in service_pids)
        and service_pids.isdisjoint(_process_group_members(owned_pgid)),
        timeout_s=10.0,
        interval_s=0.25,
        description=f"owned service processes {sorted(service_pids)} to exit",
    )
