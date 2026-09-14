from __future__ import annotations

import json
import re
import subprocess
from decimal import Decimal

import pytest

from .harness import (
    DEVNET_PORTS, Account, CommandResult, DevnetManager, OffckbRunner, RpcClient,
    _command_references_path, _configured_devnet_paths, is_port_open,
)


pytestmark = pytest.mark.core


# TEST-MAP: CLI-01
def test_cli_version_matches_installed_package(offckb: OffckbRunner) -> None:
    """用户安装后查到的 OffCKB 版本与实际安装包一致。"""
    package_path = offckb.cli_entry.parent.parent / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))

    result = offckb.run("--version", json_mode=False, timeout_s=15)

    assert result.stdout.strip() == package["version"], (
        f"offckb --version must match the installed package at {package_path}"
    )


def _assert_no_devnet_started(devnet: DevnetManager) -> None:
    config, data = _configured_devnet_paths(devnet.runner.env)
    assert not config.exists() and not data.exists(), "the CLI unexpectedly initialized a devnet"
    assert not any(is_port_open(port) for port in DEVNET_PORTS)
    processes = subprocess.run(
        ["ps", "-axo", "pid=,command="], capture_output=True, text=True, check=True, timeout=5,
    ).stdout
    assert not any(
        _command_references_path(line, devnet.runner.cli_entry)
        for line in processes.splitlines()
    ), "the command left an OffCKB process behind"


def _error(result: CommandResult, code: str) -> dict:
    assert result.returncode != 0
    assert result.stdout == "", "a failed command must not publish a success result"
    records = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    assert len(records) == 1, "the command must emit exactly one error record"
    assert records[0]["ok"] is False and records[0]["code"] == code
    assert isinstance(records[0].get("message"), str) and records[0]["message"]
    return records[0]


# TEST-MAP: CLI-02
@pytest.mark.parametrize("arguments", [(), ("--help",)], ids=["no-command", "help"])
def test_first_time_help_discovers_commands_without_starting_a_chain(
    uninitialized_devnet: DevnetManager, arguments: tuple[str, ...],
) -> None:
    """首次查看帮助能找到开发入口，不会因此启动开发链。"""
    devnet = uninitialized_devnet
    result = devnet.runner.run(*arguments, json_mode=False, timeout_s=15)
    for entry in ("Usage:", "Options:", "Commands:", "--version", "--json"):
        assert entry in result.stdout
    for command in ("node", "create", "accounts", "balance", "deploy", "debug", "config"):
        assert re.search(rf"^\s+{command}\b", result.stdout, re.MULTILINE), command
    _assert_no_devnet_started(devnet)


# TEST-MAP: CLI-03
def test_scripted_transfer_and_balance_have_one_result_and_json_progress(
    devnet: DevnetManager, offckb: OffckbRunner, rpc: RpcClient,
    accounts: list[Account], private_key_file,
) -> None:
    """用户脚本可直接解析转账结果和到账余额，无需从进度日志抓取结果。"""
    sender, receiver = accounts[3], accounts[4]
    rpc.wait_indexer()
    before = rpc.ckb_balance(receiver.lock_script)
    transfer = offckb.run(
        "transfer", receiver.address, "100", "--network", "devnet",
        "--privkey-file", private_key_file(sender),
    )
    receipt = json.loads(transfer.stdout)
    assert receipt["ok"] is True and receipt["command"] == "transfer"
    assert receipt["toAddress"] == receiver.address and str(receipt["amount"]) == "100"
    committed = rpc.wait_transaction(receipt["txHash"])
    rpc.wait_indexer(committed["tx_status"]["block_number"])
    assert rpc.ckb_balance(receiver.lock_script) == before + 100 * 100_000_000
    balance = offckb.run("balance", receiver.address, "--no-udt")
    value = json.loads(balance.stdout)
    assert value["ok"] is True and value["command"] == "balance"
    assert value["address"] == receiver.address and value["network"] == "devnet"
    assert value["udt"] == []
    assert Decimal(str(value["ckb"])) * 100_000_000 == before + 100 * 100_000_000
    progress = []
    for result in (transfer, balance):
        records = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
        assert all(isinstance(record, dict) and "message" in record for record in records)
        assert all(record.get("ok") is not True for record in records)
        progress.extend(records)
    assert progress, "exercise a command that actually emits progress"
    assert any(receipt["txHash"] in event["message"] for event in progress)


# TEST-MAP: CLI-04
def test_global_json_flag_works_before_between_and_after_nested_commands(
    isolated_devnet: DevnetManager,
) -> None:
    """用户调整全局参数位置，不会改变同一嵌套命令的输出模式或结果。"""
    results = [
        isolated_devnet.runner.run(*args, json_mode=False)
        for args in (("--json", "node", "stop"), ("node", "--json", "stop"), ("node", "stop", "--json"))
    ]
    parsed = [json.loads(result.stdout) for result in results]
    assert all(value == parsed[0] for value in parsed)
    assert parsed[0]["ok"] is True and parsed[0]["command"] == "node.stop"
    assert parsed[0]["stopped"] is False
    for result in results:
        assert all(isinstance(json.loads(line), dict) for line in result.stderr.splitlines() if line.strip())


# TEST-MAP: CLI-05
def test_success_without_a_specialized_result_emits_a_completion_object(offckb: OffckbRunner) -> None:
    """读取设置的脚本也能取得明确的成功完成信号。"""
    result = offckb.run("config", "get", "ckb-version")
    assert json.loads(result.stdout) == {"ok": True, "command": "config", "completed": True}
    assert [json.loads(line) for line in result.stderr.splitlines() if line.strip()]


# TEST-MAP: CLI-06
@pytest.mark.parametrize(
    ("arguments", "code", "locator"),
    [
        pytest.param(("balance",), "commander.missingArgument", "toAddress", id="missing-argument"),
        pytest.param(("node", "--not-an-option"), "commander.unknownOption", "--not-an-option", id="unknown-option"),
        pytest.param(("logs", "--tail", "zero"), "commander.invalidArgument", "--tail", id="invalid-value"),
    ],
)
def test_argument_errors_do_not_run_commands_or_emit_duplicate_errors(
    uninitialized_devnet: DevnetManager, arguments: tuple[str, ...], code: str, locator: str,
) -> None:
    """参数写错时立即得到一个结构化错误，业务命令不会执行。"""
    result = uninitialized_devnet.runner.run(*arguments, check=False, timeout_s=15)
    error = _error(result, code)
    assert locator in error["message"]
    _assert_no_devnet_started(uninitialized_devnet)


# TEST-MAP: CLI-07
@pytest.mark.parametrize("reason", ["missing-key", "invalid-network"])
def test_business_errors_are_structured_and_do_not_disclose_keys_or_stacks(
    uninitialized_devnet: DevnetManager, accounts: list[Account], private_key_file, reason: str,
) -> None:
    """凭据缺失或业务输入无效时，脚本收到失败结果且日志不泄漏敏感信息。"""
    runner = uninitialized_devnet.runner
    runner.env.pop("OFFCKB_PRIVATE_KEY", None)
    args = ["transfer", accounts[4].address, "100"]
    if reason == "invalid-network":
        args += ["--network", "not-a-network", "--privkey-file", private_key_file(accounts[3])]
    result = runner.run(*args, check=False, timeout_s=15)
    assert result.returncode == 1
    error = _error(result, "COMMAND_FAILED")
    assert re.search("privkey|private.key" if reason == "missing-key" else "network", error["message"], re.I)
    assert not re.search(r'"stack"\s*:|(?:\\n|\n)\s+at\s', result.stderr), "internal stack trace leaked"
    leaked = any(
        secret in result.stdout + result.stderr
        for account in accounts for secret in (account.private_key, account.private_key.removeprefix("0x"))
    )
    assert not leaked, "private key leaked"
    _assert_no_devnet_started(uninitialized_devnet)
