"""首次下载、安装修复、下载失败恢复与真实终端确认。"""

from __future__ import annotations

import hashlib
import io
import os
import re
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

import pytest

from tests.harness import _process_alive, _process_command
from .environment import FiberEnvironment
from .flows import (
    assert_close_amounts, channel_pair, close_channel, opened_ckb_channel, pay_invoice,
)
from .installation_support import ReleaseProxy, controlled_downloads, file_manifest
from .support_terminal import TerminalCommand
from .support_configuration import assert_failure, assert_failed_start_is_clean


pytestmark = pytest.mark.fiber


@pytest.fixture
def network_enabled(pytestconfig: pytest.Config) -> None:
    if not pytestconfig.getoption("--fiber-network"):
        raise pytest.UsageError("Fiber installation tests need --fiber-network; offline runs must exclude the network marker.")


@pytest.fixture
def terminal_enabled(pytestconfig: pytest.Config) -> None:
    if not pytestconfig.getoption("--fiber-tty"):
        raise pytest.UsageError("Fiber terminal tests need --fiber-tty; noninteractive runs must exclude the tty marker.")
    if os.name != "posix":
        raise pytest.UsageError("Fiber terminal acceptance currently needs Linux/macOS; no Windows platform emulation is used.")


def assert_installed_release(env: FiberEnvironment, proxy: ReleaseProxy, *, after: int = 0) -> None:
    proxy.assert_valid_download(after=after)
    binary = env.managed_fnn
    assert binary.is_file() and not binary.is_symlink() and os.access(binary, os.X_OK)
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, timeout=10, check=False)
    assert version.returncode == 0 and re.search(r"\bv?0\.9\.0\b", version.stdout)
    # Match installed binary + all bundled config files to the independently
    # verified official bytes that the CLI actually requested through its proxy.
    assert proxy._archive is not None
    with tarfile.open(fileobj=io.BytesIO(proxy._archive), mode="r:gz") as archive:
        checked: set[str] = set()
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.parts and relative.parts[0] == proxy.asset.removesuffix(".tar.gz"):
                relative = PurePosixPath(*relative.parts[1:])
            assert not relative.is_absolute() and ".." not in relative.parts
            if not member.isfile() or not (str(relative) == "fnn" or relative.parts[:1] == ("config",)):
                continue
            source = archive.extractfile(member)
            assert source is not None
            with source:
                expected = hashlib.file_digest(source, "sha256").hexdigest()
            actual = binary.parent / str(relative)
            assert actual.is_file(), f"missing release file: {relative}"
            with actual.open("rb") as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == expected, f"changed release file: {relative}"
            checked.add(str(relative))
        assert {"fnn", "config/testnet/config.yml"} <= checked
    env.assert_ready_snapshot(env._public_snapshot())


def pay_and_close(env: FiberEnvironment):
    with opened_ckb_channel(env) as channel:
        pay_invoice(env, channel)
        closing = close_channel(env, channel)
        assert_close_amounts(env.rpc, channel, closing, (env.accounts[3], env.accounts[4]))
        return channel


def persistent_identity_files(env: FiberEnvironment) -> dict[str, tuple[str, str]]:
    manifest = file_manifest(env.config_path / "fiber")
    names = {"nodes.yml"}
    names.update(f"nodes/{node}/{name}" for node in (1, 2) for name in (
        "config.yml", "fiber/sk", "ckb/key", "password",
    ))
    assert names <= manifest.keys(), "the original Fiber environment must contain its identity and config files"
    return {name: manifest[name] for name in names}


def assert_no_fnn_after_failure(env: FiberEnvironment, result) -> None:
    assert_failed_start_is_clean(env, attached=True)
    assert result.returncode != 0 and not result.stdout.strip()
    assert "installed successfully" not in result.stderr.lower()
    assert not env.managed_fnn.exists(), "failed download installed an unverified FNN binary"


def assert_existing_chain_preserved(env: FiberEnvironment) -> None:
    before = env.attached_snapshot
    assert before is not None
    assert env.rpc.call("get_block_hash", ["0x0"]) == before["genesis"]
    assert env.rpc.call("get_transaction", [before["tx_hash"]]) == before["transaction"]
    for pid, command in before["commands"].items():
        assert _process_alive(pid) and _process_command(pid) == command, "download failure replaced or stopped the existing CKB"
    assert env.rpc.ready() and env.proxy_rpc.ready()


# TEST-MAP: FIB-INST-01
@pytest.mark.network
def test_first_fiber_use_downloads_the_real_platform_release(network_enabled, fiber_environment) -> None:
    with fiber_environment("combined-foreground", start=False, prepare_fnn=False) as env:
        assert not env.managed_fnn.parent.exists()
        env.startup_timeout_s = max(env.startup_timeout_s, 300)
        with controlled_downloads(env.runner, env.root) as proxy:
            env.start()
            assert_installed_release(env, proxy)
            pay_and_close(env)


# TEST-MAP: FIB-INST-02
@pytest.mark.network
@pytest.mark.parametrize("missing", ["fnn", "config/testnet/config.yml"], ids=["missing-binary", "missing-config"])
def test_incomplete_installation_repairs_then_reuses_it_offline(network_enabled, fiber_environment, missing: str) -> None:
    with fiber_environment("combined-daemon") as env:
        channel = pay_and_close(env)
        original_keys = [rpc.call("node_info")["pubkey"] for rpc in env.fnn]
        block_number = env.rpc.tip()
        block_hash = env.rpc.call("get_block_hash", [hex(block_number)])
        env.stop()
        preserved = persistent_identity_files(env)
        (env.managed_fnn.parent / missing).unlink()
        env.startup_timeout_s = max(env.startup_timeout_s, 300)
        with controlled_downloads(env.runner, env.root) as proxy:
            env.start()
            assert_installed_release(env, proxy)
            assert [rpc.call("node_info")["pubkey"] for rpc in env.fnn] == original_keys
            assert persistent_identity_files(env) == preserved
            assert env.rpc.call("get_block_hash", [hex(block_number)]) == block_hash
            assert all(item["state"]["state_name"] == "Closed" for item in channel_pair(env, channel.channel_id))
            env.stop()
            proxy.mode = "offline"
            before = len(proxy.requests)
            env.start()
            assert len(proxy.requests) == before, "complete local install still attempted network access"
            assert [rpc.call("node_info")["pubkey"] for rpc in env.fnn] == original_keys
            assert persistent_identity_files(env) == preserved
            assert env.rpc.call("get_block_hash", [hex(block_number)]) == block_hash
            assert env.rpc.wait_transaction(channel.outpoint["tx_hash"])["tx_status"]["status"] == "committed"
            assert all(item["state"]["state_name"] == "Closed" for item in channel_pair(env, channel.channel_id))
            pay_and_close(env)


# TEST-MAP: FIB-INST-03
@pytest.mark.network
@pytest.mark.parametrize("fault", ["disconnect", "truncated"], ids=["connection-failure", "interrupted-download"])
def test_download_failure_keeps_ckb_and_can_retry_in_same_environment(network_enabled, fiber_environment, fault: str) -> None:
    with fiber_environment("attached-daemon", start=False, prepare_fnn=False) as env:
        env.start_existing_chain()
        env.startup_timeout_s = max(env.startup_timeout_s, 300)
        with controlled_downloads(env.runner, env.root) as proxy:
            proxy.mode = fault
            result = assert_failure(env, ("fiber", "start", "--daemon"), r"fetch|download|socket|premature|closed|502")
            assert_no_fnn_after_failure(env, result)
            assert proxy.requests and proxy.requests[-1]["mode"] == fault
            assert not proxy.errors, proxy.errors
            assert_existing_chain_preserved(env)
            before = len(proxy.requests)
            proxy.mode = "valid"
            env.start(start_ckb=False)
            assert_installed_release(env, proxy, after=before)
            assert_existing_chain_preserved(env)
            pay_and_close(env)


# TEST-MAP: FIB-INST-04
@pytest.mark.network
def test_corrupt_download_is_rejected_before_install_and_valid_retry_works(network_enabled, fiber_environment) -> None:
    with fiber_environment("attached-daemon", start=False, prepare_fnn=False) as env:
        env.start_existing_chain()
        env.startup_timeout_s = max(env.startup_timeout_s, 300)
        with controlled_downloads(env.runner, env.root) as proxy:
            proxy.mode = "corrupt"
            result = assert_failure(env, ("fiber", "start", "--daemon"), r"SHA-256 checksum mismatch")
            assert_no_fnn_after_failure(env, result)
            assert proxy.requests and proxy.requests[-1]["body_sha256"] != proxy.sha256
            assert not proxy.errors, proxy.errors
            assert not env.managed_fnn.parent.exists(), "checksum failure created an installation directory"
            assert not list((env.root / "tmp").glob("offckb-fnn-*/extract")), "checksum failure extracted the corrupt package"
            assert_existing_chain_preserved(env)
            before = len(proxy.requests)
            proxy.mode = "valid"
            env.start(start_ckb=False)
            assert_installed_release(env, proxy, after=before)
            assert_existing_chain_preserved(env)
            pay_and_close(env)


# TEST-MAP: FIB-INST-05
@pytest.mark.tty
@pytest.mark.parametrize("data_only", [False, True], ids=["whole-fiber", "stores-only"])
def test_terminal_clean_defaults_to_no_and_requires_explicit_yes(terminal_enabled, fiber_environment, data_only: bool) -> None:
    with fiber_environment("combined-daemon") as env:
        pay_and_close(env)
        env.stop()
        fiber_root = env.config_path / "fiber"
        before = file_manifest(fiber_root)
        ckb_before = file_manifest(env.config_path / "data")
        tools_before = file_manifest(env.managed_ckb.parent.parent)
        assert ckb_before and before
        assert any("/fiber/store/" in name for name in before)
        arguments = ("fiber", "clean", *(('--data',) if data_only else ()))
        prompt = "Delete all FNN stores?" if data_only else "Delete the whole fiber environment?"

        def assert_scope(text: str) -> None:
            if data_only:
                assert "every FNN store" in text and "identity keys, passwords and logs are kept" in text
                for node in (1, 2):
                    assert str(fiber_root / "nodes" / str(node) / "fiber" / "store") in text
            else:
                assert "whole fiber environment" in text and "NEW node identities" in text
                assert "downloaded FNN binary and the devnet CKB data are kept" in text
                assert str(fiber_root) in text

        for name, answer in (("enter", "\n"), ("no", "n\n")):
            with TerminalCommand(env.runner, arguments, f"clean-{name}") as terminal:
                assert_scope(terminal.wait_prompt(prompt))
                assert file_manifest(fiber_root) == before, "clean changed data before confirmation"
                code, output = terminal.answer(answer)
                assert code != 0 and "Aborted" in output
            assert file_manifest(fiber_root) == before, "cancelled clean changed Fiber state"
            assert file_manifest(env.config_path / "data") == ckb_before
            assert file_manifest(env.managed_ckb.parent.parent) == tools_before
        with TerminalCommand(env.runner, arguments, "clean-yes") as terminal:
            assert_scope(terminal.wait_prompt(prompt))
            assert file_manifest(fiber_root) == before
            code, output = terminal.answer("y\n")
            assert code == 0 and "cleaned" in output.lower()
        if data_only:
            expected = {name: value for name, value in before.items()
                        if not any(name == f"nodes/{node}/fiber/store" or name.startswith(f"nodes/{node}/fiber/store/") for node in (1, 2))}
            assert file_manifest(fiber_root) == expected, "data-only clean removed identities/config or kept stores"
        else:
            assert not fiber_root.exists(), "whole-environment clean retained identities or configuration"
        assert file_manifest(env.config_path / "data") == ckb_before
        assert file_manifest(env.managed_ckb.parent.parent) == tools_before
