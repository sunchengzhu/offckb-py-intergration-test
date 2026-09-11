from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from pathlib import Path
from typing import Any

import pytest

from .conftest import _empty_user_env
from .harness import (
    DEVNET_PORTS,
    DevnetManager,
    OffckbRunner,
    RpcClient,
    _command_references_path,
    _process_alive,
    _process_group_commands,
    is_port_open,
    wait_until,
)


pytestmark = [pytest.mark.core]


@pytest.fixture(scope="module")
def package_default_settings(offckb: OffckbRunner, run_root: Path) -> dict[str, Any]:
    """Read packaged defaults through the public CLI in a separate empty HOME."""
    root = run_root / "default-settings-probe"
    for name in ("home", "workspace", "commands", "tmp"):
        (root / name).mkdir(parents=True)
    runner = OffckbRunner(
        offckb.command,
        cli_entry=offckb.cli_entry,
        env=_empty_user_env(root),
        cwd=root / "workspace",
        records_dir=root / "commands",
    )
    result = runner.run("config", "list")
    candidates = []
    for line in result.stderr.splitlines():
        event = json.loads(line)
        try:
            settings = json.loads(event.get("message", ""))
        except json.JSONDecodeError:
            continue
        if isinstance(settings, dict) and "bins" in settings and "devnet" in settings:
            candidates.append(settings)
    assert len(candidates) == 1, "offckb config list did not expose one default settings object"
    settings = candidates[0]
    # Only translate the isolated user root; do not overwrite the package's defaults.
    settings["probeHome"] = runner.env["HOME"]
    return settings


@pytest.fixture(scope="module")
def default_ckb_bin(
    pytestconfig: pytest.Config, ckb_bin: Path, package_default_settings: dict[str, Any]
) -> Path:
    selected = pytestconfig.getoption("--default-ckb-bin")
    binary = Path(selected).expanduser().resolve() if selected else ckb_bin
    assert binary.is_file() and os.access(binary, os.X_OK), f"CKB binary is not executable: {binary}"
    result = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, timeout=10, check=False
    )
    version = re.search(r"\b(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)\b", result.stdout)
    expected = package_default_settings["bins"]["defaultCKBVersion"]
    assert result.returncode == 0 and version is not None, result.stdout + result.stderr
    assert version.group(1) == expected, (
        f"NODE-13 requires the package's default CKB {expected}, but {binary} reports {version.group(1)}. "
        "Prepare that real local binary and set DEFAULT_CKB_BIN or --default-ckb-bin. "
        "The test never downloads CKB or changes OffCKB's default version."
    )
    return binary


def _cleanup_foreground_group(
    process: subprocess.Popen, *, cli_entry: Path, managed_binary: Path, config_path: Path
) -> None:
    """Fallback only after the test has observed the product's own stop result."""
    process.poll()  # Reap the child before checking whether its group still exists.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        members = _process_group_commands(process.pid)
        if not members:
            return
        assert all(
            (pid == process.pid and process.poll() is None)
            or _command_references_path(command, cli_entry)
            or (
                _command_references_path(command, managed_binary)
                and _command_references_path(command, config_path)
            )
            for pid, command in members.items()
        ), f"Refusing to terminate a group with unrecognized processes: {members}"
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return
        try:
            wait_until(
                lambda: process.poll() is not None and not _process_group_commands(process.pid),
                timeout_s=10,
                description="owned foreground process group to exit during fallback cleanup",
            )
            return
        except AssertionError:
            if sig == signal.SIGKILL:
                raise


# TEST-MAP: NODE-13
def test_default_foreground_node_can_be_used_interrupted_and_restarted(
    devnet_manager: DevnetManager,
    offckb: OffckbRunner,
    run_root: Path,
    default_ckb_bin: Path,
    package_default_settings: dict[str, Any],
    pytestconfig: pytest.Config,
) -> None:
    """用户直接启动默认开发链，连接使用后按 Ctrl+C 退出，并能再次启动。"""
    devnet_manager.close()
    root = run_root / "default-foreground"
    for name in ("home", "workspace", "commands", "tmp"):
        (root / name).mkdir(parents=True)
    env = _empty_user_env(root)
    home = Path(env["HOME"])
    probe_home = Path(package_default_settings["probeHome"])
    version = package_default_settings["bins"]["defaultCKBVersion"]
    bins_root = home / Path(package_default_settings["bins"]["rootFolder"]).relative_to(probe_home)
    config_path = home / Path(package_default_settings["devnet"]["configPath"]).relative_to(probe_home)
    managed_binary = bins_root / version / "ckb"
    managed_binary.parent.mkdir(parents=True)
    managed_binary.symlink_to(default_ckb_bin)
    assert not config_path.exists(), "the node must initialize its own chain configuration"
    assert not list(home.rglob("settings.json")), "the first node launch must use packaged defaults"
    assert "OFFCKB_CLI_PATH" not in env

    rpc = RpcClient(package_default_settings["devnet"]["rpcUrl"])
    proxy_url = f"http://127.0.0.1:{package_default_settings['devnet']['rpcProxyPort']}"
    proxy = RpcClient(proxy_url)
    for attempt in (1, 2):
        stdout_path = root / "commands" / f"node-{attempt}.stdout.log"
        stderr_path = root / "commands" / f"node-{attempt}.stderr.log"
        argv = (*offckb.command, "node")
        with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
            process = subprocess.Popen(
                argv, cwd=root / "workspace", env=env,
                stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True,
            )
        try:
            def ready() -> bool:
                assert process.poll() is None, (
                    f"plain offckb node exited during startup ({process.returncode})\n"
                    f"{stdout_path.read_text()}{stderr_path.read_text()}"
                )
                output = stdout_path.read_text() + stderr_path.read_text()
                return rpc.ready() and proxy.ready() and rpc.url in output and proxy.url in output

            wait_until(
                ready, timeout_s=pytestconfig.getoption("--startup-timeout"),
                description="plain offckb node to provide a healthy node and proxy",
            )
            output = stdout_path.read_text() + stderr_path.read_text()
            assert rpc.url in output and proxy.url in output, output
            actual_version = rpc.call("local_node_info")["version"]
            assert re.match(rf"{re.escape(version)}(?:\s|$)", actual_version), actual_version
            for filename in ("ckb.toml", "ckb-miner.toml", "specs/dev.toml"):
                assert (config_path / filename).is_file()
            commands = _process_group_commands(process.pid)
            for component in ("run", "miner"):
                assert any(
                    _command_references_path(command, managed_binary)
                    and _command_references_path(command, config_path)
                    and component in command.split()
                    for command in commands.values()
                ), f"default {component} did not use the prepared managed binary: {commands}"
            before = rpc.tip()
            wait_until(
                lambda: process.poll() is None and rpc.tip() > before,
                timeout_s=30, description="the default foreground chain to keep mining",
            )

            # A terminal delivers Ctrl+C to its foreground group, including CKB/miner.
            # Merely killing the CLI PID would model a different user action.
            os.killpg(process.pid, signal.SIGINT)
            wait_until(
                lambda: process.poll() is not None
                and all(not _process_alive(pid) for pid in commands)
                and not _process_group_commands(process.pid)
                and all(not is_port_open(port) for port in DEVNET_PORTS),
                timeout_s=20, description="Ctrl+C to release the foreground chain and all ports",
            )
            assert not list(config_path.rglob("*.pid")), "foreground shutdown left PID metadata"
        finally:
            _cleanup_foreground_group(
                process, cli_entry=offckb.cli_entry, managed_binary=managed_binary, config_path=config_path,
            )
