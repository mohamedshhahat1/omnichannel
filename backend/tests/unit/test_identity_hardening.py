"""Unit coverage for the Phase 3 hardening controls.

These are the parts that can be tested without PostgreSQL: the origin rule, the
breach screen, the rate limiter and the credential-ambiguity check are all pure
functions over headers, strings or a counter. The end-to-end behaviour lives in
`tests/integration/test_identity_hardening_api.py`.

The last two tests in this file are structural rather than behavioural. They
exist because `docs/security.md` 3.2 makes PostgreSQL authoritative for runtime
authorization, and the cheapest way to break that guarantee is not a bug - it
is somebody importing the old Python grant table back into a service because it
is convenient. A structural check catches that in review; a behavioural test
alone would not, because such an import usually produces the *right* answer
right up until a grant is changed in the database.
"""

import ast
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.requests import Request

import app as app_package
from app.core.breached_passwords import BreachedPasswordScreen, is_breached
from app.core.origins import OriginDecision, evaluate_origin, normalize_origin
from app.core.rate_limit import (
    FixedWindowRateLimiter,
    InMemoryFixedWindowRateLimiter,
    bucket_digest,
)
from app.core.settings import AuthSettings, Environment, Settings
from app.modules.identity import domain
from app.modules.identity.api.dependencies import reject_ambiguous_credentials
from app.modules.identity.domain import Permission
from app.modules.identity.errors import AmbiguousCredentialsError
from app.modules.identity.repositories import MembershipRepository
from app.modules.identity.services.permissions import (
    EffectivePermissionCache,
    PermissionResolver,
)

ALLOWED_ORIGINS = ("https://app.example.com", "https://admin.example.com:8443")


def _evaluate(**overrides: Any) -> OriginDecision:
    """Evaluate an origin with permissive defaults, overriding one thing at a time."""
    arguments: dict[str, Any] = {
        "origin_header": None,
        "referer_header": None,
        "host_header": "app.example.com",
        "allowed_origins": ALLOWED_ORIGINS,
        "require_present": False,
        "require_https": False,
    }
    arguments.update(overrides)
    return evaluate_origin(**arguments)


# --------------------------------------------------------------------------
# Origin and Referer validation
# --------------------------------------------------------------------------


def test_an_origin_is_normalised_to_scheme_host_and_non_default_port() -> None:
    assert normalize_origin("HTTPS://App.Example.com:443/") == "https://app.example.com"
    assert normalize_origin("http://example.com:80") == "http://example.com"
    assert normalize_origin("https://example.com:8443") == "https://example.com:8443"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        "null",
        "NULL",
        "file:///etc/passwd",
        "data:text/html,<script>",
        "not a url",
        "http://[::1",
        "http://example.com:99999",
    ],
)
def test_an_unusable_origin_normalises_to_none(value: str | None) -> None:
    """Anything we cannot parse is refused, never treated as a wildcard.

    `null` matters most: it is what a sandboxed iframe or a `data:` document
    sends, which is exactly the context an attacker controls.
    """
    assert normalize_origin(value) is None


def test_an_allowlisted_origin_is_accepted() -> None:
    decision = _evaluate(origin_header="https://app.example.com")
    assert decision.allowed
    assert decision.reason == "allowlisted"


def test_a_foreign_origin_is_refused() -> None:
    decision = _evaluate(origin_header="https://evil.example")
    assert not decision.allowed
    assert decision.origin == "https://evil.example"


def test_the_applications_own_host_is_accepted_without_configuration() -> None:
    """A single-origin deployment needs no allowlist entry for itself.

    Safe because a browser writes `Origin` from the page that made the request:
    an attacker's page cannot claim to be us.
    """
    decision = _evaluate(origin_header="http://testserver", host_header="testserver")
    assert decision.allowed
    assert decision.reason == "same_origin"


def test_a_different_port_on_the_same_host_is_not_the_same_origin() -> None:
    decision = _evaluate(origin_header="https://app.example.com:8443")
    assert not decision.allowed


def test_referer_is_used_when_origin_is_absent() -> None:
    decision = _evaluate(referer_header="https://app.example.com/settings/keys")
    assert decision.allowed
    assert decision.origin == "https://app.example.com"


def test_origin_wins_over_referer_when_both_are_present() -> None:
    decision = _evaluate(
        origin_header="https://evil.example",
        referer_header="https://app.example.com/",
    )
    assert not decision.allowed


def test_a_missing_origin_passes_only_when_presence_is_not_required() -> None:
    assert _evaluate().allowed
    strict = _evaluate(require_present=True)
    assert not strict.allowed
    assert strict.reason == "origin_and_referer_absent"


def test_a_null_origin_is_refused_even_when_presence_is_not_required() -> None:
    decision = _evaluate(origin_header="null")
    assert not decision.allowed
    assert decision.reason == "unparsable_origin"


def test_an_insecure_origin_is_refused_when_https_is_required() -> None:
    decision = _evaluate(
        origin_header="http://app.example.com",
        allowed_origins=("http://app.example.com",),
        require_https=True,
    )
    assert not decision.allowed
    assert decision.reason == "insecure_origin_scheme"


# --------------------------------------------------------------------------
# Breached-password screening
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password",
    ["password1234", "Password1234", "  letmein12345  ", "QWERTYUIOP123", "welcome123456"],
)
def test_a_known_breached_password_is_rejected(password: str) -> None:
    """Case and surrounding whitespace do not launder a breached password.

    Each of these clears the twelve-character minimum, which is exactly why the
    length policy alone is not enough.
    """
    assert is_breached(password)


@pytest.mark.parametrize(
    "password",
    [
        "correct horse battery staple",
        "the quiet ferry leaves at nine",
        "Zt7!qmvR2wsLdA",
        "",
    ],
)
def test_a_reasonable_password_is_not_flagged(password: str) -> None:
    """Multi-word passphrases must survive the screen.

    The first case is the suite's own password and is deliberately pinned: if
    the normaliser ever starts collapsing internal whitespace, this passphrase
    would fold onto a concatenated corpus entry and every registration in the
    integration suite would begin failing for a reason unrelated to the code
    under test.
    """
    assert not is_breached(password)


def test_the_corpus_can_be_extended_without_touching_a_call_site() -> None:
    screen = BreachedPasswordScreen({"Locally Known Leak 2026"})
    assert screen.is_breached("locally known leak 2026")
    # The default screen is unaffected by another screen's corpus.
    assert not is_breached("locally known leak 2026")


# --------------------------------------------------------------------------
# Rate limiting
# --------------------------------------------------------------------------


class _FakeRedis:
    """The three commands the limiter uses, and nothing else."""

    def __init__(self) -> None:
        self.counters: dict[str, int] = {}
        self.expiries: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expiries[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.expiries.get(key, -1)


class _BrokenRedis:
    """A Redis that is unreachable, the way an outage looks from here."""

    async def incr(self, key: str) -> int:
        raise RedisError("connection refused")

    async def expire(self, key: str, seconds: int) -> bool:
        raise RedisError("connection refused")

    async def ttl(self, key: str) -> int:
        raise RedisError("connection refused")


def _limiter(redis: object | None, **overrides: Any) -> FixedWindowRateLimiter:
    arguments: dict[str, Any] = {
        "environment": "test",
        "purpose": "login",
        "limit": 3,
        "window_seconds": 60,
        "fail_open": True,
    }
    arguments.update(overrides)
    return FixedWindowRateLimiter(cast(Redis | None, redis), **arguments)


def test_a_bucket_never_appears_in_a_key_in_the_clear() -> None:
    """Source addresses are personal data and must not sit in Redis in plaintext."""
    digest = bucket_digest("203.0.113.5")
    assert "203.0.113.5" not in digest
    assert len(digest) == 32
    assert digest == bucket_digest("203.0.113.5")
    assert digest != bucket_digest("203.0.113.6")


async def test_attempts_are_allowed_up_to_the_limit_and_then_refused() -> None:
    limiter = _limiter(_FakeRedis())
    for _ in range(3):
        assert (await limiter.hit("203.0.113.5")).allowed
    refused = await limiter.hit("203.0.113.5")
    assert not refused.allowed
    assert refused.retry_after_seconds >= 1


async def test_each_source_address_has_its_own_budget() -> None:
    limiter = _limiter(_FakeRedis())
    for _ in range(4):
        await limiter.hit("203.0.113.5")
    assert (await limiter.hit("198.51.100.7")).allowed


async def test_a_limiter_with_no_backend_reports_that_it_did_not_count() -> None:
    """No Redis means no shared counter - say so rather than imply enforcement."""
    decision = await _limiter(None).hit("203.0.113.5")
    assert decision.allowed
    assert decision.degraded


async def test_a_redis_outage_fails_open_by_default() -> None:
    """The per-account lockout still applies, so this degrades rather than fails."""
    decision = await _limiter(_BrokenRedis()).hit("203.0.113.5")
    assert decision.allowed
    assert decision.degraded


async def test_a_redis_outage_can_be_configured_to_fail_closed() -> None:
    decision = await _limiter(_BrokenRedis(), fail_open=False).hit("203.0.113.5")
    assert not decision.allowed
    assert decision.degraded
    assert decision.retry_after_seconds > 0


async def test_the_window_resets_once_it_has_elapsed() -> None:
    now = [1_000.0]
    limiter = InMemoryFixedWindowRateLimiter(
        limit=2,
        window_seconds=60,
        clock=lambda: now[0],
    )
    assert (await limiter.hit("203.0.113.5")).allowed
    assert (await limiter.hit("203.0.113.5")).allowed
    assert not (await limiter.hit("203.0.113.5")).allowed

    now[0] += 61
    assert (await limiter.hit("203.0.113.5")).allowed


# --------------------------------------------------------------------------
# Credential ambiguity
# --------------------------------------------------------------------------


def _settings() -> Settings:
    auth = AuthSettings(
        cookie_secure=False,
        session_cookie_name="oc_session",
        csrf_cookie_name="oc_csrf",
    )
    return Settings(_env_file=None, environment=Environment.TEST, auth=auth)


def _request(**headers: str) -> Request:
    raw = [(key.replace("_", "-").lower().encode(), value.encode()) for key, value in headers.items()]
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/api-keys",
            "raw_path": b"/api/v1/api-keys",
            "query_string": b"",
            "root_path": "",
            "headers": raw,
            "client": ("203.0.113.5", 54_321),
            "server": ("testserver", 80),
        }
    )


def test_a_single_credential_is_never_ambiguous() -> None:
    settings = _settings()
    reject_ambiguous_credentials(_request(), settings)
    reject_ambiguous_credentials(_request(cookie="oc_session=abc"), settings)
    reject_ambiguous_credentials(_request(authorization="Bearer oc_test_key"), settings)


def test_a_cookie_and_a_bearer_together_are_refused() -> None:
    with pytest.raises(AmbiguousCredentialsError):
        reject_ambiguous_credentials(
            _request(cookie="oc_session=abc", authorization="Bearer oc_test_key"),
            _settings(),
        )


def test_an_empty_bearer_value_is_not_a_second_credential() -> None:
    """`Authorization: Bearer` with nothing after it presents no key."""
    reject_ambiguous_credentials(
        _request(cookie="oc_session=abc", authorization="Bearer   "),
        _settings(),
    )


def test_an_unrelated_cookie_is_not_a_session_credential() -> None:
    reject_ambiguous_credentials(
        _request(cookie="theme=dark", authorization="Bearer oc_test_key"),
        _settings(),
    )


def test_the_refusal_names_a_stable_error_code() -> None:
    """Clients branch on the code; it is part of the contract."""
    assert AmbiguousCredentialsError.default_code == "ambiguous_credentials"


# --------------------------------------------------------------------------
# The static grant table is not an authorization source
# --------------------------------------------------------------------------

_FORBIDDEN_SYMBOLS = frozenset({"DEFAULT_ROLE_GRANTS", "permissions_for_roles"})
_DEFINITION_SITE = Path("modules/identity/domain.py")


def test_no_runtime_module_references_the_static_grant_table() -> None:
    """Nothing under `app/` may name the Python grant table except its home.

    `DEFAULT_ROLE_GRANTS` and `permissions_for_roles()` are kept deliberately -
    migration 0002 seeds `role_permissions` from them, and they are the
    reviewable statement of what the six system roles are meant to grant. What
    must never come back is a *runtime* read: the moment a service imports
    either one, authorization stops following the database and the Phase 3 RBAC
    correction is silently undone. Because such an import usually returns the
    right answer at first, a behavioural test would not notice; this one fails
    the moment the import appears.

    Migrations live outside `app/` and are unaffected, which is the intent.
    """
    root = Path(app_package.__file__).resolve().parent
    offenders: list[str] = []

    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root)
        if relative == _DEFINITION_SITE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                offenders.extend(
                    f"{relative}:{node.lineno} imports {alias.name}"
                    for alias in node.names
                    if alias.name in _FORBIDDEN_SYMBOLS
                )
            elif isinstance(node, ast.Name) and node.id in _FORBIDDEN_SYMBOLS:
                offenders.append(f"{relative}:{node.lineno} uses {node.id}")
            elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_SYMBOLS:
                offenders.append(f"{relative}:{node.lineno} uses {node.attr}")

    assert offenders == [], (
        "runtime authorization must resolve from role_permissions in PostgreSQL; "
        f"static grant table referenced in: {offenders}"
    )


class _FakeMembershipRepository:
    """Stands in for the database with a fixed answer."""

    def __init__(self, roles: set[str], permissions: set[str]) -> None:
        self.tenant_id = uuid.uuid4()
        self._roles = frozenset(roles)
        self._permissions = frozenset(permissions)
        self.reads = 0

    async def role_and_permission_slugs_for(
        self,
        membership_id: uuid.UUID,
    ) -> tuple[frozenset[str], frozenset[str]]:
        self.reads += 1
        return self._roles, self._permissions


async def test_permissions_resolve_from_the_database_even_if_the_grant_table_explodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The behavioural half of the same guarantee.

    `permissions_for_roles` is replaced with something that raises. If any part
    of the resolution path still consulted it - as a fallback, a cross-check or
    a default - this test would fail. It resolves, so the answer came from the
    repository, which is to say from `role_permissions`.

    Note that the permission granted here (`conversations.read` to `owner`) is
    not what the static table says an owner holds; the point is that the
    resolver reports what the database says, not what Python believes.
    """

    def _explode(*args: Any, **kwargs: Any) -> frozenset[Permission]:
        raise AssertionError("runtime authorization must not call permissions_for_roles()")

    monkeypatch.setattr(domain, "permissions_for_roles", _explode)

    repository = _FakeMembershipRepository(
        roles={"owner"},
        permissions={Permission.CONVERSATIONS_READ.value},
    )
    resolver = PermissionResolver(
        cast(MembershipRepository, repository),
        EffectivePermissionCache(None, environment="test", ttl_seconds=60),
    )

    permissions = await resolver.permissions_for(uuid.uuid4())

    assert permissions == frozenset({Permission.CONVERSATIONS_READ})
    assert Permission.BILLING_MANAGE not in permissions
    assert repository.reads == 1
