# omnichannel

Multi-tenant SaaS platform for AI-powered omnichannel customer conversations across WhatsApp, Instagram DM, Facebook Messenger, and Instagram/Facebook comments.

> **Current phase: 1 — Repository & application foundation (complete).**
> The `backend/` FastAPI foundation exists: configuration, logging, correlation, error handling, health checks, tracing bootstrap and security headers. There is no database, queue, authentication or business functionality yet — those begin in Phase 2, which should not be started until it is explicitly approved.

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
| [backend/](backend/) | FastAPI application. See [backend/README.md](backend/README.md) for setup, running it locally, configuration and the quality gates. |

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
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
