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
accident. Phase 2 keeps example credentials only in the local example env file;
production secrets remain external.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PRODUCTION_ARGON2_MEMORY_KIB = 65_536
_PRODUCTION_ARGON2_TIME_COST = 3


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

    Scope note: this section covers what protects the *connection* - CORS, host
    allowlisting, response headers. Who the caller is and what they may do is
    `AuthSettings` below (Phase 3, ADR-0009).
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
        "X-CSRF-Token",
    )

    trusted_hosts: tuple[str, ...] = ()
    security_headers_enabled: bool = True
    hsts_max_age_seconds: int = Field(default=63_072_000, ge=0)


class AuthSettings(SettingsSection):
    """Identity, session, CSRF and API-key configuration (Phase 3, ADR-0009).

    Defaults are the production values from `docs/security.md` 2. They are
    settings rather than constants for two reasons: Argon2id parameters must be
    raised over time as hardware improves, and the test suite has to be able to
    lower the cost factor without every login taking 100 ms. The production
    floor enforced in `Settings` is what stops a cheap test configuration from
    ever reaching a deployment.
    """

    # --- Password hashing (Argon2id) ---------------------------------------
    argon2_time_cost: int = Field(default=_PRODUCTION_ARGON2_TIME_COST, ge=1)
    argon2_memory_kib: int = Field(default=_PRODUCTION_ARGON2_MEMORY_KIB, ge=8_192)
    argon2_parallelism: int = Field(default=4, ge=1, le=16)
    argon2_hash_bytes: int = Field(default=32, ge=16, le=64)
    argon2_salt_bytes: int = Field(default=16, ge=16, le=64)
    password_min_length: int = Field(default=12, ge=12)
    password_max_length: int = Field(default=1_024, ge=64)

    # --- Sessions -----------------------------------------------------------
    session_idle_ttl_seconds: int = Field(default=7 * 24 * 60 * 60, ge=60)
    session_absolute_ttl_seconds: int = Field(default=30 * 24 * 60 * 60, ge=300)
    # `last_seen_at` is refreshed at most this often, so a busy session does
    # not turn every authenticated request into a write.
    session_touch_interval_seconds: int = Field(default=60, ge=1)
    # Redis is a read-through cache in front of PostgreSQL, never the record of
    # truth. A short TTL bounds how long a revoked session can survive a cache
    # entry that somehow escaped invalidation.
    session_cache_ttl_seconds: int = Field(default=60, ge=0, le=300)

    # --- Cookies and CSRF ---------------------------------------------------
    session_cookie_name: str = "__Host-oc_session"
    csrf_cookie_name: str = "__Host-oc_csrf"
    csrf_header_name: str = "X-CSRF-Token"
    cookie_secure: bool = True
    cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    cookie_path: str = "/"

    # --- Single-use email tokens --------------------------------------------
    email_verification_ttl_seconds: int = Field(default=24 * 60 * 60, ge=300)
    credential_reset_ttl_seconds: int = Field(default=30 * 60, ge=60)

    # --- API keys -------------------------------------------------------------
    api_key_max_ttl_days: int = Field(default=365, ge=1, le=3_650)

    # --- Login throttling -----------------------------------------------------
    max_failed_logins: int = Field(default=10, ge=3)
    lockout_seconds: int = Field(default=15 * 60, ge=60)

    @model_validator(mode="after")
    def _validate_auth_invariants(self) -> Self:
        problems: list[str] = []

        if self.session_absolute_ttl_seconds <= self.session_idle_ttl_seconds:
            problems.append("auth.session_absolute_ttl_seconds must exceed the idle TTL")

        if self.password_max_length <= self.password_min_length:
            problems.append("auth.password_max_length must exceed password_min_length")

        if self.cookie_samesite == "none" and not self.cookie_secure:
            problems.append("auth.cookie_samesite 'none' requires auth.cookie_secure")

        cookies = (
            ("session_cookie_name", self.session_cookie_name),
            ("csrf_cookie_name", self.csrf_cookie_name),
        )
        for field_name, cookie_name in cookies:
            if not cookie_name:
                problems.append(f"auth.{field_name} must not be empty")
                continue
            # The `__Host-` and `__Secure-` prefixes are enforced by browsers,
            # not by us: a cookie that claims them without Secure is simply
            # dropped, which would look like a broken login rather than a
            # misconfiguration.
            if cookie_name.startswith(("__Host-", "__Secure-")) and not self.cookie_secure:
                problems.append(f"auth.{field_name} prefix requires auth.cookie_secure")
            if cookie_name.startswith("__Host-") and self.cookie_path != "/":
                problems.append(f"auth.{field_name} prefix requires auth.cookie_path '/'")

        if self.session_cookie_name == self.csrf_cookie_name:
            problems.append("auth.csrf_cookie_name must differ from auth.session_cookie_name")

        if problems:
            raise ValueError("; ".join(problems))
        return self


class DatabaseSettings(SettingsSection):
    """Async SQLAlchemy + asyncpg configuration."""

    url: str = "postgresql+asyncpg://oc_app:oc_app_password@postgres:5432/omnichannel"
    migration_url: str = (
        "postgresql+asyncpg://oc_migrator:oc_migrator_password@postgres:5432/omnichannel"
    )
    pool_size: int = Field(default=5, ge=1, le=100)
    max_overflow: int = Field(default=5, ge=0, le=100)
    pool_timeout_seconds: float = Field(default=5.0, gt=0)
    pool_recycle_seconds: int = Field(default=1800, ge=60)
    statement_timeout_ms: int = Field(default=10_000, ge=100)
    lock_timeout_ms: int = Field(default=2_000, ge=100)
    health_timeout_seconds: float = Field(default=2.0, gt=0, le=30)

    @model_validator(mode="after")
    def _validate_urls(self) -> Self:
        for field_name, value in (("url", self.url), ("migration_url", self.migration_url)):
            if not value.startswith("postgresql+asyncpg://"):
                raise ValueError(f"database.{field_name} must use postgresql+asyncpg")
        return self


class RedisSettings(SettingsSection):
    """Redis client configuration."""

    url: str = "redis://redis:6379/0"
    max_connections: int = Field(default=20, ge=1, le=1000)
    socket_connect_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    socket_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    health_timeout_seconds: float = Field(default=2.0, gt=0, le=30)


class CelerySettings(SettingsSection):
    """Celery broker and queue configuration."""

    broker_url: str = "redis://redis:6379/1"
    result_backend_enabled: bool = False
    result_backend_url: str | None = None
    default_queue: str = "default"
    critical_queue: str = "critical"
    background_queue: str = "background"
    task_soft_time_limit_seconds: int = Field(default=270, ge=1)
    task_time_limit_seconds: int = Field(default=300, ge=2)
    worker_prefetch_multiplier: int = Field(default=1, ge=1, le=16)

    @model_validator(mode="after")
    def _validate_backend_and_queues(self) -> Self:
        if self.result_backend_enabled and not self.result_backend_url:
            raise ValueError("celery.result_backend_url is required when enabled")
        if self.task_soft_time_limit_seconds >= self.task_time_limit_seconds:
            raise ValueError("celery soft time limit must be below hard time limit")
        queue_names = {self.default_queue, self.critical_queue, self.background_queue}
        if len(queue_names) != 3 or any(not value.strip() for value in queue_names):
            raise ValueError("celery queue names must be non-empty and distinct")
        return self


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
    service_version: str = "0.2.0"
    api_prefix: str = "/api"

    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)

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
        credentials, unparseable logs, an unset host allowlist, and - since
        Phase 3 - session cookies that a browser would refuse to protect or
        password hashing tuned for a fast test suite.
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

        if not self.auth.cookie_secure:
            problems.append("auth.cookie_secure must be enabled in production")

        if not self.auth.session_cookie_name.startswith("__Host-"):
            problems.append("auth.session_cookie_name must use the '__Host-' prefix")

        if not self.auth.csrf_cookie_name.startswith("__Host-"):
            problems.append("auth.csrf_cookie_name must use the '__Host-' prefix")

        if self.auth.argon2_memory_kib < _PRODUCTION_ARGON2_MEMORY_KIB:
            problems.append("auth.argon2_memory_kib is below the production floor")

        if self.auth.argon2_time_cost < _PRODUCTION_ARGON2_TIME_COST:
            problems.append("auth.argon2_time_cost is below the production floor")

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
