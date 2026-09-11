"""Process and diagnostic checks for the harness, without an OffCKB devnet."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.harness import OffckbRunner


class ProcessRunnerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.secret = "0x" + "ab" * 32

    def runner(self, source: str, *, secret: bool = False) -> OffckbRunner:
        script = self.root / "command.py"
        script.write_text(source, encoding="utf-8")
        env = dict(os.environ)
        env.pop("OFFCKB_PRIVATE_KEY", None)
        if secret:
            env["OFFCKB_PRIVATE_KEY"] = self.secret
        return OffckbRunner(
            (sys.executable, str(script)),
            env=env,
            cwd=self.root,
            records_dir=self.root / "commands",
        )

    def recorded(self) -> str:
        return "\n".join(path.read_text() for path in (self.root / "commands").iterdir())

    def test_text_mode_preserves_arguments_and_accepts_non_json_output(self):
        runner = self.runner("import sys\nprint('Building project')\nprint(repr(sys.argv[1:]))\n")
        result = runner.run("build", "a path with spaces", json_mode=False)
        self.assertEqual(result.returncode, 0)
        self.assertIsNone(result.json)
        self.assertNotIn("--json", result.argv)
        self.assertIn("['build', 'a path with spaces']", result.stdout)
        self.assertIn("Building project", self.recorded())

    def test_json_mode_remains_the_default(self):
        runner = self.runner("import json, sys\nprint(json.dumps({'ok': True, 'args': sys.argv[1:]}))\n")
        result = runner.run("accounts")
        self.assertEqual(result.json, {"ok": True, "args": ["--json", "accounts"]})

    def test_nonzero_text_command_is_checked_and_can_be_inspected(self):
        runner = self.runner("import sys\nprint('compile failed', file=sys.stderr)\nsys.exit(7)\n")
        with self.assertRaisesRegex(AssertionError, r"failed \(7\).*|compile failed"):
            runner.run(json_mode=False)
        result = runner.run(json_mode=False, check=False)
        self.assertEqual(result.returncode, 7)
        self.assertEqual(result.stderr.strip(), "compile failed")

    def test_key_file_secrets_are_redacted_in_failure_and_records(self):
        key_file = self.root / "signing-key"
        key_file.write_text(self.secret)
        runner = self.runner(
            "from pathlib import Path\nimport sys\n"
            "key = Path(sys.argv[sys.argv.index('--privkey-file') + 1]).read_text()\n"
            "print(key, flush=True)\nprint(key[2:], file=sys.stderr, flush=True)\nsys.exit(4)\n"
        )
        with self.assertRaises(AssertionError) as caught:
            runner.run("--privkey-file", key_file, json_mode=False)
        for diagnostic in (str(caught.exception), self.recorded()):
            self.assertNotIn(self.secret[2:], diagnostic)
            self.assertIn("<redacted-secret>", diagnostic)

    def test_sensitive_output_is_suppressed_on_failure(self):
        runner = self.runner("import sys\nprint('sensitive account dump')\nsys.exit(2)\n")
        with self.assertRaises(AssertionError) as caught:
            runner.run(json_mode=False, sensitive_output=True)
        self.assertNotIn("sensitive account dump", str(caught.exception))
        self.assertNotIn("sensitive account dump", self.recorded())
        self.assertIn("<suppressed sensitive output>", self.recorded())

    def _running(self, pid: int) -> bool:
        completed = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="],
            text=True, capture_output=True, check=False, timeout=5,
        )
        state = completed.stdout.strip()
        # An orphan may briefly await reaping by init; it can no longer run.
        return bool(state) and not state.startswith("Z")

    def _assert_stopped(self, pids: list[int]) -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not any(self._running(pid) for pid in pids):
                return
            time.sleep(0.05)
        self.fail(f"command left running processes: {pids}")

    def _cleanup_tree(self) -> None:
        for name in ("parent.pid", "child.pid"):
            path = self.root / name
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def _tree_script(self, *, fail_parent: bool) -> str:
        # The child ignores TERM to exercise the forced cleanup path. The
        # failing-parent variant also closes inherited pipes, so communicate()
        # finishes even though that child remains alive.
        child = (
            "import os, signal, time\nfrom pathlib import Path\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "Path('child.pid').write_text(str(os.getpid()))\n"
            "while True: time.sleep(1)\n"
        )
        return (
            "import os, signal, subprocess, sys, time\nfrom pathlib import Path\n"
            "Path('parent.pid').write_text(str(os.getpid()))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child!r}], "
            + ("stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n" if fail_parent else ")\n")
            + "deadline = time.monotonic() + 5\n"
            "while not Path('child.pid').exists():\n"
            "    if time.monotonic() > deadline: raise RuntimeError('child did not start')\n"
            "    time.sleep(0.01)\n"
            "print('child ready', flush=True)\n"
            "print(os.environ.get('OFFCKB_PRIVATE_KEY', ''), file=sys.stderr, flush=True)\n"
            + ("sys.exit(9)\n" if fail_parent else "while True: time.sleep(1)\n")
        )

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    def test_timeout_kills_children_and_preserves_redacted_diagnostics(self):
        self.addCleanup(self._cleanup_tree)
        runner = self.runner(self._tree_script(fail_parent=False), secret=True)
        unrelated = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        self.addCleanup(unrelated.wait, 5)
        self.addCleanup(unrelated.kill)
        with self.assertRaisesRegex(AssertionError, "timed out") as caught:
            runner.run(json_mode=False, timeout_s=1)
        pids = [int((self.root / name).read_text()) for name in ("parent.pid", "child.pid")]
        self._assert_stopped(pids)
        self.assertIsNone(unrelated.poll(), "cleanup affected an unrelated process group")
        self.assertIn("child ready", str(caught.exception))
        for diagnostic in (str(caught.exception), self.recorded()):
            self.assertNotIn(self.secret[2:], diagnostic)
            self.assertIn("<redacted-secret>", diagnostic)
        metadata = json.loads((self.root / "commands" / "001.json").read_text())
        self.assertTrue(metadata["timedOut"])
        self.assertIsNone(metadata["returncode"])
        self.assertTrue(caught.exception.__suppress_context__)

    @unittest.skipUnless(os.name == "posix", "POSIX process-group cleanup")
    def test_failed_parent_cleans_child_even_after_pipes_are_closed(self):
        self.addCleanup(self._cleanup_tree)
        runner = self.runner(self._tree_script(fail_parent=True))
        result = runner.run(json_mode=False, check=False, timeout_s=10)
        self.assertEqual(result.returncode, 9)
        pids = [int((self.root / name).read_text()) for name in ("parent.pid", "child.pid")]
        self._assert_stopped(pids)
        metadata = json.loads((self.root / "commands" / "001.json").read_text())
        self.assertFalse(metadata["timedOut"])
        self.assertEqual(metadata["returncode"], 9)


if __name__ == "__main__":
    unittest.main()
