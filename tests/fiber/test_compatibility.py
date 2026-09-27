"""Upgrade an actual OffCKB 0.4.13 devnet in place."""
from __future__ import annotations

import pytest

from tests.harness import _process_group_commands, rpc_script, hex_int, transaction_hash_from, wait_until
from .flows import _key_file, opened_ckb_channel, pay_invoice
from .support_configuration import assert_failure
from .support_compatibility import (
    CUSTOM_GENESIS_MESSAGE, assert_legacy_chain, assert_missing_contracts,
    assert_no_components, assert_no_fnn, assert_old_transaction_absent, chain_data, configs,
    install_legacy_cli, prepare_legacy_state, upgraded_ckb,
)

pytestmark = pytest.mark.fiber


@pytest.fixture(scope="module")
def legacy_cli(pytestconfig, run_root):
    return install_legacy_cli(pytestconfig, run_root)


# TEST-MAP: FIB-COMP-01
def test_upgrade_preserves_existing_plain_ckb_development(fiber_environment, legacy_cli):
    with fiber_environment("combined-daemon", start=False) as env:
        old = prepare_legacy_state(env, legacy_cli)
        with upgraded_ckb(env):
            assert_legacy_chain(env, old)
            assert_no_fnn(env)
            tip = env.rpc.tip()
            wait_until(lambda: env.rpc.tip() > tip, timeout_s=env.timeout_s, description="upgraded CKB to keep mining")
            env.rpc.wait_indexer()
            receiver = env.accounts[2]
            before = env.rpc.ckb_balance(receiver.lock_script)
            result = env.runner.run(
                "transfer", receiver.address, "100", "--network", "devnet",
                "--privkey-file", _key_file(env, env.accounts[0]),
            )
            transaction = env.rpc.wait_transaction(transaction_hash_from(result))
            env.rpc.wait_indexer(transaction["tx_status"]["block_number"])
            assert env.rpc.ckb_balance(receiver.lock_script) == before + 100 * 100_000_000
            assert any(rpc_script(o["lock"]) == rpc_script(receiver.lock_script)
                       and hex_int(o["capacity"]) == 100 * 100_000_000
                       for o in transaction["transaction"]["outputs"])
            assert_legacy_chain(env, old)
            assert_no_fnn(env)


# TEST-MAP: FIB-COMP-02
@pytest.mark.parametrize("running", [True, False], ids=["attach-to-running-old-chain", "combined-old-chain"])
def test_fiber_rejects_old_genesis_with_rebuild_guidance(fiber_environment, legacy_cli, running):
    with fiber_environment("combined-daemon", start=False) as env:
        old = prepare_legacy_state(env, legacy_cli)
        if running:
            with upgraded_ckb(env) as manager:
                original = _process_group_commands(manager.pgid)
                assert original
                result = assert_failure(env, ("fiber", "start"))
                assert_missing_contracts(result)
                assert_no_fnn(env)
                assert _process_group_commands(manager.pgid) == original, "failed attach replaced an existing CKB process"
                assert_legacy_chain(env, old)
                tip = env.rpc.tip()
                wait_until(lambda: env.rpc.tip() > tip, timeout_s=env.timeout_s, description="original CKB to remain usable")
        else:
            result = assert_failure(env, ("node", "--fiber"))
            assert_missing_contracts(result)
            assert_no_components(env)
            assert configs(env) == old.configs
            # Starting/stopping a real database can rewrite its journal. The
            # preserved committed transaction proves user data was not reset.
            with upgraded_ckb(env):
                assert_legacy_chain(env, old)
                assert_no_fnn(env)


# TEST-MAP: FIB-COMP-03
def test_data_only_clean_does_not_upgrade_old_contracts(fiber_environment, legacy_cli):
    with fiber_environment("combined-daemon", start=False) as env:
        old = prepare_legacy_state(env, legacy_cli)
        assert chain_data(env) == old.database
        env.runner.run("clean", "-d")
        assert configs(env) == old.configs
        assert not (env.config_path / "data").exists()
        result = assert_failure(env, ("node", "--fiber"))
        assert_missing_contracts(result)
        assert_no_components(env)
        assert configs(env) == old.configs
        with upgraded_ckb(env):
            assert env.rpc.call("get_block_hash", ["0x0"]) == old.genesis
            assert_old_transaction_absent(env, old)
            assert_no_fnn(env)


# TEST-MAP: FIB-COMP-04
def test_full_rebuild_from_old_devnet_supports_channel_payment(fiber_environment, legacy_cli):
    with fiber_environment("combined-daemon", start=False) as env:
        old = prepare_legacy_state(env, legacy_cli)
        env.runner.run("clean")
        assert not env.config_path.exists()
        env.start()
        assert env.rpc.call("get_block_hash", ["0x0"]) != old.genesis
        assert_old_transaction_absent(env, old)
        rebuilt = configs(env)
        assert CUSTOM_GENESIS_MESSAGE.encode() not in rebuilt["specs/dev.toml"]
        assert rebuilt["specs/dev.toml"] != old.configs["specs/dev.toml"]
        assert rebuilt["ckb.toml"] != old.configs["ckb.toml"]
        assert rebuilt["ckb-miner.toml"] != old.configs["ckb-miner.toml"]
        env.assert_ready_snapshot(env.startup_snapshot)
        with opened_ckb_channel(env) as channel:
            pay_invoice(env, channel)
