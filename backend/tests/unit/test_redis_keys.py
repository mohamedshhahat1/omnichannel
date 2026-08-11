"""Redis keys cannot cross tenant or environment namespaces."""

import pytest

from app.core.redis import tenant_key


def test_key_uses_required_namespace() -> None:
    assert tenant_key("test", "tenant-a", "cache", "profile", "42") == (
        "oc:test:t:tenant-a:cache:profile:42"
    )


def test_tenant_ids_produce_disjoint_keys() -> None:
    assert tenant_key("test", "tenant-a", "rate", "x") != tenant_key(
        "test", "tenant-b", "rate", "x"
    )


@pytest.mark.parametrize("value", ["", "a:b", "../escape", "line\nbreak", "space value"])
def test_unsafe_segments_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        tenant_key("test", value, "cache", "x")


def test_every_segment_is_validated() -> None:
    with pytest.raises(ValueError):
        tenant_key("prod", "tenant", "cache", "x:y")
