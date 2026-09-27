"""Real POSIX pseudo-terminal interaction with the installed CLI."""

from __future__ import annotations

import errno
import os
import re
import select
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tests.harness import OffckbRunner, _command_references_path, _process_group_commands


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class TerminalCommand:
    def __init__(self, runner: OffckbRunner, arguments: tuple[str, ...], name: str):
        if os.name != "posix":
            raise pytest.UsageError("Fiber TTY acceptance requires a real POSIX terminal runner; Windows needs its own terminal fixture.")
        import pty
        self.runner = runner
        self.log: Path = runner.records_dir / f"{name}.tty.log"
        self.output = bytearray()
        self.master, slave = pty.openpty()
        env = {**runner.env, "TERM": "xterm-256color", "FORCE_COLOR": "0"}
        try:
            self.process = subprocess.Popen(
                (*runner.command, *arguments), stdin=slave, stdout=slave, stderr=slave,
                cwd=runner.cwd, env=env, start_new_session=True,
            )
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        self._eof = False

    @property
    def text(self) -> str:
        return _ANSI.sub("", self.output.decode("utf-8", errors="replace")).replace("\r", "")

    def _read(self, timeout: float = 0.1) -> None:
        if self._eof:
            return
        readable, _, _ = select.select([self.master], [], [], timeout)
        if not readable:
            return
        try:
            data = os.read(self.master, 65536)
        except OSError as error:
            if error.errno != errno.EIO:
                raise
            data = b""
        if not data:
            self._eof = True
        self.output.extend(data)

    def wait_prompt(self, prompt: str, *, timeout_s: float = 30) -> str:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._read()
            if prompt in self.text and "(y/N)" in self.text:
                return self.text
            assert self.process.poll() is None, f"TTY command exited before its default-no confirmation: {self.text}"
        raise AssertionError(f"TTY confirmation did not appear: {self.text}")

    def answer(self, value: str, *, timeout_s: float = 30) -> tuple[int, str]:
        os.write(self.master, value.encode())
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            self._read()
            if self.process.poll() is not None and self._eof:
                return self.process.returncode, self.text
        raise AssertionError(f"TTY command did not exit after confirmation: {self.text}")

    def close(self) -> None:
        try:
            if self.process.poll() is None:
                members = _process_group_commands(self.process.pid)
                assert members and all(
                    _command_references_path(command, self.runner.cli_entry) for command in members.values()
                ), "Refusing to stop a TTY group containing unrelated processes"
                os.killpg(self.process.pid, signal.SIGTERM)
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(self.process.pid, signal.SIGKILL)
                    self.process.wait(timeout=5)
            self._read(timeout=0)
        finally:
            self.log.write_text(self.runner._redact_text(self.text))
            os.close(self.master)

    def __enter__(self) -> "TerminalCommand":
        return self

    def __exit__(self, *_):
        self.close()
