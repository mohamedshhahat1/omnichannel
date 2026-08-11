# Architecture

> Phase 0 document. Describes the **target** architecture. No application code exists yet.
> Companion documents: `database.md`, `security.md`, `ai.md`, `messaging.md`, `billing.md`, `deployment.md`, `observability.md`, `integrations.md`, `operations.md`.

---

## 1. Executive summary

A **modular monolith** in Python/FastAPI, backed by one PostgreSQL database (shared schema, `tenant_id` isolation), Redis, and Celery workers, deployed with Docker behind NGINX.

Inbound messages and comments from WhatsApp, Instagram and Facebook arrive at thin webhook endpoints that verify the signature, validate the payload, resolve the tenant, and — **in a single PostgreSQL transaction** — write a durable `webhook_events` row and an `outbox_events` row. A dispatcher publishes outbox rows to Celery; idempotent consumers normalise the event into the internal conversation model and drive AI or human responses. The system never treats `commit → enqueue` as atomic.

AI is isolated behind internal abstractions. The LLM has no database access and can only act through validated, tenant-scoped, audited tools. Retrieval uses PostgreSQL + pgvector. All external providers (Meta, Paddle, LLM vendors, object storage) sit behind internal interfaces.

OpenTelemetry provides tracing and correlation from HTTP request through outbox, Celery, AI run and provider call. Prometheus/Grafana provide metrics; Sentry provides error tracking.

No Kubernetes, Kafka, Elasticsearch, service mesh or microservices — each is deferred behind a documented trigger.

---

## 2. Architectural principles

1. **Modular monolith first.** One deployable, strict internal boundaries, extraction only on a measurable trigger.
2. **Tenant isolation everywhere.** Not just in SQL — also Redis keys, object keys, vector filters, task payloads, caches, exports and AI tools.
3. **Trusted context only.** Tenant identity is derived server-side from the session/membership or from the resolved channel integration.
4. **Durability lives in PostgreSQL.** Redis is a cache, a broker and a coordination primitive — never a source of truth.
5. **Atomic intent.** State changes and the work they imply are committed together (transactional outbox).
6. **Idempotent by default.** Every external event and every task can safely run twice.
7. **Provider-replaceable.** Vendor APIs live in adapters behind internal interfaces.
8. **The model is untrusted.** LLM output is validated; retrieved and crawled content is data, never instructions.
9. **Observable by construction.** Correlation identifiers cross every process and time boundary.
10. **Simplicity is a feature.** Every added component must justify its operational cost.

---

## 3. High-level architecture

```mermaid
flowchart TB
    subgraph External
        META[Meta: WhatsApp / IG / FB]
        PADDLE[Paddle]
        LLM[LLM + Embedding providers]
        WEB[Tenant websites]
    end

    subgraph Edge
        NGINX[NGINX / TLS / rate limit]
    end

    subgraph App[FastAPI modular monolith]
        HTTP[Dashboard + Agent API]
        HOOK[Webhook endpoints - thin]
        IDENT[identity]
        MSG[messaging]
        CHAN[channels]
        EVT[events: webhooks + outbox]
        AI[ai]
        KNOW[knowledge]
        CAT[catalog]
        BILL[billing]
        MEDIA[media]
    end

    subgraph Workers[Celery workers]
        WEVT[events queue]
        WAI[ai queue]
        WKNOW[knowledge queue]
        WOUT[outbound queue]
        WBILL[billing queue]
        BEAT[beat: outbox dispatcher, sweeps, aggregation]
        WCRAWL[crawler queue - isolated]
    end

    subgraph Data
        PG[(PostgreSQL + pgvector)]
        REDIS[(Redis)]
        S3[(Object storage)]
    end

    subgraph Telemetry
        OTEL[OpenTelemetry collector]
        PROM[Prometheus]
        GRAF[Grafana]
        SENTRY[Sentry]
    end

    META -->|signed webhooks| NGINX
    PADDLE -->|signed webhooks| NGINX
    NGINX --> HTTP
    NGINX --> HOOK

    HOOK -->|one transaction: webhook_event + outbox_event| PG
    BEAT -->|claim pending outbox| PG
    BEAT -->|publish| REDIS
    REDIS --> WEVT
    REDIS --> WAI
    REDIS --> WKNOW
    REDIS --> WOUT
    REDIS --> WBILL
    REDIS --> WCRAWL

    WEVT --> MSG
    WAI --> AI
    WKNOW --> KNOW
    WBILL --> BILL
    WCRAWL -->|untrusted fetch| WEB

    AI --> LLM
    AI --> KNOW
    AI --> CAT
    AI --> MSG
    WOUT --> META

    App --> PG
    App --> REDIS
    App --> S3
    Workers --> PG
    Workers --> S3

    App --> OTEL
    Workers --> OTEL
    App --> PROM
    Workers --> PROM
    PROM --> GRAF
    App --> SENTRY
    Workers --> SENTRY
```

---

## 4. Detailed component architecture

### 4.1 Edge (NGINX)
TLS termination, HTTP/2, security headers, request size limits, coarse IP rate limiting, static asset serving, upstream health checks. Webhook paths get their own limits and body-size caps.

### 4.2 API process (FastAPI)
Authentication, tenant-context resolution, RBAC, Pydantic validation, thin webhook ingestion, dashboard and agent APIs, health/readiness/liveness. **No heavy work**: no AI calls, no crawling, no embedding, no large provider fan-out.

### 4.3 Worker processes (Celery)
Same codebase, different entrypoint, queue-scoped. Consume outbox-published tasks; own AI generation, retrieval indexing, document processing, outbound delivery, billing event processing, usage aggregation, sweeps and (later) crawling.

### 4.4 Beat / scheduler
Outbox dispatcher, stuck-event reclaim, dead-letter alerting, usage aggregation, billing reconciliation, token/session cleanup, backup verification hooks.

### 4.5 Data plane
PostgreSQL is the system of record, including vectors. Redis carries the broker, caches, rate limits and short locks. Object storage carries all binaries with tenant-scoped keys.

---

## 5. Module boundaries — conceptual vs physical

**Conceptual domain** = how we reason about ownership. **Physical package** = what exists on disk. They are deliberately not one-to-one (ADR-0011).

| # | Conceptual domain | Physical package | Sub-packages | Owns (tables) |
|---|---|---|---|---|
| 1 | Identity & Access | `identity` | `users`, `auth`, `sessions`, `apikeys`, `tenancy`, `rbac` | users, credentials, sessions, api_keys, email_tokens, tenants, memberships, roles, permissions |
| 2 | Conversations | `messaging` | `contacts`, `conversations`, `messages`, `handoff` | contacts, conversations, messages, message_attachments, assignments, handoff_requests, conversation_summaries |
| 3 | Channel Integration | `channels` | `registry`, `providers/whatsapp`, `providers/instagram`, `providers/messenger`, `providers/comments` | channel_integrations, channel_accounts, provider_message_map |
| 4 | Event & Delivery Backbone | `events` | `webhooks`, `outbox`, `dispatcher`, `idempotency`, `deadletter` | webhook_events, outbox_events, processed_events, delivery_attempts |
| 5 | AI Assistant | `ai` | `orchestration`, `context`, `tools`, `guardrails`, `providers`, `runs` | ai_agents, ai_runs, ai_tool_calls, ai_guardrail_events |
| 6 | Knowledge (RAG) | `knowledge` | `sources`, `documents`, `pipeline`, `embeddings`, `retrieval` | knowledge_sources, documents, document_versions, document_chunks, chunk_embeddings, processing_jobs |
| 7 | Commerce Catalog | `catalog` | `products`, `inventory`, `pricing`, `search` | products, product_variants, prices, inventory_items, categories, product_attributes, product_images |
| 8 | Commercial | `billing` | `plans`, `subscriptions`, `entitlements`, `usage`, `providers/paddle` | plans, features, plan_features, billing_customers, subscriptions, billing_events, invoices, usage_events, usage_aggregates, entitlement_overrides |
| 9 | Media | `media` | `storage`, `objects`, `signing` | media_objects |
| 10 | Web Ingestion | `crawler` | `validation`, `fetch`, `extract`, `jobs` | crawl_jobs, crawl_pages |

**Cross-cutting (not domains):**

| Package | Contents |
|---|---|
| `core` | settings, database session/engine, Celery app, logging, OpenTelemetry bootstrap, error handling, security primitives (hashing, tokens, signatures) |
| `platform` | shared kernel: `TenantContext`, `ActorContext`, pagination, ID generation, money types, audit log writer, rate limiter, clock |

**Deliberate consolidations (from the first draft):**

- `auth` + `users` + `tenancy` + `rbac` → **`identity`** — one lifecycle, one set of invariants; splitting them produced circular dependencies.
- `knowledge` + `rag` → **`knowledge`** — RAG is a capability of the knowledge domain, not a peer of it.
- `webhooks` + `events` + outbox + idempotency → **`events`** — one delivery backbone with one reliability model.
- `entitlements` + `usage` → sub-packages of **`billing`** — entitlements are meaningless without plans and usage.
- `handoff` → sub-package of **`messaging`** — promote to a module if routing/SLA/skills logic grows.
- `audit`, `notifications`, `observability` → `platform` / `core` — cross-cutting utilities, not domains.

**Why `crawler` is separate from `knowledge`:** conceptually it feeds Knowledge, but it is an untrusted, network-restricted workload with its own deployment and security boundary (ADR-0012). This is the clearest example of conceptual and physical boundaries deliberately diverging.

---

## 6. Module dependency rules

```
HTTP / task entrypoint
   ↓
application service   (orchestration, transactions, authorisation)
   ↓
domain               (entities, invariants, policies — no I/O)
   ↓
repository           (tenant-scoped data access)
   ↓
PostgreSQL
```

1. A module exposes a **public service interface**; everything else is internal.
2. No module imports another module's repositories, ORM models, or private services.
3. Each table has exactly one owning module. Cross-module reads go through the owner's interface.
4. Provider adapters depend on internal interfaces; the domain never imports a vendor SDK.
5. `core` and `platform` may be imported by anyone; they import no domain module.
6. AI tools call application services — never repositories.
7. Asynchronous cross-module communication uses `events` (outbox), not direct task imports.

**Allowed dependency graph (→ = may call):**

```
identity   ← (everyone, for tenant/actor/permission checks)
platform, core ← (everyone)

channels   → identity, events, media
events     → identity
messaging  → identity, channels, media, events
ai         → identity, messaging, knowledge, catalog, billing(entitlements+usage), events
knowledge  → identity, media, events
catalog    → identity, media
billing    → identity, events
media      → identity
crawler    → knowledge (narrow ingestion interface only)
```

Cycles are prohibited. If module A needs to react to something in module B, B emits an outbox event and A consumes it.

---

## 7. Repository structure (target)

```text
omnichannel/
├─ PROJECT_CONTEXT.md  CURRENT_STATE.md  ENGINEERING.md  DECISIONS.md  TODO.md
├─ docs/
│   └─ architecture.md database.md security.md ai.md messaging.md
│      billing.md deployment.md observability.md integrations.md operations.md
├─ backend/
│  ├─ pyproject.toml
│  ├─ alembic.ini
│  ├─ alembic/versions/
│  ├─ app/
│  │  ├─ main.py                 # API entrypoint
│  │  ├─ worker.py               # Celery entrypoint
│  │  ├─ core/                   # settings, db, celery, logging, otel, errors, crypto
│  │  ├─ platform/               # tenant/actor context, pagination, ids, audit, ratelimit
│  │  └─ modules/
│  │     ├─ identity/  messaging/  channels/  events/  ai/
│  │     └─ knowledge/ catalog/   billing/   media/    crawler/
│  └─ tests/ {unit,integration,security,contract,fixtures}
├─ infra/ {docker,nginx,prometheus,grafana,scripts}
└─ .github/workflows/
```

Each module follows the same internal shape (only the parts it needs):

```text
modules/<module>/
├─ api/            # routers + request/response schemas
├─ services/       # application services (public interface)
├─ domain/         # entities, value objects, policies
├─ models/         # SQLAlchemy models (private to the module)
├─ repositories/   # tenant-scoped data access (private)
├─ tasks/          # Celery tasks (thin wrappers over services)
├─ events/         # published/consumed event contracts
├─ providers/      # external adapters, if any
└─ tests/
```

---

## 8. Current → Future → Trigger

| Component | CURRENT | FUTURE | TRIGGER |
|---|---|---|---|
| Deployment | Single server, Docker Compose | Split API / worker / crawler hosts | CPU sustained > 70 %, or zero-downtime deploys required |
| API | 1 container, N Uvicorn workers | Replicas behind NGINX | p95 latency SLO breach or CPU saturation |
| Workers | Shared worker, queue routing | Dedicated AI / crawler / events workers | Queue backlog > 5 min or one queue starving another |
| PostgreSQL | Self-hosted container | Managed, then replicas, then partitioning | Ops burden, PITR needs, I/O or connection pressure |
| Redis | Self-hosted container | Managed / HA | Multi-host deployment or reliability requirement |
| Vectors | pgvector on primary | Replica → separate PG → dedicated vector DB | Retrieval p95 SLO breach or OLTP degradation |
| Search | SQL filters + full-text | Hybrid (BM25 + vector) rerank | Measured retrieval quality gap |
| Tenant isolation | Application-scoped repositories | + PostgreSQL RLS | Schema stable, or compliance requirement |
| Outbox dispatch | Poller with `SKIP LOCKED` | + `LISTEN/NOTIFY` hint | Dispatch latency budget < 1 s |
| Tracing | OTel → collector → Sentry | Tempo/Jaeger or managed APM | Trace volume/retention needs |
| Crawler | Designed only | Isolated egress-restricted workers | Tenants require website ingestion |
| Visual search | Schema headroom only | Image embeddings + similarity | Proven customer demand |

---

## 9. Data flows (index)

| Flow | Document |
|---|---|
| Incoming WhatsApp message | `messaging.md` §6 |
| Instagram / Facebook comment | `messaging.md` §7 |
| Outbound delivery + retries | `messaging.md` §8 |
| Human handoff | `messaging.md` §9 |
| AI response generation | `ai.md` §4 |
| Document ingestion → RAG | `ai.md` §6 |
| Website crawling | `ai.md` §8 |
| Billing webhook | `billing.md` §5 |

---

## 10. Scalability strategy

Ordered steps, each gated by a measurable trigger. Do not pre-build them.

| Step | Action | Trigger |
|---|---|---|
| 1 | Vertical scale the single server | Any sustained resource pressure |
| 2 | API replicas behind NGINX | API CPU > 70 % sustained, or p95 SLO breach |
| 3 | Worker replicas + queue separation | Backlog age > 5 min on any queue |
| 4 | Dedicated AI workers | AI tasks occupy > 60 % of worker time |
| 5 | Dedicated crawler hosts | Crawl volume or stricter isolation needs |
| 6 | Managed PostgreSQL | Backup/PITR/HA operational burden |
| 7 | PgBouncer | Connection count approaches `max_connections` |
| 8 | Read replicas | Read-heavy analytics/reporting pressure |
| 9 | Partitioning (messages, events, usage) | Table > ~100 M rows or slow retention deletes |
| 10 | Extract a service | Independent scaling/reliability/ownership need |

---

## 11. Performance considerations

- Composite indexes lead with `tenant_id`; keyset pagination everywhere unbounded.
- Webhook endpoint budget: p95 < 150 ms (verify + validate + one transaction + return).
- Outbox dispatch budget: p95 < 1 s from commit to task publication.
- AI context is bounded: recent turns + a rolling summary + top-K chunks — never the full history.
- Batch embedding generation; cache embeddings by content hash.
- Cache only non-authoritative, tenant-scoped data with short TTLs and explicit invalidation.
- Never call an external provider inside a database transaction.
- Bound every external call with a timeout, retry policy and circuit-breaking behaviour.
- Track slow queries (`pg_stat_statements`) from the first production day.

---

## 12. Open questions

1. Which channel launches first?
2. Do AI replies auto-send at launch, or require agent approval (per channel)?
3. First LLM/embedding provider and model?
4. Initial plan/price/limit matrix?
5. Production host and object storage provider?
6. Frontend framework and hosting (affects CORS/cookie setup)?
7. Catalog: native-first, imported from a commerce platform, or both?
8. Which commerce/order system must integrate first, if any?
9. Data residency, retention and compliance obligations?
10. Launch languages for AI responses?
11. Multiple brands/pages per tenant at launch?
12. Multiple AI agents/personas per tenant at launch?
13. Expected message volume per tenant (capacity model input)?
14. Are public comment replies held for approval by default?
15. What must be visible in the audit log for launch?

---

## 13. Risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| 1 | Cross-tenant data leak | Critical | Scoped repositories, mandatory isolation tests, audit logs, RLS later |
| 2 | Prompt injection via documents/pages/messages | High | Untrusted-content framing, tool authorisation, output validation, handoff |
| 3 | Duplicate/out-of-order provider events | High | Dedup keys, outbox, `processed_events`, per-conversation ordering |
| 4 | AI fabricates price/stock/policy | High | Tools + retrieval are the only authoritative sources; refuse-and-escalate |
| 5 | Crawler SSRF | Critical | IP/DNS/redirect validation, IP pinning, egress restriction, isolated workers |
| 6 | Billing state drift | High | Webhooks authoritative + idempotent + reconciliation sweep |
| 7 | Unbounded AI cost | High | Per-tenant usage metering, entitlement caps, cost alerts |
| 8 | Meta API change | Medium | Adapters + contract tests on recorded payloads |
| 9 | Outbox backlog unnoticed | High | Pending-depth and oldest-age alerts, dead-letter alerting |
| 10 | Single-server failure | High | Tested restore, documented rebuild runbook, RTO < 4 h |
| 11 | Module boundary erosion | Medium | Import rules in CI, review discipline, ADRs |
| 12 | Malicious upload | Medium | Type/size validation, isolated storage, scanning, no execution path |

---

## 14. Postponed decisions

Kubernetes · Kafka/event streaming · Elasticsearch/OpenSearch · dedicated vector database · microservice extraction · database/schema per tenant · RLS timing · managed PostgreSQL/Redis vendor · external IdP · MFA mechanics · enterprise SSO/SCIM · visual-search model · data warehouse/BI · workflow engine (Temporal) · multi-region · data residency · ABAC/custom roles · CDN strategy.

---

## 15. Implementation roadmap

See `CURRENT_STATE.md` for live status. Reordering rationale is recorded below the table.

| Phase | Name | Key outcome |
|---|---|---|
| 0 ✅ | Architecture & documentation | This document set; ADR-0001–0012 |
| 1 | Repository & application foundation | FastAPI shell, config, logging, OTel bootstrap, lint/type/test gates, health endpoints |
| 2 | Config, Docker, PostgreSQL, Alembic, Redis, Celery | Local environment, migration workflow, worker + beat skeletons |
| 3 | Identity | Users, auth sessions, tenants, memberships, RBAC, audit log |
| 4 | **Event backbone** | `webhook_events`, `outbox_events`, dispatcher, `processed_events`, dead-letter, replay tooling |
| 5 | Conversations | Contacts, conversations, messages, attachments, normalized model |
| 6 | Channels & webhook ingestion | Provider abstraction, Meta adapters, signature verification, ingestion on the outbox |
| 7 | Outbound delivery | Delivery attempts, idempotency keys, retries, rate-limit handling |
| 8 | AI foundation | Provider abstraction, orchestrator, context builder, tool registry, guardrails, AI runs, usage/cost |
| 9 | Knowledge & RAG | Uploads, parsing, chunking, embeddings, pgvector retrieval, isolation tests |
| 10 | Catalog | Products, variants, prices, inventory, images, product tools |
| 11 | Human handoff | Handoff requests, assignment, agent workspace, AI summary, hand-back |
| 12 | Billing, entitlements, usage | Plans, Paddle adapter, webhooks, entitlement checks, metering, reconciliation |
| 13 | Observability hardening | Dashboards, alert rules, trace export, runbooks |
| 14 | CI/CD & production deployment | Pipelines, images, NGINX/TLS, migrations, backups, rollback |
| 15 | Website crawler | ADR-0012 controls, isolated workers, crawl → knowledge pipeline |
| 16 | Visual product search | Image embeddings, tenant-scoped similarity |
| 17 | Load & security testing, hardening | Load tests, isolation/pen tests, restore rehearsal, readiness checklist |

**Why this differs from the original ordering**

1. **The event backbone moved before channels (new Phase 4).** The outbox is now foundational (ADR-0003); channels, billing, AI and knowledge all publish through it. Building it after webhook ingestion would mean retrofitting reliability into live code paths.
2. **Outbound delivery is its own phase (7).** Sending to a provider is the main non-idempotent external side effect and deserves explicit attempt records and retry semantics rather than being buried inside channels.
3. **Observability is split.** A baseline (structured logs, OTel bootstrap, health endpoints) lands in Phase 1 because retrofitting context propagation is expensive; dashboards and alerts harden in Phase 13.
4. **Usage metering folded into Phase 12** rather than standing alone — it shares tables and lifecycle with billing and entitlements.
5. **Crawler and visual search moved after launch readiness** — neither is launch-critical, and the crawler carries the highest security cost in the system.
