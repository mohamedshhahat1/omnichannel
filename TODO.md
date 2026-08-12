# TODO

Prioritised backlog. **P0** = blocks the next phase or is a launch blocker · **P1** = required before real production traffic · **P2** = valuable, scheduled later.

> Phase 0 (architecture + documentation), Phase 1 (application foundation), Phase 2 (infrastructure foundation), and Phase 3 (identity and access) are complete. Phase 3 is pushed and awaiting human CI verification. Do not start Phase 4 without explicit approval.

---

## P0 — Blocking

### Verify the Phase 2 foundation in a networked environment
- [ ] Run `pytest`, `ruff check .`, `ruff format --check .`, and `mypy` in `backend/` on a machine with network access, and fix any findings
- [ ] Run opt-in PostgreSQL/Redis integration tests with `OC_TEST_DATABASE_URL` and `OC_TEST_REDIS_URL`
- [ ] Regenerate `backend/requirements.lock` with a real resolver, transitive dependencies, and hashes; review the diff

> **Corrected 2026-08-12 (Phase 0–3 audit).** The first two items were previously ticked. They were unticked because no authoring environment for Phase 2 or Phase 3 had network access, so these gates have never been executed in this repository. That is what `CURRENT_STATE.md` §7 ("could not be run and must not be assumed to pass"), `docs/security.md` §13 ("CI is the first authoritative run") and the *Verify Phase 3 in CI* item below all say. The tests are **written**; they are not **executed** or **verified**. Re-tick only against an observed run.

### Verify Phase 3 in CI
- [ ] Run and observe GitHub Actions for the Phase 3 commits; the authoring environment had no network and could not execute `pytest`, `ruff`, `mypy`, `alembic` or Docker, so CI is the first authoritative run of those gates
- [ ] Confirm `alembic upgrade head` applies `0002_identity_access` cleanly on a fresh database in the `pytest-integration` job

### Decisions needed from the product owner
- [ ] Choose the first channel to launch (WhatsApp / Instagram DM / Messenger / comments)
- [ ] Decide whether AI replies auto-send at launch or require human approval (per channel; public comments may differ from DMs)
- [ ] Choose the first LLM + embedding provider and model
- [ ] Define the initial plan matrix: prices, features, and limits
- [ ] Choose the production host and the S3-compatible storage provider
- [ ] Confirm the frontend approach for the dashboard (affects CORS/cookie configuration)

### Phase 4 readiness (do not start until approved)
- [ ] `webhook_events` table with provider, external id, signature verification state and payload
- [ ] `outbox_events` table and the transactional write path
- [ ] Dispatcher with `FOR UPDATE SKIP LOCKED`, lease reclaim and backoff
- [ ] `processed_events` consumer-side idempotency
- [ ] Dead-letter state, alerting and a documented replay procedure

### Completed in Phase 3 ✅
- [x] Identity module: users, auth sessions, tenants, memberships, RBAC, audit log
- [x] Opaque session issuance, rotation, revocation, and Argon2id password hashing
- [x] CSRF double-submit protection and strict CORS policy
- [x] Tenant-scoped API keys with hashed secrets and scopes (rate limits deferred — see P1)
- [x] Alembic revision `0002_identity_access`: eleven tables, seeded permission catalogue and six system roles
- [x] `AuthSettings` configuration section with production floors enforced by the existing hardening validator
- [x] `TenantScopedRepository` so tenant scoping is structural rather than a filter each caller must remember
- [x] Enumeration resistance: identical responses for unknown vs. taken address on register, and unknown address vs. wrong password on login
- [x] Cross-tenant access to a real object answered 404 rather than 403 so identifiers cannot be probed
- [x] Audit logging with context scrubbing, asserted not to contain an attempted password
- [x] Unit and integration tests for identity, including adversarial and negative cross-tenant cases, on real PostgreSQL

### Completed alongside Phase 2 — CI/CD foundation ✅

> **Renamed 2026-08-12 (Phase 0–3 audit).** This section was headed "Completed in Phase 14". That was a mislabel: Phase 14 in the `docs/architecture.md` §15 roadmap is *CI/CD & production deployment*, a future phase that has **not** started. The work below is the CI foundation delivered next to Phase 2, which `CURRENT_STATE.md` calls "CI/CD Foundation". No content changed.

- [x] CI workflow (`.github/workflows/ci.yml`) with seven separate required checks on every PR and every push to `main`/`phase-2-infrastructure`: Ruff lint, Ruff format, MyPy, unit Pytest, integration Pytest against real PostgreSQL/Redis service containers, production Docker image build (no push), and `docker compose config` validation — no secrets, least-privilege `contents: read`
- [x] CI guardrail test (`backend/tests/unit/test_ci_workflow.py`) validating workflow paths, Python version, commands, working directories, dependency installation and service configuration
- [x] Fixed the hermetic test environment stripping `OC_TEST_*`, which would have silently skipped the opt-in Phase 2 integration tests in CI

### Completed in Phase 2 ✅
- [x] Dependency pin artifact committed with explicit offline limitation notes
- [x] Dockerfile for API and worker: multi-stage, non-root user, minimal base
- [x] Docker Compose for local development (API, PostgreSQL, Redis, worker, beat, migration)
- [x] SQLAlchemy 2.x async engine, session lifecycle, declarative `Base`, constraint naming convention
- [x] Alembic configuration and the initial extensions migration
- [x] Redis connection management and key namespacing (`oc:{env}:t:{tenant_id}:...`)
- [x] Celery application, queue routing, smoke task, and task context carrying `tenant_id` / `correlation_id` / `traceparent`
- [x] PostgreSQL and Redis checks registered into the existing `HealthRegistry` from the application lifespan
- [x] `Settings` extended with `database`, `redis`, and `celery` sections following the existing pattern
- [x] Phase 2 unit tests and opt-in real PostgreSQL/Redis integration tests added

### Completed in Phase 1 ✅
- [x] `backend/` project skeleton and `pyproject.toml`
- [x] FastAPI application factory with lifespan and `/api/v1` router foundation
- [x] Liveness and readiness endpoints backed by an extensible health registry
- [x] Validated, typed settings layer with development/test/production separation and production hardening
- [x] Structured JSON logging with request and correlation IDs and credential redaction
- [x] OpenTelemetry SDK bootstrap, FastAPI instrumentation, configurable exporter, clean shutdown
- [x] Error taxonomy and central exception handling (ADR-0013)
- [x] Foundational HTTP security: security headers, CORS, trusted hosts, request body limit
- [x] Ruff + MyPy + Pytest configured; 100 hermetic tests written

---

## P1 — Before production traffic

### Security
- [ ] Meta and Paddle webhook signature verification with replay protection
- [x] Central authorisation checks in the service layer, with per-permission tests — delivered in Phase 3 (`app/modules/identity/services/authorization.py`); every later module must route through it rather than checking permissions in a route
- [ ] Cross-tenant isolation test suite (database, Redis, object storage, retrieval, tools) — the database surface is covered for identity in Phase 3; Redis, object storage, retrieval and tools are still outstanding
- [ ] Per-endpoint and per-source-address rate limiting — Phase 3 added per-account lockout only, so login is throttled per identity but not per caller
- [ ] Reject a request that presents both a session cookie and an API key, instead of preferring the bearer key — recorded as a known deviation in `docs/security.md` §2.10, which refers to this list
- [ ] E-mail delivery for verification and password reset; `email_tokens` is modelled and migrated but has no transport
- [ ] Secret scanning and dependency vulnerability scanning in CI
- [ ] Log redaction rules and PII minimisation review
- [ ] Upload validation: size, content type, extension, storage location

### Infrastructure
- [ ] NGINX configuration with TLS and security headers
- [ ] Production Compose with restart policies, health checks and resource limits
- [ ] Encrypted off-server backups + WAL archiving (PITR)
- [ ] Prometheus, Grafana and Sentry wired up
- [ ] OTLP export path for traces

### Reliability
- [ ] `outbox_events` schema, dispatcher with `FOR UPDATE SKIP LOCKED`, lease reclaim
- [ ] `processed_events` consumer-side idempotency
- [ ] Dead-letter state, alerting and a documented replay procedure
- [ ] Stuck-event reconciliation sweep
- [ ] Outbound message attempt records with idempotency keys
- [ ] Retry policies with exponential backoff and jitter for every external call

### Testing
- [ ] Webhook test matrix: valid, invalid signature, duplicate, out-of-order, malformed
- [ ] Outbox tests: crash-before-commit, crash-before-dispatch, crash-before-ack, duplicate publish
- [ ] Migration upgrade path executed in CI
- [ ] Prompt-injection resistance fixtures for retrieved and crawled content
- [ ] Billing webhook idempotency and out-of-order tests

### Operations
- [ ] Restore rehearsal from an encrypted off-server backup, timed against the RTO target
- [ ] Runbooks: outbox backlog, provider outage, AI provider outage, Redis loss, database restore, key rotation
- [ ] Alert routing and on-call expectations
- [ ] Deployment and rollback procedure documented and rehearsed

---

## P2 — Later

- [ ] PostgreSQL Row Level Security as defence in depth
- [ ] Website crawler implementation (design already accepted in ADR-0012)
- [ ] Visual product search (image embeddings, tenant-scoped similarity)
- [ ] Order creation tool and commerce workflows
- [ ] TOTP MFA and step-up authentication
- [ ] SSO / OIDC / SAML and SCIM for enterprise tenants
- [ ] Custom roles and finer-grained permissions
- [ ] Team inbox, SLAs, agent availability, skills-based routing
- [ ] Commerce platform integrations (Shopify, WooCommerce)
- [ ] Partitioning for `messages`, `webhook_events`, `outbox_events`, `usage_events`
- [ ] Read replicas and managed PostgreSQL/Redis
- [ ] Retrieval quality evaluation harness and regression set
- [ ] Cost dashboards per tenant and per feature
- [ ] Load testing and capacity model

---

## Technical debt register

| Item | Accepted because | Revisit when |
|---|---|---|
| No RLS at launch | Application-level scoping is required regardless; RLS protects only one surface | Schema stabilises (Phase 6–8) |
| pgvector on the primary database | Avoids a second datastore | Retrieval p95 breaches SLO or vector load degrades OLTP |
| Polling outbox dispatcher | Simple and predictable | Sub-second dispatch latency becomes a requirement |
| Single-server production | Cost and operational simplicity | CPU saturation, queue backlog, or zero-downtime deploys required |
| `handoff` as a sub-package | Avoids premature module fragmentation | Routing, SLAs or skills-based assignment appear |
| Sentry as the initial trace sink | Avoids running a trace backend early | Trace volume or retention needs justify Tempo/Jaeger |
| Offline-authored dependency pin set | No resolver/network in the authoring environment | Regenerated in CI or another networked environment |
| `app/platform` and `app/core/logging.py` shadow stdlib module names | Safe under Python 3 absolute imports; names match the approved architecture | Only if a dependency performs implicit relative imports |
| Redis role-slug cache staleness window | Avoids a role join on every authenticated request; bounded by `session_cache_ttl_seconds` (default 60 s) | Permission changes must take effect instantly |
| Effective permissions resolved from the Python `DEFAULT_ROLE_GRANTS` table rather than by reading `role_permissions` at runtime | The grant matrix is code plus seeded migration data, not runtime-editable configuration. `PermissionResolver` reads role *slugs* from the database and expands them in process; an integration test asserts the seeded rows and the domain table match exactly, so the two cannot drift | Custom or tenant-defined roles become a requirement (P2), at which point the grant matrix has to become data |
| `email_tokens` modelled without a delivery path | The table and lifecycle belong with the identity schema; transport is a separate concern | E-mail verification or password reset becomes a product requirement |
| Login throttled per account, not per source address | Per-account lockout protects the account; per-address limiting needs the shared rate limiter that does not exist yet | The P1 rate limiting work is picked up |
| Plain `text` e-mail column with a lowercase CHECK rather than `citext` | Smaller extension surface; the constraint is explicit and portable | Case-insensitive matching is needed where the CHECK cannot reach |
