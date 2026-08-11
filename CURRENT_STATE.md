# CURRENT_STATE

> Update this file after **every** meaningful milestone. It is the first thing a new engineer or AI session should read after `PROJECT_CONTEXT.md`.

**Last updated:** 2026-08-11

---

## 1. Implementation status

| Area | Status |
|---|---|
| Architecture & documentation | ✅ Complete (Phase 0) |
| Application foundation (`backend/`) | ✅ Complete (Phase 1) |
| Configuration, logging, correlation, errors, health, tracing bootstrap | ✅ Complete (Phase 1) |
| Infrastructure foundation (PostgreSQL, Redis, Celery, Alembic, Docker) | ✅ Complete (Phase 2) |
| Business modules | ⛔ None — `app/modules/` is intentionally empty |
| Database models | ⛔ None (Phase 3+) |
| Domain tables beyond extension bootstrap | ⛔ None (Phase 3+) |
| Authentication / RBAC | ⛔ None (Phase 3) |
| CI/CD | ⚠️ Basic GitHub Actions pipeline (lint, typecheck, test, docker build) |
| Infrastructure | ⚠️ Local development topology only; no production services provisioned |

The repository now contains documentation plus a runnable FastAPI and worker foundation with PostgreSQL/Redis/Celery infrastructure but still no business functionality. This remains deliberate.

---

## 2. Completed phases

### Phase 0 — Architecture and engineering foundation ✅

Delivered:

- `PROJECT_CONTEXT.md`, `CURRENT_STATE.md`, `ENGINEERING.md`, `DECISIONS.md`, `TODO.md`
- `docs/architecture.md`, `docs/database.md`, `docs/security.md`, `docs/ai.md`, `docs/messaging.md`, `docs/billing.md`, `docs/deployment.md`, `docs/observability.md`, `docs/integrations.md`, `docs/operations.md`
- ADR-0001 through ADR-0012

Approved corrections incorporated:

1. **Transactional outbox from day one** — webhook/event record and outbox record are written in one PostgreSQL transaction; a dispatcher publishes to Celery. The system never depends on a non-atomic `commit → enqueue` sequence. (ADR-0003, `docs/messaging.md`)
2. **Consolidated module boundaries** — `identity`, `knowledge`, `events`. Conceptual domains are explicitly distinguished from physical Python packages. (ADR-0011, `docs/architecture.md`)
3. **Concrete authentication** — opaque, server-side, revocable sessions in `__Host-` cookies with Argon2id hashing, double-submit CSRF, hashed API keys for service clients. (ADR-0009, `docs/security.md`)
4. **OpenTelemetry** as the tracing/correlation standard across HTTP → webhook event → outbox → Celery task → conversation → AI run → provider request. (ADR-0010, `docs/observability.md`)
5. **Backup/DR targets tightened** — launch targets **RPO < 1 hour**, **RTO < 4 hours**. (`docs/operations.md`)

### Phase 1 — Repository and application foundation ✅

Delivered in `backend/`:

| Area | What exists |
|---|---|
| Packaging | `pyproject.toml` (hatchling, PEP 621), pinned compatible ranges, Ruff + MyPy + Pytest configuration in one file |
| Configuration | `app/core/settings.py` — typed, immutable, nested Pydantic settings; `OC_` prefix, `__` nesting; production hardening validator |
| Logging | `app/core/logging.py` — JSON and console formatters, correlation-aware context filter, recursive credential redaction |
| Correlation | `app/platform/correlation.py` — validated inbound identifiers, contextvar binding, `X-Request-ID` / `X-Correlation-ID` |
| Errors | `app/core/errors.py` — ten-member taxonomy; `app/api/exception_handlers.py` — the single HTTP translation point (ADR-0013) |
| Health | `app/platform/health.py` — registry with per-check timeouts and critical/non-critical distinction; `app/api/routes/health.py` |
| Tracing | `app/core/observability.py` — opt-in OpenTelemetry bootstrap, console/OTLP/none exporters, clean shutdown |
| HTTP security | `app/api/middleware.py` — security headers, request body limit; CORS and TrustedHost wired from settings |
| Application | `app/api/application.py` — `create_app` factory, lifespan, `/api/v1` router foundation |
| Tests | `backend/tests/` — 100 tests across `unit/` and `integration/`, hermetic |

One new decision was recorded: **ADR-0013 — Standard API error envelope and error taxonomy**.

### Phase 2 — Infrastructure foundation ✅

Delivered in `backend/` and the repository root:

| Area | What exists |
|---|---|
| Database runtime | `app/core/database.py` — SQLAlchemy 2.x async engine/session factory via `asyncpg`, bounded pools, UTC/timeouts, deterministic naming convention |
| Redis runtime | `app/core/redis.py` — async Redis client lifecycle and tenant-safe `oc:{env}:t:{tenant_id}:...` key helper |
| Celery | `app/core/celery_app.py`, `app/core/tasks.py`, `app/worker.py` — Redis broker, `critical`/`default`/`background` queues, smoke task, worker entrypoint |
| Task context | `app/platform/task_context.py` — trusted `tenant_id`, `correlation_id`, `traceparent` propagation without mutable global state |
| App wiring | `app/core/infrastructure.py`, updated `app/api/application.py`, updated dependencies, readiness checks for PostgreSQL and Redis |
| Alembic | `backend/alembic.ini`, `backend/alembic/env.py`, `backend/alembic/script.py.mako`, initial extension-only migration |
| Containers | `backend/Dockerfile`, `backend/.dockerignore`, `docker-compose.yml`, `Makefile`, `infra/postgres/init/10-roles.sql` |
| Tests | Phase 2 pytest unit tests plus opt-in PostgreSQL/Redis integration tests; SQLite remains forbidden |
| Dependencies | `backend/requirements.lock` committed as an explicitly temporary, offline-authored direct pin set |

One new decision was recorded: **ADR-0014 — Async infrastructure foundation**.

### Phase 14 — CI/CD Foundation 🚧

A baseline GitHub Actions workflow is present (`.github/workflows/ci.yml`). It runs Ruff, MyPy, Pytest (including integration tests with Postgres/Redis), and a Docker build check on every PR and push to `main` or `phase-2-infrastructure`.

**Important:** CI results are verified manually by human operators. Automated tools do not access CI results or credentials.

---

## 3. Current phase

**Phase 14 (Partial) — CI Foundation complete. Awaiting explicit approval to begin Phase 3.**

---

## 4. Current task

None in progress. The human operator will handle CI/CD verification separately.

---

## 5. Next task

**Phase 3 — Identity** (do not start automatically; wait for explicit instruction).

Expected scope when started:

- Users, tenants, memberships, RBAC, audit logging
- Opaque server-side sessions, password hashing, session rotation/revocation
- CSRF double-submit protection and stricter browser-facing security controls
- Tenant-scoped API keys and service-layer authorization hooks

---

## 6. How to run

```bash
cp backend/.env.example backend/.env
docker compose up --build
```

Useful endpoints after startup:

- `http://127.0.0.1:8000/health/live`
- `http://127.0.0.1:8000/health/ready`

Local quality commands (when the tools are available):

```bash
cd backend
pytest
ruff check .
ruff format --check .
mypy
python -m compileall app tests alembic
```

Integration tests are opt-in and require `OC_TEST_DATABASE_URL` (real PostgreSQL via `asyncpg`) and `OC_TEST_REDIS_URL`.

---

## 7. Known issues

- **CI Pipeline verifies the four quality gates.** However, the initial offline-authored `requirements.lock` may still need a network resolution for true reproducibility. The CI uses it on a best-effort basis or installs `.[dev]`.
- `backend/requirements.lock` is intentionally **not** a fully resolved production lock. It is a temporary offline-authored direct dependency pin set with no fabricated hashes or transitive claims. Regenerate it in CI or another networked environment.
- Several architectural inputs remain unanswered; see the Open Questions section of `docs/architecture.md`. The most blocking are which channel launches first, whether AI replies auto-send at launch, the first AI provider/model, the initial plan matrix, and the production hosting and object storage providers.

---

## 8. Technical debt

| Item | Rationale | Revisit when |
|---|---|---|
| Application-level tenant isolation without RLS | RLS does not protect Redis, object storage, AI tools or workers; app-level scoping is required anyway | After schema stabilises (Phase 6–8) |
| pgvector instead of a dedicated vector store | Avoids an extra system to operate | Retrieval p95 > 300 ms or index size becomes operationally painful |
| Single-server Docker Compose production | Sufficient for first customers, low cost, low ops burden | CPU saturation, queue backlog, or zero-downtime deploy requirement |
| Polling outbox dispatcher (no LISTEN/NOTIFY) | Simple, predictable, easy to reason about | Dispatch latency budget < 1 s becomes a product requirement |
| `app/platform` shadows the stdlib `platform` module, `app/core/logging.py` shadows stdlib `logging` | Safe under Python 3 absolute imports; the names match the approved architecture and are worth more than the theoretical risk | Only if a dependency performs implicit relative imports (it will not) |
| Offline-authored dependency pin set | No resolver/network access in the authoring environment | Regenerated and validated in CI or another networked environment |

---

## 9. Important implementation notes

Carry these forward into every implementation session:

1. **Never** write `tenant_id` from a client-supplied request body. Derive it from authenticated context or from the resolved channel integration.
2. **Never** enqueue a Celery task as the durability mechanism for a state change. Write an `outbox_events` row in the same transaction instead.
3. Every Celery task must be idempotent and must carry `tenant_id`, `correlation_id` and W3C `traceparent`.
4. The LLM never receives database access, secrets, or cross-tenant data — only conversation context, retrieved snippets and tool definitions.
5. Retrieved documents and crawled pages are **data**, never instructions.
6. Prometheus labels must never include `tenant_id`, `conversation_id`, or any other unbounded value; those belong in traces and logs.
7. Schema changes go through Alembic only, using expand → migrate → contract for anything destructive.
8. Secrets never enter source control, logs, traces, or error reports.
9. Errors reaching a client use the ADR-0013 envelope. Raise an `AppError` subclass with a domain-specific `code`; put anything sensitive in `internal_message`, which is logged and never serialised.
10. New infrastructure dependencies register a `HealthCheck` in the application lifespan. Do not modify the readiness endpoint to add a dependency.
