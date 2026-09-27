"""Build genuine 0.4.13 user state before switching the packaged CLI."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import pytest

from tests.asset_failure_support import assert_cli_failure
from tests.conftest import (
    _empty_user_env, _install_offckb_package, _resolve_pnpm,
    _resolve_pnpm_cache, _resolve_pnpm_store, _safe_host_tool_env,
)
from tests.harness import (
    DevnetManager, OffckbRunner,
    hex_int, is_port_open, transaction_hash_from, wait_until,
)
from .environment import ALL_PORTS, FNN_PORTS, FiberEnvironment
from .flows import _key_file

CONFIG_FILES = ("ckb.toml", "ckb-miner.toml", "specs/dev.toml")
CUSTOM_GENESIS_MESSAGE = "offckb-0.4.13-fiber-upgrade-acceptance"


@dataclass(frozen=True)
class LegacyCli:
    executable: Path
    entry: Path


def install_legacy_cli(pytestconfig: pytest.Config, run_root: Path) -> LegacyCli:
    configured = os.environ.get("OFFCKB_LEGACY_PACKAGE")
    candidates = [Path(configured).expanduser()] if configured else [
        Path(pytestconfig.rootpath) / "source/.prepared/packages/offckb-cli-0.4.13.tgz",
        Path(pytestconfig.rootpath) / "source/.prepared/offckb-cli.tgz",
    ]
    package = None
    for candidate in candidates:
        if not candidate.is_file():
            continue
        with tarfile.open(candidate) as archive:
            member = archive.extractfile("package/package.json")
            assert member is not None
            manifest = json.load(member)
        if manifest.get("name") == "@offckb/cli" and manifest.get("version") == "0.4.13":
            package = candidate.absolute()
            break
    if package is None:
        raise pytest.UsageError(
            "Fiber upgrade tests require the actual @offckb/cli 0.4.13 npm tarball; "
            "prepare it separately and set OFFCKB_LEGACY_PACKAGE. No package is downloaded by the test."
        )
    root = run_root / "legacy-offckb-0.4.13"
    for name in ("home", "workspace", "commands", "tmp"):
        (root / name).mkdir(parents=True)
    environment = _empty_user_env(root)
    pnpm = _resolve_pnpm(pytestconfig)
    store = _resolve_pnpm_store(
        pytestconfig, pnpm, cwd=root / "workspace", env=_safe_host_tool_env(), run_root=root,
    )
    executable, entry, _ = _install_offckb_package(
        package, pnpm, store, _resolve_pnpm_cache(pytestconfig), env=environment, run_root=root,
    )
    runner = OffckbRunner((str(executable),), cli_entry=entry, env=environment,
                          cwd=root / "workspace", records_dir=root / "commands")
    version = runner.run("--version", json_mode=False)
    assert version.stdout.strip() == "0.4.13"
    (root / "package.json").write_text(json.dumps({
        "package": str(package), "version": version.stdout.strip(),
        "sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
    }, indent=2) + "\n")
    return LegacyCli(executable, entry)


def configs(env: FiberEnvironment) -> dict[str, bytes]:
    return {name: (env.config_path / name).read_bytes() for name in CONFIG_FILES}


def chain_data(env: FiberEnvironment) -> dict[str, str]:
    """Fingerprint closed database files, excluding append-only command/log output."""
    root = env.config_path / "data" / "db"
    assert root.is_dir(), "the old CLI must have initialized a real on-disk chain database"
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


@dataclass(frozen=True)
class LegacyState:
    configs: dict[str, bytes]
    genesis: str
    transaction: dict[str, Any]
    tip: int
    database: dict[str, str]

    @property
    def tx_hash(self) -> str:
        return self.transaction["transaction"]["hash"]


def prepare_legacy_state(env: FiberEnvironment, legacy: LegacyCli) -> LegacyState:
    assert not env.config_path.exists(), "old state must originate from the real 0.4.13 CLI"
    runner = OffckbRunner((str(legacy.executable),), cli_entry=legacy.entry, env=env.runner.env,
                          cwd=env.runner.cwd, records_dir=env.root / "legacy-commands")
    for account in env.accounts:
        runner.register_secret(account.private_key)
    manager = DevnetManager(runner, env.rpc, env.proxy_rpc, env.managed_ckb,
                            startup_timeout_s=env.startup_timeout_s)
    try:
        manager.start()
        assert env.rpc.call("local_node_info")["version"].startswith(env.ckb_version)
        manager.stop()
        before = tomllib.loads((env.config_path / "ckb.toml").read_text())
        miner = tomllib.loads((env.config_path / "ckb-miner.toml").read_text())
        runner.run(
            "devnet", "config", "--set", f"ckb.logger.color={str(not before['logger']['color']).lower()}",
            "--set", f"miner.client.poll_interval={miner['miner']['client']['poll_interval'] + 1}",
        )
        # This is a user's custom OLD chain spec. Never remove new-package contracts
        # to manufacture an old chain; the generating executable above is 0.4.13.
        spec = env.config_path / "specs" / "dev.toml"
        original = spec.read_text()
        assert not any(name in original for name in ("funding-lock", "commitment-lock", "funding_lock", "commitment_lock"))
        customized, replacements = re.subn(
            r'(?m)^message\s*=\s*"[^"\n]*"', f'message = "{CUSTOM_GENESIS_MESSAGE}"', original, count=1,
        )
        assert replacements == 1
        spec.write_text(customized)
        runner.run("clean", "-d")
        manager.start()
        env.rpc.wait_indexer()
        sent = runner.run(
            "transfer", env.accounts[1].address, "100", "--network", "devnet",
            "--privkey-file", _key_file(env, env.accounts[0]),
        )
        transaction = env.rpc.wait_transaction(transaction_hash_from(sent))
        env.rpc.wait_indexer(transaction["tx_status"]["block_number"])
        assert hex_int(transaction["tx_status"]["block_number"]) > 0
        genesis, tip = env.rpc.call("get_block_hash", ["0x0"]), env.rpc.tip()
        assert all(not is_port_open(port) for port in FNN_PORTS)
        manager.stop()
        return LegacyState(configs(env), genesis, transaction, tip, chain_data(env))
    finally:
        manager.close()


@contextmanager
def upgraded_ckb(env: FiberEnvironment) -> Iterator[DevnetManager]:
    manager = DevnetManager(env.runner, env.rpc, env.proxy_rpc, env.managed_ckb,
                            startup_timeout_s=env.startup_timeout_s)
    try:
        manager.start(use_managed_binary=True)
        yield manager
    finally:
        manager.close()


def assert_legacy_chain(env: FiberEnvironment, old: LegacyState) -> None:
    assert configs(env) == old.configs
    assert env.rpc.call("get_block_hash", ["0x0"]) == old.genesis
    recovered = env.rpc.wait_transaction(old.tx_hash)
    assert recovered["transaction"] == old.transaction["transaction"]
    assert recovered["tx_status"]["block_hash"] == old.transaction["tx_status"]["block_hash"]
    assert env.rpc.tip() >= old.tip


def assert_missing_contracts(result: Any) -> None:
    message = assert_cli_failure(result).lower()
    assert "fiber" in message and "contract" in message
    assert "offckb clean" in message and "stop" in message
    assert "delete" in message and "data" in message and "channel" in message
    assert "start" in message or "rebuild" in message


def assert_no_fnn(env: FiberEnvironment) -> None:
    wait_until(lambda: not any(is_port_open(p) for p in FNN_PORTS), timeout_s=20,
               description="failed upgrade to leave no FNN listeners")
    assert not env.runtime_path.exists()
    assert not env.pid_files[1].exists()
    env.remember_processes()
    assert all("fnn" not in command.lower() for command in env.process_commands().values())


def assert_no_components(env: FiberEnvironment) -> None:
    assert_no_fnn(env)
    wait_until(lambda: not any(is_port_open(p) for p in ALL_PORTS), timeout_s=20,
               description="failed combined upgrade to release all component ports")
    assert all(not path.exists() for path in env.pid_files)
    assert not env.process_commands()


def assert_old_transaction_absent(env: FiberEnvironment, old: LegacyState) -> None:
    # CKB 0.208 returns a structured unknown response, not JSON null.
    result = env.rpc.call("get_transaction", [old.tx_hash])
    assert result["transaction"] is None and result["tx_status"]["status"] == "unknown", result
    for index in range(len(old.transaction["transaction"]["outputs"])):
        assert env.rpc.get_live_cell(old.tx_hash, index)["status"] != "live"
