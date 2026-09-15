"""Offline checks of the test oracle itself; these are not Fiber acceptance coverage."""

from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.harness import CKB, Account, OffckbRunner, RpcClient, _command_references_path  # noqa: E402
from tests.fiber.environment import FiberEnvironment  # noqa: E402
from tests.fiber.flows import Channel, assert_close_amounts, unpack_outpoint  # noqa: E402


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
