"""Auth configuration invariants and the production floor.

The cheap way to weaken authentication is not to write insecure code, it is to
ship a fast, convenient local configuration to production. These tests exist so
that every relaxation a developer might reasonably make - a non-Secure cookie,
a cheap Argon2 cost - is a startup failure rather than a silent downgrade.
"""

from typing import Any

import pytest
from pydantic import ValidationError

from app.core.settings import (
    AuthSettings,
    Environment,
    LoggingSettings,
    SecuritySettings,
    Settings,
)

PRODUCTION_ARGON2_MEMORY_KIB = 65_536
PRODUCTION_ARGON2_TIME_COST = 3


def production_settings(**overrides: Any) -> Settings:
    defaults: dict[str, Any] = {
        "environment": Environment.PRODUCTION,
        "debug": False,
        "logging": LoggingSettings(level="INFO", format="json"),
        "security": SecuritySettings(trusted_hosts=("api.example.com",)),
    }
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def relaxed_auth(**overrides: Any) -> AuthSettings:
    """The configuration a developer needs over plain HTTP on localhost.

    `__Host-` cookies are refused by browsers without HTTPS, so local work has
    to drop both the prefix and the Secure flag together.
    """
    defaults: dict[str, Any] = {
        "cookie_secure": False,
        "session_cookie_name": "oc_session",
        "csrf_cookie_name": "oc_csrf",
    }
    defaults.update(overrides)
    return AuthSettings(**defaults)


def test_defaults_are_already_production_grade() -> None:
    auth = AuthSettings()
    assert auth.cookie_secure is True
    assert auth.session_cookie_name.startswith("__Host-")
    assert auth.csrf_cookie_name.startswith("__Host-")
    assert auth.cookie_samesite == "lax"
    assert auth.cookie_path == "/"
    assert auth.argon2_memory_kib >= PRODUCTION_ARGON2_MEMORY_KIB
    assert auth.argon2_time_cost >= PRODUCTION_ARGON2_TIME_COST
    assert auth.password_min_length >= 12


def test_unknown_keys_are_rejected() -> None:
    with pytest.raises(ValidationError):
        AuthSettings(argon_time_cost=3)  # type: ignore[call-arg]


def test_host_prefixed_cookies_require_the_secure_flag() -> None:
    # A browser ignores a `__Host-` cookie that is not Secure, so this
    # combination is not "slightly weaker", it is a broken login.
    with pytest.raises(ValidationError, match="prefix requires auth.cookie_secure"):
        AuthSettings(cookie_secure=False)


def test_host_prefixed_cookies_require_the_root_path() -> None:
    with pytest.raises(ValidationError, match=r"prefix requires auth.cookie_path"):
        AuthSettings(cookie_path="/api")


def test_the_two_cookies_must_not_collide() -> None:
    with pytest.raises(ValidationError, match="must differ from"):
        AuthSettings(csrf_cookie_name="__Host-oc_session")


@pytest.mark.parametrize("field", ["session_cookie_name", "csrf_cookie_name"])
def test_cookie_names_must_not_be_empty(field: str) -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        AuthSettings(**{field: ""})


def test_samesite_none_requires_the_secure_flag() -> None:
    with pytest.raises(ValidationError, match="requires auth.cookie_secure"):
        relaxed_auth(cookie_samesite="none")


def test_absolute_session_lifetime_must_exceed_the_idle_one() -> None:
    # Otherwise the absolute cap is unreachable and sessions renew forever.
    with pytest.raises(ValidationError, match="must exceed the idle TTL"):
        AuthSettings(session_idle_ttl_seconds=600, session_absolute_ttl_seconds=600)


def test_password_bounds_must_not_invert() -> None:
    with pytest.raises(ValidationError, match="must exceed password_min_length"):
        AuthSettings(password_min_length=100, password_max_length=64)


@pytest.mark.parametrize(
    "overrides",
    [
        {"argon2_memory_kib": 1_024},
        {"argon2_parallelism": 0},
        {"argon2_parallelism": 17},
        {"argon2_salt_bytes": 8},
        {"password_min_length": 8},
        {"session_cache_ttl_seconds": 301},
        {"max_failed_logins": 2},
        {"api_key_max_ttl_days": 0},
        {"api_key_max_ttl_days": 4_000},
        {"lockout_seconds": 30},
    ],
)
def test_field_bounds_are_enforced(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        AuthSettings(**overrides)


def test_relaxed_local_configuration_is_valid_outside_production() -> None:
    settings = Settings(_env_file=None, environment=Environment.DEVELOPMENT, auth=relaxed_auth())
    assert settings.auth.cookie_secure is False


def test_a_correct_production_configuration_is_accepted() -> None:
    settings = production_settings()
    assert settings.is_production
    assert settings.auth.cookie_secure is True


def test_production_refuses_a_non_secure_session_cookie() -> None:
    with pytest.raises(ValidationError, match="cookie_secure must be enabled in production"):
        production_settings(auth=relaxed_auth())


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"session_cookie_name": "oc_session"}, "session_cookie_name must use"),
        ({"csrf_cookie_name": "oc_csrf"}, "csrf_cookie_name must use"),
    ],
)
def test_production_requires_host_prefixed_cookies(
    overrides: dict[str, Any],
    expected: str,
) -> None:
    # Secure stays on, so AuthSettings itself is happy; only the production
    # gate catches the missing prefix.
    with pytest.raises(ValidationError, match=expected):
        production_settings(auth=AuthSettings(**overrides))


def test_production_refuses_cheap_password_hashing() -> None:
    # The exact configuration used by the unit tests in this suite. It must be
    # impossible to deploy.
    with pytest.raises(ValidationError, match="argon2_memory_kib is below"):
        production_settings(
            auth=AuthSettings(argon2_memory_kib=8_192, argon2_time_cost=PRODUCTION_ARGON2_TIME_COST)
        )


def test_production_refuses_a_reduced_time_cost() -> None:
    with pytest.raises(ValidationError, match="argon2_time_cost is below"):
        production_settings(auth=AuthSettings(argon2_time_cost=1))


def test_production_reports_every_problem_at_once() -> None:
    # One restart per misconfiguration is how a rushed deployment ends up
    # fixing the first three and shipping the fourth.
    with pytest.raises(ValidationError) as caught:
        production_settings(auth=relaxed_auth(argon2_memory_kib=8_192, argon2_time_cost=1))
    rendered = str(caught.value)
    assert "cookie_secure" in rendered
    assert "session_cookie_name" in rendered
    assert "argon2_memory_kib" in rendered
    assert "argon2_time_cost" in rendered


def test_auth_settings_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OC_AUTH__MAX_FAILED_LOGINS", "5")
    monkeypatch.setenv("OC_AUTH__SESSION_IDLE_TTL_SECONDS", "900")
    settings = Settings(_env_file=None)
    assert settings.auth.max_failed_logins == 5
    assert settings.auth.session_idle_ttl_seconds == 900


def test_no_credential_material_is_configurable() -> None:
    # Nothing in AuthSettings may hold a key, pepper or shared secret: those
    # would end up in .env.example, in a container image, and in a ticket.
    forbidden = ("secret", "pepper", "private_key", "salt_value", "password")
    for name in AuthSettings.model_fields:
        assert not any(marker in name for marker in forbidden if marker != "password")
        if "password" in name:
            assert name in {"password_min_length", "password_max_length"}
