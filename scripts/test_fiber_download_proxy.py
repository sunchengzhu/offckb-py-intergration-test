"""Local-only transport self-tests; no OffCKB, FNN or official download is run."""

from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.fiber.installation_support import ReleaseProxy, _VERIFIED_RELEASES


# Fixed, local fixture bytes exercise the transport only. They are never offered
# to the packaged product or counted as acceptance coverage.
_ARCHIVE = gzip.compress(bytes(range(256)) * 8, mtime=0)
_SHA256 = hashlib.sha256(_ARCHIVE).hexdigest()


class ReleaseProxyTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="fiber-proxy-self-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.proxies: list[ReleaseProxy] = []
        self.addCleanup(self._close_proxies)
        self.opener = Mock()
        self.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(_ARCHIVE)
        upstream = patch("tests.fiber.installation_support.urllib.request.build_opener", return_value=self.opener)
        upstream.start()
        self.addCleanup(upstream.stop)

    def _close_proxies(self) -> None:
        for proxy in self.proxies:
            proxy.close()
            _VERIFIED_RELEASES.pop((proxy.url, _SHA256), None)

    def proxy(self, mode: str = "valid") -> ReleaseProxy:
        root = self.root / f"proxy-{len(self.proxies)}"
        root.mkdir()
        proxy = ReleaseProxy(root)
        # Only this tool self-test substitutes the fixed local fixture hash.
        # Production acceptance always uses the immutable release pin table.
        proxy.sha256 = _SHA256
        proxy.mode = mode
        self.proxies.append(proxy)
        return proxy

    def request(self, proxy: ReleaseProxy) -> tuple[int, bytes]:
        context = ssl.create_default_context(cafile=str(proxy.ca_cert))
        with socket.create_connection(proxy.server.server_address, timeout=5) as connection:
            # Exercise a real HTTP CONNECT tunnel with hostname-verified TLS.
            connection.sendall(b"CONNECT github.com:443 HTTP/1.1\r\nHost: github.com:443\r\n\r\n")
            status = ReleaseProxy._headers(connection)
            if b"200 Connection Established" not in status:
                return int(status.split(b" ")[1]), b""
            with context.wrap_socket(connection, server_hostname="github.com") as secure:
                secure.sendall(f"GET {proxy.path} HTTP/1.1\r\nHost: github.com\r\nConnection: close\r\n\r\n".encode())
                response = http.client.HTTPResponse(secure)
                response.begin()
                return response.status, response.read()

    def test_verified_https_tunnel_delivers_exact_fixture_bytes(self) -> None:
        proxy = self.proxy()
        status, body = self.request(proxy)
        self.assertEqual((status, body), (200, _ARCHIVE))
        proxy.assert_valid_download()
        self.assertEqual(self.opener.open.call_args.args[0].full_url, proxy.url)
        self.assertEqual(proxy.requests[0]["body_sha256"], _SHA256)

    def test_truncated_response_is_an_incomplete_download(self) -> None:
        proxy = self.proxy("truncated")
        with self.assertRaises(http.client.IncompleteRead) as caught:
            self.request(proxy)
        self.assertEqual(caught.exception.partial, _ARCHIVE[:len(_ARCHIVE) // 2])
        self.assertFalse(proxy.errors)

    def test_corruption_changes_checksum_but_keeps_transport_length(self) -> None:
        proxy = self.proxy("corrupt")
        status, body = self.request(proxy)
        self.assertEqual(status, 200)
        self.assertEqual(len(body), len(_ARCHIVE))
        self.assertNotEqual(hashlib.sha256(body).hexdigest(), _SHA256)
        self.assertFalse(proxy.errors)

    def test_disconnection_and_offline_modes_never_fetch_upstream(self) -> None:
        for mode, status in (("disconnect", 502), ("offline", 503)):
            with self.subTest(mode=mode):
                proxy = self.proxy(mode)
                self.assertEqual(self.request(proxy), (status, b""))
                self.assertEqual(proxy.requests[-1]["mode"], mode)
        self.opener.open.assert_not_called()

    def test_verified_bytes_are_reused_by_independent_proxy_instances(self) -> None:
        first = self.proxy()
        second = self.proxy()
        self.assertNotEqual(first.address, second.address)
        self.assertEqual(self.request(first), (200, _ARCHIVE))
        self.assertEqual(self.request(second), (200, _ARCHIVE))
        self.opener.open.assert_called_once()
        first.assert_valid_download()
        second.assert_valid_download()
        self.assertEqual(len(first.requests), 1)
        self.assertEqual(len(second.requests), 1)

    def test_bad_upstream_bytes_are_never_cached(self) -> None:
        proxy = self.proxy()
        self.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(b"not the pinned fixture")
        with self.assertRaises(http.client.RemoteDisconnected):
            self.request(proxy)
        self.assertNotIn((proxy.url, proxy.sha256), _VERIFIED_RELEASES)
        self.assertTrue(any("SHA-256 mismatch" in error for error in proxy.errors))
        self.opener.open.side_effect = lambda *args, **kwargs: io.BytesIO(_ARCHIVE)
        self.assertEqual(self.request(proxy), (200, _ARCHIVE))
        self.assertEqual(self.opener.open.call_count, 2)


if __name__ == "__main__":
    unittest.main()
