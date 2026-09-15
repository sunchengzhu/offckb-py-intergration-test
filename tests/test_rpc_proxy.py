from __future__ import annotations

import json
import re
import urllib.request
import uuid
from pathlib import Path

import pytest

from .harness import Account, DevnetManager, OffckbRunner, RpcClient, _configured_devnet_paths, wait_until


pytestmark = pytest.mark.core


def _response(rpc: RpcClient, method: str) -> dict:
    """Read the complete public RPC envelope so forwarding preserves errors and IDs."""
    request = urllib.request.Request(
        rpc.url, data=json.dumps({"jsonrpc": "2.0", "id": "offckb-proxy-check", "method": method, "params": []}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _rpc_log(offckb: OffckbRunner) -> Path:
    _, data = _configured_devnet_paths(offckb.env)
    return data / "logs" / "proxy.log"


def _new_lines(path: Path, offset: int) -> list[str]:
    with path.open("rb") as stream:
        stream.seek(offset)
        return stream.read().decode("utf-8").splitlines()


# TEST-MAP: OBS-01
def test_proxy_connects_to_the_same_local_chain_and_records_queries(
    devnet: DevnetManager, offckb: OffckbRunner, rpc: RpcClient, proxy_rpc: RpcClient,
) -> None:
    """项目连接 proxy 后看到同一节点与开发链，并可查到本次查询记录。"""
    log = _rpc_log(offckb)
    offset = log.stat().st_size
    direct = rpc.call("local_node_info")
    proxied = proxy_rpc.call("local_node_info")
    assert proxied["node_id"] == direct["node_id"] and proxied["version"] == direct["version"]
    assert proxy_rpc.call("get_block_hash", ["0x0"]) == rpc.call("get_block_hash", ["0x0"])
    tip = proxy_rpc.call("get_tip_header")
    assert tip == rpc.call("get_header", [tip["hash"]])
    methods = ("local_node_info", "get_block_hash", "get_tip_header")
    wait_until(
        lambda: all(any(f"request {method}" in line for line in _new_lines(log, offset)) for method in methods),
        timeout_s=10, description="the proxy to record this project's queries",
    )
    shown = offckb.run("logs", "rpc", "--tail", "100", json_mode=False)
    assert all(f"request {method}" in shown.stdout for method in methods)


# TEST-MAP: OBS-02
def test_successful_proxy_submission_keeps_the_confirmed_transaction_and_log(
    devnet: DevnetManager, offckb: OffckbRunner, rpc: RpcClient,
    accounts: list[Account], private_key_file,
) -> None:
    """用户提交成功后能按哈希找回实际交易以及代理提交记录。"""
    rpc.wait_indexer()
    result = offckb.run(
        "transfer", accounts[5].address, "100", "--proxy-rpc",
        "--privkey-file", private_key_file(accounts[4]),
    )
    tx_hash = result.json["txHash"]
    committed = rpc.wait_transaction(tx_hash)["transaction"]
    assert committed["hash"] == tx_hash
    config, _ = _configured_devnet_paths(offckb.env)
    cached = config / "transactions" / f"{tx_hash}.json"
    recorded = None

    def cache_written() -> bool:
        nonlocal recorded
        recorded = json.loads(cached.read_text())
        return isinstance(recorded, dict)

    wait_until(cache_written, timeout_s=10, description="the proxy's complete transaction record")
    # SDK requests may omit the optional type script; CKB returns it as null.
    for output in recorded["outputs"]:
        output.setdefault("type", None)
    assert recorded == {key: value for key, value in committed.items() if key != "hash"}
    log = _rpc_log(offckb)
    wait_until(
        lambda: f"send_transaction {tx_hash}" in log.read_text(),
        timeout_s=10, description="the proxy's submission event for this transaction",
    )
    shown = offckb.run("logs", "rpc", "--grep", tx_hash, "--tail", "100", json_mode=False)
    assert f"send_transaction {tx_hash}" in shown.stdout


# TEST-MAP: OBS-03
def test_rpc_error_is_forwarded_logged_and_can_be_corrected_without_restart(
    devnet: DevnetManager, offckb: OffckbRunner, rpc: RpcClient, proxy_rpc: RpcClient,
) -> None:
    """项目方法名写错后能看到原始错误与日志，修正请求即可继续开发。"""
    method = "offckb_missing_" + uuid.uuid4().hex
    log = _rpc_log(offckb)
    offset = log.stat().st_size
    expected = _response(rpc, method)
    actual = _response(proxy_rpc, method)
    assert expected["error"]["code"] == -32601 and actual == expected
    error = actual["error"]
    wait_until(
        lambda: any(f"error [{error['code']}] {error['message']}" in line for line in _new_lines(log, offset)),
        timeout_s=10, description="the proxy to log the original method error",
    )
    shown = offckb.run("logs", "rpc", "--tail", "100", json_mode=False)
    assert f"request {method}" in shown.stdout
    assert f"error [{error['code']}] {error['message']}" in shown.stdout
    assert proxy_rpc.call("get_block_hash", ["0x0"]) == rpc.call("get_block_hash", ["0x0"])


# TEST-MAP: OBS-05
def test_control_characters_cannot_forge_proxy_log_records(
    devnet: DevnetManager, offckb: OffckbRunner, rpc: RpcClient, proxy_rpc: RpcClient,
) -> None:
    """异常方法名中的控制字符不能伪造额外日志，代理仍可继续查询。"""
    marker = uuid.uuid4().hex
    method = f"invalid_{marker}\nrequest FORGED_{marker}\terror FALSE\r\x00\x1b\x7f\x85"
    log = _rpc_log(offckb)
    offset = log.stat().st_size
    expected = _response(rpc, method)
    assert "error" in expected and _response(proxy_rpc, method) == expected
    wait_until(
        lambda: any("error [" in line for line in _new_lines(log, offset)),
        timeout_s=10, description="the proxy to finish logging the control-character request",
    )
    lines = _new_lines(log, offset)
    assert len(lines) == 2, f"one request and one error must occupy two physical lines: {lines!r}"
    assert " request " in lines[0] and " error [" in lines[1]
    assert not any(re.search(r"[\x00-\x1f\x7f-\x9f]", line) for line in lines)
    assert f"invalid_{marker}" in lines[0] and f"FORGED_{marker}" in lines[0]
    shown = offckb.run("logs", "rpc", "--tail", "2", json_mode=False)
    assert shown.stdout.splitlines() == lines
    assert proxy_rpc.ready()
