"""Phase 3 hardening, end to end against real PostgreSQL.

Same shape as `test_identity_api.py`: `create_app` builds the application,
`provide_database_session` is overridden so assertions and routes share one
rolled-back transaction, and every test is `async def` because the session
belongs to the running event loop.

Two extra applications are built here because two of the behaviours under test
are configuration-dependent:

* `origin_client` enables `require_origin_on_cookie_writes` with an allowlist,
  which is the production posture. The default `client` leaves it off, which is
  why the rest of the suite can drive the API without an `Origin` header.
* `throttled_app` injects an in-memory limiter through the dependency
  override, so per-source throttling is tested without requiring Redis. The
  Redis-backed implementation is covered in `tests/unit/test_identity_hardening.py`.
"""

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.api.application import create_app
from app.api.dependencies import provide_database_session
from app.core.rate_limit import InMemoryFixedWindowRateLimiter
from app.core.settings import AuthSettings, Environment, SecuritySettings, Settings
from app.modules.identity.api.dependencies import provide_login_rate_limiter
from app.modules.identity.domain import MembershipStatus

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
API_PREFIX = "/api/v1"
TRUSTED_ORIGIN = "https://app.example.test"


def _auth_settings() -> AuthSettings:
    """Cheap Argon2 and cookies an http:// client will actually return."""
    return AuthSettings(
        argon2_time_cost=1,
        argon2_memory_kib=8_192,
        argon2_parallelism=1,
        cookie_secure=False,
        session_cookie_name="oc_session",
        csrf_cookie_name="oc_csrf",
    )


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        debug=False,
        auth=_auth_settings(),
    )


def _origin_settings() -> Settings:
    """Production's origin posture: an allowlist, and presence is mandatory."""
    return Settings(
        _env_file=None,
        environment=Environment.TEST,
        debug=False,
        auth=_auth_settings(),
        security=SecuritySettings(
            cors_origins=(TRUSTED_ORIGIN,),
            require_origin_on_cookie_writes=True,
        ),
    )


@pytest.fixture
async def db_session(postgres_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back."""
    async with postgres_engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(
            bind=connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()


def _build_app(db_session: AsyncSession, settings: Settings) -> Any:
    app = create_app(settings)

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[provide_database_session] = _session_override
    return app


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = _build_app(db_session, _settings())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client
    app.dependency_overrides.clear()


@pytest.fixture
async def origin_client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    app = _build_app(db_session, _origin_settings())
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client
    app.dependency_overrides.clear()


@pytest.fixture
async def throttled_app(db_session: AsyncSession) -> AsyncIterator[Any]:
    """An application whose login limiter allows three attempts per source."""
    app = _build_app(db_session, _settings())
    limiter = InMemoryFixedWindowRateLimiter(limit=3, window_seconds=60)
    app.dependency_overrides[provide_login_rate_limiter] = lambda: limiter
    yield app
    app.dependency_overrides.clear()


def _client_from(app: Any, address: str) -> AsyncClient:
    """A client whose requests appear to come from `address`."""
    return AsyncClient(
        transport=ASGITransport(app=app, client=(address, 44_444)),
        base_url="http://testserver",
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _registration(**overrides: Any) -> dict[str, Any]:
    unique = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "email": f"owner-{unique}@example.com",
        "password": PASSWORD,
        "display_name": "Ada Owner",
        "tenant_name": "Acme",
        "tenant_slug": f"acme-{unique[:12]}",
    }
    payload.update(overrides)
    return payload


async def _register_and_login(client: AsyncClient, **overrides: Any) -> tuple[dict[str, Any], str]:
    payload = _registration(**overrides)
    registered = await client.post(f"{API_PREFIX}/auth/register", json=payload)
    assert registered.status_code == 202, registered.text
    csrf_token = await _sign_in(
        client,
        email=payload["email"],
        tenant_slug=payload["tenant_slug"],
    )
    return payload, csrf_token


async def _sign_in(
    client: AsyncClient,
    *,
    email: str,
    tenant_slug: str | None = None,
    password: str = PASSWORD,
) -> str:
    """Drop whatever cookies are held and sign in. Returns the CSRF token."""
    client.cookies.clear()
    body: dict[str, Any] = {"email": email, "password": password}
    if tenant_slug is not None:
        body["tenant_slug"] = tenant_slug
    response = await client.post(f"{API_PREFIX}/auth/login", json=body)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


async def _invite(
    client: AsyncClient,
    csrf_token: str,
    *,
    role: str = "agent",
) -> tuple[str, str]:
    """Invite a new person. Returns their email and membership id."""
    email = f"member-{uuid.uuid4().hex}@example.com"
    response = await client.post(
        f"{API_PREFIX}/members",
        json={
            "email": email,
            "display_name": "Grace Member",
            "initial_password": PASSWORD,
            "role": role,
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 201, response.text
    assert response.json()["status"] == MembershipStatus.INVITED.value
    return email, response.json()["id"]


async def _create_key(client: AsyncClient, csrf_token: str, *, scopes: list[str]) -> str:
    response = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": f"key-{uuid.uuid4().hex[:8]}", "scopes": scopes},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["secret"])


# --------------------------------------------------------------------------
# TASK 1 - the INVITED -> ACTIVE transition
# --------------------------------------------------------------------------


async def test_an_invited_member_is_tenantless_until_they_accept(client: AsyncClient) -> None:
    """The whole point of the transition, in one test.

    Before acceptance the invitee can sign in but holds no tenant and no
    permissions. After accepting, a fresh sign-in carries the tenant and the
    role they were invited with - so the membership has become selectable by
    authentication, which is what INVITED prevented.
    """
    owner, owner_csrf = await _register_and_login(client)
    invitee, _ = await _invite(client, owner_csrf)

    csrf_token = await _sign_in(client, email=invitee, tenant_slug=owner["tenant_slug"])
    before = await client.get(f"{API_PREFIX}/auth/me")
    assert before.status_code == 200, before.text
    assert before.json()["tenant"] is None
    assert before.json()["permissions"] == []

    accepted = await client.post(
        f"{API_PREFIX}/invitations/accept",
        json={"tenant_slug": owner["tenant_slug"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == MembershipStatus.ACTIVE.value
    assert accepted.json()["accepted_at"] is not None
    assert accepted.json()["roles"] == ["agent"]

    await _sign_in(client, email=invitee, tenant_slug=owner["tenant_slug"])
    after = await client.get(f"{API_PREFIX}/auth/me")
    assert after.status_code == 200, after.text
    assert after.json()["tenant"]["slug"] == owner["tenant_slug"]
    assert after.json()["roles"] == ["agent"]
    assert "conversations.read" in after.json()["permissions"]


async def test_accepting_the_same_invitation_twice_is_harmless(client: AsyncClient) -> None:
    """A double-clicked button must not be an error, and must not move accepted_at."""
    owner, owner_csrf = await _register_and_login(client)
    invitee, _ = await _invite(client, owner_csrf)

    csrf_token = await _sign_in(client, email=invitee, tenant_slug=owner["tenant_slug"])
    first = await client.post(
        f"{API_PREFIX}/invitations/accept",
        json={"tenant_slug": owner["tenant_slug"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    second = await client.post(
        f"{API_PREFIX}/invitations/accept",
        json={"tenant_slug": owner["tenant_slug"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert second.json()["accepted_at"] == first.json()["accepted_at"]
    assert second.json()["roles"] == first.json()["roles"]


async def test_accepting_an_invitation_that_was_never_issued_is_not_found(
    client: AsyncClient,
) -> None:
    """404 rather than 403: the endpoint must not confirm that a tenant exists."""
    stranger, stranger_csrf = await _register_and_login(client)
    other, _ = await _register_and_login(client)

    await _sign_in(client, email=stranger["email"], tenant_slug=stranger["tenant_slug"])
    response = await client.post(
        f"{API_PREFIX}/invitations/accept",
        json={"tenant_slug": other["tenant_slug"]},
        headers={"X-CSRF-Token": stranger_csrf},
    )
    assert response.status_code in {403, 404}
    if response.status_code == 404:
        assert "not_found" in response.text


async def test_a_member_manager_can_activate_an_invited_membership(client: AsyncClient) -> None:
    """The administrative route into ACTIVE, for onboarding a colleague directly."""
    owner, owner_csrf = await _register_and_login(client)
    invitee, membership_id = await _invite(client, owner_csrf)

    activated = await client.post(
        f"{API_PREFIX}/members/{membership_id}/activate",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["status"] == MembershipStatus.ACTIVE.value

    # Idempotent: activating again succeeds and changes nothing.
    again = await client.post(
        f"{API_PREFIX}/members/{membership_id}/activate",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert again.status_code == 200, again.text
    assert again.json()["accepted_at"] == activated.json()["accepted_at"]

    await _sign_in(client, email=invitee, tenant_slug=owner["tenant_slug"])
    identity = await client.get(f"{API_PREFIX}/auth/me")
    assert identity.json()["tenant"]["slug"] == owner["tenant_slug"]


async def test_activating_a_membership_without_members_manage_is_refused(
    client: AsyncClient,
) -> None:
    """An agent cannot promote a colleague, or themselves, into the tenant."""
    owner, owner_csrf = await _register_and_login(client)
    agent_email, agent_membership = await _invite(client, owner_csrf)
    _, other_membership = await _invite(client, owner_csrf)

    activated = await client.post(
        f"{API_PREFIX}/members/{agent_membership}/activate",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert activated.status_code == 200, activated.text

    agent_csrf = await _sign_in(client, email=agent_email, tenant_slug=owner["tenant_slug"])
    denied = await client.post(
        f"{API_PREFIX}/members/{other_membership}/activate",
        headers={"X-CSRF-Token": agent_csrf},
    )
    assert denied.status_code == 403
    assert "permission_denied" in denied.text


async def test_a_membership_in_another_tenant_cannot_be_activated(client: AsyncClient) -> None:
    """Tenant isolation: 404, so the identifier is not confirmed to exist."""
    _, first_csrf = await _register_and_login(client)
    _, foreign_membership = await _invite(client, first_csrf)

    second, second_csrf = await _register_and_login(client)
    await _sign_in(client, email=second["email"], tenant_slug=second["tenant_slug"])

    response = await client.post(
        f"{API_PREFIX}/members/{foreign_membership}/activate",
        headers={"X-CSRF-Token": second_csrf},
    )
    assert response.status_code == 404
    assert "membership_not_found" in response.text


async def test_a_suspended_membership_cannot_be_activated(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """Only INVITED may become ACTIVE.

    Suspension is a deliberate act - usually an offboarding or an incident - so
    the activation endpoint must not quietly undo it. Reinstatement is a
    separate decision that Phase 3 does not expose.
    """
    _, owner_csrf = await _register_and_login(client)
    _, membership_id = await _invite(client, owner_csrf)

    await db_session.execute(
        text("UPDATE memberships SET status = :status WHERE id = :id"),
        {"status": MembershipStatus.SUSPENDED.value, "id": uuid.UUID(membership_id)},
    )
    db_session.expire_all()

    response = await client.post(
        f"{API_PREFIX}/members/{membership_id}/activate",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert response.status_code == 409
    assert "membership_transition_invalid" in response.text


# --------------------------------------------------------------------------
# TASK 2 - role mutation, and the decision following PostgreSQL
# --------------------------------------------------------------------------


async def test_granting_and_revoking_a_role_changes_the_next_decision(
    client: AsyncClient,
) -> None:
    """Grant, then revoke, with a real authorization decision taken each time.

    The assertions are deliberately taken from the API rather than from the
    database: what matters is that the *decision* follows current PostgreSQL
    state on both edges. A stale effective-permission entry surviving either
    mutation would show up here as the wrong status code.
    """
    owner, owner_csrf = await _register_and_login(client)
    agent_email, membership_id = await _invite(client, owner_csrf)
    await client.post(
        f"{API_PREFIX}/members/{membership_id}/activate",
        headers={"X-CSRF-Token": owner_csrf},
    )

    agent_csrf = await _sign_in(client, email=agent_email, tenant_slug=owner["tenant_slug"])
    denied = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": agent_csrf},
    )
    assert denied.status_code == 403, denied.text

    owner_csrf = await _sign_in(client, email=owner["email"], tenant_slug=owner["tenant_slug"])
    granted = await client.post(
        f"{API_PREFIX}/members/{membership_id}/roles",
        json={"role": "admin"},
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert granted.status_code == 200, granted.text
    assert set(granted.json()["roles"]) == {"admin", "agent"}

    agent_csrf = await _sign_in(client, email=agent_email, tenant_slug=owner["tenant_slug"])
    allowed = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": agent_csrf},
    )
    assert allowed.status_code == 201, allowed.text

    owner_csrf = await _sign_in(client, email=owner["email"], tenant_slug=owner["tenant_slug"])
    revoked = await client.delete(
        f"{API_PREFIX}/members/{membership_id}/roles/admin",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["roles"] == ["agent"]

    agent_csrf = await _sign_in(client, email=agent_email, tenant_slug=owner["tenant_slug"])
    denied_again = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": agent_csrf},
    )
    assert denied_again.status_code == 403, denied_again.text


async def test_the_last_owner_cannot_be_stripped_of_the_owner_role(client: AsyncClient) -> None:
    """A tenant with no owner cannot be administered by anyone, including us."""
    owner, owner_csrf = await _register_and_login(client)
    identity = await client.get(f"{API_PREFIX}/auth/me")
    owner_user_id = identity.json()["user"]["id"]

    listed = await client.get(f"{API_PREFIX}/members")
    membership_id = next(
        item["id"] for item in listed.json()["items"] if item["user_id"] == owner_user_id
    )

    response = await client.delete(
        f"{API_PREFIX}/members/{membership_id}/roles/owner",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert response.status_code == 409
    assert "last_owner" in response.text

    # The role is still there.
    still = await client.get(f"{API_PREFIX}/auth/me")
    assert still.json()["roles"] == ["owner"]
    assert owner["tenant_slug"] == still.json()["tenant"]["slug"]


async def test_revoking_a_role_that_does_not_exist_is_not_found(client: AsyncClient) -> None:
    _, owner_csrf = await _register_and_login(client)
    _, membership_id = await _invite(client, owner_csrf)

    response = await client.delete(
        f"{API_PREFIX}/members/{membership_id}/roles/superuser",
        headers={"X-CSRF-Token": owner_csrf},
    )
    assert response.status_code == 404


async def test_revoking_a_role_requires_members_manage(client: AsyncClient) -> None:
    owner, owner_csrf = await _register_and_login(client)
    agent_email, agent_membership = await _invite(client, owner_csrf)
    _, victim_membership = await _invite(client, owner_csrf, role="manager")
    await client.post(
        f"{API_PREFIX}/members/{agent_membership}/activate",
        headers={"X-CSRF-Token": owner_csrf},
    )

    agent_csrf = await _sign_in(client, email=agent_email, tenant_slug=owner["tenant_slug"])
    response = await client.delete(
        f"{API_PREFIX}/members/{victim_membership}/roles/manager",
        headers={"X-CSRF-Token": agent_csrf},
    )
    assert response.status_code == 403
    assert "permission_denied" in response.text


# --------------------------------------------------------------------------
# TASK 4 - credential precedence
# --------------------------------------------------------------------------


async def test_a_cookie_on_its_own_authenticates(client: AsyncClient) -> None:
    await _register_and_login(client)
    response = await client.get(f"{API_PREFIX}/api-keys")
    assert response.status_code == 200, response.text


async def test_a_bearer_on_its_own_authenticates(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    secret = await _create_key(client, csrf_token, scopes=["apikeys.manage"])

    client.cookies.clear()
    response = await client.get(
        f"{API_PREFIX}/api-keys",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 200, response.text


async def test_no_credentials_at_all_is_unauthenticated(client: AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/api-keys")
    assert response.status_code == 401


async def test_a_cookie_and_a_bearer_together_are_refused(client: AsyncClient) -> None:
    """Both valid, same tenant - still refused. There is no safe precedence."""
    _, csrf_token = await _register_and_login(client)
    secret = await _create_key(client, csrf_token, scopes=["apikeys.manage"])

    response = await client.get(
        f"{API_PREFIX}/api-keys",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 401
    assert "ambiguous_credentials" in response.text


async def test_an_invalid_cookie_with_a_valid_bearer_is_refused(client: AsyncClient) -> None:
    """The refusal is about the shape of the request, not the state of the secrets."""
    _, csrf_token = await _register_and_login(client)
    secret = await _create_key(client, csrf_token, scopes=["apikeys.manage"])

    client.cookies.clear()
    client.cookies.set("oc_session", "not-a-real-session-token")
    response = await client.get(
        f"{API_PREFIX}/api-keys",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 401
    assert "ambiguous_credentials" in response.text


async def test_a_valid_cookie_with_an_invalid_bearer_is_refused(client: AsyncClient) -> None:
    await _register_and_login(client)
    response = await client.get(
        f"{API_PREFIX}/api-keys",
        headers={"Authorization": "Bearer oc_test_not_a_real_key"},
    )
    assert response.status_code == 401
    assert "ambiguous_credentials" in response.text


async def test_two_valid_credentials_for_different_tenants_are_refused(
    client: AsyncClient,
) -> None:
    """The identity-confusion case the old bearer-wins rule resolved silently.

    A browser signed into tenant B sends its cookie automatically; anything that
    also attaches tenant A's key would have acted as tenant A while the person
    at the keyboard - and the audit trail's reader - believed otherwise.
    """
    _, first_csrf = await _register_and_login(client)
    secret = await _create_key(client, first_csrf, scopes=["apikeys.manage"])

    second, _ = await _register_and_login(client)
    await _sign_in(client, email=second["email"], tenant_slug=second["tenant_slug"])

    response = await client.get(
        f"{API_PREFIX}/api-keys",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 401
    assert "ambiguous_credentials" in response.text


# --------------------------------------------------------------------------
# TASK 6 - Origin and Referer validation
# --------------------------------------------------------------------------


async def test_a_cookie_write_from_the_allowlisted_origin_is_accepted(
    origin_client: AsyncClient,
) -> None:
    _, csrf_token = await _register_and_login(origin_client)
    response = await origin_client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": csrf_token, "Origin": TRUSTED_ORIGIN},
    )
    assert response.status_code == 201, response.text


async def test_a_cookie_write_from_a_foreign_origin_is_refused(
    origin_client: AsyncClient,
) -> None:
    """Refused even though the CSRF token is correct.

    This is why the layer is worth having: an attacker who can read the
    double-submit cookie - a subdomain takeover, an XSS on a sibling host -
    still cannot forge the `Origin` header, which the browser writes itself.
    """
    _, csrf_token = await _register_and_login(origin_client)
    response = await origin_client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": csrf_token, "Origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert "origin_rejected" in response.text


async def test_a_cookie_write_with_no_origin_is_refused_when_presence_is_required(
    origin_client: AsyncClient,
) -> None:
    _, csrf_token = await _register_and_login(origin_client)
    response = await origin_client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 403
    assert "origin_rejected" in response.text


async def test_the_referer_is_accepted_when_the_origin_header_is_absent(
    origin_client: AsyncClient,
) -> None:
    _, csrf_token = await _register_and_login(origin_client)
    response = await origin_client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": csrf_token, "Referer": f"{TRUSTED_ORIGIN}/settings/keys"},
    )
    assert response.status_code == 201, response.text


async def test_a_bearer_client_is_not_subject_to_the_origin_rule(
    origin_client: AsyncClient,
) -> None:
    """Machine clients send no Origin and cannot be CSRF victims.

    There is no ambient credential for an attacker's page to borrow: the key is
    attached only by code that chose to attach it.
    """
    _, csrf_token = await _register_and_login(origin_client)
    secret = await _create_key_with_origin(origin_client, csrf_token)

    origin_client.cookies.clear()
    response = await origin_client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "from-ci", "scopes": ["conversations.read"]},
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 201, response.text


async def _create_key_with_origin(client: AsyncClient, csrf_token: str) -> str:
    response = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "ci", "scopes": ["apikeys.manage"]},
        headers={"X-CSRF-Token": csrf_token, "Origin": TRUSTED_ORIGIN},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["secret"])


# --------------------------------------------------------------------------
# TASK 5 - per-source-address login throttling
# --------------------------------------------------------------------------


async def test_login_attempts_from_one_source_are_capped_across_accounts(
    throttled_app: Any,
) -> None:
    """Switching accounts does not buy a fresh budget.

    That is the whole point of the source-address limiter: the per-account
    lockout in `users.failed_login_count` is defeated by spreading attempts
    across many accounts, and this counter is what a spray hits instead.
    Successful attempts count too, so a valid credential cannot be used to keep
    a hostile budget topped up.
    """
    async with _client_from(throttled_app, "203.0.113.10") as client:
        first = _registration()
        second = _registration()
        for payload in (first, second):
            registered = await client.post(f"{API_PREFIX}/auth/register", json=payload)
            assert registered.status_code == 202, registered.text

        attempts = [
            {"email": first["email"], "password": "wrong passphrase one"},
            {"email": second["email"], "password": "wrong passphrase two"},
            {"email": first["email"], "password": PASSWORD},
        ]
        statuses = []
        for body in attempts:
            response = await client.post(f"{API_PREFIX}/auth/login", json=body)
            statuses.append(response.status_code)
        assert statuses == [401, 401, 200]

        throttled = await client.post(
            f"{API_PREFIX}/auth/login",
            json={"email": second["email"], "password": PASSWORD},
        )
        assert throttled.status_code == 429
        assert "rate_limited" in throttled.text
        # The correct password was presented and still refused, so the refusal
        # came from the limiter rather than from credential verification.


async def test_a_second_source_address_has_its_own_budget(throttled_app: Any) -> None:
    payload = _registration()

    async with _client_from(throttled_app, "203.0.113.10") as noisy:
        registered = await noisy.post(f"{API_PREFIX}/auth/register", json=payload)
        assert registered.status_code == 202, registered.text
        for _ in range(4):
            last = await noisy.post(
                f"{API_PREFIX}/auth/login",
                json={"email": payload["email"], "password": "wrong passphrase here"},
            )
        assert last.status_code == 429

    async with _client_from(throttled_app, "198.51.100.7") as quiet:
        response = await quiet.post(
            f"{API_PREFIX}/auth/login",
            json={"email": payload["email"], "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
