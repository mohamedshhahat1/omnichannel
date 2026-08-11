"""Business domain modules.

This package is intentionally empty in Phase 1. Domain modules are created by
the phase that implements them, so that the tree never fills with placeholder
packages that assert boundaries nobody is enforcing yet.

Planned physical modules (ADR-0011), each owning its own tables and exposing a
public service interface:

    identity   messaging   channels   events   ai
    knowledge  catalog     billing    media    crawler

Module rules (ENGINEERING.md section 1):

1. A module may call another module's public service interface only - never its
   repositories, ORM models or private services.
2. A table has exactly one owning module.
3. Dependencies point inward: api -> services -> domain -> repositories.
4. Cross-module asynchronous communication goes through `events` (the
   transactional outbox), not through direct imports.
"""

__all__: list[str] = []
