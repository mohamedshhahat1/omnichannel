"""Credential primitives: password hashing, opaque tokens, constant-time compare.

Nothing here knows about HTTP, SQLAlchemy or tenants. This is the one place
that decides *how* a secret becomes something safe to store, so a review of
credential handling only has to read this file.

The scheme is fixed by `docs/security.md` 2 and ADR-0009, and is deliberately
not configurable per call site:

* **Passwords** use Argon2id. Cost parameters come from `AuthSettings` so they
  can be raised as hardware improves; `Settings` enforces a production floor so
  lowering them for a fast test suite can never reach a deployment.
* **Session tokens, API-key secrets and email tokens** are 256-bit CSPRNG
  values, and only their SHA-256 digest is persisted. A slow KDF would be the
  wrong tool: these are full-entropy random strings rather than user-chosen
  secrets, so there is no dictionary to attack, and authentication has to stay
  a single indexed lookup.
* Every comparison of secret material is constant time.

Plaintext credential material is returned to a caller exactly once, at
creation. It is never assigned to a model attribute, a log record, an error
message or a span attribute.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from typing import Final

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError

from app.core.settings import AuthSettings, Environment

TOKEN_ENTROPY_BYTES: Final = 32
"""256 bits: the floor for every opaque token the platform issues."""

API_KEY_NAMESPACE: Final = "oc"
_API_KEY_SEGMENTS: Final = 4
_KEY_ID_BYTES: Final = 8
_DECOY_CANDIDATE_BYTES: Final = 16

_ENVIRONMENT_LABELS: Final[dict[Environment, str]] = {
    Environment.DEVELOPMENT: "dev",
    Environment.TEST: "test",
    Environment.PRODUCTION: "live",
}


def environment_label(environment: Environment) -> str:
    """Return the short, stable label embedded in issued API keys.

    The label exists so that a leaked key is immediately identifiable as
    production or not, which is the difference between a routine rotation and
    an incident.
    """
    return _ENVIRONMENT_LABELS[environment]


def generate_token(entropy_bytes: int = TOKEN_ENTROPY_BYTES) -> str:
    """Return a URL-safe, unpadded CSPRNG string of at least 256 bits."""
    if entropy_bytes < TOKEN_ENTROPY_BYTES:
        raise ValueError("opaque tokens must carry at least 256 bits of entropy")
    return secrets.token_urlsafe(entropy_bytes)


def digest_secret(value: str) -> str:
    """Return the lowercase SHA-256 hex digest stored in place of a secret."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def secrets_equal(left: str, right: str) -> bool:
    """Compare secret material without leaking its contents through timing."""
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class PasswordVerification:
    """Outcome of checking a candidate password against a stored hash."""

    matched: bool
    needs_rehash: bool


class PasswordHashingService:
    """Argon2id hashing with transparent parameter upgrades.

    One instance is built per application and reused: constructing a
    `PasswordHasher` is cheap, but sharing one keeps the configured cost
    parameters in exactly one place.
    """

    def __init__(self, settings: AuthSettings) -> None:
        self._hasher = PasswordHasher(
            time_cost=settings.argon2_time_cost,
            memory_cost=settings.argon2_memory_kib,
            parallelism=settings.argon2_parallelism,
            hash_len=settings.argon2_hash_bytes,
            salt_len=settings.argon2_salt_bytes,
        )
        self._decoy_hash: str | None = None

    def hash(self, password: str) -> str:
        """Return an Argon2id PHC string. The plaintext is never retained."""
        try:
            return self._hasher.hash(password)
        except HashingError as exc:
            # The exception text can embed the parameters that failed; it must
            # not reach the client, and the plaintext must not reach anything.
            raise RuntimeError("password hashing failed") from exc

    def verify(self, *, stored_hash: str, candidate: str) -> PasswordVerification:
        """Check a candidate password, reporting whether the hash is stale.

        A malformed stored hash is treated as a failed verification rather than
        an error: a corrupt row must not become a 500 that tells an attacker
        the account exists.
        """
        try:
            self._hasher.verify(stored_hash, candidate)
        except (VerificationError, InvalidHashError):
            return PasswordVerification(matched=False, needs_rehash=False)
        return PasswordVerification(
            matched=True,
            needs_rehash=self._hasher.check_needs_rehash(stored_hash),
        )

    def spend_verification_budget(self) -> None:
        """Burn comparable CPU when there is no stored credential to check.

        Without this, "unknown email" answers in microseconds while "known
        email, wrong password" takes ~100 ms. That difference is a free
        account-enumeration oracle, so the unknown-account path does the same
        work against a throwaway hash.
        """
        if self._decoy_hash is None:
            self._decoy_hash = self._hasher.hash(generate_token())
        self.verify(
            stored_hash=self._decoy_hash,
            candidate=secrets.token_urlsafe(_DECOY_CANDIDATE_BYTES),
        )


@dataclass(frozen=True, slots=True)
class ApiKeyMaterial:
    """A freshly minted API key.

    `plaintext` is the only copy that will ever exist. It is handed to the
    caller once and then discarded; only `secret_digest` is persisted.
    """

    key_id: str
    plaintext: str
    secret_digest: str


@dataclass(frozen=True, slots=True)
class ParsedApiKey:
    """The public identifier and secret half of a presented API key."""

    environment: str
    key_id: str
    secret: str


def mint_api_key(label: str) -> ApiKeyMaterial:
    """Mint `oc_{env}_{key_id}_{secret}` as specified by `docs/security.md` 2.

    `key_id` is public and indexed so authentication is a single lookup rather
    than a scan that hashes every stored key.
    """
    key_id = secrets.token_hex(_KEY_ID_BYTES)
    secret = generate_token()
    return ApiKeyMaterial(
        key_id=key_id,
        plaintext=f"{API_KEY_NAMESPACE}_{label}_{key_id}_{secret}",
        secret_digest=digest_secret(secret),
    )


def parse_api_key(presented: str) -> ParsedApiKey | None:
    """Split a presented API key, or return None when it is not well formed.

    The split is bounded at four segments so that a secret containing `_` (the
    URL-safe alphabet includes it) survives parsing intact.
    """
    parts = presented.split("_", _API_KEY_SEGMENTS - 1)
    if len(parts) != _API_KEY_SEGMENTS:
        return None
    namespace, label, key_id, secret = parts
    if namespace != API_KEY_NAMESPACE or not label or not key_id or not secret:
        return None
    return ParsedApiKey(environment=label, key_id=key_id, secret=secret)
