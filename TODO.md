# TODO

Prioritised backlog. **P0** = blocks the next phase or is a launch blocker · **P1** = required before real production traffic · **P2** = valuable, scheduled later.

> Phase 0 (architecture + documentation) and Phase 1 (application foundation) are complete. Do not start Phase 2 without explicit approval.

---

## P0 — Blocking

### Verify the Phase 1 foundation locally
- [ ] Run `pytest`, `ruff check .`, `ruff format --check .` and `mypy` in `backend/` on a machine with network access, and fix any findings
  - The Phase 1 code was authored offline with no package index available. Compilation, `pyproject.toml` parsing and the pure-logic tests were verified; the four gates themselves were not executed. See CURRENT_STATE section 7.

### Decisions needed from the product owner
- [ ] Choose the first channel to launch (WhatsApp / Instagram DM / Messenger / comments)
- [ ] Decide whether AI replies auto-send at launch or require human approval (per channel; public comments may differ from DMs)
- [ ] Choose the first LLM + embedding provider and model
- [ ] Define the initial plan matrix: prices, features, and limits
- [ ] Choose the production host and the S3-compatible storage provider
- [ ] Confirm the frontend approach for the dashboard (affects CORS/cookie configuration)

### Phase 2 readiness (do not start until approved)
- [ ] Dependency lock file so installs are reproducible across machines and CI (`uv.lock`, or `pip-compile` output committed as `requirements.lock`)
- [ ] Dockerfile for API and worker: multi-stage, non-root user, minimal base, no build toolchain in the runtime layer
- [ ] Docker Compose for local development (API, PostgreSQL, Redis, worker, beat)
- [ ] SQLAlchemy 2.x async engine, session lifecycle, declarative `Base`, constraint naming convention
- [ ] Alembic configuration and the expand → migrate → contract workflow
- [ ] Redis connection management and key namespacing (`oc:{env}:t:{tenant_id}:...`)
- [ ] Celery application, queue routing, beat schedule, task base class carrying `tenant_id` / `correlation_id` / `traceparent`
- [ ] Register PostgreSQL and Redis checks into the existing `HealthRegistry` from the application lifespan — do not modify the readiness endpoint
- [ ] Extend `Settings` with `database`, `redis` and `celery` sections following the existing pattern

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
- [ ] Argon2id password hashing with tuned parameters
- [ ] Opaque session issuance, rotation, sliding/absolute expiry, revocation
- [ ] CSRF double-submit protection and strict CORS policy
- [ ] Email verification and password reset flows (hashed, single-use, expiring tokens)
- [ ] Tenant-scoped API keys with hashed secrets, scopes and rate limits
- [ ] Meta and Paddle webhook signature verification with replay protection
- [ ] Central authorisation checks in the service layer, with per-permission tests
- [ ] Cross-tenant isolation test suite (database, Redis, object storage, retrieval, tools)
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
- [ ] Integration test harness with real PostgreSQL and Redis
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
| No dependency lock file | Phase 1 has no reproducible-build requirement yet | Phase 2, before Docker images are built |
| Quality gates run manually, not in CI | CI is Phase 14; running them locally is cheap | Phase 14, or earlier if a regression slips through |
| `app/platform` and `app/core/logging.py` shadow stdlib module names | Safe under Python 3 absolute imports; names match the approved architecture | Only if a dependency performs implicit relative imports |
