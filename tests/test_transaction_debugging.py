from __future__ import annotations

import json
import re
from typing import Any

import pytest

from .diagnostic_support import DiagnosticCall, diagnostic_call
from .harness import RpcClient, dep_group_members, hex_int, wait_until
from .project_support import project_factory


pytestmark = [pytest.mark.core, pytest.mark.project]


def _assert_resolved_cell(rpc: RpcClient, point: dict[str, str], item: dict[str, Any]) -> None:
    previous = rpc.call("get_transaction", [point["tx_hash"]])
    assert previous and previous["tx_status"]["status"] == "committed"
    index = hex_int(point["index"])
    assert item["output"] == previous["transaction"]["outputs"][index]
    assert item["data"] == previous["transaction"]["outputs_data"][index]


def _assert_debug_context(call: DiagnosticCall, rpc: RpcClient, dump: dict[str, Any]) -> None:
    assert dump["tx"] == call.transaction, "debugger received a different transaction"
    inputs = dump["mock_info"]["inputs"]
    assert [item["input"] for item in inputs] == call.transaction["inputs"]
    for item in inputs:
        _assert_resolved_cell(rpc, item["input"]["previous_output"], item)
    deps = dump["mock_info"]["cell_deps"]
    for required in call.transaction["cell_deps"]:
        matches = [item for item in deps if item["cell_dep"] == required]
        assert len(matches) == 1, f"debug dump lost or duplicated a transaction dependency: {required}"
        if required["dep_type"] == "dep_group":
            for member in dep_group_members(matches[0]["data"]):
                assert any(
                    item["cell_dep"] == {"out_point": member, "dep_type": "code"} for item in deps
                ), f"debug dump did not resolve a dependency-group member: {member}"
    # Includes the dep-group code cells resolved by OffCKB itself, when present.
    for item in deps:
        _assert_resolved_cell(rpc, item["cell_dep"]["out_point"], item)


# TEST-MAP: DBG-01
def test_rejected_contract_transaction_can_be_debugged_by_its_hash(diagnostic_call, rpc: RpcClient) -> None:
    """合约调用失败后，用户只提供交易哈希即可看到自己的脚本输出和失败原因。"""
    call = diagnostic_call("failed-contract", exit_code=42)
    assert call.error is not None, "the deliberately failing contract transaction was accepted"
    assert "ValidationFailure" in call.error and re.search(r"\berror code\s+42\b", call.error), call.error
    assert re.search(r"\bOutputs?\[0\]\.Type\b", call.error), call.error
    cached = call.config_path / "transactions" / f"{call.tx_hash}.json"
    full = call.config_path / "full-transactions" / f"{call.tx_hash}.json"
    wait_until(cached.is_file, timeout_s=10, description="the proxy to retain the rejected transaction")
    assert json.loads(cached.read_text()) == call.transaction
    assert not full.exists(), "the test must let OffCKB prepare the debugging context"

    result = call.cli.run("debug", "--tx-hash", call.tx_hash, "--network", "devnet", json_mode=False, check=False)
    output = result.stdout + result.stderr
    # Earlier input locks succeed; only the output type is our failing contract.
    assert "Output[0].Type" in output, output
    for index in range(len(call.transaction["inputs"])):
        assert f"Input[{index}].Lock" in output, output
        lock_output = output.split(f"Input[{index}].Lock", 1)[1]
        lock_output = re.split(r"(?:Input|Output)\[\d+\]\.(?:Lock|Type)", lock_output, maxsplit=1)[0]
        assert re.search(r"Run result:\s*0\b", lock_output), "OffCKB could not debug the real input lock"
    group = output.split("Output[0].Type", 1)[1]
    for marker in call.markers:
        assert marker in group, "debug output does not belong to the submitted contract"
    assert re.search(r"Run result:\s*42\b", group), group
    _assert_debug_context(call, rpc, json.loads(full.read_text()))
    assert json.loads(cached.read_text()) == call.transaction
    # Rejection must leave the original inputs available to the user.
    for input_cell in call.transaction["inputs"]:
        assert rpc.call("get_live_cell", [input_cell["previous_output"], False])["status"] == "live"
