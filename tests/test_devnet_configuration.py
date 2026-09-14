from __future__ import annotations

import copy
import hashlib
import json
import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from .harness import (
    DEVNET_PORTS, Account, DevnetManager, _configured_devnet_paths, hex_int, is_port_open,
    transaction_hash_from, wait_until,
)


pytestmark = pytest.mark.core

LOG_LEVEL = re.compile(r"\s(TRACE|DEBUG|INFO|WARN|ERROR)\s+\S+\s")


def _data_snapshot(root: Path) -> dict[str, tuple[str, str]]:
    """Compare stopped-chain contents without depending on timestamps or inodes."""
    assert root.is_dir(), f"chain data was removed: {root}"
    snapshot = {}
    for path in root.rglob("*"):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[name] = ("link", str(path.readlink()))
        elif path.is_dir():
            snapshot[name] = ("directory", "")
        else:
            assert path.is_file(), f"unexpected special file in chain data: {path}"
            with path.open("rb") as stream:
                snapshot[name] = ("file", hashlib.file_digest(stream, "sha256").hexdigest())
    return snapshot


def _configuration_error(result) -> str:
    assert result.returncode != 0, "invalid configuration request was reported as successful"
    assert result.stdout == "", "failed configuration request returned a command result"
    errors = [event for line in result.stderr.splitlines() if (event := json.loads(line)).get("ok") is False]
    assert len(errors) == 1 and errors[0].get("code"), result.stderr
    assert isinstance(errors[0].get("message"), str) and errors[0]["message"], errors[0]
    return errors[0]["message"]


# TEST-MAP: DVCFG-01
def test_batch_configuration_saves_typed_values_and_preserves_other_state(
    isolated_devnet: DevnetManager,
) -> None:
    """用户一次保存字符串、布尔和整数选项，其他配置及已有链文件不变。"""
    devnet = isolated_devnet
    devnet.start()
    tip = devnet.rpc.tip()
    wait_until(lambda: devnet.rpc.tip() > tip, timeout_s=30, description="blocks before configuration editing")
    devnet.stop()
    config_path, data_path = _configured_devnet_paths(devnet.runner.env)
    expected = {
        name: tomllib.loads((config_path / name).read_text(encoding="utf-8"))
        for name in ("ckb.toml", "ckb-miner.toml")
    }
    node, miner = expected["ckb.toml"], expected["ckb-miner.toml"]
    next_filter = "error" if node["logger"]["filter"] != "error" else "warn"
    next_log_to_file = not node["logger"]["log_to_file"]
    next_interval = miner["miner"]["client"]["poll_interval"] + 1
    data_before = _data_snapshot(data_path)
    spec_before = (config_path / "specs" / "dev.toml").read_bytes()
    expected = copy.deepcopy(expected)
    expected["ckb.toml"]["logger"].update(filter=next_filter, log_to_file=next_log_to_file)
    expected["ckb-miner.toml"]["miner"]["client"]["poll_interval"] = next_interval

    devnet.runner.run(
        "devnet", "config", "--set", f"ckb.logger.filter={next_filter}",
        "--set", f"ckb.logger.log_to_file={str(next_log_to_file).lower()}",
        "--set", f"miner.client.poll_interval={next_interval}",
    )
    actual = {
        name: tomllib.loads((config_path / name).read_text(encoding="utf-8")) for name in expected
    }
    assert actual == expected, "batch editing changed an unspecified configuration field"
    assert type(actual["ckb.toml"]["logger"]["filter"]) is str
    assert type(actual["ckb.toml"]["logger"]["log_to_file"]) is bool
    assert type(actual["ckb-miner.toml"]["miner"]["client"]["poll_interval"]) is int
    assert _data_snapshot(data_path) == data_before
    assert (config_path / "specs" / "dev.toml").read_bytes() == spec_before


# TEST-MAP: DVCFG-04
@pytest.mark.parametrize("condition, filename", [
    pytest.param("uninitialized", None, id="uninitialized"),
    pytest.param("missing", "ckb.toml", id="missing-node-config"),
    pytest.param("missing", "ckb-miner.toml", id="missing-miner-config"),
    pytest.param("malformed", "ckb.toml", id="malformed-node-config"),
    pytest.param("malformed", "ckb-miner.toml", id="malformed-miner-config"),
])
def test_configuration_edit_rejects_missing_or_malformed_files_without_overwriting(
    isolated_devnet: DevnetManager, condition: str, filename: str | None,
) -> None:
    """配置尚未建立、文件缺失或损坏时，编辑失败并保留可恢复的原文件及链数据。"""
    devnet = isolated_devnet
    config_path = devnet.config_path
    if filename is not None:
        devnet.start()
        devnet.stop()
        target = config_path / filename
        if condition == "missing":
            target.unlink()
        else:
            target.write_text('[logger\nfilter = "user-recoverable-value"\n', encoding="utf-8")
    assert config_path.exists() == (condition != "uninitialized")
    before = _data_snapshot(config_path) if config_path.exists() else None
    settings_before = {
        path: path.read_bytes() for path in Path(devnet.runner.env["HOME"]).rglob("settings.json")
    }
    result = devnet.runner.run("devnet", "config", "--set", "ckb.logger.filter=info", check=False, timeout_s=15)
    message = _configuration_error(result).lower()
    if condition == "malformed":
        assert any(word in message for word in ("parse", "toml", "line", "row", "character")), message
    else:
        assert any(word in message for word in ("missing", "does not exist", "not found")), message
    assert (_data_snapshot(config_path) if config_path.exists() else None) == before
    assert {path: path.read_bytes() for path in Path(devnet.runner.env["HOME"]).rglob("settings.json")} == settings_before
    assert not devnet.pid_file.exists()
    assert not any(is_port_open(port) for port in DEVNET_PORTS)


# TEST-MAP: DVCFG-05
def test_configuration_editor_without_set_fails_promptly_outside_a_terminal(
    isolated_devnet: DevnetManager,
) -> None:
    """脚本误调用交互编辑器时立即得到使用终端或 --set 的提示，环境保持原样。"""
    devnet = isolated_devnet
    devnet.start()
    devnet.stop()
    before = _data_snapshot(devnet.config_path)
    result = devnet.runner.run("devnet", "config", check=False, timeout_s=10)
    message = _configuration_error(result).lower()
    assert "--set" in message or "tty" in message or "terminal" in message
    assert _data_snapshot(devnet.config_path) == before
    assert not devnet.pid_file.exists()
    assert not any(is_port_open(port) for port in DEVNET_PORTS)


# TEST-MAP: DVCFG-02
def test_log_level_changes_take_effect_without_losing_development_progress(
    isolated_devnet: DevnetManager,
    accounts: list[Account],
    private_key_file: Callable[[Account], Path],
) -> None:
    """用户临时打开启动日志排障，再恢复 warn，正常重启始终保留已有交易。"""
    devnet = isolated_devnet
    runner, rpc = devnet.runner, devnet.rpc
    devnet.start()
    rpc.wait_indexer()
    transfer = runner.run(
        "transfer", accounts[5].address, "100", "--network", "devnet",
        "--privkey-file", private_key_file(accounts[4]),
    )
    tx_hash = transaction_hash_from(transfer)
    committed = rpc.wait_transaction(tx_hash)
    assert hex_int(committed["tx_status"]["block_number"]) > 0
    devnet.stop()

    config_path, data_path = _configured_devnet_paths(runner.env)
    node_config = config_path / "ckb.toml"
    node_log = data_path / "logs" / "run.log"
    info_startup: str | None = None
    for level in ("warn", "info", "warn"):
        runner.run("devnet", "config", "--set", f"ckb.logger.filter={level}")
        assert tomllib.loads(node_config.read_text(encoding="utf-8"))["logger"]["filter"] == level
        before = node_log.read_bytes()

        devnet.start()
        tip = rpc.tip()
        wait_until(
            lambda: rpc.tip() > tip,
            timeout_s=30, interval_s=0.5, description=f"the chain to keep mining with {level} logging",
        )
        transaction = rpc.call("get_transaction", [tx_hash])
        assert transaction and transaction["tx_status"]["status"] == "committed"
        assert transaction["tx_status"]["block_hash"] == committed["tx_status"]["block_hash"]
        assert transaction["transaction"] == committed["transaction"]
        devnet.stop()

        # Stop the writer before comparing logs; only the appended suffix belongs to this run.
        after = node_log.read_bytes()
        assert after.startswith(before), "changing the log level removed or rewrote existing node logs"
        lines = after.decode("utf-8").splitlines()
        shown = runner.run("logs", "node", "--tail", max(1, len(lines)), json_mode=False)
        assert shown.stdout.splitlines() == lines, "logs node did not display the real node records"
        fresh_lines = after[len(before):].decode("utf-8").splitlines()
        fresh_info = [
            line for line in fresh_lines
            if (record := LOG_LEVEL.search(line)) and record.group(1) == "INFO"
        ]
        if level == "info":
            startup = [line for line in fresh_info if "ckb version:" in line]
            assert startup, "info logging did not expose this node's startup information"
            info_startup = startup[0]
        else:
            assert not fresh_info, f"warn logging still emitted new INFO records: {fresh_info}"
            if info_startup is not None:
                # A quiet new run must not pass because the log reader returned nothing.
                assert info_startup in shown.stdout.splitlines()


# TEST-MAP: DVCFG-03
@pytest.mark.parametrize(
    ("invalid", "locators"),
    [
        pytest.param("ckb.logger.filte=info", ("ckb.logger.filte",), id="unknown-key"),
        pytest.param("ckb.logger.color=maybe", ("ckb.logger.color", "maybe"), id="invalid-type"),
        pytest.param("miner.client.poll_interval", ("miner.client.poll_interval",), id="missing-equals"),
    ],
)
def test_invalid_batch_preserves_both_configs_and_chain_data(
    isolated_devnet: DevnetManager, invalid: str, locators: tuple[str, ...],
) -> None:
    """用户批量改配置时最后一项写错，错误指出问题，前面合法项也不会被部分保存。"""
    devnet = isolated_devnet
    devnet.start()
    tip = devnet.rpc.tip()
    wait_until(
        lambda: devnet.rpc.tip() > tip, timeout_s=30,
        description="the development chain to contain mined blocks before editing",
    )
    devnet.stop()

    config_path, data_path = _configured_devnet_paths(devnet.runner.env)
    configs = {name: (config_path / name).read_bytes() for name in ("ckb.toml", "ckb-miner.toml")}
    node_config = tomllib.loads(configs["ckb.toml"].decode("utf-8"))
    miner_config = tomllib.loads(configs["ckb-miner.toml"].decode("utf-8"))
    next_filter = "error" if node_config["logger"]["filter"] != "error" else "warn"
    next_interval = miner_config["miner"]["client"]["poll_interval"] + 1
    assert any(path.is_file() for path in (data_path / "db").rglob("*")), "a real chain database is required"
    data_before = _data_snapshot(data_path)
    spec_before = (config_path / "specs" / "dev.toml").read_bytes()

    result = devnet.runner.run(
        "devnet", "config",
        "--set", f"ckb.logger.filter={next_filter}",
        "--set", f"miner.client.poll_interval={next_interval}",
        "--set", invalid, check=False, timeout_s=15,
    )

    assert result.returncode != 0, "the invalid batch was reported as successful"
    assert result.stdout == "", "failed configuration edit returned a command result"
    for name, before in configs.items():
        assert (config_path / name).read_bytes() == before, f"the rejected batch changed {name}"
    assert _data_snapshot(data_path) == data_before, "the rejected edit changed chain data"
    assert (config_path / "specs" / "dev.toml").read_bytes() == spec_before
    errors = [event for line in result.stderr.splitlines() if (event := json.loads(line)).get("ok") is False]
    assert len(errors) == 1, f"expected one configuration error: {result.stderr}"
    assert isinstance(errors[0].get("code"), str) and errors[0]["code"], errors[0]
    message = errors[0]["message"]
    assert any(locator in message for locator in locators), (
        f"the batch error does not identify the invalid field or value {invalid!r}: {message}"
    )
