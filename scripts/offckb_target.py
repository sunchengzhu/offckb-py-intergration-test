"""Prepare a versioned OffCKB artifact without switching the source checkout."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from urllib.parse import urlparse
from urllib.request import urlopen
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = "https://registry.npmjs.org"


@dataclass(frozen=True)
class Target:
    root: Path
    source: Path | None
    repo: str
    ref: str

    @property
    def cache(self) -> Path:
        return self.root / "source" / ".prepared"

    @property
    def selection(self) -> dict[str, str]:
        if self.ref == "latest":
            return {"package": "@offckb/cli", "registry": REGISTRY, "ref": "latest"}
        return {"source": str(self.source), "repo": self.repo, "ref": self.ref}


def source_path(root: Path, configured: str | None = None) -> Path:
    candidates = (
        (Path(configured).expanduser(),)
        if configured else (root / "source" / "offckb", root.parent / "offckb")
    )
    for candidate in candidates:
        if candidate.is_symlink() and not candidate.exists():
            raise ValueError(f"源码链接已失效，请修复后重试：{candidate}")
        if candidate.exists() or candidate.is_symlink():
            return candidate.resolve()
    if configured:
        return candidates[0].resolve()
    return root / "source" / "offckb"


def target_settings(
    root: Path = ROOT, *, source: str | None = None, repo: str | None = None, ref: str | None = None
) -> Target:
    with (root / "config" / "offckb.toml").open("rb") as stream:
        defaults = tomllib.load(stream)
    selected_ref = ref or os.environ.get("OFFCKB_REF") or defaults["ref"]
    target = Target(
        root=root,
        source=None if selected_ref == "latest" else source_path(root, source or os.environ.get("OFFCKB_SOURCE")),
        repo=repo or os.environ.get("OFFCKB_REPO") or defaults["repo"],
        ref=selected_ref,
    )
    if (
        not isinstance(target.repo, str) or not isinstance(target.ref, str)
        or not target.repo.strip() or not target.ref.strip() or target.ref.startswith("-")
    ):
        raise ValueError("OffCKB repo 和 ref 必须是非空的有效配置")
    return target


def git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args], capture_output=True, text=True,
        timeout=120, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def ensure_source(target: Target) -> None:
    source = target.source
    assert source is not None
    if not source.exists():
        if target.ref == "working-tree":
            raise ValueError(f"working-tree 要求已有源码目录：{source}")
        source.parent.mkdir(parents=True, exist_ok=True)
        print(f"克隆 OffCKB 源码到 {source}", flush=True)
        subprocess.run(
            ["git", "clone", "--", target.repo, str(source)], check=True, timeout=300,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    if Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source.resolve():
        raise ValueError(f"OffCKB 源码必须是独立 Git 仓库根目录：{source}")
    manifest = json.loads((source / "package.json").read_text())
    if not isinstance(manifest, dict) or manifest.get("name") != "@offckb/cli":
        raise ValueError(f"源码目录不是 @offckb/cli：{source}")


def resolve_commit(target: Target) -> str:
    """Named refs come from the configured repository, never a local namesake."""
    assert target.source is not None
    if target.ref == "working-tree":
        return git(target.source, "rev-parse", "HEAD")
    if re.fullmatch(r"[0-9a-fA-F]{7,40}", target.ref):
        try:
            return git(target.source, "rev-parse", "--verify", f"{target.ref}^{{commit}}")
        except ValueError:
            if len(target.ref) != 40:
                raise ValueError("本地不存在该短 commit，请使用完整 SHA") from None
            commit = target.ref.lower()
    else:
        if target.ref.startswith("refs/"):
            names = [target.ref, f"{target.ref}^{{}}"]
        else:
            names = [f"refs/heads/{target.ref}", f"refs/tags/{target.ref}", f"refs/tags/{target.ref}^{{}}"]
        output = git(target.source, "ls-remote", "--exit-code", "--", target.repo, *names)
        refs = dict(line.split("\t", 1)[::-1] for line in output.splitlines())
        commit = refs.get(names[0]) if names[0].startswith("refs/heads/") else None
        commit = commit or refs.get(names[-1]) or refs.get(names[-2])
        if not commit:
            raise ValueError(f"仓库中不存在分支或 tag：{target.ref}")
    try:
        return git(target.source, "rev-parse", "--verify", f"{commit}^{{commit}}")
    except ValueError:
        print(f"获取 OffCKB commit {commit}", flush=True)
        # Fetch objects only: keep the developer's branches, index and FETCH_HEAD intact.
        git(target.source, "fetch", "--no-tags", "--no-write-fetch-head", "--", target.repo, commit)
        return git(target.source, "rev-parse", "--verify", f"{commit}^{{commit}}")


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def package_info(package: Path) -> dict[str, str]:
    try:
        with tarfile.open(package, "r:gz") as archive:
            member = archive.getmember("package/package.json")
            if not member.isfile() or member.size > 1_000_000:
                raise ValueError("发布包中的 package.json 无效")
            stream = archive.extractfile(member)
            assert stream is not None
            manifest = json.load(stream)
    except (OSError, KeyError, tarfile.TarError, ValueError) as error:
        raise ValueError(f"无法读取 OffCKB 发布包 {package}：{error}") from error
    if (
        not isinstance(manifest, dict) or manifest.get("name") != "@offckb/cli"
        or not isinstance(manifest.get("version"), str)
    ):
        raise ValueError(f"发布包不是有效的 @offckb/cli：{package}")
    return {"package": str(package), "version": manifest["version"], "sha256": sha256(package)}


def describe(info: dict, *, runtime_version: str | None = None) -> str:
    title = "OffCKB 被测版本与来源" if runtime_version is not None else "OffCKB 待测包信息（尚未执行 --version）"
    lines = ["=" * 72, title, "-" * 72]
    if runtime_version is not None:
        lines.append(f"OffCKB 版本：{runtime_version}（offckb --version）")
    if info.get("mode") == "entry":
        lines.extend([
            "构建来源：ENTRY / 本地调试入口",
            f"入口路径：{info['entry']}",
        ])
    else:
        lines.append(f"包内版本：{info['version']}（package.json）")
        if info.get("mode") == "npm":
            lines.extend([
                "构建来源：RELEASE / npm 官方发布包",
                "版本选择：@offckb/cli@latest",
            ])
        elif info.get("selection"):
            ref = info["selection"]["ref"]
            lines.extend([
                f"构建来源：SOURCE / 源码编译（{ref}）",
                f"Git ref：{ref}",
                f"Git repo：{info['selection']['repo']}",
                f"Git commit：{info['commit']}",
                f"包含本地修改：{'是' if info['dirty'] else '否'}",
            ])
        else:
            lines.append("构建来源：TARBALL / 外部指定包（发布来源未验证）")
        lines.append(f"包 SHA256：{info['sha256']}")
    lines.append("=" * 72)
    return "\n".join(lines)


def describe_prepared(info: dict) -> str:
    if info.get("mode") == "entry":
        return f"OffCKB 已准备：ENTRY / {info['entry']}（版本将在测试时确认）"
    if info.get("mode") == "npm":
        source = "RELEASE / npm latest"
    elif info.get("selection"):
        source = f"SOURCE / {info['selection']['ref']} @ {info['commit'][:12]}"
        if info["dirty"]:
            source += "（包含本地修改）"
    else:
        source = "TARBALL / 外部指定包"
    return f"OffCKB 已准备：{info['version']} | {source}（包内版本）"


def load_prepared(target: Target) -> dict:
    path = target.cache / "target.json"
    if not path.is_file():
        raise ValueError("尚未准备 OffCKB 版本，请先运行 make prepare")
    info = json.loads(path.read_text())
    if not isinstance(info, dict) or info.get("selection") != target.selection:
        raise ValueError("OffCKB 版本配置已改变，请先运行 make prepare，避免测试旧版本")
    package = Path(info["package"])
    if not package.is_file() or sha256(package) != info["sha256"]:
        raise ValueError("已准备的 OffCKB 包丢失或内容改变，请重新运行 make prepare")
    return info


def publish(target: Target, package: Path, info: dict) -> dict:
    destination = target.cache / "offckb-cli.tgz"
    info.update({"selection": target.selection, "package": str(destination)})
    metadata = package.parent / "target.json"
    metadata.write_text(json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    os.replace(package, destination)
    os.replace(metadata, target.cache / "target.json")
    print(describe_prepared(info), flush=True)
    return info


def warm_dependencies(package: Path, pnpm: str) -> None:
    """Populate pnpm caches in a disposable prefix without running package scripts."""
    store = os.environ.get("PNPM_STORE_DIR") or subprocess.check_output(
        [pnpm, "store", "path"], cwd=package.parent, text=True, timeout=60,
    ).strip()
    cache = os.environ.get("PNPM_CACHE_DIR")
    if not cache:
        if os.environ.get("XDG_CACHE_HOME"):
            cache = str(Path(os.environ["XDG_CACHE_HOME"]) / "pnpm")
        else:
            cache = str(Path.home() / ("Library/Caches/pnpm" if sys.platform == "darwin" else ".cache/pnpm"))
    cache_dir = Path(cache).expanduser().resolve()
    if cache_dir.name != "pnpm":
        raise ValueError("PNPM_CACHE_DIR 必须指向名为 pnpm 的缓存目录")
    prefix = package.parent / "npm-prefix"
    prefix.mkdir()
    (prefix / "package.json").write_text(json.dumps({"name": "offckb-prepare", "private": True}))
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if key in os.environ}
    env.update({"CI": "true", "HUSKY": "0", "npm_config_ignore_scripts": "true"})
    for key in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR", "APPDATA", "LOCALAPPDATA"):
        directory = package.parent / key.lower()
        directory.mkdir(mode=0o700)
        env[key] = str(directory)
    env["XDG_CACHE_HOME"] = str(cache_dir.parent)
    print("准备发布包依赖缓存（临时 prefix，禁用安装脚本）", flush=True)
    subprocess.run(
        [pnpm, "--ignore-workspace", "add", "--dir", str(prefix), "--store-dir", str(Path(store).expanduser().resolve()),
         "--prefer-offline", "--ignore-scripts", "--save-exact", "--reporter=append-only", str(package)],
        check=True, cwd=prefix, env=env, timeout=600,
    )


def prepare_release(target: Target, pnpm: str) -> dict:
    print("查询 npm @offckb/cli@latest", flush=True)
    with urlopen(f"{REGISTRY}/@offckb%2fcli/latest", timeout=60) as response:
        release = json.load(response)
    if (
        not isinstance(release, dict) or release.get("name") != "@offckb/cli"
        or not isinstance(release.get("version"), str) or not isinstance(release.get("dist"), dict)
    ):
        raise ValueError("npm 返回的 @offckb/cli 发布信息无效")
    dist = release["dist"]
    url, integrity = dist.get("tarball", ""), dist.get("integrity", "")
    if not isinstance(url, str) or not isinstance(integrity, str):
        raise ValueError("npm 发布包地址或完整性信息无效")
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "registry.npmjs.org":
        raise ValueError("npm 发布包必须来自官方 HTTPS registry")
    target.cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="release-", dir=target.cache) as temporary:
        package = Path(temporary) / "offckb-cli.tgz"
        print(f"下载已发布的 @offckb/cli@{release['version']}", flush=True)
        with urlopen(url, timeout=120) as response, package.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        with package.open("rb") as stream:
            digest = base64.b64encode(hashlib.file_digest(stream, "sha512").digest()).decode()
        if f"sha512-{digest}" not in integrity.split():
            raise ValueError("npm 发布包 SHA512 完整性校验失败")
        info = package_info(package)
        if info["version"] != release["version"]:
            raise ValueError("npm 发布信息与包内版本不一致")
        info.update({"mode": "npm", "tarball": url, "integrity": integrity})
        warm_dependencies(package, pnpm)
        return publish(target, package, info)


def prepare(target: Target, pnpm: str) -> dict:
    if target.ref == "latest":
        return prepare_release(target, pnpm)
    ensure_source(target)
    commit = resolve_commit(target)
    dirty = target.ref == "working-tree" and bool(git(target.source, "status", "--porcelain"))
    print(f"准备目标：{target.repo} | {target.ref} -> {commit}", flush=True)
    target.cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=target.cache) as temporary:
        staging = Path(temporary)
        if target.ref == "working-tree":
            build_root = target.source
        else:
            build_root = staging / "source"
            build_root.mkdir()
            archive_path = staging / "source.tar"
            git(target.source, "archive", "--format=tar", f"--output={archive_path}", commit)
            with tarfile.open(archive_path) as archive:
                archive.extractall(build_root, filter="data")
        env = {**os.environ, "CI": "true", "HUSKY": "0"}
        install = [pnpm, "-C", str(build_root), "install", "--frozen-lockfile", "--reporter=append-only"]
        if os.environ.get("PNPM_STORE_DIR"):
            install.extend(["--store-dir", os.environ["PNPM_STORE_DIR"]])
        subprocess.run(install, check=True, env=env, timeout=600)
        subprocess.run([pnpm, "-C", str(build_root), "build"], check=True, env=env, timeout=300)
        subprocess.run(
            [pnpm, "-C", str(build_root), "--ignore-workspace", "pack", "--pack-destination", str(staging)],
            check=True, env={**env, "npm_config_ignore_scripts": "true"}, timeout=180,
        )
        packages = list(staging.glob("*.tgz"))
        if len(packages) != 1:
            raise ValueError(f"pnpm pack 应生成一个包，实际为 {len(packages)} 个")
        info = package_info(packages[0])
        if target.ref == "working-tree" and git(target.source, "rev-parse", "HEAD") != commit:
            raise ValueError("构建期间源码 HEAD 改变，请重新运行 make prepare")
        info.update({
            "mode": "git" if target.ref != "working-tree" else "working-tree",
            "commit": commit, "dirty": dirty,
        })
        return publish(target, packages[0], info)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare"])
    parser.parse_args()
    try:
        package = os.environ.get("OFFCKB_PACKAGE")
        entry = os.environ.get("OFFCKB_ENTRY")
        if package and entry:
            raise ValueError("OFFCKB_PACKAGE 和 OFFCKB_ENTRY 不能同时设置")
        if package:
            print(describe_prepared(package_info(Path(package).expanduser().resolve())), flush=True)
        elif entry:
            if not Path(entry).expanduser().is_file():
                raise ValueError(f"OffCKB 调试入口不存在：{entry}")
            print(describe_prepared({"mode": "entry", "entry": entry}), flush=True)
        else:
            prepare(target_settings(), os.environ.get("PNPM_BIN") or shutil.which("pnpm") or "pnpm")
    except (ValueError, OSError, KeyError, tarfile.TarError, subprocess.SubprocessError) as error:
        print(f"OffCKB 准备失败：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
