# omnichannel

Multi-tenant SaaS platform for AI-powered omnichannel customer conversations across WhatsApp, Instagram DM, Facebook Messenger, and Instagram/Facebook comments.

> **Current phase: 3 — Identity & access.**
> The `backend/` FastAPI foundation includes configuration, logging, correlation, error handling, health checks, tracing bootstrap, PostgreSQL/Redis/Celery infrastructure, Alembic, Docker/Compose, and now users, tenants, memberships, RBAC, sessions, API keys and audit logging. There is still no conversation, channel, AI, catalog or billing functionality.

## Read this first

This repository's documentation is the persistent source of truth for both human engineers and AI coding sessions.

| Order | File | Purpose |
|---|---|---|
| 1 | [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) | Vision, users, channels, stack, strategy, constraints |
| 2 | [CURRENT_STATE.md](CURRENT_STATE.md) | What exists now, current task, next task, known issues |
| 3 | [ENGINEERING.md](ENGINEERING.md) | Non-negotiable engineering rules |
| 4 | [DECISIONS.md](DECISIONS.md) | Architecture Decision Records (ADRs) |
| 5 | [TODO.md](TODO.md) | Prioritised backlog |

## Code

| Path | Purpose |
|---|---|
| [backend/](backend/) | FastAPI application, infrastructure foundation, and the identity module. |

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head          # migrations are never run from app startup
uvicorn app.main:app --reload
```

For a production-shaped local stack, use Docker Compose:

```bash
cp backend/.env.example backend/.env
docker compose up --build
```

## Architecture documentation

| File | Purpose |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System architecture, module boundaries, repository structure |
| [docs/database.md](docs/database.md) | Data model, multi-tenancy, indexing, migration policy |
| [docs/security.md](docs/security.md) | Security architecture, authentication, threat model |
| [docs/ai.md](docs/ai.md) | AI orchestration, tools, RAG, crawler pipeline, visual search |
| [docs/messaging.md](docs/messaging.md) | Channels, webhooks, transactional outbox, conversations, handoff |
| [docs/billing.md](docs/billing.md) | Billing, plans, entitlements, usage metering |
| [docs/deployment.md](docs/deployment.md) | Local development, CI/CD, production deployment |
| [docs/observability.md](docs/observability.md) | OpenTelemetry, metrics, logging, alerting |
| [docs/integrations.md](docs/integrations.md) | External provider contracts and abstractions |
| [docs/operations.md](docs/operations.md) | Runbooks, backup/DR, incident response |

## Phase 2 foundation now present

- Async SQLAlchemy 2.x + asyncpg engine, session lifecycle, and naming conventions
- Async Alembic environment with one initial extension-only migration
- Async Redis client and tenant-safe key namespace `oc:{env}:t:{tenant_id}:...`
- Celery app with Redis broker, `critical`, `default`, and `background` queues
- Worker entrypoint and infrastructure smoke task with tenant/correlation/trace propagation
- Dockerfile, Compose stack, PostgreSQL role bootstrap, and Makefile targets
- `/health/live` remains dependency-free; `/health/ready` checks PostgreSQL and Redis with bounded timeouts
- Unit tests plus opt-in real PostgreSQL/Redis integration tests; SQLite is never used

## Phase 3 identity now present

- Eleven identity tables in migration `0002_identity_access`, which also seeds the 15-permission catalogue and the six system roles
- Opaque, revocable sessions in `__Host-` cookies; only SHA-256 digests are stored, never the token
- Argon2id password hashing with rehash detection and a production cost floor the settings validator refuses to start below
- Double-submit CSRF on every unsafe method; bearer API keys bypass it because they are not cookies
- Tenant-scoped API keys — shown once at creation, always expiring, revocable, and never grantable beyond the creator's own permissions
- RBAC enforced in the service layer, so a future Celery task or AI tool uses the identical check
- `TenantScopedRepository`, so tenant isolation is structural rather than a filter each caller must remember
- Enumeration resistance: identical responses for unknown vs. registered addresses, and `404` rather than `403` for cross-tenant objects
- Audit logging with context scrubbing, covering authentication, membership, role and API-key events
- 13 endpoints under `/api/v1`, plus unit and integration tests including negative cross-tenant and adversarial cases

The reasoning behind the parts of this that are not obvious is in [ADR-0015](DECISIONS.md#adr-0015--identity-tenancy-and-credential-handling).

## Core architectural commitments

- Modular monolith — not microservices
- One PostgreSQL database, shared schema, `tenant_id` isolation enforced in the application layer
- **Transactional outbox from day one** — the event record and the outbox record are committed in the same transaction
- Celery + Redis for asynchronous work; Redis is never a source of truth
- pgvector for retrieval until a measurable trigger says otherwise
- All external providers (Meta, Paddle, LLMs, object storage) behind internal abstractions
- The LLM never touches the database; it acts only through validated, tenant-scoped, audited tools
- OpenTelemetry for tracing/correlation, Prometheus/Grafana for metrics, Sentry for errors
- No Kubernetes, Kafka, Elasticsearch, or service mesh without a documented trigger

## Dependency lock caveat

`backend/requirements.lock` is intentionally a temporary, offline-authored direct pin set because the authoring sandbox had no resolver/network access. It is **not** a fully resolved production lock and must be regenerated and validated in a networked environment before reproducible builds are claimed.
