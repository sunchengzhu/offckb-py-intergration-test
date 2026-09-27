"""Helpers for the Fiber concurrency cases: real second process, residual lock, held store lock."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from tests.harness import CommandResult, wait_until

from .environment import FiberEnvironment


@dataclass
class BackgroundCommand:
    """A second OffCKB process this test owns, so both sides are real processes."""

    process: subprocess.Popen
    stdout_path: Path
    stderr_path: Path
    argv: tuple[str, ...]

    def output(self) -> str:
        return self.stdout_path.read_text(errors="replace") + self.stderr_path.read_text(errors="replace")

    def wait(self, timeout_s: float = 60.0) -> int:
        return self.process.wait(timeout=timeout_s)

    def interrupt(self, timeout_s: float = 30.0) -> int:
        """Ctrl+C equivalent for a foreground command, then confirm its group is gone."""
        if self.process.poll() is None:
            os.killpg(self.process.pid, 2)  # SIGINT
        code = self.process.wait(timeout=timeout_s)
        wait_until(lambda: not _group_alive(self.process.pid), timeout_s=timeout_s,
                   description="the interrupted foreground Fiber manager and its children to exit")
        return code


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def start_background(env: FiberEnvironment, *args: str) -> BackgroundCommand:
    """Run one more OffCKB command in the same isolated environment, in its own process group."""
    token = f"mutex-{time.monotonic_ns()}"
    stdout_path = env.root / "commands" / f"{token}.stdout.log"
    stderr_path = env.root / "commands" / f"{token}.stderr.log"
    argv = (*env.runner.command, *args)
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(
            argv, cwd=env.runner.cwd, env=env.runner.env, stdin=subprocess.DEVNULL,
            stdout=stdout, stderr=stderr, start_new_session=True,
        )
    return BackgroundCommand(process, stdout_path, stderr_path, argv)


def wait_for_environment_reservation(env: FiberEnvironment, timeout_s: float) -> int:
    """Wait until a running command reserves the environment, and return the owning process."""
    observed: dict[str, int] = {}

    def reserved() -> bool:
        record = _read_lock(env_lock_path(env))
        if record is None:
            return False
        owner = record.get("pid")
        if not isinstance(owner, int) or not _alive(owner):
            return False
        observed["pid"] = owner
        return True

    wait_until(reserved, timeout_s=timeout_s, interval_s=0.05,
               description="a running command to reserve the environment")
    return observed["pid"]


def _read_lock(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def assert_rejected_second_command(env: FiberEnvironment, result: BackgroundCommand, purpose: str) -> CommandResult:
    """The concurrent command must fail cleanly and explain why the environment is unavailable.

    OffCKB may answer with the environment reservation or with the still-running service it
    protects; both are correct refusals, so the test reports the reason instead of fixing it.
    """
    code = result.wait(timeout_s=env.startup_timeout_s)
    output, errors = result.stdout_path.read_text(), result.stderr_path.read_text()
    completed = CommandResult(
        result.argv, env.runner.cwd, code, output, errors, 0.0, None,
    )
    assert code != 0, f"the concurrent {purpose} unexpectedly succeeded"
    assert not output.strip(), "a rejected concurrent command wrote a result to stdout"
    diagnostic = env.runner._redact_text(errors).lower()
    occupied = "another offckb process" in diagnostic
    service_running = "fiber daemon is already running" in diagnostic or "fiber daemon is still running" in diagnostic
    assert occupied or service_running, (
        f"the rejection must come from the environment protection, got:\n{diagnostic}"
    )
    return completed


def exited_process_id() -> int:
    """PID of a real process that has already exited, for a residual lock owned by nobody."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    assert child.wait(timeout=10) == 0
    return child.pid


def snapshot(path: Path) -> bytes | None:
    """Bytes of a metadata file, or None when the state does not exist yet."""
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def env_lock_path(env: FiberEnvironment) -> Path:
    """The environment lock beside the devnet config root, as documented for the isolated user directory."""
    return env.config_path.parent / ".offckb-devnet.lock"


def write_residual_lock(env: FiberEnvironment, pid: int) -> Path:
    path = env_lock_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'{{"pid":{pid},"acquiredAt":"2026-01-01T00:00:00.000Z"}}', encoding="utf-8")
    return path


def store_lock_files(env: FiberEnvironment) -> list[Path]:
    return [env.config_path / "fiber" / "nodes" / str(node) / "fiber" / "store" / "LOCK" for node in (1, 2)]


@contextmanager
def hold_store_lock(env: FiberEnvironment, lock_file: Path) -> Iterator[subprocess.Popen]:
    """Open one node's database lock from a live process, the way a leftover FNN would."""
    assert lock_file.is_file(), f"the FNN store lock must exist: {lock_file}"
    marker = env.root / "commands" / f"holder-{lock_file.parent.parent.parent.name}.ready"
    program = (
        "import pathlib,sys,time\n"
        "handle = open(sys.argv[1], 'rb')\n"
        "pathlib.Path(sys.argv[2]).write_text('held')\n"
        "time.sleep(600)\n"
    )
    stderr_path = env.root / "commands" / "store-holder.stderr.log"
    with stderr_path.open("w") as stderr:
        holder = subprocess.Popen(
            [sys.executable, "-c", program, str(lock_file), str(marker)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=stderr, start_new_session=True,
        )
    try:
        wait_until(lambda: holder.poll() is None and marker.is_file(),
                   timeout_s=10, description="the store lock holder to be ready")
        yield holder
    finally:
        if holder.poll() is None:
            holder.terminate()
        try:
            holder.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=5)
        marker.unlink(missing_ok=True)
