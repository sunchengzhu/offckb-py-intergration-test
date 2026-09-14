from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from .harness import CKB, Account, DevnetManager, OffckbRunner, RpcClient, dep_group_members, hex_int, rpc_script


pytestmark = pytest.mark.core


def _read_displayed_scripts(stderr: str) -> dict[str, dict[str, Any]]:
    messages = [json.loads(line)["message"].strip() for line in stderr.splitlines() if line.strip()]
    assert "*** CKB DEVNET System Scripts ***" in messages, "the CLI must identify the selected devnet"
    scripts: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for message in messages:
        if message.startswith("- name: "):
            name = message.removeprefix("- name: ")
            assert name not in scripts, f"duplicate displayed script: {name}"
            current = scripts[name] = {}
        elif current is not None:
            for label, key in (("code_hash: ", "codeHash"), ("hash_type: ", "hashType")):
                if message.startswith(label):
                    current[key] = message.removeprefix(label)
            if message.startswith("cellDeps: "):
                current["cellDeps"] = json.loads(message.removeprefix("cellDeps: "))
    return scripts


def _out_point(point: dict[str, Any]) -> dict[str, str]:
    index = point.get("index")
    return {"tx_hash": point.get("txHash", point.get("tx_hash")), "index": hex(hex_int(index))}


def _read_sdk_export(stderr: str, title: str) -> dict[str, Any]:
    messages = [json.loads(line)["message"].strip() for line in stderr.splitlines() if line.strip()]
    assert title in messages, "the SDK export must identify devnet and the requested style"
    documents = [json.loads(message) for message in messages if message.startswith("{")]
    assert len(documents) == 1 and documents[0], "the export must contain one nonempty JSON object"
    return documents[0]


def _lumos_script(config: dict[str, Any]) -> dict[str, Any]:
    # Public @ckb-lumos/config-manager ScriptConfig fields. DEP_TYPE is
    # camelCase here; conversion to dep_group belongs at the RPC boundary.
    assert re.fullmatch(r"0x[0-9a-fA-F]{64}", config["CODE_HASH"])
    assert config["HASH_TYPE"] in {"type", "data", "data1", "data2"}
    assert re.fullmatch(r"0x[0-9a-fA-F]{64}", config["TX_HASH"])
    assert re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", config["INDEX"])
    assert config["DEP_TYPE"] in {"depGroup", "code"}
    return {
        "codeHash": config["CODE_HASH"], "hashType": config["HASH_TYPE"],
        "cellDeps": [{"cellDep": {
            "outPoint": {"txHash": config["TX_HASH"], "index": hex_int(config["INDEX"])},
            "depType": config["DEP_TYPE"],
        }}],
    }


def _reference(script: dict[str, Any]) -> tuple[Any, ...]:
    return script["codeHash"], script["hashType"], [
        (_out_point(dep["cellDep"]["outPoint"]), dep["cellDep"]["depType"])
        for dep in script["cellDeps"]
    ]


def _assert_script_reference(rpc: RpcClient, name: str, script: dict[str, Any]) -> None:
    assert re.fullmatch(r"0x[0-9a-f]{64}", script.get("codeHash", "")), f"invalid {name} code hash"
    assert script.get("hashType") in {"type", "data", "data1", "data2"}, f"invalid {name} hash type"
    assert script.get("cellDeps"), f"missing {name} dependencies"
    matching_code = []
    for dependency in script["cellDeps"]:
        dep = dependency["cellDep"]
        point = _out_point(dep["outPoint"])
        live = rpc.call("get_live_cell", [point, True])
        assert live["status"] == "live", f"{name} dependency is not live: {point}"
        if dep["depType"] == "depGroup":
            members = dep_group_members(live["cell"]["data"]["content"])
        else:
            assert dep["depType"] == "code", f"unknown {name} dependency kind"
            members = [point]
        for member in members:
            code = rpc.call("get_live_cell", [member, True])
            assert code["status"] == "live", f"{name} code/dependency group member is not live: {member}"
            cell = code["cell"]
            if script["hashType"] == "type":
                type_script = cell["output"]["type"]
                actual_hash = rpc.script_hash(type_script) if type_script else None
            else:
                data = bytes.fromhex(cell["data"]["content"].removeprefix("0x"))
                actual_hash = "0x" + hashlib.blake2b(data, digest_size=32, person=b"ckb-default-hash").hexdigest()
            if actual_hash == script["codeHash"]:
                assert cell["data"]["content"] != "0x", f"{name} points to empty contract code"
                matching_code.append(member)
    assert matching_code, f"{name} code hash does not identify code in its exported dependencies"


# TEST-MAP: SYS-01
def test_displayed_system_scripts_can_spend_an_account_input(
    devnet: DevnetManager,
    offckb: OffckbRunner,
    rpc: RpcClient,
    accounts: list[Account],
    private_key_file,
    run_root: Path,
    integration_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    """用户用列表里的账户锁和依赖签名、消费原输入，无需另找部署位置。"""
    scripts = _read_displayed_scripts(offckb.run("system-scripts", "--network", "devnet").stderr)
    for name in ("secp256k1_blake160_sighash_all", "xudt"):
        assert name in scripts, f"missing named devnet script: {name}"
        _assert_script_reference(rpc, name, scripts[name])
    expected_xudt = rpc.udt_script("xudt")
    assert scripts["xudt"]["codeHash"] == expected_xudt["code_hash"], "xudt names the wrong system script"
    assert scripts["xudt"]["hashType"] == expected_xudt["hash_type"]

    account = accounts[7]
    exported = scripts["secp256k1_blake160_sighash_all"]
    lock = {"codeHash": exported["codeHash"], "hashType": exported["hashType"], "args": account.lock_script["args"]}
    assert rpc_script(lock) == rpc_script(account.lock_script), "exported lock does not match the user's account"
    rpc.wait_indexer()
    inputs = [
        cell for cell in rpc.live_cells(lock)
        if cell["output"]["type"] is None and cell["output_data"] == "0x"
        and hex_int(cell["output"]["capacity"]) > 62 * CKB
    ]
    assert inputs, "the built-in account must have a spendable input using the exported lock"
    selected = inputs[0]
    request = run_root / "workspace" / "system-script-transfer.json"
    request.write_text(json.dumps({
        "rpcUrl": rpc.url,
        "scripts": scripts,
        "lock": lock,
        "input": selected["out_point"],
        "capacity": selected["output"]["capacity"],
    }))
    helper = OffckbRunner(
        (pytestconfig.getoption("--node-bin"), str(integration_root / "fixtures" / "use_system_scripts.cjs")),
        env=offckb.env, cwd=offckb.cwd, records_dir=run_root / "commands" / "system-script-transfer",
    )
    result = helper.run(
        offckb.cli_entry, request, "--privkey-file", private_key_file(account), json_mode=False,
    )
    receipt = json.loads(result.stdout)
    tx_hash = receipt["txHash"]
    assert re.fullmatch(r"0x[0-9a-f]{64}", tx_hash)
    committed = rpc.wait_transaction(tx_hash)["transaction"]
    assert [item["previous_output"] for item in committed["inputs"]] == [selected["out_point"]]
    expected_deps = [
        {"out_point": _out_point(item["cellDep"]["outPoint"]),
         "dep_type": "dep_group" if item["cellDep"]["depType"] == "depGroup" else "code"}
        for item in exported["cellDeps"]
    ]
    assert committed["cell_deps"] == expected_deps, "the SDK substituted or supplemented the exported dependencies"
    assert committed["outputs"][0]["lock"] == rpc_script(lock)
    # The committed transaction above proves consumption; CKB may report a
    # spent cell as unknown after removing it from the live-cell index.
    assert rpc.call("get_live_cell", [selected["out_point"], False])["status"] in {"dead", "unknown"}


# TEST-MAP: SYS-02
def test_sdk_exports_and_saved_project_config_match_the_local_chain(
    devnet: DevnetManager,
    offckb: OffckbRunner,
    rpc: RpcClient,
    run_root: Path,
    integration_root: Path,
    pytestconfig: pytest.Config,
) -> None:
    """用户复制 SDK 配置并保存到项目路径，得到可解析且属于当前链的脚本引用。"""
    displayed = _read_displayed_scripts(offckb.run("system-scripts", "--network", "devnet").stderr)
    exports = {
        style: _read_sdk_export(
            offckb.run("system-scripts", "--network", "devnet", "--export-style", style).stderr,
            f"*** CKB DEVNET System Scripts As {title} ***",
        )
        for style, title in (("ccc", "CCC KnownScripts"), ("lumos", "LumosConfig"))
    }
    lumos = exports["lumos"]
    assert lumos["PREFIX"] == "ckt"
    assert isinstance(lumos["SCRIPTS"], dict) and lumos["SCRIPTS"]
    lumos_scripts = {name: _lumos_script(config) for name, config in lumos["SCRIPTS"].items()}

    workspace = run_root / "workspace"
    request = workspace / "parse-system-script-exports.json"
    request.write_text(json.dumps({"rpcUrl": rpc.url, "ccc": exports["ccc"]}))
    helper = OffckbRunner(
        (pytestconfig.getoption("--node-bin"), str(integration_root / "fixtures/parse_system_script_exports.cjs")),
        env=offckb.env, cwd=offckb.cwd, records_dir=run_root / "commands" / "parse-system-script-exports",
    )
    parsed = json.loads(helper.run(offckb.cli_entry, request, json_mode=False).stdout)["representatives"]

    destination = workspace / "my contract project" / "config" / "system scripts.json"
    destination.parent.mkdir(parents=True)
    assert not destination.exists()
    offckb.run(
        "system-scripts", "--network", "devnet", "--export-style", "ccc", "--output", destination,
    )
    assert destination.is_file() and destination.stat().st_size > 0
    saved = json.loads(destination.read_text())
    assert isinstance(saved, dict) and {"devnet", "testnet", "mainnet"} <= saved.keys()
    for network in ("devnet", "testnet", "mainnet"):
        assert isinstance(saved[network], dict) and saved[network], f"empty {network} configuration"
        for name in ("secp256k1_blake160_sighash_all", "xudt"):
            script = saved[network][name]["script"]
            assert re.fullmatch(r"0x[0-9a-f]{64}", script["codeHash"])
            assert script["hashType"] in {"type", "data", "data1", "data2"} and script["cellDeps"]

    for name, lumos_name in (("secp256k1_blake160_sighash_all", "SECP256K1_BLAKE160"), ("xudt", "XUDT")):
        expected = _reference(displayed[name])
        for origin, script in (
            ("CCC", parsed[name]), ("Lumos", lumos_scripts[lumos_name]),
            ("saved devnet", saved["devnet"][name]["script"]),
        ):
            assert _reference(script) == expected, f"{origin} disagrees with the displayed {name} reference"
            _assert_script_reference(rpc, name, script)
