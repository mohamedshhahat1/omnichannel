# TODO

Prioritised backlog. **P0** = blocks the next phase or is a launch blocker · **P1** = required before real production traffic · **P2** = valuable, scheduled later.

> Phase 0 (architecture + documentation), Phase 1 (application foundation), Phase 2 (infrastructure foundation), and Phase 3 (identity and access) are complete. Do not start Phase 4 without explicit approval.

> **RBAC correction 2026-08-12 — merged.** Runtime authorization resolves effective permissions from PostgreSQL `role_permissions` rather than from the Python `DEFAULT_ROLE_GRANTS` constant. The branch `fix/rbac-database-authoritative` was squash-merged into `phase-2-infrastructure` on 2026-08-12, after four rounds of CI-surfaced fixes. The operator reports the pipeline green; no automated agent has accessed or may access GitHub Actions results.

> **Phase 3 hardening 2026-08-12 — on `fix/phase-3-hardening`, NOT merged.** A second correction pass closed the credential-ambiguity deviation, the missing `invited → active` transition, per-source login throttling, browser `Origin`/`Referer` validation, breached-password screening, and role revocation. The status boxes below have been corrected to match that code. **Implemented is not verified:** the branch is unmerged, and its CI has not been observed green at the current HEAD — two observed runs each found real defects, which were fixed, and no run has been observed since. Treat every item marked complete on this branch as complete-in-code and pending a green pipeline.

---

## P0 — Blocking

### Verify the Phase 2 foundation in a networked environment
- [x] Run `pytest`, `ruff check .`, `ruff format --check .`, and `mypy` in `backend/` on a machine with network access, and fix any findings — executed in CI; four rounds of findings were fixed on `fix/rbac-database-authoritative` before the merge
- [x] Run opt-in PostgreSQL/Redis integration tests with `OC_TEST_DATABASE_URL` and `OC_TEST_REDIS_URL` — the `pytest-integration` job supplies both against real service containers
- [ ] Regenerate `backend/requirements.lock` with a real resolver, transitive dependencies, and hashes; review the diff

> **Corrected 2026-08-12 (Phase 0–3 audit).** The first two items were previously ticked, then unticked because no authoring environment had network access and the gates had never been executed. They are ticked again now for the opposite reason: the gates *have* been executed, in CI, and the operator reports them green. The distinction the earlier note drew still matters — written is not executed, and executed is not observed-by-an-agent. What is recorded here is the operator's observation. The third item remains genuinely open.

### Verify Phase 3 in CI
- [x] Run and observe GitHub Actions for the Phase 3 commits — done by the operator; CI was the first authoritative run of these gates and it found real defects (see the four fix rounds below)
- [x] Confirm `alembic upgrade head` applies `0002_identity_access` cleanly on a fresh database in the `pytest-integration` job — the job runs it as a separate step before the suite, and the suite depends on its seeded rows
- [ ] **Observe a fully green pipeline for `fix/phase-3-hardening`.** Two runs have been observed against earlier commits on this branch; both found real defects. The most recent observed run left one unit failure (an incorrect assertion in a new test, since fixed) and produced no MyPy, Docker build or Compose output at all, so three of the seven gates have never been observed for this branch's code

### Verify the RBAC correction
- [x] Run and observe CI for `fix/rbac-database-authoritative` — done by the operator over four rounds: MyPy typing plus `204` response models; the login lockout guard (`locked_until IS NULL` must not read as "locked forever") and expired-key staleness; the stale Phase-1 `test_database` metadata assertion, error-envelope parity on the unknown-address path, and invited-member test setup; then activation of the invited membership in the two API-key tests
- [x] Confirm `backend/tests/integration/test_identity_rbac_resolution.py` runs with both `OC_TEST_DATABASE_URL` and `OC_TEST_REDIS_URL` set — both are set in the `pytest-integration` job, so the four caching and tenant-isolation tests do not skip
- [x] Confirm the renamed `EffectivePermissionCache` resolves cleanly everywhere — an unresolved `RoleSlugCache` reference would have been an `ImportError` at collection time, and collection succeeded
- [x] Decide whether to merge the branch into `phase-2-infrastructure` — merged (squash) on 2026-08-12

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
- [x] **RBAC correction (2026-08-12):** runtime authorization resolves effective permissions from `role_permissions` in PostgreSQL; `DEFAULT_ROLE_GRANTS` is now seed/reference/test data only and is not imported by any module on the authorization path — merged, and exercised by CI

### Completed in the Phase 3 hardening pass — `fix/phase-3-hardening`, pending a green pipeline
- [x] **Credential ambiguity refused.** `reject_ambiguous_credentials` (`app/modules/identity/api/dependencies.py`) raises `ambiguous_credentials` (401) when a request carries both a session cookie and a `Bearer` header, before either is parsed. Called from `provide_session_identity` and `provide_principal`, so the bearer branch is reachable only when no cookie was presented and there is no precedence rule left
- [x] **`invited → active` transition.** `ProvisioningService._transition_to_active` is the single writer, reached by `accept_invitation` (`POST /invitations/accept`, the invitee's own membership, no id in the signature) and `activate_membership` (`POST /members/{id}/activate`, requires `members.manage`). `INVITED → ACTIVE` only; already-active is an idempotent no-op; `SUSPENDED` is refused with 409; both call `PermissionResolver.invalidate`
- [x] **Role revocation.** `DELETE /members/{id}/roles/{slug}` → `ProvisioningService.remove_role`, with last-active-owner protection, `billing.manage` required to remove `owner`, and cache invalidation
- [x] **Per-source login throttling.** `app/core/rate_limit.py` (fixed window, hashed bucket, explicit `fail_open`), applied by `AuthenticationService._enforce_source_rate_limit` before the address lookup and before Argon2 runs. Complements the per-account lockout rather than replacing it
- [x] **Browser `Origin`/`Referer` validation.** `app/core/origins.py`, layer 3 of `docs/security.md` 2.4, applied to cookie-authenticated writes only. A different port on the same host is a different origin
- [x] **Breached-password screening.** `app/core/breached_passwords.py`, applied in `ProvisioningService._enforce_password_policy` so registration and invitation share one gate. Local list, no network call, no plaintext egress; production refuses to start with it disabled
- [x] **Safe source-address determination.** `_client_ip` reads no forwarding header until `server.trusted_proxy_hops` says how many to trust, so a caller cannot choose their own rate-limit bucket or forge the audited address
- [x] **Structural guard against Python-derived RBAC.** An AST test walks every module under `app/` and fails if any of them references `DEFAULT_ROLE_GRANTS` or `permissions_for_roles` outside the file that defines them

### Completed alongside Phase 2 — CI/CD foundation ✅

> **Renamed 2026-08-12 (Phase 0–3 audit).** This section was headed "Completed in Phase 14". That was a mislabel: Phase 14 in the `docs/architecture.md` §15 roadmap is *CI/CD & production deployment*, a future phase that has **not** started. The work below is the CI foundation delivered next to Phase 2, which `CURRENT_STATE.md` calls "CI/CD Foundation". No content changed.

- [x] CI workflow (`.github/workflows/ci.yml`) with seven separate required checks on every pull request and every push to **any** branch (`branches: ["**"]`, which matches all branches without also firing on tag pushes): Ruff lint, Ruff format, MyPy, unit Pytest, integration Pytest against real PostgreSQL/Redis service containers, production Docker image build (no push), and `docker compose config` validation — no secrets, least-privilege `contents: read`
- [x] CI guardrail test (`backend/tests/unit/test_ci_workflow.py`) validating workflow paths, Python version, commands, working directories, dependency installation and service configuration, and asserting that the push trigger covers every branch rather than an allowlist
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
- [ ] Cross-tenant isolation test suite (database, Redis, object storage, retrieval, tools) — the database surface is covered for identity in Phase 3; the Redis surface is covered for the RBAC cache by the 2026-08-12 correction; object storage, retrieval and tools are still outstanding
- [ ] **Per-endpoint** rate limiting. The per-source-address half of `docs/security.md` 2.5 is delivered for login on `fix/phase-3-hardening` (`app/core/rate_limit.py`, applied by `AuthenticationService._enforce_source_rate_limit`), and the shared `RateLimiter` protocol is deliberately generic so webhooks, password reset and AI runs can reuse it. What remains is applying it beyond login, and API-key rate limits
- [x] Reject a request that presents both a session cookie and an API key, instead of preferring the bearer key — delivered on `fix/phase-3-hardening` by `reject_ambiguous_credentials`; `docs/security.md` §2.10 still records this as an open deviation and needs the amendment listed in `docs/phase-3-hardening.md` §7
- [x] **Membership acceptance: an `invited → active` transition.** Delivered on `fix/phase-3-hardening`. `ProvisioningService._transition_to_active` is the only writer; `POST /invitations/accept` lets the invitee accept their own membership (identity-based authorisation, no membership id in the signature) and `POST /members/{id}/activate` lets a holder of `members.manage` activate somebody else's. `INVITED → ACTIVE` only, already-active is an idempotent no-op, `SUSPENDED` is refused, a membership in another tenant is *not found* rather than *forbidden*, and `PermissionResolver.invalidate` is called on every successful transition. The two API-key integration tests no longer need to activate the row in raw SQL
- [ ] E-mail delivery for verification and password reset; `email_tokens` is modelled and migrated but has no transport. This is the delivery half of the invitation work — the state change no longer depends on it
- [ ] Secret scanning and dependency vulnerability scanning in CI
- [ ] Log redaction rules and PII minimisation review
- [ ] Upload validation: size, content type, extension, storage location
- [ ] **Role-permission** mutation API. Role *assignment* and *revocation* are delivered (`POST` and `DELETE /members/{id}/roles/...`), both calling `PermissionResolver.invalidate`. What remains is an API to create a role or edit which permissions a role grants. The RBAC correction makes `role_permissions` runtime-authoritative, so such an edit now changes behaviour; `PermissionResolver.invalidate` is the hook it must call, and it must invalidate every membership holding the role rather than one. See `docs/security.md` §3.2

### Infrastructure
- [ ] NGINX configuration with TLS and security headers — note that `server.trusted_proxy_hops` must be set to 1 at the same time, or the per-source login limiter will bucket every request to NGINX and the audit log will record the proxy's address
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
- [x] Migration upgrade path executed in CI — the `pytest-integration` job runs `alembic upgrade head` on a fresh database before the suite
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
- [ ] Custom roles and finer-grained permissions — the storage and resolution path now support this, since a tenant-owned role with its own `role_permissions` rows resolves at runtime with no code change; what is missing is the API to create one
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
| Redis effective-permission cache staleness window | Avoids a role/permission join on every authenticated request. Bounded by `session_cache_ttl_seconds` (default 60 s, maximum 300 s) and invalidated explicitly on role assignment and revocation, so the window only applies to a mutation that bypasses `PermissionResolver.invalidate` | Permission changes must take effect instantly, or a mutation path is added that cannot call the invalidation hook |
| Fixed-window rather than sliding-window login rate limiting | Two Redis commands, no Lua, no cross-process clock skew. A fixed window admits at most one extra burst at a boundary, which is acceptable for a control whose job is to make automation expensive rather than to shape traffic precisely | Boundary bursts are shown to matter, or the limiter is reused for something that needs precise shaping |
| Per-source login limiter fails **open** when Redis is unreachable | The per-account lockout lives in PostgreSQL and is untouched by a Redis outage, so failing open degrades to exactly the protection that existed before the limiter, whereas failing closed turns a cache outage into a total authentication outage. `auth.login_rate_limit_fail_open=false` inverts the trade per deployment | An unthrottled window during a Redis outage is judged worse than an authentication outage |
| ~~Effective permissions resolved from the Python `DEFAULT_ROLE_GRANTS` table rather than by reading `role_permissions` at runtime~~ — **corrected 2026-08-12, no longer debt** | Was accepted on the grounds that the grant matrix is code plus seeded migration data rather than runtime-editable configuration. That reasoning was wrong in one specific way: it made the `role_permissions` rows decorative, so an operator editing a grant in the database would see no change in behaviour and no error. `PermissionResolver` now walks `membership_roles → roles → role_permissions → permissions`, and `DEFAULT_ROLE_GRANTS` seeds those rows without being consulted at runtime | Closed. Custom or tenant-defined roles (P2) are now a data change rather than a code change |
| ~~No `invited → active` membership transition~~ — **corrected 2026-08-12 on `fix/phase-3-hardening`, no longer debt** | The acceptance flow had been deferred with the e-mail transport, which removed the state change as well as the delivery and left an invited member stranded until an operator edited the row. `_transition_to_active` now performs it, reached by `POST /invitations/accept` and `POST /members/{id}/activate`; the deferral is back to what was intended, namely delivery only | Closed for the state change. E-mail delivery remains open (P1) |
| ~~Login throttled per account, not per source address~~ — **corrected 2026-08-12 on `fix/phase-3-hardening`, no longer debt** | Per-account lockout protects the account under attack but is stepped around by rotating victims, which is exactly what credential stuffing does. `app/core/rate_limit.py` adds the per-source half, counted before the address lookup and before Argon2 runs | Closed for login. Applying the same limiter per endpoint remains open (P1) |
| `email_tokens` modelled without a delivery path | The table and lifecycle belong with the identity schema; transport is a separate concern | E-mail verification or password reset becomes a product requirement |
| Plain `text` e-mail column with a lowercase CHECK rather than `citext` | Smaller extension surface; the constraint is explicit and portable | Case-insensitive matching is needed where the CHECK cannot reach |
| No API to create a role or edit a role's permissions | Role *assignment* and *revocation* now exist and invalidate the cache. Grants themselves are still seeded by migration `0002_identity_access` and nothing edits them at runtime, so adding this is wiring rather than redesign — but it is the one RBAC mutation whose invalidation is not a single-membership call, since changing a role affects every membership holding it | Role management is exposed to tenant administrators (P1) |
| `DEFAULT_ROLE_GRANTS` and `permissions_for_roles()` retained in `domain.py` after ceasing to be runtime authority | They remain the source the migration seeds `role_permissions` from and the reference the RBAC tests assert the seeded rows against, so deleting them would delete the specification of what the seed *should* contain. The risk is that a future author mistakes them for live authority; that is contained by a structural AST test that fails if any module under `app/` outside `domain.py` references either name | The seeding path moves into the migration itself, at which point the constant can follow it out of the domain module |
