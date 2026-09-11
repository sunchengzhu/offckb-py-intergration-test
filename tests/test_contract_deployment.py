from __future__ import annotations

import hashlib
import json
import struct
import tomllib
from pathlib import Path
from typing import Any

import pytest

from .harness import rpc_script


_CKB_HASH_PERSONALIZATION = b"ckb-default-hash"
_HASH_TYPE_BYTES = {"data": 0, "type": 1, "data1": 2, "data2": 4}
_TYPE_ID_CODE_HASH = "0x00000000000000000000000000000000000000000000000000545950455f4944"
_IMMUTABLE_CONTRACT = b"\x00asm\x01\x00\x00\x00\xffoffckb-deploy-immutable-v1"
_TYPE_ID_CONTRACT_V1 = b"\x00asm\x01\x00\x00\x00\xffoffckb-deploy-type-id-v1"
_TYPE_ID_CONTRACT_V2 = b"\x00asm\x01\x00\x00\x00\xffoffckb-deploy-type-id-v2"

pytestmark = pytest.mark.core


def _ckb_hash(payload: bytes) -> str:
    digest = hashlib.blake2b(payload, digest_size=32, person=_CKB_HASH_PERSONALIZATION)
    return f"0x{digest.hexdigest()}"


def _hex_bytes(value: str) -> bytes:
    assert value.startswith("0x"), f"expected 0x-prefixed hex, got {value!r}"
    return bytes.fromhex(value[2:])


def _script_hash(script: dict[str, str]) -> str:
    code_hash = _hex_bytes(script["code_hash"])
    args = _hex_bytes(script["args"])
    assert len(code_hash) == 32

    hash_type = _HASH_TYPE_BYTES[script["hash_type"]]
    args_field = struct.pack("<I", len(args)) + args
    header_size = 4 * 4
    code_hash_offset = header_size
    hash_type_offset = code_hash_offset + len(code_hash)
    args_offset = hash_type_offset + 1
    total_size = args_offset + len(args_field)
    molecule_script = b"".join(
        (
            struct.pack("<I", total_size),
            struct.pack("<I", code_hash_offset),
            struct.pack("<I", hash_type_offset),
            struct.pack("<I", args_offset),
            code_hash,
            bytes((hash_type,)),
            args_field,
        )
    )
    return _ckb_hash(molecule_script)


def _write_contract(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _run_deploy(
    offckb: Any,
    contract: Path,
    output: Path,
    key_file: Path,
    *,
    type_id: bool,
) -> None:
    args = [
        "deploy",
        "--network",
        "devnet",
        "--target",
        str(contract),
        "--output",
        str(output),
        "--privkey-file",
        str(key_file),
        "--yes",
    ]
    if type_id:
        args.append("--type-id")

    result = offckb.run(*args, check=True)
    assert result.returncode == 0
    assert result.json["ok"] is True
    assert result.json["command"] == "deploy"
    assert result.json["completed"] is True


def _migration_files(output: Path, contract_name: str) -> list[Path]:
    migration_dir = output / "devnet" / contract_name / "migrations"
    assert migration_dir.is_dir()
    files = sorted(migration_dir.glob("*.json"))
    assert files
    return files


def _read_migration(path: Path, contract_name: str) -> dict[str, Any]:
    migration = json.loads(path.read_text(encoding="utf-8"))
    assert migration["dep_group_recipes"] == []
    assert len(migration["cell_recipes"]) == 1
    recipe = migration["cell_recipes"][0]
    assert recipe["name"] == contract_name
    assert isinstance(recipe["index"], int)
    assert isinstance(recipe["occupied_capacity"], int)
    return recipe


def _read_artifacts(output: Path, contract: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    deployment_toml = output / "devnet" / contract.name / "deployment.toml"
    scripts_json = output / "scripts.json"
    assert deployment_toml.is_file()
    assert scripts_json.is_file()

    with deployment_toml.open("rb") as stream:
        deployment = tomllib.load(stream)
    scripts = json.loads(scripts_json.read_text(encoding="utf-8"))
    return deployment, scripts


def _assert_deployment_toml(
    deployment: dict[str, Any],
    contract: Path,
    *,
    type_id: bool,
) -> None:
    assert len(deployment["cells"]) == 1
    cell = deployment["cells"][0]
    assert cell == {
        "name": contract.name,
        "enable_type_id": type_id,
        "location": {"file": str(contract)},
    }
    assert set(deployment["lock"]) == {"code_hash", "args", "hash_type"}


def _assert_deployment_owner(
    deployment: dict[str, Any], live_cell: dict[str, Any], expected_lock: dict[str, Any]
) -> None:
    expected = rpc_script(expected_lock)
    assert deployment["lock"] == expected
    assert live_cell["output"]["lock"] == expected


def _assert_type_id_script(type_script: dict[str, str]) -> None:
    assert type_script["code_hash"] == _TYPE_ID_CODE_HASH
    assert type_script["hash_type"] == "type"
    assert len(_hex_bytes(type_script["args"])) == 32


def _script_record(scripts: dict[str, Any], contract_name: str) -> dict[str, Any]:
    assert scripts["mainnet"] == {}
    assert scripts["testnet"] == {}
    assert set(scripts["devnet"]) == {contract_name}
    return scripts["devnet"][contract_name]


def _assert_script_record(
    record: dict[str, Any],
    recipe: dict[str, Any],
    *,
    code_hash: str,
    hash_type: str,
) -> None:
    assert record["codeHash"] == code_hash
    assert record["hashType"] == hash_type
    assert record["cellDeps"] == [
        {
            "cellDep": {
                "outPoint": {"txHash": recipe["tx_hash"], "index": recipe["index"]},
                "depType": "code",
            }
        }
    ]


def _wait_committed(rpc: Any, tx_hash: str) -> dict[str, Any]:
    transaction = rpc.wait_transaction(tx_hash)
    status = transaction["tx_status"]
    assert status["status"] == "committed"
    block_number = status.get("block_number")
    assert block_number is not None, transaction
    rpc.wait_indexer(block_number)
    return transaction


def _assert_live_code_cell(
    rpc: Any,
    recipe: dict[str, Any],
    content: bytes,
) -> dict[str, Any]:
    live_cell = rpc.get_live_cell(recipe["tx_hash"], recipe["index"])
    assert live_cell["status"] == "live"
    cell = live_cell["cell"]
    assert _hex_bytes(cell["data"]["content"]) == content
    assert cell["data"]["hash"] == _ckb_hash(content)
    assert recipe["data_hash"] == cell["data"]["hash"]
    assert recipe["occupied_capacity"] == len(content) * 100_000_000
    return cell


# TEST-MAP: DEPLOY-01
def test_deploy_immutable_contract(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
    tmp_path: Path,
) -> None:
    rpc.wait_indexer()
    contract = _write_contract(tmp_path / "immutable-contract.bin", _IMMUTABLE_CONTRACT)
    output = tmp_path / "deployment"
    owner = accounts[7]
    key_file = private_key_file(owner)

    _run_deploy(offckb, contract, output, key_file, type_id=False)

    migrations = _migration_files(output, contract.name)
    assert len(migrations) == 1
    recipe = _read_migration(migrations[0], contract.name)
    deployment, scripts = _read_artifacts(output, contract)
    _assert_deployment_toml(deployment, contract, type_id=False)

    _wait_committed(rpc, recipe["tx_hash"])
    live_cell = _assert_live_code_cell(rpc, recipe, _IMMUTABLE_CONTRACT)
    _assert_deployment_owner(deployment, live_cell, owner.lock_script)
    assert live_cell["output"]["type"] is None
    assert recipe.get("type_id") is None
    _assert_script_record(
        _script_record(scripts, contract.name),
        recipe,
        code_hash=recipe["data_hash"],
        hash_type="data2",
    )


# TEST-MAP: DEPLOY-02
def test_first_type_id_deployment(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
    tmp_path: Path,
) -> None:
    rpc.wait_indexer()
    contract = _write_contract(tmp_path / "type-id-contract.bin", _TYPE_ID_CONTRACT_V1)
    output = tmp_path / "deployment"
    owner = accounts[7]
    key_file = private_key_file(owner)

    _run_deploy(offckb, contract, output, key_file, type_id=True)

    migrations = _migration_files(output, contract.name)
    assert len(migrations) == 1
    recipe = _read_migration(migrations[0], contract.name)
    deployment, scripts = _read_artifacts(output, contract)
    _assert_deployment_toml(deployment, contract, type_id=True)

    _wait_committed(rpc, recipe["tx_hash"])
    live_cell = _assert_live_code_cell(rpc, recipe, _TYPE_ID_CONTRACT_V1)
    type_script = live_cell["output"]["type"]
    assert type_script is not None
    _assert_type_id_script(type_script)
    _assert_deployment_owner(deployment, live_cell, owner.lock_script)
    assert recipe["type_id"].startswith("0x")
    assert recipe["type_id"] == _script_hash(type_script)
    _assert_script_record(
        _script_record(scripts, contract.name),
        recipe,
        code_hash=recipe["type_id"],
        hash_type="type",
    )


# TEST-MAP: DEPLOY-03
def test_upgrade_preserves_type_id_and_consumes_old_cell(
    devnet: Any,
    offckb: Any,
    rpc: Any,
    accounts: list[Any],
    private_key_file: Any,
    tmp_path: Path,
) -> None:
    rpc.wait_indexer()
    contract = _write_contract(tmp_path / "upgradable-contract.bin", _TYPE_ID_CONTRACT_V1)
    output = tmp_path / "deployment"
    owner = accounts[7]
    key_file = private_key_file(owner)

    _run_deploy(offckb, contract, output, key_file, type_id=True)
    old_migrations = _migration_files(output, contract.name)
    assert len(old_migrations) == 1
    old_recipe = _read_migration(old_migrations[0], contract.name)
    _wait_committed(rpc, old_recipe["tx_hash"])
    old_cell = _assert_live_code_cell(rpc, old_recipe, _TYPE_ID_CONTRACT_V1)
    old_deployment, _ = _read_artifacts(output, contract)
    _assert_deployment_owner(old_deployment, old_cell, owner.lock_script)
    old_type_script = old_cell["output"]["type"]
    assert old_type_script is not None
    _assert_type_id_script(old_type_script)
    assert old_recipe["type_id"] == _script_hash(old_type_script)

    contract.write_bytes(_TYPE_ID_CONTRACT_V2)
    _run_deploy(offckb, contract, output, key_file, type_id=True)

    new_migrations = _migration_files(output, contract.name)
    added_migrations = set(new_migrations) - set(old_migrations)
    assert len(new_migrations) == len(old_migrations) + 1
    assert len(added_migrations) == 1
    new_recipe = _read_migration(added_migrations.pop(), contract.name)
    upgrade_transaction = _wait_committed(rpc, new_recipe["tx_hash"])

    old_cell_after_upgrade = rpc.get_live_cell(old_recipe["tx_hash"], old_recipe["index"])
    assert old_cell_after_upgrade["status"] != "live"
    consumed_outpoints = {
        (item["previous_output"]["tx_hash"], int(item["previous_output"]["index"], 16))
        for item in upgrade_transaction["transaction"]["inputs"]
    }
    assert (old_recipe["tx_hash"], old_recipe["index"]) in consumed_outpoints
    new_cell = _assert_live_code_cell(rpc, new_recipe, _TYPE_ID_CONTRACT_V2)
    new_type_script = new_cell["output"]["type"]
    assert new_type_script is not None
    _assert_type_id_script(new_type_script)

    assert new_recipe["tx_hash"] != old_recipe["tx_hash"]
    assert new_recipe["data_hash"] != old_recipe["data_hash"]
    assert new_recipe["type_id"] == old_recipe["type_id"]
    assert new_type_script == old_type_script
    assert _script_hash(new_type_script) == new_recipe["type_id"]

    deployment, scripts = _read_artifacts(output, contract)
    _assert_deployment_toml(deployment, contract, type_id=True)
    _assert_deployment_owner(deployment, new_cell, owner.lock_script)
    _assert_script_record(
        _script_record(scripts, contract.name),
        new_recipe,
        code_hash=new_recipe["type_id"],
        hash_type="type",
    )
