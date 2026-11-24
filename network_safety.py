"""Network-boundary helpers that only connect to public HTTP(S) targets."""

from __future__ import annotations

import socket
import sys
import os
from dataclasses import dataclass
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3 import ProxyManager
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import ConnectTimeoutError, NewConnectionError


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_DEFAULT_MAX_REDIRECTS = 5

_ALLOW_PRIVATE_AI_ENDPOINTS = os.environ.get(
    "AI_ALLOW_PRIVATE_ENDPOINTS", ""
).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class _TrustedProxy:
    """An administrator-configured egress proxy trusted to reach public targets."""

    url: str
    resolved_addresses: tuple[tuple, ...]


class UnsafeUrlError(ValueError):
    """Raised when a URL is not safe for a server-side request."""


def _unsafe(message: str = "URL target is not allowed") -> UnsafeUrlError:
    return UnsafeUrlError(message)


def _resolve_public_http_url(url: str) -> tuple[str, tuple[tuple, ...]]:
    """Return a safe URL and the exact public socket addresses it resolved to."""
    return _resolve_http_url(url, allow_private=False)


def _resolve_http_url(
    url: str, *, allow_private: bool = False
) -> tuple[str, tuple[tuple, ...]]:
    """Return a safe URL and the exact socket addresses it resolved to.

    When ``allow_private`` is False, only global (public) resolved addresses
    are retained and at least one must remain. When True, loopback/private
    addresses are permitted so locally self-hosted AI gateways (Ollama,
    one-api) remain usable behind an opt-in.
    """
    if not isinstance(url, str):
        raise _unsafe("A valid public HTTP(S) URL is required")

    candidate = url.strip()
    if not candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        raise _unsafe("A valid public HTTP(S) URL is required")

    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        raise _unsafe("A valid public HTTP(S) URL is required") from None

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise _unsafe("A valid public HTTP(S) URL is required")

    def _addr_ok(addr: "ip_address") -> bool:
        if addr.is_multicast:
            return False
        if allow_private:
            return True
        return addr.is_global

    hostname = parsed.hostname
    if "%" in hostname:
        raise _unsafe()

    try:
        literal_address = ip_address(hostname)
    except ValueError:
        literal_address = None

    if literal_address is not None:
        if not _addr_ok(literal_address):
            raise _unsafe()
        family = socket.AF_INET6 if literal_address.version == 6 else socket.AF_INET
        sockaddr = (
            (str(literal_address), port or (443 if parsed.scheme.lower() == "https" else 80), 0, 0)
            if family == socket.AF_INET6
            else (str(literal_address), port or (443 if parsed.scheme.lower() == "https" else 80))
        )
        return candidate, (
            (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr),
        )

    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
        target_port = port or (443 if parsed.scheme.lower() == "https" else 80)
        addresses = socket.getaddrinfo(
            ascii_hostname,
            target_port,
            type=socket.SOCK_STREAM,
        )
    except (OSError, UnicodeError, ValueError):
        raise _unsafe("URL target could not be safely resolved") from None

    if not addresses:
        raise _unsafe("URL target could not be safely resolved")

    normalized_addresses = []
    try:
        for family, socktype, proto, canonname, sockaddr in addresses:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                raise ValueError
            resolved = ip_address(sockaddr[0])
            if not _addr_ok(resolved):
                continue
            normalized_sockaddr = (
                (str(resolved), target_port, sockaddr[2], sockaddr[3])
                if family == socket.AF_INET6
                else (str(resolved), target_port)
            )
            normalized_addresses.append(
                (
                    family,
                    socktype or socket.SOCK_STREAM,
                    proto or socket.IPPROTO_TCP,
                    canonname,
                    normalized_sockaddr,
                )
            )
    except (IndexError, TypeError, ValueError):
        raise _unsafe("URL target could not be safely resolved") from None

    if not normalized_addresses:
        raise _unsafe("URL target could not be safely resolved")
    return candidate, tuple(normalized_addresses)


def assert_public_http_url(url: str) -> str:
    """Validate that *url* is HTTP(S) and every resolved address is public."""
    candidate, _ = _resolve_public_http_url(url)
    return candidate


def assert_ai_endpoint_url(url: str) -> str:
    """Validate an AI endpoint URL.

    Defaults to public-only (SSRF guard). When ``AI_ALLOW_PRIVATE_ENDPOINTS``
    is set to a truthy value, loopback/private addresses are permitted so
    self-hosted LLM gateways (Ollama, one-api) remain usable behind an opt-in.
    """
    candidate, _ = _resolve_http_url(url, allow_private=_ALLOW_PRIVATE_AI_ENDPOINTS)
    return candidate


def _resolve_trusted_proxy_url(url: str) -> tuple[str, tuple[tuple, ...]]:
    """Validate and bind an administrator-configured HTTP(S) proxy endpoint.

    Unlike requested destinations, this fixed deployment egress endpoint may
    be loopback/private so Docker and host proxy deployments remain usable.
    """
    if not isinstance(url, str):
        raise _unsafe("Configured proxy URL is invalid")
    candidate = url.strip()
    if not candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        raise _unsafe("Configured proxy URL is invalid")
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except (TypeError, ValueError):
        raise _unsafe("Configured proxy URL is invalid") from None
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or "%" in parsed.hostname
    ):
        raise _unsafe("Configured proxy URL is invalid")

    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
        target_port = port or (443 if parsed.scheme.lower() == "https" else 80)
        addresses = socket.getaddrinfo(hostname, target_port, type=socket.SOCK_STREAM)
        normalized_addresses = []
        for family, socktype, proto, canonname, sockaddr in addresses:
            if family not in {socket.AF_INET, socket.AF_INET6}:
                raise ValueError
            resolved = ip_address(sockaddr[0])
            normalized_sockaddr = (
                (str(resolved), target_port, sockaddr[2], sockaddr[3])
                if family == socket.AF_INET6
                else (str(resolved), target_port)
            )
            normalized_addresses.append(
                (
                    family,
                    socktype or socket.SOCK_STREAM,
                    proto or socket.IPPROTO_TCP,
                    canonname,
                    normalized_sockaddr,
                )
            )
    except (IndexError, OSError, TypeError, UnicodeError, ValueError):
        raise _unsafe("Configured proxy URL is invalid") from None
    if not normalized_addresses:
        raise _unsafe("Configured proxy URL is invalid")
    return candidate, tuple(normalized_addresses)


def _trusted_environment_proxy(url: str) -> _TrustedProxy | None:
    """Return an administrator-trusted proxy explicitly configured per scheme.

    The proxy is an intentional trust boundary: HTTP forwarding/CONNECT makes
    the proxy, not this process, perform the final destination connection.
    Destination URLs are still validated before this boundary is crossed.
    """
    scheme = urlsplit(url).scheme.lower()
    environment_name = "HTTPS_PROXY" if scheme == "https" else "HTTP_PROXY"
    configured_proxy = os.environ.get(environment_name, "").strip()
    if not configured_proxy:
        return None
    proxy_url, proxy_addresses = _resolve_trusted_proxy_url(configured_proxy)
    return _TrustedProxy(proxy_url, proxy_addresses)


class _BoundConnectionMixin:
    """Connect urllib3 to prevalidated sockaddrs without resolving again."""

    def __init__(self, *args, resolved_addresses: tuple[tuple, ...], on_connect=None, **kwargs):
        self._resolved_addresses = resolved_addresses
        self._on_connect = on_connect
        super().__init__(*args, **kwargs)

    def connect(self):
        super().connect()
        if self._on_connect is not None:
            try:
                self._on_connect(self.sock)
            except BaseException:
                self.close()
                raise

    def _new_conn(self) -> socket.socket:
        last_error: OSError | None = None
        for family, socktype, proto, _canonname, sockaddr in self._resolved_addresses:
            sock = None
            try:
                sock = socket.socket(family, socktype, proto)
                sock.settimeout(self.timeout)
                for option in self.socket_options or ():
                    sock.setsockopt(*option)
                if self.source_address:
                    sock.bind(self.source_address)
                sock.connect(sockaddr)
                sys.audit("http.client.connect", self, self.host, self.port)
                return sock
            except TimeoutError as exc:
                if sock is not None:
                    sock.close()
                raise ConnectTimeoutError(
                    self,
                    f"Connection to public target timed out. (connect timeout={self.timeout})",
                ) from exc
            except OSError as exc:
                last_error = exc
                if sock is not None:
                    sock.close()

        raise NewConnectionError(
            self,
            "Failed to establish a connection to the validated public target",
        ) from last_error


class _BoundHTTPConnection(_BoundConnectionMixin, HTTPConnection):
    pass


class _BoundHTTPSConnection(_BoundConnectionMixin, HTTPSConnection):
    pass


class _BoundHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _BoundHTTPConnection


class _BoundHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _BoundHTTPSConnection


class _BoundAddressAdapter(HTTPAdapter):
    """Requests adapter whose pools retain the origin but dial only validated IPs."""

    def __init__(
        self,
        resolved_addresses: tuple[tuple, ...],
        *,
        trusted_proxy: _TrustedProxy | None = None,
        on_connect=None,
    ):
        self._resolved_addresses = resolved_addresses
        self._trusted_proxy = trusted_proxy
        self._on_connect = on_connect
        self._proxy_manager = (
            ProxyManager(trusted_proxy.url) if trusted_proxy is not None else None
        )
        super().__init__()

    def _pool_for_url(self, url: str, pool_kwargs: dict | None = None):
        parsed = urlsplit(url)
        # Keep the original hostname on the pool/connection: urllib3 uses it
        # for the HTTP Host header and for HTTPS SNI/certificate matching.
        # Only _new_conn's socket destination is replaced with validated IPs.
        if self._trusted_proxy is not None and parsed.scheme == "http":
            # Requests supplies TLS-only values such as cert_reqs here even for
            # plain HTTP. Passing them to HTTPConnection would raise TypeError.
            proxy = self._proxy_manager.proxy
            return _BoundHTTPConnectionPool(
                proxy.host,
                proxy.port,
                resolved_addresses=self._trusted_proxy.resolved_addresses,
                on_connect=self._on_connect,
            )
        if parsed.scheme == "https":
            proxy_kwargs = {}
            connection_addresses = self._resolved_addresses
            if self._trusted_proxy is not None:
                proxy_kwargs = {
                    "_proxy": self._proxy_manager.proxy,
                    "_proxy_headers": self._proxy_manager.proxy_headers,
                    "_proxy_config": self._proxy_manager.proxy_config,
                }
                connection_addresses = self._trusted_proxy.resolved_addresses
            return _BoundHTTPSConnectionPool(
                parsed.hostname,
                parsed.port,
                resolved_addresses=connection_addresses,
                on_connect=self._on_connect,
                **proxy_kwargs,
                **(pool_kwargs or {}),
            )
        return _BoundHTTPConnectionPool(
            parsed.hostname,
            parsed.port,
            resolved_addresses=self._resolved_addresses,
            on_connect=self._on_connect,
        )

    def get_connection_with_tls_context(
        self,
        request,
        verify,
        proxies=None,
        cert=None,
    ):
        if bool(proxies) != bool(self._trusted_proxy):
            raise _unsafe("Proxy routing is not allowed for safe requests")
        _host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        return self._pool_for_url(request.url, pool_kwargs)

    def get_connection(self, url, proxies=None):
        """Compatibility with Requests versions before get_connection_with_tls_context."""
        if bool(proxies) != bool(self._trusted_proxy):
            raise _unsafe("Proxy routing is not allowed for safe requests")
        return self._pool_for_url(url)


def _send_bound_request(
    url: str,
    *,
    method: str,
    resolved_addresses: tuple[tuple, ...],
    trusted_proxy: _TrustedProxy | None = None,
    on_connect=None,
    **kwargs,
) -> requests.Response:
    """Send one hop through an adapter bound to the validated socket addresses."""
    if "proxies" in kwargs:
        raise _unsafe("Explicit proxy routing is not allowed for safe requests")
    kwargs["allow_redirects"] = False
    proxies = (
        {"http": trusted_proxy.url, "https": trusted_proxy.url}
        if trusted_proxy is not None
        else {}
    )
    session = requests.Session()
    session.trust_env = False
    adapter = _BoundAddressAdapter(resolved_addresses, trusted_proxy=trusted_proxy, on_connect=on_connect)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    try:
        response = session.request(method, url, proxies=proxies, **kwargs)
    except BaseException:
        # Close the owned session even for process-level interruptions, then
        # immediately re-raise the original exception without suppressing it.
        session.close()
        raise
    if kwargs.get("stream"):
        original_close = response.close
        def _close_and_release_session(*a, **kw):
            try:
                return original_close(*a, **kw)
            finally:
                session.close()
        response.close = _close_and_release_session
    else:
        session.close()
    return response


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme.lower() == "https" else 80
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), port


def _without_sensitive_cross_origin_headers(
    headers: dict | None,
    source_url: str,
    target_url: str,
) -> dict | None:
    if not headers or _origin(source_url) == _origin(target_url):
        return headers
    sensitive = {"authorization", "cookie", "proxy-authorization", "x-api-key"}
    return {key: value for key, value in headers.items() if key.lower() not in sensitive}


def _without_body_headers(headers: dict | None) -> dict | None:
    if not headers:
        return headers
    body_headers = {"content-length", "content-type", "transfer-encoding"}
    return {key: value for key, value in headers.items() if key.lower() not in body_headers}


def _safe_request(method, url, *, max_redirects=_DEFAULT_MAX_REDIRECTS, allow_private=False, **kwargs):
    """Send a GET or POST after validating every redirect hop."""
    if method not in {"GET", "POST"}:
        raise ValueError("safe requests support GET and POST only")
    if not isinstance(max_redirects, int) or max_redirects < 0:
        raise ValueError("max_redirects must be a non-negative integer")
    if "proxies" in kwargs:
        raise _unsafe("Explicit proxy routing is not allowed for safe requests")

    current_url, resolved_addresses = _resolve_http_url(url, allow_private=allow_private)
    current_method = method
    request_kwargs = dict(kwargs)
    request_kwargs["allow_redirects"] = False

    redirects_followed = 0
    while True:
        trusted_proxy = _trusted_environment_proxy(current_url)
        response = _send_bound_request(
            current_url,
            method=current_method,
            resolved_addresses=resolved_addresses,
            trusted_proxy=trusted_proxy,
            **request_kwargs,
        )
        location = response.headers.get("Location") if response.status_code in _REDIRECT_STATUSES else None
        if not location:
            return response

        if redirects_followed >= max_redirects:
            response.close()
            raise _unsafe("Too many redirects")

        try:
            next_url, next_addresses = _resolve_http_url(
                urljoin(current_url, location), allow_private=allow_private,
            )
        except (UnsafeUrlError, TypeError, ValueError):
            response.close()
            raise _unsafe() from None
        next_headers = _without_sensitive_cross_origin_headers(
            request_kwargs.get("headers"),
            current_url,
            next_url,
        )

        if current_method == "POST" and response.status_code in {301, 302, 303}:
            current_method = "GET"
            for body_key in ("data", "json", "files"):
                request_kwargs.pop(body_key, None)
            next_headers = _without_body_headers(next_headers)

        if next_headers is not None:
            request_kwargs["headers"] = next_headers
        response.close()
        current_url = next_url
        resolved_addresses = next_addresses
        redirects_followed += 1


def safe_get(
    url: str,
    *,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    allow_private: bool = False,
    **kwargs,
) -> requests.Response:
    """GET a public URL while validating every redirect target."""
    return _safe_request("GET", url, max_redirects=max_redirects, allow_private=allow_private, **kwargs)


def safe_post(
    url: str,
    *,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    allow_private: bool = False,
    **kwargs,
) -> requests.Response:
    """POST to a public URL while validating every redirect target."""
    return _safe_request("POST", url, max_redirects=max_redirects, allow_private=allow_private, **kwargs)
