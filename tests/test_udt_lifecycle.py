from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest


HEX32_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

pytestmark = pytest.mark.core


def _json_result(result: Any) -> Mapping[str, Any]:
    payload = result.json
    assert isinstance(payload, Mapping), (
        f"expected one JSON object on stdout, got {payload!r}; "
        f"stdout={result.stdout!r}, stderr={result.stderr!r}"
    )
    assert payload.get("ok") is True, payload
    return payload


def _tx_hash(payload: Mapping[str, Any]) -> str:
    tx_hash = payload.get("txHash")
    assert isinstance(tx_hash, str) and HEX32_RE.fullmatch(tx_hash), payload
    return tx_hash


def _wait_committed_and_indexed(rpc: Any, tx_hash: str) -> Mapping[str, Any]:
    transaction = rpc.wait_transaction(tx_hash)
    assert isinstance(transaction, Mapping), transaction
    tx_status = transaction.get("tx_status")
    assert isinstance(tx_status, Mapping), transaction
    assert tx_status.get("status") == "committed", transaction
    rpc.wait_indexer()
    return transaction


def _issuer_type_args(rpc: Any, account: Any) -> str:
    type_args = rpc.script_hash(account.lock_script)
    assert isinstance(type_args, str) and HEX32_RE.fullmatch(type_args), type_args
    return type_args.lower()


def _udt_balance(rpc: Any, account: Any, kind: str, type_args: str) -> int:
    balance = rpc.udt_balance(account.lock_script, kind, type_args)
    assert isinstance(balance, int) and not isinstance(balance, bool), balance
    assert balance >= 0, balance
    return balance


def _script(script: Mapping[str, Any]) -> dict[str, str]:
    code_hash = script.get("code_hash", script.get("codeHash"))
    hash_type = script.get("hash_type", script.get("hashType"))
    args = script.get("args")
    assert all(isinstance(value, str) for value in (code_hash, hash_type, args)), script
    return {
        "code_hash": str(code_hash).lower(),
        "hash_type": str(hash_type).lower(),
        "args": str(args).lower(),
    }


def _quantity(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return int(value, 16) if value.lower().startswith("0x") else int(value)
    raise AssertionError(f"unsupported RPC quantity: {value!r}")


def _udt_amount(output_data: Any) -> int:
    assert isinstance(output_data, str) and output_data.startswith("0x"), output_data
    raw = bytes.fromhex(output_data[2:])
    assert len(raw) == 16, (
        f"UDT output data must be a 16-byte little-endian u128: {output_data!r}"
    )
    return int.from_bytes(raw, byteorder="little", signed=False)


def _transaction_body(transaction_result: Mapping[str, Any]) -> Mapping[str, Any]:
    transaction = transaction_result.get("transaction")
    assert isinstance(transaction, Mapping), transaction_result
    return transaction


def _target_outputs(
    transaction_result: Mapping[str, Any], target_type: Mapping[str, str]
) -> list[tuple[dict[str, str], int]]:
    transaction = _transaction_body(transaction_result)
    outputs = transaction.get("outputs")
    outputs_data = transaction.get("outputs_data", transaction.get("outputsData"))
    assert isinstance(outputs, list) and isinstance(outputs_data, list), transaction
    assert len(outputs) == len(outputs_data), transaction

    target = _script(target_type)
    matched: list[tuple[dict[str, str], int]] = []
    for output, output_data in zip(outputs, outputs_data, strict=True):
        assert isinstance(output, Mapping), output
        type_script = output.get("type")
        if not isinstance(type_script, Mapping):
            continue
        actual_type = _script(type_script)
        if actual_type["args"] == target["args"]:
            assert actual_type == target, {"expectedType": target, "unexpectedType": actual_type}
        if actual_type != target:
            continue
        lock = output.get("lock")
        assert isinstance(lock, Mapping), output
        matched.append((_script(lock), _udt_amount(output_data)))
    return matched


def _target_input_total(
    rpc: Any,
    transaction_result: Mapping[str, Any],
    target_type: Mapping[str, str],
) -> int:
    transaction = _transaction_body(transaction_result)
    inputs = transaction.get("inputs")
    assert isinstance(inputs, list) and inputs, transaction
    target = _script(target_type)
    total = 0

    for tx_input in inputs:
        assert isinstance(tx_input, Mapping), tx_input
        previous_output = tx_input.get("previous_output", tx_input.get("previousOutput"))
        assert isinstance(previous_output, Mapping), tx_input
        tx_hash = previous_output.get("tx_hash", previous_output.get("txHash"))
        index = previous_output.get("index")
        assert isinstance(tx_hash, str) and index is not None, previous_output

        origin = rpc.call("get_transaction", [tx_hash])
        assert isinstance(origin, Mapping), {"outPoint": previous_output, "origin": origin}
        origin_transaction = _transaction_body(origin)
        origin_outputs = origin_transaction.get("outputs")
        origin_outputs_data = origin_transaction.get(
            "outputs_data", origin_transaction.get("outputsData")
        )
        assert isinstance(origin_outputs, list) and isinstance(origin_outputs_data, list), (
            origin_transaction
        )
        output_index = _quantity(index)
        assert 0 <= output_index < len(origin_outputs) == len(origin_outputs_data), previous_output
        origin_output = origin_outputs[output_index]
        assert isinstance(origin_output, Mapping), origin_output
        type_script = origin_output.get("type")
        if not isinstance(type_script, Mapping):
            continue
        actual_type = _script(type_script)
        if actual_type["args"] == target["args"]:
            assert actual_type == target, {"expectedType": target, "unexpectedInputType": actual_type}
        if actual_type == target:
            total += _udt_amount(origin_outputs_data[output_index])
    return total


def _assert_issue_transaction(
    rpc: Any,
    transaction_result: Mapping[str, Any],
    *,
    receiver_lock: Mapping[str, Any],
    type_args: str,
    amount: int,
) -> dict[str, str]:
    transaction = _transaction_body(transaction_result)
    outputs = transaction.get("outputs")
    assert isinstance(outputs, list), transaction
    candidates = [
        _script(type_script)
        for output in outputs
        if isinstance(output, Mapping)
        and isinstance((type_script := output.get("type")), Mapping)
        and _script(type_script)["args"] == type_args
    ]
    assert len(candidates) == 1, {
        "typeArgs": type_args,
        "candidateTypes": candidates,
        "transaction": transaction,
    }
    target_type = candidates[0]
    assert _target_input_total(rpc, transaction_result, target_type) == 0
    assert _target_outputs(transaction_result, target_type) == [(_script(receiver_lock), amount)]
    return target_type


def _assert_transfer_transaction(
    rpc: Any,
    transaction_result: Mapping[str, Any],
    *,
    target_type: Mapping[str, str],
    sender_lock: Mapping[str, Any],
    receiver_lock: Mapping[str, Any],
    amount: int,
) -> None:
    inputs_total = _target_input_total(rpc, transaction_result, target_type)
    outputs = _target_outputs(transaction_result, target_type)
    outputs_total = sum(output_amount for _, output_amount in outputs)
    assert inputs_total >= amount
    assert outputs_total == inputs_total

    sender = _script(sender_lock)
    receiver = _script(receiver_lock)
    assert all(lock in (sender, receiver) for lock, _ in outputs), outputs
    receiver_outputs = [output_amount for lock, output_amount in outputs if lock == receiver]
    sender_outputs = [output_amount for lock, output_amount in outputs if lock == sender]
    assert receiver_outputs and sum(receiver_outputs) == amount, outputs
    expected_change = inputs_total - amount
    assert sum(sender_outputs) == expected_change, outputs


def _assert_destroy_transaction(
    rpc: Any,
    transaction_result: Mapping[str, Any],
    *,
    target_type: Mapping[str, str],
    holder_lock: Mapping[str, Any],
    balance_before: int,
    amount: int,
) -> None:
    inputs_total = _target_input_total(rpc, transaction_result, target_type)
    outputs = _target_outputs(transaction_result, target_type)
    assert 0 < inputs_total <= balance_before
    holder = _script(holder_lock)
    assert all(lock == holder for lock, _ in outputs), outputs
    assert inputs_total - sum(output_amount for _, output_amount in outputs) == amount


def _issue(
    offckb: Any,
    key_file: Path,
    *,
    kind: str,
    amount: int,
    type_args: str | None = None,
) -> Mapping[str, Any]:
    args = [
        "udt",
        "issue",
        str(amount),
        "--network",
        "devnet",
        "--udt-kind",
        kind,
        "--privkey-file",
        str(key_file),
    ]
    if type_args is not None:
        args.extend(("--type-args", type_args))
    return _json_result(offckb.run(*args, check=True))


# TEST-MAP: UDT-01
def test_issue_sudt_uses_issuer_lock_hash(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
) -> None:
    issuer = accounts[0]
    rpc.wait_indexer()
    amount = 10_000
    expected_type_args = _issuer_type_args(rpc, issuer)
    balance_before = _udt_balance(rpc, issuer, "sudt", expected_type_args)

    payload = _issue(
        offckb,
        private_key_file(issuer),
        kind="sudt",
        amount=amount,
    )

    assert payload.get("command") == "udt.issue", payload
    assert payload.get("network") == "devnet", payload
    assert payload.get("kind") == "sudt", payload
    assert payload.get("amount") == str(amount), payload
    assert payload.get("receiver") == issuer.address, payload
    assert str(payload.get("typeArgs", "")).lower() == expected_type_args, payload
    issue_transaction = _wait_committed_and_indexed(rpc, _tx_hash(payload))
    _assert_issue_transaction(
        rpc,
        issue_transaction,
        receiver_lock=issuer.lock_script,
        type_args=expected_type_args,
        amount=amount,
    )

    assert _udt_balance(rpc, issuer, "sudt", expected_type_args) == balance_before + amount


# TEST-MAP: UDT-02
def test_issue_xudt_preserves_type_args_and_kind(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
) -> None:
    issuer = accounts[0]
    rpc.wait_indexer()
    amount = 20_000
    type_args = _issuer_type_args(rpc, issuer)
    xudt_before = _udt_balance(rpc, issuer, "xudt", type_args)
    sudt_before = _udt_balance(rpc, issuer, "sudt", type_args)

    payload = _issue(
        offckb,
        private_key_file(issuer),
        kind="xudt",
        amount=amount,
        type_args=type_args,
    )

    assert payload.get("command") == "udt.issue", payload
    assert payload.get("network") == "devnet", payload
    assert payload.get("kind") == "xudt", payload
    assert payload.get("amount") == str(amount), payload
    assert payload.get("receiver") == issuer.address, payload
    assert payload.get("typeArgs") == type_args, payload
    issue_transaction = _wait_committed_and_indexed(rpc, _tx_hash(payload))
    _assert_issue_transaction(
        rpc,
        issue_transaction,
        receiver_lock=issuer.lock_script,
        type_args=type_args,
        amount=amount,
    )

    assert _udt_balance(rpc, issuer, "xudt", type_args) == xudt_before + amount
    assert _udt_balance(rpc, issuer, "sudt", type_args) == sudt_before


# TEST-MAP: UDT-03
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_transfer_udt_preserves_amount_and_type(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
    kind: str,
) -> None:
    sender, receiver = accounts[:2]
    rpc.wait_indexer()
    assert sender.address != receiver.address
    issued_amount = 30_000
    transfer_amount = 7_500
    type_args = _issuer_type_args(rpc, sender)
    sender_before = _udt_balance(rpc, sender, kind, type_args)
    receiver_before = _udt_balance(rpc, receiver, kind, type_args)

    issue_payload = _issue(
        offckb,
        private_key_file(sender),
        kind=kind,
        amount=issued_amount,
        type_args=type_args if kind == "xudt" else None,
    )
    issue_transaction = _wait_committed_and_indexed(rpc, _tx_hash(issue_payload))
    target_type = _assert_issue_transaction(
        rpc,
        issue_transaction,
        receiver_lock=sender.lock_script,
        type_args=type_args,
        amount=issued_amount,
    )
    sender_after_issue = _udt_balance(rpc, sender, kind, type_args)
    receiver_after_issue = _udt_balance(rpc, receiver, kind, type_args)
    assert sender_after_issue == sender_before + issued_amount
    assert receiver_after_issue == receiver_before

    result = offckb.run(
        "transfer",
        receiver.address,
        str(transfer_amount),
        "--network",
        "devnet",
        "--udt-kind",
        kind,
        "--udt-type-args",
        type_args,
        "--privkey-file",
        str(private_key_file(sender)),
        check=True,
    )
    payload = _json_result(result)

    assert payload.get("command") == "udt.transfer", payload
    assert payload.get("network") == "devnet", payload
    assert payload.get("kind") == kind, payload
    assert payload.get("amount") == str(transfer_amount), payload
    assert payload.get("typeArgs") == type_args, payload
    assert payload.get("toAddress") == receiver.address, payload
    transfer_transaction = _wait_committed_and_indexed(rpc, _tx_hash(payload))
    _assert_transfer_transaction(
        rpc,
        transfer_transaction,
        target_type=target_type,
        sender_lock=sender.lock_script,
        receiver_lock=receiver.lock_script,
        amount=transfer_amount,
    )

    sender_after = _udt_balance(rpc, sender, kind, type_args)
    receiver_after = _udt_balance(rpc, receiver, kind, type_args)
    assert sender_after == sender_after_issue - transfer_amount
    assert receiver_after == receiver_after_issue + transfer_amount
    assert sender_after + receiver_after == sender_after_issue + receiver_after_issue


# TEST-MAP: UDT-04
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_destroy_partial_udt_keeps_exact_change(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
    kind: str,
) -> None:
    holder, other = accounts[:2]
    rpc.wait_indexer()
    issued_amount = 40_000
    destroy_amount = 9_000
    type_args = _issuer_type_args(rpc, holder)
    holder_before = _udt_balance(rpc, holder, kind, type_args)
    other_before = _udt_balance(rpc, other, kind, type_args)

    issue_payload = _issue(
        offckb,
        private_key_file(holder),
        kind=kind,
        amount=issued_amount,
        type_args=type_args if kind == "xudt" else None,
    )
    issue_transaction = _wait_committed_and_indexed(rpc, _tx_hash(issue_payload))
    target_type = _assert_issue_transaction(
        rpc,
        issue_transaction,
        receiver_lock=holder.lock_script,
        type_args=type_args,
        amount=issued_amount,
    )
    holder_after_issue = _udt_balance(rpc, holder, kind, type_args)
    assert holder_after_issue == holder_before + issued_amount

    result = offckb.run(
        "udt",
        "destroy",
        str(destroy_amount),
        "--network",
        "devnet",
        "--udt-kind",
        kind,
        "--type-args",
        type_args,
        "--privkey-file",
        str(private_key_file(holder)),
        check=True,
    )
    payload = _json_result(result)

    assert payload.get("command") == "udt.destroy", payload
    assert payload.get("network") == "devnet", payload
    assert payload.get("kind") == kind, payload
    assert payload.get("amount") == str(destroy_amount), payload
    assert payload.get("typeArgs") == type_args, payload
    destroy_transaction = _wait_committed_and_indexed(rpc, _tx_hash(payload))
    _assert_destroy_transaction(
        rpc,
        destroy_transaction,
        target_type=target_type,
        holder_lock=holder.lock_script,
        balance_before=holder_after_issue,
        amount=destroy_amount,
    )

    holder_after = _udt_balance(rpc, holder, kind, type_args)
    other_after = _udt_balance(rpc, other, kind, type_args)
    assert holder_after == holder_after_issue - destroy_amount
    assert other_after == other_before
    assert holder_after + other_after == holder_after_issue + other_before - destroy_amount
