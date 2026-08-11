# Operations

> Backup and disaster recovery, runbooks, incident response and routine maintenance.

---

## 1. Backup and disaster recovery

### 1.1 Targets

| Objective | Launch target | Future target |
|---|---|---|
| **RPO** (max acceptable data loss) | **< 1 hour** | < 5 minutes |
| **RTO** (max acceptable downtime) | **< 4 hours** | < 1 hour |

These are commitments, not aspirations: the launch checklist is not complete until a timed restore rehearsal has demonstrated both.

### 1.2 Launch requirements (mandatory before production traffic)

| # | Requirement | Detail |
|---|---|---|
| 1 | **Encrypted off-server backups** | Encrypted at rest **before** leaving the server; stored with a different provider or at least a different region/account from the database host. Losing the server must never mean losing the backups. |
| 2 | **Daily full logical backup** | `pg_dump` custom format, automated, timestamped, checksummed, with success/failure reported to monitoring. |
| 3 | **WAL archiving + PITR** | Continuous WAL shipping to object storage with `archive_timeout` of 5–15 minutes, which is what actually delivers RPO < 1 hour. Tooling: pgBackRest or WAL-G. |
| 4 | **Retention** | 7 daily · 4 weekly · 6 monthly, with a documented deletion policy and object-storage lifecycle rules. |
| 5 | **Restore rehearsal** | At least one full, timed restore into a clean environment **before launch**, then a monthly automated restore verification with row-count and checksum validation. An untested backup is not a backup. |
| 6 | **Documented runbook** | Step-by-step restore procedure (§1.5), including PITR to a specific timestamp, with expected durations per step. |
| 7 | **Monitoring** | Alert on backup failure, on backup age > 26 hours, on WAL archiving lag, and on restore-verification failure. |
| 8 | **Object storage durability** | Versioning enabled, lifecycle rules configured, deletion protection on the backup bucket. |
| 9 | **Secrets and configuration backup** | Encrypted offline copy of environment configuration, signing keys and provider credentials, stored separately from the database backups. |
| 10 | **Redis: no backup required** | Deliberate. Redis holds only the broker, caches, rate-limit counters and short locks. |

### 1.3 Why Redis needs no backup — an outbox dividend

If Redis is lost entirely: queued Celery tasks disappear, caches go cold, rate-limit counters reset. **No business work is lost**, because durable intent lives in `outbox_events` in PostgreSQL. Rows in `pending` are dispatched on the next poll; rows stuck in `dispatching` are reclaimed when their lease expires. Recovery is: restart Redis, restart workers, watch the backlog drain.

This is one of the strongest practical arguments for the transactional outbox (ADR-0003) and it directly simplifies the DR plan.

### 1.4 What must be recovered

| Asset | Mechanism | Notes |
|---|---|---|
| PostgreSQL data | Daily dump + WAL/PITR | The critical path |
| Object storage | Provider durability + versioning | Cross-account/region copy is a future improvement |
| Secrets and configuration | Encrypted offline copy | Required to bring the app up at all |
| Container images | Registry, tagged by commit SHA | Rebuildable from source if lost |
| Infrastructure definition | Git repository | Compose files, NGINX config, scripts |
| Redis | Not backed up | See §1.3 |

### 1.5 Restore runbook (target < 4 hours)

1. **Declare** the incident, note the target recovery point, notify stakeholders.
2. **Provision** a clean host (or use the standby) — target 30 min.
3. **Restore secrets/configuration** from the encrypted offline copy — 10 min.
4. **Restore PostgreSQL:** base backup, then replay WAL to the chosen timestamp — 60–120 min depending on size.
5. **Verify data:** row counts on key tables, latest message and event timestamps, subscription state spot-checks, integrity checks — 20 min.
6. **Start the application:** run migrations if needed, start API and workers, confirm `/readyz` — 15 min.
7. **Verify the backbone:** outbox drains, webhooks are accepted, a test message flows end to end — 20 min.
8. **Restore traffic:** DNS/TLS/upstream cut-over — 15 min.
9. **Reconcile:** replay provider events for the gap where possible; run the billing reconciliation sweep; identify conversations affected by the recovery-point gap.
10. **Post-incident review** with corrective actions recorded in `TODO.md`.

### 1.6 Future improvements (post-launch)

| Improvement | Benefit | Trigger |
|---|---|---|
| Managed PostgreSQL with automated PITR | Removes most backup operations from us | Ops burden or reliability requirements |
| Streaming standby replica | RTO in minutes | Downtime cost exceeds infrastructure cost |
| Automated failover | Removes manual promotion | Documented availability commitments |
| Cross-region backup copies | Survives a region failure | Enterprise or compliance requirements |
| Cross-region object replication | Media survives a region failure | Same |
| Quarterly DR game days | Proves RTO under realistic conditions | Paying customers with contractual SLAs |
| Per-tenant export/restore | Recover one tenant without a full restore | Support load |
| Target RPO < 5 min / RTO < 1 h | Higher availability tier | Enterprise contracts |

---

## 2. Runbooks

Every alert links to one of these. An alert without a runbook is deleted or written up.

### 2.1 Outbox backlog growing
**Symptoms:** `outbox_pending_count` climbing, `outbox_oldest_pending_age_seconds` > 300.
**Check:** is beat running? is the dispatcher erroring? is Redis reachable? are workers alive? are rows stuck in `dispatching` with expired leases? is one `event_type` dominating?
**Actions:** restart beat/workers as needed; run the reclaim sweep; scale worker concurrency; if a single poison event type is blocking, isolate it to dead-letter and replay after the fix.
**Never:** delete outbox rows to clear the alert.

### 2.2 Dead-letter events
**Check:** `last_error`, the `event_type`, whether the failure is systemic or per-tenant.
**Actions:** fix the cause → reset the affected rows to `pending` with `attempts = 0` via the replay tool → confirm consumers are idempotent → verify effects were applied exactly once → record the incident.

### 2.3 Redis down or lost
**Actions:** restart Redis; restart workers; confirm the outbox drains; expect a cold cache and reset rate limits. Verify no duplicate customer messages were sent (delivery attempt records and provider idempotency keys protect this).

### 2.4 PostgreSQL unavailable
**Actions:** confirm the container/host state, disk space, and connection saturation; the API should be failing readiness and NGINX returning a maintenance response; if the data is intact, restart and verify; if not, execute §1.5. Communicate status to tenants.

### 2.5 Provider (Meta) outage
**Actions:** confirm via the provider status page; verify the circuit breaker paused sending; inbound events keep persisting durably; do not manually retry in bulk; when the provider recovers, allow throttled drain and watch for rate limiting.

### 2.6 AI provider outage or degradation
**Actions:** confirm error rate and latency; conversations should escalate to humans rather than hang; notify affected tenants if handoff volume spikes; consider temporarily disabling AI auto-reply per tenant; resume when healthy.

### 2.7 Webhook signature failure spike
**Actions:** distinguish a rotated/misconfigured secret from an attack; check whether one integration or all are affected; rotate and update the secret if needed; block abusive sources at the edge; verify no unverified event was processed.

### 2.8 Suspected cross-tenant data exposure
**Treat as a security incident.** Preserve evidence; identify scope via audit logs and `correlation_id`; contain (disable the endpoint/feature, revoke sessions/keys); notify per policy; write a regression test that reproduces it before shipping the fix.

### 2.9 Disk pressure
**Check:** database growth, WAL accumulation (is archiving failing?), logs, crawler artifacts, orphaned media.
**Actions:** fix WAL archiving first, rotate logs, run retention jobs, clean crawler temp storage, then expand the volume.

### 2.10 Secret or key rotation
Generate the new secret → support both old and new during the overlap where the provider allows → update configuration → redeploy → verify → revoke the old → audit-log the rotation. Database credentials, provider tokens, signing keys and API keys each have a documented cadence.

---

## 3. Routine maintenance

| Cadence | Task |
|---|---|
| Daily | Backup success and age · error-rate review · queue and outbox health · dead-letter count |
| Weekly | Slow-query review · disk and table growth · dependency alerts · open incidents |
| Monthly | **Automated restore verification** · security patching · index and cost review · alert-threshold tuning |
| Quarterly | Access review (users, API keys, roles) · key rotation · DR rehearsal · capacity review · documentation audit |

---

## 4. Incident response

**Severity:** SEV1 platform down or data exposure · SEV2 major feature broken or degraded for many tenants · SEV3 single-tenant or minor impairment · SEV4 cosmetic.

**Process:** detect → declare and assign an incident lead → communicate → mitigate before diagnosing → resolve → verify → blameless post-mortem within five working days.

**Post-mortems** capture timeline, impact (tenants, conversations, revenue), root cause, contributing factors, what worked, what did not, and concrete corrective actions with owners — which land in `TODO.md`.

**Every incident answers:** was any data lost? was any customer message dropped or duplicated? was any tenant boundary crossed? did an alert fire, and was it actionable?

---

## 5. Operational health checklist (weekly)

- [ ] Backups succeeded every day; latest is < 26 hours old
- [ ] WAL archiving current, no lag
- [ ] Outbox pending count and oldest age within thresholds
- [ ] No unresolved dead-letter events
- [ ] Queue backlogs within thresholds
- [ ] No unresolved Sentry regressions
- [ ] Certificates valid for > 30 days
- [ ] Disk usage below 70 %
- [ ] Integration health: no tenant stuck in `degraded`
- [ ] AI cost within budget
- [ ] No unexpected entitlement denials
