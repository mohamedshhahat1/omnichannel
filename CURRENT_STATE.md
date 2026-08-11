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
| Business modules | ⛔ None — `app/modules/` is intentionally empty |
| Database models | ⛔ None (Phase 2) |
| Migrations | ⛔ None (Phase 2) |
| Redis / Celery | ⛔ None (Phase 2) |
| Docker / Compose | ⛔ None (Phase 2) |
| Authentication / RBAC | ⛔ None (Phase 3) |
| CI/CD | ⛔ None (Phase 14) |
| Infrastructure | ⛔ None provisioned |

The repository contains documentation plus a runnable FastAPI foundation with no business functionality. This is deliberate.

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

Deliberately **not** built: database, Redis, Celery, Docker, authentication, RBAC, CSRF, business modules, CI.

One new decision was recorded: **ADR-0013 — Standard API error envelope and error taxonomy**.

---

## 3. Current phase

**Phase 1 — complete. Awaiting explicit approval to begin Phase 2.**

---

## 4. Current task

None in progress.

---

## 5. Next task

**Phase 2 — Configuration, Docker, PostgreSQL, Alembic, Redis, Celery** (do not start automatically; wait for explicit instruction).

Scope when started:

- Dockerfile (non-root, multi-stage) and Compose for local development
- SQLAlchemy 2.x async engine, session management, `Base`, naming conventions
- Alembic setup and the expand → migrate → contract workflow
- Redis connection management
- Celery application, queue routing, beat scheduling
- PostgreSQL and Redis readiness checks registered into the existing health registry
- Dependency lock file

The Phase 1 structure was designed so none of this requires restructuring: the engine and pool open in the existing `lifespan`, their checks register into the existing `HealthRegistry`, their settings become new sections on the existing `Settings`, and their instrumentation attaches to the existing tracer provider.

---

## 6. How to run

```bash
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env

uvicorn app.main:app --reload    # http://127.0.0.1:8000/health/live

pytest                           # tests
ruff check .                     # lint
ruff format --check .            # formatting
mypy                             # strict type checking
```

See `backend/README.md` for detail.

---

## 7. Known issues

- **The four quality gates have not been executed in a networked environment.** The Phase 1 code was authored in an offline sandbox with no package index, so `pytest`, `ruff check`, `ruff format --check` and `mypy` could not be installed or run. What *was* verified: every file byte-compiles under Python 3.13, `pyproject.toml` parses, and 49 pure-logic test functions (correlation, errors, health registry, logging) execute green under a stdlib harness. **Run all four gates locally before building on this foundation** and fix any findings in a follow-up commit.
- No dependency lock file yet — see TODO P0.
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
| Quality gates unverified in CI | No network in the authoring environment | Phase 2, when Docker gives a reproducible environment; enforced in CI at Phase 14 |

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
