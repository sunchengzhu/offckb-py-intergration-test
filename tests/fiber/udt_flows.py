"""UDT preparation and payment through the packaged CLI and public FNN RPC."""
from __future__ import annotations

import json
import struct
import subprocess
from dataclasses import dataclass
from typing import Any

from tests.asset_assertions import assert_udt_balance
from tests.harness import CKB, hex_int, rpc_script, transaction_hash_from, wait_until
from .environment import FiberEnvironment
from .flows import Channel, _key_file, channel_pair, list_channels, settled, unpack_outpoint

ISSUED_UNITS = 30_000
DISTRIBUTED_UNITS = 10_000
FUNDED_UNITS = (5_000, 3_000)
PAID_UNITS = 100


class KnownUdtFundingDefect(AssertionError):
    """A funding timeout supported by the reported issue's node-log evidence."""


def reported_funding_issue(log: str, funded_units: tuple[int, int], funding_fee_rate: int | None) -> str | None:
    # Missing dependencies are a separate, unreported failure, not either fee/change issue.
    if "ScriptNotFound" in log:
        return None
    if 0 < funded_units[1] < DISTRIBUTED_UNITS and "Peer sent us an invalid funding tx" in log:
        return "https://github.com/nervosnetwork/fiber/issues/1667"
    if funding_fee_rate in (None, 1000) and "PoolRejectedTransactionByMinFeeRate" in log and "LowFeeRate" in log:
        return "https://github.com/nervosnetwork/fiber/issues/1663"
    return None


def _amount(data: str) -> int:
    raw = bytes.fromhex(data.removeprefix("0x"))
    assert len(raw) == 16, "UDT amounts must be exact little-endian u128 values"
    return int.from_bytes(raw, "little")


def _configure_chain_scripts(env: FiberEnvironment) -> None:
    result = subprocess.run(
        [str(env.managed_ckb), "list-hashes", "-C", str(env.config_path), "-f", "json"],
        cwd=env.runner.cwd, env=env.runner.env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    chain = next(iter(json.loads(result.stdout).values()))
    scripts = {}
    for cell in chain["system_cells"]:
        name = str(cell["path"]).rstrip(")").replace("\\", "/").rsplit("/", 1)[-1]
        if name in {"sudt", "xudt"}:
            scripts[name] = {
                "code_hash": cell.get("type_hash") or cell["data_hash"],
                "hash_type": "type" if cell.get("type_hash") else "data2", "args": "0x",
            }
    assert set(scripts) == {"sudt", "xudt"}
    env.rpc.configure_udt_scripts(scripts)


@dataclass(frozen=True)
class PreparedUdt:
    kind: str
    script: dict[str, str]
    configs: tuple[bytes, bytes]


def generated_configs(env: FiberEnvironment) -> tuple[bytes, bytes]:
    return tuple((env.config_path / "fiber" / "nodes" / str(i) / "config.yml").read_bytes() for i in (1, 2))


def prepare_udt(env: FiberEnvironment, kind: str, *, batches: int = 1) -> PreparedUdt:
    """Account 19 issues the default asset and distributes exact raw units to FNN accounts.

    ``batches`` splits each account's share into that many transfers, so the account ends up
    holding the same total across several cells.
    """
    assert batches >= 1 and DISTRIBUTED_UNITS % batches == 0, "batches must divide the distribution exactly"
    _configure_chain_scripts(env)
    env.rpc.wait_indexer()
    issuer = env.accounts[19]
    type_args = env.rpc.script_hash(issuer.lock_script)
    script = {**env.rpc.udt_script(kind), "args": type_args}
    participants = (issuer, env.accounts[3], env.accounts[4])
    before = [assert_udt_balance(env.runner, env.rpc, a, kind, type_args) for a in participants]
    assert before == [0, 0, 0], "each UDT case requires its own fresh asset state"
    configs = generated_configs(env)
    issue = env.runner.run(
        "udt", "issue", str(ISSUED_UNITS), "--network", "devnet", "--udt-kind", kind,
        "--privkey-file", _key_file(env, issuer),
    )
    assert issue.json and issue.json["typeArgs"] == type_args and issue.json["kind"] == kind
    issued = env.rpc.wait_transaction(transaction_hash_from(issue))
    env.rpc.wait_indexer(issued["tx_status"]["block_number"])
    issued_outputs = [
        (output, data) for output, data in zip(issued["transaction"]["outputs"], issued["transaction"]["outputs_data"], strict=True)
        if output.get("type") is not None
    ]
    assert issued_outputs and all(rpc_script(o["type"]) == script for o, _ in issued_outputs)
    assert sum(_amount(data) for _, data in issued_outputs) == ISSUED_UNITS
    assert all(rpc_script(o["lock"]) == rpc_script(issuer.lock_script) for o, _ in issued_outputs)
    per_transfer = DISTRIBUTED_UNITS // batches
    for offset, account in enumerate(participants[1:], 1):
        delivered = 0
        for _ in range(batches):
            transfer = env.runner.run(
                "transfer", account.address, str(per_transfer), "--network", "devnet",
                "--udt-kind", kind, "--udt-type-args", type_args, "--privkey-file", _key_file(env, issuer),
            )
            assert transfer.json and transfer.json["typeArgs"] == type_args and transfer.json["kind"] == kind
            sent = env.rpc.wait_transaction(transaction_hash_from(transfer))
            env.rpc.wait_indexer(sent["tx_status"]["block_number"])
            typed = [
                (output, data) for output, data in zip(sent["transaction"]["outputs"], sent["transaction"]["outputs_data"], strict=True)
                if output.get("type") is not None
            ]
            assert typed and all(rpc_script(o["type"]) == script for o, _ in typed)
            assert sum(_amount(data) for o, data in typed if rpc_script(o["lock"]) == rpc_script(account.lock_script)) == per_transfer
            delivered += per_transfer
            assert assert_udt_balance(env.runner, env.rpc, account, kind, type_args) == delivered
        assert delivered == DISTRIBUTED_UNITS
        assert assert_udt_balance(env.runner, env.rpc, account, kind, type_args) == DISTRIBUTED_UNITS
        assert assert_udt_balance(env.runner, env.rpc, issuer, kind, type_args) == ISSUED_UNITS - offset * DISTRIBUTED_UNITS
    for node, account in zip(env.fnn, participants[1:], strict=True):
        entries = [entry for entry in node.call("node_info")["udt_cfg_infos"] if entry["name"] == kind]
        assert len(entries) == 1
        whitelist = entries[0]
        assert whitelist["script"] == {**script, "args": "^" + type_args + "$"}
        assert whitelist.get("auto_accept_amount") is None, "the default UDT flow requires manual acceptance"
        assert whitelist["cell_deps"], "OffCKB must generate the UDT contract dependency"
        for dep in whitelist["cell_deps"]:
            point = dep["cell_dep"]["out_point"]
            assert env.rpc.get_live_cell(point["tx_hash"], hex_int(point["index"]))["status"] == "live"
        plain_capacity = sum(
            hex_int(cell["output"]["capacity"]) for cell in env.rpc.live_cells(account.lock_script)
            if cell["output"].get("type") is None
        )
        assert plain_capacity >= 1_000 * CKB, "UDT does not replace CKB capacity needed to fund the channel"
    assert generated_configs(env) == configs
    return PreparedUdt(kind, script, configs)


def open_udt_channel(
    env: FiberEnvironment, asset: PreparedUdt, *, funded_units: tuple[int, int] = FUNDED_UNITS,
    funding_fee_rate: int | None = None,
) -> Channel:
    assert all(not list_channels(node) for node in env.fnn), "unexpected alternate payment route"
    infos = [node.call("node_info") for node in env.fnn]
    request: dict[str, Any] = {
        "pubkey": infos[1]["pubkey"], "funding_amount": hex(funded_units[0]),
        "funding_udt_type_script": asset.script,
    }
    if funding_fee_rate is not None:
        request["funding_fee_rate"] = hex(funding_fee_rate)
    result = env.fnn[0].call("open_channel", [request])
    temporary_id = result["temporary_channel_id"]
    pending = None

    def request_visible() -> bool:
        nonlocal pending
        matches = [c for c in env.fnn[1].call("list_channels", [{"only_pending": True}])["channels"] if c["channel_id"] == temporary_id]
        assert len(matches) <= 1
        pending = matches[0] if matches else None
        return pending is not None

    wait_until(request_visible, timeout_s=env.timeout_s, description="receiver to expose the pending UDT request")
    assert pending is not None and pending["is_acceptor"] is True
    assert pending["state"]["state_name"] == "NegotiatingFunding"
    assert rpc_script(pending["funding_udt_type_script"]) == asset.script
    assert hex_int(pending["remote_balance"]) == funded_units[0]
    assert pending["channel_outpoint"] is None
    accepted = env.fnn[1].call("accept_channel", [{
        "temporary_channel_id": temporary_id, "funding_amount": hex(funded_units[1]),
    }])
    channel_id = accepted["channel_id"]
    assert channel_id != temporary_id
    pair = None

    def ready() -> bool:
        nonlocal pair
        pair = channel_pair(env, channel_id)
        if pair is None:
            return False
        assert all(c["state"]["state_name"] != "Closed" for c in pair), pair
        return all(c["state"]["state_name"] == "ChannelReady" and settled(c) for c in pair)

    try:
        wait_until(ready, timeout_s=env.timeout_s, description="both UDT channels to reach ChannelReady")
    except AssertionError as error:
        # Only a funding timeout with healthy RPC and specific node-log evidence is expected.
        # Other assertions, connection errors, setup and teardown failures remain real failures.
        if str(error).startswith("Timed out waiting for both UDT channels to reach ChannelReady") and "; last error:" not in str(error):
            env.rpc.call("get_tip_block_number")
            for node in env.fnn:
                node.call("node_info")
            log = "\n".join(path.read_text(errors="replace") for path in
                            (env.config_path / "fiber" / "nodes").glob("*/fnn.log"))
            issue = reported_funding_issue(log, funded_units, funding_fee_rate)
            if issue:
                raise KnownUdtFundingDefect(f"{issue}: {error}") from error
        raise
    assert pair is not None
    assert tuple(hex_int(c["local_balance"]) for c in pair) == funded_units
    assert tuple(hex_int(c["remote_balance"]) for c in pair) == funded_units[::-1]
    assert all(rpc_script(c["funding_udt_type_script"]) == asset.script for c in pair)
    point = unpack_outpoint(pair[0]["channel_outpoint"])
    assert unpack_outpoint(pair[1]["channel_outpoint"]) == point
    funding = env.rpc.wait_transaction(point["tx_hash"])
    env.rpc.wait_indexer(funding["tx_status"]["block_number"])
    index = hex_int(point["index"])
    assert rpc_script(funding["transaction"]["outputs"][index]["type"]) == asset.script
    assert _amount(funding["transaction"]["outputs_data"][index]) == sum(funded_units)
    for account, funded in zip(env.accounts[3:5], funded_units, strict=True):
        assert assert_udt_balance(env.runner, env.rpc, account, asset.kind, asset.script["args"]) == DISTRIBUTED_UNITS - funded
    assert generated_configs(env) == asset.configs, "opening must not rewrite the managed whitelist"
    return Channel(channel_id, temporary_id, point, funding, funded_units, pair)


def _molecule_script(script: dict[str, str]) -> str:
    code_hash = bytes.fromhex(script["code_hash"][2:])
    args = bytes.fromhex(script["args"][2:])
    hash_type = bytes([{"data": 0, "type": 1, "data1": 2, "data2": 4}[script["hash_type"]]])
    return "0x" + (struct.pack("<IIII", 53 + len(args), 16, 48, 49) + code_hash + hash_type + struct.pack("<I", len(args)) + args).hex()


def pay_udt_invoice(env: FiberEnvironment, asset: PreparedUdt, channel: Channel) -> None:
    before = channel_pair(env, channel.channel_id)
    assert before is not None and all(settled(c) for c in before)
    expected = (hex_int(before[0]["local_balance"]) - PAID_UNITS, hex_int(before[1]["local_balance"]) + PAID_UNITS)
    invoice = env.fnn[1].call("new_invoice", [{
        "amount": hex(PAID_UNITS), "currency": "Fibd", "udt_type_script": asset.script,
    }])
    assert hex_int(invoice["invoice"]["amount"]) == PAID_UNITS
    assert invoice["invoice"]["currency"] == "Fibd"
    assert {"udt_script": _molecule_script(asset.script)} in invoice["invoice"]["data"]["attrs"]
    payment_hash = invoice["invoice"]["data"]["payment_hash"]
    sent = env.fnn[0].call("send_payment", [{"invoice": invoice["invoice_address"]}])
    assert sent["payment_hash"] == payment_hash

    def paid() -> bool:
        payment = env.fnn[0].call("get_payment", [{"payment_hash": payment_hash}])
        receipt = env.fnn[1].call("get_invoice", [{"payment_hash": payment_hash}])
        assert payment["status"] != "Failed", payment
        assert receipt["status"] not in {"Cancelled", "Expired"}, receipt
        pair = channel_pair(env, channel.channel_id)
        assert pair is not None and all(c["state"]["state_name"] == "ChannelReady" for c in pair)
        assert all(rpc_script(c["funding_udt_type_script"]) == asset.script for c in pair)
        return (
            payment["status"] == "Success" and receipt["status"] == "Paid"
            and tuple(hex_int(c["local_balance"]) for c in pair) == expected
            and tuple(hex_int(c["remote_balance"]) for c in pair) == expected[::-1]
            and all(settled(c) for c in pair)
        )

    wait_until(paid, timeout_s=env.timeout_s, description="100 raw UDT units to settle with exact balance changes")
    assert generated_configs(env) == asset.configs
