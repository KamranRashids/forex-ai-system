"""Unit tests for trusted-proxy client-IP resolution (Phase 10 hardening).

These verify the security contract in ``app/api/deps.py::client_ip``:

* Direct/untrusted peers can NEVER influence the attributed IP via forwarded
  headers — the socket peer is always used, so the rate limiter's per-IP key
  cannot be spoofed through a direct connection.
* Only a peer inside ``settings.trusted_proxies`` may supply forwarded headers.
* ``X-Real-IP`` (overwritten by our NGINX) is preferred; the right-most
  ``X-Forwarded-For`` entry is the fallback.
"""

from __future__ import annotations

import ipaddress

import pytest
from app.api.deps import client_ip
from app.core.config import Settings
from starlette.requests import Request

TRUSTED_SUBNET = "172.28.0.0/16"


def make_request(client_host: str, headers: dict[str, str] | None = None) -> Request:
    headers = headers or {}
    scope: dict[str, object] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/auth/login",
        "query_string": b"",
        "headers": [
            (key.lower().encode("ascii"), value.encode("ascii")) for key, value in headers.items()
        ],
        "client": (client_host, 54321),
        "server": ("api", 8000),
    }
    return Request(scope)  # type: ignore[arg-type]


def make_settings(trusted_proxies: str) -> Settings:
    return Settings(
        trusted_proxies=trusted_proxies,
        secret_key="unit-test-secret-key-0123456789abcdef0123456789abcdef",
    )


@pytest.mark.unit
def test_direct_peer_used_when_no_proxy_configured() -> None:
    settings = make_settings(trusted_proxies="")
    request = make_request("198.51.100.10", headers={"X-Real-IP": "203.0.113.9"})
    assert client_ip(request, settings) == "198.51.100.10"


@pytest.mark.unit
def test_untrusted_peer_cannot_spoof_forwarded_headers() -> None:
    settings = make_settings(trusted_proxies=TRUSTED_SUBNET)
    request = make_request(
        "203.0.113.9",
        headers={
            "X-Real-IP": "1.2.3.4",
            "X-Forwarded-For": "1.2.3.4, 6.6.6.6",
        },
    )
    assert client_ip(request, settings) == "203.0.113.9"


@pytest.mark.unit
def test_trusted_peer_uses_x_real_ip() -> None:
    settings = make_settings(trusted_proxies=TRUSTED_SUBNET)
    request = make_request("172.28.0.5", headers={"X-Real-IP": "203.0.113.9"})
    assert client_ip(request, settings) == "203.0.113.9"


@pytest.mark.unit
def test_trusted_peer_prefers_x_real_ip_over_spoofed_xff() -> None:
    settings = make_settings(trusted_proxies=TRUSTED_SUBNET)
    request = make_request(
        "172.28.0.5",
        headers={"X-Real-IP": "203.0.113.9", "X-Forwarded-For": "6.6.6.6"},
    )
    assert client_ip(request, settings) == "203.0.113.9"


@pytest.mark.unit
def test_trusted_peer_walks_xff_rightmost_when_x_real_ip_absent() -> None:
    settings = make_settings(trusted_proxies=TRUSTED_SUBNET)
    request = make_request(
        "172.28.0.5",
        headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"},
    )
    assert client_ip(request, settings) == "203.0.113.9"


@pytest.mark.unit
def test_trusted_peer_ignores_malformed_headers_and_falls_back_to_peer() -> None:
    settings = make_settings(trusted_proxies=TRUSTED_SUBNET)
    request = make_request(
        "172.28.0.5",
        headers={"X-Real-IP": "not-an-ip", "X-Forwarded-For": "also-not-an-ip"},
    )
    assert client_ip(request, settings) == "172.28.0.5"


@pytest.mark.unit
def test_ipv6_trusted_peer_resolution() -> None:
    settings = make_settings(trusted_proxies="fd00::/8")
    request = make_request("fd00::1", headers={"X-Real-IP": "::ffff:203.0.113.9"})
    assert client_ip(request, settings) == "::ffff:203.0.113.9"


@pytest.mark.unit
def test_missing_client_falls_back_to_none() -> None:
    settings = make_settings(trusted_proxies=TRUSTED_SUBNET)
    assert client_ip(make_request("172.28.0.5"), settings) == "172.28.0.5"
    # No client in scope -> None, so enforce_rate_limit uses the "unknown" key.
    scope: dict[str, object] = {
        "type": "http",
        "method": "POST",
        "scheme": "http",
        "path": "/api/v1/auth/login",
        "query_string": b"",
        "headers": [],
        "server": ("api", 8000),
    }
    assert client_ip(Request(scope), settings) is None  # type: ignore[arg-type]


@pytest.mark.unit
def test_trusted_proxy_cidrs_parsing() -> None:
    settings = make_settings(trusted_proxies="172.28.0.0/16, fd00::/8")
    cidrs = settings.trusted_proxy_cidrs
    assert len(cidrs) == 2
    assert ipaddress.ip_address("172.28.0.5") in cidrs[0]
    assert ipaddress.ip_address("fd00::1") in cidrs[1]


@pytest.mark.unit
def test_empty_trusted_proxies_yields_no_cidrs() -> None:
    assert make_settings(trusted_proxies="").trusted_proxy_cidrs == []
