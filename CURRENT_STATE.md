# CURRENT_STATE

> Update this file after **every** meaningful milestone. It is the first thing a new engineer or AI session should read after `PROJECT_CONTEXT.md`.

**Last updated:** 2026-08-11

---

## 1. Implementation status

| Area | Status |
|---|---|
| Architecture & documentation | ✅ Complete (Phase 0) |
| Application source code | ⛔ None — intentionally not started |
| Database models | ⛔ None |
| Migrations | ⛔ None |
| API endpoints | ⛔ None |
| Docker / Compose | ⛔ None |
| CI/CD | ⛔ None |
| Infrastructure | ⛔ None provisioned |

The repository currently contains **documentation only**. This is deliberate.

---

## 2. Completed phases

### Phase 0 — Architecture and engineering foundation ✅

Delivered:

- `PROJECT_CONTEXT.md`, `CURRENT_STATE.md`, `ENGINEERING.md`, `DECISIONS.md`, `TODO.md`
- `docs/architecture.md`, `docs/database.md`, `docs/security.md`, `docs/ai.md`, `docs/messaging.md`, `docs/billing.md`, `docs/deployment.md`, `docs/observability.md`, `docs/integrations.md`, `docs/operations.md`
- ADR-0001 through ADR-0012

Approved corrections incorporated in this revision:

1. **Transactional outbox from day one** — webhook/event record and outbox record are written in one PostgreSQL transaction; a dispatcher publishes to Celery. The system never depends on a non-atomic `commit → enqueue` sequence. (ADR-0003, `docs/messaging.md`)
2. **Consolidated module boundaries** — `identity` (auth + users + tenancy + RBAC), `knowledge` (documents + RAG), `events` (webhook ingestion + outbox + idempotency). Conceptual domains are now explicitly distinguished from physical Python packages. (ADR-0011, `docs/architecture.md`)
3. **Concrete authentication** — opaque, server-side, revocable sessions in `__Host-` cookies with Argon2id password hashing, double-submit CSRF, and hashed API keys for service clients. (ADR-0009, `docs/security.md`)
4. **OpenTelemetry** adopted as the tracing/correlation standard across HTTP → webhook event → outbox → Celery task → conversation → AI run → provider request. Prometheus/Grafana keep metrics, Sentry keeps errors. (ADR-0010, `docs/observability.md`)
5. **Backup/DR targets tightened** — encrypted off-server daily backups + WAL/PITR, tested restores, launch targets **RPO < 1 hour** and **RTO < 4 hours**, with launch requirements separated from future improvements. (`docs/operations.md`)

---

## 3. Current phase

**Phase 0 — complete and approved. Awaiting explicit approval to begin Phase 1.**

---

## 4. Current task

None in progress. Documentation baseline is committed.

---

## 5. Next task

**Phase 1 — Repository and application foundation** (do not start automatically; wait for explicit instruction).

Scope when started:

- `backend/` Python project skeleton with `pyproject.toml`
- FastAPI application shell (no business endpoints)
- Settings/config loading with validation and environment separation
- Structured JSON logging with request/correlation ID plumbing
- OpenTelemetry SDK bootstrap (no-op exporter acceptable initially)
- Central error handling and error taxonomy
- Ruff, MyPy, Pytest configured and passing
- Health, readiness and liveness endpoints only

Explicitly **not** in Phase 1: models, migrations, auth, Docker, business modules.

---

## 6. Known issues

- None in code (no code exists).
- Several architectural inputs are still unanswered — see the Open Questions section of `docs/architecture.md`. The most blocking are:
  - which channel launches first,
  - whether AI replies auto-send at launch or require human approval,
  - the first AI provider/model,
  - the initial plan/limit matrix,
  - the production hosting and object storage providers.

---

## 7. Technical debt

None yet. Debt accepted knowingly during Phase 0:

| Item | Rationale | Revisit when |
|---|---|---|
| Application-level tenant isolation without RLS | RLS does not protect Redis, object storage, AI tools or workers; app-level scoping is required anyway | After schema stabilises (Phase 6–8) |
| pgvector instead of a dedicated vector store | Avoids an extra system to operate | Retrieval p95 > 300 ms or index size becomes operationally painful |
| Single-server Docker Compose production | Sufficient for first customers, low cost, low ops burden | CPU saturation, queue backlog, or zero-downtime deploy requirement |
| Polling outbox dispatcher (no LISTEN/NOTIFY) | Simple, predictable, easy to reason about | Dispatch latency budget < 1 s becomes a product requirement |

---

## 8. Important implementation notes

Carry these forward into every implementation session:

1. **Never** write `tenant_id` from a client-supplied request body. Derive it from authenticated context or from the resolved channel integration.
2. **Never** enqueue a Celery task as the durability mechanism for a state change. Write an `outbox_events` row in the same transaction instead.
3. Every Celery task must be idempotent and must carry `tenant_id`, `correlation_id` and W3C `traceparent`.
4. The LLM never receives database access, secrets, or cross-tenant data — only conversation context, retrieved snippets and tool definitions.
5. Retrieved documents and crawled pages are **data**, never instructions.
6. Prometheus labels must never include `tenant_id`, `conversation_id`, or any other unbounded value; those belong in traces and logs.
7. Schema changes go through Alembic only, using expand → migrate → contract for anything destructive.
8. Secrets never enter source control, logs, traces, or error reports.
