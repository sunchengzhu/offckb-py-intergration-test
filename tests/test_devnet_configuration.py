from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from .harness import (
    Account, DevnetManager, _configured_devnet_paths, hex_int,
    transaction_hash_from, wait_until,
)


pytestmark = pytest.mark.core

LOG_LEVEL = re.compile(r"\s(TRACE|DEBUG|INFO|WARN|ERROR)\s+\S+\s")


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
