"""Public CLI, process and file observations for Fiber configuration/recovery."""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from tests.harness import (
    DEVNET_PORTS, CommandResult, _command_references_path, _process_alive,
    _process_group_commands, is_port_open, wait_until,
)
from .environment import FiberEnvironment


def events(text: str) -> list[dict[str, Any]]:
    records = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert all(isinstance(record, dict) for record in records), "CLI stderr must be NDJSON"
    return records


def messages(result: CommandResult) -> str:
    return "\n".join(str(item.get("message", "")) for item in events(result.stderr))


def result_json(result: CommandResult, command: str) -> dict[str, Any]:
    assert result.returncode == 0
    value = json.loads(result.stdout)
    assert isinstance(value, dict) and value.get("ok") is True
    assert value["command"] == command
    events(result.stderr)
    return value


def read_yaml(env: FiberEnvironment, path: Path) -> dict[str, Any]:
    # The installed public YAML package parses external files, never product internals.
    script = (
        "const fs=require('fs');const {createRequire}=require('module');"
        "const load=createRequire(fs.realpathSync(process.argv[1]));"
        "process.stdout.write(JSON.stringify(load('js-yaml').load(fs.readFileSync(process.argv[2],'utf8'))));"
    )
    node = env.runner.command[0] if len(env.runner.command) > 1 else shutil.which("node", path=env.runner.env.get("PATH"))
    assert node, "Node.js is required to parse the installed package's public YAML format"
    answer = subprocess.run(
        [node, "-e", script, str(env.runner.cli_entry), str(path)],
        cwd=env.runner.cwd, env=env.runner.env, capture_output=True, text=True, timeout=15,
    )
    assert answer.returncode == 0, env.runner._redact_text(answer.stderr)
    parsed = json.loads(answer.stdout)
    assert isinstance(parsed, dict)
    return parsed


def nodes_path(env: FiberEnvironment) -> Path:
    return env.config_path / "fiber" / "nodes.yml"


def write_nodes(env: FiberEnvironment, data: dict[str, Any]) -> None:
    path = nodes_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    # JSON is also valid YAML; no product configuration code is imported.
    path.write_text(json.dumps(data, indent=2) + "\n")


def assert_chain_advances(env: FiberEnvironment) -> None:
    tip = env.rpc.tip()
    wait_until(lambda: env.rpc.tip() > tip, timeout_s=30, description="the original CKB chain to keep mining")


def assert_processes_unchanged(env: FiberEnvironment, before: dict[int, str]) -> None:
    current = env.process_commands()
    assert before and all(_process_alive(pid) and current.get(pid) == command for pid, command in before.items())


def assert_failure(env: FiberEnvironment, args: tuple[object, ...], *patterns: str) -> CommandResult:
    """Observe failed CLI cleanup before fixture fallback can hide leaked children."""
    argv = (*env.runner.command, "--json", *(str(arg) for arg in args))
    token = f"expected-failure-{time.monotonic_ns()}"
    stdout_path, stderr_path = (env.root / "commands" / f"{token}.{stream}.log" for stream in ("stdout", "stderr"))
    started = time.monotonic()
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            argv, cwd=env.runner.cwd, env=env.runner.env, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, start_new_session=True,
        )
    env.track_command(process)
    # On timeout/failure the outer environment owns cleanup, after the failed assertion.
    wait_until(lambda: process.poll() is not None, timeout_s=env.startup_timeout_s,
               description=f"invalid Fiber command {token} to exit")
    output, errors = stdout_path.read_text(), stderr_path.read_text()
    assert process.returncode != 0, "the invalid command succeeded"
    wait_until(lambda: not _process_group_commands(process.pid), timeout_s=20,
               description="failed CLI to stop its own process group")
    result = CommandResult(tuple(argv), env.runner.cwd, process.returncode, output, errors,
                           time.monotonic() - started, None)
    assert not output.strip(), "failed JSON command wrote a success result to stdout"
    records = events(errors)
    failures = [record for record in records if record.get("ok") is False]
    assert len(failures) == 1 and failures[0].get("code"), env.runner._redact_text(errors)
    assert not any(record.get("ok") is True for record in records)
    diagnostic = messages(result)
    # A daemon launcher is allowed to point to its own diagnostic log.
    for path in (env.config_path / "fiber" / "logs" / "daemon.log",
                 env.config_path / "data" / "logs" / "daemon.log"):
        if path.is_file() and str(path) in diagnostic:
            diagnostic += "\n" + path.read_text(errors="replace")
    for pattern in patterns:
        assert re.search(pattern, diagnostic, re.I), env.runner._redact_text(diagnostic)
    env.remember_processes()
    return result


def assert_failed_start_is_clean(env: FiberEnvironment, *, attached: bool, reserved_port: int | None = None) -> None:
    fnn_ports = [21714, 21715, 8344, 8345]
    ports = fnn_ports if attached else [*DEVNET_PORTS, *fnn_ports]
    wait_until(lambda: not any(is_port_open(port) for port in ports if port != reserved_port),
               timeout_s=20, description="failed startup to release its service ports")
    assert not env.runtime_path.exists()
    assert not env.pid_files[1].exists()
    commands = env.process_commands()
    assert not any(_command_references_path(command, env.managed_fnn) for command in commands.values())
    if attached:
        assert env.attached_snapshot is not None
        assert_processes_unchanged(env, env.attached_snapshot["commands"])
        assert_chain_advances(env)
    else:
        assert not commands, f"failed combined start left processes: {commands}"
        assert not env.pid_files[0].exists()


@contextmanager
def occupied_port(env: FiberEnvironment, port: int) -> Iterator[subprocess.Popen]:
    """A real unrelated listener owned by this test, never an RPC stub."""
    marker = env.root / f"port-{port}.ready"
    program = (
        "import pathlib,socket,sys; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);"
        "s.bind(('127.0.0.1',int(sys.argv[1])));"
        "s.listen(); pathlib.Path(sys.argv[2]).write_text('ready');"
        "\nwhile True:\n c,a=s.accept(); c.close()\n"
    )
    stderr_path = env.root / "commands" / f"port-{port}.stderr.log"
    with stderr_path.open("w") as stderr:
        process = subprocess.Popen([sys.executable, "-c", program, str(port), str(marker)],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=stderr, start_new_session=True)
    try:
        wait_until(lambda: process.poll() is None and marker.is_file() and is_port_open(port),
                   timeout_s=10, description=f"test listener on {port}")
        yield process
        assert process.poll() is None, "OffCKB killed the unrelated port owner"
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        marker.unlink(missing_ok=True)


@contextmanager
def paused_miner(env: FiberEnvironment) -> Iterator[int]:
    candidates = [pid for pid, command in env.process_commands().items()
                  if _command_references_path(command, env.managed_ckb)
                  and _command_references_path(command, env.config_path) and "miner" in command.split()]
    assert len(candidates) == 1, "expected one miner belonging to this environment"
    pid = candidates[0]
    os.kill(pid, signal.SIGSTOP)
    try:
        # Drain any already-submitted work before the product begins its 15 s mining probe.
        last_tip, last_change = env.rpc.tip(), time.monotonic()

        def settled_tip() -> bool:
            nonlocal last_tip, last_change
            current = env.rpc.tip()
            if current != last_tip:
                last_tip, last_change = current, time.monotonic()
            return time.monotonic() - last_change >= 2

        wait_until(settled_tip, timeout_s=10, description="the deliberately paused miner to stop advancing the chain")
        yield pid
    finally:
        if _process_alive(pid):
            os.kill(pid, signal.SIGCONT)
