from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from .harness import (
    DevnetManager,
    _command_references_path,
    _process_group_commands,
    read_ckb_version,
    read_cli_settings,
    wait_until,
)


pytestmark = pytest.mark.core


# TEST-MAP: CFG-02
def test_selected_ckb_version_persists_and_drives_node_and_miner(
    uninitialized_devnet: DevnetManager, default_ckb_bin: Path,
) -> None:
    """用户选择另一个本地 CKB 版本后，下一次启动实际使用该版本并保留其他设置。"""
    devnet = uninitialized_devnet
    cli = devnet.runner
    home = Path(cli.env["HOME"])
    defaults = read_cli_settings(cli)
    default_version = defaults["bins"]["defaultCKBVersion"]
    selected_version = read_ckb_version(devnet.ckb_bin)
    assert selected_version != default_version, (
        "CFG-02 needs CKB_BIN to differ from the package default; "
        "use DEFAULT_CKB_BIN for the package's default version."
    )
    bins_root = Path(defaults["bins"]["rootFolder"])
    assert bins_root.is_relative_to(home)
    binary_name = "ckb.exe" if os.name == "nt" else "ckb"
    # Both real versions are available, so silently choosing the default is observable.
    for version, binary in ((default_version, default_ckb_bin), (selected_version, devnet.ckb_bin)):
        managed = bins_root / version / binary_name
        managed.parent.mkdir(parents=True)
        managed.symlink_to(binary)
    selected_binary = bins_root / selected_version / binary_name

    cli.run("config", "set", "proxy", "http://127.0.0.1:19090")
    before = read_cli_settings(cli)
    assert before["proxy"] == {"host": "127.0.0.1", "port": 19090, "protocol": "http"}
    settings_files = list(home.rglob("settings.json"))
    assert len(settings_files) == 1
    settings_file = settings_files[0]
    persisted_before = json.loads(settings_file.read_text(encoding="utf-8"))

    cli.run("config", "set", "ckb-version", f"v{selected_version}")
    expected = copy.deepcopy(before)
    expected["bins"]["defaultCKBVersion"] = selected_version
    # Effective settings include bundled defaults that need not be written to disk.
    persisted_expected = copy.deepcopy(persisted_before)
    persisted_expected["bins"]["defaultCKBVersion"] = selected_version
    assert json.loads(settings_file.read_text(encoding="utf-8")) == persisted_expected
    # Each CLI call is a new process; verify both the public read and persisted value.
    selected = cli.run("config", "get", "ckb-version")
    messages = [json.loads(line)["message"].strip() for line in selected.stderr.splitlines()]
    assert selected_version in messages
    assert read_cli_settings(cli) == expected

    devnet.start(use_managed_binary=True)
    assert devnet.rpc.call("local_node_info")["version"].split()[0] == selected_version
    assert devnet.pgid is not None
    commands = _process_group_commands(devnet.pgid)
    for component in ("run", "miner"):
        assert any(
            _command_references_path(command, selected_binary)
            and _command_references_path(command, devnet.config_path)
            and component in command.split()
            for command in commands.values()
        ), f"{component} did not use the selected CKB {selected_version}: {commands}"
    tip = devnet.rpc.tip()
    wait_until(
        lambda: devnet.rpc.tip() > tip, timeout_s=30,
        description="the selected CKB node and miner to keep producing blocks",
    )
    assert read_cli_settings(cli) == expected
    assert json.loads(settings_file.read_text(encoding="utf-8")) == persisted_expected
