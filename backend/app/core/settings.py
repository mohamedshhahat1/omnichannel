"""Typed application configuration.

All configuration is read from the environment (or a local, git-ignored `.env`
file), validated once at startup, and exposed as a frozen object. Code reads
settings through `get_settings()` or the FastAPI dependency - never through
ad-hoc `os.environ` lookups (ENGINEERING.md 8.6).

Naming: every variable is prefixed `OC_`. Nested groups use a double
underscore, so `OC_SERVER__PORT` populates `settings.server.port`. Complex
values such as lists are parsed as JSON.

Adding a settings group in a later phase is three steps:

1. Define a `SettingsSection` subclass, e.g. `DatabaseSettings`.
2. Add it as a field on `Settings` with a `default_factory`.
3. Document the variables in `.env.example`.

Secrets use `pydantic.SecretStr` so they cannot be printed or logged by
accident. Phase 1 needs no secrets, so none are defined yet - a setting is
added by the phase that consumes it, not in advance.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    """Deployment environment."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class SettingsSection(BaseModel):
    """Base class for grouped settings. Frozen, and rejects unknown keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ServerSettings(SettingsSection):
    """HTTP server and request-handling configuration."""

    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    root_path: str = ""

    request_id_header: str = "X-Request-ID"
    correlation_id_header: str = "X-Correlation-ID"

    # Off by default. Only enable when a reverse proxy is the sole ingress and
    # always overwrites this header, otherwise any client can choose the id
    # that identifies its own request.
    trust_inbound_request_id: bool = False

    # Defence in depth. The authoritative limit belongs at NGINX (Phase 2);
    # this stops an oversized body from being buffered by the application.
    max_request_body_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)


class LoggingSettings(SettingsSection):
    """Structured logging configuration."""

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["json", "console"] = "json"


class ObservabilitySettings(SettingsSection):
    """OpenTelemetry configuration.

    Tracing is disabled by default so that local development and the test suite
    need no tracing backend and no network egress.
    """

    tracing_enabled: bool = False
    exporter: Literal["none", "console", "otlp"] = "none"
    otlp_endpoint: str | None = None
    sample_ratio: float = Field(default=1.0, ge=0.0, le=1.0)

    # Comma-separated URL patterns excluded from tracing. Health probes fire
    # constantly and would otherwise dominate trace volume and cost.
    excluded_urls: str = "health/live,health/ready"


class SecuritySettings(SettingsSection):
    """Transport-level security configuration.

    Authentication, sessions, CSRF and RBAC are Phase 3 (ADR-0009) and are
    deliberately absent here.
    """

    cors_origins: tuple[str, ...] = ()
    cors_allow_credentials: bool = True
    cors_allow_methods: tuple[str, ...] = (
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "OPTIONS",
    )
    cors_allow_headers: tuple[str, ...] = (
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-Correlation-ID",
    )

    trusted_hosts: tuple[str, ...] = ()
    security_headers_enabled: bool = True
    hsts_max_age_seconds: int = Field(default=63_072_000, ge=0)


class Settings(BaseSettings):
    """Root application settings."""

    model_config = SettingsConfigDict(
        env_prefix="OC_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    environment: Environment = Environment.DEVELOPMENT
    debug: bool = False
    service_name: str = "omnichannel-api"
    service_version: str = "0.1.0"
    api_prefix: str = "/api"

    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    @property
    def is_production(self) -> bool:
        """True when running in the production environment."""
        return self.environment is Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        """True when running in the development environment."""
        return self.environment is Environment.DEVELOPMENT

    @property
    def is_test(self) -> bool:
        """True when running in the test environment."""
        return self.environment is Environment.TEST

    @model_validator(mode="after")
    def _validate_api_prefix(self) -> Self:
        prefix = self.api_prefix
        if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
            message = "api_prefix must start with '/' and must not end with '/'."
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_exporter(self) -> Self:
        if self.observability.exporter == "otlp" and not self.observability.otlp_endpoint:
            message = "observability.otlp_endpoint is required when exporter is 'otlp'."
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _validate_production_hardening(self) -> Self:
        """Refuse to start with a configuration that is unsafe in production.

        These are the mistakes that are easy to make and expensive to notice:
        debug output leaking internals, a wildcard CORS policy paired with
        credentials, unparseable logs, and an unset host allowlist.
        """
        if not self.is_production:
            return self

        problems: list[str] = []

        if self.debug:
            problems.append("debug must be disabled in production")

        if self.logging.format != "json":
            problems.append("logging.format must be 'json' in production")

        if "*" in self.security.cors_origins:
            problems.append("security.cors_origins must not contain '*' in production")

        if not self.security.trusted_hosts:
            problems.append("security.trusted_hosts must be set in production")
        elif "*" in self.security.trusted_hosts:
            problems.append("security.trusted_hosts must not contain '*' in production")

        if problems:
            raise ValueError("; ".join(problems))
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance.

    Cached rather than global-mutable: the object is frozen, it is built once,
    and tests reset it with `get_settings.cache_clear()`.
    """
    return Settings()
