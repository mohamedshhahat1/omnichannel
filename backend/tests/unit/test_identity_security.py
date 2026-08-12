"""Credential primitives.

The recurring assertion in this module is negative: given a secret, nothing
derived from it may contain it. Hashes, digests, error text and API-key
material are all checked for leakage, because every credential disclosure bug
looks exactly like a working feature until someone reads a log.
"""

import hashlib

import pytest

from app.core.security import (
    API_KEY_NAMESPACE,
    TOKEN_ENTROPY_BYTES,
    PasswordHashingService,
    digest_secret,
    environment_label,
    generate_token,
    mint_api_key,
    parse_api_key,
    secrets_equal,
)
from app.core.settings import AuthSettings, Environment

PASSWORD = "correct horse battery staple"


@pytest.fixture(scope="module")
def hashing() -> PasswordHashingService:
    """Argon2id at the lowest cost the settings allow.

    Production cost would add roughly 100 ms to every hash in this module. The
    production floor is enforced by `Settings`, and
    `tests/unit/test_auth_settings.py` is what proves these values could never
    reach a deployment.
    """
    return PasswordHashingService(
        AuthSettings(argon2_time_cost=1, argon2_memory_kib=8_192, argon2_parallelism=1)
    )


def test_tokens_carry_at_least_256_bits() -> None:
    token = generate_token()
    # token_urlsafe emits ~1.34 characters per byte of entropy.
    assert len(token) >= TOKEN_ENTROPY_BYTES


def test_tokens_are_unique() -> None:
    assert len({generate_token() for _ in range(256)}) == 256


def test_tokens_are_url_safe() -> None:
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    assert set(generate_token()) <= allowed


def test_weak_entropy_requests_are_refused() -> None:
    # A caller must not be able to ask for a shorter token than the policy.
    with pytest.raises(ValueError):
        generate_token(16)


def test_digest_matches_sha256() -> None:
    assert digest_secret("abc") == hashlib.sha256(b"abc").hexdigest()


def test_digest_is_lowercase_hex_of_fixed_width() -> None:
    digest = digest_secret(generate_token())
    assert len(digest) == 64
    assert digest == digest.lower()
    assert set(digest) <= set("0123456789abcdef")


def test_digest_is_deterministic_and_input_dependent() -> None:
    assert digest_secret("same") == digest_secret("same")
    assert digest_secret("a") != digest_secret("b")


def test_digest_does_not_contain_the_secret() -> None:
    secret = generate_token()
    assert secret not in digest_secret(secret)


def test_constant_time_comparison_is_still_correct() -> None:
    assert secrets_equal("same-value", "same-value")
    assert not secrets_equal("a-value", "another-value")
    assert not secrets_equal("value", "value ")
    assert not secrets_equal("", "value")


def test_password_hash_is_argon2id(hashing: PasswordHashingService) -> None:
    # The scheme is fixed by docs/security.md 2. A silent downgrade to argon2i
    # or bcrypt would still verify, and would still be a weaker control.
    assert hashing.hash(PASSWORD).startswith("$argon2id$")


def test_password_hash_is_salted(hashing: PasswordHashingService) -> None:
    assert hashing.hash(PASSWORD) != hashing.hash(PASSWORD)


def test_password_hash_does_not_contain_the_password(hashing: PasswordHashingService) -> None:
    assert PASSWORD not in hashing.hash(PASSWORD)


def test_correct_password_verifies(hashing: PasswordHashingService) -> None:
    result = hashing.verify(stored_hash=hashing.hash(PASSWORD), candidate=PASSWORD)
    assert result.matched
    assert not result.needs_rehash


@pytest.mark.parametrize(
    "candidate",
    ["wrong password entirely", PASSWORD.upper(), PASSWORD + " ", "", PASSWORD[:-1]],
)
def test_wrong_password_does_not_verify(
    hashing: PasswordHashingService,
    candidate: str,
) -> None:
    stored = hashing.hash(PASSWORD)
    assert not hashing.verify(stored_hash=stored, candidate=candidate).matched


@pytest.mark.parametrize(
    "stored",
    ["", "not-a-hash", "$argon2id$truncated", "$2b$12$abcdefghijklmnopqrstuv"],
)
def test_malformed_stored_hash_fails_closed(
    hashing: PasswordHashingService,
    stored: str,
) -> None:
    # A corrupt row must be a failed login, not a 500 that confirms the account
    # exists and hands an attacker a stack trace.
    assert not hashing.verify(stored_hash=stored, candidate=PASSWORD).matched


def test_stale_parameters_are_reported_for_rehash() -> None:
    weak = PasswordHashingService(
        AuthSettings(argon2_time_cost=1, argon2_memory_kib=8_192, argon2_parallelism=1)
    )
    stronger = PasswordHashingService(
        AuthSettings(argon2_time_cost=2, argon2_memory_kib=16_384, argon2_parallelism=1)
    )
    stored = weak.hash(PASSWORD)
    result = stronger.verify(stored_hash=stored, candidate=PASSWORD)
    assert result.matched
    assert result.needs_rehash


def test_verification_budget_can_be_spent_repeatedly(hashing: PasswordHashingService) -> None:
    # The unknown-account path calls this so that "no such user" and "wrong
    # password" cost the same. It must be safe to call and must never raise.
    hashing.spend_verification_budget()
    hashing.spend_verification_budget()


@pytest.mark.parametrize(
    ("environment", "label"),
    [
        (Environment.DEVELOPMENT, "dev"),
        (Environment.TEST, "test"),
        (Environment.PRODUCTION, "live"),
    ],
)
def test_environment_labels_are_stable(environment: Environment, label: str) -> None:
    # A leaked key has to be identifiable as production at a glance.
    assert environment_label(environment) == label


def test_minted_key_follows_the_documented_format() -> None:
    material = mint_api_key("live")
    assert material.plaintext.startswith(f"{API_KEY_NAMESPACE}_live_")
    assert material.key_id in material.plaintext


def test_only_the_digest_of_the_secret_is_retained() -> None:
    material = mint_api_key("test")
    parsed = parse_api_key(material.plaintext)
    assert parsed is not None
    assert digest_secret(parsed.secret) == material.secret_digest
    # The stored half must not be sufficient to reconstruct the presented key.
    assert parsed.secret not in material.secret_digest
    assert material.plaintext not in material.secret_digest


def test_minted_keys_are_unique() -> None:
    keys = [mint_api_key("test") for _ in range(64)]
    assert len({key.key_id for key in keys}) == 64
    assert len({key.secret_digest for key in keys}) == 64


def test_parse_round_trips_a_minted_key() -> None:
    material = mint_api_key("dev")
    parsed = parse_api_key(material.plaintext)
    assert parsed is not None
    assert parsed.environment == "dev"
    assert parsed.key_id == material.key_id


def test_parse_preserves_underscores_inside_the_secret() -> None:
    # token_urlsafe includes '_', so an unbounded split would silently corrupt
    # roughly half of all issued keys.
    parsed = parse_api_key("oc_live_abcd1234_secret_with_underscores")
    assert parsed is not None
    assert parsed.key_id == "abcd1234"
    assert parsed.secret == "secret_with_underscores"


@pytest.mark.parametrize(
    "presented",
    [
        "",
        "oc",
        "oc_live",
        "oc_live_keyid",
        "xx_live_keyid_secret",
        "_live_keyid_secret",
        "oc__keyid_secret",
        "oc_live__secret",
        "nonsense",
    ],
)
def test_malformed_keys_are_rejected_without_raising(presented: str) -> None:
    assert parse_api_key(presented) is None
