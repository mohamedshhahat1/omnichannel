"""Identity endpoints end-to-end against real PostgreSQL.

These exercise the HTTP surface: cookies, CSRF, the shared error envelope and
the rule that a key's plaintext appears exactly once, in the response that
created it, and nowhere else afterwards.

The application is built by `create_app`, but `ASGITransport` does not run the
lifespan, so startup never opens a second connection pool against
`settings.database.url`. That matters, because the assertions read from the
same rolled-back transaction the routes write to - which is why
`provide_database_session` is overridden instead. Everything the routes
actually need is wired by `create_app` rather than by startup: settings, the
Argon2 hasher and the health registry. `RoleSlugCache` degrades to a
PostgreSQL-only lookup when `app.state.redis` is absent, which is exactly the
case here.

Every test is `async def` on purpose. The session under test belongs to the
running event loop, and driving the app from a synchronous client would hand
an asyncpg connection to a second loop.
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
from app.core.settings import AuthSettings, Environment, Settings

pytestmark = pytest.mark.integration

PASSWORD = "correct horse battery staple"
API_PREFIX = "/api/v1"


def _settings() -> Settings:
    """Test settings that an HTTP client can actually hold cookies for.

    `__Host-` names and `Secure` are the production defaults and are asserted
    in `tests/unit/test_auth_settings.py`. Over plain `http://testserver` a
    Secure cookie is never sent back, so the flow would fail for a reason that
    has nothing to do with the code under test.
    """
    auth = AuthSettings(
        argon2_time_cost=1,
        argon2_memory_kib=8_192,
        argon2_parallelism=1,
        cookie_secure=False,
        session_cookie_name="oc_session",
        csrf_cookie_name="oc_csrf",
    )
    return Settings(_env_file=None, environment=Environment.TEST, debug=False, auth=auth)


@pytest.fixture
async def db_session(postgres_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session inside a transaction that is always rolled back.

    `create_savepoint` lets the routes call `commit()` for real while the
    outer transaction still discards everything at the end of the test.
    """
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


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[AsyncClient]:
    """An HTTP client bound to the test's own transaction."""
    app = create_app(_settings())

    async def _session_override() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[provide_database_session] = _session_override
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as http_client:
        yield http_client
    app.dependency_overrides.clear()


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
    """Register a tenant owner, sign in, and return the payload and CSRF token."""
    payload = _registration(**overrides)
    registered = await client.post(f"{API_PREFIX}/auth/register", json=payload)
    assert registered.status_code == 202, registered.text

    signed_in = await client.post(
        f"{API_PREFIX}/auth/login",
        json={
            "email": payload["email"],
            "password": payload["password"],
            "tenant_slug": payload["tenant_slug"],
        },
    )
    assert signed_in.status_code == 200, signed_in.text
    csrf_token = signed_in.json()["csrf_token"]
    assert isinstance(csrf_token, str)
    return payload, csrf_token


# --------------------------------------------------------------------------
# Registration and sign-in
# --------------------------------------------------------------------------


async def test_registration_then_sign_in_returns_the_owner_identity(client: AsyncClient) -> None:
    payload, _ = await _register_and_login(client)

    response = await client.get(f"{API_PREFIX}/auth/me")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["email"] == payload["email"]
    assert body["tenant"]["slug"] == payload["tenant_slug"]
    assert body["roles"] == ["owner"]
    assert "billing.manage" in body["permissions"]


async def test_registration_never_reveals_whether_the_address_is_taken(
    client: AsyncClient,
) -> None:
    payload = _registration()
    first = await client.post(f"{API_PREFIX}/auth/register", json=payload)
    second = await client.post(
        f"{API_PREFIX}/auth/register",
        json=_registration(email=payload["email"]),
    )
    # Same status and same body: the form is not an account-existence oracle.
    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json() == second.json()


async def test_a_taken_slug_is_reported_as_a_conflict(client: AsyncClient) -> None:
    payload = _registration()
    await client.post(f"{API_PREFIX}/auth/register", json=payload)
    clash = await client.post(
        f"{API_PREFIX}/auth/register",
        json=_registration(tenant_slug=payload["tenant_slug"]),
    )
    assert clash.status_code == 409
    assert "tenant_slug_taken" in clash.text


async def test_the_wrong_password_is_rejected_without_a_hint(client: AsyncClient) -> None:
    payload, _ = await _register_and_login(client)
    response = await client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": payload["email"], "password": "not the right passphrase"},
    )
    assert response.status_code == 401
    assert "authentication_failed" in response.text
    assert "not the right passphrase" not in response.text


async def test_an_unknown_address_is_rejected_identically(client: AsyncClient) -> None:
    known, _ = await _register_and_login(client)
    unknown = await client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": f"nobody-{uuid.uuid4().hex}@example.com", "password": PASSWORD},
    )
    wrong = await client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": known["email"], "password": "wrong passphrase entirely"},
    )
    assert unknown.status_code == wrong.status_code == 401
    assert unknown.json() == wrong.json()


async def test_a_short_password_is_refused_without_echoing_it(client: AsyncClient) -> None:
    response = await client.post(
        f"{API_PREFIX}/auth/register",
        json=_registration(password="short"),
    )
    assert response.status_code == 422
    assert "short" not in response.json().get("error", {}).get("message", "")


# --------------------------------------------------------------------------
# Session handling
# --------------------------------------------------------------------------


async def test_an_anonymous_caller_gets_401(client: AsyncClient) -> None:
    response = await client.get(f"{API_PREFIX}/auth/me")
    assert response.status_code == 401


async def test_the_session_cookie_is_not_readable_by_scripts(client: AsyncClient) -> None:
    payload = _registration()
    await client.post(f"{API_PREFIX}/auth/register", json=payload)
    response = await client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": payload["email"], "password": PASSWORD},
    )
    header = response.headers["set-cookie"]
    assert "httponly" in header.lower()
    assert "samesite" in header.lower()


async def test_a_write_without_the_csrf_header_is_refused(client: AsyncClient) -> None:
    await _register_and_login(client)
    response = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
    )
    assert response.status_code == 403
    assert "csrf" in response.text.lower()


async def test_a_write_with_the_wrong_csrf_token_is_refused(client: AsyncClient) -> None:
    await _register_and_login(client)
    response = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": "not-the-issued-token"},
    )
    assert response.status_code == 403


async def test_signing_out_invalidates_the_session(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    assert (await client.get(f"{API_PREFIX}/auth/me")).status_code == 200

    signed_out = await client.post(
        f"{API_PREFIX}/auth/logout",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert signed_out.status_code == 204
    assert (await client.get(f"{API_PREFIX}/auth/me")).status_code == 401


async def test_signing_out_everywhere_invalidates_the_session(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    response = await client.post(
        f"{API_PREFIX}/auth/logout-all",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 204
    assert (await client.get(f"{API_PREFIX}/auth/me")).status_code == 401


# --------------------------------------------------------------------------
# RBAC over HTTP
# --------------------------------------------------------------------------


async def test_the_role_catalogue_is_visible_to_a_member(client: AsyncClient) -> None:
    await _register_and_login(client)
    response = await client.get(f"{API_PREFIX}/roles")
    assert response.status_code == 200
    slugs = {role["slug"] for role in response.json()["items"]}
    assert slugs >= {"owner", "admin", "manager", "agent", "billing_admin", "viewer"}


async def test_an_owner_can_invite_a_member_who_then_has_only_their_role(
    client: AsyncClient,
) -> None:
    _, csrf_token = await _register_and_login(client)
    invitee = f"agent-{uuid.uuid4().hex}@example.com"
    invited = await client.post(
        f"{API_PREFIX}/members",
        json={
            "email": invitee,
            "display_name": "Grace Agent",
            "initial_password": PASSWORD,
            "role": "agent",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert invited.status_code == 201, invited.text
    assert invited.json()["roles"] == ["agent"]

    listed = await client.get(f"{API_PREFIX}/members")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 2


async def test_an_agent_cannot_manage_api_keys(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    invitee = f"agent-{uuid.uuid4().hex}@example.com"
    invited = await client.post(
        f"{API_PREFIX}/members",
        json={
            "email": invitee,
            "display_name": "Grace Agent",
            "initial_password": PASSWORD,
            "role": "agent",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert invited.status_code == 201, invited.text

    await client.post(f"{API_PREFIX}/auth/logout", headers={"X-CSRF-Token": csrf_token})
    client.cookies.clear()

    signed_in = await client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": invitee, "password": PASSWORD},
    )
    assert signed_in.status_code == 200, signed_in.text
    agent_csrf = signed_in.json()["csrf_token"]

    denied = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": agent_csrf},
    )
    assert denied.status_code == 403
    assert "permission_denied" in denied.text


async def test_an_unknown_role_is_refused(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    response = await client.post(
        f"{API_PREFIX}/members",
        json={
            "email": f"nobody-{uuid.uuid4().hex}@example.com",
            "display_name": "Nobody",
            "initial_password": PASSWORD,
            "role": "superuser",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code in {404, 422}


# --------------------------------------------------------------------------
# API keys over HTTP
# --------------------------------------------------------------------------


async def test_a_key_is_shown_once_and_never_again(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    created = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"], "ttl_days": 30},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    secret = body["secret"]
    assert secret.startswith("oc_test_")

    listed = await client.get(f"{API_PREFIX}/api-keys")
    assert listed.status_code == 200
    # The plaintext is gone for good; only the public key id remains.
    assert secret not in listed.text
    assert listed.json()["items"][0]["key_id"] == body["api_key"]["key_id"]
    assert "secret" not in listed.json()["items"][0]


async def test_a_key_authenticates_as_a_bearer_token(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    created = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["apikeys.manage"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]

    client.cookies.clear()
    response = await client.get(
        f"{API_PREFIX}/api-keys",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 200, response.text


async def test_a_key_cannot_be_granted_more_than_its_creator_holds(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    invitee = f"admin-{uuid.uuid4().hex}@example.com"
    await client.post(
        f"{API_PREFIX}/members",
        json={
            "email": invitee,
            "display_name": "Adam Admin",
            "initial_password": PASSWORD,
            "role": "admin",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    await client.post(f"{API_PREFIX}/auth/logout", headers={"X-CSRF-Token": csrf_token})
    client.cookies.clear()

    signed_in = await client.post(
        f"{API_PREFIX}/auth/login",
        json={"email": invitee, "password": PASSWORD},
    )
    assert signed_in.status_code == 200, signed_in.text

    # admin holds everything except billing.manage.
    denied = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "escalate", "scopes": ["billing.manage"]},
        headers={"X-CSRF-Token": signed_in.json()["csrf_token"]},
    )
    assert denied.status_code == 403


async def test_an_unknown_scope_is_refused(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    response = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["everything.always"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert response.status_code == 422
    assert "everything.always" in response.text


async def test_a_revoked_key_stops_working(client: AsyncClient) -> None:
    _, csrf_token = await _register_and_login(client)
    created = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["apikeys.manage"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert created.status_code == 201, created.text
    secret = created.json()["secret"]
    api_key_id = created.json()["api_key"]["id"]

    revoked = await client.delete(
        f"{API_PREFIX}/api-keys/{api_key_id}",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert revoked.status_code == 204

    client.cookies.clear()
    response = await client.get(
        f"{API_PREFIX}/api-keys",
        headers={"Authorization": f"Bearer {secret}"},
    )
    assert response.status_code == 401


async def test_a_key_from_another_tenant_cannot_be_revoked(client: AsyncClient) -> None:
    _, first_csrf = await _register_and_login(client)
    created = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": first_csrf},
    )
    assert created.status_code == 201, created.text
    foreign_id = created.json()["api_key"]["id"]

    await client.post(f"{API_PREFIX}/auth/logout", headers={"X-CSRF-Token": first_csrf})
    client.cookies.clear()
    _, second_csrf = await _register_and_login(client)

    # 404, not 403: the caller must not learn that the identifier is real.
    response = await client.delete(
        f"{API_PREFIX}/api-keys/{foreign_id}",
        headers={"X-CSRF-Token": second_csrf},
    )
    assert response.status_code == 404
    assert "api_key_not_found" in response.text


# --------------------------------------------------------------------------
# Secret non-disclosure
# --------------------------------------------------------------------------


async def test_no_endpoint_ever_returns_credential_material(
    client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    payload, csrf_token = await _register_and_login(client)
    created = await client.post(
        f"{API_PREFIX}/api-keys",
        json={"name": "deploy", "scopes": ["conversations.read"]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert created.status_code == 201, created.text

    for path in ("/auth/me", "/api-keys", "/members", "/roles"):
        response = await client.get(f"{API_PREFIX}{path}")
        assert response.status_code == 200, response.text
        assert PASSWORD not in response.text
        assert "password_hash" not in response.text
        assert "secret_digest" not in response.text
        assert "token_digest" not in response.text

    stored = await db_session.scalar(
        text("SELECT password_hash FROM users WHERE email = :email"),
        {"email": payload["email"]},
    )
    assert stored is not None
    assert PASSWORD not in stored
