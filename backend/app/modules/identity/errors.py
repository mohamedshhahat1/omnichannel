"""Identity domain errors.

Every class here subclasses the transport-agnostic taxonomy in
`app.core.errors`, so `app.api.exception_handlers` renders them into the same
error envelope as everything else (ADR-0013) with no identity-specific
handling.

Two choices are security decisions rather than style:

* **One failure code for every authentication failure.** Unknown email, wrong
  password, unverified account, suspended account and locked account all raise
  `AuthenticationFailedError` with an identical code and message. Anything more
  helpful is an account-enumeration oracle. The distinguishing detail goes in
  `internal_message`, which is logged and never serialised.
* **Missing membership is 404, insufficient permission is 403.** If the API
  answered 403 for a tenant the caller has no membership in, that response
  would confirm the tenant exists. A caller learns a tenant exists only after
  proving they belong to it.
"""

from __future__ import annotations

from app.core.errors import (
    ConflictError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    UnprocessableEntityError,
)


class AuthenticationFailedError(UnauthorizedError):
    """Credentials were missing, malformed, expired, revoked or simply wrong.

    Deliberately undifferentiated. Callers must not be able to tell these cases
    apart from the response.
    """

    default_code = "authentication_failed"
    default_message = "Authentication failed."


class AmbiguousCredentialsError(UnauthorizedError):
    """A request presented both a session cookie and a bearer credential.

    `docs/security.md` 2.8 makes the two mutually exclusive per request. This
    is raised *before* either credential is examined, so the response cannot
    reveal which of them - if either - would have been accepted.

    It is a distinct code rather than a plain `authentication_failed` because
    the caller is a client that can fix the request, and the fix ("send one
    credential") is not guessable from a generic failure. Nothing about the
    validity of either credential is disclosed by saying that two were sent,
    which the caller already knows.
    """

    default_code = "ambiguous_credentials"
    default_message = "Present either a session cookie or an API key, not both."


class CsrfValidationError(ForbiddenError):
    """A cookie-authenticated state-changing request failed its CSRF check."""

    default_code = "csrf_validation_failed"
    default_message = "The request could not be verified. Please retry."


class OriginRejectedError(ForbiddenError):
    """A cookie-authenticated write came from an origin that is not allowed.

    `docs/security.md` 2.4 layer 3. The reason - unparsable header, missing
    header, foreign origin - stays in `internal_message`, because naming it
    would let a caller map the allowlist one request at a time.
    """

    default_code = "origin_rejected"
    default_message = "The request could not be verified. Please retry."


class PermissionDeniedError(ForbiddenError):
    """The principal belongs to the tenant but lacks the required permission."""

    default_code = "permission_denied"
    default_message = "You do not have permission to perform this action."


class TenantNotFoundError(NotFoundError):
    """The tenant does not exist, or the caller has no membership in it.

    The two cases are indistinguishable on purpose; see the module docstring.
    """

    default_code = "tenant_not_found"
    default_message = "The requested tenant was not found."


class MembershipNotFoundError(NotFoundError):
    """No membership matched within the caller's tenant."""

    default_code = "membership_not_found"
    default_message = "The requested membership was not found."


class RoleNotFoundError(NotFoundError):
    """The role does not exist, or belongs to a different tenant."""

    default_code = "role_not_found"
    default_message = "The requested role was not found."


class ApiKeyNotFoundError(NotFoundError):
    """No API key matched within the caller's tenant."""

    default_code = "api_key_not_found"
    default_message = "The requested API key was not found."


class TenantSlugTakenError(ConflictError):
    """Another tenant already uses this slug.

    Unlike email addresses, slugs are a shared public namespace: the caller has
    to be told, and being told reveals nothing about any person.
    """

    default_code = "tenant_slug_taken"
    default_message = "That workspace address is already taken."


class MembershipAlreadyExistsError(ConflictError):
    """The person already has a membership in this tenant."""

    default_code = "membership_already_exists"
    default_message = "That person is already a member of this workspace."


class MembershipTransitionError(ConflictError):
    """The membership cannot move from its current status to the requested one.

    Raised when activation is attempted on a membership that is neither invited
    nor already active - a suspended one, for instance. Reinstating suspended
    access is a separate decision with its own authorisation story, and letting
    an activation call perform it silently would turn "accept an invitation"
    into "undo a suspension".
    """

    default_code = "membership_transition_invalid"
    default_message = "That membership cannot be activated from its current state."


class LastOwnerError(ConflictError):
    """The change would leave the tenant with no active owner."""

    default_code = "last_owner"
    default_message = "A workspace must always have at least one active owner."


class IdentityValidationError(UnprocessableEntityError):
    """A domain rule rejected the input.

    Raised for normalisation and password-policy failures. The message is
    written to be shown to a person and never echoes the value that failed,
    because that value is sometimes a password.
    """

    default_code = "identity_validation_failed"
    default_message = "The submitted details are not valid."
