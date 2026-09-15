from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import shutil
import socket
import struct
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


DIRECT_RPC_URL = "http://127.0.0.1:8114"
PROXY_RPC_URL = "http://127.0.0.1:28114"
DEVNET_PORTS = (8114, 28114, 18114, 8115)
CKB = 100_000_000


def read_ckb_version(binary: Path) -> str:
    result = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, timeout=10, check=False,
    )
    version = re.search(r"\b(\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?)\b", result.stdout)
    assert result.returncode == 0 and version is not None, (
        f"cannot determine CKB version from {binary}: {result.stdout}{result.stderr}"
    )
    return version.group(1)


def hex_int(value: str | int) -> int:
    return value if isinstance(value, int) else int(value, 16)


def rpc_script(script: Mapping[str, Any]) -> dict[str, str]:
    """Convert public CLI/CCC script keys to the CKB JSON-RPC shape."""
    return {
        "code_hash": str(script.get("code_hash", script.get("codeHash"))),
        "hash_type": str(script.get("hash_type", script.get("hashType"))),
        "args": str(script["args"]),
    }


def camel_script(script: Mapping[str, Any]) -> dict[str, str]:
    return {
        "codeHash": str(script.get("codeHash", script.get("code_hash"))),
        "hashType": str(script.get("hashType", script.get("hash_type"))),
        "args": str(script["args"]),
    }


def dep_group_members(data: str) -> list[dict[str, str]]:
    encoded = bytes.fromhex(data.removeprefix("0x"))
    assert len(encoded) >= 4, "dep group is missing its OutPointVec count"
    count = struct.unpack_from("<I", encoded)[0]
    assert count > 0 and len(encoded) == 4 + count * 36, "invalid dep group OutPointVec"
    return [
        {
            "tx_hash": "0x" + encoded[offset:offset + 32].hex(),
            "index": hex(struct.unpack_from("<I", encoded, offset + 32)[0]),
        }
        for offset in range(4, len(encoded), 36)
    ]


def is_port_open(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((host, port)) == 0


def wait_until(predicate, *, timeout_s: float, interval_s: float = 0.25, description: str) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except AssertionError:
            raise
        except Exception as error:  # keep the final diagnostic from a transient polling probe
            last_error = error
        time.sleep(interval_s)
    detail = f"; last error: {last_error}" if last_error else ""
    raise AssertionError(f"Timed out waiting for {description} after {timeout_s:.1f}s{detail}")


@dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    json: dict[str, Any] | None


@dataclass(frozen=True)
class Account:
    index: int
    address: str
    lock_script: dict[str, str]
    private_key: str = field(repr=False)
    pubkey: str


class OffckbRunner:
    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        records_dir: Path,
        default_timeout_s: float = 180.0,
        cli_entry: Path | None = None,
    ) -> None:
        self.command = tuple(command)
        # Process ownership only: never override how the packaged CLI starts itself.
        self.cli_entry = cli_entry or Path(self.command[-1])
        self.env = dict(env)
        self.cwd = cwd
        self.records_dir = records_dir
        self.default_timeout_s = default_timeout_s
        self._sequence = 0
        self._secrets: set[str] = set()
        self.records_dir.mkdir(parents=True, exist_ok=True)
        inherited_secret = self.env.get("OFFCKB_PRIVATE_KEY")
        if inherited_secret:
            self.register_secret(inherited_secret)

    def register_secret(self, value: str) -> None:
        secret = value.strip()
        if not secret:
            return
        self._secrets.add(secret)
        if secret.lower().startswith("0x") and len(secret) > 2:
            self._secrets.add(secret[2:])

    def run(
        self,
        *args: object,
        check: bool = True,
        cwd: Path | None = None,
        timeout_s: float | None = None,
        record: bool = True,
        sensitive_output: bool = False,
        json_mode: bool = True,
    ) -> CommandResult:
        argv = (*self.command, *(("--json",) if json_mode else ()), *(str(arg) for arg in args))
        command_cwd = Path(cwd or self.cwd)
        self._register_argv_secrets(argv, command_cwd)
        started = time.monotonic()
        process = subprocess.Popen(
            argv,
            cwd=command_cwd,
            env=self.env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        try:
            stdout, stderr = process.communicate(
                timeout=self.default_timeout_s if timeout_s is None else timeout_s
            )
        except subprocess.TimeoutExpired:
            stdout, stderr = _stop_command_group(process)
            duration_s = time.monotonic() - started
            if record:
                self._record(
                    argv,
                    command_cwd,
                    None,
                    stdout,
                    stderr,
                    duration_s,
                    timed_out=True,
                    sensitive_output=sensitive_output,
                )
            diagnostic_stdout = self._diagnostic_text(stdout, sensitive=sensitive_output)
            diagnostic_stderr = self._diagnostic_text(stderr, sensitive=sensitive_output)
            raise AssertionError(
                f"offckb command timed out after {duration_s:.1f}s: {_display_argv(argv)}\n"
                f"stdout:\n{diagnostic_stdout}\nstderr:\n{diagnostic_stderr}"
            ) from None
        except BaseException:
            _stop_command_group(process)
            raise

        if process.returncode != 0:
            # A failed package script may leave a child running with redirected
            # output. Only clean the group created for this invocation; detached
            # OffCKB daemons have their own group and remain owned by their fixture.
            _stop_command_group(process)
        completed = subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)

        duration_s = time.monotonic() - started
        parsed = _parse_single_result(completed.stdout)
        result = CommandResult(
            argv=tuple(argv),
            cwd=command_cwd,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_s=duration_s,
            json=parsed,
        )
        if record:
            self._record(
                argv,
                command_cwd,
                completed.returncode,
                completed.stdout,
                completed.stderr,
                duration_s,
                timed_out=False,
                sensitive_output=sensitive_output,
            )
        if check and completed.returncode != 0:
            diagnostic_stdout = self._diagnostic_text(completed.stdout, sensitive=sensitive_output)
            diagnostic_stderr = self._diagnostic_text(completed.stderr, sensitive=sensitive_output)
            raise AssertionError(
                f"offckb command failed ({completed.returncode}): {_display_argv(argv)}\n"
                f"stdout:\n{diagnostic_stdout}\nstderr:\n{diagnostic_stderr}"
            )
        if check and json_mode and parsed is None:
            diagnostic_stdout = self._diagnostic_text(completed.stdout, sensitive=sensitive_output)
            diagnostic_stderr = self._diagnostic_text(completed.stderr, sensitive=sensitive_output)
            raise AssertionError(
                f"offckb command succeeded without one JSON result: {_display_argv(argv)}\n"
                f"stdout:\n{diagnostic_stdout}\nstderr:\n{diagnostic_stderr}"
            )
        if check and json_mode and parsed.get("ok") is not True:
            diagnostic = self._diagnostic_text(repr(parsed), sensitive=sensitive_output)
            raise AssertionError(f"offckb JSON result did not report success: {diagnostic}")
        return result

    def _register_argv_secrets(self, argv: Sequence[str], cwd: Path) -> None:
        index = 0
        while index < len(argv):
            arg = str(argv[index])
            if arg == "--privkey" and index + 1 < len(argv):
                self.register_secret(str(argv[index + 1]))
                index += 2
                continue
            if arg.startswith("--privkey="):
                self.register_secret(arg.split("=", 1)[1])
            elif arg == "--privkey-file" and index + 1 < len(argv):
                self._register_secret_file(str(argv[index + 1]), cwd)
                index += 2
                continue
            elif arg.startswith("--privkey-file="):
                self._register_secret_file(arg.split("=", 1)[1], cwd)
            index += 1

    def _register_secret_file(self, value: str, cwd: Path) -> None:
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = cwd / path
        try:
            self.register_secret(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            # The CLI owns validation and the user-facing error for an unreadable
            # key file. There is no secret value to redact when it cannot be read.
            return

    def _redact_text(self, value: str) -> str:
        redacted = value
        for secret in sorted(self._secrets, key=len, reverse=True):
            redacted = redacted.replace(secret, "<redacted-secret>")
        return redacted

    def _diagnostic_text(self, value: str, *, sensitive: bool) -> str:
        if sensitive:
            return "<suppressed sensitive output>"
        return self._redact_text(value)

    def _record(
        self,
        argv: Sequence[str],
        cwd: Path,
        returncode: int | None,
        stdout: str,
        stderr: str,
        duration_s: float,
        *,
        timed_out: bool,
        sensitive_output: bool,
    ) -> None:
        self._sequence += 1
        stem = f"{self._sequence:03d}"
        metadata = {
            "argv": _redact_argv(argv),
            "cwd": str(cwd),
            "returncode": returncode,
            "durationSeconds": round(duration_s, 3),
            "timedOut": timed_out,
        }
        (self.records_dir / f"{stem}.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        recorded_stdout = self._diagnostic_text(stdout, sensitive=sensitive_output)
        recorded_stderr = self._diagnostic_text(stderr, sensitive=sensitive_output)
        (self.records_dir / f"{stem}.stdout.log").write_text(recorded_stdout, encoding="utf-8")
        (self.records_dir / f"{stem}.stderr.log").write_text(recorded_stderr, encoding="utf-8")


def _stop_command_group(process: subprocess.Popen[str]) -> tuple[str, str]:
    """Reap the command and stop its children without touching other sessions."""
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
        if process.poll() is None:
            process.kill()
        return process.communicate(timeout=5)

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    finally:
        # Even if the parent has exited and all pipes closed, a child can still
        # be alive. Signal the original group, not just the parent PID.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    return process.communicate(timeout=5)


def _parse_single_result(stdout: str) -> dict[str, Any] | None:
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    try:
        value = json.loads(lines[0])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _redact_argv(argv: Sequence[str]) -> list[str]:
    result: list[str] = []
    redact_next = False
    for arg in argv:
        if redact_next:
            result.append("<redacted>")
            redact_next = False
            continue
        if arg == "--privkey":
            result.append(arg)
            redact_next = True
        elif arg.startswith("--privkey="):
            result.append("--privkey=<redacted>")
        else:
            result.append(str(arg))
    return result


def _display_argv(argv: Sequence[str]) -> str:
    return " ".join(_redact_argv(argv))


def read_cli_settings(runner: OffckbRunner) -> dict[str, Any]:
    """config list exposes effective settings in a JSON-mode stderr event."""
    result = runner.run("config", "list")
    candidates = []
    for line in result.stderr.splitlines():
        event = json.loads(line)
        try:
            settings = json.loads(event.get("message", ""))
        except json.JSONDecodeError:
            continue
        if isinstance(settings, dict) and "bins" in settings and "devnet" in settings:
            candidates.append(settings)
    assert len(candidates) == 1, "offckb config list did not expose one settings object"
    return candidates[0]


class RpcClient:
    def __init__(self, url: str = DIRECT_RPC_URL, *, timeout_s: float = 10.0, tx_timeout_s: float = 180.0):
        self.url = url
        self.timeout_s = timeout_s
        self.tx_timeout_s = tx_timeout_s
        self._request_id = 0
        self._udt_scripts: dict[str, dict[str, str]] = {}

    def configure_udt_scripts(self, scripts: Mapping[str, Mapping[str, str]]) -> None:
        self._udt_scripts = {kind: rpc_script(script) for kind, script in scripts.items()}

    def udt_script(self, kind: str) -> dict[str, str]:
        """The script resolved independently from the running chain's CKB config."""
        assert kind in self._udt_scripts, f"effective {kind} script is not loaded"
        return dict(self._udt_scripts[kind])

    def call(self, method: str, params: Sequence[Any] = ()) -> Any:
        self._request_id += 1
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": list(params)}
        ).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:
                body = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise RuntimeError(f"RPC {method} failed at {self.url}: {error}") from error
        decoded = json.loads(body)
        if decoded.get("error") is not None:
            raise RuntimeError(f"RPC {method} returned {decoded['error']}")
        return decoded.get("result")

    def ready(self) -> bool:
        info = self.call("local_node_info")
        tip = self.call("get_tip_block_number")
        return isinstance(info, dict) and isinstance(tip, str)

    def tip(self) -> int:
        return hex_int(self.call("get_tip_block_number"))

    def indexer_tip(self) -> int:
        result = self.call("get_indexer_tip")
        if not isinstance(result, dict) or "block_number" not in result:
            raise RuntimeError(f"Unexpected get_indexer_tip result: {result!r}")
        return hex_int(result["block_number"])

    def wait_indexer(self, block_number: int | str | None = None, timeout_s: float = 90.0) -> int:
        target = self.tip() if block_number is None else hex_int(block_number)
        observed = -1

        def caught_up() -> bool:
            nonlocal observed
            observed = self.indexer_tip()
            return observed >= target

        wait_until(caught_up, timeout_s=timeout_s, interval_s=0.5, description=f"indexer tip >= {target}")
        return observed

    def wait_transaction(self, tx_hash: str, timeout_s: float | None = None) -> dict[str, Any]:
        last: Any = None

        def committed() -> bool:
            nonlocal last
            last = self.call("get_transaction", [tx_hash])
            if not last:
                return False
            status = last.get("tx_status", {}).get("status")
            if status == "rejected":
                reason = last.get("tx_status", {}).get("reason")
                raise AssertionError(f"Transaction {tx_hash} was rejected: {reason or last}")
            return status == "committed"

        wait_until(
            committed,
            timeout_s=timeout_s or self.tx_timeout_s,
            interval_s=1.0,
            description=f"transaction {tx_hash} to commit; last={last!r}",
        )
        assert isinstance(last, dict)
        status = last.get("tx_status", {})
        if status.get("block_number") is None and status.get("block_hash"):
            header = self.call("get_header", [status["block_hash"]])
            if isinstance(header, dict) and header.get("number") is not None:
                status["block_number"] = header["number"]
        return last

    def live_cells(self, lock_script: Mapping[str, Any]) -> list[dict[str, Any]]:
        search_key = {
            "script": rpc_script(lock_script),
            "script_type": "lock",
            "script_search_mode": "exact",
            "with_data": True,
        }
        return self._all_cells(search_key)

    def _all_cells(self, search_key: Mapping[str, Any]) -> list[dict[str, Any]]:
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_out_points: set[tuple[str, str]] = set()
        objects: list[dict[str, Any]] = []
        deadline = time.monotonic() + min(max(self.timeout_s * 2, 30.0), self.tx_timeout_s)
        for _page_number in range(10_000):
            if time.monotonic() >= deadline:
                raise AssertionError("get_cells pagination exceeded its overall deadline")
            params: list[Any] = [search_key, "asc", "0x64"]
            if cursor is not None:
                params.append(cursor)
            page = self.call("get_cells", params)
            if not isinstance(page, dict):
                raise AssertionError(f"Unexpected get_cells response: {page!r}")
            batch = page.get("objects")
            if not isinstance(batch, list) or any(not isinstance(cell, dict) for cell in batch):
                raise AssertionError(f"Unexpected get_cells objects: {batch!r}")
            if len(batch) > 100:
                raise AssertionError(f"get_cells returned {len(batch)} objects for a 100-object page")
            if not batch:
                break

            for cell in batch:
                out_point = cell.get("out_point")
                if not isinstance(out_point, dict):
                    raise AssertionError(f"get_cells object has no out_point: {cell!r}")
                tx_hash = out_point.get("tx_hash")
                index = out_point.get("index")
                if not isinstance(tx_hash, str) or not isinstance(index, str):
                    raise AssertionError(f"get_cells object has an invalid out_point: {out_point!r}")
                key = (tx_hash, index)
                if key in seen_out_points:
                    raise AssertionError(f"get_cells repeated an out_point across pages: {out_point!r}")
                seen_out_points.add(key)

            next_cursor = page.get("last_cursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                raise AssertionError("get_cells returned objects without a usable last_cursor")
            if next_cursor == cursor or next_cursor in seen_cursors:
                raise AssertionError(f"get_cells pagination cursor did not advance: {next_cursor!r}")

            objects.extend(batch)
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise AssertionError("get_cells pagination exceeded 10,000 pages")
        return objects

    def ckb_balance(self, lock_script: Mapping[str, Any]) -> int:
        return sum(hex_int(cell["output"]["capacity"]) for cell in self.live_cells(lock_script))

    def udt_balance(self, lock_script: Mapping[str, Any], kind: str, type_args: str) -> int:
        expected = self._udt_scripts.get(kind)
        if expected is None:
            raise AssertionError(f"UDT script metadata for {kind!r} was not loaded from the effective chain spec")
        total = 0
        for cell in self.live_cells(lock_script):
            type_script = cell.get("output", {}).get("type")
            if not type_script:
                continue
            actual = rpc_script(type_script)
            if actual != {**expected, "args": type_args}:
                continue
            output_data = cell.get("output_data", "0x")
            raw = bytes.fromhex(output_data.removeprefix("0x"))
            if len(raw) != 16:
                continue
            total += int.from_bytes(raw, byteorder="little", signed=False)
        return total

    def udt_balances(self, lock_script: Mapping[str, Any]) -> dict[tuple[str, str, str, str], int]:
        """Discover assets from lock-indexed live cells and the effective chain spec."""
        assert set(self._udt_scripts) == {"sudt", "xudt"}, "effective UDT scripts are not loaded"
        balances: dict[tuple[str, str, str, str], int] = {}
        for cell in self.live_cells(lock_script):
            script = cell["output"].get("type")
            if script is None:
                continue
            for kind, expected in self._udt_scripts.items():
                if any(script[field] != expected[field] for field in ("code_hash", "hash_type")):
                    continue
                data = bytes.fromhex(cell["output_data"].removeprefix("0x"))
                assert len(data) >= 16, f"invalid UDT amount data: {cell['out_point']}"
                key = (kind, script["code_hash"], script["hash_type"], script["args"])
                balances[key] = balances.get(key, 0) + int.from_bytes(data[:16], "little")
        return balances

    def get_live_cell(self, tx_hash: str, index: int) -> dict[str, Any]:
        result = self.call("get_live_cell", [{"tx_hash": tx_hash, "index": hex(index)}, True])
        if not isinstance(result, dict):
            raise AssertionError(f"Unexpected get_live_cell result: {result!r}")
        return result

    @staticmethod
    def script_hash(script: Mapping[str, Any]) -> str:
        normalized = rpc_script(script)
        code_hash = bytes.fromhex(normalized["code_hash"].removeprefix("0x"))
        if len(code_hash) != 32:
            raise ValueError("script code hash must be 32 bytes")
        hash_type_values = {"data": 0, "type": 1, "data1": 2, "data2": 4}
        try:
            hash_type = bytes([hash_type_values[normalized["hash_type"]]])
        except KeyError as error:
            raise ValueError(f"unsupported script hash type: {normalized['hash_type']}") from error
        args = bytes.fromhex(normalized["args"].removeprefix("0x"))
        molecule_args = struct.pack("<I", len(args)) + args
        first_offset = 4 * 4
        offsets = (
            first_offset,
            first_offset + len(code_hash),
            first_offset + len(code_hash) + len(hash_type),
        )
        total_size = offsets[2] + len(molecule_args)
        molecule_script = struct.pack("<IIII", total_size, *offsets) + code_hash + hash_type + molecule_args
        digest = hashlib.blake2b(molecule_script, digest_size=32, person=b"ckb-default-hash").hexdigest()
        return "0x" + digest


def _configured_devnet_paths(env: Mapping[str, str]) -> tuple[Path, Path]:
    home = Path(env["HOME"])
    app_name = "offckb-nodejs"
    if sys.platform == "darwin":
        data_root = home / "Library" / "Application Support" / app_name
        settings_file = home / "Library" / "Preferences" / app_name / "settings.json"
    else:
        data_home = Path(env.get("XDG_DATA_HOME", str(home / ".local" / "share")))
        config_home = Path(env.get("XDG_CONFIG_HOME", str(home / ".config")))
        data_root = data_home / app_name
        settings_file = config_home / app_name / "settings.json"

    config_path = data_root / "devnet"
    data_path = config_path / "data"
    try:
        settings = json.loads(settings_file.read_text(encoding="utf-8"))
        devnet = settings.get("devnet") if isinstance(settings, dict) else None
        if isinstance(devnet, dict):
            configured_config = devnet.get("configPath")
            configured_data = devnet.get("dataPath")
            if isinstance(configured_config, str) and configured_config:
                config_path = Path(configured_config)
            if isinstance(configured_data, str) and configured_data:
                data_path = Path(configured_data)
            elif isinstance(configured_config, str) and configured_config:
                data_path = config_path / "data"
    except (OSError, json.JSONDecodeError):
        # The product also falls back to defaults for missing/malformed settings.
        pass
    return config_path, data_path


def _read_pid_metadata(pid_file: Path) -> dict[str, Any] | None:
    try:
        raw = pid_file.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimeError(f"Cannot read daemon PID metadata {pid_file}: {error}") from error
    if not raw:
        raise RuntimeError(f"Daemon PID metadata is empty: {pid_file}")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Daemon PID metadata is not valid JSON: {pid_file}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Daemon PID metadata must be an object: {pid_file}")
    pid = value.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RuntimeError(f"Daemon PID metadata has an invalid PID: {pid!r}")
    return value


def _process_group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _path_aliases(value: str | Path) -> set[str]:
    """Return exact absolute aliases for macOS's /var -> /private/var indirection."""
    path = Path(value)
    aliases = {str(path.absolute()), str(path.resolve())}
    if sys.platform == "darwin":
        for alias in tuple(aliases):
            if alias.startswith("/private/"):
                aliases.add(alias.removeprefix("/private"))
            elif alias.startswith(("/var/", "/tmp/")):
                aliases.add("/private" + alias)
    return aliases


def _command_references_path(command: str, value: str | Path) -> bool:
    # pnpm's executable shim invokes node via .bin/../.pnpm; compare that
    # spelling with the same installed file without splitting paths with spaces.
    command = command.replace("/node_modules/.bin/../", "/node_modules/")
    return any(alias in command for alias in _path_aliases(value))


def _process_group_commands(pgid: int) -> dict[int, str]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,command="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"Cannot inspect process group {pgid}: {completed.stderr.strip()}")
    members: dict[int, str] = {}
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            pid_value, pgid_value = int(fields[0]), int(fields[1])
        except ValueError:
            continue
        if pgid_value == pgid:
            members[pid_value] = fields[2]
    return members


class DevnetManager:
    def __init__(
        self,
        runner: OffckbRunner,
        rpc: RpcClient,
        proxy_rpc: RpcClient,
        ckb_bin: Path,
        *,
        startup_timeout_s: float = 120.0,
    ) -> None:
        self.runner = runner
        self.rpc = rpc
        self.proxy_rpc = proxy_rpc
        self.ckb_bin = ckb_bin
        self.startup_timeout_s = startup_timeout_s
        self.start_result: dict[str, Any] | None = None
        self.pid: int | None = None
        self.pgid: int | None = None

    @property
    def running(self) -> bool:
        return self.pid is not None and _process_alive(self.pid) and is_port_open(8114)

    @property
    def pid_file(self) -> Path:
        value = self.start_result.get("pidFile") if self.start_result else None
        if isinstance(value, str):
            return Path(value)
        _, data_path = _configured_devnet_paths(self.runner.env)
        return data_path / "logs" / "daemon.pid"

    @property
    def config_path(self) -> Path:
        config_path, _ = _configured_devnet_paths(self.runner.env)
        return config_path

    @property
    def _owned_cli_entry(self) -> str:
        value = self.runner.cli_entry
        # Keep the package-visible path as the primary identity. `_path_aliases`
        # also adds its resolved pnpm-store target, so process commands using
        # either side of pnpm's symlink layout are accepted.
        return str(Path(value).absolute())

    def _pid_metadata_owned_by_runner(self, metadata: Mapping[str, Any]) -> bool:
        script_path = metadata.get("scriptPath")
        if not isinstance(script_path, str) or not script_path:
            return False
        return bool(_path_aliases(script_path) & _path_aliases(self._owned_cli_entry))

    def _recover_owned_pid(self, *, retry_timeout_s: float = 0.0) -> int | None:
        deadline = time.monotonic() + retry_timeout_s
        last_error: RuntimeError | None = None
        while True:
            try:
                metadata = _read_pid_metadata(self.pid_file)
                last_error = None
            except RuntimeError as error:
                metadata = None
                last_error = error
            if metadata is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if metadata is None:
            if last_error is not None:
                raise last_error
            return None
        if not self._pid_metadata_owned_by_runner(metadata):
            raise RuntimeError(
                f"Refusing to claim daemon PID metadata for another CLI: {self.pid_file}: {metadata!r}"
            )
        pid = int(metadata["pid"])
        if _process_alive(pid):
            command = _process_command(pid)
            if not command or not _command_references_path(command, self._owned_cli_entry):
                raise RuntimeError(
                    f"Refusing to claim PID {pid}; command does not contain {self._owned_cli_entry!r}: {command!r}"
                )
        self.pid = pid
        self.pgid = self._verified_owned_process_group(pid)
        if self.pgid is None and not _process_alive(pid) and _process_group_alive(pid):
            if not self._orphan_group_is_owned(pid):
                raise RuntimeError(
                    f"Refusing to claim live process group {pid} after its recorded daemon PID exited"
                )
            self.pgid = pid
        return pid

    def _orphan_group_is_owned(self, pgid: int) -> bool:
        """Fail closed before signaling a group whose original leader has exited."""
        members = _process_group_commands(pgid)
        if not members:
            return False
        for command in members.values():
            if _command_references_path(command, self._owned_cli_entry):
                continue
            if _command_references_path(command, self.ckb_bin) and _command_references_path(
                command, self.config_path
            ):
                continue
            return False
        return True

    def _has_owned_state(self) -> bool:
        return (
            self.pid is not None
            or (self.pgid is not None and _process_group_alive(self.pgid))
            or self.pid_file.exists()
        )

    def ensure_running(self) -> "DevnetManager":
        if self.running:
            return self
        if self._has_owned_state():
            recovered = self.pid if self.pid is not None else self._recover_owned_pid()
            if recovered is not None and _process_alive(recovered):
                raise AssertionError(f"Owned offckb daemon PID {recovered} is alive but its RPC is not ready")
            if self.pgid is not None and _process_group_alive(self.pgid):
                raise AssertionError(f"Owned offckb process group {self.pgid} is alive but its daemon PID exited")
        self.start()
        return self

    def start(self, *, use_managed_binary: bool = False) -> dict[str, Any]:
        occupied = [port for port in DEVNET_PORTS if is_port_open(port)]
        if occupied:
            raise AssertionError(
                f"Cannot start isolated offckb devnet; ports already in use: {occupied}. "
                "Stop the owning service rather than letting the test runner kill it."
            )
        try:
            binary_args = () if use_managed_binary else ("--binary-path", self.ckb_bin)
            result = self.runner.run(
                "node",
                "--daemon",
                *binary_args,
                timeout_s=self.startup_timeout_s,
            )
        except BaseException as error:
            try:
                self._cleanup_failed_start()
            except BaseException as cleanup_error:
                error.add_note(f"failed-start cleanup also failed: {cleanup_error}")
            raise
        try:
            assert result.json is not None
            self.start_result = result.json
            self.pid = int(result.json["pid"])
            self.pgid = self._verified_owned_process_group(self.pid)
            self._assert_ready_when_daemon_command_returned()
            self._wait_service_ready()
            self._load_effective_udt_scripts()
        except BaseException as error:
            try:
                self.stop()
            except BaseException as cleanup_error:
                error.add_note(f"post-start cleanup also failed: {cleanup_error}")
            raise
        return result.json

    def _assert_ready_when_daemon_command_returned(self) -> None:
        """Probe once, without retries, so an early daemon-command return cannot be hidden."""
        if self.pid is None or not _process_alive(self.pid):
            raise AssertionError(f"node --daemon returned with a non-running daemon PID: {self.pid}")
        try:
            direct_ready = self.rpc.ready()
        except Exception as error:
            raise AssertionError("node --daemon returned before the direct CKB RPC was ready") from error
        if not direct_ready:
            raise AssertionError("node --daemon returned before the direct CKB RPC was ready")
        try:
            proxy_ready = self.proxy_rpc.ready()
        except Exception as error:
            raise AssertionError("node --daemon returned before the proxy CKB RPC was ready") from error
        if not proxy_ready:
            raise AssertionError("node --daemon returned before the proxy CKB RPC was ready")
        try:
            direct_genesis = self.rpc.call("get_block_hash", ["0x0"])
            proxy_genesis = self.proxy_rpc.call("get_block_hash", ["0x0"])
        except Exception as error:
            raise AssertionError("node --daemon returned before both RPCs could serve genesis") from error
        if direct_genesis != proxy_genesis:
            raise AssertionError(f"Direct/proxy genesis mismatch: {direct_genesis} != {proxy_genesis}")
        if self.pid is None or not _process_alive(self.pid):
            raise AssertionError(f"offckb daemon PID {self.pid} exited during the immediate readiness probe")

    def _cleanup_failed_start(self) -> None:
        owned_pid = self._recover_owned_pid(retry_timeout_s=2.0)
        if owned_pid is not None and (
            _process_alive(owned_pid) or (self.pgid is not None and _process_group_alive(self.pgid))
        ):
            self._stop_owned_process(owned_pid, known_pgid=self.pgid)
        wait_until(
            lambda: not any(is_port_open(port) for port in DEVNET_PORTS),
            timeout_s=20.0,
            description=f"failed-start devnet ports {DEVNET_PORTS} to close",
        )
        self._remove_owned_pid_file_after_exit(owned_pid)
        self.pid = None
        self.pgid = None

    def _wait_service_ready(self) -> None:
        wait_until(
            lambda: self.rpc.ready() and self.proxy_rpc.ready(),
            timeout_s=self.startup_timeout_s,
            interval_s=0.5,
            description="direct and proxy CKB RPC readiness",
        )
        direct_genesis = self.rpc.call("get_block_hash", ["0x0"])
        proxy_genesis = self.proxy_rpc.call("get_block_hash", ["0x0"])
        if direct_genesis != proxy_genesis:
            raise AssertionError(f"Direct/proxy genesis mismatch: {direct_genesis} != {proxy_genesis}")
        if self.pid is None or not _process_alive(self.pid):
            raise AssertionError(f"offckb daemon PID {self.pid} exited during readiness")

    def _load_effective_udt_scripts(self) -> None:
        config_path = self.config_path
        if config_path is None:
            raise AssertionError("Cannot resolve the effective devnet config path from daemon metadata")
        completed = subprocess.run(
            [str(self.ckb_bin), "list-hashes", "-C", str(config_path), "-f", "json"],
            cwd=self.runner.cwd,
            env=self.runner.env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"Failed to inspect effective CKB system scripts ({completed.returncode}): {completed.stderr}"
            )
        document = json.loads(completed.stdout)
        if not isinstance(document, dict) or not document:
            raise AssertionError(f"Unexpected CKB list-hashes output: {document!r}")
        chain = next(iter(document.values()))
        resolved: dict[str, dict[str, str]] = {}
        for cell in chain.get("system_cells", []):
            path_value = str(cell.get("path", "")).rstrip(")").replace("\\", "/")
            name = path_value.rsplit("/", 1)[-1]
            if name not in {"sudt", "xudt"}:
                continue
            type_hash = cell.get("type_hash")
            resolved[name] = {
                "code_hash": type_hash or cell["data_hash"],
                "hash_type": "type" if type_hash else "data2",
                "args": "0x",
            }
        if set(resolved) != {"sudt", "xudt"}:
            raise AssertionError(f"Effective chain spec is missing SUDT/xUDT system scripts: {resolved!r}")
        self.rpc.configure_udt_scripts(resolved)

    def miner_lock_script(self) -> dict[str, str]:
        config_path = self.config_path
        if config_path is None:
            raise AssertionError("Cannot resolve devnet config while reading the miner lock")
        with (config_path / "ckb.toml").open("rb") as stream:
            config = tomllib.load(stream)
        assembler = config.get("block_assembler")
        if not isinstance(assembler, dict):
            raise AssertionError("Effective ckb.toml has no block_assembler configuration")
        return {
            "code_hash": str(assembler["code_hash"]),
            "hash_type": str(assembler["hash_type"]),
            "args": str(assembler["args"]),
        }

    def wait_miner_funded(self, minimum_shannons: int, timeout_s: float = 120.0) -> int:
        lock = self.miner_lock_script()
        observed = 0

        def spendable() -> bool:
            nonlocal observed
            if self.rpc.indexer_tip() < self.rpc.tip():
                return False
            observed = self.rpc.ckb_balance(lock)
            return observed >= minimum_shannons

        wait_until(
            spendable,
            timeout_s=timeout_s,
            interval_s=1.0,
            description=f"devnet miner balance >= {minimum_shannons} shannons",
        )
        return observed

    def stop(self) -> dict[str, Any]:
        if not self._has_owned_state():
            return {"ok": True, "command": "node.stop", "stopped": False, "reason": "not-running"}

        owned_pid = self.pid if self.pid is not None else self._recover_owned_pid()
        owned_pgid = self.pgid if self.pgid is not None else self._verified_owned_process_group(owned_pid)
        command_error: BaseException | None = None
        payload: dict[str, Any] = {}
        try:
            result = self.runner.run("node", "stop", timeout_s=30.0)
            payload = result.json or {}
        except BaseException as error:
            command_error = error
            if owned_pid is not None and (_process_alive(owned_pid) or owned_pgid is not None):
                self._stop_owned_process(owned_pid, known_pgid=owned_pgid)

        wait_until(
            lambda: not any(is_port_open(port) for port in DEVNET_PORTS),
            timeout_s=20.0,
            description=f"devnet ports {DEVNET_PORTS} to close",
        )
        if owned_pid is not None:
            wait_until(
                lambda: not _process_alive(owned_pid)
                and (owned_pgid is None or not _process_group_alive(owned_pgid)),
                timeout_s=10.0,
                description=f"owned daemon PID/group {owned_pid}/{owned_pgid} to exit",
            )
        if command_error is not None:
            # Cleanup may repair state only after a failure, never turn it into success.
            raise command_error
        wait_until(
            lambda: not self.pid_file.exists(),
            timeout_s=5.0,
            description=f"OffCKB itself to remove daemon PID metadata {self.pid_file}",
        )
        self.pid = None
        self.pgid = None
        return payload

    def reset(self) -> "DevnetManager":
        if self._has_owned_state():
            self.stop()
        self.runner.run("clean", timeout_s=30.0)
        self.start()
        return self

    def close(self) -> None:
        if not self._has_owned_state():
            return
        try:
            self.stop()
        except BaseException as error:
            try:
                owned_pid = self.pid if self.pid is not None else self._recover_owned_pid()
                if owned_pid is not None and (
                    _process_alive(owned_pid) or (self.pgid is not None and _process_group_alive(self.pgid))
                ):
                    self._stop_owned_process(owned_pid, known_pgid=self.pgid)
                wait_until(
                    lambda: not any(is_port_open(port) for port in DEVNET_PORTS),
                    timeout_s=20.0,
                    description=f"fallback devnet ports {DEVNET_PORTS} to close",
                )
                self._remove_owned_pid_file_after_exit(owned_pid)
                self.pid = None
                self.pgid = None
            except BaseException as cleanup_error:
                error.add_note(f"fallback teardown also failed: {cleanup_error}")
            raise

    def _verified_owned_process_group(self, pid: int | None) -> int | None:
        if pid is None or not _process_alive(pid):
            return None
        command = _process_command(pid)
        cli_entry = self._owned_cli_entry
        if not command or not _command_references_path(command, cli_entry):
            raise RuntimeError(
                f"Refusing fallback termination: PID {pid} command does not contain owned CLI entry {cli_entry!r}: {command!r}"
            )
        pgid = os.getpgid(pid)
        return pgid if pgid == pid else None

    def _stop_owned_process(self, pid: int, *, known_pgid: int | None = None) -> None:
        pgid = known_pgid
        if _process_alive(pid):
            verified_pgid = self._verified_owned_process_group(pid)
            if pgid is None:
                pgid = verified_pgid
            elif verified_pgid != pgid:
                raise RuntimeError(f"Owned daemon PID {pid} changed process group: {pgid} -> {verified_pgid}")
        elif pgid is not None and _process_group_alive(pgid) and not self._orphan_group_is_owned(pgid):
            raise RuntimeError(f"Refusing to signal unverified orphan process group {pgid}")

        target = -pgid if pgid == pid else pid
        try:
            os.kill(target, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            wait_until(
                lambda: not _process_alive(pid) and (pgid is None or not _process_group_alive(pgid)),
                timeout_s=5.0,
                description=f"PID/group {pid}/{pgid} after SIGTERM",
            )
        except AssertionError:
            try:
                os.kill(target, signal.SIGKILL)
            except ProcessLookupError:
                pass
            wait_until(
                lambda: not _process_alive(pid) and (pgid is None or not _process_group_alive(pgid)),
                timeout_s=5.0,
                description=f"PID/group {pid}/{pgid} after SIGKILL",
            )

    def _remove_owned_pid_file_after_exit(self, owned_pid: int | None) -> None:
        metadata = _read_pid_metadata(self.pid_file)
        if metadata is None:
            return
        if not self._pid_metadata_owned_by_runner(metadata):
            raise RuntimeError(f"Refusing to remove unowned daemon PID metadata: {self.pid_file}: {metadata!r}")
        metadata_pid = int(metadata["pid"])
        if owned_pid is not None and metadata_pid != owned_pid:
            raise RuntimeError(f"Daemon PID metadata changed from {owned_pid} to {metadata_pid}; refusing removal")
        if _process_alive(metadata_pid):
            raise RuntimeError(f"Refusing to remove PID metadata while owned daemon {metadata_pid} is alive")
        if self.pgid is not None and _process_group_alive(self.pgid):
            raise RuntimeError(f"Refusing to remove PID metadata while owned process group {self.pgid} is alive")
        self.pid_file.unlink()


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _process_command(pid: int) -> str | None:
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=5,
    )
    command = completed.stdout.strip()
    return command or None


def scrub_secret_artifacts(run_root: Path) -> None:
    root = run_root.resolve()
    secrets_dir = (root / "secrets").resolve()
    if secrets_dir.parent != root:
        raise RuntimeError(f"Refusing to scrub secrets outside the isolated runtime: {secrets_dir}")
    if secrets_dir.exists():
        shutil.rmtree(secrets_dir)


def transaction_hash_from(result: CommandResult) -> str:
    if not result.json:
        raise AssertionError(f"Command has no JSON result: {result}")
    for key in ("txHash", "tx_hash", "transactionHash"):
        value = result.json.get(key)
        if isinstance(value, str) and value.startswith("0x") and len(value) == 66:
            return value
    raise AssertionError(f"Command result has no 32-byte transaction hash: {result.json}")


def block_number_from_transaction(result: Mapping[str, Any]) -> int:
    status = result.get("tx_status", {})
    value = status.get("block_number")
    if value is not None:
        return hex_int(value)
    block_hash = status.get("block_hash")
    if not block_hash:
        raise AssertionError(f"Committed transaction is missing block location: {result}")
    raise AssertionError("CKB response omitted tx_status.block_number; caller must resolve block hash")


def assert_unique(items: Iterable[Any], *, description: str) -> None:
    values = list(items)
    if len(set(values)) != len(values):
        raise AssertionError(f"Expected unique {description}, got {values!r}")
