from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from .conftest import _resolve_pnpm, _resolve_pnpm_cache, _resolve_pnpm_store, _safe_host_tool_env
from .harness import Account, OffckbRunner


@dataclass
class Project:
    path: Path
    contract: str
    cli: OffckbRunner
    pnpm: OffckbRunner

    def run(self, *args: str):
        return self.pnpm.run(*args, cwd=self.path, json_mode=False, timeout_s=240)


@pytest.fixture(scope="module")
def project_factory(
    offckb: OffckbRunner,
    run_root: Path,
    pytestconfig: pytest.Config,
    accounts: list[Account],
    request: pytest.FixtureRequest,
) -> Iterator[Any]:
    """Prepare real tools; generated files and scripts remain the product's responsibility."""
    root = run_root / "projects" / request.module.__name__
    root.mkdir(parents=True)
    pnpm = _resolve_pnpm(pytestconfig)
    debugger_value = pytestconfig.getoption("--ckb-debugger-bin")
    if not debugger_value:
        raise pytest.UsageError("Project tests require a native CKB_DEBUGGER_BIN / --ckb-debugger-bin")
    debugger = Path(debugger_value).expanduser().resolve()
    try:
        version = subprocess.run([str(debugger), "--version"], capture_output=True, text=True, timeout=10)
    except OSError as error:
        raise pytest.UsageError(f"CKB_DEBUGGER_BIN cannot run on this platform: {debugger}: {error}") from error
    version_match = re.search(r"\d+\.\d+\.\d+(?:-[\w.]+)?", version.stdout)
    if version.returncode != 0 or version_match is None:
        raise pytest.UsageError(f"CKB_DEBUGGER_BIN --version failed: {debugger}")
    settings_result = offckb.run("config", "list")
    settings = []
    for line in settings_result.stderr.splitlines():
        try:
            value = json.loads(json.loads(line).get("message", ""))
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "tools" in value:
            settings.append(value)
    assert len(settings) == 1, "config list must expose the packaged tool settings"
    tool_settings = settings[0]["tools"]
    actual = tuple(int(part) for part in version_match.group().split("-")[0].split("."))
    minimum = tuple(int(part) for part in tool_settings["ckbDebugger"]["minVersion"].split("."))
    if actual < minimum or (actual == minimum and "-" in version_match.group()):
        raise pytest.UsageError(
            f"CKB_DEBUGGER_BIN {version_match.group()} is older than the package requires "
            f"({tool_settings['ckbDebugger']['minVersion']}); prepare a suitable native binary first"
        )
    managed_debugger = Path(tool_settings["rootFolder"]) / "ckb-debugger"
    assert managed_debugger.is_relative_to(Path(offckb.env["HOME"])), "tool path escaped the isolated HOME"
    managed_debugger.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(debugger, managed_debugger)
    managed_debugger.chmod(0o755)

    # Only the package manager uses its shared content/metadata caches. HOME and
    # every OffCKB root stay isolated, including during `offckb create`.
    store = _resolve_pnpm_store(
        pytestconfig, pnpm, cwd=root, env=_safe_host_tool_env(), run_root=run_root,
    )
    cache = _resolve_pnpm_cache(pytestconfig)
    tools_dir = root / "bin"
    tools_dir.mkdir()
    (tools_dir / "pnpm").symlink_to(pnpm)
    launcher = tools_dir / "offckb"
    launcher.write_text("#!/bin/sh\nexec " + shlex.join(offckb.command) + ' "$@"\n')
    launcher.chmod(0o755)
    env = {
        **offckb.env,
        "PATH": str(tools_dir) + os.pathsep + offckb.env["PATH"],
        "npm_config_store_dir": str(store),
        "npm_config_cache_dir": str(cache),
        "npm_config_offline": "false" if pytestconfig.getoption("--project-online") else "true",
        "GIT_AUTHOR_NAME": "OffCKB Integration Test",
        "GIT_COMMITTER_NAME": "OffCKB Integration Test",
        "GIT_AUTHOR_EMAIL": "offckb-test@example.invalid",
        "GIT_COMMITTER_EMAIL": "offckb-test@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    def create(
        name: str, *, contract: str = "user-contract", defaults: bool = False,
    ) -> Project:
        case_root = root / name
        case_root.mkdir()
        command_root = case_root / "commands"
        cli = OffckbRunner(
            offckb.command, cli_entry=offckb.cli_entry, env=env, cwd=case_root,
            records_dir=command_root / "offckb",
        )
        package_manager = OffckbRunner(
            (str(pnpm),), env=env, cwd=case_root, records_dir=command_root / "pnpm",
        )
        for account in accounts:
            cli.register_secret(account.private_key)
            package_manager.register_secret(account.private_key)
        path = case_root / "nested" / name
        args = ["create", str(path), "--no-interactive", "--language", "typescript", "--manager", "pnpm"]
        if not defaults:
            args += ["--contract-name", contract, "--no-install", "--no-git"]
        cli.run(*args, json_mode=False, timeout_s=240)
        selected_debugger = shutil.which("ckb-debugger", path=env["PATH"])
        assert selected_debugger and Path(selected_debugger).parent.resolve() == tools_dir.resolve(), (
            "create did not expose its managed debugger to the generated project; "
            "a debugger elsewhere on the host PATH must not conceal a missing project tool"
        )
        return Project(path, "hello-world" if defaults else contract, cli, package_manager)

    try:
        yield create
    finally:
        # Generated dotenv files contain the built-in signing key. Even a kept
        # failed runtime must not retain these credentials in project artifacts.
        for path in root.glob("*/nested/*/.env*"):
            path.unlink()
