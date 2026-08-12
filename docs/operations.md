# Operations

> Backup and disaster recovery, runbooks, incident response and routine maintenance.
>
> **Phase 2 status:** the repository now includes the local PostgreSQL/Redis/Celery topology, least-privileged local PostgreSQL roles, readiness checks, and migration bootstrap. Production backup automation, WAL archiving, monitoring-backed alerts, and restore rehearsal remain future operational work.
>
> **Phase 3 status:** Phase 3 (identity and access) added **no new operational infrastructure** — no scheduled job, no daemon, no new service, no new deployment secret. What it added is a set of operator procedures that previously had nothing to act on: account lockout and manual unlock, forced sign-out via the session epoch, API-key revocation and rotation, and an access review that can now actually be performed. Those are §2.11–§2.14. It also introduced two acknowledged gaps — there is no session-reaper job and no audit-log retention job (§3) — and one hard limitation: there is no e-mail transport, so password reset and e-mail verification cannot be driven end to end (§2.14).

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

**Phase 3 note.** Losing Redis does not sign anyone out. Sessions and API keys are authoritative in PostgreSQL; Redis holds only a short-lived read cache of session and permission lookups (`OC_AUTH__SESSION_CACHE_TTL_SECONDS`, default 60 s). With Redis gone, identity degrades to PostgreSQL-only — slightly more database load per request, and the role-cache staleness window in §2.12 disappears entirely.

### 1.4 What must be recovered

| Asset | Mechanism | Notes |
|---|---|---|
| PostgreSQL data | Daily dump + WAL/PITR | The critical path |
| Object storage | Provider durability + versioning | Cross-account/region copy is a future improvement |
| Secrets and configuration | Encrypted offline copy | Required to bring the app up at all |
| Container images | Registry, tagged by commit SHA | Rebuildable from source if lost |
| Infrastructure definition | Git repository | Compose files, NGINX config, scripts |
| Redis | Not backed up | See §1.3 |

**Phase 3 note.** All identity data — users, tenants, memberships, roles, permissions, sessions, API keys, e-mail tokens and audit logs — lives entirely in PostgreSQL and is therefore covered by the first row. There is no separate identity store, no external identity provider, and no credential material held outside the database, so identity adds no new row to this table.

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

**Phase 3 addition to step 5 (verify data).** Add these to the spot-check list — a restore that silently lost the seeded reference data will authenticate users successfully and then deny every single one of them, which is a confusing failure to diagnose under pressure:

```sql
SELECT count(*) FROM users;
SELECT count(*) FROM tenants WHERE deleted_at IS NULL;
SELECT count(*) FROM memberships WHERE status = 'active';
SELECT count(*) FROM permissions;                 -- expect 15
SELECT count(*) FROM roles WHERE tenant_id IS NULL; -- expect 6 system roles
SELECT count(*) FROM role_permissions;            -- grants must be present, not just the roles
```

**Phase 3 addition to step 6 (run migrations).** `alembic upgrade head` now applies `0002_identity_access` as well as `0001_initial_infra`, and `0002` seeds the reference data above. Restoring from a dump means that data is already present and the migration is a no-op. Rebuilding from migrations into an *empty* database recreates only the reference data — permissions, system roles and grants — and **not** tenants, users, memberships, sessions or API keys, which exist only in the dump. Migrations are never run automatically from application startup (ADR-0004); this step is deliberate and manual.

**Phase 3 note on the recovery-point gap (step 9).** Sessions created after the recovery point are gone, so the affected users are simply signed out and sign in again — no action needed. API keys issued after the recovery point are also gone, and those *do* need action: the plaintext was shown once at creation and cannot be recovered, so affected tenants must be told to issue new keys. Include "API keys created after the recovery point" in the reconciliation list alongside conversations.

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

**Phase 3 — how to actually contain and investigate.** "Revoke sessions/keys" is now a concrete operation: §2.12 for sessions, §2.13 for API keys. `audit_logs` is the evidence source and stores `correlation_id`, `request_id`, `tenant_id`, actor type, actor id, action and outcome for every identity action, so scope can be reconstructed from the correlation id outward. Preserve it — there is no retention job (§3), so nothing will quietly delete it during an investigation. The regression test belongs in `backend/tests/integration/test_identity_persistence.py`, which already contains the cross-tenant denial cases to copy from.

### 2.9 Disk pressure
**Check:** database growth, WAL accumulation (is archiving failing?), logs, crawler artifacts, orphaned media.
**Actions:** fix WAL archiving first, rotate logs, run retention jobs, clean crawler temp storage, then expand the volume.

**Phase 3 note.** Add `sessions` and `audit_logs` to the growth check. Neither has a cleanup job (§3), so on a busy system they are plausible contributors long before anything else identity-related is.

### 2.10 Secret or key rotation
Generate the new secret → support both old and new during the overlap where the provider allows → update configuration → redeploy → verify → revoke the old → audit-log the rotation. Database credentials, provider tokens, signing keys and API keys each have a documented cadence.

**Phase 3 note — identity credential rotation needs no redeploy.** Rotating anything Phase 3 owns is a **database operation**: no restart, no configuration change, no deployment. That is a direct consequence of holding no key material — passwords are Argon2id hashes, and session tokens and API keys are opaque random values stored as SHA-256 digests, so there is nothing in the environment to swap. The "support both old and new during the overlap" step does not translate to API keys either: a tenant issues the new key, deploys it, confirms it is being used, then revokes the old one, and the overlap is simply the window in which both are live. The rotation is audit-logged automatically (`apikey.create`, `apikey.revoke`) rather than by hand.

### 2.11 Account locked out
**Symptoms:** a user reports repeated "Authentication failed." while insisting the password is correct.

**Background:** after `OC_AUTH__MAX_FAILED_LOGINS` consecutive failures (default 10) the account is locked for `OC_AUTH__LOCKOUT_SECONDS` (default 900 s). A locked account, a suspended account, an unknown e-mail address and a wrong password all return the *same* 401 `authentication_failed` envelope — deliberately, to prevent account enumeration (`docs/security.md` §2). So neither the user nor the support agent can tell these apart without looking at the database.

**Check:**

```sql
SELECT id, status, failed_logins, locked_until, last_login_at
FROM users WHERE lower(email) = lower(:email);

SELECT action, outcome, created_at, context
FROM audit_logs
WHERE actor_user_id = :user_id AND action = 'auth.login'
ORDER BY created_at DESC LIMIT 20;
```

The distinguishing reason (`locked_out`, `bad_credentials`, `inactive_account`) is recorded in the audit context and in the log line's `internal_message`; it is never in the HTTP response.

**Actions:** the lock expires by itself, and a successful login resets `failed_logins` to 0 and clears `locked_until`. To unlock immediately:

```sql
UPDATE users SET failed_logins = 0, locked_until = NULL WHERE id = :user_id;
```

**Decide first whether this is one forgetful user or an attack.** If many accounts are locked at once, or the failures cluster by source address, treat it as credential stuffing and do **not** mass-unlock: there is no per-IP or per-account rate limiting yet (`docs/security.md` §2.10), so lockout is currently the only control standing between an attacker and unlimited guesses. There is also no alert for a failed-login spike (`docs/observability.md` §10) — detection is manual today.

**Never:** raise `OC_AUTH__MAX_FAILED_LOGINS` or lower `OC_AUTH__LOCKOUT_SECONDS` to make a complaint go away.

### 2.12 Forced sign-out ("sign out everywhere")

**One session** — the user signs out normally (`POST /api/v1/auth/logout`), or an operator revokes the row:

```sql
UPDATE sessions SET revoked_at = now() WHERE id = :session_id;
```

**Every session for a user** — the product path is `POST /api/v1/auth/logout-all`, which revokes the rows **and** increments `users.session_epoch`. The operator equivalent, in this order:

```sql
UPDATE sessions SET revoked_at = now() WHERE user_id = :user_id AND revoked_at IS NULL;
UPDATE users SET session_epoch = session_epoch + 1 WHERE id = :user_id;
```

**Why the epoch bump is not optional.** Session lookups are cached in Redis for up to `OC_AUTH__SESSION_CACHE_TTL_SECONDS` (default 60 s, max 300 s). Revoking the row alone could therefore leave a cached session usable for up to that long — unacceptable for a compromised account. Every authenticated request re-checks the session's epoch against the user's current `session_epoch`, so bumping it invalidates every existing session **immediately**, regardless of cache state. Do both statements; the `UPDATE users` is the one that actually guarantees the cut-off.

**Role and permission changes behave differently — know the difference.** Resolved permission sets are cached for the same TTL and are **not** covered by the epoch, so removing a role or downgrading a membership can take up to `session_cache_ttl_seconds` (default 60 s) to take effect. This is a bounded staleness window, not a revocation hole: if access must stop *now*, revoke the session and bump the epoch rather than waiting for the permission cache to expire. If Redis is unavailable the cache degrades to PostgreSQL-only and the window disappears entirely.

**Suspending rather than signing out:** setting `users.status` to `suspended`, or a membership's `status` to `suspended`, blocks future authentication and authorisation but does not itself kill live sessions. Pair it with the two statements above.

### 2.13 API key compromise, revocation and rotation

**Format:** `oc_{env}_{key_id}_{secret}`. Only `key_id` is stored in the clear; the secret half is stored as a SHA-256 digest, and the plaintext is returned exactly once at creation and never again by any endpoint.

**Identify.** If a key has leaked into a log, a repository, a screenshot or a support ticket, the `key_id` segment alone is enough to find it — which is the point of the format:

```sql
SELECT id, key_id, name, tenant_id, created_by_id, scopes,
       last_used_at, expires_at, revoked_at
FROM api_keys WHERE key_id = :key_id;
```

**Revoke.** `DELETE /api/v1/api-keys/{id}` (requires `apikeys.manage`), or:

```sql
UPDATE api_keys SET revoked_at = now() WHERE id = :api_key_id;
```

Revocation takes effect immediately: API-key authentication reads the database on every request and is not cached, so there is no staleness window of the kind sessions have.

**Rotate with zero downtime.** Issue the new key → deploy it to the consumer → confirm `last_used_at` is advancing on the new key and static on the old → revoke the old. There is deliberately **no update path**: a key is never edited in place, because rotating in place would mean handling plaintext twice.

**Expiry.** `OC_AUTH__API_KEY_MAX_TTL_DAYS` (default 365) caps the requested lifetime. An expired key fails authentication with exactly the same generic envelope as an unknown or revoked key.

**Scopes.** A key can never be granted more than the issuing member holds; an attempted escalation is refused with 403 and audit-logged. When investigating what a leaked key could reach, read `scopes` on the row — not the issuer's current permissions, which may have changed since.

**Never** ask a tenant to send you a key so you can "check it". Ask for the `key_id` segment. A key that has been pasted into a support channel is compromised by definition and must be rotated, not verified.

### 2.14 Password reset or e-mail verification requested

**There is no e-mail transport in the system.** Phase 3 created the `email_tokens` table, the `EmailTokenPurpose` values (`email_verification`, `password_reset`) and their TTL settings (`OC_AUTH__EMAIL_VERIFICATION_TTL_SECONDS`, default 24 h; `OC_AUTH__CREDENTIAL_RESET_TTL_SECONDS`, default 30 min) — but **no endpoint issues or redeems those tokens, and nothing can deliver a message**. Neither flow can currently be driven end to end. This is a known limitation recorded in `TODO.md`, not a misconfiguration to hunt for.

**Consequence for support:** a user who has forgotten their password cannot self-serve, and an unverified address cannot be verified. Until a transport and the corresponding endpoints exist, the only recovery path is an operator setting a new Argon2id hash using **the application's own hashing service and configuration** — never an ad-hoc script with different parameters, never a hash pasted from elsewhere, and never a plaintext password written into a ticket, a shell history or a script file. Follow it with §2.12 to sign out any existing sessions.

**Do not** work around this by suspending authentication for the user, by sharing a session cookie, or by issuing an API key as a stand-in for a login. API keys are tenant-scoped machine credentials with their own scope rules and their own audit trail; using one as a personal sign-in destroys attribution in `audit_logs`, which is the thing you will want most if the account turns out to have been compromised.

---

## 3. Routine maintenance

| Cadence | Task |
|---|---|
| Daily | Backup success and age · error-rate review · queue and outbox health · dead-letter count |
| Weekly | Slow-query review · disk and table growth · dependency alerts · open incidents |
| Monthly | **Automated restore verification** · security patching · index and cost review · alert-threshold tuning |
| Quarterly | Access review (users, API keys, roles) · key rotation · DR rehearsal · capacity review · documentation audit |

**Phase 2 local-only additions:** verify the migration container still upgrades cleanly, the Compose services remain healthy, and the offline-authored `requirements.lock` has not been mistaken for a production-grade resolved lock.

**Phase 3 additions.**

**The quarterly access review is now executable** rather than aspirational — before Phase 3 there were no users, keys or roles to review. The three queries:

```sql
-- Dormant or suspended accounts
SELECT email, status, email_verified_at, last_login_at
FROM users ORDER BY last_login_at NULLS FIRST;

-- Who holds what, per tenant
SELECT t.slug, u.email, m.status, r.slug AS role
FROM memberships m
JOIN tenants t ON t.id = m.tenant_id
JOIN users u ON u.id = m.user_id
JOIN membership_roles mr ON mr.membership_id = m.id
JOIN roles r ON r.id = mr.role_id
WHERE m.status = 'active'
ORDER BY t.slug, r.slug, u.email;

-- Live keys, least recently used first
SELECT key_id, name, tenant_id, last_used_at, expires_at
FROM api_keys WHERE revoked_at IS NULL
ORDER BY last_used_at NULLS FIRST;
```

Pay particular attention to `owner` assignments — owner is the only role that can mint another owner, and it is the only role holding `billing.manage` in addition to everything else. A key with a null or ancient `last_used_at` is a revocation candidate, not a mystery to leave alone.

**No session-reaper job exists.** Expired and revoked rows stay in `sessions` until something removes them. Authentication is unaffected — expiry, revocation and the epoch are all checked on every lookup — but the table grows without bound. Cleanup is a manual task today, and `sessions_absolute_expires_at_idx` exists so that it is cheap when it happens:

```sql
DELETE FROM sessions WHERE absolute_expires_at < now() OR revoked_at IS NOT NULL;
```

**No audit-log retention job exists either**, and that one is deliberate for now: `audit_logs` is evidence (§2.8), and deleting it on a schedule before a retention window has been agreed would be worse than letting it grow. Track its size in the weekly disk review; the future options (partitioning or an archival sweep) are recorded in `docs/observability.md` §11.

**Migration check:** the Phase 2 line above still applies, and `alembic upgrade head` now covers `0002_identity_access` in addition to `0001_initial_infra`.

---

## 4. Incident response

**Severity:** SEV1 platform down or data exposure · SEV2 major feature broken or degraded for many tenants · SEV3 single-tenant or minor impairment · SEV4 cosmetic.

**Process:** detect → declare and assign an incident lead → communicate → mitigate before diagnosing → resolve → verify → blameless post-mortem within five working days.

**Post-mortems** capture timeline, impact (tenants, conversations, revenue), root cause, contributing factors, what worked, what did not, and concrete corrective actions with owners — which land in `TODO.md`.

**Every incident answers:** was any data lost? was any customer message dropped or duplicated? was any tenant boundary crossed? did an alert fire, and was it actionable?

**Phase 3 addition — two more questions for any incident touching identity:** was any credential material exposed (in a log, a response body, an exception, a trace or a backup), and was any session or key used by someone other than its owner? A credential-exposure incident is SEV1 regardless of how few accounts are involved, and its mitigation always begins with §2.12 and §2.13 before diagnosis, per the "mitigate before diagnosing" rule above.

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
- [ ] No unexplained failed-login pattern in `audit_logs` — checked manually, there is no alert for this yet (§2.11)
- [ ] No account still locked out with no explanation (§2.11)
- [ ] No live API key unused for > 90 days (§2.13)
- [ ] `sessions` row count within expectation — no reaper job exists (§3)
- [ ] `audit_logs` growth reviewed — no retention job exists (§3)
