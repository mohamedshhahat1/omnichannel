# DECISIONS — Architecture Decision Records

Format: ID · Title · Status · Context · Decision · Alternatives · Rejection rationale · Consequences · Future migration path.

Statuses: `Proposed` · `Accepted` · `Superseded` · `Deprecated`.

| ID | Title | Status |
|---|---|---|
| [ADR-0001](#adr-0001--modular-monolith-first) | Modular monolith first | Accepted |
| [ADR-0002](#adr-0002--postgresql-shared-schema-multi-tenancy) | PostgreSQL shared-schema multi-tenancy | Accepted |
| [ADR-0003](#adr-0003--transactional-outbox-for-all-durable-event-driven-work) | Transactional outbox for all durable event-driven work | Accepted |
| [ADR-0004](#adr-0004--redis--celery-for-background-processing) | Redis + Celery for background processing | Accepted |
| [ADR-0005](#adr-0005--pgvector-for-retrieval) | pgvector for retrieval | Accepted |
| [ADR-0006](#adr-0006--channel-and-provider-abstractions) | Channel and provider abstractions | Accepted |
| [ADR-0007](#adr-0007--paddle-behind-a-billing-abstraction) | Paddle behind a billing abstraction | Accepted |
| [ADR-0008](#adr-0008--s3-compatible-object-storage-for-binaries) | S3-compatible object storage for binaries | Accepted |
| [ADR-0009](#adr-0009--authentication-opaque-server-side-sessions-in-secure-cookies) | Authentication: opaque server-side sessions in secure cookies | Accepted |
| [ADR-0010](#adr-0010--opentelemetry-as-the-tracing-and-correlation-standard) | OpenTelemetry as the tracing and correlation standard | Accepted |
| [ADR-0011](#adr-0011--consolidated-module-boundaries-conceptual-vs-physical) | Consolidated module boundaries (conceptual vs physical) | Accepted |
| [ADR-0012](#adr-0012--website-crawler-as-an-isolated-untrusted-workload) | Website crawler as an isolated untrusted workload | Accepted |

---

## ADR-0001 — Modular monolith first

**Status:** Accepted · 2026-08-11

### Context
A small team must ship a secure, multi-tenant SaaS with messaging, AI, RAG, catalog and billing. Microservices would multiply deployment units, network failure modes, distributed-transaction problems and observability cost before we have any of the scale or team-structure pressure that justifies them.

### Decision
Build one deployable FastAPI backend with strict internal module boundaries, service-interface communication, one owning module per table, and an inward dependency direction. Workers run the same codebase with different entrypoints and queues.

### Alternatives
1. Microservices from day one.
2. Serverless-first (functions per workflow).
3. Unstructured monolith (no enforced boundaries).

### Rejection rationale
1. Microservices: no scaling, reliability or ownership trigger exists; they would add network partitions, distributed transactions, versioned contracts and multi-service debugging to a team that cannot absorb it.
2. Serverless: poor fit for long-running AI/crawl/embedding jobs, harder local development, unpredictable cold-start latency on webhook paths, and awkward connection pooling against PostgreSQL.
3. Unstructured monolith: cheap now, but makes future extraction impossible and lets tenant-isolation bugs spread.

### Consequences
- One CI pipeline, one artifact, one deployment, simple local development.
- Cross-module transactions are available and cheap — a genuine advantage we intentionally exploit (see ADR-0003).
- Boundary discipline must be enforced by review, import rules and tests, because the compiler will not enforce it.
- A noisy module can affect the whole process; queue separation and resource limits mitigate this.

### Future migration path
Extract a module only against a measurable trigger (sustained CPU saturation attributable to one module, queue backlog that cannot be solved by worker replicas, independent reliability/deployment requirements, or team ownership split). Extraction order candidates: webhook ingestion → crawler → AI processing. Because modules already communicate through service interfaces and events, extraction means replacing an in-process call with an HTTP call plus its own outbox — not a rewrite.

---

## ADR-0002 — PostgreSQL shared-schema multi-tenancy

**Status:** Accepted · 2026-08-11

### Context
We need strong tenant isolation with a small ops team and unknown per-tenant load. Isolation must hold not only in the database but also in Redis, object storage, vector search, background jobs and AI tools.

### Decision
One PostgreSQL database, one shared schema, `tenant_id` on every tenant-owned row. Isolation is enforced at the application/domain/query boundary through tenant-scoped repositories fed by a trusted server-derived `TenantContext`. PostgreSQL RLS is planned as an additional defence-in-depth layer, not as the primary mechanism.

### Alternatives
1. Database per tenant.
2. Schema per tenant.
3. RLS as the only isolation mechanism.

### Rejection rationale
1. Database per tenant: migrations, connection pooling, backups, monitoring and support all multiply by tenant count; onboarding becomes a provisioning problem.
2. Schema per tenant: same migration fan-out with additional tooling friction, and cross-tenant platform queries become painful.
3. RLS-only: RLS protects exactly one surface. It cannot protect Redis keys, object storage prefixes, vector filters, Celery payloads or AI tool arguments — all of which the application must scope anyway.

### Consequences
- Simple operations, one backup, one migration path, low cost.
- A missing `tenant_id` filter is a data-leak class of bug; scoped repositories and mandatory cross-tenant tests are the controls.
- Large tenants share resources; per-tenant rate limits and quotas are required.

### Future migration path
Add RLS policies with a per-transaction `app.tenant_id` setting once the schema stabilises. Then, in order of need: partition high-volume tables (messages, events, usage) by time; add read replicas; move the largest tenants to dedicated databases behind the same repository interface.

---

## ADR-0003 — Transactional outbox for all durable event-driven work

**Status:** Accepted · 2026-08-11 · *(supersedes the earlier "outbox later, if justified" position)*

### Context
The naive pattern `INSERT event → COMMIT → celery.delay()` is not atomic. A crash, a Redis outage, or a network failure between commit and enqueue silently drops work: the database says the event was received, but nothing ever processes it. For webhook-driven revenue and customer conversations, silent loss is unacceptable. Reversing the order (enqueue first) is worse — it produces tasks referencing rows that were never committed.

### Decision
Every state change that must produce asynchronous work writes its business row **and** an `outbox_events` row in the **same PostgreSQL transaction**. A separate outbox dispatcher (a Celery beat task using `FOR UPDATE SKIP LOCKED`) claims pending rows, publishes them to Celery, and marks them dispatched. Consumers are idempotent and record processing in a `processed_events` table inside their own business transaction.

```
webhook → verify signature → validate → resolve integration/tenant
  → BEGIN
       INSERT webhook_events   (dedup on provider + provider_event_id)
       INSERT outbox_events    (same transaction)
     COMMIT
  → 202/200 to provider
  → outbox dispatcher claims → Celery → consumer (idempotent) → mark dispatched
```

### Alternatives
1. `commit → enqueue` with best-effort retry.
2. Celery as the durable store (Redis persistence / result backend).
3. Kafka or another log-based broker with exactly-once semantics.
4. Debezium/CDC-based outbox publishing.

### Rejection rationale
1. Not atomic; loses work exactly when the system is already degraded. Reconciliation sweeps would be needed anyway — which is a worse outbox.
2. Redis is not a durable business store; AOF/RDB loss, evictions and failovers can drop queued work, and we explicitly forbid Redis as a source of truth.
3. Kafka is explicitly out of scope: heavy to operate, adds partitions/consumer-group/retention semantics, and solves a throughput problem we do not have. "Exactly once" still requires idempotent consumers, so it buys little here.
4. CDC adds a replication-slot dependency, extra infrastructure, and operational failure modes (slot lag filling the disk) for latency we do not need.

### Consequences
- **Atomicity:** if the transaction commits, the work is guaranteed to be published eventually; if it rolls back, neither the event nor the job exists.
- **At-least-once delivery:** a crash between publish and `mark dispatched` re-publishes after the lease expires. Every consumer must therefore be idempotent — this is a mandatory rule, not a suggestion.
- **Redis becomes disposable:** losing Redis loses in-flight tasks, not work. Un-acknowledged outbox rows are re-dispatched automatically.
- Cost: one extra table write per event, a dispatcher process, poll-interval latency (target < 1 s), and monitoring of pending depth/age.
- Ordering is per-ordering-key (for example, per conversation), not global; consumers serialise on that key.

### Failure recovery
| Failure | Recovery |
|---|---|
| Crash before commit | Nothing persisted; provider retries the webhook; dedup key prevents duplicates |
| Crash after commit, before dispatch | Row stays `pending`; dispatcher picks it up on the next poll |
| Crash after publish, before `dispatched` | Lease expires; row is re-published; consumer's `processed_events` insert conflicts and the duplicate is skipped |
| Redis down | Dispatch fails, rows stay `pending`, backlog alert fires; work resumes automatically when Redis returns |
| Poison event | Retries with backoff until `max_attempts`, then `dead_letter` with the last error, alert, and a documented replay runbook |
| Duplicate provider delivery | Unique `(provider, provider_event_id)` on `webhook_events` makes the second insert a no-op |

### Future migration path
If dispatch latency ever needs to be sub-100 ms, add `LISTEN/NOTIFY` as a *hint* while keeping the poller as the correctness mechanism. If a genuine cross-service event bus becomes necessary after module extraction, the outbox already contains the canonical event stream and can feed an HTTP relay or a broker without changing producers.

---

## ADR-0004 — Redis + Celery for background processing

**Status:** Accepted · 2026-08-11

### Context
Webhook processing, AI generation, embedding, outbound delivery, crawling, usage aggregation and scheduled maintenance must run outside the request path. The stack is Python; the team is small.

### Decision
Celery with Redis as broker (and, where useful, result backend), organised into dedicated queues: `default`, `events`, `ai`, `knowledge`, `outbound`, `billing`, `usage`, `crawler`, `maintenance`. Celery beat runs the outbox dispatcher, reconciliation sweeps and aggregation. Redis is additionally used for caching, rate limiting and short-lived locks — never as a source of truth.

### Alternatives
1. RabbitMQ as broker.
2. Dramatiq / RQ / arq.
3. Temporal or another workflow engine.
4. Kafka consumers.

### Rejection rationale
1. RabbitMQ is a better broker in isolation but adds a second stateful system when we already need Redis for caching and rate limiting; durability is provided by the outbox regardless of broker.
2. Dramatiq/RQ/arq are fine but offer no decisive advantage over Celery's maturity, routing, beat scheduling and ecosystem.
3. Temporal solves long-running stateful orchestration we do not yet have, at a significant operational cost.
4. Kafka: out of scope by principle; wrong tool for task execution.

### Consequences
- Simple, well-documented, one extra container in local development.
- At-least-once execution; idempotency is mandatory.
- Queue separation prevents slow AI/crawler work from starving webhook processing.
- Long tasks need explicit soft/hard time limits and prefetch tuning.

### Future migration path
Split workers by queue onto separate hosts; move to managed Redis; swap the broker to RabbitMQ behind Celery's configuration if broker semantics become the bottleneck; adopt a workflow engine only if multi-day, multi-step human-in-the-loop orchestration appears.

---

## ADR-0005 — pgvector for retrieval

**Status:** Accepted · 2026-08-11

### Context
Tenant knowledge (documents, FAQs, policies, product text, crawled pages) must be chunked, embedded and retrieved with strict tenant scoping. Volumes at launch are modest and unknown.

### Decision
Store chunks and embeddings in PostgreSQL using `pgvector`, with `tenant_id` filtering in every query and HNSW indexing. Retrieval sits behind a `RetrievalService` interface so the storage engine can change without touching the AI layer.

### Alternatives
1. Dedicated vector database (Pinecone, Qdrant, Weaviate, Milvus).
2. Elasticsearch/OpenSearch for hybrid search.
3. In-process FAISS index.

### Rejection rationale
1. Adds a second datastore to secure, back up, monitor and keep consistent with PostgreSQL, plus per-tenant namespace management and dual-write consistency problems — for a workload that currently fits comfortably in PostgreSQL.
2. Elasticsearch is explicitly out of scope; PostgreSQL full-text plus pgvector covers hybrid search adequately at this scale.
3. FAISS in-process breaks with multiple workers, needs its own persistence, and has no tenant-aware access control.

### Consequences
- One datastore: transactional consistency between documents, chunks and embeddings; one backup; one security model.
- Tenant filtering is a plain SQL predicate — the same isolation mechanism as the rest of the system.
- Embedding dimensionality changes require a re-embed migration; embeddings are versioned by model.
- Vector queries compete with OLTP traffic on the same instance.

### Future migration path
Triggers: retrieval p95 above the SLO, index size/rebuild time becoming operationally painful, or vector load measurably degrading OLTP. Then: move vectors to a read replica or a dedicated PostgreSQL instance first; only after that consider a dedicated vector store behind the existing `RetrievalService` interface, with a documented re-index/backfill plan.

---

## ADR-0006 — Channel and provider abstractions

**Status:** Accepted · 2026-08-11

### Context
WhatsApp, Instagram DM, Messenger, and Instagram/Facebook comments are all Meta products with different payloads, reply rules, media handling, and messaging windows. Meta changes these APIs frequently. Business logic must not be rewritten each time.

### Decision
A `ChannelProvider` interface (verify webhook, normalise event, send message, reply to comment, fetch profile, capabilities) with one adapter per channel. Everything downstream operates on the internal normalised `Conversation`/`Message` model plus a declarative capability descriptor (supports public reply, private reply, media types, messaging window, template requirements).

### Alternatives
1. Call Meta APIs directly from services.
2. One generic Meta adapter with conditionals.
3. A separate domain model per channel.

### Rejection rationale
1. Couples every feature to a vendor API and makes testing require mocking HTTP everywhere.
2. Conditional sprawl becomes unmaintainable once five channels with different rules exist.
3. Duplicates conversation, assignment, handoff and AI logic per channel.

### Consequences
- Adding a channel is an adapter plus a capability descriptor, not a domain change.
- Channel-specific semantics must be expressed explicitly in capabilities, or they will be lost in normalisation.
- Raw provider payloads are retained on the webhook event for debugging and replay.

### Future migration path
The same interface accepts non-Meta channels (Telegram, web chat, email, SMS). If Meta ships a breaking API version, only adapters change, behind contract tests using recorded payloads.

---

## ADR-0007 — Paddle behind a billing abstraction

**Status:** Accepted · 2026-08-11

### Context
We need subscriptions, plans, entitlements and usage limits. Paddle is the chosen initial provider (merchant of record, handles tax). Provider choice may change; billing correctness may not.

### Decision
A provider-agnostic billing module owns plans, subscriptions, entitlements, usage and invoices in our own database. A `BillingProvider` interface (create checkout, fetch subscription, cancel, verify webhook, parse event) has a Paddle adapter. **Paddle webhooks are the source of truth for subscription state; frontend/checkout state never is.** Billing events are stored idempotently and are auditable.

### Alternatives
1. Paddle SDK calls scattered through the application.
2. Trust the frontend checkout success callback.
3. Query Paddle at runtime for entitlement decisions.
4. Build billing in-house.

### Rejection rationale
1. Vendor lock-in and untestable business logic.
2. Trivially spoofable and frequently wrong (async payment methods, retries, chargebacks).
3. Puts a third-party API on the hot path of every request; fails when Paddle is slow or down.
4. Tax, invoicing and compliance are not our business.

### Consequences
- Entitlement checks are local, fast and available during a provider outage.
- We must handle duplicate, delayed and out-of-order webhooks, plus a periodic reconciliation sweep against the provider.
- A local subscription state machine must be explicitly defined, including grace and dunning behaviour.

### Future migration path
Add a Stripe (or other) adapter behind the same interface; run both in parallel during migration keyed by `billing_customers.provider`. Enterprise/manual invoicing becomes another adapter.

---

## ADR-0008 — S3-compatible object storage for binaries

**Status:** Accepted · 2026-08-11

### Context
The platform handles uploaded documents, product images, inbound customer media and crawler artifacts. These are large, numerous, and mostly write-once/read-many.

### Decision
Store binaries in S3-compatible object storage behind an internal `ObjectStorage` interface. PostgreSQL stores only metadata (`media_objects`). Keys are tenant-scoped: `tenants/{tenant_id}/{category}/{object_id}/{filename}`. Buckets are private; access is granted through short-lived signed URLs issued only after an authorisation check.

### Alternatives
1. Store binaries in PostgreSQL (`bytea`/large objects).
2. Local filesystem on the application server.
3. A CDN-only solution with public URLs.

### Rejection rationale
1. Bloats the database, slows backups and restores, wastes buffer cache, and makes PITR far more expensive — directly conflicting with the RPO/RTO targets.
2. Local disk breaks as soon as there is more than one server, complicates backups, and risks disk exhaustion from the crawler.
3. Public URLs violate tenant isolation and leak customer media.

### Consequences
- Metadata and object lifecycles can diverge; an orphan-reaper job and checksum verification are required.
- Signed URLs must be short-lived and never logged.
- Storage usage becomes a metered, billable dimension.

### Future migration path
Swapping providers (MinIO ↔ S3 ↔ R2 ↔ Spaces) is an adapter change plus a copy job. Add a CDN in front for product images; add server-side encryption with customer-managed keys if compliance requires it.

---

## ADR-0009 — Authentication: opaque server-side sessions in secure cookies

**Status:** Accepted · 2026-08-11 · *(replaces the earlier undecided "sessions or JWT" position)*

### Context
The first client is a first-party web dashboard used by tenant admins and human agents who handle customer conversations. Immediate revocation matters (staff offboarding, compromised device, password reset). Machine clients and provider webhooks need a different mechanism.

### Decision
**Dashboard:** opaque, high-entropy (256-bit) session tokens stored **hashed** (SHA-256) in PostgreSQL, delivered in a `__Host-oc_session` cookie — `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, no `Domain`. Sliding idle expiry of 7 days, absolute expiry of 30 days, token rotation on login, on password change and on privilege change. Redis caches session lookups with a short TTL and is invalidated explicitly on revocation; PostgreSQL remains authoritative. No separate refresh token: renewal is a rotation of the same opaque session.

**CSRF:** `SameSite=Lax` plus a double-submit token (`__Host-oc_csrf`, readable by JS) required in the `X-CSRF-Token` header for all unsafe methods, plus `Origin`/`Referer` validation. Cookie authentication is rejected on webhook and API-key routes.

**Passwords:** Argon2id (m ≈ 64 MiB, t = 3, p = 4, tuned to ~250 ms), per-hash salt, transparent rehash on parameter change, breached-password rejection, generic error responses, per-account and per-IP rate limiting with backoff.

**Email verification / password reset:** single-use, hashed, expiring tokens (verification 24 h, reset 30 min); reset revokes all sessions and notifies the user; responses are constant-time and non-enumerating.

**Tenant selection:** the session identifies the *user*. The active tenant is requested per call and authorised against `memberships` server-side; the trusted `TenantContext` is derived from that check, never from the raw client value.

**Service clients / integrations:** tenant-scoped API keys `oc_{env}_{key_id}_{secret}` with only the secret's hash stored, explicit scopes, per-key rate limits, `last_used_at`, rotation and instant revocation, sent as `Authorization: Bearer`. Inbound provider webhooks authenticate by HMAC signature, not by session or API key. Future outbound webhooks to tenant systems are HMAC-signed with a timestamp and replay window.

### Alternatives
1. Stateless JWT access tokens plus refresh tokens.
2. `localStorage`-held bearer tokens for the dashboard.
3. A third-party identity provider (Auth0/Clerk/Cognito) from day one.

### Rejection rationale
1. Stateless JWTs cannot be revoked without a server-side denylist — which reintroduces the very state they were meant to avoid, while adding key rotation, clock skew and algorithm-confusion risks. Short expiry plus refresh tokens increases complexity without improving our actual threat posture.
2. `localStorage` tokens are readable by any XSS payload; `HttpOnly` cookies are strictly safer for a first-party dashboard.
3. A third-party IdP is a reasonable future option but adds cost, an external availability dependency on the login path, and vendor-specific user-lifecycle semantics before we know our enterprise requirements.

### Consequences
- Every authenticated request performs a session lookup (Redis-cached, PostgreSQL-authoritative) — a small, predictable cost.
- Revocation, device listing and "sign out everywhere" are trivial.
- Cookie auth requires disciplined CSRF handling and a strict CORS policy.
- A non-browser mobile/native client would use the API-key/token path or need a bearer-session variant.

### Future migration path
Add TOTP MFA and step-up authentication for sensitive actions; add OIDC/SAML SSO and SCIM for enterprise tenants behind the same session issuance point; add passkeys/WebAuthn. Because sessions are opaque and server-side, adding an external IdP changes only how a session is *established*, not how it is *validated*.

---

## ADR-0010 — OpenTelemetry as the tracing and correlation standard

**Status:** Accepted · 2026-08-11

### Context
A single customer message crosses an HTTP webhook, a database transaction, an outbox row, a Celery task, retrieval, an LLM call, tool calls and an outbound provider request — across processes and across time. Debugging "why did this customer get that answer, and why was it slow?" is impossible without correlation. Metrics alone cannot answer it.

### Decision
Adopt OpenTelemetry as the tracing and context-propagation standard for API and workers, with auto-instrumentation for FastAPI, SQLAlchemy, Redis, Celery and the HTTP client, plus manual spans for AI runs, retrieval, tool calls and provider requests. W3C `traceparent` is persisted on webhook and outbox rows and carried in Celery headers; asynchronous continuations use span links. A standard correlation set (`trace_id`, `request_id`, `correlation_id`, `tenant_id`, `webhook_event_id`, `outbox_event_id`, `conversation_id`, `message_id`, `ai_run_id`, `provider_request_id`) appears on spans and in every structured log line. Prometheus/Grafana remain for metrics; Sentry remains for error tracking, tagged with `trace_id`.

### Alternatives
1. Logs and metrics only, with a home-grown request-ID header.
2. A vendor-proprietary APM agent (Datadog, New Relic).
3. Defer tracing until after launch.

### Rejection rationale
1. A bare request ID does not survive the outbox/Celery boundary in a standard way, gives no span timing, and would be reinvented worse than the existing W3C standard.
2. Proprietary agents lock instrumentation to a vendor and cost more; OTel can export to them later anyway.
3. Retrofitting context propagation after the async pipeline exists is far more expensive than doing it during Phase 1, and the outbox/Celery design depends on it.

### Consequences
- Trace context must be persisted in the database (outbox rows) — a deliberate, documented column.
- Sampling policy is required: 100 % of errors and AI runs, head-based sampling for routine traffic, to control cost.
- Metric cardinality rules must be enforced: `tenant_id` and other unbounded values go on spans/logs, never on Prometheus labels.
- Instrumentation is a standing definition-of-done item for new workflows.

### Future migration path
Start by exporting OTLP to a collector alongside Sentry. Add a trace backend (Tempo/Jaeger, or a managed vendor) when trace volume justifies it; add OTel logs and metrics pipelines later. Because instrumentation is vendor-neutral, changing backend is a collector configuration change.

---

## ADR-0011 — Consolidated module boundaries (conceptual vs physical)

**Status:** Accepted · 2026-08-11 · *(revises the initial, more fragmented module list)*

### Context
The first architecture draft listed roughly twenty modules (`auth`, `users`, `tenancy`, `rbac`, `events`, `webhooks`, `rag`, `knowledge`, `billing`, `entitlements`, `usage`, `handoff`, ...). That level of fragmentation creates circular dependencies, forces trivial cross-module calls for tightly coupled concepts, and imposes ceremony without protection.

### Decision
Separate **conceptual domains** (how we reason and assign ownership) from **physical Python packages** (what actually exists on disk). Ten physical modules, each with internal sub-packages:

- `identity` — users, credentials, sessions, API keys, tenants, memberships, roles, permissions
- `messaging` — contacts, conversations, messages, assignment, handoff
- `channels` — channel integrations and per-channel provider adapters
- `events` — webhook ingestion, webhook events, transactional outbox, dispatcher, idempotency, dead-letter
- `ai` — orchestration, context building, tools, guardrails, LLM provider abstraction, AI runs
- `knowledge` — knowledge sources, documents, chunking, embeddings, retrieval (RAG)
- `catalog` — products, variants, SKUs, prices, inventory, categories, product images
- `billing` — plans, subscriptions, provider adapters, entitlements, usage metering
- `media` — object storage abstraction, media metadata, signed URLs
- `crawler` — isolated website ingestion workload (conceptually part of Knowledge, physically separate — see ADR-0012)

Cross-cutting concerns live in `core` (framework wiring) and `platform` (shared kernel: tenant context, pagination, IDs, audit, rate limiting).

### Alternatives
1. Keep ~20 fine-grained modules.
2. Collapse to three or four coarse modules.
3. Package by technical layer (`models/`, `services/`, `routers/`).

### Rejection rationale
1. Boundary ceremony without boundary value; `auth` and `users` cannot be meaningfully separated, and `entitlements` cannot be reasoned about without plans and usage.
2. Too coarse to guide ownership or future extraction; "core" becomes a dumping ground.
3. Layer-first packaging spreads a single feature across the tree and makes extraction and ownership nearly impossible.

### Consequences
- Fewer, more meaningful interfaces; sub-packages keep internal structure clear.
- A conceptual→physical mapping table must be maintained in `docs/architecture.md`.
- `handoff` is a sub-package of `messaging` rather than a module; if agent routing, SLAs and skills-based assignment grow, it can be promoted.

### Future migration path
Sub-packages are the natural split points. If `billing.entitlements` or `messaging.handoff` grows its own lifecycle, promote it to a module first, and only then consider extracting it to a service (ADR-0001).

---

## ADR-0012 — Website crawler as an isolated untrusted workload

**Status:** Accepted · 2026-08-11 · *(design accepted now; implementation deferred to a later phase)*

### Context
Tenants will supply URLs so the platform can ingest their website content as knowledge. Any server-side fetcher driven by user-supplied URLs is an SSRF primitive and a path into the internal network and cloud metadata endpoints. Crawled HTML is also a prime prompt-injection vector, and crawling can exhaust CPU, memory and disk.

### Decision
The crawler is a separate physical module executed by dedicated, network-restricted workers. Controls: allowlist scheme/port; reject private, loopback, link-local, reserved, multicast and cloud-metadata address ranges; resolve DNS and validate **every** resolved IP; pin the validated IP for the connection to defeat DNS rebinding; re-validate every redirect hop; cap redirects, response size, content types, pages, depth, duration and concurrency; enforce container CPU/memory/disk limits; verify domain ownership before crawling; run without access to PostgreSQL/Redis/internal APIs/Docker socket where practical, writing results through a narrow, authenticated ingestion interface. Crawled content enters the knowledge pipeline flagged as untrusted, quarantined from instruction context.

### Alternatives
1. Crawl synchronously inside the API process.
2. Crawl from ordinary workers with normal network access.
3. Use a third-party managed crawling/extraction API.
4. Application-level URL validation only, with no network egress restrictions.

### Rejection rationale
1. Blocks request threads, offers no resource isolation, and puts an SSRF primitive inside the trusted API process.
2. Ordinary workers can reach the database, Redis and internal endpoints — exactly what an SSRF payload wants.
3. A managed service is a legitimate future option but adds cost and a data-sharing question; it does not remove the need for URL validation.
4. Application-level validation alone is bypassable through TOCTOU and rebinding races; defence in depth requires network-level egress control.

### Consequences
- Extra deployment complexity: a separate worker container, network policy and resource limits.
- Crawling is asynchronous with visible per-job status and quotas.
- Ingestion must tolerate partial results and per-page failures.
- Crawler limits become plan-level entitlements.

### Future migration path
Move crawler workers to a dedicated host or VPC with an explicit egress proxy allowlist; optionally delegate fetching to a managed extraction provider behind the same internal interface while keeping our own validation and quotas.
