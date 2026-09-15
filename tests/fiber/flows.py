"""Real FNN/CKB flow helpers; all observations use public RPC and the packaged CLI."""

from __future__ import annotations

import os
import struct
import time
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator, Protocol

from ..harness import (
    CKB, Account, OffckbRunner, RpcClient, hex_int, rpc_script, transaction_hash_from,
)


FUNDING_AMOUNT = 10_000 * CKB
PAYMENT_AMOUNT = 100 * CKB


class FiberEnvironment(Protocol):
    runner: OffckbRunner
    rpc: RpcClient
    fnn: tuple[RpcClient, RpcClient]
    accounts: list[Account]
    timeout_s: float
    root: Path
    mode: str


@dataclass
class Channel:
    channel_id: str
    temporary_id: str
    outpoint: dict[str, str]
    funding_transaction: dict[str, Any]
    principal: tuple[int, int]
    initial: tuple[dict[str, Any], dict[str, Any]]
    paid: int = 0
    closed: bool = False


def outpoint_key(point: dict[str, Any]) -> tuple[str, int]:
    return str(point["tx_hash"]).lower(), hex_int(point["index"])


def unpack_outpoint(value: str) -> dict[str, str]:
    """FNN serializes a Molecule OutPoint as 32 hash bytes + little-endian u32."""
    assert isinstance(value, str) and value.startswith("0x"), "missing channel outpoint"
    encoded = bytes.fromhex(value[2:])
    assert len(encoded) == 36, f"expected 36-byte channel outpoint, got {len(encoded)}"
    return {"tx_hash": "0x" + encoded[:32].hex(), "index": hex(struct.unpack("<I", encoded[32:])[0])}


def list_channels(rpc: RpcClient, *, include_closed: bool = False) -> list[dict[str, Any]]:
    result = rpc.call("list_channels", [{"include_closed": include_closed}])
    assert isinstance(result, dict) and isinstance(result.get("channels"), list), "invalid list_channels result"
    return result["channels"]


def channel_pair(env: FiberEnvironment, channel_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    pair = []
    for rpc in env.fnn:
        matches = [c for c in list_channels(rpc, include_closed=True) if c["channel_id"] == channel_id]
        assert len(matches) <= 1, f"duplicate channel {channel_id}"
        if not matches:
            return None
        pair.append(matches[0])
    return pair[0], pair[1]


def settled(channel: dict[str, Any]) -> bool:
    return (
        channel["pending_tlcs"] == []
        and hex_int(channel["offered_tlc_balance"]) == 0
        and hex_int(channel["received_tlc_balance"]) == 0
    )


def _summary(pair: tuple[dict[str, Any], dict[str, Any]] | None) -> Any:
    if pair is None:
        return None
    return [
        {key: c.get(key) for key in ("channel_id", "state", "local_balance", "remote_balance", "failure_detail")}
        for c in pair
    ]


def open_ckb_channel(env: FiberEnvironment) -> Channel:
    # Each case owns its channel, so an earlier test cannot supply a payment route.
    assert all(not list_channels(rpc) for rpc in env.fnn), "a previous active channel would make routing ambiguous"
    previous = [{c["channel_id"] for c in list_channels(rpc, include_closed=True)} for rpc in env.fnn]
    info = [rpc.call("node_info") for rpc in env.fnn]
    principal = (FUNDING_AMOUNT, hex_int(info[1]["auto_accept_channel_ckb_funding_amount"]))
    assert principal[1] > 0, "default receiver must automatically accept the CKB channel"
    response = env.fnn[0].call("open_channel", [{"pubkey": info[1]["pubkey"], "funding_amount": hex(FUNDING_AMOUNT)}])
    temporary_id = response["temporary_channel_id"]
    assert isinstance(temporary_id, str) and len(temporary_id) == 66
    deadline = time.monotonic() + env.timeout_s
    pair = None
    while time.monotonic() < deadline:
        candidates = [c for c in list_channels(env.fnn[0], include_closed=True) if c["channel_id"] not in previous[0]]
        assert len(candidates) <= 1, "one open request created multiple channels"
        if candidates:
            channel_id = candidates[0]["channel_id"]
            pair = channel_pair(env, channel_id)
            if pair is not None:
                assert all(c["state"]["state_name"] != "Closed" for c in pair), _summary(pair)
                if all(c["state"]["state_name"] == "ChannelReady" and settled(c) for c in pair):
                    break
        time.sleep(0.5)
    else:
        raise AssertionError(f"channel did not become ready on both nodes: {_summary(pair)}")
    assert pair is not None
    final_id = pair[0]["channel_id"]
    assert final_id != temporary_id and final_id not in previous[1]
    assert pair[0]["pubkey"] == info[1]["pubkey"] and pair[1]["pubkey"] == info[0]["pubkey"]
    assert pair[0]["is_acceptor"] is False and pair[1]["is_acceptor"] is True
    assert all(c["funding_udt_type_script"] is None for c in pair)
    outpoint = unpack_outpoint(pair[0]["channel_outpoint"])
    assert unpack_outpoint(pair[1]["channel_outpoint"]) == outpoint
    funding = env.rpc.wait_transaction(outpoint["tx_hash"], timeout_s=env.timeout_s)
    env.rpc.wait_indexer(funding["tx_status"]["block_number"], timeout_s=env.timeout_s)
    output = funding["transaction"]["outputs"][hex_int(outpoint["index"])]
    assert output.get("type") is None
    assert hex_int(output["capacity"]) == sum(principal), "funding cell must include both principals and reserves"
    assert hex_int(pair[0]["local_balance"]) == hex_int(pair[1]["remote_balance"])
    assert hex_int(pair[1]["local_balance"]) == hex_int(pair[0]["remote_balance"])
    assert hex_int(pair[0]["local_balance"]) >= PAYMENT_AMOUNT
    assert all(0 <= hex_int(c["local_balance"]) < p for c, p in zip(pair, principal)), "CKB reserves must be excluded from spendable channel balances"
    return Channel(final_id, temporary_id, outpoint, funding, principal, pair)


def pay_invoice(env: FiberEnvironment, channel: Channel, amount: int = PAYMENT_AMOUNT) -> tuple[dict[str, Any], dict[str, Any]]:
    before = channel_pair(env, channel.channel_id)
    assert before is not None and all(settled(c) and c["state"]["state_name"] == "ChannelReady" for c in before)
    expected = (hex_int(before[0]["local_balance"]) - amount, hex_int(before[1]["local_balance"]) + amount)
    assert expected[0] >= 0
    invoice = env.fnn[1].call("new_invoice", [{"amount": hex(amount), "currency": "Fibd"}])
    assert invoice["invoice"]["currency"] == "Fibd" and hex_int(invoice["invoice"]["amount"]) == amount
    payment_hash = invoice["invoice"]["data"]["payment_hash"]
    sent = env.fnn[0].call("send_payment", [{"invoice": invoice["invoice_address"]}])
    assert sent["payment_hash"] == payment_hash
    deadline = time.monotonic() + env.timeout_s
    states: tuple[str | None, str | None] = (None, None)
    pair = None
    while time.monotonic() < deadline:
        payment = env.fnn[0].call("get_payment", [{"payment_hash": payment_hash}])
        received = env.fnn[1].call("get_invoice", [{"payment_hash": payment_hash}])
        states = payment["status"], received["status"]
        assert states[0] != "Failed", f"invoice payment failed: {payment.get('failed_error')}"
        assert states[1] not in {"Cancelled", "Expired"}, f"invoice became {states[1]}"
        pair = channel_pair(env, channel.channel_id)
        if pair is not None:
            assert all(c["state"]["state_name"] == "ChannelReady" for c in pair), _summary(pair)
            balances = tuple(hex_int(c["local_balance"]) for c in pair)
            remote = tuple(hex_int(c["remote_balance"]) for c in pair)
            if states == ("Success", "Paid") and balances == expected and remote == expected[::-1] and all(settled(c) for c in pair):
                assert hex_int(payment["fee"]) == 0, "a direct payment must not charge a forwarding fee"
                channel.paid += amount
                return pair
        time.sleep(0.5)
    raise AssertionError(f"invoice did not settle: status={states}, expected={expected}, channels={_summary(pair)}")


def close_channel(
    env: FiberEnvironment, channel: Channel, *, recipient: Account | None = None,
) -> dict[str, Any]:
    pair = channel_pair(env, channel.channel_id)
    assert pair is not None
    if all(c["state"]["state_name"] == "ChannelReady" for c in pair):
        assert all(settled(c) for c in pair), "cannot cooperatively close unsettled test transfers"
        env.fnn[0].call("shutdown_channel", [{
            "channel_id": channel.channel_id,
            "close_script": rpc_script((recipient or env.accounts[3]).lock_script),
            "fee_rate": hex(1000),
            "force": False,
        }])
    deadline = time.monotonic() + env.timeout_s
    while time.monotonic() < deadline:
        pair = channel_pair(env, channel.channel_id)
        if pair is not None and all(c["state"]["state_name"] == "Closed" for c in pair):
            assert all({flag.strip() for flag in c["state"]["state_flags"].split("|")} == {"COOPERATIVE"} for c in pair), _summary(pair)
            hashes = [c["shutdown_transaction_hash"] for c in pair]
            if hashes[0] and hashes[0] == hashes[1]:
                transaction = env.rpc.wait_transaction(hashes[0], timeout_s=env.timeout_s)
                env.rpc.wait_indexer(transaction["tx_status"]["block_number"], timeout_s=env.timeout_s)
                channel.closed = True
                return transaction
        time.sleep(0.5)
    raise AssertionError(f"cooperative close did not commit on both nodes: {_summary(pair)}")


@contextmanager
def opened_ckb_channel(env: FiberEnvironment) -> Iterator[Channel]:
    channel = open_ckb_channel(env)
    try:
        yield channel
    finally:
        if not channel.closed:
            close_channel(env, channel)


def _script_key(script: dict[str, Any]) -> tuple[str, str, str]:
    value = rpc_script(script)
    return value["code_hash"].lower(), value["hash_type"].lower(), value["args"].lower()


def assert_close_amounts(
    rpc: RpcClient, channel: Channel, closing: dict[str, Any], accounts: tuple[Account, Account],
) -> tuple[int, int]:
    """Account for original funding principal, reserves, payment and the actual close fee."""
    transaction = closing["transaction"]
    inputs = [outpoint_key(value["previous_output"]) for value in transaction["inputs"]]
    assert inputs == [outpoint_key(channel.outpoint)], "close must consume exactly this channel's funding cell"
    funding_output = channel.funding_transaction["transaction"]["outputs"][hex_int(channel.outpoint["index"])]
    input_capacity = hex_int(funding_output["capacity"])
    outputs = transaction["outputs"]
    assert all(output.get("type") is None for output in outputs)
    assert all(data == "0x" for data in transaction["outputs_data"])
    expected_locks = [_script_key(account.lock_script) for account in accounts]
    assert set(_script_key(output["lock"]) for output in outputs) == set(expected_locks)
    returned = tuple(sum(hex_int(o["capacity"]) for o in outputs if _script_key(o["lock"]) == lock) for lock in expected_locks)
    fee = input_capacity - sum(hex_int(output["capacity"]) for output in outputs)
    assert 0 < fee < CKB, f"unexpected cooperative-close fee: {fee} shannons"
    expected = (channel.principal[0] - channel.paid - fee, channel.principal[1] + channel.paid)
    assert returned == expected, f"wrong close payout: got={returned}, expected={expected}, fee={fee}"
    # Some CKB versions report a consumed cell as unknown; the committed input above proves consumption.
    assert rpc.get_live_cell(channel.outpoint["tx_hash"], hex_int(channel.outpoint["index"]))["status"] != "live"
    return returned[0], returned[1]


def _key_file(env: FiberEnvironment, account: Account) -> Path:
    env.runner.register_secret(account.private_key)
    directory = env.root / "secrets"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / f"fiber-account-{account.index}.key"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(account.private_key + "\n")
    target.chmod(0o600)
    return target


def _ckb_text(shannons: int) -> str:
    whole, fraction = divmod(shannons, CKB)
    return f"{whole}.{fraction:08d}"


def assert_returned_funds_spendable(
    env: FiberEnvironment, closing: dict[str, Any], before_close: tuple[int, int], returned: tuple[int, int],
    recipients: tuple[Account, Account],
) -> None:
    transaction = closing["transaction"]
    closing_hash = transaction["hash"]
    destination = env.accounts[8]
    for offset, account in enumerate(recipients):
        balance = env.runner.run("balance", account.address, "--network", "devnet", "--no-udt").json
        assert balance is not None and balance["command"] == "balance" and balance["address"] == account.address
        actual = Decimal(str(balance["ckb"])) * CKB
        expected_balance = before_close[offset] + returned[offset]
        assert actual == expected_balance and env.rpc.ckb_balance(account.lock_script) == expected_balance
        # Sending more than the old balance forces collection of the returned cell;
        # a small transfer could otherwise succeed entirely from genesis funding.
        amount = before_close[offset] + CKB
        result = env.runner.run(
            "transfer", destination.address, _ckb_text(amount), "--network", "devnet",
            "--privkey-file", _key_file(env, account),
        )
        sent = env.rpc.wait_transaction(transaction_hash_from(result), timeout_s=env.timeout_s)
        env.rpc.wait_indexer(sent["tx_status"]["block_number"], timeout_s=env.timeout_s)
        return_points = {
            (closing_hash.lower(), index) for index, output in enumerate(transaction["outputs"])
            if _script_key(output["lock"]) == _script_key(account.lock_script)
        }
        spent = {outpoint_key(value["previous_output"]) for value in sent["transaction"]["inputs"]}
        assert return_points and return_points <= spent, "successful transfer did not spend the actual returned funds"
        assert sum(
            hex_int(output["capacity"]) for output in sent["transaction"]["outputs"]
            if _script_key(output["lock"]) == _script_key(destination.lock_script)
        ) == amount
        # Restore liquidity for independently selected cases in the same isolated environment.
        restored = env.runner.run(
            "transfer", account.address, _ckb_text(amount), "--network", "devnet",
            "--privkey-file", _key_file(env, destination),
        )
        committed = env.rpc.wait_transaction(transaction_hash_from(restored), timeout_s=env.timeout_s)
        env.rpc.wait_indexer(committed["tx_status"]["block_number"], timeout_s=env.timeout_s)
