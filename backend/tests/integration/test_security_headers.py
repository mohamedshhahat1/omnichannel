"""Foundational HTTP security configuration."""

from fastapi.testclient import TestClient

from app.api.application import create_app
from app.core.settings import Environment, SecuritySettings
from tests.conftest import build_settings


def test_baseline_security_headers_are_present(client: TestClient) -> None:
    headers = client.get("/health/live").headers

    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"
    assert headers["Cross-Origin-Opener-Policy"] == "same-origin"
    assert headers["Cross-Origin-Resource-Policy"] == "same-origin"
    assert headers["Cache-Control"] == "no-store"
    assert "Permissions-Policy" in headers


def test_security_headers_are_present_on_error_responses(client: TestClient) -> None:
    headers = client.get("/does-not-exist").headers
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_hsts_is_absent_outside_production(client: TestClient) -> None:
    assert "Strict-Transport-Security" not in client.get("/health/live").headers


def test_hsts_is_sent_in_production() -> None:
    settings = build_settings(
        environment=Environment.PRODUCTION,
        security=SecuritySettings(trusted_hosts=("testserver",)),
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        header = test_client.get("/health/live").headers["Strict-Transport-Security"]

    assert "max-age=63072000" in header
    assert "includeSubDomains" in header


def test_security_headers_can_be_disabled() -> None:
    settings = build_settings(security=SecuritySettings(security_headers_enabled=False))
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        assert "X-Content-Type-Options" not in test_client.get("/health/live").headers


def test_cors_is_off_when_no_origin_is_configured(client: TestClient) -> None:
    response = client.get("/health/live", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_configured_origins_are_allowed() -> None:
    settings = build_settings(
        security=SecuritySettings(cors_origins=("https://app.example.com",)),
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        response = test_client.get(
            "/health/live",
            headers={"Origin": "https://app.example.com"},
        )

    assert response.headers["access-control-allow-origin"] == "https://app.example.com"


def test_unlisted_origins_are_not_allowed() -> None:
    settings = build_settings(
        security=SecuritySettings(cors_origins=("https://app.example.com",)),
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        response = test_client.get(
            "/health/live",
            headers={"Origin": "https://evil.example.com"},
        )

    assert "access-control-allow-origin" not in response.headers


def test_correlation_headers_are_exposed_to_browsers() -> None:
    settings = build_settings(
        security=SecuritySettings(cors_origins=("https://app.example.com",)),
    )
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        response = test_client.get(
            "/health/live",
            headers={"Origin": "https://app.example.com"},
        )

    assert "X-Request-ID" in response.headers["access-control-expose-headers"]


def test_trusted_hosts_reject_a_forged_host_header() -> None:
    settings = build_settings(security=SecuritySettings(trusted_hosts=("api.example.com",)))
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        allowed = test_client.get("/health/live", headers={"Host": "api.example.com"})
        forged = test_client.get("/health/live", headers={"Host": "attacker.example.com"})

    assert allowed.status_code == 200
    assert forged.status_code == 400
