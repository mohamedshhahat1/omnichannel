# Observability

> **OpenTelemetry** is the tracing and correlation standard (ADR-0010). **Prometheus + Grafana** own metrics. **Sentry** owns error tracking. Logs are structured JSON.
>
> **Phase 2 status:** the repository now implements the OpenTelemetry bootstrap, FastAPI instrumentation, optional SQLAlchemy and Redis instrumentation hooks, request/task correlation propagation, and bounded PostgreSQL/Redis readiness checks. Prometheus, Grafana, Sentry wiring, and alert delivery remain future work.

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

---

## 6. Logging

Structured JSON to stdout; collected by the Docker logging driver.

Mandatory fields on every line: `timestamp`, `level`, `service`, `env`, `version`, `logger`, `message`, `trace_id`, `request_id`, `correlation_id`, and `tenant_id` when known.

**Never logged:** passwords or hashes, session tokens, API key secrets, refresh/reset tokens, provider access tokens, webhook signing secrets, signed URLs, full card/payment data, full document contents, full customer message bodies at INFO level.

Redaction is centralised in the logging configuration (deny-list + pattern matching), applied to Sentry payloads too. Customer message content is logged only at DEBUG in non-production environments.

Levels: `ERROR` needs action · `WARNING` degraded but handled · `INFO` state transitions · `DEBUG` development only.

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

---

## 10. Alerting (initial thresholds — tune after launch)

| Severity | Condition |
|---|---|
| **Page** | API 5xx rate > 5 % for 5 min · readiness failing · PostgreSQL unreachable · `outbox_oldest_pending_age_seconds` > 300 · dead-letter created · backup failed or age > 26 h · disk > 85 % |
| **Ticket** | Queue backlog age > 5 min · webhook signature failure spike · provider error rate > 10 % · AI provider errors > 5 % · delivery failure rate > 5 % · reconciliation drift detected |
| **Inform** | AI cost above the daily budget · tenant approaching a plan limit · slow-query regression · unusual crawler block rate |

Every alert must name an owner and link to a runbook in `operations.md`. Alerts without a runbook get deleted or written up — no silent noise.

---

## 11. Current → Future → Trigger

| Concern | CURRENT | FUTURE | TRIGGER |
|---|---|---|---|
| Trace backend | OTel bootstrap plus local exporter selection | Tempo/Jaeger or managed APM | Trace volume or retention needs |
| Log aggregation | Docker logs + `docker compose logs`/journald | Loki or a managed log platform | Multi-host deployment or search pain |
| Metrics | Self-hosted Prometheus later | Remote-write / managed, longer retention | Retention or HA requirements |
| Profiling | None | Continuous profiling | Unexplained CPU cost |
| SLOs | Informal targets | Error budgets and burn-rate alerts | Paying customers with contractual SLAs |
