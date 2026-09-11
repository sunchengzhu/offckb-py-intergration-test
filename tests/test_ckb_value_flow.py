"""Black-box acceptance tests for the core CKB value flow."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from .asset_assertions import assert_udt_balance


SHANNONS_PER_CKB = 100_000_000
GENESIS_ACCOUNT_BALANCE = 42_000_000 * SHANNONS_PER_CKB
DEPOSIT_AMOUNT = "100"
TRANSFER_FUNDING_AMOUNT = "200"
TRANSFER_AMOUNT = "100"
TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32M_CONSTANT = 0x2BC830A3
_MISSING = object()

pytestmark = pytest.mark.core


def _field(value: Any, *names: str, default: Any = _MISSING) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    if default is not _MISSING:
        return default
    raise AssertionError(f"missing field {names!r} in {value!r}")


def _json_result(completed: Any) -> Mapping[str, Any]:
    assert _field(completed, "returncode", default=0) == 0
    payload = _field(completed, "json", default=None)
    if callable(payload):
        payload = payload()
    if payload is None:
        payload = json.loads(_field(completed, "stdout"))
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert isinstance(payload, Mapping), f"expected one JSON object, got {payload!r}"
    assert payload.get("ok") is True
    return payload


def _script(script: Any) -> dict[str, str]:
    return {
        "code_hash": str(_field(script, "code_hash", "codeHash")).lower(),
        "hash_type": str(_field(script, "hash_type", "hashType")).lower(),
        "args": str(_field(script, "args")).lower(),
    }


def _account_lock(account: Any) -> Any:
    return _field(account, "lock_script", "lockScript")


def _quantity(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    raise AssertionError(f"unsupported RPC quantity: {value!r}")


def _ckb_to_shannons(value: Any) -> int:
    amount = Decimal(str(value)) * SHANNONS_PER_CKB
    assert amount == amount.to_integral_value(), f"CKB value has sub-shannon precision: {value!r}"
    return int(amount)


def _bech32_polymod(values: Iterable[int]) -> int:
    checksum = 1
    generators = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    for value in values:
        top = checksum >> 25
        checksum = ((checksum & 0x1FFFFFF) << 5) ^ value
        for index, generator in enumerate(generators):
            if (top >> index) & 1:
                checksum ^= generator
    return checksum


def _convert_bits(values: Iterable[int], from_bits: int, to_bits: int) -> bytes:
    accumulator = 0
    bit_count = 0
    result = bytearray()
    max_value = (1 << to_bits) - 1
    for value in values:
        assert 0 <= value < (1 << from_bits), f"invalid {from_bits}-bit value: {value}"
        accumulator = (accumulator << from_bits) | value
        bit_count += from_bits
        while bit_count >= to_bits:
            bit_count -= to_bits
            result.append((accumulator >> bit_count) & max_value)
    assert bit_count < from_bits
    assert ((accumulator << (to_bits - bit_count)) & max_value) == 0
    return bytes(result)


def _decode_full_ckb_address(address: str) -> dict[str, str]:
    assert address == address.lower(), f"address must use canonical lowercase encoding: {address}"
    separator = address.rfind("1")
    assert separator > 0 and separator + 7 <= len(address), f"invalid bech32m address: {address}"
    hrp = address[:separator]
    assert hrp == "ckt", f"expected a devnet/testnet address, got HRP {hrp!r}"

    try:
        data = [_BECH32_CHARSET.index(char) for char in address[separator + 1 :]]
    except ValueError as error:
        raise AssertionError(f"invalid bech32m character in address: {address}") from error

    expanded_hrp = [ord(char) >> 5 for char in hrp] + [0] + [ord(char) & 31 for char in hrp]
    assert _bech32_polymod([*expanded_hrp, *data]) == _BECH32M_CONSTANT, "invalid address checksum"

    payload = _convert_bits(data[:-6], 5, 8)
    assert len(payload) >= 34 and payload[0] == 0, "expected a full-format CKB address"
    hash_types = {0: "data", 1: "type", 2: "data1"}
    assert payload[33] in hash_types, f"unknown CKB hash type: {payload[33]}"
    return {
        "code_hash": f"0x{payload[1:33].hex()}",
        "hash_type": hash_types[payload[33]],
        "args": f"0x{payload[34:].hex()}",
    }


def _has_private_key_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            if normalized in {"privkey", "privatekey"}:
                return True
            if _has_private_key_field(nested):
                return True
    elif isinstance(value, list):
        return any(_has_private_key_field(item) for item in value)
    return False


def _tx_hash(result: Mapping[str, Any]) -> str:
    value = str(_field(result, "txHash", "tx_hash"))
    assert TX_HASH.fullmatch(value), f"invalid transaction hash: {value!r}"
    return value


def _wait_committed_and_indexed(rpc: Any, tx_hash: str) -> Mapping[str, Any]:
    transaction_result = rpc.wait_transaction(tx_hash)
    assert isinstance(transaction_result, Mapping)
    status = _field(transaction_result, "tx_status", "txStatus")
    assert str(_field(status, "status")).lower() == "committed"
    block_number = _quantity(_field(status, "block_number", "blockNumber"))
    rpc.wait_indexer(block_number)
    return transaction_result


def _out_point_key(out_point: Any) -> tuple[str, int]:
    return (
        str(_field(out_point, "tx_hash", "txHash")).lower(),
        _quantity(_field(out_point, "index")),
    )


def _transaction_fee(transaction_result: Mapping[str, Any], input_cells: list[Mapping[str, Any]]) -> int:
    transaction = _field(transaction_result, "transaction")
    cells_by_out_point = {
        _out_point_key(_field(cell, "out_point", "outPoint")): cell for cell in input_cells
    }

    inputs = _field(transaction, "inputs")
    assert inputs, "committed transfer must consume at least one input"
    missing_inputs: list[tuple[str, int]] = []
    input_capacity = 0
    for tx_input in inputs:
        key = _out_point_key(_field(tx_input, "previous_output", "previousOutput"))
        cell = cells_by_out_point.get(key)
        if cell is None:
            missing_inputs.append(key)
            continue
        input_capacity += _quantity(_field(_field(cell, "output"), "capacity"))
    assert not missing_inputs, f"transaction consumed cells outside the captured sender set: {missing_inputs!r}"

    output_capacity = sum(
        _quantity(_field(output, "capacity")) for output in _field(transaction, "outputs")
    )
    fee = input_capacity - output_capacity
    assert 0 < fee < SHANNONS_PER_CKB, f"unexpected transfer fee: {fee} shannons"
    return fee


# TEST-MAP: CKB-01
def test_fresh_devnet_exposes_twenty_prefunded_accounts(
    fresh_devnet: Any, offckb: Any, rpc: Any, accounts: list[Any]
) -> None:
    del fresh_devnet  # The fixture owns readiness, a clean chain, and teardown.
    rpc.wait_indexer()
    completed = offckb.run("accounts", "--json")
    result = _json_result(completed)

    assert result["command"] == "accounts"
    assert result["context"] == "DEVNET"
    assert result["forked"] is False
    returned_accounts = result["accounts"]
    assert isinstance(returned_accounts, list) and len(returned_accounts) == 20
    assert {_quantity(account["index"]) for account in returned_accounts} == set(range(20))

    addresses = [str(account["address"]) for account in returned_accounts]
    assert len(set(addresses)) == 20
    assert not _has_private_key_field(result)

    output = f"{_field(completed, 'stdout', default='')}\n{_field(completed, 'stderr', default='')}"
    for account in accounts:
        secret = str(_field(account, "private_key", "privkey"))
        assert secret not in output
        assert secret.removeprefix("0x") not in output

    lock_args: set[str] = set()
    for account in returned_accounts:
        raw_lock_script = _field(account, "lockScript", "lock_script")
        lock_script = _script(raw_lock_script)
        lock_arg = str(_field(account, "lockArg", "lock_arg")).lower()
        assert lock_arg == lock_script["args"]
        assert _decode_full_ckb_address(str(account["address"])) == lock_script
        assert lock_arg not in lock_args
        lock_args.add(lock_arg)
        assert rpc.ckb_balance(raw_lock_script) == GENESIS_ACCOUNT_BALANCE


# TEST-MAP: CKB-02
def test_balance_omits_existing_udt_only_when_requested(
    devnet: Any, offckb: Any, rpc: Any, accounts: list[Any], private_key_file: Any
) -> None:
    del devnet
    rpc.wait_indexer()
    account = accounts[6]
    address = str(_field(account, "address"))
    type_args = rpc.script_hash(_account_lock(account))
    amount = 1234
    before = rpc.udt_balance(_account_lock(account), "sudt", type_args)
    issued = _json_result(offckb.run(
        "udt", "issue", str(amount), "--network", "devnet", "--udt-kind", "sudt",
        "--privkey-file", str(private_key_file(account)),
    ))
    _wait_committed_and_indexed(rpc, _tx_hash(issued))
    assert assert_udt_balance(offckb, rpc, account, "sudt", type_args) == before + amount

    with_udt = _json_result(offckb.run("balance", address, "--network", "devnet"))
    assert with_udt["udt"], "the --no-udt control account must actually hold discoverable UDT"

    result = _json_result(offckb.run("balance", address, "--network", "devnet", "--no-udt", "--json"))

    assert result["command"] == "balance"
    assert result["network"] == "devnet"
    assert result["address"] == address
    assert result["udt"] == []
    assert _ckb_to_shannons(result["ckb"]) == _ckb_to_shannons(with_udt["ckb"])
    live_cells = rpc.live_cells(_account_lock(account))
    pure_ckb = sum(
        _quantity(cell["output"]["capacity"])
        for cell in live_cells
        if cell["output"].get("type") is None and cell["output_data"] == "0x"
    )
    total_capacity = sum(_quantity(cell["output"]["capacity"]) for cell in live_cells)
    assert pure_ckb < total_capacity, "UDT cells must make pure CKB differ from total capacity"
    assert _ckb_to_shannons(result["ckb"]) == pure_ckb


# TEST-MAP: CKB-03
def test_deposit_commits_and_increases_receiver_by_exact_amount(
    devnet: Any, offckb: Any, rpc: Any, accounts: list[Any]
) -> None:
    devnet.wait_miner_funded(_ckb_to_shannons(DEPOSIT_AMOUNT) + SHANNONS_PER_CKB)
    receiver = accounts[-1]
    receiver_address = str(_field(receiver, "address"))
    receiver_lock = _account_lock(receiver)
    balance_before = rpc.ckb_balance(receiver_lock)

    result = _json_result(
        offckb.run("deposit", receiver_address, DEPOSIT_AMOUNT, "--network", "devnet", "--json")
    )
    assert result["command"] == "deposit"
    assert result["network"] == "devnet"
    assert result["toAddress"] == receiver_address
    assert str(result["amount"]) == DEPOSIT_AMOUNT

    tx_hash = _tx_hash(result)
    _wait_committed_and_indexed(rpc, tx_hash)
    balance_after = rpc.ckb_balance(receiver_lock)
    assert balance_after - balance_before == _ckb_to_shannons(DEPOSIT_AMOUNT)


# TEST-MAP: CKB-04
def test_transfer_uses_private_key_file_and_accounts_for_the_actual_fee(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
) -> None:
    sender, receiver = accounts[8], accounts[9]
    receiver_address = str(_field(receiver, "address"))
    sender_lock = _account_lock(sender)
    receiver_lock = _account_lock(receiver)

    devnet.wait_miner_funded(_ckb_to_shannons(TRANSFER_FUNDING_AMOUNT) + SHANNONS_PER_CKB)
    sender_before_funding = rpc.ckb_balance(sender_lock)
    funding = _json_result(
        offckb.run(
            "deposit",
            str(_field(sender, "address")),
            TRANSFER_FUNDING_AMOUNT,
            "--network",
            "devnet",
            "--json",
        )
    )
    assert funding["command"] == "deposit"
    assert funding["toAddress"] == str(_field(sender, "address"))
    _wait_committed_and_indexed(rpc, _tx_hash(funding))

    sender_before = rpc.ckb_balance(sender_lock)
    assert sender_before - sender_before_funding == _ckb_to_shannons(TRANSFER_FUNDING_AMOUNT)
    receiver_before = rpc.ckb_balance(receiver_lock)
    sender_cells = rpc.live_cells(sender_lock)
    assert isinstance(sender_cells, list) and sender_cells

    key_path = Path(private_key_file(sender))
    assert key_path.stat().st_mode & 0o077 == 0, "private-key file must not be group/world accessible"
    completed = offckb.run(
        "transfer",
        receiver_address,
        TRANSFER_AMOUNT,
        "--network",
        "devnet",
        "--privkey-file",
        str(key_path),
        "--json",
    )
    result = _json_result(completed)
    assert result["command"] == "transfer"
    assert result["network"] == "devnet"
    assert result["toAddress"] == receiver_address
    assert str(result["amount"]) == TRANSFER_AMOUNT

    secret = str(_field(sender, "private_key", "privkey")).strip()
    command_output = f"{_field(completed, 'stdout', default='')}\n{_field(completed, 'stderr', default='')}"
    assert secret not in command_output
    assert secret.removeprefix("0x") not in command_output

    transaction_result = _wait_committed_and_indexed(rpc, _tx_hash(result))
    actual_fee = _transaction_fee(transaction_result, sender_cells)
    sender_after = rpc.ckb_balance(sender_lock)
    receiver_after = rpc.ckb_balance(receiver_lock)
    amount = _ckb_to_shannons(TRANSFER_AMOUNT)

    assert receiver_after - receiver_before == amount
    assert sender_before - sender_after == amount + actual_fee
