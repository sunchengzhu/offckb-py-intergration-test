from __future__ import annotations

import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import _empty_user_env
from tests.harness import Account, DevnetManager, OffckbRunner, RpcClient
from .environment import FIBER_MODES, FiberEnvironment, prepare_managed_tools


@pytest.fixture(scope="session")
def fnn_bin(pytestconfig: pytest.Config, package_default_settings: dict[str, Any]) -> Path:
    version = package_default_settings["bins"].get("defaultFnnVersion")
    if version != "0.9.0":
        raise pytest.UsageError(
            "Fiber acceptance requires the 0.5.0 canary package with default FNN 0.9.0; "
            "prepare the requested canary package before selecting tests/fiber."
        )
    configured = pytestconfig.getoption("--fnn-bin") or os.environ.get("FNN_BIN")
    if not configured:
        raise pytest.UsageError("Pass --fnn-bin or FNN_BIN pointing to fnn in a complete FNN 0.9.0 release package.")
    binary = Path(configured).expanduser().absolute()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise pytest.UsageError(f"FNN binary is not executable: {binary}")
    result = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10, check=False)
    reported = re.search(r"\bv?(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)\b", result.stdout)
    if result.returncode != 0 or reported is None or reported.group(1) != version:
        raise pytest.UsageError(f"The provided FNN binary must report {version}: {binary}")
    config = binary.parent / "config" / "testnet" / "config.yml"
    if not config.is_file() or not config.read_text().strip():
        raise pytest.UsageError(f"FNN release configuration is missing or empty: {config}; use the complete release package.")
    return binary


@pytest.fixture(scope="module")
def fiber_environment(
    fixed_port_lease: None, devnet_manager: DevnetManager, offckb: OffckbRunner,
    run_root: Path, default_ckb_bin: Path, fnn_bin: Path,
    package_default_settings: dict[str, Any], accounts: list[Account], pytestconfig: pytest.Config,
):
    """Fresh user state per context, sharing only the verified installed CLI and port lease."""
    @contextmanager
    def create(mode: str):
        if mode not in FIBER_MODES:
            raise ValueError(f"Unknown Fiber mode: {mode}")
        devnet_manager.close()
        root = Path(tempfile.mkdtemp(prefix=f"fiber-{mode}-", dir=run_root))
        for name in ("home", "workspace", "commands", "tmp", "secrets"):
            (root / name).mkdir()
        env = _empty_user_env(root)
        managed_ckb, managed_fnn, config_path = prepare_managed_tools(
            root, env, package_default_settings, default_ckb_bin, fnn_bin,
        )
        timeout_s = pytestconfig.getoption("--tx-timeout")
        runner = OffckbRunner(
            offckb.command, cli_entry=offckb.cli_entry, env=env,
            cwd=root / "workspace", records_dir=root / "commands", default_timeout_s=timeout_s,
        )
        direct_url = package_default_settings["devnet"]["rpcUrl"]
        proxy_url = f"http://127.0.0.1:{package_default_settings['devnet']['rpcProxyPort']}"
        instance = FiberEnvironment(
            runner=runner, rpc=RpcClient(direct_url, tx_timeout_s=timeout_s),
            proxy_rpc=RpcClient(proxy_url, tx_timeout_s=timeout_s),
            fnn=tuple(RpcClient(f"http://127.0.0.1:{port}", tx_timeout_s=timeout_s) for port in (21714, 21715)),
            accounts=accounts, timeout_s=timeout_s, root=root, config_path=config_path, mode=mode,
            managed_ckb=managed_ckb, managed_fnn=managed_fnn,
            startup_timeout_s=pytestconfig.getoption("--startup-timeout"),
            ckb_version=package_default_settings["bins"]["defaultCKBVersion"],
            fnn_version=package_default_settings["bins"]["defaultFnnVersion"],
        )
        try:
            instance.start()
            yield instance
        except BaseException as error:
            try:
                instance.close()
            except BaseException as cleanup_error:
                error.add_note(f"Fiber cleanup also failed: {cleanup_error}")
            raise
        else:
            instance.close()

    return create


@pytest.fixture(scope="module", params=FIBER_MODES)
def fiber_env(request: pytest.FixtureRequest, fiber_environment):
    with fiber_environment(request.param) as environment:
        yield environment
