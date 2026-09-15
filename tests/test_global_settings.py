from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from .conftest import _empty_user_env
from .harness import (
    DEVNET_PORTS,
    DevnetManager,
    OffckbRunner,
    _command_references_path,
    _process_group_commands,
    read_ckb_version,
    read_cli_settings,
    is_port_open,
    wait_until,
)


pytestmark = pytest.mark.core


def _messages(runner: OffckbRunner, *args: str) -> list[str]:
    return [json.loads(line)["message"].strip() for line in runner.run(*args).stderr.splitlines()]


def _settings_file(runner: OffckbRunner) -> Path:
    locations = [
        Path(message.split("config file:", 1)[1].strip())
        for message in _messages(runner, "config", "list") if message.startswith("config file:")
    ]
    assert len(locations) == 1, "config list did not report its settings file"
    assert locations[0].is_relative_to(Path(runner.env["HOME"]))
    return locations[0]


def _assert_isolated_paths(settings: dict, runner: OffckbRunner) -> None:
    home = Path(runner.env["HOME"])
    paths = [
        value for section in settings.values() if isinstance(section, dict)
        for key, value in section.items() if key.endswith("Path") or key == "rootFolder"
    ]
    assert paths, "config list did not report environment paths"
    assert all(Path(value).is_absolute() and Path(value).is_relative_to(home) for value in paths)


# TEST-MAP: CFG-01
def test_first_configuration_read_reports_isolated_defaults_without_starting_node(
    uninitialized_devnet: DevnetManager, package_default_settings: dict,
) -> None:
    """首次查询报告包默认设置及当前用户目录，读取不会启动开发链。"""
    devnet = uninitialized_devnet
    runner = devnet.runner
    assert not any(is_port_open(port) for port in DEVNET_PORTS)
    settings = read_cli_settings(runner)
    _assert_isolated_paths(settings, runner)
    assert _settings_file(runner).name == "settings.json"
    assert settings["bins"]["defaultCKBVersion"] == package_default_settings["bins"]["defaultCKBVersion"]
    assert settings.get("proxy") is None
    assert settings["bins"]["defaultCKBVersion"] in _messages(runner, "config", "get", "ckb-version")
    assert any("no proxy" in message.lower() for message in _messages(runner, "config", "get", "proxy"))
    assert not devnet.pid_file.exists()
    assert not (devnet.config_path / "ckb.toml").exists()
    assert not any(is_port_open(port) for port in DEVNET_PORTS)


# TEST-MAP: CFG-03
def test_proxy_read_write_and_removal_preserve_other_settings(uninitialized_devnet: DevnetManager) -> None:
    """用户设置并移除代理，后续进程读取一致且保留所选 CKB 及其他设置。"""
    runner = uninitialized_devnet.runner
    runner.run("config", "set", "ckb-version", "v0.203.0")
    before = read_cli_settings(runner)
    settings_file = _settings_file(runner)
    persisted_before = json.loads(settings_file.read_text(encoding="utf-8"))
    runner.run("config", "set", "proxy", "http://127.0.0.1:19090")
    expected = copy.deepcopy(before)
    expected["proxy"] = {"host": "127.0.0.1", "port": 19090, "protocol": "http"}
    assert read_cli_settings(runner) == expected
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {**persisted_before, "proxy": expected["proxy"]}
    assert "http://127.0.0.1:19090" in _messages(runner, "config", "get", "proxy")
    runner.run("config", "rm", "proxy")
    assert read_cli_settings(runner) == before
    assert json.loads(settings_file.read_text(encoding="utf-8")) == persisted_before
    assert any("no proxy" in message.lower() for message in _messages(runner, "config", "get", "proxy"))
    assert "0.203.0" in _messages(runner, "config", "get", "ckb-version")


# TEST-MAP: CFG-04
@pytest.mark.parametrize("args", [
    ("set", "ckb-version", "not-a-version"),
    ("set", "proxy", "not-a-url"),
    ("set", "ckb-version"),
    ("set", "proxy"),
    ("set", "unknown-setting", "value"),
    ("unknown-action", "proxy"),
    ("rm", "ckb-version"),
])
def test_invalid_configuration_request_preserves_existing_settings(
    uninitialized_devnet: DevnetManager, args: tuple[str, ...],
) -> None:
    """错误配置请求明确失败，磁盘和新进程读取的既有配置均不变。"""
    runner = uninitialized_devnet.runner
    runner.run("config", "set", "ckb-version", "v0.203.0")
    runner.run("config", "set", "proxy", "http://127.0.0.1:19090")
    settings_file = _settings_file(runner)
    before = settings_file.read_bytes()
    effective_before = read_cli_settings(runner)
    result = runner.run("config", *args, check=False, timeout_s=15)
    assert result.returncode != 0
    assert not result.stdout
    errors = [event for line in result.stderr.splitlines() if (event := json.loads(line)).get("ok") is False]
    assert len(errors) == 1 and errors[0].get("code"), result.stderr
    assert any(word in errors[0]["message"].lower() for word in ("invalid", "required", "missing", "no proxy"))
    assert settings_file.read_bytes() == before
    assert read_cli_settings(runner) == effective_before


# TEST-MAP: CFG-05
def test_two_user_environments_keep_independent_settings(uninitialized_devnet: DevnetManager) -> None:
    """切换两套用户环境读取、修改和删除设置，另一套环境保持原样。"""
    first = uninitialized_devnet.runner
    root = first.cwd.parent / "second-user"
    for name in ("home", "workspace", "tmp"):
        (root / name).mkdir(parents=True)
    second = OffckbRunner(
        first.command, cli_entry=first.cli_entry, env=_empty_user_env(root),
        cwd=root / "workspace", records_dir=root / "commands",
    )
    for runner, version, proxy in (
        (first, "0.203.0", "http://127.0.0.1:19090"),
        (second, "0.204.0", "http://127.0.0.1:19091"),
    ):
        runner.run("config", "set", "ckb-version", version)
        runner.run("config", "set", "proxy", proxy)
        assert version in _messages(runner, "config", "get", "ckb-version")
        assert proxy in _messages(runner, "config", "get", "proxy")
        _assert_isolated_paths(read_cli_settings(runner), runner)
    first_file, second_file = _settings_file(first), _settings_file(second)
    assert first_file != second_file
    second_before = second_file.read_bytes()
    first_before, second_effective = read_cli_settings(first), read_cli_settings(second)
    first.run("config", "set", "ckb-version", "0.205.0")
    first.run("config", "rm", "proxy")
    expected = copy.deepcopy(first_before)
    expected["bins"]["defaultCKBVersion"] = "0.205.0"
    expected.pop("proxy")
    assert read_cli_settings(first) == expected
    persisted = json.loads(first_file.read_text(encoding="utf-8"))
    assert persisted["bins"]["defaultCKBVersion"] == "0.205.0" and "proxy" not in persisted
    assert "0.205.0" in _messages(first, "config", "get", "ckb-version")
    assert any("no proxy" in message.lower() for message in _messages(first, "config", "get", "proxy"))
    assert read_cli_settings(second) == second_effective
    assert second_file.read_bytes() == second_before


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
