from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from .harness import Account, DevnetManager, OffckbRunner, RpcClient, _configured_devnet_paths, hex_int, rpc_script


@dataclass(frozen=True)
class DiagnosticCall:
    cli: OffckbRunner
    tx_hash: str
    transaction: dict[str, Any]
    markers: tuple[str, str]
    error: str | None
    config_path: Path
    data_path: Path


@pytest.fixture
def diagnostic_call(
    project_factory,
    devnet: DevnetManager,
    rpc: RpcClient,
    proxy_rpc: RpcClient,
    accounts: list[Account],
    private_key_file,
    integration_root: Path,
    pytestconfig: pytest.Config,
) -> Callable[..., DiagnosticCall]:
    def call(name: str, *, exit_code: int) -> DiagnosticCall:
        project = project_factory(name, contract="diagnostic-contract")
        marker = "OFFCKB_DIAGNOSTIC_" + uuid.uuid4().hex
        markers = (marker + "_FIRST", marker + "_SECOND")
        # Editing one's contract is the user action; generated build/deploy
        # scripts are used unchanged, including the managed debugger entry.
        source = project.path / "contracts" / project.contract / "src" / "index.ts"
        source.write_text(
            "import * as bindings from '@ckb-js-std/bindings';\n"
            + "\n".join(f"bindings.debug({json.dumps(value)});" for value in markers)
            + f"\nbindings.exit({exit_code});\n"
        )
        project.run("install")
        project.run("run", "build")
        owner = accounts[9]
        project.pnpm.env["OFFCKB_PRIVATE_KEY"] = owner.private_key
        try:
            project.run("run", "deploy", "--network", "devnet", "--yes")
        finally:
            project.pnpm.env.pop("OFFCKB_PRIVATE_KEY", None)
        records = json.loads((project.path / "deployment/scripts.json").read_text())
        contract = records["devnet"][f"{project.contract}.bc"]
        dep = contract["cellDeps"][0]["cellDep"]["outPoint"]
        deployed = rpc.wait_transaction(dep["txHash"])
        rpc.wait_indexer(deployed["tx_status"]["block_number"])
        live = rpc.get_live_cell(dep["txHash"], hex_int(dep["index"]))
        assert live["status"] == "live"
        assert live["cell"]["data"]["content"] == "0x" + (
            project.path / "dist" / f"{project.contract}.bc"
        ).read_bytes().hex()

        request = project.path.parent / "contract-call.json"
        request.write_text(json.dumps({
            "rpcUrl": rpc.url,
            "systemScripts": json.loads((project.path / "deployment/system-scripts.json").read_text()),
            "contract": contract,
        }))
        sdk = OffckbRunner(
            (pytestconfig.getoption("--node-bin"), str(integration_root / "fixtures/build_contract_call.cjs")),
            env=project.cli.env, cwd=project.path, records_dir=project.cli.records_dir.parent / "call",
        )
        result = sdk.run(
            project.cli.cli_entry, request, "--privkey-file", private_key_file(owner), json_mode=False,
        )
        prepared = json.loads(result.stdout)
        tx_hash, transaction = prepared["txHash"], prepared["transaction"]
        error = None
        try:
            submitted = proxy_rpc.call("send_transaction", [transaction, "passthrough"])
        except RuntimeError as rejected:
            # A transport failure is not a rejected contract transaction.
            if "RPC send_transaction returned " not in str(rejected):
                raise
            error = str(rejected)
        else:
            assert submitted == tx_hash
            if exit_code == 0:
                rpc.wait_transaction(tx_hash)
        config_path, data_path = _configured_devnet_paths(project.cli.env)
        return DiagnosticCall(project.cli, tx_hash, transaction, markers, error, config_path, data_path)

    return call


@pytest.fixture
def direct_diagnostic_call(
    project_factory,
    devnet: DevnetManager,
    rpc: RpcClient,
    accounts: list[Account],
    private_key_file,
    integration_root: Path,
    pytestconfig: pytest.Config,
) -> DiagnosticCall:
    project = project_factory("direct-script-selection", contract="target-lock")
    marker = "OFFCKB_DIRECT_DIAGNOSTIC_" + uuid.uuid4().hex
    markers = (marker + "_TARGET_LOCK", marker + "_OTHER_TYPE")
    names = {"target": project.contract, "other": "other-type"}
    for name, message in zip(names.values(), markers, strict=True):
        source = project.path / "contracts" / name / "src" / "index.ts"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(
            "import * as bindings from '@ckb-js-std/bindings';\n"
            + f"bindings.debug({json.dumps(message)});\nbindings.exit(0);\n"
        )
    project.run("install")
    project.run("run", "build")
    owner = accounts[9]
    project.pnpm.env["OFFCKB_PRIVATE_KEY"] = owner.private_key
    try:
        project.run("run", "deploy", "--network", "devnet", "--yes")
    finally:
        project.pnpm.env.pop("OFFCKB_PRIVATE_KEY", None)
    records = json.loads((project.path / "deployment/scripts.json").read_text())["devnet"]
    contracts = {role: records[f"{name}.bc"] for role, name in names.items()}
    for role, contract in contracts.items():
        dep = contract["cellDeps"][0]["cellDep"]["outPoint"]
        deployed = rpc.wait_transaction(dep["txHash"])
        rpc.wait_indexer(deployed["tx_status"]["block_number"])
        live = rpc.get_live_cell(dep["txHash"], hex_int(dep["index"]))
        assert live["status"] == "live"
        assert live["cell"]["data"]["content"] == "0x" + (
            project.path / "dist" / f"{names[role]}.bc"
        ).read_bytes().hex()
    assert contracts["target"]["codeHash"] != contracts["other"]["codeHash"]

    request_path = project.path.parent / "direct-contract-call.json"
    request = {
        "rpcUrl": rpc.url,
        "systemScripts": json.loads((project.path / "deployment/system-scripts.json").read_text()),
        "contracts": contracts,
    }
    sdk = OffckbRunner(
        (pytestconfig.getoption("--node-bin"), str(integration_root / "fixtures/build_direct_diagnostic_call.cjs")),
        env=project.cli.env, cwd=project.path, records_dir=project.cli.records_dir.parent / "direct-call",
    )

    def prepare_and_submit() -> dict[str, Any]:
        request_path.write_text(json.dumps(request))
        result = sdk.run(project.cli.cli_entry, request_path, "--privkey-file", private_key_file(owner), json_mode=False)
        prepared = json.loads(result.stdout)
        assert rpc.call("send_transaction", [prepared["transaction"], "passthrough"]) == prepared["txHash"]
        committed = rpc.wait_transaction(prepared["txHash"])["transaction"]
        assert committed["hash"] == prepared["txHash"]
        assert {key: value for key, value in committed.items() if key != "hash"} == prepared["transaction"]
        return prepared

    funding = prepare_and_submit()
    assert funding["transaction"]["outputs"][0]["lock"] == rpc_script(funding["scripts"]["target"])
    request["fundingHash"] = funding["txHash"]
    call = prepare_and_submit()
    assert call["transaction"]["inputs"] == [{
        "previous_output": {"tx_hash": funding["txHash"], "index": "0x0"}, "since": "0x0",
    }]
    assert call["transaction"]["outputs"][0]["type"] == rpc_script(call["scripts"]["other"])
    assert call["scripts"]["target"] != call["scripts"]["other"]
    config_path, data_path = _configured_devnet_paths(project.cli.env)
    assert not (config_path / "transactions" / f"{call['txHash']}.json").exists()
    assert not (config_path / "full-transactions" / f"{call['txHash']}.json").exists()
    return DiagnosticCall(project.cli, call["txHash"], call["transaction"], markers, None, config_path, data_path)
