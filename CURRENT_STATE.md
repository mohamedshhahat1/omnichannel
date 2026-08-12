# CURRENT_STATE

> Update this file after **every** meaningful milestone. It is the first thing a new engineer or AI session should read after `PROJECT_CONTEXT.md`.

**Last updated:** 2026-08-12

---

## 1. Implementation status

| Area | Status |
|---|---|
| Architecture & documentation | ✅ Complete (Phase 0) |
| Application foundation (`backend/`) | ✅ Complete (Phase 1) |
| Configuration, logging, correlation, errors, health, tracing bootstrap | ✅ Complete (Phase 1) |
| Infrastructure foundation (PostgreSQL, Redis, Celery, Alembic, Docker) | ✅ Complete (Phase 2) |
| Business modules | ⚠️ `identity` only (Phase 3). Every other module in `app/modules/` is still unstarted |
| Database models | ⚠️ Identity entities only (Phase 3). Conversation, channel, event, catalog and billing tables are Phase 4+ |
| Authentication / RBAC | ✅ Complete (Phase 3). The 2026-08-12 RBAC correction is **merged into this branch** — see §3 |
| CI/CD | ✅ GitHub Actions pipeline (Ruff lint, Ruff format, MyPy, unit + integration Pytest on real PostgreSQL/Redis, Docker build, Compose validation) |
| Infrastructure | ⚠️ Local development topology only; no production services provisioned |

The repository now contains documentation, a runnable FastAPI and worker foundation with PostgreSQL/Redis/Celery infrastructure, and a complete identity and access layer. There is still no conversational or channel functionality. This remains deliberate.

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

### CI/CD Foundation ✅

Delivered `.github/workflows/ci.yml` on `phase-2-infrastructure`. Triggers: every pull request and every push to `main` or `phase-2-infrastructure`. Seven separate required checks, each a named job: `ruff-lint`, `ruff-format`, `mypy`, `pytest-unit`, `pytest-integration` (real `pgvector/pgvector:pg16` and `redis:7-alpine` service containers, the committed role bootstrap, and `alembic upgrade head` first), `docker-build` (production `backend/Dockerfile`; build only, nothing pushed), and `compose-config` (`docker compose config`). Least-privilege permissions (`contents: read`), no secrets, pip caching keyed on `backend/requirements.lock`, no `continue-on-error`. `backend/tests/unit/test_ci_workflow.py` guards the workflow definition (paths, Python version, commands, working directories, dependency installation, service configuration, permissions).

**How to interpret failures:** each check is a separate job; a red job fails the pipeline and blocks merge. Reproduce locally with the matching command: `cd backend && ruff check .` / `ruff format --check .` / `mypy` / `pytest -m "not integration"` / `pytest -m "integration"`, or `docker build -t omnichannel-backend:ci backend` / `docker compose config` from the repository root.

**Important:** CI results are verified manually by the human operator. Automated agents (including Opus) must not access GitHub Actions results, inspect workflow runs, or request CI credentials or tokens.

### Phase 3 — Identity and access ✅

Delivered in `backend/`. Additive only: no Phase 0–2 architecture, lifecycle, error envelope, test or CI configuration was replaced or weakened.

| Area | What exists |
|---|---|
| Domain rules | `app/modules/identity/domain.py` — the 15-permission catalogue, six system roles and their default grants, `Principal` / `TenantContext`, e-mail / slug / display-name normalisation, password policy |
| Credential primitives | `app/core/security.py` — Argon2id hashing with rehash detection, 256-bit opaque token generation, SHA-256 digesting, constant-time comparison, API-key minting and parsing |
| Configuration | `app/core/settings.py` — new `AuthSettings` section; production floors (Argon2 cost, `__Host-` cookie prefix, `Secure`) enforced by the existing hardening validator |
| Tables | `app/modules/identity/models.py` — `tenants`, `users`, `memberships`, `roles`, `permissions`, `role_permissions`, `membership_roles`, `sessions`, `api_keys`, `email_tokens`, `audit_logs` |
| Repositories | `app/modules/identity/repositories.py` — `TenantScopedRepository` base; tenant scoping is structural rather than a filter each caller must remember |
| Services | `app/modules/identity/services/` — authentication, sessions, API keys, RBAC resolution from `role_permissions` behind a Redis effective-permission cache, provisioning, audit, authorisation helpers, input validation |
| HTTP surface | `app/modules/identity/api/` — 13 endpoints under `/api/v1`, FastAPI dependency providers, response schemas built field by field so no ORM attribute can leak |
| Migration | `alembic/versions/20260812_0100_0002_identity_and_access.py` — revision `0002_identity_access`, down revision `0001_initial_infra`; creates the eleven tables and seeds the permission catalogue and system roles |
| Errors | `app/modules/identity/errors.py` — eleven domain errors mapped onto the existing ADR-0013 envelope |
| Tests | 5 unit modules and 3 integration modules; roughly 71 identity tests plus the RBAC resolution suite added on 2026-08-12 |

One new decision was recorded: **ADR-0015 — Identity, tenancy and credential handling**, with a dated correction note covering the RBAC change below.

**Authentication.** Passwords are Argon2id only; the plaintext is never stored, logged, traced or echoed in an error. Sessions are opaque 256-bit tokens delivered in an `HttpOnly` cookie; only the SHA-256 digest is persisted, so a database disclosure yields no usable session. Unknown address, wrong password and malformed address produce an identical 401, and registration returns an identical 202 whether or not the address is already taken — neither endpoint is an account-existence oracle. Repeated failures lock the account for a configured interval, after which even the correct password is refused. Sessions carry both an idle and an absolute expiry, and a `session_epoch` on the user invalidates every outstanding session at once.

**Authorisation.** Effective permissions are resolved from PostgreSQL: `membership_roles → roles → role_permissions → permissions`. **`role_permissions` is the runtime RBAC source of truth.** `DEFAULT_ROLE_GRANTS` in `identity/domain.py` defines the initial/default system-role grant matrix used for seeding, reference and testing, and is not consulted during runtime authorization — an integration test still asserts the seeded rows match it, pinning the Python-defaults-to-seed direction. Permission checks raise the existing taxonomy: 403 for a member who lacks a permission, 404 when there is no membership at all. Role changes take effect on the next request, not whenever the session happens to expire.

> **Corrected 2026-08-12.** This paragraph previously read "Six system roles resolve to a fixed permission set, seeded by the migration and asserted against the domain table by an integration test — if the two ever disagree, the test fails rather than the database silently winning." That described the delivered Phase 3 behaviour accurately: the Python constant was the operative matrix and the database rows were a verified copy. The implementation has since been corrected so the database rows are operative. See `docs/security.md` §3.2 and the ADR-0015 correction note.

**Tenancy.** Every tenant-scoped read and write goes through a repository constructed with a `TenantContext`, so "forgot the tenant filter" is not something a caller can express. A real, active membership in another tenant resolves to `None` through this tenant's repository, by id and by user, and never appears in a listing. Cross-tenant access to an object that exists is reported as 404, never 403, so an identifier cannot be probed. Signing in against a tenant you do not belong to yields a session with no principal rather than an error, making "no such workspace" and "not your workspace" indistinguishable.

**Sessions and API keys.** API keys use the `oc_{env}_{key_id}_{secret}` format from `docs/security.md`; only `key_id` is stored in the clear, so authentication is one indexed lookup rather than a scan that hashes every stored key. The plaintext is returned exactly once, in the response that created it. Keys always expire, can be revoked, are tenant scoped, and can never be granted more authority than the person who created them. Malformed, unknown, wrong-secret, wrong-environment, revoked and expired keys all fail identically.

**Auditability.** `audit_logs` records the actor, tenant, action, outcome and correlation id. Context is scrubbed against a marker list before it is written, and an integration test asserts that a failed login's context does not contain the attempted password.

---

## 3. Current phase

**Phase 3 — Identity and access: implemented, documented, and merged into `phase-2-infrastructure`.**

**RBAC correction (2026-08-12) — merged.** Runtime authorization resolves effective permissions from PostgreSQL `role_permissions` rather than from the Python `DEFAULT_ROLE_GRANTS` constant. The branch `fix/rbac-database-authoritative` was **squash-merged** into `phase-2-infrastructure` on 2026-08-12. Because the merge was a squash, the branch's individual commits do not appear on this branch as separate objects; their content does, in one commit.

**Verification status — read this carefully, the distinction matters.** The operator reported the CI pipeline green for the correction branch head immediately before the merge, after four rounds of CI-surfaced fixes: MyPy typing and `204` response models, the login lockout guard, expired-key staleness, the stale Phase-1 `test_database` metadata assertion, error-envelope parity on the unknown-address path, and invited-member test setup. Those four rounds are the first time this repository's gates had ever been executed, and they found real defects, which is exactly why the earlier "written, not run" wording existed.

What this file can and cannot assert:

- **Recorded, not observed:** the green result is the operator's report. No automated agent has accessed GitHub Actions results, and per repository policy none may.
- **Tree identity:** the squash commit on `phase-2-infrastructure` has the same tree as the branch head that was reported green. Its own pipeline run is the operator's to verify.

Phase 4 must not begin until the operator explicitly approves.

---

## 4. Current task

None in progress. The 2026-08-12 audit merged the RBAC correction and reconciled the documentation that still described it as unmerged and unexecuted. The next decision is the operator's: confirm CI on `phase-2-infrastructure` and approve or defer Phase 4.

---

## 5. Next task

**Phase 4 — Event backbone** (do not start automatically; wait for explicit instruction).

Expected scope when started, per `docs/architecture.md` §15:

- `webhook_events` and `outbox_events` tables
- The outbox dispatcher with `FOR UPDATE SKIP LOCKED` and lease reclaim
- `processed_events` consumer-side idempotency
- Dead-letter state and replay tooling

---

## 6. How to run

```bash
cp backend/.env.example backend/.env
docker compose up --build
```

Apply migrations (never run automatically from application startup):

```bash
cd backend && alembic upgrade head
```

Useful endpoints after startup:

- `http://127.0.0.1:8000/health/live`
- `http://127.0.0.1:8000/health/ready`

Identity endpoints, all under `/api/v1`:

| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/register` | Create a user and their first tenant (always 202) |
| POST | `/auth/login` | Issue a session and CSRF token |
| GET | `/auth/me` | Current identity, roles and permissions |
| POST | `/auth/logout` | Revoke this session |
| POST | `/auth/logout-all` | Revoke every session for the user |
| POST | `/tenants` | Create an additional tenant |
| GET | `/roles` | The role catalogue, with each role's grants read from `role_permissions` |
| GET | `/members` | List members of the current tenant |
| POST | `/members` | Invite a member with a role |
| POST | `/members/{membership_id}/roles` | Assign a role |
| GET | `/api-keys` | List keys (never secrets) |
| POST | `/api-keys` | Mint a key; the plaintext appears only here |
| DELETE | `/api-keys/{api_key_id}` | Revoke a key |

There is deliberately **no** endpoint that accepts an invitation. See the invited-membership limitation in §7.

Browser-facing writes require the double-submit CSRF header. Service clients send `Authorization: Bearer oc_{env}_{key_id}_{secret}` instead, which bypasses CSRF because it is not a cookie.

Local quality commands (when the tools are available):

```bash
cd backend
pytest
ruff check .
ruff format --check .
mypy
python -m compileall app tests alembic
```

Integration tests are opt-in and require `OC_TEST_DATABASE_URL` (real PostgreSQL via `asyncpg`) and `OC_TEST_REDIS_URL`. The RBAC resolution suite needs **both**: its four caching and tenant-isolation tests skip without Redis, and skipping them removes exactly the evidence that the permission cache is safe.

---

## 7. Known issues

- **Phase 3 quality gates were only partially executable in the authoring environment.** That environment has no network and no third-party packages installed, so `pytest`, `ruff check .`, `ruff format --check .`, `mypy`, `alembic` and Docker **could not be run** there and were not assumed to pass. What was executed locally: byte-compilation and AST parsing of all 33 Phase 3 files, a >100-character line scan, a trailing-whitespace and tab scan, a trailing-newline check, a relative-import (TID252) scan, a credential-literal scan, an AST-based unused-import approximation, and a tokenizer-based approximation of `ruff format`'s string-quote normalisation. Those scans caught and fixed three real defects that would otherwise have failed CI (an unused `MembershipRepository` import, and two single-quoted strings `ruff format` would rewrite). Reading the integration suite back also caught a fourth: it drove the app with a synchronous test client, which would have handed an asyncpg connection to a second event loop. **Status 2026-08-12:** the gates have since been executed in CI and the operator reports them green; this file records that report rather than an observation made here. The authoritative verdict for every gate remains CI, observed by the operator.
- **The 2026-08-12 RBAC correction was authored with even less available.** That environment had no shell at all: no working tree, no `git` binary, no Python interpreter, no PostgreSQL, no Redis, no Docker. Not one check was run against it at authoring time — not even byte-compilation — and `git status` and `git diff` were replaced by reading the pushed commits back through the GitHub API. **Status 2026-08-12:** CI subsequently exercised it and surfaced four rounds of genuine defects, every one of which was fixed on the branch before the merge, and the operator reports the pipeline green. The change is therefore no longer unexercised. It remains true that no statement about it in this repository rests on a run performed by an automated agent.
- **CI verifies all quality gates** (Ruff lint, Ruff format, MyPy, unit + integration Pytest, Docker build, Compose validation). However, the initial offline-authored `requirements.lock` is still a direct-pin set only: CI installs from it as documented, and transitive dependencies float until the lock is regenerated with a networked resolver (P0). Phase 3 added `argon2-cffi` to it under the same limitation.
- `backend/requirements.lock` is intentionally **not** a fully resolved production lock. It is a temporary offline-authored direct dependency pin set with no fabricated hashes or transitive claims. Regenerate it in CI or another networked environment.
- **An invited member cannot reach the tenant they were invited to.** `ProvisioningService.invite_member` creates the membership with status `invited`. Both `_select_tenant` (at login) and `_principal_for_session` (on every request) require status `active`, and **no Phase 3 endpoint performs the `invited → active` transition**. The invited person can sign in with the initial password the inviter set, but every tenant-scoped request answers `404` until someone activates the row directly in the database:

  ```sql
  UPDATE memberships SET status = 'active', accepted_at = now() WHERE id = :membership_id;
  ```

  This is the acceptance half of the deferred e-mail work (`invite_member`'s docstring, `docs/security.md` §2.6), and it is broader than "no transport": the state transition itself is missing, so even an out-of-band invitation cannot be completed through the API. `POST /members` is honest about what it does — it creates a membership — but the tenant is unusable to the invitee until activation. Recorded in `TODO.md` under P1.
- **Identity limitations carried forward.** `email_tokens` exists and is migrated, but no delivery mechanism is wired, so e-mail verification and password reset are modelled and not yet usable. There is no per-endpoint rate limiting yet — only per-account lockout — so login is throttled per identity but not per source address. There is no MFA, no SSO and no custom roles; all three are explicitly P2. There is no API for editing roles or `role_permissions`: the rows are now runtime-authoritative but only a migration writes them.
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
| Redis effective-permission cache introduces a staleness window | A grant edited directly in the database, outside the service layer, becomes visible after at most `session_cache_ttl_seconds` (default 60 s). Mutations made through the application invalidate the entry immediately; the alternative is a join on every authenticated request | Permission changes must take effect instantly even for out-of-band SQL, or the cache is shown to be unnecessary |
| No API for role or `role_permissions` mutation | `role_permissions` is now runtime-authoritative, but only migration `0002_identity_access` writes it. The invalidation hook exists and is called on role assignment, so exposing an API is wiring rather than redesign | Role management is offered to tenant administrators (P1) |
| No `invited → active` membership transition | Acceptance was deferred alongside the e-mail transport, but the deferral removed the state change as well as the delivery, so an invited member is stranded until an operator edits the row (§7). The repository work is small — a service method plus an endpoint — and independent of the transport | The e-mail delivery work is picked up, or sooner if invitations are used before then (P1) |
| `email_tokens` modelled without a delivery path | The table and lifecycle belong with the identity schema; the transport is a separate concern | E-mail verification or password reset becomes a product requirement |
| Login throttled per account, not per source address | Per-account lockout is the control that protects the account; per-address limiting needs the shared rate limiter that does not exist yet | The rate limiting work in P1 is picked up |
| Plain `text` e-mail column with a lowercase CHECK rather than `citext` | Keeps the extension surface small and the constraint explicit and portable; normalisation happens in the application and is enforced by the database | Case-insensitive matching is needed somewhere the CHECK cannot cover |

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
11. Tenant-scoped data is reached through a repository constructed with a `TenantContext`, never through an ad-hoc query with a `tenant_id` filter. If a new module needs tenant-scoped access, extend `TenantScopedRepository`.
12. Authorisation happens in the service layer through `services/authorization.py`, not in the route. A missing permission is 403; a missing membership is 404.
13. Anything that lets a caller distinguish "exists but is not yours" from "does not exist" is a bug. Cross-tenant access to a real object returns 404.
14. Credential material is stored as a one-way digest and returned to a caller exactly once, at creation. Never add it to a response schema, a log line, an audit context or an exception message.
15. **`DEFAULT_ROLE_GRANTS` seeds the database; it never answers an authorization question.** Effective permissions come from `role_permissions` through `PermissionResolver`. If you add a code path that mutates a role, a role's grants or a membership's roles, it must call `PermissionResolver.invalidate` for every affected membership.
