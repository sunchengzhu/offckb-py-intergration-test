from __future__ import annotations

import re
from typing import Any

from .harness import Account, OffckbRunner, RpcClient


def assert_udt_balance(
    offckb: OffckbRunner, rpc: RpcClient, account: Account, kind: str, type_args: str
) -> int:
    """Check CLI discovery and filtering against independently enumerated live cells."""
    expected = rpc.udt_balances(account.lock_script)

    def query(*options: str) -> dict[tuple[str, str, str, str], int]:
        result = offckb.run("balance", account.address, "--network", "devnet", *options)
        payload = result.json
        assert isinstance(payload, dict), result.stdout
        assert payload.get("ok") is True, payload
        assert payload.get("command") == "balance", payload
        assert payload.get("network") == "devnet", payload
        assert payload.get("address") == account.address, payload
        entries = payload.get("udt")
        assert isinstance(entries, list), payload
        actual: dict[tuple[str, str, str, str], int] = {}
        for entry in entries:
            assert isinstance(entry, dict), entry
            key = (entry.get("kind"), entry.get("codeHash"), entry.get("hashType"), entry.get("args"))
            assert all(isinstance(value, str) for value in key), entry
            assert key not in actual, f"CLI returned duplicate asset rows: {entries}"
            amount: Any = entry.get("balance")
            assert isinstance(amount, str) and re.fullmatch(r"[0-9]+", amount), entry
            actual[key] = int(amount)
        return actual

    actual = query()
    assert actual == expected, {"address": account.address, "cli": actual, "chain": expected}
    filtered_expected = {
        key: amount for key, amount in expected.items() if key[0] == kind and key[3] == type_args
    }
    filtered = query("--udt-kind", kind, "--udt-type-args", type_args)
    assert filtered == filtered_expected, {"cli": filtered, "chain": filtered_expected}
    return sum(filtered_expected.values())
