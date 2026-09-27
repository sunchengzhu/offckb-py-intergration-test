"""用户取得 canary 新增 Fiber 合约的真实 devnet 引用。"""

from __future__ import annotations

import hashlib
import json

import pytest

from tests.harness import dep_group_members
from tests.test_system_scripts import _assert_script_reference, _read_displayed_scripts, _reference, _out_point


pytestmark = pytest.mark.fiber


# TEST-MAP: SYS-03
def test_canary_fiber_scripts_are_discoverable_and_match_genesis(fiber_environment) -> None:
    """查看和保存的完整名称、引用一致，且依赖确实部署在本轮 genesis。"""
    with fiber_environment("attached-daemon", start=False) as env:
        env.start_existing_chain()
        displayed = _read_displayed_scripts(
            env.runner.run("system-scripts", "--network", "devnet").stderr,
        )
        destination = env.runner.cwd / "my project" / "fiber scripts.json"
        destination.parent.mkdir()
        env.runner.run("system-scripts", "--network", "devnet", "--output", destination)
        assert destination.is_file() and destination.stat().st_size > 0
        saved = json.loads(destination.read_text())
        assert isinstance(saved, dict) and {"devnet", "testnet", "mainnet"} <= saved.keys()
        genesis_hash = env.rpc.call("get_block_hash", ["0x0"])
        genesis = env.rpc.call("get_block", [genesis_hash])
        transactions = {tx["hash"]: tx for tx in genesis["transactions"]}
        names = ("auth", "funding_lock", "commitment_lock")
        references = []
        for name in names:
            assert name in displayed and name in saved["devnet"], f"missing complete Fiber script name: {name}"
            script = displayed[name]
            assert _reference(script) == _reference(saved["devnet"][name]["script"]), (
                f"saved devnet reference disagrees with the displayed {name}"
            )
            _assert_script_reference(env.rpc, name, script)
            code_digests = set()
            for dependency in script["cellDeps"]:
                point = _out_point(dependency["cellDep"]["outPoint"])
                assert point["tx_hash"] in transactions, f"{name} dependency does not belong to this genesis"
                assert int(point["index"], 16) < len(transactions[point["tx_hash"]]["outputs"])
                data = transactions[point["tx_hash"]]["outputs_data"][int(point["index"], 16)]
                members = dep_group_members(data) if dependency["cellDep"]["depType"] == "depGroup" else [point]
                for member in members:
                    assert member["tx_hash"] in transactions, f"{name} code does not belong to this genesis"
                    content = transactions[member["tx_hash"]]["outputs_data"][int(member["index"], 16)]
                    code_digests.add(hashlib.sha256(bytes.fromhex(content.removeprefix("0x"))).hexdigest())
            contract = env.config_path / "specs" / "fiber" / name
            assert contract.is_file() and contract.stat().st_size > 0, f"missing initialized contract file: {name}"
            assert hashlib.sha256(contract.read_bytes()).hexdigest() in code_digests, (
                f"{name} references do not contain its own contract code"
            )
            references.append((script["codeHash"], script["hashType"]))
        assert len(set(references)) == len(names), "different Fiber script names alias the same contract reference"
