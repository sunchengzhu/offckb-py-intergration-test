from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harness import Account, OffckbRunner, RpcClient, _configured_devnet_paths, hex_int


def assert_cli_failure(result: Any) -> str:
    """失败必须结构化，且不能同时输出成功结果或交易哈希字段。"""
    assert result.returncode != 0, "invalid operation unexpectedly succeeded"
    assert result.stdout.strip() == "", "failed command emitted a result on stdout"
    events = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    assert events and all(isinstance(event, dict) for event in events)
    failures = [event for event in events if event.get("ok") is False]
    assert len(failures) == 1, "expected one structured command error"
    assert isinstance(failures[0].get("code"), str) and failures[0]["code"]
    assert isinstance(failures[0].get("message"), str) and failures[0]["message"]
    assert not any(event.get("ok") is True or "txHash" in event or "tx_hash" in event for event in events)
    return failures[0]["message"]


def _submission_records(offckb: OffckbRunner) -> tuple[dict[str, bytes], int]:
    config_path, data_path = _configured_devnet_paths(offckb.env)
    transactions = config_path / "transactions"
    records = {path.name: path.read_bytes() for path in transactions.glob("*.json")}
    proxy_log = data_path / "logs" / "proxy.log"
    assert proxy_log.is_file(), "running devnet must expose its real proxy log"
    submissions = sum(
        " send_transaction " in line for line in proxy_log.read_text(encoding="utf-8").splitlines()
    )
    return records, submissions


@dataclass(frozen=True)
class AssetState:
    accounts: tuple[Account, ...]
    live_cells: tuple[list[dict[str, Any]], ...]
    balances: tuple[dict[str, Any], ...]
    submissions: tuple[dict[str, bytes], int]


def capture_asset_state(offckb: OffckbRunner, rpc: RpcClient, *accounts: Account) -> AssetState:
    rpc.wait_indexer()
    cells = tuple(rpc.live_cells(account.lock_script) for account in accounts)
    balances = []
    for account in accounts:
        result = offckb.run("balance", account.address, "--network", "devnet")
        assert result.json is not None and result.json.get("ok") is True
        assert result.json.get("address") == account.address
        balances.append(result.json)
    return AssetState(tuple(accounts), cells, tuple(balances), _submission_records(offckb))


def assert_asset_state_unchanged(offckb: OffckbRunner, rpc: RpcClient, before: AssetState) -> None:
    after = capture_asset_state(offckb, rpc, *before.accounts)
    assert after.submissions == before.submissions, "failed operation submitted a transaction through the proxy"
    assert after.live_cells == before.live_cells, "failed operation changed account live cells"
    assert after.balances == before.balances, "failed operation changed user-visible CKB or UDT balances"
    # Query the node as well as the Indexer, so stale indexed cells cannot hide consumption.
    for cells in before.live_cells:
        for cell in cells:
            point = cell["out_point"]
            live = rpc.get_live_cell(point["tx_hash"], hex_int(point["index"]))
            assert live["status"] == "live", "failed operation consumed an original cell"
            assert live["cell"]["output"] == cell["output"]
            assert live["cell"]["data"]["content"] == cell["output_data"]


def temporary_devnet_account(case: str, key_path: Path, offckb: OffckbRunner) -> Account:
    """独立于 genesis 的确定性 devnet fixture；私钥仅写入受限文件。"""
    fixtures = {
        "sweep": (
            2,
            "02c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5",
            "a3c778981c19e1dcc611fb2132dcdaac075a5064",
            "ckt1qzda0cr08m85hc8jlnfp3zer7xulejywt49kt2rr0vthywaa50xwsqdrcaufs8qeu8wvvy0myyedek4vqad9qeq3gc4cf",
        ),
        "unfunded": (
            3,
            "02f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9",
            "67f08e2153e10e8d4f358b6090b44bd6a9745a6d",
            "ckt1qzda0cr08m85hc8jlnfp3zer7xulejywt49kt2rr0vthywaa50xwsqt87z8zz5lpp6x57dvtvzgtgj7k49695mg00tsje",
        ),
    }
    scalar, pubkey, args, address = fixtures[case]
    private_key = "0x" + scalar.to_bytes(32, "big").hex()
    offckb.register_secret(private_key)
    key_path.touch(mode=0o600)
    key_path.write_text(private_key + "\n", encoding="utf-8")
    assert key_path.stat().st_mode & 0o077 == 0
    return Account(
        index=-scalar, address=address, private_key=private_key, pubkey="0x" + pubkey,
        lock_script={
            "code_hash": "0x9bd7e06f3ecf4be0f2fcd2188b23f1b9fcc88e5d4b65a8637b17723bbda3cce8",
            "hash_type": "type", "args": "0x" + args,
        },
    )
