from __future__ import annotations

import json

import pytest

from .harness import OffckbRunner


pytestmark = pytest.mark.core


# TEST-MAP: CLI-01
def test_cli_version_matches_installed_package(offckb: OffckbRunner) -> None:
    """用户安装后查到的 OffCKB 版本与实际安装包一致。"""
    package_path = offckb.cli_entry.parent.parent / "package.json"
    package = json.loads(package_path.read_text(encoding="utf-8"))

    result = offckb.run("--version", json_mode=False, timeout_s=15)

    assert result.stdout.strip() == package["version"], (
        f"offckb --version must match the installed package at {package_path}"
    )
