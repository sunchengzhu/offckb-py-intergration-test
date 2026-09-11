from __future__ import annotations

import json
import re

import pytest

from .diagnostic_support import diagnostic_call
from .harness import DevnetManager, wait_until
from .project_support import project_factory


pytestmark = [pytest.mark.core, pytest.mark.project]


def _ckb_log_target(line: str) -> str | None:
    """Read a log record's target without interpreting its message or timestamp."""
    match = re.search(r"\s(?:TRACE|DEBUG|INFO|WARN|ERROR)\s+(\S+)\s", line)
    return match.group(1) if match else None


def _is_script_record(line: str) -> bool:
    target = _ckb_log_target(line)
    return target == "ckb-script" or (target == "ckb_script::verify" and "DEBUG OUTPUT:" in line)


# TEST-MAP: OBS-04
def test_user_can_find_contract_output_and_narrow_real_service_logs(
    diagnostic_call, devnet_manager: DevnetManager,
) -> None:
    """用户运行合约后直接查看后台节点的日志，并按目标、文本和行数缩小范围。"""
    call = diagnostic_call("log-demo", exit_code=0)
    assert call.error is None
    assert devnet_manager.running, "the log lookup must also work while the devnet runs as a daemon"

    logs = call.data_path / "logs"
    node_log = logs / "run.log"
    wait_until(
        lambda: node_log.is_file() and all(marker in node_log.read_text() for marker in call.markers),
        timeout_s=30, description="the real contract output to be flushed to the node log",
    )
    daemon_script = call.cli.run("logs", "script", "--tail", "20", json_mode=False)

    # Read during the ordinary daemon workflow above, then stop the writer so
    # exact line comparisons use stable, genuinely produced service logs.
    devnet_manager.stop()
    source_lines = {}
    for target, filename in (("node", "run.log"), ("miner", "miner.log"), ("rpc", "proxy.log")):
        lines = (logs / filename).read_text(encoding="utf-8").splitlines()
        assert lines, f"the real {target} service did not produce a log"
        source_lines[target] = lines
        tail = min(3, len(lines))
        result = call.cli.run("logs", target, "--tail", tail, json_mode=False)
        assert result.stdout.splitlines() == lines[-tail:], f"logs {target} read the wrong source or tail"

    # The RPC target must help locate this transaction, independently of the
    # script target's contract markers. No fabricated log lines are involved.
    rpc_lines = source_lines["rpc"]
    transaction_lines = [line for line in rpc_lines if call.tx_hash in line]
    assert transaction_lines, "the proxy log lost this contract submission"
    filtered_rpc = call.cli.run(
        "logs", "rpc", "--tail", len(rpc_lines), "--grep", call.tx_hash, json_mode=False,
    )
    assert filtered_rpc.stdout.splitlines() == transaction_lines

    node_lines = source_lines["node"]
    actual_contract_lines = [line for line in node_lines if any(marker in line for marker in call.markers)]
    for marker in call.markers:
        assert marker in daemon_script.stdout, (
            "offckb logs script omitted the contract output while the daemon was running; "
            f"actual node records: {actual_contract_lines!r}; CLI stdout: {daemon_script.stdout!r}"
        )
    # Read enough actual node lines to include all script entries. This avoids
    # reproducing the CLI's script scan-window implementation in the oracle.
    script = call.cli.run("logs", "script", "--tail", len(node_lines), json_mode=False)
    script_lines = script.stdout.splitlines()
    assert script_lines
    assert all(line in node_lines for line in script_lines), "script output did not come from the node log"
    records = [line for line in script_lines if _ckb_log_target(line) is not None]
    assert records and all(_is_script_record(line) for line in records), (
        "logs script included records from another node component"
    )
    non_script_lines = [line for line in node_lines if _ckb_log_target(line) is not None and not _is_script_record(line)]
    assert non_script_lines, "a non-script node record is required to distinguish node and script logs"
    assert not set(non_script_lines).intersection(script_lines)
    for marker in call.markers:
        assert any(marker in line for line in script_lines), f"logs script lost this contract's marker: {marker}"

    tail = call.cli.run("logs", "script", "--tail", "2", json_mode=False)
    assert len(script_lines) >= 2
    assert tail.stdout.splitlines() == script_lines[-2:]

    pattern = call.markers[0]
    expected = [line for line in script_lines[-20:] if pattern in line]
    assert expected and len(expected) < len(script_lines[-20:]), "the grep example needs matching and other lines"
    filtered = call.cli.run("logs", "script", "--tail", "20", "--grep", pattern, json_mode=False)
    assert filtered.stdout.splitlines() == expected
    assert all(call.markers[1] not in line for line in expected)

    machine = call.cli.run("logs", "script", "--tail", "20", "--grep", pattern)
    messages = [json.loads(line)["message"] for line in machine.stderr.splitlines()]
    assert messages == expected, "JSON log reading did not return the same script records"
