from __future__ import annotations

import hashlib
import json
import re
import stat
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from .conftest import _create_isolated_env
from .harness import (
    DIRECT_RPC_URL,
    PROXY_RPC_URL,
    Account,
    DevnetManager,
    OffckbRunner,
    RpcClient,
    _configured_devnet_paths,
    _process_group_commands,
    hex_int,
    transaction_hash_from,
    wait_until,
)


pytestmark = [pytest.mark.core]

CONFIG_FILES = ("ckb.toml", "ckb-miner.toml", "specs/dev.toml")


def _read_configs(root: Path) -> dict[str, Any]:
    return {name: tomllib.loads((root / name).read_text(encoding="utf-8")) for name in CONFIG_FILES}


def _file_fingerprint(path: Path) -> tuple[int, str | None, str]:
    """Also detect a removed/replaced managed-binary link or lost executable bit."""
    assert path.is_file(), f"protected file was removed: {path}"
    link = str(path.readlink()) if path.is_symlink() else None
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return stat.S_IMODE(path.stat().st_mode), link, digest


@dataclass(frozen=True)
class SavedDevelopment:
    devnet: DevnetManager
    data_path: Path
    defaults: dict[str, Any]
    customized: dict[str, Any]
    genesis: str
    transaction: dict[str, Any]
    tip: dict[str, Any]
    protected: dict[Path, tuple[int, str | None, str]]

    @property
    def tx_hash(self) -> str:
        return self.transaction["transaction"]["hash"]

    @property
    def cache_path(self) -> Path:
        return self.devnet.config_path / "transactions" / f"{self.tx_hash}.json"

    def assert_outside_files_unchanged(self) -> None:
        for path, expected in self.protected.items():
            assert _file_fingerprint(path) == expected, f"protected file changed: {path}"


@pytest.fixture
def saved_development(
    devnet_manager: DevnetManager,
    offckb: OffckbRunner,
    run_root: Path,
    ckb_bin: Path,
    accounts: list[Account],
    private_key_file: Callable[[Account], Path],
    pytestconfig: pytest.Config,
    request: pytest.FixtureRequest,
) -> Iterator[SavedDevelopment]:
    """独立准备用户已保存配置、完成转账并正常停止的开发环境。"""
    devnet_manager.close()
    root = run_root / "devnet-state" / request.node.name
    for directory in ("home", "workspace", "commands", "tmp"):
        (root / directory).mkdir(parents=True)
    runner = OffckbRunner(
        offckb.command,
        cli_entry=offckb.cli_entry,
        env=_create_isolated_env(root, ckb_bin),
        cwd=root / "workspace",
        records_dir=root / "commands",
    )
    devnet = DevnetManager(
        runner, RpcClient(DIRECT_RPC_URL), RpcClient(PROXY_RPC_URL), ckb_bin,
        startup_timeout_s=pytestconfig.getoption("--startup-timeout"),
    )
    config_path, data_path = _configured_devnet_paths(runner.env)
    assert config_path.is_relative_to(root) and data_path.is_relative_to(config_path)
    assert not config_path.exists(), "the CLI must initialize this case's own devnet"

    try:
        devnet.start()
        # Capture normal initialization, including the CLI's miner RPC URL normalization.
        defaults = _read_configs(config_path)
        devnet.stop()
        default_color = defaults["ckb.toml"]["logger"]["color"]
        default_poll = defaults["ckb-miner.toml"]["miner"]["client"]["poll_interval"]
        assert isinstance(default_color, bool)
        assert type(default_poll) is int and default_poll > 0
        custom_color, custom_poll = not default_color, default_poll + 1
        runner.run(
            "devnet", "config", "--set", f"ckb.logger.color={str(custom_color).lower()}",
            "--set", f"miner.client.poll_interval={custom_poll}",
        )
        customized = _read_configs(config_path)
        assert customized["ckb.toml"]["logger"]["color"] is custom_color
        assert customized["ckb-miner.toml"]["miner"]["client"]["poll_interval"] == custom_poll
        assert customized["specs/dev.toml"] == defaults["specs/dev.toml"]

        devnet.start()
        rpc = devnet.rpc
        rpc.wait_indexer()
        sender, receiver = accounts[4], accounts[5]
        transfer = runner.run(
            "transfer", receiver.address, "100", "--network", "devnet",
            "--privkey-file", private_key_file(sender), "--proxy-rpc",
        )
        transaction = rpc.wait_transaction(transaction_hash_from(transfer))
        assert hex_int(transaction["tx_status"]["block_number"]) > 0
        rpc.wait_indexer(transaction["tx_status"]["block_number"])
        genesis = rpc.call("get_block_hash", ["0x0"])
        tip = rpc.call("get_tip_header")
        assert rpc.call("get_live_cell", [
            {"tx_hash": transaction["transaction"]["hash"], "index": "0x0"}, False,
        ])["status"] == "live"
        devnet.stop()
        assert _read_configs(config_path) == customized
        assert data_path.is_dir() and any(path.is_file() for path in (data_path / "db").rglob("*")), (
            "the clean precondition requires a real, nonempty chain database"
        )

        project_file = root / "workspace" / "saved-project" / "src" / "main.rs"
        project_file.parent.mkdir(parents=True)
        project_file.write_text('fn main() { println!("keep my work"); }\n', encoding="utf-8")
        sibling_file = config_path.parent / "other-user-data.txt"
        sibling_file.write_text("data outside devnet must survive\n", encoding="utf-8")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        managed_binaries = [path for path in (root / "managed-bins").rglob("*") if path.is_file()]
        assert managed_binaries, "the managed binary must exist before checking clean's scope"
        protected_paths = [Path(manifest["settingsFile"]), project_file, sibling_file, *managed_binaries]
        for path in protected_paths:
            assert path.is_relative_to(root) and not path.is_relative_to(config_path)

        yield SavedDevelopment(
            devnet, data_path, defaults, customized, genesis, transaction, tip,
            {path: _file_fingerprint(path) for path in protected_paths},
        )
    finally:
        devnet.close()


def _assert_old_transaction_cleared(state: SavedDevelopment) -> None:
    rpc = state.devnet.rpc
    transaction = rpc.call("get_transaction", [state.tx_hash])
    assert transaction is None or transaction["tx_status"]["status"] != "committed", (
        "the old transaction is still committed after cleaning chain data"
    )
    assert rpc.call("get_live_cell", [{"tx_hash": state.tx_hash, "index": "0x0"}, False])["status"] != "live"


def _assert_chain_advances(devnet: DevnetManager, previous_tip: int) -> None:
    wait_until(
        lambda: devnet.rpc.tip() > previous_tip,
        timeout_s=30, interval_s=0.5, description="the restarted development chain to keep mining",
    )


# TEST-MAP: NODE-06
def test_restart_preserves_configuration_and_development_progress(saved_development: SavedDevelopment) -> None:
    """用户下次启动时保留自定义配置和已完成交易，并沿原链继续开发。"""
    state = saved_development
    devnet = state.devnet
    devnet.start()

    assert _read_configs(devnet.config_path) == state.customized
    assert devnet.rpc.call("get_block_hash", ["0x0"]) == state.genesis
    transaction = devnet.rpc.call("get_transaction", [state.tx_hash])
    assert transaction and transaction["tx_status"]["status"] == "committed"
    assert transaction["tx_status"]["block_hash"] == state.transaction["tx_status"]["block_hash"]
    assert transaction["transaction"] == state.transaction["transaction"]
    assert devnet.rpc.call("get_block_hash", [state.tip["number"]]) == state.tip["hash"]
    restarted_tip = devnet.rpc.tip()
    assert restarted_tip >= hex_int(state.tip["number"])
    _assert_chain_advances(devnet, restarted_tip)


# TEST-MAP: NODE-07
def test_clean_data_rebuilds_chain_without_losing_configuration(saved_development: SavedDevelopment) -> None:
    """用户只重置链数据，保留开发配置和目录外文件，再启动重新实验。"""
    state = saved_development
    devnet = state.devnet
    devnet.runner.run("clean", "-d")

    # Observe deletion before the CLI gets a chance to recreate directories.
    assert not state.data_path.exists(), "clean -d did not remove chain data"
    assert _read_configs(devnet.config_path) == state.customized
    state.assert_outside_files_unchanged()

    devnet.start()
    assert _read_configs(devnet.config_path) == state.customized
    assert devnet.rpc.call("get_block_hash", ["0x0"]) == state.genesis
    _assert_chain_advances(devnet, devnet.rpc.tip())
    _assert_old_transaction_cleared(state)
    state.assert_outside_files_unchanged()


# TEST-MAP: NODE-08
def test_full_clean_restores_defaults_and_removes_development_state(saved_development: SavedDevelopment) -> None:
    """用户完整重置开发链，恢复随包配置并清掉交易与调试缓存，其他数据不变。"""
    state = saved_development
    devnet = state.devnet
    # This is a real transaction cached by the proxy, not a fabricated debug dump.
    cached = json.loads(state.cache_path.read_text(encoding="utf-8"))
    # Submitted CKB-only outputs can omit the optional type; RPC returns it as null.
    cached["outputs"] = [{**output, "type": output.get("type")} for output in cached["outputs"]]
    assert cached == {key: value for key, value in state.transaction["transaction"].items() if key != "hash"}
    package_defaults = _read_configs(devnet.runner.cli_entry.parent.parent / "ckb" / "devnet")
    assert state.defaults["ckb.toml"]["logger"]["color"] == package_defaults["ckb.toml"]["logger"]["color"]
    assert state.defaults["ckb-miner.toml"]["miner"]["client"]["poll_interval"] == (
        package_defaults["ckb-miner.toml"]["miner"]["client"]["poll_interval"]
    )
    assert state.defaults["specs/dev.toml"] == package_defaults["specs/dev.toml"]

    devnet.runner.run("clean")
    assert not devnet.config_path.exists(), "full clean did not remove the entire devnet root"
    state.assert_outside_files_unchanged()

    devnet.start()
    assert _read_configs(devnet.config_path) == state.defaults
    assert devnet.rpc.call("get_block_hash", ["0x0"]) == state.genesis
    _assert_chain_advances(devnet, devnet.rpc.tip())
    _assert_old_transaction_cleared(state)
    assert not state.cache_path.exists(), "the old debug transaction cache survived full clean"
    state.assert_outside_files_unchanged()


# TEST-MAP: NODE-12
@pytest.mark.parametrize("clean_args", [(), ("-d",)], ids=["full", "data-only"])
def test_clean_running_chain_is_rejected_without_losing_progress(
    saved_development: SavedDevelopment, clean_args: tuple[str, ...],
) -> None:
    """忘记停止后台链时，清理应提示先停链并保留正在使用的开发环境。"""
    state = saved_development
    devnet = state.devnet
    devnet.start()
    configs = {name: (devnet.config_path / name).read_bytes() for name in CONFIG_FILES}
    metadata = devnet.pid_file.read_bytes()
    assert devnet.pgid is not None
    processes = _process_group_commands(devnet.pgid)
    assert devnet.pid in processes and len(processes) >= 3

    try:
        result = devnet.runner.run("clean", *clean_args, check=False, timeout_s=30)
        # Inspect the filesystem before RPCs or teardown can mask deletion.
        database_present = (state.data_path / "db").is_dir()
        assert database_present, (
            f"clean removed the running chain database; exit={result.returncode}; "
            f"stdout={result.stdout!r}; stderr={result.stderr!r}"
        )
        for name, content in configs.items():
            assert (devnet.config_path / name).read_bytes() == content, f"clean changed {name}"
        assert devnet.pid_file.read_bytes() == metadata
        state.assert_outside_files_unchanged()

        assert result.returncode != 0, f"clean accepted a running chain: {result.stdout}"
        assert not result.stdout.strip(), result.stdout
        records = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
        errors = [record for record in records if record.get("ok") is False]
        assert len(errors) == 1 and errors[0].get("code"), result.stderr
        assert re.search(r"\b(?:offckb\s+)?node\s+stop\b", errors[0].get("message", "")), result.stderr

        assert devnet.rpc.ready() and devnet.proxy_rpc.ready()
        transaction = devnet.rpc.call("get_transaction", [state.tx_hash])
        assert transaction and transaction["tx_status"]["status"] == "committed"
        assert transaction["transaction"] == state.transaction["transaction"]
        _assert_chain_advances(devnet, devnet.rpc.tip())
        assert _process_group_commands(devnet.pgid) == processes

        # Reopen the database to catch deletions hidden by live handles or caches.
        devnet.stop()
        devnet.start()
        restored = devnet.rpc.call("get_transaction", [state.tx_hash])
        assert restored and restored["tx_status"]["status"] == "committed"
        assert restored["transaction"] == state.transaction["transaction"]
        assert devnet.rpc.call("get_block_hash", [state.tip["number"]]) == state.tip["hash"]
    finally:
        # A broken clean can delete the PID file while leaving the processes alive.
        # Only after observing the outcome, stop this fixture's verified group so
        # the ordinary fixture teardown can still finish without stale listeners.
        if devnet.pid is not None and not devnet.pid_file.exists():
            devnet._stop_owned_process(devnet.pid, known_pgid=devnet.pgid)
