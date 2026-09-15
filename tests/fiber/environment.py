from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.harness import (
    DEVNET_PORTS, Account, CommandResult, DevnetManager, OffckbRunner, RpcClient,
    _command_references_path, _path_aliases, _process_alive, _process_command,
    _process_group_commands, is_port_open, rpc_script, wait_until,
)


FIBER_MODES = (
    "combined-foreground", "combined-daemon", "attached-foreground", "attached-daemon",
)
FNN_PORTS = (21714, 21715, 8344, 8345)
ALL_PORTS = (*DEVNET_PORTS, *FNN_PORTS)


def prepare_managed_tools(
    root: Path, env: dict[str, str], defaults: dict[str, Any], ckb_bin: Path, fnn_bin: Path,
) -> tuple[Path, Path, Path]:
    """Put real tools at packaged default paths; let OffCKB create all settings."""
    probe_home = Path(defaults["probeHome"])
    home = Path(env["HOME"])
    bins_root = home / Path(defaults["bins"]["rootFolder"]).relative_to(probe_home)
    config_path = home / Path(defaults["devnet"]["configPath"]).relative_to(probe_home)
    ckb = bins_root / defaults["bins"]["defaultCKBVersion"] / "ckb"
    fnn = bins_root / "fnn" / defaults["bins"]["defaultFnnVersion"] / "fnn"
    for source, destination in ((ckb_bin, ckb), (fnn_bin, fnn)):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.symlink_to(source)
    shutil.copytree(fnn_bin.parent / "config", fnn.parent / "config")
    assert not config_path.exists(), "OffCKB must create its own devnet and FNN configuration"
    assert not list(home.rglob("settings.json")), "first Fiber launch must use package defaults"
    assert "OFFCKB_CLI_PATH" not in env
    return ckb, fnn, config_path


@dataclass
class FiberEnvironment:
    runner: OffckbRunner
    rpc: RpcClient
    proxy_rpc: RpcClient
    fnn: tuple[RpcClient, RpcClient]
    accounts: list[Account]
    timeout_s: float
    root: Path
    config_path: Path
    mode: str
    managed_ckb: Path
    managed_fnn: Path
    startup_timeout_s: float
    ckb_version: str
    fnn_version: str
    start_result: CommandResult | None = None
    foreground: subprocess.Popen | None = None
    startup_snapshot: dict[str, Any] = field(default_factory=dict)
    attached_snapshot: dict[str, Any] | None = None
    _ckb_manager: DevnetManager | None = field(default=None, repr=False)
    _groups: set[int] = field(default_factory=set, repr=False)
    _known_pids: set[int] = field(default_factory=set, repr=False)

    @property
    def foreground_stdout(self) -> Path:
        return self.root / "commands" / "fiber-foreground.stdout.log"

    @property
    def foreground_stderr(self) -> Path:
        return self.root / "commands" / "fiber-foreground.stderr.log"

    @property
    def node_logs(self) -> tuple[Path, Path]:
        return tuple(self.config_path / "fiber" / "nodes" / str(i) / "fnn.log" for i in (1, 2))

    @property
    def runtime_path(self) -> Path:
        return self.config_path / "fiber" / "runtime.json"

    @property
    def pid_files(self) -> tuple[Path, Path]:
        return (
            self.config_path / "data" / "logs" / "daemon.pid",
            self.config_path / "fiber" / "logs" / "daemon.pid",
        )

    def start(self) -> None:
        assert self.mode in FIBER_MODES
        occupied = [port for port in ALL_PORTS if is_port_open(port)]
        assert not occupied, f"Fiber test ports already occupied; refusing to stop their owners: {occupied}"
        for account in self.accounts:
            self.runner.register_secret(account.private_key)
        if self.mode.startswith("attached"):
            self._start_existing_chain()
        args = ("node", "--fiber") if self.mode.startswith("combined") else ("fiber", "start")
        if self.mode.endswith("daemon"):
            self.start_result = self.runner.run(*args, "--daemon", timeout_s=self.startup_timeout_s)
            # No readiness retry here: a premature successful command is a failure.
            try:
                self.startup_snapshot = self._public_snapshot()
                self.assert_ready_snapshot(self.startup_snapshot)
            except Exception as error:
                raise AssertionError(f"{self.mode} returned before the complete environment was ready") from error
        else:
            with self.foreground_stdout.open("w") as stdout, self.foreground_stderr.open("w") as stderr:
                self.foreground = subprocess.Popen(
                    (*self.runner.command, *args), cwd=self.runner.cwd, env=self.runner.env,
                    stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, start_new_session=True,
                )
            self._groups.add(self.foreground.pid)
            self._known_pids.add(self.foreground.pid)

            def ready() -> bool:
                assert self.foreground is not None and self.foreground.poll() is None, (
                    f"{self.mode} exited during startup; inspect {self.foreground_stdout} and {self.foreground_stderr}"
                )
                snapshot = self._public_snapshot()
                if not self._snapshot_ready(snapshot):
                    return False
                output = self.foreground_stdout.read_text() + self.foreground_stderr.read_text()
                if not all(client.url in output for client in self.fnn):
                    return False
                self.startup_snapshot = snapshot
                return True

            wait_until(ready, timeout_s=self.startup_timeout_s, description=f"{self.mode} readiness and addresses")
            self.assert_ready_snapshot(self.startup_snapshot)
        self._remember_processes()
        self.startup_snapshot["commands"] = self.process_commands()
        self._register_fnn_secrets()
        (self.root / "startup.json").write_text(json.dumps(self.startup_snapshot, indent=2) + "\n")

    def _start_existing_chain(self) -> None:
        self._ckb_manager = DevnetManager(
            self.runner, self.rpc, self.proxy_rpc, self.managed_ckb,
            startup_timeout_s=self.startup_timeout_s,
        )
        self._ckb_manager.start(use_managed_binary=True)
        self._remember_processes()
        self.rpc.wait_indexer()
        sender, receiver = self.accounts[0], self.accounts[1]
        key = self.root / "secrets" / "existing-chain.key"
        key.write_text(sender.private_key + "\n")
        key.chmod(0o600)
        result = self.runner.run(
            "transfer", receiver.address, "100", "--privkey-file", key, timeout_s=self.timeout_s,
        )
        assert result.json is not None
        tx_hash = result.json["txHash"]
        transaction = self.rpc.wait_transaction(tx_hash)
        self.attached_snapshot = {
            "genesis": self.rpc.call("get_block_hash", ["0x0"]),
            "tx_hash": tx_hash,
            "transaction": transaction,
            "tip": self.rpc.tip(),
            "commands": self.process_commands(),
            "ckb_manager_pid": self._ckb_manager.pid,
        }
        (self.root / "attached-before-fiber.json").write_text(json.dumps(self.attached_snapshot, indent=2) + "\n")

    def _public_snapshot(self) -> dict[str, Any]:
        return {
            "genesis": self.rpc.call("get_block_hash", ["0x0"]),
            "proxy_genesis": self.proxy_rpc.call("get_block_hash", ["0x0"]),
            "ckb_version": self.rpc.call("local_node_info")["version"],
            "tip": self.rpc.tip(),
            "proxy_tip": self.proxy_rpc.tip(),
            "node_infos": [client.call("node_info") for client in self.fnn],
            "peers": [client.call("list_peers")["peers"] for client in self.fnn],
        }

    def _snapshot_ready(self, snapshot: dict[str, Any]) -> bool:
        infos = snapshot["node_infos"]
        if not all(isinstance(info, dict) and info.get("pubkey") for info in infos):
            return False
        return all(
            any(peer.get("pubkey") == infos[1 - i]["pubkey"] for peer in snapshot["peers"][i])
            for i in range(2)
        )

    def assert_ready_snapshot(self, snapshot: dict[str, Any]) -> None:
        assert snapshot["genesis"] == snapshot["proxy_genesis"]
        assert re.match(rf"{re.escape(self.ckb_version)}(?:\s|$)", snapshot["ckb_version"])
        infos = snapshot["node_infos"]
        assert infos[0]["pubkey"] != infos[1]["pubkey"], "FNNs must have different network identities"
        for i, info in enumerate(infos):
            assert info["version"] == self.fnn_version
            assert info["chain_hash"] == snapshot["genesis"]
            assert rpc_script(info["default_funding_lock_script"]) == rpc_script(self.accounts[i + 3].lock_script)
        assert self._snapshot_ready(snapshot), "OffCKB did not automatically connect the expected peers"

    def _valid_member(self, pid: int, command: str, pgid: int) -> bool:
        if pid == pgid and _command_references_path(command, self.runner.cli_entry):
            return True
        if _command_references_path(command, self.managed_ckb) and _command_references_path(command, self.config_path):
            return "run" in command.split() or "miner" in command.split()
        return _command_references_path(command, self.managed_fnn) and any(
            _command_references_path(command, self.config_path / "fiber" / "nodes" / str(i)) for i in (1, 2)
        )

    def _remember_processes(self) -> None:
        # Failed startup may remove PID metadata before an orphan child exits.
        # Discover only children naming this fresh fixture's exact data path.
        process_table = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,command="], capture_output=True, text=True,
            check=False, timeout=5,
        )
        assert process_table.returncode == 0, "Unable to inspect fixture process ownership"
        for line in process_table.stdout.splitlines():
            fields = line.strip().split(maxsplit=2)
            if len(fields) != 3:
                continue
            pid, pgid = int(fields[0]), int(fields[1])
            if pid != pgid and self._valid_member(pid, fields[2], pgid):
                self._groups.add(pgid)
                self._known_pids.add(pid)
        for pid_file in self.pid_files:
            if not pid_file.exists():
                continue
            try:
                data = json.loads(pid_file.read_text())
            except json.JSONDecodeError:
                continue  # A startup may still be writing metadata; tracked groups remain authoritative.
            pid = data.get("pid")
            script = data.get("scriptPath")
            assert isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            assert isinstance(script, str) and (_path_aliases(script) & _path_aliases(self.runner.cli_entry)), (
                f"Refusing unrelated PID metadata in {pid_file}"
            )
            if _process_alive(pid):
                command = _process_command(pid)
                assert command and _command_references_path(command, self.runner.cli_entry), (
                    f"Refusing unrelated manager PID {pid}"
                )
                assert os.getpgid(pid) == pid, f"manager PID {pid} does not own its session"
            self._groups.add(pid)
            self._known_pids.add(pid)
        for pgid in self._groups:
            members = _process_group_commands(pgid)
            assert all(self._valid_member(pid, command, pgid) for pid, command in members.items()), (
                f"Refusing process group {pgid} with members outside this Fiber environment"
            )
            self._known_pids.update(members)

    def process_commands(self) -> dict[int, str]:
        commands: dict[int, str] = {}
        for pgid in self._groups:
            commands.update(_process_group_commands(pgid))
        return commands

    def _register_fnn_secrets(self) -> None:
        for node in (1, 2):
            directory = self.config_path / "fiber" / "nodes" / str(node)
            for relative in ("password", "fiber/sk", "ckb/key"):
                path = directory / relative
                if path.is_file():
                    try:
                        self.runner.register_secret(path.read_text())
                    except UnicodeError:
                        pass

    def _stopped(self) -> bool:
        if self.foreground is not None:
            self.foreground.poll()
        return (
            all(not _process_alive(pid) for pid in self._known_pids)
            and not any(_process_group_commands(pgid) for pgid in self._groups)
            and not any(is_port_open(port) for port in ALL_PORTS)
        )

    def _fallback(self) -> None:
        self._remember_processes()
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pgid in self._groups:
                if self.foreground is not None:
                    self.foreground.poll()
                members = _process_group_commands(pgid)
                assert all(self._valid_member(pid, command, pgid) for pid, command in members.items()), (
                    f"Refusing fallback termination of unverified group {pgid}"
                )
                if members:
                    try:
                        os.killpg(pgid, sig)
                    except ProcessLookupError:
                        pass
            try:
                wait_until(self._stopped, timeout_s=10, description="owned Fiber processes to exit during fallback")
                return
            except AssertionError:
                if sig == signal.SIGKILL:
                    raise

    def close(self) -> None:
        """Observe the product's shutdown first; fallback cleanup never makes it pass."""
        failure: BaseException | None = None
        try:
            self._remember_processes()
            if self.foreground is not None and self.foreground.poll() is None:
                os.killpg(self.foreground.pid, signal.SIGINT)
                wait_until(
                    lambda: self.foreground.poll() is not None
                    and not _process_group_commands(self.foreground.pid),
                    timeout_s=20, description="foreground Fiber manager and children to stop",
                )
            elif self.mode.endswith("daemon") and self._groups:
                args = ("node", "stop") if self.mode.startswith("combined") else ("fiber", "stop")
                result = self.runner.run(*args, timeout_s=30)
                assert result.json and result.json.get("stopped") is True, "product did not confirm Fiber shutdown"
            if self._ckb_manager is not None:
                self._ckb_manager.close()
            wait_until(self._stopped, timeout_s=20, description="all owned Fiber/CKB processes and ports to stop")
            wait_until(
                lambda: not self.runtime_path.exists() and not any(path.exists() for path in self.pid_files),
                timeout_s=5, description="OffCKB to remove its Fiber runtime and PID metadata",
            )
        except BaseException as error:
            failure = error
            (self.root / "teardown-failure.txt").write_text(self.runner._redact_text(str(error)) + "\n")
            try:
                self._fallback()
            except BaseException as cleanup_error:
                error.add_note(f"owned-process fallback failed: {cleanup_error}")
        finally:
            # Do not remove a password/key while a surviving FNN could still need it.
            if not any(_process_group_commands(pgid) for pgid in self._groups):
                self.scrub_secrets()
        if failure is not None:
            raise failure

    def scrub_secrets(self) -> None:
        self._register_fnn_secrets()
        for node in (1, 2):
            directory = self.config_path / "fiber" / "nodes" / str(node)
            for relative in ("password", "fiber/sk", "ckb/key"):
                (directory / relative).unlink(missing_ok=True)
            # Both the live RocksDB store and automatic backups persist keys.
            for name in ("store", "backups"):
                sensitive_directory = directory / "fiber" / name
                if sensitive_directory.exists():
                    shutil.rmtree(sensitive_directory)
        shutil.rmtree(self.root / "secrets", ignore_errors=True)
        for directory in (self.root / "commands", self.config_path):
            if directory.exists():
                for path in directory.rglob("*.log"):
                    try:
                        original = path.read_text()
                    except (OSError, UnicodeError):
                        continue
                    redacted = self.runner._redact_text(original)
                    if redacted != original:
                        path.write_text(redacted)
