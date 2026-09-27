"""Owned HTTPS proxy for exercising the packaged FNN downloader, without patching it."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import socket
import socketserver
import ssl
import subprocess
import threading
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from tests.harness import OffckbRunner


# Independent acceptance pins for the official v0.9.0 release assets. Keep these
# stable when testing a package: reading the SUT's expected hash is not an oracle.
RELEASE_SHA256 = {
    ("Linux", "x86_64"): "4085453de9a3f7ca0f0aeb7db9e34c0af4d34feb84566be554a5c70557cecbea",
    ("Linux", "aarch64"): "f7b03271d0c8e67cc28dd8ff76876d7740d8ac95a0523764e4846bcef0dcce80",
    ("Darwin", "x86_64"): "088410e1db827d2972df752fd5dbb73c90eed8d985a81bc3b3045835f6798640",
    ("Darwin", "arm64"): "e55720bf167f556ba437b03d0e7c2e3a000c4a9c3d612293be7671fb54168a00",
}


# A single pytest run may create several independent proxy instances. Cache only
# bytes fetched from the fixed official URL and verified against its independent
# pin; every SUT installation still downloads those bytes through its own proxy.
_VERIFIED_RELEASES: dict[tuple[str, str], bytes] = {}
_RELEASE_CACHE_LOCK = threading.Lock()


def release_asset() -> tuple[str, str]:
    actual = platform.system(), platform.machine()
    if actual not in RELEASE_SHA256:
        raise pytest.UsageError(
            f"Fiber download acceptance needs a real Linux/macOS x64/arm64 runner; got {actual}. "
            "Windows needs a separate supported process/terminal runner, not platform spoofing."
        )
    architecture = "aarch64" if actual[1] in {"arm64", "aarch64"} else "x86_64"
    asset = f"fnn_v0.9.0-{architecture}-{actual[0].lower()}-portable.tar.gz"
    return asset, RELEASE_SHA256[actual]


def file_manifest(root: Path) -> dict[str, tuple[str, str]]:
    """Compare stopped state without retaining private file contents in diagnostics."""
    result: dict[str, tuple[str, str]] = {}
    if not root.exists():
        return result
    for path in sorted(root.rglob("*")):
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = ("link", str(path.readlink()))
        elif path.is_file():
            with path.open("rb") as stream:
                result[name] = ("file", hashlib.file_digest(stream, "sha256").hexdigest())
        elif path.is_dir():
            result[name] = ("directory", "")
    return result


def _make_certificate(directory: Path) -> tuple[Path, Path, Path]:
    executable = shutil.which("openssl")
    if executable is None:
        raise pytest.UsageError("Fiber controlled-download tests require openssl to issue a test-only TLS certificate.")
    directory.mkdir(mode=0o700)
    ca_key, ca_cert = directory / "ca.key", directory / "ca.pem"
    key, certificate = directory / "server.key", directory / "server.pem"
    request = directory / "server.csr"
    ca_config = directory / "ca.cnf"
    ca_config.write_text(
        "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ca\n"
        "[dn]\nCN=OffCKB isolated acceptance CA\n"
        "[ca]\nbasicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,cRLSign\n"
    )
    extensions = directory / "server.ext"
    extensions.write_text(
        "basicConstraints=critical,CA:FALSE\n"
        "keyUsage=critical,digitalSignature,keyEncipherment\n"
        "extendedKeyUsage=serverAuth\nsubjectAltName=DNS:github.com\n"
    )
    commands = [
        ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-sha256",
         "-config", str(ca_config), "-keyout", str(ca_key), "-out", str(ca_cert)],
        ["req", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", "/CN=github.com",
         "-keyout", str(key), "-out", str(request)],
        ["x509", "-req", "-in", str(request), "-CA", str(ca_cert), "-CAkey", str(ca_key),
         "-CAcreateserial", "-days", "1", "-sha256", "-extfile", str(extensions), "-out", str(certificate)],
    ]
    for arguments in commands:
        result = subprocess.run([executable, *arguments], capture_output=True, text=True, timeout=30, check=False)
        if result.returncode:
            raise pytest.UsageError(f"Could not create the isolated proxy certificate: {result.stderr}")
    for private_file in (ca_key, key):
        private_file.chmod(0o600)
    return ca_cert, certificate, key


class ReleaseProxy:
    """Only intercept the one official FNN release URL; deny every other target.

    Upstream bytes are fetched over normally verified HTTPS and checked against
    an independent pin. Fault injection changes the downstream transport/body,
    never the installed CLI, hash oracle or upstream TLS verification.
    """

    def __init__(self, root: Path):
        self.root = root
        self.asset, self.sha256 = release_asset()
        self.path = f"/nervosnetwork/fiber/releases/download/v0.9.0/{self.asset}"
        self.url = "https://github.com" + self.path
        self.mode = "valid"
        self.requests: list[dict] = []
        self.errors: list[str] = []
        self._archive: bytes | None = None
        self._sockets: set[socket.socket] = set()
        self.ca_cert, certificate, key = _make_certificate(root / "proxy-tls")
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(certificate, key)
        proxy = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                proxy._serve(self.request)

        class Server(socketserver.ThreadingTCPServer):
            daemon_threads = True
            allow_reuse_address = False

            def handle_error(self, request, client_address):
                # Request exceptions are captured by _serve, never printed with
                # certificate locations or unexpected request contents.
                proxy.errors.append("unhandled controlled proxy request failure")

        try:
            self.server = Server(("127.0.0.1", 0), Handler)
        except BaseException:
            shutil.rmtree(root / "proxy-tls")
            raise
        self.address = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _official_archive(self) -> bytes:
        cache_key = self.url, self.sha256
        with _RELEASE_CACHE_LOCK:
            archive = _VERIFIED_RELEASES.get(cache_key)
            if archive is None:
                # Never inherit developer proxy settings or the test CA upstream.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                request = urllib.request.Request(self.url, headers={"User-Agent": "offckb-acceptance"})
                with opener.open(request, timeout=120) as response:
                    archive = response.read(200 * 1024 * 1024 + 1)
                assert len(archive) <= 200 * 1024 * 1024, "unexpected oversized FNN release"
                digest = hashlib.sha256(archive).hexdigest()
                assert digest == self.sha256, f"official FNN release SHA-256 mismatch: {digest}"
                _VERIFIED_RELEASES[cache_key] = archive
            self._archive = archive
            return archive

    @staticmethod
    def _headers(connection: socket.socket) -> bytes:
        result = bytearray()
        while not result.endswith(b"\r\n\r\n"):
            value = connection.recv(1)
            if not value:
                raise ConnectionError("connection closed before request headers")
            result.extend(value)
            if len(result) > 16384:
                raise ValueError("oversized proxy request")
        return bytes(result)

    def _serve(self, connection: socket.socket) -> None:
        tracked = connection
        self._sockets.add(tracked)
        connection.settimeout(150)
        try:
            first = self._headers(connection).split(b"\r\n", 1)[0]
            if first not in {b"CONNECT github.com:443 HTTP/1.1", b"CONNECT github.com:443 HTTP/1.0"}:
                connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                self.errors.append("proxy received a target outside the fixed official release host")
                return
            if self.mode == "disconnect":
                self.requests.append({"mode": self.mode, "url": self.url, "sent_bytes": 0})
                connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            if self.mode == "offline":
                self.requests.append({"mode": self.mode, "url": self.url, "sent_bytes": 0})
                connection.sendall(b"HTTP/1.1 503 Offline\r\nContent-Length: 0\r\n\r\n")
                return
            connection.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            connection = self._tls.wrap_socket(connection, server_side=True)
            self._sockets.add(connection)
            first = self._headers(connection).split(b"\r\n", 1)[0]
            if first != f"GET {self.path} HTTP/1.1".encode():
                connection.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                self.errors.append("proxy received an unexpected release path or method")
                return
            official = self._official_archive()
            body = official
            if self.mode == "corrupt":
                # Keep a valid gzip stream's size but change one byte. The
                # product must reject the checksum before extraction starts.
                body = official[:32] + bytes([official[32] ^ 1]) + official[33:]
            elif self.mode == "truncated":
                body = official[:len(official) // 2]
            elif self.mode != "valid":
                raise ValueError(f"unsupported controlled proxy mode: {self.mode}")
            record = {"mode": self.mode, "url": self.url, "upstream_sha256": self.sha256,
                      "body_sha256": hashlib.sha256(body).hexdigest(), "sent_bytes": len(body)}
            self.requests.append(record)
            connection.sendall(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/gzip\r\nContent-Length: {len(official)}\r\nConnection: close\r\n\r\n".encode()
            )
            connection.sendall(body)
        except Exception as error:
            self.errors.append(f"{type(error).__name__}: {error}")
        finally:
            self._sockets.discard(tracked)
            self._sockets.discard(connection)
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def assert_valid_download(self, *, after: int = 0) -> None:
        assert not self.errors, self.errors
        downloads = [entry for entry in self.requests[after:] if entry["mode"] == "valid"]
        assert downloads, "OffCKB did not request the real release through its configured proxy"
        assert all(item["body_sha256"] == self.sha256 and item["url"] == self.url for item in downloads)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        for connection in tuple(self._sockets):
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        (self.root / "download-requests.json").write_text(json.dumps(self.requests, indent=2) + "\n")
        # The authority was trusted by one child's environment only. Do not
        # retain its signing keys even when a failed runtime is kept.
        shutil.rmtree(self.root / "proxy-tls")


@contextmanager
def controlled_downloads(runner: OffckbRunner, root: Path) -> Iterator[ReleaseProxy]:
    proxy = ReleaseProxy(root)
    previous_ca = runner.env.get("NODE_EXTRA_CA_CERTS")
    runner.env["NODE_EXTRA_CA_CERTS"] = str(proxy.ca_cert)
    try:
        runner.run("config", "set", "proxy", proxy.address)
        yield proxy
    finally:
        if previous_ca is None:
            runner.env.pop("NODE_EXTRA_CA_CERTS", None)
        else:
            runner.env["NODE_EXTRA_CA_CERTS"] = previous_ca
        proxy.close()
