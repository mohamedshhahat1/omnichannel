"""Request and response models for the identity endpoints.

Two conventions here are security controls rather than style:

* **Responses are constructed field by field from ORM rows, never with
  `from_attributes`.** `users.password_hash`, `sessions.token_digest` and
  `api_keys.secret_digest` all live on models that these responses describe.
  Automatic attribute mapping means the day somebody adds a column is the day
  it starts appearing in an API response. Explicit construction makes exposure
  a decision.
* **Incoming secrets are `SecretStr`.** Pydantic then masks them in `repr`,
  in `model_dump()` and therefore in any log line, traceback or error report
  that happens to include the parsed body.

Email is validated with the project's own normaliser rather than pydantic's
`EmailStr`, which would add the `email-validator` dependency for a check the
domain layer already owns.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from app.modules.identity.domain import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_EMAIL_LENGTH,
    MAX_SLUG_LENGTH,
    MIN_SLUG_LENGTH,
)

_MAX_PASSWORD_LENGTH = 1_024


class RegistrationRequest(BaseModel):
    """Create an account and its first tenant."""

    email: str = Field(min_length=3, max_length=MAX_EMAIL_LENGTH)
    password: SecretStr = Field(min_length=1, max_length=_MAX_PASSWORD_LENGTH)
    display_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    tenant_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    tenant_slug: str = Field(min_length=MIN_SLUG_LENGTH, max_length=MAX_SLUG_LENGTH)


class AcceptedResponse(BaseModel):
    """Deliberately uninformative acknowledgement.

    Registration answers with this whether or not the address was already
    taken. Anything else would let an anonymous caller test email addresses
    against the user table.
    """

    status: Literal["accepted"] = "accepted"
    message: str = Field(
        default="If the address can be registered, the account has been created.",
    )


class LoginRequest(BaseModel):
    """Exchange a password for a session cookie."""

    email: str = Field(min_length=3, max_length=MAX_EMAIL_LENGTH)
    password: SecretStr = Field(min_length=1, max_length=_MAX_PASSWORD_LENGTH)
    tenant_slug: str | None = Field(
        default=None,
        max_length=MAX_SLUG_LENGTH,
        description="Tenant to activate for this session. Omit to sign in without one.",
    )


class UserSummary(BaseModel):
    """The public view of an account."""

    id: uuid.UUID
    email: str
    display_name: str
    status: str
    email_verified: bool


class TenantSummary(BaseModel):
    """The public view of a tenant."""

    id: uuid.UUID
    name: str
    slug: str
    status: str


class IdentityResponse(BaseModel):
    """Who the caller is and what they may do in the active tenant."""

    user: UserSummary
    tenant: TenantSummary | None = None
    principal_kind: str
    roles: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)


class LoginResponse(BaseModel):
    """Result of a successful password login.

    `csrf_token` is duplicated from the readable cookie so a browser client on
    a different origin can pick it up without reading cookies. It is an
    anti-forgery value, not a credential: on its own it authenticates nothing.
    """

    identity: IdentityResponse
    csrf_token: str


class TenantCreateRequest(BaseModel):
    """Create a tenant owned by the calling user."""

    name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    slug: str = Field(min_length=MIN_SLUG_LENGTH, max_length=MAX_SLUG_LENGTH)


class MemberInviteRequest(BaseModel):
    """Add a person to the calling principal's tenant."""

    email: str = Field(min_length=3, max_length=MAX_EMAIL_LENGTH)
    display_name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    initial_password: SecretStr = Field(min_length=1, max_length=_MAX_PASSWORD_LENGTH)
    role: str = Field(min_length=1, max_length=MAX_SLUG_LENGTH)


class RoleAssignmentRequest(BaseModel):
    """Grant one role to an existing membership."""

    role: str = Field(min_length=1, max_length=MAX_SLUG_LENGTH)


class MembershipSummary(BaseModel):
    """One person's access to one tenant."""

    id: uuid.UUID
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    status: str
    roles: list[str] = Field(default_factory=list)
    created_at: datetime
    accepted_at: datetime | None = None


class MembershipListResponse(BaseModel):
    """A page of memberships."""

    items: list[MembershipSummary] = Field(default_factory=list)


class RoleSummary(BaseModel):
    """A role and the permissions it confers."""

    slug: str
    name: str
    description: str
    is_system: bool
    permissions: list[str] = Field(default_factory=list)


class RoleListResponse(BaseModel):
    """Every role the caller's tenant may assign."""

    items: list[RoleSummary] = Field(default_factory=list)


class ApiKeyCreateRequest(BaseModel):
    """Mint a machine credential for the caller's tenant."""

    name: str = Field(min_length=1, max_length=MAX_DISPLAY_NAME_LENGTH)
    scopes: list[str] = Field(min_length=1)
    ttl_days: int | None = Field(default=None, ge=1)


class ApiKeySummary(BaseModel):
    """An API key without any secret material.

    `key_id` is the public half and is safe to display; it is what an operator
    matches against a log line when retiring a key.
    """

    id: uuid.UUID
    key_id: str
    name: str
    scopes: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None


class ApiKeyListResponse(BaseModel):
    """A page of API keys. Never contains secret material."""

    items: list[ApiKeySummary] = Field(default_factory=list)


class ApiKeyCreatedResponse(BaseModel):
    """The only response that ever carries an API key's plaintext.

    There is no endpoint that can return `secret` again. A lost key is
    replaced, not recovered - which is what makes digest-only storage possible.
    """

    api_key: ApiKeySummary
    secret: str = Field(description="Shown once. Store it now; it cannot be retrieved later.")
