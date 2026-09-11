from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from .harness import Account, DevnetManager, OffckbRunner, RpcClient, _configured_devnet_paths, hex_int


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
