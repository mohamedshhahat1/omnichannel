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
| [ADR-0013](#adr-0013--standard-api-error-envelope-and-error-taxonomy) | Standard API error envelope and error taxonomy | Accepted |
| [ADR-0014](#adr-0014--async-infrastructure-foundation) | Async infrastructure foundation | Accepted |
| [ADR-0015](#adr-0015--identity-tenancy-and-credential-handling) | Identity, tenancy and credential handling | Accepted |

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

**Status:** Accepted · 2026-08-11 · *(replaces the earlier undecided "sessions or JWT" position; implemented in Phase 3 — see ADR-0015)*

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
4. Application-level URL validation alone is bypassable through TOCTOU and rebinding races; defence in depth requires network-level egress control.

### Consequences
- Extra deployment complexity: a separate worker container, network policy and resource limits.
- Crawling is asynchronous with visible per-job status and quotas.
- Ingestion must tolerate partial results and per-page failures.
- Crawler limits become plan-level entitlements.

### Future migration path
Move crawler workers to a dedicated host or VPC with an explicit egress proxy allowlist; optionally delegate fetching to a managed extraction provider behind the same internal interface while keeping our own validation and quotas.

---

## ADR-0013 — Standard API error envelope and error taxonomy

**Status:** Accepted · 2026-08-11 · *(introduced during Phase 1 implementation)*

### Context
Phase 1 had to answer a question every later phase inherits: what does an error look like on the wire?

FastAPI's defaults are not adequate for this platform:

- `HTTPException` produces `{"detail": "..."}` with no machine-readable code, so clients must string-match human prose that we will later want to reword or translate.
- `RequestValidationError` produces a 422 body that **includes the offending input**. On a login route that reflects the submitted password back to the caller and into every proxy and CDN log along the way; on a webhook route it reflects provider payload fragments. This is a real disclosure path, not a theoretical one.
- Unhandled exceptions leak a stack trace when `debug` is enabled — and `debug` is exactly the setting most likely to be wrong in a hurry.
- None of the defaults carry a request identifier, so a customer saying "it failed at about 2pm" cannot be tied to a log line.

Deciding this once, before any endpoint exists, is far cheaper than harmonising ten modules' error shapes in Phase 12.

### Decision
Every non-success response — from a route, a dependency, an exception handler, or middleware — uses one envelope:

```json
{
  "error": {
    "code": "conflict",
    "message": "That channel is already connected.",
    "details": { "channel": "whatsapp" },
    "request_id": "...",
    "correlation_id": "..."
  }
}
```

- `code` is a stable, lowercase, machine-readable identifier and is part of the public API contract. Clients branch on it.
- `message` is human-readable and always safe to display.
- `details` is optional structured context and never contains sensitive data.
- `request_id` and `correlation_id` are read from the ambient correlation context **by the envelope builder itself**, so no call site can forget them.

A closed base taxonomy covers HTTP semantics: `bad_request` (400), `unauthorized` (401), `forbidden` (403), `not_found` (404), `conflict` (409), `payload_too_large` (413), `unprocessable_entity` (422), `rate_limited` (429), `internal_error` (500), `service_unavailable` (503).

Four supporting rules:

1. **`internal_message` is never serialised.** Every `AppError` may carry an operator-facing `internal_message` (`"no row for tenant_id=42 conversation_id=7"`). It is logged; it never reaches the client.
2. **Validation failures are summarised, not echoed.** Only `location`, `type` and `message` survive. `input` and `ctx` are dropped.
3. **5xx responses use a fixed generic sentence.** The exception type, message and traceback go to the log, tagged with the request id.
4. **Modules do not define new exception classes for HTTP purposes.** They raise a base error with a domain-specific `code` (`ConflictError(..., code="channel_already_connected")`). The taxonomy stays closed; vocabulary stays open.

### Alternatives
1. FastAPI/Starlette defaults, unchanged.
2. RFC 7807 `application/problem+json`.
3. Per-module error shapes, harmonised later.
4. Always return 200 with an error field in the body (GraphQL style).

### Rejection rationale
1. Defaults have no stable codes, no request id, and — decisively — echo submitted input on validation failure. Overriding them is the whole point.
2. RFC 7807 is a reasonable standard, but `type` as a dereferenceable URI implies documentation infrastructure we will not maintain, and `title`/`detail`/`instance` map awkwardly onto what clients actually need. The chosen envelope carries the same information in a shape that is easier to consume, and can be rendered as `problem+json` later without changing call sites.
3. Harmonising later never happens; by Phase 12 there would be ten shapes and a breaking change to fix them.
4. Returning 200 for failures breaks caches, proxies, retry logic, monitoring and every HTTP client's error handling. Status codes exist; use them.

### Consequences
- Clients write one error handler and branch on `code`.
- Support has a request id on every failure, present in both the response body and the `X-Request-ID` header, and on every log line for that request.
- `code` values become a public contract: renaming one is a breaking change and needs the same care as renaming a field.
- A small discipline cost — raise `AppError` subclasses, not `HTTPException` — enforced by review and by the integration tests that assert nothing sensitive appears in an error body.
- The envelope builder depends on the correlation context, so correlation middleware must remain outside the exception handlers. This is why middleware ordering is documented in `app/api/application.py`.

### Future migration path
If a partner integration ever requires RFC 7807, add a content-negotiated renderer at the single `_json_error` choke point; no call site changes. If per-field client-side validation messages are needed, extend `details.fields` — it is already a list of structured entries. Localisation would key off `code`, which is precisely why `code` and `message` are separate.

---

## ADR-0014 — Async infrastructure foundation

**Status:** Accepted · 2026-08-11 · *(introduced during Phase 2 implementation)*

### Context
Phase 2 had to add the infrastructure foundation without introducing business domains: PostgreSQL, SQLAlchemy 2.x async/asyncpg, Alembic, Redis, Celery, Docker/Compose, lifecycle wiring, and health checks. The stack had to remain honest about durability, preserve tenant isolation rules across non-SQL surfaces, and fit a small team's operational budget.

### Decision
1. **Database runtime:** use SQLAlchemy 2.x in async mode with `asyncpg`, one bounded engine per process, one async session factory per process, UTC/timeouts in server settings, and deterministic metadata naming conventions. Phase 2 adds no ORM models or domain tables.
2. **Migration runtime:** use async Alembic configured from validated settings. Application DML credentials and migration DDL credentials are separate. The initial migration enables only `pgcrypto`, `pg_stat_statements`, `pg_trgm`, and `vector`.
3. **Redis runtime:** use the async redis-py client and enforce tenant-safe key construction as `oc:{env}:t:{tenant_id}:{purpose}:...`; unsafe key segments are rejected.
4. **Task execution:** use Celery with Redis as broker, exactly three foundation queues (`critical`, `default`, `background`), and no default result backend because durable business state belongs in PostgreSQL, not Redis.
5. **Context propagation:** every task carries trusted `tenant_id`, `correlation_id`, and optional W3C `traceparent` in task headers. Context is bound with context variables for one invocation and reset reliably; no mutable global tenant state is allowed.
6. **Application lifecycle:** the FastAPI lifespan owns PostgreSQL and Redis resources and registers readiness checks there; `/health/live` remains dependency-free while `/health/ready` checks PostgreSQL and Redis only after startup and with bounded per-check timeouts.
7. **Local topology:** use a non-root multi-stage backend image and Docker Compose services for PostgreSQL, Redis, one-shot migrations, API, worker, and exactly one beat process. Dependency ordering uses health/completion conditions, never `service_started`.
8. **Testing posture:** ship actual pytest unit tests plus opt-in real PostgreSQL/Redis integration tests; SQLite is forbidden for Phase 2 infrastructure coverage.
9. **Dependency lock honesty:** if the authoring environment cannot resolve dependencies, commit only an explicitly temporary offline-authored direct pin artifact and document its limitation rather than fabricating hashes or transitive claims.

### Alternatives
1. Synchronous SQLAlchemy + psycopg.
2. SQLite-backed infrastructure tests.
3. Redis as a durable task/result store.
4. Global process-level tenant/task context.
5. A shared database role for runtime and migrations.
6. RabbitMQ, Temporal, Kubernetes, or microservices at Phase 2.

### Rejection rationale
1. The API and long-lived connections are async already; adding a synchronous ORM path complicates the stack and risks blocking.
2. SQLite cannot validate PostgreSQL extensions, async driver behavior, transaction semantics, or deployment-shape readiness checks.
3. ADR-0003 makes PostgreSQL the durability boundary; using Redis for durable workflow state would weaken that guarantee.
4. Global mutable task context risks cross-tenant leaks across concurrent work.
5. A shared role violates least privilege and makes destructive mistakes easier.
6. None of those technologies solve a present problem that justifies their operational cost.

### Consequences
- The repository now has a production-shaped local infrastructure baseline with clear extension points but still no domain data model.
- Readiness reflects actual dependencies without compromising liveness semantics.
- Redis remains disposable; Celery tasks must still be idempotent.
- The committed `requirements.lock` cannot be presented as a validated fully resolved production lock until regenerated in a networked environment.
- CI/CD, backup automation, and the transactional outbox schema itself remain later phases.

### Future migration path
When measured need exists: add PgBouncer for connection pressure, managed PostgreSQL/Redis for operational burden, dedicated worker pools by queue for backlog isolation, and a result backend only for a documented consumer and retention policy. Preserve the same settings and service interfaces so that operational evolution does not require an application rewrite.

---

## ADR-0015 — Identity, tenancy and credential handling

**Status:** Accepted · 2026-08-12 · *(introduced during Phase 3 implementation; implements ADR-0009 and gives ADR-0002 its enforcement mechanism)*

### Context
ADR-0009 fixed the authentication *design*: opaque server-side sessions, Argon2id, double-submit CSRF, hashed API keys in the `oc_{env}_{key_id}_{secret}` format. ADR-0002 fixed the tenancy *strategy*: one shared schema with `tenant_id` on every tenant-owned row, scoped at the application boundary.

Neither said how those properties are *guaranteed* once real code exists. Building Phase 3 forced seven questions that the earlier ADRs leave open, and each of them is a security boundary rather than a coding-style preference:

- ADR-0002 says a missing `tenant_id` filter is "a data-leak class of bug" and names scoped repositories as the control — but nothing so far *prevents* a developer from writing a bare query.
- Returning 403 for an object that belongs to another tenant confirms that the object exists. Returning 404 does not. The earlier ADRs never chose.
- The permission catalogue exists in `docs/security.md` prose, in Python enums, and in seeded database rows. Three copies of a security rule will drift.
- `docs/security.md` requires case-insensitive e-mail identity, which in PostgreSQL usually implies `citext`.
- ADR-0009 allows a Redis session cache with PostgreSQL authoritative, but says nothing about caching *role* lookups, which is the actual per-request cost.
- Nothing prevented an API key from being minted with more authority than the person minting it.
- "Sign out everywhere" needs to invalidate sessions that a single `DELETE` cannot cheaply reach.

### Decision

**1. Tenant scoping is structural, not conventional.** Tenant-owned data is reachable only through a repository constructed with a `TenantContext`. `TenantScopedRepository` holds the tenant and applies it; there is no code path that takes a `tenant_id` argument a caller could omit or supply from a request body. Repositories that are legitimately global (user lookup by e-mail, session lookup by digest, API-key lookup by `key_id`) are separate classes with names that say so, so the exceptions are visible rather than incidental.

**2. Cross-tenant access to a real object is 404, never 403.** A 403 says "this exists and is not yours", which is an enumeration oracle for identifiers. The same rule applies to authentication: signing in against a tenant you do not belong to produces a session with no principal rather than an error, so "no such workspace" and "not your workspace" are indistinguishable. 403 is reserved for a caller who *is* a member of the tenant but lacks the permission; a missing membership is 404.

**3. The RBAC catalogue is seeded by the migration and asserted against the domain table by a test.** The database rows are what actually authorise a request, so they are the operative copy; the Python enums exist so application code can reason about permissions without a query. An integration test compares the seeded `role_permissions` rows against `DEFAULT_ROLE_GRANTS` per role. If the two ever diverge, the build fails rather than the database silently winning.

**4. E-mail is a plain `text` column with a lowercase `CHECK`, not `citext`.** The application normalises addresses on the way in; the constraint guarantees no future code path can insert `Ada@example.com` beside `ada@example.com` and hand one mailbox two accounts. The uniqueness index is therefore an ordinary one.

**5. The Redis role-slug cache is a bounded-staleness optimisation.** PostgreSQL stays authoritative, matching ADR-0009's posture for the session cache. The cache degrades to a direct query when Redis is absent, so the API remains correct — merely slower — during a Redis outage, consistent with ADR-0003's "Redis is disposable" rule.

**6. API-key scopes are intersected against the creator's permissions at issue time.** A key can never carry more authority than the person who minted it, so a compromised key cannot be used to escalate by re-issuing a broader one. An unrecognised scope is a 422 that names it; a real scope the caller does not hold is the same 403 that calling the endpoint directly would produce.

**7. Session invalidation uses a `session_epoch` counter on the user.** Incrementing it invalidates every outstanding session in one write, without enumerating rows, and covers sessions created between the read and the write.

Supporting rules that follow from the above: only digests of session tokens, CSRF tokens and API-key secrets are persisted; an API key's plaintext is returned exactly once, in the response that created it; audit context is scrubbed against a marker list before it is written; and response schemas are built field by field rather than from ORM attributes, so a column added later cannot leak by default.

### Alternatives
1. Pass `tenant_id` explicitly to every query and rely on review plus tests.
2. Rely on PostgreSQL RLS as the Phase 3 isolation mechanism.
3. Return 403 for cross-tenant access, as most frameworks do by default.
4. Treat the Python permission table as authoritative and derive the seed from it at runtime.
5. Use `citext` for e-mail.
6. Cache the fully resolved permission set rather than role slugs.
7. Delete session rows to implement "sign out everywhere".

### Rejection rationale
1. This is the status quo ADR-0002 already identified as a data-leak class of bug. Review catches most omissions; "most" is not a security boundary, and the failure is silent.
2. RLS is the P2 defence-in-depth layer, not the primary mechanism — ADR-0002 already settled that, and it protects only the SQL surface. Adding it now would also require a per-transaction session variable on every connection before any code depends on it.
3. It leaks existence. The cost of returning 404 is one confusing support ticket a year; the cost of 403 is a working identifier oracle.
4. Seeding from application code at runtime means the migration is not self-contained, cannot be reviewed as SQL, and would let a code change silently alter permissions in production without a migration. The parity test gives the same protection without that coupling.
5. `citext` is a reasonable choice, but it widens the extension surface, and its comparison semantics are locale-dependent in ways that are easy to get subtly wrong. An explicit `CHECK` plus application normalisation is portable, greppable and obvious in the schema.
6. Permission sets are larger, change shape when the catalogue changes, and would need invalidating on any grant edit. Role slugs are small and stable, and expanding slugs to permissions in process is cheap.
7. Deleting rows loses the audit trail and races with sessions created during the delete. A counter comparison is atomic and leaves history intact.

### Consequences
- A new module gets tenant isolation by extending `TenantScopedRepository`; it does not get to invent its own scoping.
- Permission changes become visible after at most `session_cache_ttl_seconds` (default 60 s). This is a deliberate, documented staleness window, not an accident.
- The parity test couples the migration to the domain table on purpose: changing a role grant requires a new migration *and* a domain change, and forgetting either fails CI.
- 404-for-cross-tenant means logs and traces are the only way to distinguish "missing" from "forbidden" during support. `internal_message` carries the real reason, per ADR-0013.
- Argon2id at the documented cost makes login deliberately expensive; tests run at the settings floor, and the production floor is enforced by a settings validator rather than by convention.
- E-mail verification and password reset are modelled (`email_tokens`) but have no transport yet, so those flows are incomplete until the P1 delivery work lands.
- Login is throttled per account, not per source address; per-address limiting needs the shared rate limiter that does not exist yet.

### Future migration path
Add RLS policies keyed on a per-transaction `app.tenant_id` as defence in depth once the schema stabilises — the repository layer already guarantees the value is server-derived, so RLS becomes a second lock on the same door rather than a redesign. Add TOTP MFA, SSO and custom roles behind the same `Principal`/permission-resolution point, so authorisation call sites do not change. If the staleness window ever becomes unacceptable, publish an invalidation message on role change instead of shortening the TTL.
