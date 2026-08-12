"""API-key issuance, authentication and revocation.

The format is fixed by `docs/security.md` 2: `oc_{env}_{key_id}_{secret}`. Only
`key_id` is stored in the clear, so authentication is one indexed lookup rather
than a scan that hashes every stored key; only the SHA-256 digest of the secret
half is persisted, so a database disclosure does not yield working keys.

A key can never carry more authority than the person who created it. Scopes are
intersected against the creator's effective permissions at issue time, so a
manager cannot mint a key that manages billing - and a compromised key cannot
be used to escalate by re-issuing a broader one.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    digest_secret,
    environment_label,
    mint_api_key,
    parse_api_key,
    secrets_equal,
)
from app.core.settings import AuthSettings, Environment
from app.modules.identity import models
from app.modules.identity.domain import Permission, Principal
from app.modules.identity.errors import (
    ApiKeyNotFoundError,
    IdentityValidationError,
    PermissionDeniedError,
)
from app.modules.identity.repositories import ApiKeyAuthenticationRepository, ApiKeyRepository
from app.modules.identity.services.authorization import require_permission
from app.platform.clock import is_expired, utcnow

_SCOPE_CATALOGUE: Final = frozenset(permission.value for permission in Permission)


def resolve_scopes(principal: Principal, requested: Sequence[str]) -> list[str]:
    """Return the scopes to grant, refusing anything the caller cannot delegate.

    Two distinct failures, deliberately answered differently:

    * an unrecognised scope is a client mistake and gets a 422 naming it;
    * a real scope the caller does not hold is an escalation attempt and gets
      the same 403 as calling the endpoint directly would.
    """
    unknown = sorted({scope for scope in requested if scope not in _SCOPE_CATALOGUE})
    if unknown:
        raise IdentityValidationError(
            "one or more requested scopes are not recognised",
            details={"field": "scopes", "unknown_scopes": unknown},
        )

    held = {permission.value for permission in principal.permissions}
    escalated = sorted({scope for scope in requested if scope not in held})
    if escalated:
        raise PermissionDeniedError(
            "An API key cannot be granted more access than you have.",
            details={"required_permission": escalated[0]},
            internal_message=(
                f"api key scope escalation refused for tenant {principal.tenant_id}: "
                f"{', '.join(escalated)}"
            ),
        )
    return sorted(set(requested))


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """A freshly minted key and the single plaintext copy of it.

    `plaintext` exists only until the response is serialised. It is never
    written to the row, a log line, a span or an audit entry.
    """

    record: models.ApiKey
    plaintext: str


class ApiKeyService:
    """Owns the API-key lifecycle."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: AuthSettings,
        environment: Environment,
    ) -> None:
        self._session = session
        self._settings = settings
        self._environment = environment

    def _repository(self, principal: Principal) -> ApiKeyRepository:
        """Return a repository pinned to the caller's tenant.

        Every read and write below goes through this, so "forgot the tenant
        filter" is not something a caller can express.
        """
        return ApiKeyRepository(self._session, principal.tenant)

    async def list_for_tenant(
        self,
        *,
        principal: Principal,
        limit: int,
        offset: int,
    ) -> Sequence[models.ApiKey]:
        """List the tenant's keys. Never includes secret material."""
        require_permission(principal, Permission.APIKEYS_MANAGE)
        return await self._repository(principal).list_all(limit=limit, offset=offset)

    async def issue(
        self,
        *,
        principal: Principal,
        name: str,
        scopes: Sequence[str],
        ttl_days: int | None = None,
    ) -> IssuedApiKey:
        """Mint a key for the caller's tenant and return its plaintext once."""
        require_permission(principal, Permission.APIKEYS_MANAGE)
        granted = resolve_scopes(principal, scopes)

        maximum = self._settings.api_key_max_ttl_days
        lifetime_days = maximum if ttl_days is None else ttl_days
        if lifetime_days > maximum:
            # Every key expires. An unbounded credential is one that nobody
            # ever revokes because nobody remembers it exists.
            raise IdentityValidationError(
                "the requested key lifetime exceeds the configured maximum",
                details={"field": "ttl_days", "maximum_days": maximum},
            )

        material = mint_api_key(environment_label(self._environment))
        record = await self._repository(principal).create(
            key_id=material.key_id,
            secret_digest=material.secret_digest,
            name=name,
            created_by_id=principal.user_id,
            expires_at=utcnow() + timedelta(days=lifetime_days),
        )
        record.scopes = granted
        await self._session.flush()
        return IssuedApiKey(record=record, plaintext=material.plaintext)

    async def revoke(self, *, principal: Principal, api_key_id: uuid.UUID) -> models.ApiKey:
        """Revoke a key belonging to the caller's tenant.

        A key in another tenant is reported as missing, not forbidden: the
        caller has no way to learn that the identifier is real.
        """
        require_permission(principal, Permission.APIKEYS_MANAGE)
        repository = self._repository(principal)
        record = await repository.get_by_id(api_key_id)
        if record is None:
            raise ApiKeyNotFoundError(
                internal_message=(
                    f"api key {api_key_id} is not visible to tenant {principal.tenant_id}"
                ),
            )
        if record.revoked_at is None:
            await repository.revoke(record, now=utcnow())
        return record

    async def authenticate(self, presented: str) -> models.ApiKey | None:
        """Resolve a presented key, or return None.

        Malformed, unknown, wrong-secret, wrong-environment, revoked and
        expired all produce the same `None`. The caller has no legitimate use
        for the difference, and neither has anybody probing the endpoint.
        """
        parsed = parse_api_key(presented)
        if parsed is None:
            return None
        if parsed.environment != environment_label(self._environment):
            # A staging key replayed against production must never work, even
            # if the two databases were ever restored from one another.
            return None

        repository = ApiKeyAuthenticationRepository(self._session)
        record = await repository.get_by_key_id(parsed.key_id)
        if record is None:
            return None
        if not secrets_equal(record.secret_digest, digest_secret(parsed.secret)):
            return None
        if record.revoked_at is not None or is_expired(record.expires_at):
            return None

        await repository.mark_used(record, now=utcnow())
        return record
