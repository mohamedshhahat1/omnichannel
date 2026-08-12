"""Identity dependencies.

These assemble the services from request-scoped state and, more importantly,
are where a request stops being anonymous. Three levels are offered so a route
asks for exactly the authority it needs:

* `SessionIdentityDep` - a signed-in person, tenant or no tenant. Used by the
  endpoints that exist precisely because the caller has no tenant yet.
* `PrincipalDep` - a caller acting inside a tenant, from either a session
  cookie or a bearer API key. Everything tenant-scoped depends on this.

CSRF is enforced here rather than in middleware, because it applies to exactly
one authentication method. Cookies are attached by the browser automatically,
so a cookie-authenticated state change needs the double-submit check; a bearer
key is attached only by code that meant to, so it does not.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import Depends, Request
from redis.asyncio import Redis

from app.api.dependencies import DatabaseSessionDep, SettingsDep
from app.core.errors import InternalError
from app.core.security import PasswordHashingService
from app.modules.identity.domain import Principal
from app.modules.identity.errors import (
    AuthenticationFailedError,
    CsrfValidationError,
    TenantNotFoundError,
)
from app.modules.identity.repositories import MembershipRepository, SessionRepository
from app.modules.identity.services.api_keys import ApiKeyService
from app.modules.identity.services.audit import AuditService
from app.modules.identity.services.authentication import AuthenticationService, SessionIdentity
from app.modules.identity.services.permissions import PermissionResolver, RoleSlugCache
from app.modules.identity.services.provisioning import ProvisioningService
from app.modules.identity.services.sessions import SessionService

_BEARER_PREFIX: Final = "Bearer "
_SAFE_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


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


def provide_role_slug_cache(request: Request, settings: SettingsDep) -> RoleSlugCache:
    """Return the RBAC cache, disabled when Redis is not attached.

    Resolved defensively rather than through `provide_redis`: the test
    application deliberately starts no infrastructure, and authorization must
    keep working there - from PostgreSQL alone - instead of failing with a 500.
    """
    client = getattr(request.app.state, "redis", None)
    return RoleSlugCache(
        client if isinstance(client, Redis) else None,
        environment=settings.environment.value,
        ttl_seconds=settings.auth.session_cache_ttl_seconds,
    )


RoleSlugCacheDep = Annotated[RoleSlugCache, Depends(provide_role_slug_cache)]


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
    cache: RoleSlugCacheDep,
    audit: AuditServiceDep,
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


async def provide_session_identity(
    request: Request,
    settings: SettingsDep,
    authentication: AuthenticationServiceDep,
) -> SessionIdentity | None:
    """Resolve the session cookie, if one was presented and is still live."""
    token = request.cookies.get(settings.auth.session_cookie_name)
    if not token:
        return None
    return await authentication.identify_session(token)


SessionIdentityOptionalDep = Annotated[
    SessionIdentity | None,
    Depends(provide_session_identity),
]


def _enforce_csrf(
    request: Request,
    settings: SettingsDep,
    sessions: SessionService,
    identity: SessionIdentity,
) -> None:
    """Require a matching CSRF header on cookie-authenticated writes."""
    if request.method in _SAFE_METHODS:
        return
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

    A bearer key wins over a cookie when both are present, because sending an
    explicit `Authorization` header is an unambiguous statement of intent,
    while the cookie may just be whatever the browser had lying around.
    """
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
    cache: RoleSlugCacheDep,
) -> PermissionResolver:
    """Return a resolver scoped to the caller's tenant."""
    return PermissionResolver(MembershipRepository(session, principal.tenant), cache)


PermissionResolverDep = Annotated[PermissionResolver, Depends(build_permission_resolver)]
