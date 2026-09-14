"""Fault-injection checks for the test harness; these are not product case mappings."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.asset_assertions import assert_udt_balance
from tests.harness import Account, DevnetManager, OffckbRunner, RpcClient, rpc_script
from tests.test_contract_deployment import _assert_deployment_owner
from tests.test_node_recovery import _owned_processes
from tests.test_transaction_debugging import _assert_debug_context
from tests.test_udt_lifecycle import _assert_issue_transaction


class StopObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        runner = OffckbRunner(
            (str(root / "offckb"),), cli_entry=root / "index.js",
            env={"HOME": str(root)}, cwd=root, records_dir=root / "commands",
        )
        self.manager = DevnetManager(runner, Mock(), Mock(), root / "ckb")
        self.pid_file = root / "daemon.pid"
        self.pid_file.write_text(json.dumps({"pid": 123456, "scriptPath": str(runner.cli_entry)}))
        self.manager.pid = 123456
        self.manager.start_result = {"pidFile": str(self.pid_file)}
        runner.run = Mock(return_value=SimpleNamespace(json={"ok": True, "stopped": True}))

        def poll_once(predicate, **kwargs):
            if not predicate():
                raise AssertionError(kwargs["description"])

        for name, replacement in (
            ("_process_alive", lambda *_: False),
            ("_process_group_alive", lambda *_: False),
            ("is_port_open", lambda *_: False),
            ("wait_until", poll_once),
        ):
            patcher = patch(f"tests.harness.{name}", replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_success_with_stale_pid_is_a_failure_and_does_not_repair_it(self):
        with self.assertRaisesRegex(AssertionError, "OffCKB itself"):
            self.manager.stop()
        self.assertTrue(self.pid_file.exists())

    def test_fallback_cleanup_keeps_the_failure_visible(self):
        with self.assertRaisesRegex(AssertionError, "OffCKB itself"):
            self.manager.close()
        self.assertFalse(self.pid_file.exists())

    def test_product_removing_pid_passes(self):
        def product_stop(*args, **kwargs):
            self.pid_file.unlink()
            return SimpleNamespace(json={"ok": True, "stopped": True})

        self.manager.runner.run.side_effect = product_stop
        self.assertTrue(self.manager.stop()["stopped"])


class FailedStartupOwnershipTests(unittest.TestCase):
    def test_shared_binary_and_log_readers_do_not_belong_to_the_failed_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "case" / "workspace"
            workspace.mkdir(parents=True)
            real_binary = root / "shared-ckb"
            real_binary.touch()
            alias = workspace / "ckb"
            alias.symlink_to(real_binary)
            entry = root / "package" / "index.js"
            devnet = SimpleNamespace(
                runner=SimpleNamespace(cwd=workspace, cli_entry=entry), ckb_bin=alias,
            )
            commands = {
                101: f"node {entry} node --daemon --binary-path {alias}",
                102: f"{real_binary} run -C {workspace.parent}/home/devnet",
                103: f"{real_binary} miner -C {workspace.parent}/home/devnet",
                201: f"node {entry} node --daemon --binary-path {root}/other/ckb",
                202: f"{real_binary} run -C {root}/other/devnet",
                203: f"tail -f {workspace.parent}/commands/daemon.log",
            }
            snapshot = SimpleNamespace(stdout="\n".join(f"{pid} {cmd}" for pid, cmd in commands.items()))
            with patch("tests.test_node_recovery.subprocess.run", return_value=snapshot):
                self.assertEqual(_owned_processes(devnet), {pid: commands[pid] for pid in (101, 102, 103)})


class AssetObservationTests(unittest.TestCase):
    def setUp(self):
        self.args = "0x" + "ab" * 32 + "00000000"
        self.account = Account(7, "test-address", {"codeHash": "0x" + "11" * 32,
                              "hashType": "type", "args": "0x1234"}, "fixture", "fixture")
        self.rpc = RpcClient("http://unused.invalid")
        scripts = {kind: {"codeHash": "0x" + byte * 32, "hashType": "data2", "args": "0x"}
                   for kind, byte in (("sudt", "22"), ("xudt", "33"))}
        self.rpc.configure_udt_scripts(scripts)
        self.cells = []
        self.entries = []
        for kind, quantities in (("sudt", [13]), ("xudt", [4, 6])):
            args = self.args if kind == "xudt" else self.args[:-8]
            script = {**scripts[kind], "args": args}
            self.entries.append({**script, "kind": kind, "balance": str(sum(quantities))})
            for quantity in quantities:
                self.cells.append({"output": {"type": rpc_script(script)},
                                   "output_data": "0x" + quantity.to_bytes(16, "little").hex()})
        self.rpc.live_cells = Mock(return_value=self.cells)

    def runner(self, entries, *, ignore_filter=False):
        def balance(*args):
            selected = entries
            if "--udt-kind" in args and not ignore_filter:
                selected = [entry for entry in entries if entry["kind"] == "xudt" and entry["args"] == self.args]
            return SimpleNamespace(json={"ok": True, "command": "balance", "network": "devnet",
                                         "address": self.account.address, "udt": selected})

        return SimpleNamespace(run=balance)

    def test_cli_aggregate_accepts_multiple_live_cells_for_one_asset(self):
        self.assertEqual(assert_udt_balance(self.runner(self.entries), self.rpc, self.account,
                                            "xudt", self.args), 10)

    def test_issuance_accepts_merged_inputs_and_split_outputs_but_checks_net_amount(self):
        lock = rpc_script(self.account.lock_script)
        script = self.cells[-1]["output"]["type"]
        origin = {"transaction": {"outputs": [{"lock": lock, "type": script}],
                                  "outputs_data": ["0x" + (5).to_bytes(16, "little").hex()]}}
        self.rpc.call = Mock(return_value=origin)
        transaction = {"transaction": {
            "inputs": [{"previous_output": {"tx_hash": "0x" + "44" * 32, "index": "0x0"}}],
            "outputs": [{"lock": lock, "type": script}, {"lock": lock, "type": script}],
            "outputs_data": ["0x" + value.to_bytes(16, "little").hex() for value in (7, 8)],
        }}
        _assert_issue_transaction(self.rpc, transaction, receiver_lock=lock, type_args=self.args, amount=10)
        with self.assertRaises(AssertionError):
            _assert_issue_transaction(self.rpc, transaction, receiver_lock=lock, type_args=self.args, amount=11)

    def test_cli_discovery_errors_are_detected_even_when_chain_is_correct(self):
        for defect in ("omitted", "wrong-kind", "truncated-args", "stale-balance", "duplicate"):
            with self.subTest(defect=defect):
                entries = copy.deepcopy(self.entries)
                if defect == "omitted":
                    entries.pop()
                elif defect == "wrong-kind":
                    entries[-1]["kind"] = "sudt"
                elif defect == "truncated-args":
                    entries[-1]["args"] = self.args[:-8]
                elif defect == "stale-balance":
                    entries[-1]["balance"] = "9"
                else:
                    entries.append(copy.deepcopy(entries[-1]))
                with self.assertRaises(AssertionError):
                    assert_udt_balance(self.runner(entries), self.rpc, self.account, "xudt", self.args)

    def test_ignored_filter_is_detected(self):
        with self.assertRaises(AssertionError):
            assert_udt_balance(self.runner(self.entries, ignore_filter=True), self.rpc,
                               self.account, "xudt", self.args)

    def test_deployment_record_and_chain_agreeing_on_wrong_owner_is_detected(self):
        wrong_lock = {**rpc_script(self.account.lock_script), "args": "0x5678"}
        with self.assertRaises(AssertionError):
            _assert_deployment_owner({"lock": wrong_lock}, {"output": {"lock": wrong_lock}},
                                     self.account.lock_script)


class DebugContextObservationTests(unittest.TestCase):
    def setUp(self):
        points = [{"tx_hash": "0x" + byte * 32, "index": "0x0"} for byte in ("11", "22", "33", "44")]
        owner, group, first_code, second_code = points
        group_data = "0x" + (
            (2).to_bytes(4, "little")
            + bytes.fromhex(first_code["tx_hash"][2:]) + (0).to_bytes(4, "little")
            + bytes.fromhex(second_code["tx_hash"][2:]) + (0).to_bytes(4, "little")
        ).hex()
        lock = {"code_hash": "0x" + "aa" * 32, "hash_type": "type", "args": "0x1234"}
        output = {"capacity": "0x174876e800", "lock": lock, "type": None}
        self.chain = {
            point["tx_hash"]: {
                "tx_status": {"status": "committed"},
                "transaction": {"outputs": [copy.deepcopy(output)], "outputs_data": [data]},
            }
            for point, data in zip(points, ("0x", group_data, "0x1234", "0x5678"))
        }
        transaction = {
            "inputs": [{"previous_output": owner, "since": "0x0"}],
            "cell_deps": [{"out_point": group, "dep_type": "dep_group"}],
            "outputs": [copy.deepcopy(output)],
            "outputs_data": ["0x"],
        }
        self.call = SimpleNamespace(transaction=copy.deepcopy(transaction))
        self.dump = {
            "tx": copy.deepcopy(transaction),
            "mock_info": {
                "inputs": [{"input": copy.deepcopy(transaction["inputs"][0]),
                            "output": copy.deepcopy(output), "data": "0x"}],
                "cell_deps": [
                    {"cell_dep": {"out_point": copy.deepcopy(point), "dep_type": dep_type},
                     "output": copy.deepcopy(output), "data": data}
                    for point, dep_type, data in (
                        (group, "dep_group", group_data),
                        (first_code, "code", "0x1234"),
                        (second_code, "code", "0x5678"),
                    )
                ],
            },
        }

        def read_transaction(method, params):
            self.assertEqual(method, "get_transaction")
            return copy.deepcopy(self.chain[params[0]])

        self.rpc = Mock(spec=RpcClient)
        self.rpc.call.side_effect = read_transaction

    def test_complete_context_matches_the_real_inputs_and_resolved_dependency_group(self):
        _assert_debug_context(self.call, self.rpc, self.dump)

    def test_missing_dependency_group_member_is_detected(self):
        self.dump["mock_info"]["cell_deps"].pop()
        with self.assertRaisesRegex(AssertionError, "dependency-group member"):
            _assert_debug_context(self.call, self.rpc, self.dump)

    def test_wrong_input_or_dependency_content_is_detected(self):
        for kind, index in (("inputs", 0), ("cell_deps", 1)):
            for field in ("output", "data"):
                with self.subTest(kind=kind, field=field):
                    dump = copy.deepcopy(self.dump)
                    item = dump["mock_info"][kind][index]
                    if field == "data":
                        item["data"] = "0xbad0"
                    else:
                        item["output"]["lock"]["args"] = "0x5678"
                    with self.assertRaises(AssertionError):
                        _assert_debug_context(self.call, self.rpc, dump)


if __name__ == "__main__":
    unittest.main()
