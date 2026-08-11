"""Application error taxonomy."""

import pytest

from app.core.errors import (
    AppError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    InternalError,
    NotFoundError,
    PayloadTooLargeError,
    RateLimitedError,
    ServiceUnavailableError,
    UnauthorizedError,
    UnprocessableEntityError,
)

ERROR_TYPES: list[type[AppError]] = [
    BadRequestError,
    UnauthorizedError,
    ForbiddenError,
    NotFoundError,
    ConflictError,
    PayloadTooLargeError,
    UnprocessableEntityError,
    RateLimitedError,
    InternalError,
    ServiceUnavailableError,
]

EXPECTED_STATUS: dict[type[AppError], int] = {
    BadRequestError: 400,
    UnauthorizedError: 401,
    ForbiddenError: 403,
    NotFoundError: 404,
    ConflictError: 409,
    PayloadTooLargeError: 413,
    UnprocessableEntityError: 422,
    RateLimitedError: 429,
    InternalError: 500,
    ServiceUnavailableError: 503,
}


def test_status_codes_match_http_semantics() -> None:
    for error_type, status_code in EXPECTED_STATUS.items():
        assert error_type().http_status == status_code


def test_every_error_subclasses_the_base() -> None:
    for error_type in ERROR_TYPES:
        assert issubclass(error_type, AppError)


def test_error_codes_are_unique_and_machine_readable() -> None:
    codes = [error_type().code for error_type in ERROR_TYPES]
    assert len(codes) == len(set(codes))
    for code in codes:
        assert code == code.lower()
        assert " " not in code


def test_defaults_are_present_without_arguments() -> None:
    error = NotFoundError()
    assert error.code == "not_found"
    assert error.message
    assert error.details is None
    assert error.internal_message is None


def test_message_and_code_can_be_overridden() -> None:
    error = ConflictError("That channel is already connected.", code="channel_already_connected")
    assert error.message == "That channel is already connected."
    assert error.code == "channel_already_connected"
    assert error.http_status == 409


def test_internal_message_never_reaches_the_client_facing_message() -> None:
    error = NotFoundError(internal_message="no row for tenant_id=42 conversation_id=7")
    assert error.internal_message == "no row for tenant_id=42 conversation_id=7"
    assert "tenant_id" not in error.message
    assert "tenant_id" not in str(error)


def test_details_are_carried_through() -> None:
    error = UnprocessableEntityError(details={"fields": ["name"]})
    assert error.details == {"fields": ["name"]}


def test_errors_are_catchable_by_the_base_class() -> None:
    with pytest.raises(AppError) as caught:
        raise ServiceUnavailableError()
    assert caught.value.http_status == 503
