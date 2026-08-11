"""Identity and access module (Phase 3).

Owns users, tenants, memberships, roles, permissions, sessions, API keys,
single-use email tokens and the identity audit trail. Everything that answers
"who is calling, which tenant are they in, and what may they do" lives here.

Layering, per ADR-0011:

* `domain`        - pure rules: the permission catalogue, role grants,
                    principals, and input normalisation. No I/O.
* `models`        - SQLAlchemy tables.
* `repositories`  - tenant-scoped data access. The only place that builds
                    queries against the tables above.
* `services`      - use cases. Enforcement lives here, not in route decorators,
                    so a Celery task gets the same checks as an HTTP request.
* `api`           - HTTP surface: schemas, dependencies, routes.
"""
