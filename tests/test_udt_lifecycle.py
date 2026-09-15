from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from .asset_assertions import assert_udt_balance
from .asset_failure_support import assert_asset_state_unchanged, assert_cli_failure, capture_asset_state


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
    assert candidates and all(candidate == candidates[0] for candidate in candidates), {
        "typeArgs": type_args,
        "candidateTypes": candidates,
        "transaction": transaction,
    }
    target_type = candidates[0]
    inputs_total = _target_input_total(rpc, transaction_result, target_type)
    outputs = _target_outputs(transaction_result, target_type)
    assert all(lock == _script(receiver_lock) for lock, _ in outputs), outputs
    assert sum(value for _, value in outputs) - inputs_total == amount, outputs
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
    """用户用自己选择的开发账户发行 SUDT，并在余额查询中找到它。"""
    issuer = accounts[2]
    rpc.wait_indexer()
    amount = 10_000
    expected_type_args = _issuer_type_args(rpc, issuer)
    balance_before = assert_udt_balance(offckb, rpc, issuer, "sudt", expected_type_args)

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

    assert assert_udt_balance(offckb, rpc, issuer, "sudt", expected_type_args) == balance_before + amount


# TEST-MAP: UDT-02
def test_issue_xudt_preserves_type_args_and_kind(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
) -> None:
    """用户发行指定标识的 xUDT，并查询到完整标识和正确数量。"""
    issuer = accounts[3]
    rpc.wait_indexer()
    amount = 20_000
    default_args = _issuer_type_args(rpc, issuer)
    # RFC 0052: zero flags need no extension data and preserve input-lock ownership.
    type_args = default_args + "00000000"
    assert type_args != default_args
    xudt_before = assert_udt_balance(offckb, rpc, issuer, "xudt", type_args)
    default_xudt_before = assert_udt_balance(offckb, rpc, issuer, "xudt", default_args)
    sudt_before = assert_udt_balance(offckb, rpc, issuer, "sudt", default_args)

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

    assert assert_udt_balance(offckb, rpc, issuer, "xudt", type_args) == xudt_before + amount
    assert assert_udt_balance(offckb, rpc, issuer, "xudt", default_args) == default_xudt_before
    assert assert_udt_balance(offckb, rpc, issuer, "sudt", default_args) == sudt_before


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
    """用户转出部分代币后，双方都能查询到准确的余额变化。"""
    sender, receiver = accounts[4], accounts[5]
    rpc.wait_indexer()
    assert sender.address != receiver.address
    issued_amount = 30_000
    transfer_amount = 7_500
    type_args = _issuer_type_args(rpc, sender)
    sender_before = assert_udt_balance(offckb, rpc, sender, kind, type_args)
    receiver_before = assert_udt_balance(offckb, rpc, receiver, kind, type_args)

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
    sender_after_issue = assert_udt_balance(offckb, rpc, sender, kind, type_args)
    receiver_after_issue = assert_udt_balance(offckb, rpc, receiver, kind, type_args)
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

    sender_after = assert_udt_balance(offckb, rpc, sender, kind, type_args)
    receiver_after = assert_udt_balance(offckb, rpc, receiver, kind, type_args)
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
    """用户销毁自己的部分代币，同时保留另一持有者已有的代币。"""
    holder, other = accounts[10], accounts[11]
    rpc.wait_indexer()
    issued_amount = 40_000
    other_amount = 2_000
    destroy_amount = 9_000
    type_args = _issuer_type_args(rpc, holder)
    holder_before = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    other_before = assert_udt_balance(offckb, rpc, other, kind, type_args)

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
    holder_after_issue = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    assert holder_after_issue == holder_before + issued_amount

    transfer_payload = _json_result(offckb.run(
        "transfer",
        other.address,
        str(other_amount),
        "--network",
        "devnet",
        "--udt-kind",
        kind,
        "--udt-type-args",
        type_args,
        "--privkey-file",
        str(private_key_file(holder)),
        check=True,
    ))
    transfer_transaction = _wait_committed_and_indexed(rpc, _tx_hash(transfer_payload))
    _assert_transfer_transaction(
        rpc,
        transfer_transaction,
        target_type=target_type,
        sender_lock=holder.lock_script,
        receiver_lock=other.lock_script,
        amount=other_amount,
    )
    holder_before_destroy = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    other_before_destroy = assert_udt_balance(offckb, rpc, other, kind, type_args)
    assert holder_before_destroy == holder_after_issue - other_amount
    assert other_before_destroy == other_before + other_amount
    assert other_before_destroy > 0

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
        balance_before=holder_before_destroy,
        amount=destroy_amount,
    )

    holder_after = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    other_after = assert_udt_balance(offckb, rpc, other, kind, type_args)
    assert holder_after == holder_before_destroy - destroy_amount
    assert other_after == other_before_destroy
    assert holder_after + other_after == holder_before_destroy + other_before_destroy - destroy_amount


# TEST-MAP: UDT-05
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
@pytest.mark.parametrize(
    ("operation", "amount", "invalid_type_args"),
    [
        pytest.param("issue", "0", False, id="zero-issue"),
        pytest.param("transfer", "0x10", False, id="nondecimal-transfer"),
        pytest.param("destroy", str(1 << 128), False, id="out-of-range-destroy"),
        pytest.param("transfer", "10", True, id="short-type-args-transfer"),
    ],
)
def test_invalid_udt_input_preserves_existing_assets(
    devnet: Any, offckb: Any, rpc: Any, accounts: list[Any], private_key_file: Any,
    kind: str, operation: str, amount: str, invalid_type_args: bool,
) -> None:
    """已有代币时输入代表性的错误数量或标识，不能提交交易或改变资产。"""
    holder, receiver = accounts[12], accounts[13]
    type_args = _issuer_type_args(rpc, holder)
    key_path = private_key_file(holder)
    rpc.wait_indexer()
    balance_before_issue = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    issue = _issue(
        offckb, key_path, kind=kind, amount=1_000,
        type_args=type_args if kind == "xudt" else None,
    )
    _wait_committed_and_indexed(rpc, _tx_hash(issue))
    assert assert_udt_balance(offckb, rpc, holder, kind, type_args) == balance_before_issue + 1_000
    before = capture_asset_state(offckb, rpc, holder, receiver)
    selected_args = "0x1234" if invalid_type_args else type_args
    if operation == "transfer":
        command = ["transfer", receiver.address, amount, "--udt-type-args", selected_args]
    else:
        command = ["udt", operation, amount, "--type-args", selected_args]
    result = offckb.run(
        *command, "--network", "devnet", "--udt-kind", kind,
        "--privkey-file", key_path, check=False,
    )
    message = assert_cli_failure(result).lower()
    assert ("type args" if invalid_type_args else "amount") in message
    assert_asset_state_unchanged(offckb, rpc, before)


# TEST-MAP: UDT-06
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_destroy_more_than_owned_keeps_original_udt_cells_live(
    devnet: Any, offckb: Any, rpc: Any, accounts: list[Any], private_key_file: Any, kind: str,
) -> None:
    """销毁数量超过持有量时明确报告余额不足，并保留原代币与 CKB。"""
    holder = accounts[12]
    type_args = _issuer_type_args(rpc, holder)
    key_path = private_key_file(holder)
    rpc.wait_indexer()
    before_issue = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    issue = _issue(
        offckb, key_path, kind=kind, amount=1_000,
        type_args=type_args if kind == "xudt" else None,
    )
    _wait_committed_and_indexed(rpc, _tx_hash(issue))
    balance = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    assert balance == before_issue + 1_000
    before = capture_asset_state(offckb, rpc, holder)
    result = offckb.run(
        "udt", "destroy", str(balance + 1), "--network", "devnet", "--udt-kind", kind,
        "--type-args", type_args, "--privkey-file", key_path, check=False,
    )
    message = assert_cli_failure(result).lower()
    assert "insufficient" in message and "udt" in message and "balance" in message
    assert_asset_state_unchanged(offckb, rpc, before)


# TEST-MAP: UDT-07
@pytest.mark.parametrize("kind", ["sudt", "xudt"])
def test_destroy_all_udt_clears_holder_balance(
    devnet: Any, offckb: Any, rpc: Any, accounts: list[Any], private_key_file: Any, kind: str,
) -> None:
    """用户一次销毁全部代币，余额归零且另一持有者的代币不变。"""
    holder, other = accounts[14], accounts[15]
    type_args = _issuer_type_args(rpc, holder)
    key_path = private_key_file(holder)
    issued_amount, other_amount = 10_000, 2_000
    rpc.wait_indexer()
    holder_before = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    other_before = assert_udt_balance(offckb, rpc, other, kind, type_args)

    issue = _issue(
        offckb, key_path, kind=kind, amount=issued_amount,
        type_args=type_args if kind == "xudt" else None,
    )
    issue_transaction = _wait_committed_and_indexed(rpc, _tx_hash(issue))
    target_type = _assert_issue_transaction(
        rpc, issue_transaction, receiver_lock=holder.lock_script,
        type_args=type_args, amount=issued_amount,
    )
    transfer = _json_result(offckb.run(
        "transfer", other.address, str(other_amount), "--network", "devnet",
        "--udt-kind", kind, "--udt-type-args", type_args,
        "--privkey-file", key_path, check=True,
    ))
    transfer_transaction = _wait_committed_and_indexed(rpc, _tx_hash(transfer))
    _assert_transfer_transaction(
        rpc, transfer_transaction, target_type=target_type,
        sender_lock=holder.lock_script, receiver_lock=other.lock_script, amount=other_amount,
    )
    balance = assert_udt_balance(offckb, rpc, holder, kind, type_args)
    other_before_destroy = assert_udt_balance(offckb, rpc, other, kind, type_args)
    assert balance == holder_before + issued_amount - other_amount
    assert balance > 0
    assert other_before_destroy == other_before + other_amount
    assert other_before_destroy > 0

    payload = _json_result(offckb.run(
        "udt", "destroy", str(balance), "--network", "devnet", "--udt-kind", kind,
        "--type-args", type_args, "--privkey-file", key_path, check=True,
    ))
    assert payload.get("command") == "udt.destroy", payload
    assert payload.get("network") == "devnet", payload
    assert payload.get("kind") == kind, payload
    assert payload.get("amount") == str(balance), payload
    assert payload.get("typeArgs") == type_args, payload
    destroy_transaction = _wait_committed_and_indexed(rpc, _tx_hash(payload))
    _assert_destroy_transaction(
        rpc, destroy_transaction, target_type=target_type, holder_lock=holder.lock_script,
        balance_before=balance, amount=balance,
    )

    assert assert_udt_balance(offckb, rpc, holder, kind, type_args) == 0
    assert assert_udt_balance(offckb, rpc, other, kind, type_args) == other_before_destroy
