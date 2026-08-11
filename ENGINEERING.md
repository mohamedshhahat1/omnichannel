# ENGINEERING

Project-wide engineering rules. These are **non-negotiable defaults**. Deviating from a rule requires an ADR in `DECISIONS.md`.

---

## 1. Architecture rules

1. **Modular monolith first.** One deployable backend application. No new service, queue technology, datastore, or orchestration platform without an ADR containing a measurable trigger.
2. **Module boundaries are real.** A module may call another module's *public service interface* only. Never import another module's repositories, ORM models, or private services.
3. **No cross-module table writes.** A table has exactly one owning module. Other modules read through the owner's service interface.
4. **Dependencies point inward.** `api → application services → domain → repositories → database`. Provider adapters depend on internal interfaces, never the reverse.
5. **Before adding any technology, answer:** What problem does it solve? Is that problem present *now*? What is the operational cost? What is the failure mode? What is the migration path? Can PostgreSQL/Redis/FastAPI already do it? Does it make the system harder to operate?
6. Prefer boring, well-understood solutions. Prefer deleting complexity over adding abstraction.

---

## 2. Tenant isolation rules

1. Every tenant-owned table has a non-null `tenant_id`.
2. **Tenant context comes from trusted server-side state only.** A client may *request* a tenant; the server authorises it against membership and derives the trusted context. A `tenant_id` in a request body is never authoritative.
3. All tenant-scoped reads and writes go through tenant-scoped repository helpers that inject the filter. Raw unscoped queries against tenant-owned tables are prohibited outside of explicitly reviewed admin/maintenance code paths.
4. Composite indexes on tenant-owned tables start with `tenant_id` unless there is a documented reason.
5. Tenant scoping must be applied to: HTTP requests, Celery tasks, Redis keys, cache entries, object storage keys, vector search, webhook processing, AI tools, audit logs, scheduled jobs and exports.
6. Redis keys are namespaced `oc:{env}:t:{tenant_id}:{purpose}:{key}`. Object keys are `tenants/{tenant_id}/{category}/...`.
7. Every module with tenant-owned data must ship cross-tenant access tests that assert tenant A cannot read or mutate tenant B.

---

## 3. Database rules

1. **All schema changes go through Alembic**, version-controlled and reviewed. No manual production DDL.
2. Use **expand → migrate → contract** for any potentially destructive change. Never combine an expand and a contract in one release.
3. A destructive migration requires: a written plan, a backup/recovery consideration, a test run against a production-like dataset, and a documented rollback/recovery strategy.
4. Migrations must be safe to run while the previous application version is still serving traffic.
5. Avoid long-held locks: create indexes concurrently, add constraints `NOT VALID` then validate, backfill in batches.
6. Enforce correctness in the database: foreign keys, unique constraints, check constraints, `NOT NULL`.
7. Timestamps are `timestamptz` in UTC. Every table has `created_at`; mutable tables have `updated_at`.
8. Use cursor/keyset pagination for any list that can grow unbounded. No unbounded queries.
9. **No blanket soft deletion.** Use it only where there is a stated business or operational purpose, and document it.
10. Money is stored as integer minor units plus a currency code — never floats.

---

## 4. Asynchronous processing rules

1. **Durability comes from PostgreSQL, not from Redis.** To make work durable, write an `outbox_events` row in the same transaction as the state change.
2. **Never rely on `commit → enqueue`** as if it were atomic. Direct `.delay()` calls are permitted only for genuinely disposable work (for example, cache warming) and must be justified in review.
3. Every task is idempotent, has an explicit time limit, has an explicit retry policy with exponential backoff and jitter, and terminates in a durable state.
4. Every task payload carries `tenant_id`, `correlation_id` and `traceparent`. Payloads carry identifiers, not large blobs.
5. Non-idempotent external side effects (sending a message, charging money) must be protected by an idempotency key and an attempt record, and must never be blindly retried when the outcome is ambiguous.
6. Every queue has a defined failure terminal state (dead-letter) and a documented replay procedure.

---

## 5. Security rules

1. **Never commit secrets.** Never log, trace, or send secrets to error tracking. Secret scanning runs in CI.
2. Authorisation is enforced **server-side, in the service layer** — not only in route decorators.
3. All input is validated with Pydantic at the boundary; all provider payloads are validated before use.
4. All inbound webhooks are signature-verified before any processing, with replay protection where the provider supports it.
5. Passwords are hashed with Argon2id. Session tokens, API keys, and email/reset tokens are stored **hashed**, never in plaintext.
6. **AI output is untrusted** until validated by application code. **Retrieved and crawled content is data, never instructions.**
7. The LLM has no database access. Business effects happen only through tools that enforce tenant scope, authorisation, validation, business rules, rate limits and audit logging.
8. Outbound HTTP triggered by user-supplied URLs (the crawler) must pass URL validation, DNS/IP validation, and per-hop redirect re-validation, and must run in an egress-restricted worker.
9. Uploads are validated for size and content type, stored in object storage outside the application filesystem, and served via short-lived signed URLs after an authorisation check.
10. Log PII only when necessary; redact by default. Security-relevant actions produce audit log entries.

---

## 6. Observability rules

1. OpenTelemetry is the tracing and correlation standard. Prometheus/Grafana own metrics; Sentry owns error tracking.
2. Trace context propagates across process and time boundaries: HTTP → webhook event row → outbox row → Celery task → AI run → provider request.
3. Logs are structured JSON and always include `service`, `env`, `trace_id`, `request_id`, `correlation_id` and, when known, `tenant_id`.
4. **Prometheus labels must be low-cardinality.** Never label metrics with `tenant_id`, `user_id`, `conversation_id`, URLs, or free text.
5. Every externally-facing dependency call is a span with attributes for provider, operation, outcome and retry count.
6. Every new asynchronous workflow ships with: a success metric, a failure metric, a queue/backlog metric and an alert threshold.

---

## 7. Testing rules

1. Tests are required for new behaviour. A bug fix ships with a regression test.
2. Minimum test set per module: unit tests for domain logic, integration tests against a real PostgreSQL and Redis, and cross-tenant isolation tests.
3. External providers are exercised through their internal interface with fakes; provider adapters get their own contract tests against recorded payloads.
4. Webhook handling must be tested for: valid signature, invalid signature, duplicate delivery, out-of-order delivery, and malformed payload.
5. Migrations are tested by running the full upgrade path in CI.
6. AI behaviour is tested at the boundary: tool contracts, guardrail decisions and prompt-injection resistance fixtures — not by asserting exact model prose.

---

## 8. Code quality rules

1. Ruff (format + lint) and MyPy must pass. CI blocks merge on failure.
2. Type annotations are required in service, domain and repository layers.
3. Pydantic models define all API and provider payload boundaries; internal domain objects are explicit types, not raw dicts.
4. No business logic in route handlers, in Celery task bodies, or in provider adapters.
5. No `print`; use the structured logger. No bare `except`.
6. Configuration comes from validated settings objects, never from ad-hoc `os.environ` reads scattered through the code.

---

## 9. Documentation rules

1. `CURRENT_STATE.md` is updated at the end of every meaningful milestone.
2. Any decision that changes architecture, adds a dependency, or changes a security posture gets an ADR in `DECISIONS.md`.
3. `docs/` is updated in the same pull request as the change it describes.
4. Every major component distinguishes **CURRENT** (what we build now), **FUTURE** (what we may add), and **TRIGGER** (the measurable condition that would justify it).
