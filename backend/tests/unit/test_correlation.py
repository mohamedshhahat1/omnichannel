"""Correlation identifier handling."""

import pytest

from app.platform.correlation import (
    MAX_ID_LENGTH,
    MIN_ID_LENGTH,
    CorrelationIds,
    bind,
    get_correlation_id,
    get_request_id,
    is_valid_external_id,
    new_id,
    resolve_correlation_ids,
    sanitize_external_id,
    unbind,
)


def test_generated_ids_are_unique_and_self_valid() -> None:
    first = new_id()
    second = new_id()
    assert first != second
    assert is_valid_external_id(first)


@pytest.mark.parametrize(
    "candidate",
    [
        "contains a space",
        "contains\nnewline-injection",
        "contains\ttab-character",
        "contains;semicolon;chars",
        "contains/slash/chars",
        "contains%2Fencoded",
        "<script>alert(1)</script>",
    ],
)
def test_unsafe_characters_are_rejected(candidate: str) -> None:
    assert not is_valid_external_id(candidate)


def test_length_bounds_are_enforced() -> None:
    assert not is_valid_external_id("a" * (MIN_ID_LENGTH - 1))
    assert not is_valid_external_id("a" * (MAX_ID_LENGTH + 1))
    assert is_valid_external_id("a" * MIN_ID_LENGTH)
    assert is_valid_external_id("a" * MAX_ID_LENGTH)


def test_missing_values_are_rejected() -> None:
    assert not is_valid_external_id(None)
    assert not is_valid_external_id("")


def test_sanitize_passes_safe_values_and_drops_unsafe_ones() -> None:
    assert sanitize_external_id("valid-id-1234") == "valid-id-1234"
    assert sanitize_external_id("invalid value") is None


def test_inbound_request_id_is_ignored_when_not_trusted() -> None:
    ids = resolve_correlation_ids(
        inbound_request_id="client-chosen-identifier",
        inbound_correlation_id=None,
        trust_inbound_request_id=False,
    )
    assert ids.request_id != "client-chosen-identifier"
    assert is_valid_external_id(ids.request_id)


def test_inbound_request_id_is_used_when_trusted() -> None:
    ids = resolve_correlation_ids(
        inbound_request_id="proxy-generated-identifier",
        inbound_correlation_id=None,
        trust_inbound_request_id=True,
    )
    assert ids.request_id == "proxy-generated-identifier"


def test_trusted_but_malformed_request_id_is_replaced() -> None:
    ids = resolve_correlation_ids(
        inbound_request_id="malformed\nidentifier",
        inbound_correlation_id=None,
        trust_inbound_request_id=True,
    )
    assert ids.request_id != "malformed\nidentifier"
    assert is_valid_external_id(ids.request_id)


def test_correlation_id_defaults_to_request_id() -> None:
    ids = resolve_correlation_ids(
        inbound_request_id=None,
        inbound_correlation_id=None,
        trust_inbound_request_id=False,
    )
    assert ids.correlation_id == ids.request_id


def test_valid_inbound_correlation_id_joins_the_existing_workflow() -> None:
    ids = resolve_correlation_ids(
        inbound_request_id=None,
        inbound_correlation_id="workflow-abc-123",
        trust_inbound_request_id=False,
    )
    assert ids.correlation_id == "workflow-abc-123"
    assert ids.request_id != ids.correlation_id


def test_malformed_inbound_correlation_id_is_replaced() -> None:
    ids = resolve_correlation_ids(
        inbound_request_id=None,
        inbound_correlation_id="workflow abc\r\nInjected-Header: 1",
        trust_inbound_request_id=False,
    )
    assert ids.correlation_id == ids.request_id


def test_bind_then_unbind_restores_the_previous_context() -> None:
    tokens = bind(CorrelationIds(request_id="request-0001", correlation_id="workflow-0001"))
    try:
        assert get_request_id() == "request-0001"
        assert get_correlation_id() == "workflow-0001"
    finally:
        unbind(tokens)

    assert get_request_id() is None
    assert get_correlation_id() is None


def test_nested_binding_restores_the_outer_values() -> None:
    outer = bind(CorrelationIds(request_id="outer-request", correlation_id="outer-workflow"))
    inner = bind(CorrelationIds(request_id="inner-request", correlation_id="inner-workflow"))
    assert get_request_id() == "inner-request"

    unbind(inner)
    assert get_request_id() == "outer-request"

    unbind(outer)
    assert get_request_id() is None
