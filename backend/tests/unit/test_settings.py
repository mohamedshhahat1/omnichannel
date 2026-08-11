"""Configuration typing, defaults and validation."""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.settings import (
    Environment,
    LoggingSettings,
    ObservabilitySettings,
    SecuritySettings,
    ServerSettings,
    Settings,
    get_settings,
)


def make_settings(**overrides: Any) -> Settings:
    return Settings(_env_file=None, **overrides)


def production_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "environment": Environment.PRODUCTION,
        "debug": False,
        "logging": LoggingSettings(level="INFO", format="json"),
        "security": SecuritySettings(trusted_hosts=("api.example.com",)),
    }
    defaults.update(overrides)
    return make_settings(**defaults)


def test_defaults_are_safe_for_local_development() -> None:
    settings = make_settings()

    assert settings.environment is Environment.DEVELOPMENT
    assert settings.debug is False
    assert settings.server.host == "127.0.0.1"
    assert settings.server.trust_inbound_request_id is False
    assert settings.observability.tracing_enabled is False
    assert settings.security.cors_origins == ()
    assert settings.security.trusted_hosts == ()
    assert settings.security.security_headers_enabled is True


def test_environment_predicates() -> None:
    assert production_settings().is_production
    assert make_settings(environment=Environment.TEST).is_test
    assert make_settings(environment=Environment.DEVELOPMENT).is_development


def test_settings_are_immutable() -> None:
    settings = make_settings()
    with pytest.raises(ValidationError):
        settings.debug = True  # type: ignore[misc]


def test_sections_reject_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ServerSettings(prot=8000)  # type: ignore[call-arg]


def test_port_and_sample_ratio_bounds_are_enforced() -> None:
    with pytest.raises(ValidationError):
        ServerSettings(port=70_000)
    with pytest.raises(ValidationError):
        ObservabilitySettings(sample_ratio=1.5)


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LoggingSettings(level="TRACE")


@pytest.mark.parametrize("prefix", ["api", "/api/", "api/"])
def test_malformed_api_prefix_is_rejected(prefix: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(api_prefix=prefix)


@pytest.mark.parametrize("prefix", ["/api", "/api/internal", ""])
def test_valid_api_prefix_is_accepted(prefix: str) -> None:
    assert make_settings(api_prefix=prefix).api_prefix == prefix


def test_otlp_exporter_requires_an_endpoint() -> None:
    with pytest.raises(ValidationError, match="otlp_endpoint"):
        make_settings(
            observability=ObservabilitySettings(tracing_enabled=True, exporter="otlp"),
        )


def test_otlp_exporter_with_an_endpoint_is_accepted() -> None:
    settings = make_settings(
        observability=ObservabilitySettings(
            tracing_enabled=True,
            exporter="otlp",
            otlp_endpoint="http://collector:4318/v1/traces",
        ),
    )
    assert settings.observability.otlp_endpoint == "http://collector:4318/v1/traces"


def test_production_rejects_debug_mode() -> None:
    with pytest.raises(ValidationError, match="debug must be disabled"):
        production_settings(debug=True)


def test_production_rejects_non_json_logging() -> None:
    with pytest.raises(ValidationError, match="logging.format"):
        production_settings(logging=LoggingSettings(format="console"))


def test_production_rejects_wildcard_cors_origins() -> None:
    with pytest.raises(ValidationError, match="cors_origins"):
        production_settings(
            security=SecuritySettings(cors_origins=("*",), trusted_hosts=("api.example.com",)),
        )


def test_production_requires_trusted_hosts() -> None:
    with pytest.raises(ValidationError, match="trusted_hosts must be set"):
        production_settings(security=SecuritySettings())


def test_production_rejects_wildcard_trusted_hosts() -> None:
    with pytest.raises(ValidationError, match="trusted_hosts must not contain"):
        production_settings(security=SecuritySettings(trusted_hosts=("*",)))


def test_a_correct_production_configuration_is_accepted() -> None:
    settings = production_settings(
        security=SecuritySettings(
            cors_origins=("https://app.example.com",),
            trusted_hosts=("api.example.com",),
        ),
    )
    assert settings.is_production
    assert settings.security.cors_origins == ("https://app.example.com",)


def test_values_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC_ENVIRONMENT", "production")
    monkeypatch.setenv("OC_SERVER__PORT", "9001")
    monkeypatch.setenv("OC_SECURITY__TRUSTED_HOSTS", '["api.example.com"]')

    settings = Settings(_env_file=None)

    assert settings.is_production
    assert settings.server.port == 9001
    assert settings.security.trusted_hosts == ("api.example.com",)


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC_SERVICE_NAME", "first-name")
    get_settings.cache_clear()
    first = get_settings()

    monkeypatch.setenv("OC_SERVICE_NAME", "second-name")
    assert get_settings() is first

    get_settings.cache_clear()
    assert get_settings().service_name == "second-name"
