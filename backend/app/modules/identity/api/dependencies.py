"""Identity dependencies.

These assemble the services from request-scoped state and, more importantly,
are where a request stops being anonymous. Three levels are offered so a route
asks for exactly the authority it needs:

* `SessionIdentityDep` - a signed-in person, tenant or no tenant. Used by the
  endpoints that exist precisely because the caller has no tenant yet.
* `PrincipalDep` - a caller acting inside a tenant, from either a session
  cookie or a bearer API key. Everything tenant-scoped depends on this.

Two transport-level rules live here rather than in middleware, because both
apply to exactly one authentication method:

* **CSRF and origin checks apply to cookie writes only.** A cookie is attached
  by the browser whether or not the page meant it, so a cookie-authenticated
  state change needs proof that the page intended it. A bearer key is attached
  only by code that chose to, and no attacker's page can make a browser attach
  one.
* **A request presents one credential, never two.** `docs/security.md` 2.8
  makes cookie and API-key authentication mutually exclusive per request.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends, Request
from redis.asyncio import Redis

from app.api.dependencies import DatabaseSessionDep, SettingsDep
from app.core.errors import InternalError
from app.core.origins import evaluate_origin
from app.core.rate_limit import FixedWindowRateLimiter
from app.core.security import PasswordHashingService
from app.core.settings import Settings
from app.modules.identity.domain import Principal
from app.modules.identity.errors import (
    AmbiguousCredentialsError,
    AuthenticationFailedError,
    CsrfValidationError,
    OriginRejectedError,
    TenantNotFoundError,
)
from app.modules.identity.repositories import MembershipRepository, SessionRepository
from app.modules.identity.services.api_keys import ApiKeyService
from app.modules.identity.services.audit import AuditService
from app.modules.identity.services.authentication import AuthenticationService, SessionIdentity
from app.modules.identity.services.permissions import (
    EffectivePermissionCache,
    PermissionResolver,
)
from app.modules.identity.services.provisioning import ProvisioningService
from app.modules.identity.services.sessions import SessionService

_BEARER_PREFIX: Final = "Bearer "
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})
_LOGIN_RATE_LIMIT_PURPOSE: Final = "login"


def provide_password_hashing(request: Request) -> PasswordHashingService:
    """Return the application-wide Argon2id hasher.

    Built once in `create_app`. Constructing one per request would be
    harmless but pointless; a missing one means the app was not built by the
    factory, which is a 500 rather than a client error.
    """
    service = getattr(request.app.state, "password_hashing", None)
    if isinstance(service, PasswordHashingService):
        return service
    raise InternalError(internal_message="password hashing service is unavailable.")


PasswordHashingDep = Annotated[PasswordHashingService, Depends(provide_password_hashing)]


def provide_effective_permission_cache(
    request: Request,
    settings: SettingsDep,
) -> EffectivePermissionCache:
    """Return the RBAC cache, disabled when Redis is not attached.

    Resolved defensively rather than through `provide_redis`: the test
    application deliberately starts no infrastructure, and authorization must
    keep working there - from PostgreSQL alone - instead of failing with a 500.
    That fallback is also the honest statement of the design: Redis holds a
    copy, PostgreSQL holds the answer.
    """
    client = getattr(request.app.state, "redis", None)
    return EffectivePermissionCache(
        client if isinstance(client, Redis) else None,
        environment=settings.environment.value,
        ttl_seconds=settings.auth.session_cache_ttl_seconds,
    )


EffectivePermissionCacheDep = Annotated[
    EffectivePermissionCache,
    Depends(provide_effective_permission_cache),
]


def provide_login_rate_limiter(
    request: Request,
    settings: SettingsDep,
) -> FixedWindowRateLimiter | None:
    """Return the per-source login limiter, or None when it is switched off.

    Redis is resolved the same defensive way as the RBAC cache, and for the
    same reason: the test application starts no infrastructure, and login must
    keep working from PostgreSQL alone. The limiter itself then reports
    `degraded` rather than pretending it counted anything, and the per-account
    lockout - which lives in PostgreSQL - is unaffected either way.
    """
    if not settings.auth.login_rate_limit_enabled:
        return None
    client = getattr(request.app.state, "redis", None)
    return FixedWindowRateLimiter(
        client if isinstance(client, Redis) else None,
        environment=settings.environment.value,
        purpose=_LOGIN_RATE_LIMIT_PURPOSE,
        limit=settings.auth.login_rate_limit_max_attempts,
        window_seconds=settings.auth.login_rate_limit_window_seconds,
        fail_open=settings.auth.login_rate_limit_fail_open,
    )


LoginRateLimiterDep = Annotated[
    FixedWindowRateLimiter | None,
    Depends(provide_login_rate_limiter),
]


def provide_audit_service(session: DatabaseSessionDep) -> AuditService:
    """Return an audit writer bound to the request's transaction."""
    return AuditService(session)


AuditServiceDep = Annotated[AuditService, Depends(provide_audit_service)]


def provide_session_service(session: DatabaseSessionDep, settings: SettingsDep) -> SessionService:
    """Return the session lifecycle service."""
    return SessionService(SessionRepository(session), settings.auth)


SessionServiceDep = Annotated[SessionService, Depends(provide_session_service)]


def provide_api_key_service(session: DatabaseSessionDep, settings: SettingsDep) -> ApiKeyService:
    """Return the API-key lifecycle service."""
    return ApiKeyService(session, settings=settings.auth, environment=settings.environment)


ApiKeyServiceDep = Annotated[ApiKeyService, Depends(provide_api_key_service)]


def provide_authentication_service(
    session: DatabaseSessionDep,
    settings: SettingsDep,
    passwords: PasswordHashingDep,
    sessions: SessionServiceDep,
    api_keys: ApiKeyServiceDep,
    cache: EffectivePermissionCacheDep,
    audit: AuditServiceDep,
    login_limiter: LoginRateLimiterDep,
) -> AuthenticationService:
    """Return the credential-verification service."""
    return AuthenticationService(
        session,
        settings=settings.auth,
        passwords=passwords,
        sessions=sessions,
        api_keys=api_keys,
        cache=cache,
        audit=audit,
        login_limiter=login_limiter,
    )


AuthenticationServiceDep = Annotated[AuthenticationService, Depends(provide_authentication_service)]


def provide_provisioning_service(
    session: DatabaseSessionDep,
    settings: SettingsDep,
    passwords: PasswordHashingDep,
    audit: AuditServiceDep,
) -> ProvisioningService:
    """Return the tenant, account and membership service."""
    return ProvisioningService(
        session,
        passwords=passwords,
        settings=settings.auth,
        audit=audit,
    )


ProvisioningServiceDep = Annotated[ProvisioningService, Depends(provide_provisioning_service)]


def _presented_credentials(request: Request, settings: Settings) -> tuple[bool, bool]:
    """Report which credential kinds this request carries, without reading them.

    Presence only. Nothing here validates or even parses a credential, so the
    result is safe to branch on before authentication has happened.
    """
    cookie = bool(request.cookies.get(settings.auth.session_cookie_name))
    header = request.headers.get("Authorization", "")
    bearer = header.startswith(_BEARER_PREFIX) and bool(header[len(_BEARER_PREFIX) :].strip())
    return cookie, bearer


def reject_ambiguous_credentials(request: Request, settings: Settings) -> None:
    """Refuse a request that presents both a session cookie and a bearer key.

    `docs/security.md` 2.8: the two authentication methods are mutually
    exclusive per request. Phase 3 preferred the bearer key when both arrived,
    which is the wrong answer for three reasons:

    * **Identity confusion.** A browser attaches its cookie to every request to
      this origin. A page that adds an `Authorization` header - or a proxy, or
      an SDK with a stale key in its environment - silently acts as the key's
      tenant while the person at the keyboard is signed in as somebody else.
      The audit trail then records the wrong actor, and the CSRF check that
      protects the cookie path is skipped entirely.
    * **CSRF bypass.** Preferring the bearer key means an attacker who can get
      *any* `Authorization` header onto a cookie-carrying request routes around
      the double-submit check.
    * **No safe precedence exists.** Preferring the cookie instead just moves
      the confusion; the only unambiguous reading of two credentials is that
      the client did not mean one of them.

    Raised before either credential is inspected, so the refusal reveals
    nothing about whether either was valid - it is a statement about the shape
    of the request, not about its secrets.
    """
    cookie, bearer = _presented_credentials(request, settings)
    if cookie and bearer:
        raise AmbiguousCredentialsError(
            internal_message="request presented both a session cookie and a bearer credential",
        )


async def provide_session_identity(
    request: Request,
    settings: SettingsDep,
    authentication: AuthenticationServiceDep,
) -> SessionIdentity | None:
    """Resolve the session cookie, if one was presented and is still live.

    The ambiguity check runs here, at the first point that touches a
    credential, so every dependent - `require_session`, `provide_principal` and
    anything added later - inherits it rather than having to remember it.
    """
    reject_ambiguous_credentials(request, settings)
    token = request.cookies.get(settings.auth.session_cookie_name)
    if not token:
        return None
    return await authentication.identify_session(token)


SessionIdentityOptionalDep = Annotated[
    SessionIdentity | None,
    Depends(provide_session_identity),
]


def _enforce_origin(request: Request, settings: Settings) -> None:
    """Check the browser origin of a cookie-authenticated write.

    Layer 3 of the four CSRF layers in `docs/security.md` 2.4. It earns its
    place next to the double-submit token because the two fail differently: the
    token is carried in a cookie that any same-site context can read, while
    `Origin` is written by the browser and cannot be forged by page script. A
    subdomain takeover or an XSS on a sibling host defeats the first and not
    the second.

    The allowlist is the CORS origins plus `csrf_trusted_origins`, and the
    application's own host always passes - so a single-origin deployment needs
    no configuration to keep working, and an attacker still cannot pass,
    because a browser will not let their page claim our host as its origin.

    Whether a *missing* `Origin` and `Referer` is fatal comes from
    `Settings.require_origin_on_cookie_writes`, which is unconditionally true
    in production. Outside production it is off so that curl and the test suite
    can drive the API; a non-browser client cannot be a CSRF victim anyway,
    because there is no ambient cookie for an attacker's page to borrow.
    """
    decision = evaluate_origin(
        origin_header=request.headers.get("Origin"),
        referer_header=request.headers.get("Referer"),
        host_header=request.headers.get("Host"),
        allowed_origins=(
            *settings.security.cors_origins,
            *settings.security.csrf_trusted_origins,
        ),
        require_present=settings.require_origin_on_cookie_writes,
        require_https=settings.is_production,
    )
    if not decision.allowed:
        raise OriginRejectedError(
            internal_message=(
                f"origin check failed ({decision.reason}) for "
                f"{request.method} {request.url.path}"
            ),
        )


def _enforce_csrf(
    request: Request,
    settings: SettingsDep,
    sessions: SessionService,
    identity: SessionIdentity,
) -> None:
    """Require a valid origin and CSRF header on cookie-authenticated writes.

    Origin first: it is a header comparison with no secret in it, so a request
    from a foreign origin is refused without the token check running at all.
    Both failures return the same generic message to the caller; only the
    internal message says which layer refused.
    """
    if request.method in _SAFE_METHODS:
        return
    _enforce_origin(request, settings)
    presented = request.headers.get(settings.auth.csrf_header_name)
    if not sessions.verify_csrf(identity.record, presented):
        raise CsrfValidationError(
            internal_message=f"csrf check failed for session {identity.record.id}",
        )


async def require_session(
    request: Request,
    settings: SettingsDep,
    sessions: SessionServiceDep,
    identity: SessionIdentityOptionalDep,
) -> SessionIdentity:
    """Require a live session. The caller need not have an active tenant."""
    if identity is None:
        raise AuthenticationFailedError(internal_message="no live session cookie presented")
    _enforce_csrf(request, settings, sessions, identity)
    return identity


SessionIdentityDep = Annotated[SessionIdentity, Depends(require_session)]


async def provide_principal(
    request: Request,
    settings: SettingsDep,
    sessions: SessionServiceDep,
    authentication: AuthenticationServiceDep,
    identity: SessionIdentityOptionalDep,
) -> Principal:
    """Resolve the caller to a tenant-scoped principal.

    Exactly one credential decides the outcome:

    * cookie only - the session's principal, after the CSRF and origin checks;
    * bearer only - the API key's principal, with no CSRF check because no
      browser attaches an `Authorization` header on its own;
    * both - refused by `reject_ambiguous_credentials`, which has already run
      inside `provide_session_identity`;
    * neither - unauthenticated.

    The bearer branch is reached only when no session cookie was presented, so
    there is no precedence rule left to reason about - which is the point.
    """
    reject_ambiguous_credentials(request, settings)

    header = request.headers.get("Authorization", "")
    if header.startswith(_BEARER_PREFIX):
        presented = header[len(_BEARER_PREFIX) :].strip()
        principal = await authentication.identify_api_key(presented)
        if principal is None:
            raise AuthenticationFailedError(internal_message="api key rejected")
        return principal

    if identity is None:
        raise AuthenticationFailedError(internal_message="no credentials presented")
    _enforce_csrf(request, settings, sessions, identity)
    if identity.principal is None:
        # Signed in, but with no active membership in an active tenant. 404
        # rather than 403: see `services.authorization`.
        raise TenantNotFoundError(
            internal_message=f"session {identity.record.id} has no active tenant membership",
        )
    return identity.principal


PrincipalDep = Annotated[Principal, Depends(provide_principal)]


def build_permission_resolver(
    session: DatabaseSessionDep,
    principal: PrincipalDep,
    cache: EffectivePermissionCacheDep,
) -> PermissionResolver:
    """Return a resolver scoped to the caller's tenant."""
    return PermissionResolver(MembershipRepository(session, principal.tenant), cache)


PermissionResolverDep = Annotated[PermissionResolver, Depends(build_permission_resolver)]
