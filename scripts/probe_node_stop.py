#!/usr/bin/env python3
"""Native Linux daemon-stop investigation; not mapped acceptance coverage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path


def inspect(pid: int):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().rstrip("\0").split("\0")
        return {"pid": pid, "state": fields[0], "group": int(fields[2]),
                "start_ticks": fields[19], "argv": argv}
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(snapshot):
    return snapshot is not None and snapshot["state"] not in ("Z", "X")


def replace_field(path: Path, key: str, value: str):
    content, count = re.subn(rf"(?m)^{re.escape(key)}\s*=.*$", f"{key} = {value}", path.read_text())
    if count != 1:
        raise RuntimeError(f"Expected one {key} in {path.name}; got {count}")
    path.write_text(content)
    tomllib.loads(content)


def rpc(port: int):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "get_tip_block_number", "params": []}).encode()
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port}", body, {"Content-Type": "application/json"})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=1) as response:
            return json.load(response).get("result")
    except (OSError, ValueError):
        return None


def open_port(port: int):
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for(predicate, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offckb-package", type=Path, required=True)
    parser.add_argument("--ckb-bin", type=Path, required=True)
    parser.add_argument("--node-bin", default="node")
    parser.add_argument("--launch-mode", choices=("baseline", "cli-options", "env-options"),
                        default="baseline", help="Compare ordinary Node startup methods")
    args = parser.parse_args()
    if platform.system() != "Linux":
        parser.error("Run this diagnostic on native Linux (no Docker required).")
    package, ckb = args.offckb_package.resolve(strict=True), args.ckb_bin.resolve(strict=True)
    node = shutil.which(args.node_bin)
    if not node or not os.access(ckb, os.X_OK):
        parser.error("Node.js and an executable CKB binary are required.")
    root = Path(tempfile.mkdtemp(prefix="offckb-native-stop-"))
    print(f"Isolated runtime and evidence: {root}", flush=True)
    env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LD_LIBRARY_PATH") if k in os.environ}
    for variable, directory in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"),
                                ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                                ("XDG_STATE_HOME", "state"), ("XDG_RUNTIME_DIR", "runtime")):
        path = root / directory
        path.mkdir(mode=0o700)
        env[variable] = str(path)
    env["LC_ALL"] = "C"
    report = {"kernel": platform.uname()._asdict(), "package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
              "launch_mode": args.launch_mode}
    preload_log = root / "preload-observations.jsonl"
    preload = root / "observe-startup.cjs"
    # This observer only records the runtime's public state. It does not patch
    # spawn, execPath, argv, PID metadata or any OffCKB implementation.
    preload.write_text(
        "require('node:fs').appendFileSync(" + json.dumps(str(preload_log)) + ","
        "JSON.stringify({pid:process.pid,execArgv:process.execArgv,argv:process.argv,"
        "nodeOptions:process.env.NODE_OPTIONS ?? null})+'\\n');\n"
    )
    node_flags = ["--no-opt", "--require", str(preload)]
    if args.launch_mode == "env-options":
        # Node permits --require here but rejects --no-opt in NODE_OPTIONS.
        env["NODE_OPTIONS"] = "--require " + json.dumps(str(preload))
    owned = {}
    ports = []
    reservations = []
    pid = None
    cli = None

    def run(label, command):
        try:
            completed = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=120)
            result = {"exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
        except subprocess.TimeoutExpired as error:
            result = {"exit_code": None, "error": "timeout", "stdout": str(error.stdout or ""), "stderr": str(error.stderr or "")}
        (root / f"{label}.json").write_text(json.dumps(result, indent=2))
        report[label] = result
        return result

    def call(label, *arguments):
        flags = node_flags if args.launch_mode == "cli-options" else []
        return run(label, [node, *flags, str(cli), "--json", *arguments])

    def remember_owned():
        # An immutable start tick prevents signalling a reused PID. Every member
        # must reference this run's CLI or CKB config, never a host service.
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                snapshot = inspect(int(entry.name))
            except (OSError, ValueError):
                continue
            if snapshot and snapshot["group"] == pid and (
                str(cli) in snapshot["argv"] or str(config) in snapshot["argv"]
            ):
                owned[snapshot["pid"]] = snapshot

    try:
        prefix = root / "prefix"
        extract = root / "unpack"
        extract.mkdir()
        with tarfile.open(package, "r:gz") as archive:
            # Python 3.11.0 also works. Reject links/special files and escapes
            # before extraction rather than depending on a later tarfile API.
            for member in archive.getmembers():
                (extract / member.name).resolve().relative_to(extract)
                if not (member.isfile() or member.isdir()):
                    raise RuntimeError("Package contains a link or special file")
            archive.extractall(extract)
        installed = prefix / "node_modules" / "@offckb" / "cli"
        installed.parent.mkdir(parents=True)
        shutil.move(str(extract / "package"), installed)
        manifest = json.loads((installed / "package.json").read_text())
        if manifest["name"] != "@offckb/cli":
            raise RuntimeError("Not an @offckb/cli package")
        report["package_version"] = manifest["version"]
        cli = installed / manifest["bin"]["offckb"]
        cli.resolve(strict=True).relative_to(installed)
        (prefix / "bin").mkdir()
        (prefix / "bin" / "offckb").symlink_to(cli)
        report["node_version"] = run("node-version", [node, "--version"])["stdout"].strip()
        report["ckb_version"] = run("ckb-version", [str(ckb), "--version"])["stdout"].strip()
        listed = call("config-list", "config", "list")
        if listed["exit_code"] != 0:
            raise RuntimeError("config list failed")
        events = [json.loads(line) for line in listed["stderr"].splitlines() if line.startswith("{")]
        settings_path = next(Path(e["message"].removeprefix("config file:").strip()) for e in events
                             if e.get("message", "").startswith("config file:"))
        settings_path.resolve().relative_to(root)
        settings = None
        for event in events:
            try:
                candidate = json.loads(event.get("message", ""))
            except ValueError:
                continue
            if isinstance(candidate, dict) and "devnet" in candidate and "bins" in candidate:
                settings = candidate
                break
        if settings is None:
            raise RuntimeError("config list did not return settings")
        config = Path(settings["devnet"]["configPath"])
        config.resolve().relative_to(root)
        for _ in range(4):
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            reservations.append(sock)
            ports.append(sock.getsockname()[1])
        http, proxy, tcp, p2p = ports
        report["ports"] = {"rpc": http, "proxy": proxy, "tcp": tcp, "p2p": p2p}
        settings["devnet"]["rpcUrl"] = f"http://127.0.0.1:{http}"
        settings["devnet"]["rpcProxyPort"] = proxy
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2))
        shutil.copytree(installed / "ckb" / "devnet", config, dirs_exist_ok=True)
        replace_field(config / "ckb.toml", "listen_addresses", json.dumps([f"/ip4/127.0.0.1/tcp/{p2p}"]))
        replace_field(config / "ckb.toml", "listen_address", json.dumps(f"127.0.0.1:{http}"))
        replace_field(config / "ckb.toml", "tcp_listen_address", json.dumps(f"127.0.0.1:{tcp}"))
        replace_field(config / "ckb-miner.toml", "rpc_url", json.dumps(f"http://127.0.0.1:{http}/"))
        for sock in reservations:
            sock.close()
        started = call("start", "node", "--daemon", "--binary-path", str(ckb))
        # Metadata is also collected on partial startup so cleanup remains scoped.
        candidates = list(root.rglob("daemon.pid"))
        if started["exit_code"] == 0:
            startup = json.loads(started["stdout"])
            candidates.insert(0, Path(startup["pidFile"]))
        for path in candidates:
            path.resolve().relative_to(root)
            metadata = json.loads(path.read_text())
            if metadata.get("scriptPath") == str(cli) and isinstance(metadata.get("pid"), int):
                pid = metadata["pid"]
                report["metadata"] = metadata
                report["pid_file"] = str(path)
                break
        if pid is not None:
            remember_owned()
        if started["exit_code"] != 0 or pid is None:
            raise RuntimeError("Daemon startup failed; inspect start.json")
        report["before"] = inspect(pid)
        report["daemon_argv"] = report["before"]["argv"] if report["before"] else None
        report["daemon_script_index"] = (report["daemon_argv"].index(str(cli))
                                         if report["daemon_argv"] and str(cli) in report["daemon_argv"] else None)
        observations = [json.loads(line) for line in preload_log.read_text().splitlines()] if preload_log.exists() else []
        report["preload_observations"] = observations
        report["daemon_preload"] = next((item for item in observations if item["pid"] == pid), None)
        if args.launch_mode == "env-options" and report["daemon_preload"] is None:
            raise RuntimeError("Invalid experiment: NODE_OPTIONS preload did not run in daemon")
        if args.launch_mode == "cli-options" and not observations:
            raise RuntimeError("Invalid experiment: CLI preload did not run")
        if not wait_for(lambda: rpc(http) is not None and rpc(proxy) is not None):
            raise RuntimeError("RPC/proxy readiness timeout")
        report["rpc_before"] = [rpc(http), rpc(proxy)]
        stopped = call("stop", "node", "stop")
        report["stop_exit_code"] = stopped["exit_code"]
        report["identity_rejected"] = "does not appear to be the offckb daemon" in stopped["stderr"]
        remember_owned()
        report["after"] = inspect(pid)
        report["rpc_after"] = [rpc(http), rpc(proxy)]
        report["pid_file_exists_after"] = path.exists()
        stop_result = json.loads(stopped["stdout"]) if stopped["exit_code"] == 0 else {}
        released = wait_for(lambda: not alive(inspect(pid)) and not path.exists()
                            and all(not open_port(port) for port in ports), timeout=15)
        report["passed"] = stopped["exit_code"] == 0 and stop_result.get("stopped") is True and released
        if not report["passed"]:
            report["error"] = "node stop did not stop and clean up its own daemon"
    except Exception as error:
        report["passed"] = False
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        for sock in reservations:
            sock.close()
        report["fallback_cleanup"] = []
        # Kill individual verified members, not arbitrary PID-file values or groups.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for child, original in owned.items():
                current = inspect(child)
                if alive(current) and current["start_ticks"] == original["start_ticks"] and current["argv"] == original["argv"]:
                    try:
                        os.kill(child, sig)
                        report["fallback_cleanup"].append({"pid": child, "signal": sig.name})
                    except ProcessLookupError:
                        pass
            if wait_for(lambda: all(not alive(inspect(child)) for child in owned), timeout=5):
                break
        report["owned_processes_remaining"] = [child for child in owned if alive(inspect(child))]
        if report["owned_processes_remaining"]:
            report["passed"] = False
        (root / "result.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({k: report.get(k) for k in (
            "launch_mode", "passed", "package_version", "node_version", "ckb_version", "ports",
            "daemon_argv", "daemon_script_index", "daemon_preload", "stop_exit_code", "identity_rejected",
            "error", "fallback_cleanup", "owned_processes_remaining",
        )}, indent=2))
        print(f"Evidence: {root / 'result.json'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
