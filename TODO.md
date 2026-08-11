# TODO

Prioritised backlog. **P0** = blocks the next phase or is a launch blocker · **P1** = required before real production traffic · **P2** = valuable, scheduled later.

> Phase 0 (architecture + documentation) is complete. Do not start Phase 1 without explicit approval.

---

## P0 — Blocking

### Decisions needed from the product owner
- [ ] Choose the first channel to launch (WhatsApp / Instagram DM / Messenger / comments)
- [ ] Decide whether AI replies auto-send at launch or require human approval (per channel; public comments may differ from DMs)
- [ ] Choose the first LLM + embedding provider and model
- [ ] Define the initial plan matrix: prices, features, and limits
- [ ] Choose the production host and the S3-compatible storage provider
- [ ] Confirm the frontend approach for the dashboard (affects CORS/cookie configuration)

### Phase 1 readiness (do not start until approved)
- [ ] `backend/` project skeleton and `pyproject.toml`
- [ ] FastAPI application shell with health / readiness / liveness endpoints
- [ ] Validated settings/config layer with environment separation
- [ ] Structured JSON logging with request and correlation IDs
- [ ] OpenTelemetry SDK bootstrap and context propagation helpers
- [ ] Error taxonomy and central exception handling
- [ ] Ruff + MyPy + Pytest configured and green

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
- [ ] Docker images for API and worker (non-root, minimal base)
- [ ] Docker Compose for local development (API, PostgreSQL, Redis, worker, beat)
- [ ] Alembic setup and migration workflow
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
