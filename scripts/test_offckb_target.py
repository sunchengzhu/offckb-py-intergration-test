"""Runner checks using temporary Git repositories, fake npm responses and a stand-in CLI."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from scripts.offckb_target import (
    Target, describe, ensure_source, git, load_prepared, package_info,
    prepare, resolve_commit, source_path, target_settings,
)
from tests.conftest import OffckbArtifact, _create_offckb_runner


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.remote = self.root / "remote"
        self.remote.mkdir()
        self.command(self.remote, "init", "-b", "develop")
        (self.remote / "package.json").write_text(json.dumps({"name": "@offckb/cli", "version": "1.0.0"}))
        self.initial = self.commit(self.remote)
        self.target = Target(self.root / "runner", self.root / "checkout", str(self.remote), "develop")
        ensure_source(self.target)

    def command(self, directory, *args):
        return subprocess.check_output(
            ["git", "-C", str(directory), *args], text=True, stderr=subprocess.DEVNULL,
        ).strip()

    def commit(self, directory):
        self.command(directory, "add", ".")
        self.command(directory, "-c", "user.name=Runner Test", "-c", "user.email=runner@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        return self.command(directory, "rev-parse", "HEAD")

    def advance_remote(self):
        (self.remote / "new.txt").write_text("new remote revision")
        return self.commit(self.remote)

    def test_named_branch_uses_remote_and_preserves_local_changes(self):
        (self.target.source / "local.txt").write_text("local commit")
        local = self.commit(self.target.source)
        (self.target.source / "package.json").write_text("uncommitted content")
        (self.target.source / "untracked.txt").write_text("keep me")
        before = git(self.target.source, "status", "--porcelain")
        self.assertEqual(resolve_commit(self.target), self.initial)
        self.assertEqual(git(self.target.source, "rev-parse", "HEAD"), local)
        self.assertEqual(git(self.target.source, "status", "--porcelain"), before)
        self.assertEqual((self.target.source / "package.json").read_text(), "uncommitted content")

    def test_develop_refresh_fetches_objects_without_changing_head_or_fetch_head(self):
        marker = self.target.source / ".git" / "FETCH_HEAD"
        marker.write_text("developer fetch state\n")
        latest = self.advance_remote()
        self.assertEqual(resolve_commit(self.target), latest)
        self.assertEqual(git(self.target.source, "rev-parse", "HEAD"), self.initial)
        self.assertEqual(marker.read_text(), "developer fetch state\n")

    def test_annotated_tag_resolves_to_commit(self):
        self.command(self.remote, "-c", "user.name=Runner Test", "-c", "user.email=runner@example.invalid",
                     "tag", "-a", "v1.0.0", "-m", "fixture", self.initial)
        self.advance_remote()
        self.assertEqual(resolve_commit(replace(self.target, ref="v1.0.0")), self.initial)

    def test_explicit_commit_and_working_tree_can_select_local_commit(self):
        (self.target.source / "local.txt").write_text("not pushed")
        local = self.commit(self.target.source)
        self.assertEqual(resolve_commit(replace(self.target, ref=local[:10])), local)
        self.assertEqual(resolve_commit(replace(self.target, ref="working-tree")), local)

    def test_missing_ref_does_not_fall_back_to_local_branch(self):
        self.command(self.target.source, "branch", "local-only")
        with self.assertRaises(ValueError):
            resolve_commit(replace(self.target, ref="local-only"))
        self.assertEqual(git(self.target.source, "rev-parse", "HEAD"), self.initial)

    def test_broken_source_link_is_not_cloned_over(self):
        source_dir = self.root / "source"
        source_dir.mkdir()
        (source_dir / "offckb").symlink_to(self.root / "missing")
        with self.assertRaisesRegex(ValueError, "链接已失效"):
            source_path(self.root)
        self.assertFalse((self.root / "missing").exists())

    def make_package(self, name="@offckb/cli"):
        self.target.cache.mkdir(parents=True, exist_ok=True)
        path = self.target.cache / "offckb-cli.tgz"
        data = json.dumps({"name": name, "version": "1.0.0"}).encode()
        with tarfile.open(path, "w:gz") as archive:
            member = tarfile.TarInfo("package/package.json")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        return path

    def test_prepared_package_rejects_selection_changes_and_tampering(self):
        package = self.make_package()
        info = {**package_info(package), "selection": self.target.selection}
        (self.target.cache / "target.json").write_text(json.dumps(info))
        self.assertEqual(load_prepared(self.target)["version"], "1.0.0")
        with self.assertRaisesRegex(ValueError, "版本配置已改变"):
            load_prepared(replace(self.target, ref="different"))
        package.write_bytes(b"changed package")
        with self.assertRaisesRegex(ValueError, "丢失或内容改变"):
            load_prepared(self.target)

    def test_rejects_tarball_for_another_package(self):
        with self.assertRaisesRegex(ValueError, "不是有效的"):
            package_info(self.make_package("another-package"))

    def test_config_defaults_environment_and_explicit_precedence(self):
        directory = self.root / "config"
        directory.mkdir()
        (directory / "offckb.toml").write_text('repo = "configured-repo"\nref = "latest"\n')
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(target_settings(self.root).ref, "latest")
        with patch.dict(os.environ, {"OFFCKB_REF": "env-ref", "OFFCKB_REPO": "env-repo"}, clear=True):
            self.assertEqual(target_settings(self.root).ref, "env-ref")
            explicit = target_settings(self.root, ref="explicit", repo="explicit-repo")
            self.assertEqual((explicit.ref, explicit.repo), ("explicit", "explicit-repo"))


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.target = Target(self.root, None, "unused-git-repo", "latest")

    def release(self, version="1.0.0"):
        output = io.BytesIO()
        data = json.dumps({"name": "@offckb/cli", "version": version}).encode()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            member = tarfile.TarInfo("package/package.json")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        content = output.getvalue()
        metadata = {
            "name": "@offckb/cli", "version": version,
            "dist": {
                "tarball": f"https://registry.npmjs.org/@offckb/cli/-/cli-{version}.tgz",
                "integrity": "sha512-" + base64.b64encode(hashlib.sha512(content).digest()).decode(),
            },
        }
        return metadata, content

    def prepare_release(self, metadata, content):
        with patch("scripts.offckb_target.urlopen", side_effect=[io.BytesIO(json.dumps(metadata).encode()), io.BytesIO(content)]), \
             patch("scripts.offckb_target.warm_dependencies"):
            return prepare(self.target, "unused-pnpm")

    def test_latest_uses_exact_published_bytes_and_refreshes_only_on_prepare(self):
        for version in ("1.0.0", "1.1.0"):
            metadata, content = self.release(version)
            info = self.prepare_release(metadata, content)
            self.assertEqual(Path(info["package"]).read_bytes(), content)
            self.assertEqual(info["version"], version)
            self.assertNotIn("commit", info)
            self.assertIn("RELEASE / npm 官方发布包", describe(info))
            with patch("scripts.offckb_target.urlopen", side_effect=AssertionError("test must not contact npm")):
                self.assertEqual(load_prepared(self.target), info)
                self.assertEqual(load_prepared(replace(self.target, repo="another-repo")), info)
        with self.assertRaisesRegex(ValueError, "版本配置已改变"):
            load_prepared(replace(self.target, ref="develop"))

    def test_corrupted_download_keeps_previous_prepared_package(self):
        metadata, content = self.release()
        previous = self.prepare_release(metadata, content)
        with self.assertRaisesRegex(ValueError, "完整性校验失败"):
            self.prepare_release(metadata, content + b"tampered")
        self.assertEqual(load_prepared(self.target), previous)

    def test_registry_and_package_versions_must_match(self):
        metadata, content = self.release()
        metadata["version"] = "9.9.9"
        with self.assertRaisesRegex(ValueError, "包内版本不一致"):
            self.prepare_release(metadata, content)
        self.assertFalse((self.target.cache / "target.json").exists())

    def test_latest_needs_no_source_even_with_broken_source_link(self):
        (self.root / "config").mkdir()
        (self.root / "config" / "offckb.toml").write_text('repo = "unused-repo"\nref = "latest"\n')
        (self.root / "source").mkdir()
        (self.root / "source" / "offckb").symlink_to(self.root / "missing")
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(target_settings(self.root).source)
            with self.assertRaisesRegex(ValueError, "链接已失效"):
                target_settings(self.root, ref="develop")


class RuntimeVersionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "workspace").mkdir()
        (self.root / "manifest.json").write_text("{}")
        self.entry = self.root / "stand_in_cli.py"
        self.entry.write_text(
            "import os, sys\n"
            "assert sys.argv[1:] == ['--version']\n"
            "sys.stdout.write(os.environ.get('TEST_VERSION_OUTPUT', ''))\n"
            "sys.exit(int(os.environ.get('TEST_VERSION_EXIT', '0')))\n"
        )
        self.terminal = Mock()
        self.config = SimpleNamespace(pluginmanager=SimpleNamespace(get_plugin=lambda name: self.terminal))

    def create_runtime(self, output, *, expected=None, exit_code=0):
        artifact = OffckbArtifact(
            command_entry=self.entry, daemon_entry=self.entry, source="runner-fixture",
            target_info={"version": expected} if expected is not None else None,
        )
        return _create_offckb_runner(
            offckb_command=(sys.executable, str(self.entry)), offckb_artifact=artifact,
            offckb_entry=self.entry, run_root=self.root, pytestconfig=self.config,
            isolated_env={"HOME": str(self.root), "TEST_VERSION_OUTPUT": output, "TEST_VERSION_EXIT": str(exit_code)},
        )

    def test_entry_version_comes_from_executed_command_and_is_recorded(self):
        self.create_runtime("0.7.0-dev\n")
        manifest = json.loads((self.root / "manifest.json").read_text())
        self.assertEqual(manifest["offckbVersion"], "0.7.0-dev")
        self.assertEqual((self.root / "commands" / "artifact-version.stdout.log").read_text(), "0.7.0-dev\n")
        banner = self.terminal.write_line.call_args.args[0]
        self.assertIn("OffCKB 版本：0.7.0-dev（offckb --version）", banner)
        self.assertIn("ENTRY", banner)

    def test_package_version_mismatch_blocks_runtime_and_keeps_diagnostics(self):
        with self.assertRaisesRegex(pytest.UsageError, "版本不一致"):
            self.create_runtime("0.7.0\n", expected="0.6.0")
        self.assertEqual((self.root / "commands" / "artifact-version.stdout.log").read_text(), "0.7.0\n")
        self.terminal.write_line.assert_not_called()

    def test_invalid_version_command_never_falls_back_to_package_metadata(self):
        for output, exit_code in (("0.6.0\n", 3), ("", 0), ("0.6.0\nunexpected\n", 0)):
            with self.subTest(output=output, exit_code=exit_code):
                with self.assertRaises(pytest.UsageError):
                    self.create_runtime(output, expected="0.6.0", exit_code=exit_code)
                self.terminal.write_line.assert_not_called()


if __name__ == "__main__":
    unittest.main()
