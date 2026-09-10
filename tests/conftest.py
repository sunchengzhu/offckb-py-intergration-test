from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

import pytest

from .harness import (
    Account,
    DEVNET_PORTS,
    DIRECT_RPC_URL,
    PROXY_RPC_URL,
    DevnetManager,
    OffckbRunner,
    RpcClient,
    is_port_open,
    scrub_secret_artifacts,
)


_RUNTIME_ENV_ALLOWLIST = (
    "PATH",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TERM",
    "COLORTERM",
    "SHELL",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
)


def _safe_runtime_env() -> dict[str, str]:
    """Return only non-secret host settings required to execute local tools."""

    return {name: os.environ[name] for name in _RUNTIME_ENV_ALLOWLIST if name in os.environ}


def _safe_host_tool_env() -> dict[str, str]:
    """Add path-only host settings needed while resolving pnpm's local caches."""

    env = _safe_runtime_env()
    for name in ("HOME", "USERPROFILE", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "PNPM_HOME"):
        value = os.environ.get(name)
        if value:
            env[name] = value
    return env


@dataclass(frozen=True)
class OffckbArtifact:
    command_entry: Path
    daemon_entry: Path
    source: str
    package_path: Path | None = None
    package_sha256: str | None = None
    product_root: Path | None = None
    install_prefix: Path | None = None


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("offckb integration")
    group.addoption(
        "--offckb-source",
        action="store",
        default=os.environ.get("OFFCKB_SOURCE"),
        help="OffCKB source checkout to pack; defaults to source/offckb or the sibling ../offckb checkout",
    )
    group.addoption(
        "--offckb-entry",
        action="store",
        default=os.environ.get("OFFCKB_ENTRY"),
        help="Explicit build/index.js or standard node_modules/.bin/offckb entry; bypass package installation",
    )
    group.addoption(
        "--offckb-package",
        action="store",
        default=os.environ.get("OFFCKB_PACKAGE"),
        help="Existing @offckb/cli package tarball to install in the isolated runtime",
    )
    group.addoption(
        "--pnpm-bin",
        action="store",
        default=os.environ.get("PNPM_BIN") or shutil.which("pnpm"),
        help="pnpm executable used to pack and install the tested package",
    )
    group.addoption(
        "--pnpm-store-dir",
        action="store",
        default=os.environ.get("PNPM_STORE_DIR"),
        help="Existing pnpm store used by the prefer-offline package installation",
    )
    group.addoption(
        "--pnpm-cache-dir",
        action="store",
        default=os.environ.get("PNPM_CACHE_DIR"),
        help="Existing pnpm cache directory used while installing the tested package",
    )
    group.addoption(
        "--node-bin",
        action="store",
        default=os.environ.get("NODE_BIN") or shutil.which("node"),
        help="Node.js executable used for a JavaScript offckb entry",
    )
    group.addoption(
        "--ckb-bin",
        action="store",
        default=os.environ.get("CKB_BIN"),
        help="Local CKB binary; core tests never download one",
    )
    group.addoption("--startup-timeout", action="store", type=float, default=120.0)
    group.addoption("--tx-timeout", action="store", type=float, default=180.0)
    group.addoption(
        "--keep-runtime",
        action="store_true",
        default=False,
        help="Keep the isolated runtime even when all tests pass",
    )


def pytest_configure(config: pytest.Config) -> None:
    if os.name == "nt":
        raise pytest.UsageError("the first offckb core integration runner supports Linux and macOS only")
    if os.environ.get("PYTEST_XDIST_WORKER") or getattr(config.option, "numprocesses", None):
        raise pytest.UsageError("offckb integration tests own fixed devnet ports and cannot run under pytest-xdist")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    outcome = yield
    report = outcome.get_result()
    if not report.failed or report.when not in {"setup", "call"}:
        return
    run_root = item.funcargs.get("run_root")
    if not isinstance(run_root, Path):
        return
    rpc = item.funcargs.get("rpc")
    proxy_rpc = item.funcargs.get("proxy_rpc")
    manager = item.funcargs.get("devnet_manager")
    snapshot: dict[str, object] = {
        "nodeid": item.nodeid,
        "phase": report.when,
        "ports": {str(port): is_port_open(port) for port in DEVNET_PORTS},
    }
    if isinstance(manager, DevnetManager):
        snapshot["daemonPid"] = manager.pid
        snapshot["pidFile"] = str(manager.pid_file) if manager.pid_file else None
        snapshot["configPath"] = str(manager.config_path) if manager.config_path else None
    for label, client in (("direct", rpc), ("proxy", proxy_rpc)):
        if not isinstance(client, RpcClient):
            continue
        values: dict[str, object] = {}
        for method in ("local_node_info", "get_tip_block_number", "get_indexer_tip"):
            try:
                values[method] = client.call(method)
            except BaseException as error:
                values[method] = {"error": str(error)}
        snapshot[label] = values
    safe_nodeid = re.sub(r"[^A-Za-z0-9_.-]+", "_", item.nodeid)
    (run_root / f"failure-{safe_nodeid}.json").write_text(
        json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _command_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _run_artifact_command(
    name: str,
    argv: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    records_dir: Path,
    timeout_s: float = 180.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    records_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            cwd=cwd,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            check=False,
        )
        timed_out = False
    except subprocess.TimeoutExpired as error:
        completed = subprocess.CompletedProcess(
            list(argv),
            returncode=124,
            stdout=_command_output(error.stdout),
            stderr=_command_output(error.stderr),
        )
        timed_out = True

    duration_s = time.monotonic() - started
    record_path = records_dir / f"artifact-{name}.json"
    record_path.write_text(
        json.dumps(
            {
                "argv": list(argv),
                "cwd": str(cwd),
                "durationSeconds": round(duration_s, 3),
                "returncode": completed.returncode,
                "timedOut": timed_out,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    (records_dir / f"artifact-{name}.stdout.log").write_text(completed.stdout, encoding="utf-8")
    (records_dir / f"artifact-{name}.stderr.log").write_text(completed.stderr, encoding="utf-8")

    if check and completed.returncode != 0:
        reason = "timed out" if timed_out else f"failed with exit code {completed.returncode}"
        raise pytest.UsageError(
            f"offckb artifact {name} {reason}; diagnostic: {record_path}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed


def _resolve_pnpm(pytestconfig: pytest.Config) -> Path:
    configured = pytestconfig.getoption("--pnpm-bin")
    if not configured:
        raise pytest.UsageError("pnpm was not found; install it or pass --pnpm-bin")
    executable = Path(configured).expanduser().resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise pytest.UsageError(f"pnpm executable is invalid: {executable}")
    return executable


def _pack_offckb_source(
    product_root: Path,
    pnpm: Path,
    *,
    env: dict[str, str],
    run_root: Path,
) -> Path:
    package_json = product_root / "package.json"
    build_entry = product_root / "build" / "index.js"
    if not package_json.is_file():
        raise pytest.UsageError(
            f"offckb source checkout does not exist at {product_root}; pass --offckb-source, populate source/offckb, "
            "pass --offckb-package, or pass --offckb-entry"
        )
    if not build_entry.is_file():
        raise pytest.UsageError(
            f"offckb build entry does not exist at {build_entry}; run pnpm build in {product_root} first"
        )

    package_dir = run_root / "packages"
    package_dir.mkdir(parents=True, exist_ok=True)
    _run_artifact_command(
        "pack",
        (str(pnpm), "--ignore-workspace", "pack", "--pack-destination", str(package_dir)),
        cwd=product_root,
        env=env,
        records_dir=run_root / "commands",
    )
    packages = sorted(package_dir.glob("*.tgz"))
    if len(packages) != 1:
        raise pytest.UsageError(f"pnpm pack produced {len(packages)} tarballs in {package_dir}: {packages}")
    return packages[0].resolve()


def _resolve_pnpm_store(
    pytestconfig: pytest.Config,
    pnpm: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    run_root: Path,
) -> Path:
    configured = pytestconfig.getoption("--pnpm-store-dir")
    if configured:
        store = Path(configured).expanduser().resolve()
    else:
        completed = _run_artifact_command(
            "store-path",
            (str(pnpm), "store", "path"),
            cwd=cwd,
            env=env,
            records_dir=run_root / "commands",
        )
        store = Path(completed.stdout.strip()).expanduser().resolve()
    if not store.is_dir():
        raise pytest.UsageError(
            f"pnpm store does not exist at {store}; run pnpm install in {cwd} first "
            "or pass --pnpm-store-dir"
        )
    return store


def _resolve_pnpm_cache(pytestconfig: pytest.Config) -> Path:
    configured = pytestconfig.getoption("--pnpm-cache-dir")
    if configured:
        cache = Path(configured).expanduser().resolve()
    else:
        xdg_cache = os.environ.get("XDG_CACHE_HOME")
        if xdg_cache:
            cache = (Path(xdg_cache).expanduser() / "pnpm").resolve()
        elif sys.platform == "darwin":
            cache = (Path.home() / "Library" / "Caches" / "pnpm").resolve()
        else:
            cache = (Path.home() / ".cache" / "pnpm").resolve()
    metadata_dirs = tuple(cache.glob("metadata*")) if cache.is_dir() else ()
    if cache.name != "pnpm" or not any(path.is_dir() for path in metadata_dirs):
        raise pytest.UsageError(
            f"pnpm metadata cache does not exist under {cache}; run pnpm install first "
            "or pass --pnpm-cache-dir pointing to the pnpm cache directory"
        )
    return cache


def _install_offckb_package(
    package_path: Path,
    pnpm: Path,
    store_dir: Path,
    cache_dir: Path,
    *,
    env: dict[str, str],
    run_root: Path,
) -> tuple[Path, Path, Path]:
    prefix = run_root / "npm-prefix"
    prefix.mkdir(parents=True, exist_ok=True)
    (prefix / "package.json").write_text(
        json.dumps(
            {"name": "offckb-integration-runtime", "version": "0.0.0", "private": True},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    install_env = dict(env)
    # pnpm always appends its own `pnpm` directory to XDG_CACHE_HOME. Only this
    # package-manager process may read the host metadata cache; the installed
    # offckb process continues to receive the isolated HOME/XDG environment.
    install_env["XDG_CACHE_HOME"] = str(cache_dir.parent)
    _run_artifact_command(
        "install",
        (
            str(pnpm),
            "add",
            "--prefer-offline",
            "--ignore-scripts",
            "--save-exact",
            "--dir",
            str(prefix),
            "--store-dir",
            str(store_dir),
            str(package_path),
        ),
        cwd=prefix,
        env=install_env,
        records_dir=run_root / "commands",
    )

    installed_root = prefix / "node_modules" / "@offckb" / "cli"
    installed_bin = prefix / "node_modules" / ".bin" / "offckb"
    daemon_entry = installed_root / "build" / "index.js"
    if not installed_bin.is_file() or not os.access(installed_bin, os.X_OK):
        raise pytest.UsageError(f"installed offckb executable is missing or not executable: {installed_bin}")
    if not daemon_entry.is_file():
        raise pytest.UsageError(f"installed offckb package entry is missing: {daemon_entry}")
    return installed_bin, daemon_entry, prefix


def _record_artifact_metadata(run_root: Path, metadata: dict[str, object]) -> None:
    commands_dir = run_root / "commands"
    commands_dir.mkdir(parents=True, exist_ok=True)
    (commands_dir / "artifact.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_path = run_root / "manifest.json"
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["offckbArtifact"] = metadata
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture(scope="session")
def integration_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def project_root(integration_root: Path, pytestconfig: pytest.Config) -> Path:
    def is_offckb_checkout(path: Path) -> bool:
        try:
            manifest = json.loads((path / "package.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        return isinstance(manifest, dict) and manifest.get("name") == "@offckb/cli"

    configured = pytestconfig.getoption("--offckb-source")
    if configured:
        checkout = Path(configured).expanduser().resolve()
        if not is_offckb_checkout(checkout):
            raise pytest.UsageError(
                f"--offckb-source must point to an @offckb/cli checkout: {checkout}"
            )
        return checkout

    declared_checkout = integration_root / "source" / "offckb"
    sibling_checkout = integration_root.parent / "offckb"
    for candidate in (declared_checkout, sibling_checkout):
        if is_offckb_checkout(candidate):
            return candidate.resolve()
    # Package/entry-only runs need no checkout. Source-mode validation happens
    # in _pack_offckb_source so these independent artifact inputs remain usable.
    return declared_checkout.resolve()


@pytest.fixture(scope="session")
def run_root(
    tmp_path_factory: pytest.TempPathFactory, pytestconfig: pytest.Config, request: pytest.FixtureRequest
) -> Iterator[Path]:
    root = tmp_path_factory.mktemp("offckb-acceptance")
    for name in ("home", "workspace", "commands", "secrets", "tmp"):
        (root / name).mkdir(parents=True, exist_ok=True)
    yield root
    keep = pytestconfig.getoption("--keep-runtime") or request.session.testsfailed > 0
    if keep:
        scrub_secret_artifacts(root)
        terminal = pytestconfig.pluginmanager.get_plugin("terminalreporter")
        if terminal is not None:
            terminal.write_line(f"offckb isolated runtime kept at: {root}")
    else:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(scope="session")
def fixed_port_lease(run_root: Path) -> Iterator[None]:
    lock_path = Path(os.environ.get("TMPDIR", "/tmp")) / "offckb-integration-tests.fixed-ports.lock"
    handle = lock_path.open("a+")
    try:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise pytest.UsageError(f"another offckb integration run owns {lock_path}") from error
        occupied = [port for port in DEVNET_PORTS if is_port_open(port)]
        if occupied:
            raise pytest.UsageError(
                f"devnet ports are already occupied: {occupied}; stop the owning service before running acceptance tests"
            )
        yield
    finally:
        handle.close()


@pytest.fixture(scope="session")
def isolated_env(run_root: Path, ckb_bin: Path) -> dict[str, str]:
    home = run_root / "home"
    env = _safe_runtime_env()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "XDG_STATE_HOME": str(home / ".local" / "state"),
            "TMPDIR": str(run_root / "tmp"),
            "NO_COLOR": "1",
            "CI": "1",
        }
    )
    if os.name == "nt":
        env["APPDATA"] = str(home / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    env.pop("OFFCKB_PRIVATE_KEY", None)

    version_result = subprocess.run(
        [str(ckb_bin), "--version"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )
    match = re.search(r"\b(\d+\.\d+\.\d+(?:-rc\d+)?)\b", version_result.stdout)
    if version_result.returncode != 0 or match is None:
        raise pytest.UsageError(f"cannot determine CKB version from {ckb_bin}: {version_result.stdout}")
    ckb_version = match.group(1)
    managed_root = run_root / "managed-bins"
    managed_ckb = managed_root / ckb_version / ("ckb.exe" if os.name == "nt" else "ckb")
    managed_ckb.parent.mkdir(parents=True, exist_ok=True)
    copied_binary = False
    try:
        managed_ckb.symlink_to(ckb_bin)
    except OSError:
        shutil.copy2(ckb_bin, managed_ckb)
        copied_binary = True
    if copied_binary and os.name != "nt":
        managed_ckb.chmod(managed_ckb.stat().st_mode | stat.S_IXUSR)

    app_name = "offckb-nodejs"
    if sys.platform == "darwin":
        settings_path = home / "Library" / "Preferences" / app_name / "settings.json"
    elif os.name == "nt":
        settings_path = home / "AppData" / "Roaming" / app_name / "Config" / "settings.json"
    else:
        settings_path = Path(env["XDG_CONFIG_HOME"]) / app_name / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(
        json.dumps(
            {
                "bins": {
                    "rootFolder": str(managed_root),
                    "defaultCKBVersion": ckb_version,
                    "downloadPath": str(run_root / "downloads"),
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(ckb_bin.read_bytes()).hexdigest()
    (run_root / "manifest.json").write_text(
        json.dumps(
            {
                "ckbBinary": str(ckb_bin),
                "ckbVersion": ckb_version,
                "ckbSha256": digest,
                "settingsFile": str(settings_path),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return env


@pytest.fixture(scope="session")
def offckb_artifact(
    pytestconfig: pytest.Config,
    project_root: Path,
    isolated_env: dict[str, str],
    run_root: Path,
) -> OffckbArtifact:
    configured_entry = pytestconfig.getoption("--offckb-entry")
    configured_package = pytestconfig.getoption("--offckb-package")
    if configured_entry and configured_package:
        raise pytest.UsageError("--offckb-entry and --offckb-package are mutually exclusive")

    if configured_entry:
        command_entry = Path(configured_entry).expanduser().absolute()
        if not command_entry.is_file():
            raise pytest.UsageError(f"offckb entry does not exist: {command_entry}")
        resolved_entry = command_entry.resolve()
        if resolved_entry.suffix.lower() in {".js", ".cjs", ".mjs"}:
            daemon_entry = resolved_entry
        elif command_entry.parent.name == ".bin":
            package_entry = command_entry.parent.parent / "@offckb" / "cli" / "build" / "index.js"
            if not package_entry.is_file():
                raise pytest.UsageError(
                    f"cannot resolve the package JavaScript entry next to {command_entry}: {package_entry}"
                )
            daemon_entry = package_entry.resolve()
        else:
            raise pytest.UsageError(
                "--offckb-entry must point to build/index.js (or .cjs/.mjs) or a standard "
                "node_modules/.bin/offckb entry; use --offckb-package for release tarballs"
            )
        artifact = OffckbArtifact(
            command_entry=command_entry,
            daemon_entry=daemon_entry,
            source=f"explicit-entry:{command_entry}",
        )
    else:
        pnpm = _resolve_pnpm(pytestconfig)
        artifact_env = dict(isolated_env)
        artifact_env["npm_config_ignore_scripts"] = "true"
        if configured_package:
            package_path = Path(configured_package).expanduser().resolve()
            if not package_path.is_file():
                raise pytest.UsageError(f"offckb package does not exist: {package_path}")
            source = f"explicit-package:{package_path}"
        else:
            package_path = _pack_offckb_source(
                project_root,
                pnpm,
                env=artifact_env,
                run_root=run_root,
            )
            source = f"packed-source:{project_root}"

        package_sha256 = _sha256_file(package_path)
        _record_artifact_metadata(
            run_root,
            {
                "commandEntry": None,
                "daemonEntry": None,
                "installPrefix": str(run_root / "npm-prefix"),
                "packageInstallMode": "prefer-offline",
                "lifecycleScriptsDisabled": True,
                "package": str(package_path),
                "packageSha256": package_sha256,
                "productRoot": str(project_root) if (project_root / "package.json").is_file() else None,
                "source": source,
            },
        )

        store_cwd = project_root if (project_root / "package.json").is_file() else package_path.parent
        store_dir = _resolve_pnpm_store(
            pytestconfig,
            pnpm,
            cwd=store_cwd,
            # Resolve the host store before applying the SUT's isolated
            # HOME/XDG environment. Only pnpm may read this package cache.
            env=_safe_host_tool_env(),
            run_root=run_root,
        )
        cache_dir = _resolve_pnpm_cache(pytestconfig)
        installed_bin, daemon_entry, prefix = _install_offckb_package(
            package_path,
            pnpm,
            store_dir,
            cache_dir,
            env=artifact_env,
            run_root=run_root,
        )
        artifact = OffckbArtifact(
            command_entry=installed_bin,
            daemon_entry=daemon_entry,
            source=source,
            package_path=package_path,
            package_sha256=package_sha256,
            product_root=project_root if (project_root / "package.json").is_file() else None,
            install_prefix=prefix,
        )

    artifact_record = {
        "commandEntry": str(artifact.command_entry),
        "daemonEntry": str(artifact.daemon_entry),
        "installPrefix": str(artifact.install_prefix) if artifact.install_prefix else None,
        "packageInstallMode": "prefer-offline" if artifact.package_path is not None else None,
        "lifecycleScriptsDisabled": artifact.package_path is not None,
        "package": str(artifact.package_path) if artifact.package_path else None,
        "packageSha256": artifact.package_sha256,
        "productRoot": str(artifact.product_root) if artifact.product_root else None,
        "source": artifact.source,
    }
    _record_artifact_metadata(run_root, artifact_record)
    return artifact


@pytest.fixture(scope="session")
def offckb_entry(offckb_artifact: OffckbArtifact) -> Path:
    return offckb_artifact.daemon_entry


@pytest.fixture(scope="session")
def ckb_bin(pytestconfig: pytest.Config, project_root: Path) -> Path:
    configured = pytestconfig.getoption("--ckb-bin")
    candidates = [Path(configured).expanduser()] if configured else [project_root.parent / "ckb" / "target" / "release" / "ckb"]
    binary = candidates[0].resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise pytest.UsageError(
            f"executable CKB binary not found: {binary}. Pass an explicit local binary with --ckb-bin."
        )
    return binary


@pytest.fixture(scope="session")
def offckb_command(pytestconfig: pytest.Config, offckb_artifact: OffckbArtifact) -> tuple[str, ...]:
    command_entry = offckb_artifact.command_entry
    if command_entry.suffix.lower() in {".js", ".cjs", ".mjs"}:
        node_value = pytestconfig.getoption("--node-bin")
        if not node_value:
            raise pytest.UsageError("Node.js was not found; pass --node-bin")
        node = Path(node_value).expanduser().resolve()
        if not node.is_file() or not os.access(node, os.X_OK):
            raise pytest.UsageError(f"Node.js executable is invalid: {node}")
        return (str(node), str(command_entry))
    if not os.access(command_entry, os.X_OK):
        raise pytest.UsageError(f"offckb entry is not executable: {command_entry}")
    return (str(command_entry),)


@pytest.fixture(scope="session")
def offckb(
    offckb_command: tuple[str, ...],
    offckb_artifact: OffckbArtifact,
    offckb_entry: Path,
    isolated_env: dict[str, str],
    run_root: Path,
) -> OffckbRunner:
    env = dict(isolated_env)
    env["OFFCKB_CLI_PATH"] = str(offckb_entry)
    version_result = subprocess.run(
        [*offckb_command, "--version"],
        cwd=run_root / "workspace",
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if version_result.returncode != 0:
        raise pytest.UsageError(
            f"cannot execute offckb artifact {offckb_artifact.command_entry}: {version_result.stderr}"
        )
    manifest_path = run_root / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update(
        {
            "offckbArtifactSource": offckb_artifact.source,
            "offckbCommand": list(offckb_command),
            "offckbCommandEntry": str(offckb_artifact.command_entry),
            "offckbCommandEntrySha256": _sha256_file(offckb_artifact.command_entry),
            "offckbEntry": str(offckb_entry),
            "offckbVersion": version_result.stdout.strip(),
            "offckbSha256": _sha256_file(offckb_entry),
            "offckbInstallPrefix": str(offckb_artifact.install_prefix) if offckb_artifact.install_prefix else None,
            "offckbPackage": str(offckb_artifact.package_path) if offckb_artifact.package_path else None,
            "offckbPackageSha256": offckb_artifact.package_sha256,
            "offckbProductRoot": str(offckb_artifact.product_root) if offckb_artifact.product_root else None,
            "nodeBinary": offckb_command[0] if len(offckb_command) > 1 else None,
        }
    )
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return OffckbRunner(
        offckb_command,
        env=env,
        cwd=run_root / "workspace",
        records_dir=run_root / "commands",
    )


@pytest.fixture(scope="session")
def rpc(pytestconfig: pytest.Config) -> RpcClient:
    return RpcClient(DIRECT_RPC_URL, tx_timeout_s=pytestconfig.getoption("--tx-timeout"))


@pytest.fixture(scope="session")
def proxy_rpc(pytestconfig: pytest.Config) -> RpcClient:
    return RpcClient(PROXY_RPC_URL, tx_timeout_s=pytestconfig.getoption("--tx-timeout"))


@pytest.fixture(scope="session")
def devnet_manager(
    fixed_port_lease: None,
    offckb: OffckbRunner,
    rpc: RpcClient,
    proxy_rpc: RpcClient,
    ckb_bin: Path,
    pytestconfig: pytest.Config,
) -> Iterator[DevnetManager]:
    manager = DevnetManager(
        offckb,
        rpc,
        proxy_rpc,
        ckb_bin,
        startup_timeout_s=pytestconfig.getoption("--startup-timeout"),
    )
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def devnet(devnet_manager: DevnetManager) -> DevnetManager:
    return devnet_manager.ensure_running()


@pytest.fixture
def fresh_devnet(devnet_manager: DevnetManager) -> DevnetManager:
    return devnet_manager.reset()


@pytest.fixture(scope="session")
def accounts(offckb: OffckbRunner) -> list[Account]:
    result = offckb.run("accounts", "--show-private-keys", record=False, sensitive_output=True)
    assert result.json is not None
    raw_accounts = result.json.get("accounts")
    if isinstance(raw_accounts, list):
        for value in raw_accounts:
            if isinstance(value, dict) and isinstance(value.get("privkey"), str):
                offckb.register_secret(value["privkey"])
    if not isinstance(raw_accounts, list) or len(raw_accounts) != 20:
        count = len(raw_accounts) if isinstance(raw_accounts, list) else None
        raise AssertionError(f"Expected 20 built-in accounts, got type={type(raw_accounts).__name__}, count={count}")
    resolved: list[Account] = []
    for value in raw_accounts:
        resolved.append(
            Account(
                index=int(value["index"]),
                address=str(value["address"]),
                lock_script=dict(value["lockScript"]),
                private_key=str(value["privkey"]),
                pubkey=str(value["pubkey"]),
            )
        )
    return resolved


@pytest.fixture(scope="session")
def private_key_file(run_root: Path):
    cache: dict[int, Path] = {}

    def create(account: Account) -> Path:
        existing = cache.get(account.index)
        if existing is not None:
            return existing
        destination = run_root / "secrets" / f"account-{account.index}.key"
        destination.write_text(account.private_key + "\n", encoding="utf-8")
        destination.chmod(stat.S_IRUSR | stat.S_IWUSR)
        cache[account.index] = destination
        return destination

    return create
