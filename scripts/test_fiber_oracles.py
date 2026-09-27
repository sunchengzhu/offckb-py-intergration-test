"""Offline checks of the test oracle itself; these are not Fiber acceptance coverage."""

from __future__ import annotations

import copy
import selectors
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.harness import (  # noqa: E402
    CKB, Account, OffckbRunner, RpcClient, _command_references_path,
    _process_command, _process_group_commands,
)
from tests.fiber.environment import FiberEnvironment  # noqa: E402
from tests.fiber.flows import Channel, assert_close_amounts, unpack_outpoint  # noqa: E402
from tests.fiber import udt_flows  # noqa: E402


class ProcessInspectionTests(unittest.TestCase):
    def test_long_owned_command_is_not_truncated(self):
        marker = "offckb-owned-" + "x" * 400
        process = subprocess.Popen(
            [sys.executable, "-c", "import os,sys; print(os.getpid(), flush=True); sys.stdin.read()", marker],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True,
        )
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                self.assertTrue(selector.select(timeout=5), "owned child did not become ready")
            self.assertEqual(int(process.stdout.readline()), process.pid)
            self.assertIn(marker, _process_command(process.pid))
            self.assertIn(marker, _process_group_commands(process.pid)[process.pid])
        finally:
            process.communicate(timeout=5)


class UdtFundingOracleTests(unittest.TestCase):
    """Non-default funding must pass, while incorrect public observations fail."""

    def observe(self, *, remote=(10_000, 5_000), total=15_000, balances=(5_000, 0)):
        script = {"code_hash": "0x" + "aa" * 32, "hash_type": "type", "args": "0x" + "bb" * 32}
        point = {"tx_hash": "0x" + "11" * 32, "index": "0x0"}
        pair = tuple({
            "state": {"state_name": "ChannelReady"},
            "local_balance": hex(local), "remote_balance": hex(other),
            "funding_udt_type_script": script, "channel_outpoint": point,
        } for local, other in zip((5_000, 10_000), remote, strict=True))
        pending = {
            "channel_id": "temporary", "is_acceptor": True,
            "state": {"state_name": "NegotiatingFunding"},
            "funding_udt_type_script": script, "remote_balance": hex(5_000),
            "channel_outpoint": None,
        }
        replies = {
            "node_info": {"pubkey": "peer"},
            "open_channel": {"temporary_channel_id": "temporary"},
            "list_channels": {"channels": [pending]},
            "accept_channel": {"channel_id": "final"},
        }
        nodes = tuple(Mock() for _ in range(2))
        for node in nodes:
            node.call.side_effect = lambda method, *_: replies[method]
        transaction = {"tx_status": {"block_number": "0x1"}, "transaction": {
            "outputs": [{"type": script}],
            "outputs_data": ["0x" + total.to_bytes(16, "little").hex()],
        }}
        rpc = Mock()
        rpc.wait_transaction.return_value = transaction
        env = SimpleNamespace(fnn=nodes, accounts=list(range(5)), rpc=rpc, runner=Mock(), timeout_s=1)
        asset = SimpleNamespace(kind="sudt", script=script, configs=())
        with ExitStack() as stack:
            for name, replacement in {
                "list_channels": Mock(return_value=[]),
                "channel_pair": Mock(return_value=pair),
                "settled": Mock(return_value=True),
                "unpack_outpoint": Mock(return_value=point),
                "generated_configs": Mock(return_value=()),
                "assert_udt_balance": Mock(side_effect=balances),
                "wait_until": lambda predicate, **_: self.assertTrue(predicate()),
            }.items():
                stack.enter_context(patch.object(udt_flows, name, replacement))
            return udt_flows.open_udt_channel(env, asset, funded_units=(5_000, 10_000))

    def test_full_acceptor_funding_uses_requested_amounts(self):
        self.assertEqual(self.observe().principal, (5_000, 10_000))

    def test_stale_default_amounts_are_rejected(self):
        for wrong in ({"remote": (3_000, 5_000)}, {"total": 8_000}, {"balances": (5_000, 7_000)}):
            with self.subTest(wrong=wrong), self.assertRaises(AssertionError):
                self.observe(**wrong)

    def test_only_reported_funding_signatures_are_expected(self):
        classify = udt_flows.reported_funding_issue
        change = "Peer sent us an invalid funding tx"
        fee = "PoolRejectedTransactionByMinFeeRate: LowFeeRate"
        self.assertTrue(classify(change, (5_000, 3_000), None).endswith("/1667"))
        self.assertTrue(classify(fee, (5_000, 10_000), None).endswith("/1663"))
        for log, amounts, rate in (
            (change, (5_000, 10_000), None),
            (change, (5_000, 0), None),
            (fee, (5_000, 10_000), 2000),
            ("connection refused", (5_000, 3_000), None),
            ("ScriptNotFound " + change, (5_000, 3_000), None),
        ):
            with self.subTest(log=log, amounts=amounts, rate=rate):
                self.assertIsNone(classify(log, amounts, rate))


class _ConsumedFundingCell:
    """Only the CKB post-consumption observation used by the arithmetic oracle."""

    def get_live_cell(self, _tx_hash, _index):
        return {"status": "unknown"}


class FiberOracleTests(unittest.TestCase):
    def setUp(self):
        self.accounts = tuple(
            Account(
                index=index, address=f"test-only-{index}", private_key="", pubkey="",
                lock_script={"code_hash": "0x" + "aa" * 32, "hash_type": "type", "args": "0x" + f"{index:02x}" * 20},
            )
            for index in (3, 4)
        )
        self.point = {"tx_hash": "0x" + "11" * 32, "index": "0x0"}
        self.channel = Channel(
            channel_id="0x" + "22" * 32, temporary_id="0x" + "33" * 32,
            outpoint=self.point,
            funding_transaction={"transaction": {"outputs": [{"capacity": hex(10_099 * CKB)}]}},
            principal=(10_000 * CKB, 99 * CKB), initial=({}, {}), paid=100 * CKB,
        )
        self.closing = {"transaction": {
            "inputs": [{"previous_output": self.point}],
            # Receiver first: ownership must come from the lock, not position.
            "outputs": [
                {"capacity": hex(199 * CKB), "lock": self.accounts[1].lock_script},
                {"capacity": hex(9_900 * CKB - 1234), "lock": self.accounts[0].lock_script},
            ],
            "outputs_data": ["0x", "0x"],
        }}

    def test_reversed_outputs_include_full_principal_and_charge_initiator(self):
        self.assertEqual(
            assert_close_amounts(_ConsumedFundingCell(), self.channel, self.closing, self.accounts),
            (9_900 * CKB - 1234, 199 * CKB),
        )

    def test_reserved_capacity_cannot_be_given_to_the_other_party(self):
        closing = copy.deepcopy(self.closing)
        closing["transaction"]["outputs"][0]["capacity"] = hex(100 * CKB)
        closing["transaction"]["outputs"][1]["capacity"] = hex(9_999 * CKB - 1234)
        # Same total capacity and fee, but the receiver's 99 CKB reserve is missing.
        with self.assertRaisesRegex(AssertionError, "wrong close payout"):
            assert_close_amounts(_ConsumedFundingCell(), self.channel, closing, self.accounts)

    def test_receiver_cannot_pay_the_initiators_fee(self):
        closing = copy.deepcopy(self.closing)
        closing["transaction"]["outputs"][0]["capacity"] = hex(199 * CKB - 1234)
        closing["transaction"]["outputs"][1]["capacity"] = hex(9_900 * CKB)
        with self.assertRaisesRegex(AssertionError, "wrong close payout"):
            assert_close_amounts(_ConsumedFundingCell(), self.channel, closing, self.accounts)

    def test_closing_a_different_funding_cell_does_not_pass(self):
        closing = copy.deepcopy(self.closing)
        closing["transaction"]["inputs"][0]["previous_output"]["index"] = "0x1"
        with self.assertRaisesRegex(AssertionError, "funding cell"):
            assert_close_amounts(_ConsumedFundingCell(), self.channel, closing, self.accounts)

    def test_outpoint_index_is_little_endian(self):
        self.assertEqual(
            unpack_outpoint("0x" + "ab" * 32 + "04030201"),
            {"tx_hash": "0x" + "ab" * 32, "index": "0x1020304"},
        )


class InstalledCliOwnershipTests(unittest.TestCase):
    def test_pnpm_shim_spelling_identifies_only_the_same_installed_cli(self):
        with tempfile.TemporaryDirectory(prefix="fiber ownership ") as directory:
            root = Path(directory)
            actual = root / "node_modules/.pnpm/offckb/node_modules/@offckb/cli/build/index.js"
            actual.parent.mkdir(parents=True)
            actual.touch()
            entry = root / "node_modules/@offckb/cli"
            entry.parent.mkdir(parents=True)
            entry.symlink_to(actual.parents[1])
            shim_path = str(actual).replace("/node_modules/.pnpm/", "/node_modules/.bin/../.pnpm/", 1)
            command = f"node {shim_path} node --fiber"
            self.assertTrue(_command_references_path(command, entry / "build/index.js"))
            self.assertFalse(_command_references_path(command, root / "other/node_modules/@offckb/cli/build/index.js"))


class RetainedRuntimeSanitizationTests(unittest.TestCase):
    def test_stopped_runtime_removes_keys_stores_and_backups_but_keeps_public_logs(self):
        with tempfile.TemporaryDirectory(prefix="fiber-scrub-") as temporary:
            root = Path(temporary)
            config_path = root / "home" / "devnet"
            runner = OffckbRunner(
                (str(root / "unused-offckb"),), cli_entry=root / "unused-index.js",
                env={"HOME": str(root / "home")}, cwd=root, records_dir=root / "commands",
            )
            unused_rpc = RpcClient("http://unused.invalid")
            environment = FiberEnvironment(
                runner=runner, rpc=unused_rpc, proxy_rpc=unused_rpc, fnn=(unused_rpc, unused_rpc),
                accounts=[], timeout_s=1, root=root, config_path=config_path,
                mode="combined-foreground", managed_ckb=root / "unused-ckb", managed_fnn=root / "unused-fnn",
                startup_timeout_s=1, ckb_version="0.208.0", fnn_version="0.9.0",
            )
            # No processes are created: exercise the post-shutdown sanitizer only.
            secrets = root / "secrets"
            secrets.mkdir()
            (secrets / "issuer.key").write_text("fake-issuer-key")
            public_logs = []
            sensitive_paths = [secrets]
            secret_values = []
            for node in (1, 2):
                directory = config_path / "fiber" / "nodes" / str(node)
                password, key = f"fake-password-for-node-{node}", f"fake-key-for-node-{node}"
                secret_values.extend((password, key))
                for relative, contents in (
                    ("password", password.encode()), ("ckb/key", key.encode()), ("fiber/sk", b"\xff" * 32),
                    ("fiber/store/000001.sst", b"fake-channel-signing-keys"),
                    ("fiber/backups/first/db/000001.sst", b"fake-backed-up-signing-keys"),
                    ("fiber/backups/first/key", key.encode()), ("fiber/backups/first/sk", b"\xff" * 32),
                    ("fiber/backups/second/db/000002.sst", b"another-fake-backup"),
                ):
                    path = directory / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(contents)
                sensitive_paths.extend(directory / relative for relative in (
                    "password", "ckb/key", "fiber/sk", "fiber/store", "fiber/backups",
                ))
                log = directory / "fnn.log"
                log.write_text(f"ChannelReady node={node}\npassword={password}\nkey={key}\n")
                public_logs.append(log)
                (directory / "config.yml").write_text("services: [fiber, rpc, ckb]\n")
            command_log = root / "commands" / "startup.stderr.log"
            command_log.write_text("Startup diagnostic retained\n" + "\n".join(secret_values))
            public_logs.append(command_log)
            public_snapshot = root / "startup.json"
            public_snapshot.write_text('{"status": "startup-failed"}\n')

            environment.scrub_secrets()
            environment.scrub_secrets()  # A later cleanup pass remains harmless.

            self.assertTrue(all(not path.exists() for path in sensitive_paths))
            for log in public_logs:
                text = log.read_text()
                self.assertIn("<redacted-secret>", text)
                self.assertTrue(all(secret not in text for secret in secret_values))
            self.assertIn("ChannelReady node=1", public_logs[0].read_text())
            self.assertIn("Startup diagnostic retained", command_log.read_text())
            self.assertEqual(public_snapshot.read_text(), '{"status": "startup-failed"}\n')
            self.assertTrue(all(
                (config_path / "fiber" / "nodes" / str(node) / "config.yml").is_file() for node in (1, 2)
            ))


if __name__ == "__main__":
    unittest.main()
