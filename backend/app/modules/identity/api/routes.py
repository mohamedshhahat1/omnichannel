"""Identity endpoints.

Routes stay thin on purpose. They translate HTTP into a service call and a
service result back into a response; every authorization decision happens
inside the service, so a future Celery task or CLI reaching the same service
gets the same checks. A permission enforced in a route decorator would protect
only the callers that happen to arrive over HTTP.

Commits are explicit. `provide_database_session` yields an uncommitted session,
so a handler that raises leaves nothing behind - which is what makes
"validation failed halfway through provisioning" safe.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from app.api.dependencies import DatabaseSessionDep, SettingsDep
from app.core.settings import Settings
from app.modules.identity import models
from app.modules.identity.api.dependencies import (
    ApiKeyServiceDep,
    AuditServiceDep,
    AuthenticationServiceDep,
    PermissionResolverDep,
    PrincipalDep,
    ProvisioningServiceDep,
    SessionIdentityDep,
)
from app.modules.identity.api.schemas import (
    AcceptedResponse,
    ApiKeyCreatedResponse,
    ApiKeyCreateRequest,
    ApiKeyListResponse,
    ApiKeySummary,
    IdentityResponse,
    LoginRequest,
    LoginResponse,
    MemberInviteRequest,
    MembershipListResponse,
    MembershipSummary,
    RegistrationRequest,
    RoleAssignmentRequest,
    RoleListResponse,
    RoleSummary,
    TenantCreateRequest,
    TenantSummary,
    UserSummary,
)
from app.modules.identity.domain import (
    AuditOutcome,
    Permission,
    Principal,
)
from app.modules.identity.repositories import (
    RoleRepository,
    TenantRepository,
)
from app.modules.identity.services.authentication import SessionIdentity
from app.modules.identity.services.authorization import require_permission
from app.modules.identity.services.sessions import IssuedSession

router = APIRouter(tags=["identity"])

_PAGE_LIMIT = 50
_MAX_PAGE_LIMIT = 100

LimitQuery = Annotated[int, Query(ge=1, le=_MAX_PAGE_LIMIT)]
OffsetQuery = Annotated[int, Query(ge=0)]


def _client_ip(request: Request) -> str | None:
    client = request.client
    return client.host if client is not None else None


def _set_session_cookies(response: Response, settings: Settings, issued: IssuedSession) -> None:
    """Attach the session and CSRF cookies.

    The session cookie is `HttpOnly` so that a cross-site scripting bug cannot
    read it. The CSRF cookie deliberately is not: the double-submit pattern
    requires the page's own JavaScript to read it back and echo it in a header,
    which an attacker's origin cannot do.
    """
    auth = settings.auth
    max_age = auth.session_absolute_ttl_seconds
    response.set_cookie(
        key=auth.session_cookie_name,
        value=issued.token,
        max_age=max_age,
        httponly=True,
        secure=auth.cookie_secure,
        samesite=auth.cookie_samesite,
        path=auth.cookie_path,
    )
    response.set_cookie(
        key=auth.csrf_cookie_name,
        value=issued.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=auth.cookie_secure,
        samesite=auth.cookie_samesite,
        path=auth.cookie_path,
    )


def _clear_session_cookies(response: Response, settings: Settings) -> None:
    """Remove both cookies. Attributes must match how they were set."""
    auth = settings.auth
    for name in (auth.session_cookie_name, auth.csrf_cookie_name):
        response.delete_cookie(
            key=name,
            path=auth.cookie_path,
            secure=auth.cookie_secure,
            samesite=auth.cookie_samesite,
        )


def _user_summary(user: models.User) -> UserSummary:
    return UserSummary(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        status=user.status,
        email_verified=user.email_verified_at is not None,
    )


def _tenant_summary(tenant: models.Tenant) -> TenantSummary:
    return TenantSummary(
        id=tenant.id,
        name=tenant.name,
        slug=tenant.slug,
        status=tenant.status,
    )


def _membership_summary(record: models.Membership, roles: frozenset[str]) -> MembershipSummary:
    return MembershipSummary(
        id=record.id,
        user_id=record.user_id,
        tenant_id=record.tenant_id,
        status=record.status,
        roles=sorted(roles),
        created_at=record.created_at,
        accepted_at=record.accepted_at,
    )


def _api_key_summary(record: models.ApiKey) -> ApiKeySummary:
    """Project an API key without any secret material."""
    return ApiKeySummary(
        id=record.id,
        key_id=record.key_id,
        name=record.name,
        scopes=list(record.scopes),
        created_at=record.created_at,
        expires_at=record.expires_at,
        revoked_at=record.revoked_at,
        last_used_at=record.last_used_at,
    )


def _identity_response(
    *,
    user: models.User,
    tenant: models.Tenant | None,
    principal: Principal | None,
) -> IdentityResponse:
    return IdentityResponse(
        user=_user_summary(user),
        tenant=_tenant_summary(tenant) if tenant is not None else None,
        principal_kind=principal.kind.value if principal is not None else "user",
        roles=sorted(principal.role_slugs) if principal is not None else [],
        permissions=(
            sorted(permission.value for permission in principal.permissions)
            if principal is not None
            else []
        ),
    )


@router.post(
    "/auth/register",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=AcceptedResponse,
    summary="Register an account and its first tenant",
)
async def register(
    payload: RegistrationRequest,
    request: Request,
    session: DatabaseSessionDep,
    provisioning: ProvisioningServiceDep,
    audit: AuditServiceDep,
) -> AcceptedResponse:
    """Create an account, or silently do nothing if the address is taken.

    The response is identical either way. A 409 on a duplicate address would
    turn this endpoint into an account-existence oracle for anyone with a list
    of email addresses; the tenant slug, which is a shared public namespace and
    reveals nothing about a person, is allowed to conflict loudly.
    """
    slug = await provisioning.assert_slug_available(payload.tenant_slug)
    existing = await provisioning.find_user_by_email(payload.email)

    if existing is not None:
        await audit.record(
            action="identity.register",
            resource_type="user",
            outcome=AuditOutcome.FAILURE,
            ip_address=_client_ip(request),
            context={"reason": "email_already_registered", "email": payload.email},
        )
        await session.commit()
        return AcceptedResponse()

    user = await provisioning.create_user(
        raw_email=payload.email,
        raw_password=payload.password.get_secret_value(),
        raw_display_name=payload.display_name,
    )
    provisioned = await provisioning.create_tenant_with_owner(
        owner=user,
        raw_name=payload.tenant_name,
        raw_slug=slug,
    )
    await audit.record(
        action="identity.register",
        resource_type="user",
        resource_id=str(user.id),
        outcome=AuditOutcome.SUCCESS,
        tenant_id=provisioned.tenant.id,
        actor_user_id=user.id,
        ip_address=_client_ip(request),
        context={"tenant_slug": provisioned.tenant.slug},
    )
    await session.commit()
    return AcceptedResponse()


@router.post(
    "/auth/login",
    response_model=LoginResponse,
    summary="Exchange a password for a session",
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: SettingsDep,
    session: DatabaseSessionDep,
    authentication: AuthenticationServiceDep,
) -> LoginResponse:
    """Verify credentials and set the session cookies."""
    result = await authentication.login(
        raw_email=payload.email,
        raw_password=payload.password.get_secret_value(),
        tenant_slug=payload.tenant_slug,
        ip_address=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    await session.commit()

    identity = await authentication.identify_session(result.issued.token)
    tenant: models.Tenant | None = None
    principal: Principal | None = None
    if identity is not None and identity.principal is not None:
        principal = identity.principal
        tenant = await TenantRepository(session).get_by_id(principal.tenant_id)

    _set_session_cookies(response, settings, result.issued)
    return LoginResponse(
        identity=_identity_response(user=result.user, tenant=tenant, principal=principal),
        csrf_token=result.issued.csrf_token,
    )


@router.get(
    "/auth/me",
    response_model=IdentityResponse,
    summary="Describe the current caller",
)
async def read_current_identity(
    session: DatabaseSessionDep,
    identity: SessionIdentityDep,
) -> IdentityResponse:
    """Return the signed-in user and their authority in the active tenant."""
    tenant: models.Tenant | None = None
    if identity.principal is not None:
        tenant = await TenantRepository(session).get_by_id(identity.principal.tenant_id)
    return _identity_response(
        user=identity.user,
        tenant=tenant,
        principal=identity.principal,
    )


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke the current session",
)
async def logout(
    response: Response,
    settings: SettingsDep,
    session: DatabaseSessionDep,
    authentication: AuthenticationServiceDep,
    identity: SessionIdentityDep,
) -> None:
    """Revoke this session and clear its cookies."""
    await authentication.logout(identity.record)
    await session.commit()
    _clear_session_cookies(response, settings)


@router.post(
    "/auth/logout-all",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke every session for the current user",
)
async def logout_everywhere(
    response: Response,
    settings: SettingsDep,
    session: DatabaseSessionDep,
    authentication: AuthenticationServiceDep,
    identity: SessionIdentityDep,
) -> None:
    """Revoke all of this user's sessions, including this one."""
    await authentication.logout_everywhere(identity.user)
    await session.commit()
    _clear_session_cookies(response, settings)


@router.post(
    "/tenants",
    status_code=status.HTTP_201_CREATED,
    response_model=TenantSummary,
    summary="Create a tenant owned by the caller",
)
async def create_tenant(
    payload: TenantCreateRequest,
    request: Request,
    session: DatabaseSessionDep,
    provisioning: ProvisioningServiceDep,
    audit: AuditServiceDep,
    identity: SessionIdentityDep,
) -> TenantSummary:
    """Create a tenant and make the caller its owner.

    Authenticated but not authorized by a permission, because no permission in
    a tenant that does not exist yet could apply.
    """
    provisioned = await provisioning.create_tenant_with_owner(
        owner=identity.user,
        raw_name=payload.name,
        raw_slug=payload.slug,
    )
    # Activate the new tenant on the current session so the owner can
    # administer it immediately. Server-side state only: the caller cannot
    # nominate a tenant, they can only receive the one they just created.
    identity.record.tenant_id = provisioned.tenant.id
    await audit.record(
        action="tenant.create",
        resource_type="tenant",
        resource_id=str(provisioned.tenant.id),
        outcome=AuditOutcome.SUCCESS,
        tenant_id=provisioned.tenant.id,
        actor_user_id=identity.user.id,
        ip_address=_client_ip(request),
        context={"slug": provisioned.tenant.slug},
    )
    await session.commit()
    return _tenant_summary(provisioned.tenant)


@router.get(
    "/roles",
    response_model=RoleListResponse,
    summary="List assignable roles",
)
async def list_roles(
    session: DatabaseSessionDep,
    principal: PrincipalDep,
) -> RoleListResponse:
    """List the roles the caller's tenant may assign, with their grants.

    The grants are read from `role_permissions` - the same rows the
    authorization path resolves against - so this listing cannot advertise a
    permission that a request would then be refused, or hide one it would be
    allowed. Deriving them from the Python default matrix instead would let the
    two drift the moment a grant is changed in the database.
    """
    require_permission(principal, Permission.TENANT_READ)
    repository = RoleRepository(session, principal.tenant)
    roles = await repository.list_visible()
    return RoleListResponse(
        items=[
            RoleSummary(
                slug=role.slug,
                name=role.name,
                description=role.description,
                is_system=role.is_system,
                permissions=sorted(await repository.permission_slugs_for(role.id)),
            )
            for role in roles
        ]
    )


@router.get(
    "/members",
    response_model=MembershipListResponse,
    summary="List memberships in the caller's tenant",
)
async def list_members(
    principal: PrincipalDep,
    provisioning: ProvisioningServiceDep,
    resolver: PermissionResolverDep,
    limit: LimitQuery = _PAGE_LIMIT,
    offset: OffsetQuery = 0,
) -> MembershipListResponse:
    """Return one page of memberships, each with its role slugs."""
    memberships = await provisioning.list_members(
        principal=principal,
        limit=limit,
        offset=offset,
    )
    items = [
        _membership_summary(record, await resolver.role_slugs_for(record.id))
        for record in memberships
    ]
    return MembershipListResponse(items=items)


@router.post(
    "/members",
    status_code=status.HTTP_201_CREATED,
    response_model=MembershipSummary,
    summary="Add a person to the caller's tenant",
)
async def invite_member(
    payload: MemberInviteRequest,
    request: Request,
    session: DatabaseSessionDep,
    principal: PrincipalDep,
    provisioning: ProvisioningServiceDep,
    resolver: PermissionResolverDep,
    audit: AuditServiceDep,
) -> MembershipSummary:
    """Create a membership, and the account behind it if it is new."""
    membership = await provisioning.invite_member(
        principal=principal,
        raw_email=payload.email,
        raw_display_name=payload.display_name,
        raw_password=payload.initial_password.get_secret_value(),
        role_slug=payload.role,
    )
    await audit.record(
        action="membership.invite",
        resource_type="membership",
        resource_id=str(membership.id),
        outcome=AuditOutcome.SUCCESS,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        actor_api_key_id=principal.api_key_id,
        ip_address=_client_ip(request),
        context={"role": payload.role, "email": payload.email},
    )
    await session.commit()
    return _membership_summary(membership, await resolver.role_slugs_for(membership.id))


@router.post(
    "/members/{membership_id}/roles",
    response_model=MembershipSummary,
    summary="Grant a role to a membership",
)
async def assign_role(
    membership_id: uuid.UUID,
    payload: RoleAssignmentRequest,
    request: Request,
    session: DatabaseSessionDep,
    principal: PrincipalDep,
    provisioning: ProvisioningServiceDep,
    resolver: PermissionResolverDep,
    audit: AuditServiceDep,
) -> MembershipSummary:
    """Grant one role. A membership in another tenant is not found."""
    membership = await provisioning.assign_role(
        principal=principal,
        membership_id=membership_id,
        role_slug=payload.role,
        resolver=resolver,
    )
    await audit.record(
        action="membership.assign_role",
        resource_type="membership",
        resource_id=str(membership.id),
        outcome=AuditOutcome.SUCCESS,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        actor_api_key_id=principal.api_key_id,
        ip_address=_client_ip(request),
        context={"role": payload.role},
    )
    await session.commit()
    return _membership_summary(membership, await resolver.role_slugs_for(membership.id))


@router.get(
    "/api-keys",
    response_model=ApiKeyListResponse,
    summary="List the tenant's API keys",
)
async def list_api_keys(
    principal: PrincipalDep,
    api_keys: ApiKeyServiceDep,
    limit: LimitQuery = _PAGE_LIMIT,
    offset: OffsetQuery = 0,
) -> ApiKeyListResponse:
    """Return the tenant's keys. Secret material is never included."""
    records = await api_keys.list_for_tenant(principal=principal, limit=limit, offset=offset)
    return ApiKeyListResponse(items=[_api_key_summary(record) for record in records])


@router.post(
    "/api-keys",
    status_code=status.HTTP_201_CREATED,
    response_model=ApiKeyCreatedResponse,
    summary="Mint an API key",
)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    request: Request,
    session: DatabaseSessionDep,
    principal: PrincipalDep,
    api_keys: ApiKeyServiceDep,
    audit: AuditServiceDep,
) -> ApiKeyCreatedResponse:
    """Create a key and return its plaintext exactly once."""
    issued = await api_keys.issue(
        principal=principal,
        name=payload.name,
        scopes=payload.scopes,
        ttl_days=payload.ttl_days,
    )
    await audit.record(
        action="apikey.create",
        resource_type="api_key",
        resource_id=str(issued.record.id),
        outcome=AuditOutcome.SUCCESS,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        actor_api_key_id=principal.api_key_id,
        ip_address=_client_ip(request),
        # The public key_id is recorded so this key can be traced through the
        # audit log later. The secret never touches this row.
        context={"key_id": issued.record.key_id, "scopes": list(issued.record.scopes)},
    )
    await session.commit()
    return ApiKeyCreatedResponse(
        api_key=_api_key_summary(issued.record),
        secret=issued.plaintext,
    )


@router.delete(
    "/api-keys/{api_key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key",
)
async def revoke_api_key(
    api_key_id: uuid.UUID,
    request: Request,
    session: DatabaseSessionDep,
    principal: PrincipalDep,
    api_keys: ApiKeyServiceDep,
    audit: AuditServiceDep,
) -> None:
    """Revoke a key belonging to the caller's tenant."""
    record = await api_keys.revoke(principal=principal, api_key_id=api_key_id)
    await audit.record(
        action="apikey.revoke",
        resource_type="api_key",
        resource_id=str(record.id),
        outcome=AuditOutcome.SUCCESS,
        tenant_id=principal.tenant_id,
        actor_user_id=principal.user_id,
        actor_api_key_id=principal.api_key_id,
        ip_address=_client_ip(request),
        context={"key_id": record.key_id},
    )
    await session.commit()


__all__ = ["SessionIdentity", "router"]
