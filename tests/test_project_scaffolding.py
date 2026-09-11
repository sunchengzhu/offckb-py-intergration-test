from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import pytest

from .harness import Account, DevnetManager, OffckbRunner, RpcClient, hex_int, rpc_script
from .project_support import Project, project_factory


pytestmark = [pytest.mark.core, pytest.mark.project]


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    assert isinstance(value, dict) and value, f"expected a nonempty JSON object: {path}"
    return value


def _assert_project_layout(project: Project) -> None:
    path, contract = project.path, project.contract
    manifest = _read_json(path / "package.json")
    assert manifest["name"] == path.name
    for script in ("build", "deploy", "test", "test:only"):
        assert manifest["scripts"].get(script), f"missing generated script: {script}"
    required = (
        "package.json", "tsconfig.json", "tsconfig.base.json", "jest.config.cjs",
        "scripts/build-all.js", "scripts/build-contract.js", "scripts/deploy.js",
        f"contracts/{contract}/src/index.ts", f"tests/{contract}.mock.test.ts",
        f"tests/{contract}.devnet.test.ts", "tests/helper.ts", ".env", ".env.example",
        "deployment/scripts.json", "deployment/system-scripts.json",
    )
    for relative in required:
        assert (path / relative).is_file(), f"missing generated file: {relative}"
    for kind in ("mock", "devnet"):
        contents = (path / "tests" / f"{contract}.{kind}.test.ts").read_text()
        assert f"{contract}.bc" in contents
        assert f"{contract} contract" in contents
    # Scan only generated files, never installed dependencies or Git objects.
    for item in path.rglob("*"):
        relative = item.relative_to(path)
        if "node_modules" in relative.parts or ".git" in relative.parts or not item.is_file():
            continue
        has_placeholder = re.search(
            r"\{\{\s*[#/]?[A-Za-z_][A-Za-z0-9_]*\s*\}\}", str(relative) + item.read_text(),
        ) is not None
        # Keep dotenv contents out of pytest's rewritten assertion diagnostics.
        assert not has_placeholder, f"unexpanded template placeholder in {relative}"
    exported = path.parent / "exported-system-scripts.json"
    project.cli.run("system-scripts", "--output", exported)
    generated = _read_json(path / "deployment/system-scripts.json")
    assert generated == _read_json(exported)
    assert isinstance(generated.get("devnet"), dict) and generated["devnet"]
    assert generated["devnet"].get("ckb_js_vm"), "the example needs the exported JS VM script"


def _assert_build(project: Project) -> None:
    for extension in ("js", "bc"):
        artifact = project.path / "dist" / f"{project.contract}.{extension}"
        assert artifact.is_file() and artifact.stat().st_size > 0, f"missing build artifact: {artifact}"


def _tree_snapshot(path: Path) -> dict[str, str | None]:
    # Digest contents rather than placing private dotenv values in assertion output.
    return {
        str(item.relative_to(path)): hashlib.sha256(item.read_bytes()).hexdigest() if item.is_file() else None
        for item in path.rglob("*")
    }


# TEST-MAP: PROJ-06
def test_default_project_installs_dependencies_initializes_git_and_builds(project_factory) -> None:
    """用户采用默认合约和安装选项创建项目，随即构建，无需补装或修补文件。"""
    project = project_factory("first-project", defaults=True)
    _assert_project_layout(project)
    assert (project.path / "node_modules").is_dir(), "create did not install the project's dependencies"
    assert (project.path / ".git").is_dir(), "create did not initialize the project's Git repository"
    git = OffckbRunner(
        ("git",), env=project.cli.env, cwd=project.path,
        records_dir=project.cli.records_dir.parent / "git",
    )
    git_root = git.run("rev-parse", "--show-toplevel", json_mode=False).stdout.strip()
    assert Path(git_root).resolve() == project.path.resolve()
    git.run("rev-parse", "--verify", "HEAD", json_mode=False)
    project.run("run", "build")
    _assert_build(project)


# TEST-MAP: PROJ-01
def test_custom_project_respects_path_names_and_opt_outs(project_factory) -> None:
    """用户指定自己的项目、合约名和嵌套位置，选择稍后安装依赖且不初始化 Git。"""
    project = project_factory("my-ckb-app", contract="greeting")
    _assert_project_layout(project)
    assert not (project.path / "node_modules").exists()
    assert not (project.path / ".git").exists()


# TEST-MAP: PROJ-02
def test_existing_project_is_preserved_and_another_directory_can_be_used(project_factory) -> None:
    """误选已有项目时不覆盖用户代码，换一个目录后可以继续创建。"""
    project = project_factory("existing-project", contract="saved-contract")
    source = project.path / "contracts/saved-contract/src/index.ts"
    source.write_text(source.read_text() + "\n// User's saved contract change.\n")
    (project.path / "user-settings.json").write_text('{"keepMyConfiguration": true}\n')
    (project.path / "notes").mkdir()
    before = _tree_snapshot(project.path)
    result = project.cli.run(
        "create", project.path, "--no-interactive", "--language", "typescript", "--manager", "pnpm",
        "--no-install", "--no-git", check=False, json_mode=False,
    )
    assert result.returncode != 0
    assert "already exists" in (result.stdout + result.stderr).lower()
    assert _tree_snapshot(project.path) == before, "create altered the existing project"
    replacement = project_factory("replacement-project", contract="new-contract")
    _assert_project_layout(replacement)
    assert _tree_snapshot(project.path) == before


@pytest.fixture(scope="module")
def built_project(project_factory) -> Project:
    project = project_factory("runnable-project", contract="my-first-contract")
    project.run("install")
    project.run("run", "build")
    _assert_build(project)
    return project


# TEST-MAP: PROJ-03
def test_generated_project_can_be_installed_and_built_manually(built_project: Project) -> None:
    """用户按项目说明手动安装依赖，并构建自定义名称合约。"""
    _assert_build(built_project)
    assert (built_project.path / "node_modules").is_dir()


def _deployment_record(project: Project) -> dict[str, Any]:
    records = _read_json(project.path / "deployment/scripts.json")
    assert set(records["devnet"]) == {f"{project.contract}.bc"}
    return records["devnet"][f"{project.contract}.bc"]


def _deployment_outpoint(record: dict[str, Any]) -> dict[str, Any]:
    assert len(record["cellDeps"]) == 1
    dependency = record["cellDeps"][0]["cellDep"]
    assert dependency["depType"] == "code"
    point = dependency["outPoint"]
    return {"tx_hash": point["txHash"], "index": hex(hex_int(point["index"]))}


@pytest.fixture(scope="module")
def deployed_project(
    built_project: Project, devnet_manager: DevnetManager, rpc: RpcClient, accounts: list[Account],
) -> Project:
    devnet_manager.ensure_running()
    rpc.wait_indexer()
    # The generated deploy script inherits this supported environment variable.
    # Never pass --privkey: the generated script prints its child command line.
    built_project.pnpm.env["OFFCKB_PRIVATE_KEY"] = accounts[8].private_key
    try:
        built_project.run("run", "deploy", "--network", "devnet", "--yes")
    finally:
        built_project.pnpm.env.pop("OFFCKB_PRIVATE_KEY", None)
    point = _deployment_outpoint(_deployment_record(built_project))
    transaction = rpc.wait_transaction(point["tx_hash"])
    assert transaction["tx_status"]["status"] == "committed"
    rpc.wait_indexer(transaction["tx_status"]["block_number"])
    return built_project


# TEST-MAP: PROJ-04
def test_generated_deploy_script_records_the_live_built_contract(
    deployed_project: Project, rpc: RpcClient, accounts: list[Account],
) -> None:
    """用户运行项目部署脚本后，部署记录指向自己的已确认合约代码。"""
    record = _deployment_record(deployed_project)
    point = _deployment_outpoint(record)
    cell = rpc.get_live_cell(point["tx_hash"], hex_int(point["index"]))
    assert cell["status"] == "live"
    content = (deployed_project.path / "dist" / f"{deployed_project.contract}.bc").read_bytes()
    assert cell["cell"]["data"]["content"] == "0x" + content.hex()
    assert record["codeHash"] == cell["cell"]["data"]["hash"]
    assert record["hashType"] == "data2"
    assert cell["cell"]["output"]["lock"] == rpc_script(accounts[8].lock_script)


# TEST-MAP: PROJ-05
def test_generated_examples_execute_and_call_this_deployment(deployed_project: Project, rpc: RpcClient) -> None:
    """用户运行原 mock/devnet 示例，实际调用刚部署的合约并完成链上确认。"""
    project = deployed_project
    record = _deployment_record(project)
    point = _deployment_outpoint(record)
    report = project.path.parent / "example-tests.json"
    result = project.run("run", "test:only", "--runInBand", "--json", "--outputFile", str(report))
    summary = _read_json(report)
    assert summary["success"] is True
    assert summary["numPassedTests"] >= 2
    assert summary["numPendingTests"] == 0 and summary["numFailedTests"] == 0
    suites = {Path(suite["name"]).name: suite for suite in summary["testResults"]}
    assert set(suites) == {f"{project.contract}.mock.test.ts", f"{project.contract}.devnet.test.ts"}
    for suite in suites.values():
        assert suite["status"] == "passed"
        assert suite["assertionResults"] and all(test["status"] == "passed" for test in suite["assertionResults"])
    hashes = re.findall(r"Transaction sent:\s*(0x[0-9a-fA-F]{64})", result.stdout + result.stderr)
    assert len(set(hashes)) == 1, "the devnet example did not report its submitted contract transaction"
    transaction = rpc.wait_transaction(hashes[0])
    assert transaction["tx_status"]["status"] == "committed"
    body = transaction["transaction"]
    assert {"out_point": point, "dep_type": "code"} in body["cell_deps"]
    system = _read_json(project.path / "deployment/system-scripts.json")["devnet"]["ckb_js_vm"]["script"]
    hash_type = {"data": "00", "type": "01", "data1": "02", "data2": "04"}[record["hashType"]]
    expected_args = "0x0000" + record["codeHash"][2:] + hash_type + "00" * 32
    assert any(
        output.get("type") == {
            "code_hash": system["codeHash"], "hash_type": system["hashType"], "args": expected_args,
        }
        for output in body["outputs"]
    ), "the devnet example did not execute the JS VM with this deployment's contract"
    assert _deployment_record(project) == record
