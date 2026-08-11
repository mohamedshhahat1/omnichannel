# PROJECT_CONTEXT

> **Status:** Phase 0 — Architecture approved with corrections applied.
> **Audience:** Human engineers and future AI coding sessions.
> **Rule:** This file, together with `CURRENT_STATE.md`, `ENGINEERING.md`, `DECISIONS.md`, `TODO.md` and `docs/`, is the persistent source of truth for this project. Read these before writing any code.

---

## 1. Product vision

An omnichannel, AI-powered customer conversation platform for businesses.

A business (a **tenant**) connects its messaging channels, uploads its own knowledge (documents, policies, FAQs, product data, website content), and the platform answers its customers automatically using **that tenant's own authoritative data** — escalating to a human agent when appropriate.

The platform serves two long-term use cases:

1. **AI customer support** — answering questions, order status, policies, troubleshooting, escalation.
2. **AI sales / commerce** — product discovery, recommendations, inventory checks, assisted ordering.

---

## 2. Target users

| User | Needs |
|---|---|
| Business owner / operator (tenant admin) | Connect channels, upload knowledge, configure the AI, see conversations, manage billing |
| Human support agent | Take over conversations, see context and AI summaries, reply, hand back to AI |
| End customer | Fast, correct answers on the channel they already use |
| Platform operator (us) | Operate, observe, support and bill many tenants safely from a small team |

---

## 3. Supported channels

| Channel | Type | Phase |
|---|---|---|
| WhatsApp (Cloud API) | Direct message | Launch |
| Instagram Direct | Direct message | Launch |
| Facebook Messenger | Direct message | Launch |
| Instagram comments | Public comment + private reply | Launch |
| Facebook comments | Public comment + private reply | Launch |

All channels are normalized into **one internal conversation/message model**. The domain never branches on "is this WhatsApp?" for core behaviour — only channel *capabilities* differ.

---

## 4. Major capabilities

**Launch-critical**

- Multi-tenancy with strong tenant isolation
- Authentication, tenants, memberships, RBAC
- Channel connection + webhook ingestion (idempotent, transactional outbox)
- Normalized conversations, contacts, messages
- AI responses grounded in tenant knowledge (RAG) and authoritative data (tools)
- Human handoff and hand-back
- Product catalog as authoritative commerce data
- Subscription billing, plans, entitlements, usage metering
- Observability (OpenTelemetry traces, Prometheus metrics, Sentry errors)
- Backups with tested restore

**Designed for, deliberately deferred**

- Website crawler (untrusted workload, strict SSRF controls)
- Visual product search (image embeddings, tenant-scoped similarity)
- Order creation through controlled AI tools
- MFA, SSO, custom roles

---

## 5. Technology stack

**Backend**

- Python, FastAPI, Pydantic
- SQLAlchemy 2.x, Alembic
- PostgreSQL (+ `pgvector`)
- Redis
- Celery (workers + beat)
- Pytest, Ruff, MyPy

**Infrastructure**

- Docker, Docker Compose (local and initial production)
- NGINX (TLS termination, reverse proxy)
- Prometheus + Grafana (metrics)
- OpenTelemetry (traces + correlation)
- Sentry or equivalent (error tracking)
- GitHub Actions (CI/CD)
- S3-compatible object storage

**External providers (all behind internal abstractions)**

- Meta Graph / WhatsApp Cloud API — messaging channels
- Paddle — billing
- LLM + embedding provider(s) — AI
- S3-compatible storage provider — media

---

## 6. Architectural strategy

1. **Modular monolith first.** One deployable backend, strict internal module boundaries, extraction only when a measurable trigger appears. (ADR-0001)
2. **Shared-schema multi-tenancy.** One PostgreSQL database, `tenant_id` on every tenant-owned row, isolation enforced at the application/query boundary; RLS later as defence-in-depth. (ADR-0002)
3. **Transactional outbox from day one.** Durable event record and outbox record are written in the *same* transaction; a dispatcher publishes to Celery. We never rely on `commit → enqueue` as an atomic step. (ADR-0003)
4. **Everything heavy is asynchronous.** Webhooks verify, persist, and return. AI, embedding, crawling, delivery and aggregation run in workers.
5. **Providers are replaceable.** Meta, Paddle, LLM vendors and object storage sit behind internal interfaces.
6. **The LLM is untrusted and has no database access.** It acts only through validated, tenant-scoped, audited application tools.
7. **Retrieved and crawled content is untrusted data, never instructions.**
8. **Observable by default.** OpenTelemetry trace context flows from HTTP → outbox → Celery → AI run → provider call. (ADR-0010)
9. **Simplicity is a feature.** No Kubernetes, Kafka, Elasticsearch, service mesh or microservices without a documented trigger.

---

## 7. Important constraints

- **Small engineering team.** Operational complexity is a real cost and is treated as an architectural constraint.
- **Single production server initially**, with a documented path to API/worker/database separation without a rewrite.
- **Tenant isolation is the highest-severity risk** in the system; a leak is worse than downtime.
- **AI cost is variable and must be metered and capped** per tenant.
- **Meta and Paddle APIs change**; integration code must be isolated and independently testable.
- **Compliance posture is not yet defined** — PII minimisation and audit logging are applied pre-emptively.

---

## 8. Long-term goals

| Horizon | Goal |
|---|---|
| Near | Production-ready single-server SaaS serving real paying tenants |
| Mid | Dedicated AI and crawler workers, managed PostgreSQL/Redis, richer agent workspace, commerce integrations |
| Long | Extract high-load modules (webhook ingestion, AI processing, crawler) into services **only** where scale, reliability or team ownership justifies it |

---

## 9. Document map

| File | Purpose |
|---|---|
| `PROJECT_CONTEXT.md` | Vision, users, channels, stack, strategy, constraints (this file) |
| `CURRENT_STATE.md` | What exists today, current/next task, known issues, tech debt |
| `ENGINEERING.md` | Non-negotiable engineering rules |
| `DECISIONS.md` | Architecture Decision Records |
| `TODO.md` | Prioritised backlog (P0/P1/P2) |
| `docs/architecture.md` | System architecture, modules, boundaries, structure |
| `docs/database.md` | Data model, tenancy, indexing, migration policy |
| `docs/security.md` | Security architecture and threat model |
| `docs/ai.md` | AI, RAG, knowledge, crawler pipeline, visual search |
| `docs/messaging.md` | Channels, webhooks, outbox, conversations, handoff |
| `docs/billing.md` | Billing, entitlements, usage metering |
| `docs/deployment.md` | Local dev, CI/CD, production deployment |
| `docs/observability.md` | OpenTelemetry, metrics, logs, alerting |
| `docs/integrations.md` | External provider contracts and abstractions |
| `docs/operations.md` | Runbooks, backup/DR, incident response |
