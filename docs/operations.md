# Operations

> Backup and disaster recovery, runbooks, incident response and routine maintenance.
>
> **Phase 2 status:** the repository now includes the local PostgreSQL/Redis/Celery topology, least-privileged local PostgreSQL roles, readiness checks, and migration bootstrap. Production backup automation, WAL archiving, monitoring-backed alerts, and restore rehearsal remain future operational work.
>
> **Phase 3 status:** Phase 3 (identity and access) added **no new operational infrastructure** — no scheduled job, no daemon, no new service, no new deployment secret. What it added is a set of operator procedures that previously had nothing to act on: account lockout and manual unlock, forced sign-out via the session epoch, API-key revocation and rotation, and an access review that can now actually be performed. Those are §2.11–§2.14. It also introduced two acknowledged gaps — there is no session-reaper job and no audit-log retention job (§3) — and one hard limitation: there is no e-mail transport, so password reset and e-mail verification cannot be driven end to end (§2.14).
>
> **RBAC correction 2026-08-12.** Runtime authorization now reads `role_permissions` from PostgreSQL instead of expanding role slugs through a Python constant. This adds no infrastructure either, but it changes two operational facts: editing a grant in the database now changes behaviour, and the Redis entry described in §1.3 and §2.12 holds effective permissions rather than role slugs. The runbook advice in §2.12 is unchanged — it was already correct.

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

**Phase 3 note on item 9 — identity added no new secret to back up.** Sessions use opaque random tokens stored as SHA-256 digests in PostgreSQL rather than signed tokens (ADR-0015), so **there is no session signing key** to hold, protect, rotate or lose. The DR consequence is a good one: restoring the database restores working sessions, and there is no external key material without which the `sessions` or `api_keys` tables become unreadable. The Argon2 parameters (`OC_AUTH__ARGON2_*`) are configuration rather than secrets — each stored hash already encodes the parameters it was created with — so a restore under different settings still verifies existing passwords correctly and simply re-hashes them on next login.

### 1.3 Why Redis needs no backup — an outbox dividend

If Redis is lost entirely: queued Celery tasks disappear, caches go cold, rate-limit counters reset. **No business work is lost**, because durable intent lives in `outbox_events` in PostgreSQL. Rows in `pending` are dispatched on the next poll; rows stuck in `dispatching` are reclaimed when their lease expires. Recovery is: restart Redis, restart workers, watch the backlog drain.

This is one of the strongest practical arguments for the transactional outbox (ADR-0003) and it directly simplifies the DR plan.

**Phase 3 note, corrected 2026-08-12.** Losing Redis does not sign anyone out, and it does not deny anyone access. Sessions and API keys are authoritative in PostgreSQL and are **not cached at all** — every authenticated request reads the row. Redis holds only a short-lived read cache of a membership's **effective permission set** (`OC_AUTH__SESSION_CACHE_TTL_SECONDS`, default 60 s — the setting is named for the auth path it sits on, not because sessions are cached). With Redis gone, identity degrades to PostgreSQL-only: one extra join per authenticated request, and the staleness window in §2.12 disappears entirely. Nothing fails closed incorrectly, because a cache miss is answered from `role_permissions` rather than from an empty set.

### 1.4 What must be recovered

| Asset | Mechanism | Notes |
|---|---|---|
| PostgreSQL data | Daily dump + WAL/PITR | The critical path |
| Object storage | Provider durability + versioning | Cross-account/region copy is a future improvement |
| Secrets and configuration | Encrypted offline copy | Required to bring the app up at all |
| Container images | Registry, tagged by commit SHA | Rebuildable from source if lost |
| Infrastructure definition | Git repository | Compose files, NGINX config, scripts |
| Redis | Not backed up | See §1.3 |

**Phase 3 note.** All identity data — users, tenants, memberships, roles, permissions, sessions, API keys, e-mail tokens and audit logs — lives entirely in PostgreSQL and is therefore covered by the first row. There is no separate identity store, no external identity provider, and no credential material held outside the database, so identity adds no new row to this table. The 2026-08-12 RBAC correction reinforces this rather than changing it: the authorization decision itself is now recoverable from the same backup as everything else, because `role_permissions` is where it lives.

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

**Phase 3 addition to step 5 (verify data).** Add these to the spot-check list — a restore that silently lost the seeded reference data will authenticate users successfully and then deny every single one of them, which is a confusing failure to diagnose under pressure. Since the 2026-08-12 RBAC correction the last of these queries is the one that matters most: `role_permissions` is what authorises requests, so a restore with roles but no grants produces exactly that symptom.

```sql
SELECT count(*) FROM users;
SELECT count(*) FROM tenants WHERE deleted_at IS NULL;
SELECT count(*) FROM memberships WHERE status = 'active';
SELECT count(*) FROM permissions;                 -- expect 15
SELECT count(*) FROM roles WHERE tenant_id IS NULL; -- expect 6 system roles
SELECT count(*) FROM role_permissions;            -- grants must be present, not just the roles
```

**Phase 3 addition to step 6 (run migrations).** `alembic upgrade head` now applies `0002_identity_access` as well as `0001_initial_infra`, and `0002` seeds the reference data above. Restoring from a dump means that data is already present and the migration is a no-op. Rebuilding from migrations into an *empty* database recreates only the reference data — permissions, system roles and grants — and **not** tenants, users, memberships, sessions or API keys, which exist only in the dump. Migrations are never run automatically from application startup (ADR-0004); this step is deliberate and manual.

**Phase 3 note on the recovery-point gap (step 9).** Sessions created after the recovery point are gone, so the affected users are simply signed out and sign in again — no action needed. API keys issued after the recovery point are also gone, and those *do* need action: the plaintext was shown once at creation and cannot be recovered, so affected tenants must be told to issue new keys. Include "API keys created after the recovery point" in the reconciliation list alongside conversations. Add role assignments made after the recovery point to the same list: they are authorisation state, they are lost with everything else, and unlike a session nobody will notice by being logged out — they will notice by being denied.

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
**Actions:** restart Redis; restart workers; confirm the outbox drains; expect a cold cache and reset rate limits. Verify no duplicate customer messages were sent (delivery attempt records and provider idempotency keys protect this). Authentication and authorisation continue working throughout — see §1.3.

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

**Phase 3 — how to actually contain and investigate.** "Revoke sessions/keys" is now a concrete operation: §2.12 for sessions, §2.13 for API keys. `audit_logs` is the evidence source and stores `correlation_id`, `request_id`, `tenant_id`, actor type, actor id, action and outcome for every identity action, so scope can be reconstructed from the correlation id outward. Preserve it — there is no retention job (§3), so nothing will quietly delete it during an investigation. The regression test belongs in `backend/tests/integration/test_identity_persistence.py`, which already contains the cross-tenant denial cases to copy from, or in `test_identity_rbac_resolution.py` if the exposure involves permissions rather than rows.

### 2.9 Disk pressure
**Check:** database growth, WAL accumulation (is archiving failing?), logs, crawler artifacts, orphaned media.
**Actions:** fix WAL archiving first, rotate logs, run retention jobs, clean crawler temp storage, then expand the volume.

**Phase 3 note.** Add `sessions` and `audit_logs` to the growth check. Neither has a cleanup job (§3), so on a busy system they are plausible contributors long before anything else identity-related is.

### 2.10 Secret or key rotation
Generate the new secret → support both old and new during the overlap where the provider allows → update configuration → redeploy → verify → revoke the old → audit-log the rotation. Database credentials, provider tokens, signing keys and API keys each have a documented cadence.

**Phase 3 note — identity credential rotation needs no redeploy.** Rotating anything Phase 3 owns is a **database operation**: no restart, no configuration change, no deployment. That is a direct consequence of holding no key material — passwords are Argon2id hashes, and session tokens and API keys are opaque random values stored as SHA-256 digests, so there is nothing in the environment to swap. The "support both old and new during the overlap" step does not translate to API keys either: a tenant issues the new key, deploys it, confirms it is being used, then revokes the old one, and the overlap is simply the window in which both are live. The rotation is audit-logged automatically (`apikey.create`, `apikey.revoke`) rather than by hand.

### 2.11 Account locked out
**Symptoms:** a user reports repeated "Authentication failed." while insisting the password is correct.

**Background:** after `OC_AUTH__MAX_FAILED_LOGINS` cons