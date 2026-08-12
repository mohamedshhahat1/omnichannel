# Observability

> **OpenTelemetry** is the tracing and correlation standard (ADR-0010). **Prometheus + Grafana** own metrics. **Sentry** owns error tracking. Logs are structured JSON.
>
> **Phase 2 status:** the repository now implements the OpenTelemetry bootstrap, FastAPI instrumentation, optional SQLAlchemy and Redis instrumentation hooks, request/task correlation propagation, and bounded PostgreSQL/Redis readiness checks. Prometheus, Grafana, Sentry wiring, and alert delivery remain future work.
>
> **Phase 3 status:** Phase 3 (identity and access) added **no new telemetry infrastructure** — no exporter, no metric, no manual span, no health check, no dashboard, no alert. What it did add is the first *durable* consumer of the correlation set: every row in `audit_logs` persists the ambient `correlation_id` and `request_id`, so an identity action can be joined back to the request that caused it long after the trace has been sampled away. The Phase 3 deltas are marked in §2, §4, §5, §6, §8, §9, §10 and §11.

---

## 1. Why tracing is mandatory here

One customer message crosses: an HTTP webhook → a PostgreSQL transaction → an outbox row → Redis → a Celery task → retrieval → an LLM call → tool calls → an outbound provider request — across **processes and across time**. Metrics can tell us something is slow; only correlated traces can answer *"why did this customer get that answer, and where did the 8 seconds go?"*.

Because the outbox breaks the call stack by design, context propagation must be persisted in the database. This is a deliberate schema decision, not an afterthought.

---

## 2. The correlation set

Every span attribute set and every log line carries as many of these as are known:

| Field | Origin | Lifetime |
|---|---|---|
| `trace_id` / `span_id` | OpenTelemetry (W3C) | Per trace segment |
| `request_id` | Generated per HTTP request; returned as `X-Request-ID` | One request |
| `correlation_id` | **Business chain id** — seeded at ingress (webhook event id or API request id) and carried to the very end | Whole workflow |
| `tenant_id` | Trusted context | Whole workflow |
| `actor_type` / `actor_id` | user · api_key · system · provider · ai | Whole workflow |
| `webhook_event_id` | Ingress record | From ingestion |
| `outbox_event_id` | Outbox row | From publication |
| `celery_task_id` / `task_name` / `queue` | Worker | Per task |
| `conversation_id` / `message_id` | Messaging | Per conversation |
| `ai_run_id` | AI orchestration | Per AI run |
| `tool_call_id` | AI tool invocation | Per tool call |
| `provider` / `provider_request_id` | External adapter | Per provider call |
| `document_id` / `chunk_ids` | Retrieval | Per retrieval |

**`trace_id` vs `correlation_id`:** the trace may be sampled or split into linked segments; `correlation_id` is never sampled away and is what support uses to reconstruct a customer's journey from logs alone.

### 2.1 Phase 3 — `actor_type` / `actor_id` are now real

Until Phase 3 those two rows were a plan. The identity module writes them on every audited action:

| Correlation field | Phase 3 source |
|---|---|
| `actor_type` | `user` or `api_key` from `PrincipalKind`; `system` for unauthenticated flows such as registration. `provider` and `ai` remain reserved for later phases. |
| `actor_id` | Stored as two nullable, `ON DELETE SET NULL` columns — `audit_logs.actor_user_id` and `audit_logs.actor_api_key_id` — rather than one polymorphic id, so the foreign keys stay real. |
| `tenant_id` | `audit_logs.tenant_id`, nullable (registration happens before any tenant exists) and `ON DELETE SET NULL`, so deleting a tenant does not erase the record that it was deleted. |
| `correlation_id` / `request_id` | Read from the ambient request context, not passed in. |

That last row is the important one. `AuditService.record(...)` pulls `correlation_id` and `request_id` from `app/platform/correlation.py` itself rather than accepting them as arguments, so **no call site can forget to pass them and no call site can pass the wrong ones**. Correlation coverage on the audit trail is a property of the service, not of developer discipline.

The actions written in Phase 3 are `auth.login`, `auth.logout`, `auth.logout_all`, `identity.register`, `tenant.create`, `membership.invite`, `membership.assign_role`, `apikey.create` and `apikey.revoke`, each with an `outcome` of `success` or `failure` and a scrubbed JSON `context` blob (§6).

---

## 3. Propagation across every boundary

```
HTTP request
  │  W3C traceparent header in (or a new root span)
  │  request_id generated; correlation_id seeded
  ▼
webhook_events row            (correlation_id persisted)
  │
  ▼
outbox_events row             (traceparent + correlation_id columns — the time-travel carrier)
  │
  ▼
Celery task headers           (traceparent, correlation_id, tenant_id)
  │  new span, linked to the producing span
  ▼
conversation / message spans  (conversation_id, message_id)
  │
  ▼
ai_run span                   (ai_run_id, model, token counts, cost)
  ├─ retrieval span           (top_k, latency, chunk ids)
  ├─ tool_call spans          (tool name, authorisation outcome, duration)
  └─ llm_request span         (provider, model, latency, tokens, finish reason)
  │
  ▼
provider_request span         (provider, endpoint, status, retry_count, provider_request_id)
```

**Span links, not one giant trace.** A webhook received now and processed 200 ms later is fine in one trace; a document embedded 40 minutes later is not. Long-delayed continuations start a new trace **linked** to the originator, and both share the `correlation_id`.

---

## 4. Instrumentation plan

**Auto-instrumented:** FastAPI/ASGI, SQLAlchemy, Redis, Celery (producer and consumer), HTTPX/requests.

**Manually instrumented (with explicit attributes):**

| Span | Key attributes |
|---|---|
| `webhook.ingest` | provider, event_type, signature_valid, tenant resolved |
| `outbox.dispatch` | batch_size, claimed, published, failed |
| `outbox.consume` | event_type, duplicate (bool), attempt |
| `ai.run` | agent, model, mode, input/output tokens, cost_micros, guardrail_outcome, escalated |
| `ai.retrieval` | source_count, top_k, score_min/max, latency |
| `ai.tool_call` | tool, authorised, validation_outcome, duration |
| `channel.send` | channel, attempt_no, idempotency_key present, provider status |
| `knowledge.pipeline` | stage (parse/normalize/chunk/embed), doc size, chunk count |
| `crawler.fetch` | host (hashed), status, bytes, redirects, blocked_reason |
| `billing.webhook` | event_type, duplicate, subscription transition |

**Sampling:** 100 % of errors, AI runs, billing events and webhook ingestion; head-based sampling for routine dashboard traffic; always-on for a request carrying a debug header from an authorised operator.

**Phase 3 note — no manual spans were added.** This is a deliberate gap, recorded rather than hidden. Identity operations are short, single-process and already covered end to end by the auto-instrumented FastAPI and SQLAlchemy layers, so a hand-written `auth.login` span would mostly have duplicated data that already exists. The one identity operation with a genuinely interesting latency profile is Argon2id verification — deliberately expensive CPU work at the production floor of 65 536 KiB and 3 iterations (`docs/security.md` §2) — and it currently appears only as unattributed time inside the `POST /api/v1/auth/login` server span. If login latency ever needs to be split between hashing, database and role cache, an `auth.verify_password` span is the first thing to add.

---

## 5. Metrics (Prometheus)

### Cardinality rule (hard)
**Never** label a metric with `tenant_id`, `user_id`, `conversation_id`, URLs, or any free text. Those belong on spans and logs. Permitted labels: `env`, `service`, `endpoint_template`, `method`, `status_class`, `queue`, `task_name`, `event_type`, `provider`, `channel`, `model`, `plan_tier`, `outcome`. Per-tenant analysis is done in the database/traces, not in Prometheus.

### Core metrics

**API** — request rate, duration histogram, error rate by status class, in-flight requests, auth failures, rate-limit rejections.
**Database** — pool in-use/idle/waiting, checkout wait time, query duration, deadlocks, slow-query count, migration status.
**Queues** — depth per queue, task duration, retries, failures, dead-letters, worker concurrency and saturation.
**Outbox** — pending count, oldest pending age, dispatch duration, dispatch failures, reclaimed leases, dead-letters.
**Webhooks** — received, signature failures, duplicates, unresolved tenant, ingest duration.
**Channels** — sends, failures by reason, 429s, ambiguous outcomes, integration health by channel.
**AI** — runs, latency histogram, tokens in/out, cost, guardrail blocks, escalations, tool-call rate, tool authorisation denials, provider errors.
**Knowledge** — documents processed, pipeline failures by stage, embedding latency, retrieval latency, retrieval empty-result rate.
**Billing** — webhook events by type, processing failures, reconciliation drift count, entitlement denials.
**Crawler** — pages fetched, blocked by validation reason, size/time limit hits, failures.
**System** — CPU, memory, disk, container restarts, backup success/age.

### 5.1 Phase 3 note — "auth failures" is specified but not emitted

The API group above has listed *auth failures* and *rate-limit rejections* since Phase 0. Phase 3 built the authentication that would produce the first of those numbers, but **no Prometheus registry or `/metrics` endpoint exists in the repository yet**, so both remain specification. Do not read the line above as an implemented metric.

When it is implemented, the cardinality rule bites here in a specific and easy-to-get-wrong way: an authentication-failure counter must **not** be labelled with `tenant_id`, `user_id`, or the submitted e-mail address — the last of which would also turn the metrics endpoint into an account-enumeration oracle, defeating the uniform-response design in `docs/security.md` §2. The only appropriate label is `outcome`, with the three reasons `AuthenticationService` already distinguishes internally: `bad_credentials`, `inactive_account`, `locked_out`. Anything finer-grained — which tenant, which account, which address — is a query against `audit_logs`, exactly the split this section already mandates.

---

## 6. Logging

Structured JSON to stdout; collected by the Docker logging driver.

Mandatory fields on every line: `timestamp`, `level`, `service`, `env`, `version`, `logger`, `message`, `trace_id`, `request_id`, `correlation_id`, and `tenant_id` when known.

**Never logged:** passwords or hashes, session tokens, API key secrets, refresh/reset tokens, provider access tokens, webhook signing secrets, signed URLs, full card/payment data, full document contents, full customer message bodies at INFO level.

Redaction is centralised in the logging configuration (deny-list + pattern matching), applied to Sentry payloads too. Customer message content is logged only at DEBUG in non-production environments.

Levels: `ERROR` needs action · `WARNING` degraded but handled · `INFO` state transitions · `DEBUG` development only.

### 6.1 Phase 3 additions

The "never logged" list above already covered passwords, hashes, session tokens and API key secrets. Phase 3 is the first code that actually handles that material, and it enforces the rule in two additional places:

**Audit-context scrubbing.** `app/modules/identity/services/audit.py` passes every audit `context` dictionary through `scrub_context` before it reaches the database. Any key containing `pass`, `secret`, `token`, `credential`, `authorization`, `cookie`, `digest`, `hash`, `apikey` or `api_key` is replaced with `[redacted]`, and every surviving value is truncated to 512 characters. This deliberately duplicates the centralised logging redaction rather than reusing it: an audit row is *durable*, so a leak there is permanent and replicated into every backup, whereas a log line ages out. Two independent deny-lists on two independent paths is the intended design, not an oversight.

**Two-channel errors (ADR-0013).** The error envelope carries a safe `message` for the client and an `internal_message` for operators. Every Phase 3 credential path uses it: a wrong password, an unknown e-mail address, a suspended account and a locked-out account all return exactly the same `authentication_failed` / "Authentication failed." envelope with HTTP 401, while the distinguishing reason travels in `internal_message`, which is logged and would be attached to Sentry but is **never serialised into the HTTP response**. The same rule covers API keys: `parse_api_key` failures, unknown key ids and revoked keys are indistinguishable to the caller.

**What is safe to log in Phase 3:** user ids, tenant ids, membership ids, API key *ids* and `key_id` prefixes, role slugs, permission slugs, and failure reason codes. **What is not, and is never constructed into a log message:** the password, the Argon2 hash, the session token, the CSRF token, the API key plaintext or its secret half, and the e-mail-token values.

---

## 7. Error tracking (Sentry)

Separate projects (or tags) for API, workers and crawler. Every event is tagged with `trace_id`, `correlation_id`, `tenant_id`, `module`, `task_name` and release version. PII scrubbing is enabled and verified. Alert routing distinguishes new-issue, regression and spike conditions. Release tracking ties errors to deploys for fast rollback decisions.

---

## 8. Health endpoints

| Endpoint | Semantics |
|---|---|
| `/health/live` | Process is alive; no dependency checks; never fails on a dependency outage |
| `/health/ready` | Returns 503 before startup; after startup checks PostgreSQL and Redis with bounded per-check timeouts |
| `/metrics` | Prometheus scrape; not publicly exposed |

Workers expose their own liveness (heartbeat freshness) and readiness (broker reachable). The readiness payload exposes only safe dependency status (`pass`, `fail`, `timeout`, or exception type), never raw exception messages, DSNs, or credentials.

**Phase 3 note.** No identity check was registered with the `HealthRegistry` and the readiness payload shape is unchanged. This is deliberate: an identity probe would either be trivial (the existing PostgreSQL check under a different name) or actively dangerous (touching credential tables from an unauthenticated endpoint, and giving an unauthenticated caller a timing signal about the identity schema). Nothing in `/health/live`, `/health/ready` or their payloads exposes user, session, membership or key material, and Phase 3 did not change that.

---

## 9. Dashboards (Grafana)

1. **Platform overview** — traffic, error rate, latency, saturation, deploy markers.
2. **Reliability backbone** — outbox depth/age, queue backlogs, retries, dead-letters, reclaim rate.
3. **Channels** — inbound volume, signature failures, delivery success, provider errors, integration health.
4. **AI** — runs, latency, escalation rate, guardrail blocks, tokens and cost trend, tool-call outcomes.
5. **Knowledge** — ingestion throughput, pipeline failures, retrieval latency and empty-result rate.
6. **Billing** — webhook health, subscription transitions, entitlement denials, reconciliation drift.
7. **Database** — pool pressure, slow queries, table growth, replication/backup age.
8. **Business** — active tenants, conversations, resolution/handoff rate, usage against plan limits.

**Phase 3 note.** No dashboard was added, and no Grafana instance exists yet. A future **Identity and access** dashboard (sign-in success rate, lockouts in effect, active session count, API keys issued and revoked, permission denials) would be built primarily from `audit_logs` and the `sessions` table rather than from Prometheus — for the cardinality reason in §5, per-tenant identity questions belong in SQL.

---

## 10. Alerting (initial thresholds — tune after launch)

| Severity | Condition |
|---|---|
| **Page** | API 5xx rate > 5 % for 5 min · readiness failing · PostgreSQL unreachable · `outbox_oldest_pending_age_seconds` > 300 · dead-letter created · backup failed or age > 26 h · disk > 85 % |
| **Ticket** | Queue backlog age > 5 min · webhook signature failure spike · provider error rate > 10 % · AI provider errors > 5 % · delivery failure rate > 5 % · reconciliation drift detected |
| **Inform** | AI cost above the daily budget · tenant approaching a plan limit · slow-query regression · unusual crawler block rate |

Every alert must name an owner and link to a runbook in `operations.md`. Alerts without a runbook get deleted or written up — no silent noise.

**Phase 3 note — the identity alerts that do *not* exist.** Two signals become obviously desirable the moment real accounts exist, and neither is implemented:

| Missing alert | Why it matters | Why it is missing |
|---|---|---|
| **Failed-login spike** | Credential stuffing is risk 13 in `docs/architecture.md` §13, and there is no per-IP or per-account rate limiting yet (`docs/security.md` §2.10) — per-account lockout after 10 failures is the *only* control | Requires the §5 auth-failure metric and an alerting pipeline, neither of which exists |
| **Lockout rate across many accounts** | Distinguishes one forgetful user from an attack sweeping an account list | Same |

They are recorded here so the gap is visible rather than assumed handled. `audit_logs` already stores the raw material — `action = 'auth.login'` with `outcome = 'failure'`, plus the failure reason in the scrubbed context — so both are a query away from being a dashboard and an alerting pipeline away from being useful. Until then, detection is manual and after the fact.

---

## 11. Current → Future → Trigger

| Concern | CURRENT | FUTURE | TRIGGER |
|---|---|---|---|
| Trace backend | OTel bootstrap plus local exporter selection | Tempo/Jaeger or managed APM | Trace volume or retention needs |
| Log aggregation | Docker logs + `docker compose logs`/journald | Loki or a managed log platform | Multi-host deployment or search pain |
| Metrics | Self-hosted Prometheus later | Remote-write / managed, longer retention | Retention or HA requirements |
| Profiling | None | Continuous profiling | Unexplained CPU cost |
| SLOs | Informal targets | Error budgets and burn-rate alerts | Paying customers with contractual SLAs |
| Identity telemetry | Auth outcomes are visible only by querying `audit_logs` | Auth-failure metrics (§5.1), an identity dashboard (§9), failed-login and lockout alerting (§10) | Production traffic, or the first credential-stuffing attempt |
| Audit log retention | `audit_logs` grows without bound; no retention, archival or partitioning job exists | Time-based partitioning or an archival sweep with a documented retention window | Table size, query latency on the audit path, or a compliance requirement that fixes the window |
| Login latency attribution | Argon2id cost is unattributed time inside the login server span (§4) | An `auth.verify_password` manual span | Login p95 becomes a complaint or a capacity question |
