"""Time-ordered UUID identifiers.

Every Phase 3 table uses a UUID primary key, which `docs/database.md` 3.1 makes
the project-wide convention. Plain UUIDv4 keys are the obvious implementation
and the wrong one at scale: they scatter inserts uniformly across the primary
key B-tree, so every insert dirties a different page and the index never stays
resident in cache.

UUIDv7 (RFC 9562) keeps global uniqueness while making keys roughly
insert-ordered - a 48-bit big-endian millisecond timestamp followed by 74 bits
of CSPRNG entropy. CPython 3.13 has no `uuid.uuid7`, so the layout is
implemented here rather than taking a dependency for a dozen lines of bit
shifting.

The timestamp is not a secret, but it is *visible*: a UUIDv7 leaks its own
creation time. That is acceptable for the rows here and deliberately not used
for anything an unauthenticated caller can see - session tokens and API-key
secrets come from `app.core.security`, never from an identifier.
"""

from __future__ import annotations

import os
import time
from typing import Final
from uuid import UUID

_TIMESTAMP_BITS: Final = 48
_TIMESTAMP_MASK: Final = (1 << _TIMESTAMP_BITS) - 1
_RAND_A_MASK: Final = (1 << 12) - 1
_RAND_B_MASK: Final = (1 << 62) - 1
_VERSION: Final = 0x7
_VARIANT: Final = 0b10
_ENTROPY_BYTES: Final = 10
_UUID7_VERSION: Final = 7


def new_uuid7() -> UUID:
    """Return a fresh, time-ordered UUIDv7.

    Ordering is millisecond-granular. Two identifiers minted inside the same
    millisecond are ordered arbitrarily with respect to each other, which is
    fine for index locality and is why no code may treat a UUIDv7 as a
    tie-breaking sequence number.
    """
    timestamp_ms = time.time_ns() // 1_000_000
    entropy = int.from_bytes(os.urandom(_ENTROPY_BYTES), "big")
    high = (timestamp_ms & _TIMESTAMP_MASK) << 80
    version = _VERSION << 76
    rand_a = ((entropy >> 62) & _RAND_A_MASK) << 64
    variant = _VARIANT << 62
    rand_b = entropy & _RAND_B_MASK
    return UUID(int=high | version | rand_a | variant | rand_b)


def uuid7_timestamp_ms(value: UUID) -> int:
    """Return the embedded creation timestamp of a UUIDv7 in milliseconds."""
    if value.version != _UUID7_VERSION:
        raise ValueError("value is not a UUIDv7")
    return value.int >> 80
