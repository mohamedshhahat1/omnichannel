# Security

> Security architecture, authentication design, tenant isolation and threat model.
>
> **Implementation status:** §2 (authentication), §3 (authorisation/RBAC), the SQL and Redis rows of §4 (tenant isolation) and the identity subset of §10 (audit logging) are **implemented as of Phase 3**. Each of those sections carries an implementation-status block stating exactly what exists and what does not. Everything else remains design ahead of implementation.

---

## 1. Security principles

1. Defence in depth — no single control is the only thing between a tenant and a breach.
2. Least privilege for users, API keys, AI tools, containers and network paths.
3. Deny by default — authorisation is explicit, not implied by route reachability.
4. Trusted context only — identity and tenant are derived server-side.
5. All external input is untrusted: provider payloads, uploads, crawled pages, **and model output**.
6. Fail closed — if authorisation, verification or validation cannot be evaluated, reject.
7. Auditable — security-relevant actions are recorded with actor, tenant and correlation id.

---

## 2. Authentication (ADR-0009)

### 2.1 Decision in one line
**Opaque, server-side, revocable sessions delivered in `__Host-` prefixed secure cookies — not JWTs.**

The first client is a first-party dashboard used by tenant admins and agents handling customer conversations. Immediate revocation (offboarding, stolen device, password reset) matters far more than stateless scaling, and we already have PostgreSQL and Redis in the request path.

### 2.2 Session strategy

| Aspect | Decision |
|---|---|
| Token | 256 bits from a CSPRNG, base64url-encoded, opaque — carries no claims |
| Storage | `sessions` row with **SHA-256 hash** of the token; the plaintext exists only in the cookie |
| Source of truth | PostgreSQL |
| Cache | Redis read-through, TTL ≤ 60 s, explicitly invalidated on revocation |
| Cookie name | `__Host-oc_session` |
| Cookie flags | `HttpOnly`, `Secure`, `SameSite=Lax`, `Path=/`, **no `Domain`** |
| Idle expiry | 7 days sliding (`last_seen_at` refreshed at most once per minute) |
| Absolute expiry | 30 days, non-extendable |
| Rotation | New token on login, password change, email change and privilege change |
| Metadata | IP, user agent, created/last-seen, for a "your devices" screen |

The `__Host-` prefix is enforced by the browser: it requires `Secure`, `Path=/` and no `Domain`, which blocks subdomain cookie injection.

### 2.3 Refresh and revocation

There is **no separate refresh token**. A refresh token exists to work around the fact that a JWT cannot be revoked; an opaque session has no such problem. "Refresh" is simply sliding the idle window, with periodic token rotation.

Revocation is immediate and complete:

| Action | Effect |
|---|---|
| Sign out | Delete/revoke that session row, evict the cache key |
| Sign out everywhere | Revoke all sessions for the user |
| Password reset or change | Revoke all sessions, issue one new one |
| Membership removed / role changed | Bump the user's `session_epoch`; cached sessions with an older epoch are rejected on next use |
| Account disabled | Revoke all sessions, block issuance |

The epoch check is what keeps the ≤ 60 s cache from becoming a revocation hole: the epoch is compared on every request, so a permission change takes effect on the next request, not after TTL expiry.

### 2.4 CSRF protection

Cookie authentication requires CSRF defence, layered:

1. `SameSite=Lax` blocks cross-site POSTs from ordinary navigation.
2. **Double-submit token** — `__Host-oc_csrf` (readable by JS, not `HttpOnly`) must be echoed in the `X-CSRF-Token` header on every unsafe method; the server compares them in constant time.
3. `Origin` / `Referer` validation against an allowlist.
4. A strict CORS policy: explicit origin allowlist, `credentials: true`, no wildcards.
5. Cookie authentication is **rejected** on webhook and API-key routes, so those paths are structurally immune.

### 2.5 Password security

- **Argon2id**, ~64 MiB memory, 3 iterations, parallelism 4, tuned to roughly 250 ms on production hardware; per-hash random salt; parameters encoded in the stored hash.
- Transparent rehash on successful login when parameters change.
- Minimum 12 characters, checked against a breached-password list; no forced composition rules or rotation.
- Login responses are generic and constant-time; rate limited per account **and** per IP with exponential backoff, plus alerting on credential-stuffing patterns.
- Every authentication event (success, failure, lockout) is audit-logged.

### 2.6 Email verification and password reset

| | Email verification | Password reset |
|---|---|---|
| Token | 256-bit random, stored hashed | 256-bit random, stored hashed |
| Lifetime | 24 hours | 30 minutes |
| Uses | Single-use, consumed atomically | Single-use, consumed atomically |
| On success | Marks `email_verified_at` | Sets the new hash, **revokes all sessions**, notifies the user |
| Enumeration | Identical response regardless of account existence | Identical response regardless of account existence |
| Rate limit | Per email and per IP | Per email and per IP |

Verification is required before creating a tenant or connecting a channel.

### 2.7 Tenant context resolution

The session identifies a **user**, not a tenant. On each request the client indicates the active tenant (path segment or header); the server loads the membership, verifies it is active, and constructs the trusted `TenantContext`. A `tenant_id` in a request body is never authoritative and is rejected if it conflicts. Tenant switching is a normal authorised request, not a new login.

### 2.8 Service clients and integrations

| Client type | Mechanism |
|---|---|
| Tenant server-to-server API clients | Tenant-scoped API key `oc_{env}_{key_id}_{secret}`; only the secret's hash is stored; `key_id` is indexed for O(1) lookup; sent as `Authorization: Bearer`; scoped permissions, per-key rate limits, `last_used_at`, rotation and instant revocation; shown once at creation |
| Inbound provider webhooks (Meta, Paddle) | HMAC signature verification over the **raw** body, constant-time comparison, timestamp/replay window where the provider supports it. No session, no API key |
| Internal workers | No network auth — same process boundary and same database credentials, scoped by the tenant context carried in the task payload |
| Future outbound webhooks to tenant systems | Per-endpoint HMAC secret, timestamp header, replay window, documented verification recipe |
| Future mobile/native clients | API-key path or a bearer-session variant of the same opaque session |

Cookie authentication and API-key authentication are mutually exclusive per request; a request presenting both is rejected.

### 2.9 Future
TOTP MFA and step-up authentication for sensitive actions · passkeys/WebAuthn · OIDC/SAML SSO and SCIM for enterprise tenants. Because sessions are opaque and server-side, adding an external identity provider changes only how a session is **established**, never how it is **validated**.

### 2.10 Implementation status (Phase 3)

**Implemented as specified.**

| Area | Where |
|---|---|
| 256-bit opaque session tokens; only the SHA-256 digest persisted | `app/core/security.py`, `identity/services/sessions.py` |
| Cookie name and flags driven by `AuthSettings`; `__Host-` prefix, `Secure` and `Path=/` required in production by a settings validator | `app/core/settings.py` |
| Sliding idle expiry plus a non-extendable absolute expiry; `last_seen_at` refreshed at most once per `session_touch_interval_seconds` | `identity/services/sessions.py` |
| `session_epoch` on the user; one increment invalidates every outstanding session, checked on every request | `identity/models.py`, `identity/services/authentication.py` |
| Double-submit CSRF: `X-CSRF-Token` compared in constant time against the stored digest for every unsafe method | `identity/api/dependencies.py` |
| Argon2id with parameters from settings, transparent rehash detection on successful verification, production cost floor enforced by settings validation | `app/core/security.py` |
| Generic, indistinguishable failures: unknown address, wrong password and malformed address all return the same 401 | `identity/services/authentication.py` |
| Per-account lockout after `max_failed_logins`, refusing even the correct password until it lapses | `identity/services/authentication.py` |
| Tenant context derived server-side from an active membership; never read from a request body | `identity/api/dependencies.py` |
| API keys: `key_id` indexed for a single lookup, only the secret digest stored, plaintext returned once at creation, mandatory expiry, revocation, tenant scoping | `identity/services/api_keys.py` |
| Session metadata (IP, user agent, created, last seen) captured on the row | `identity/models.py`, `identity/api/routes.py` |
| Every authentication event audit-logged with outcome and reason | `identity/services/audit.py` |

**Deviations from the text above — the code does not yet match the design.**

| § | Design says | Implementation does | Status |
|---|---|---|---|
| 2.8 | A request presenting both a cookie and an API key is **rejected** | The bearer key wins and the cookie is ignored | Deviation. "Rejected" is the safer rule; the code should move to it. Tracked in `TODO.md` |
| 2.4 (3) | `Origin`/`Referer` validated against an allowlist | Not implemented; only `SameSite` and the double-submit token are in place | Outstanding |
| 2.5 | Passwords checked against a breached-password list | Length and surrounding-whitespace policy only | Outstanding |
| 2.5 | Rate limited per account **and per IP** | Per-account lockout only | Outstanding — needs the shared rate limiter (P1) |
| 2.2 | Rotation on password change and email change | Those endpoints do not exist yet, so only login issues a fresh session | Deferred with the endpoints |
| 2.6 | E-mail verification and password reset | `email_tokens` is modelled and migrated; no transport is wired, so neither flow is usable | Deferred to the P1 delivery work |
| 2.2 | "Your devices" screen | Metadata is captured; no listing endpoint exists | Deferred |
| 2.9 | MFA, passkeys, SSO | Not started, explicitly P2 | Deferred |

---

## 3. Authorisation and RBAC

Enforced in the **service layer**, not only in route decorators, so tasks and AI tools are covered by the same checks.

```
Permission check = active session/API key
                 + active membership in the target tenant
                 + role grants the required permission
                 + resource belongs to that tenant
                 + plan entitlement allows the feature
```

**Permissions:** `tenant.read`, `tenant.update`, `members.invite`, `members.manage`, `channels.connect`, `channels.manage`, `conversations.read`, `conversations.reply`, `conversations.assign`, `ai.configure`, `knowledge.manage`, `catalog.manage`, `billing.manage`, `apikeys.manage`, `audit.read`.

**Default roles:** Owner (all) · Admin (all except billing/ownership transfer) · Manager (conversations, catalog, knowledge, AI config) · Agent (conversations read/reply/assign) · Billing Admin (billing only) · Viewer (read only).

Roles are data, so custom roles and finer-grained permissions can be added without a schema redesign.

### 3.1 Grant matrix as seeded (Phase 3)

Migration `0002_identity_access` seeds exactly this. `●` = granted.

| Permission | owner | admin | manager | agent | billing_admin | viewer |
|---|:--:|:--:|:--:|:--:|:--:|:--:|
| `tenant.read` | ● | ● | ● | ● | ● | ● |
| `tenant.update` | ● | ● | | | | |
| `members.invite` | ● | ● | | | | |
| `members.manage` | ● | ● | | | | |
| `channels.connect` | ● | ● | | | | |
| `channels.manage` | ● | ● | | | | |
| `conversations.read` | ● | ● | ● | ● | | ● |
| `conversations.reply` | ● | ● | ● | ● | | |
| `conversations.assign` | ● | ● | ● | ● | | |
| `ai.configure` | ● | ● | ● | | | |
| `knowledge.manage` | ● | ● | ● | | | |
| `catalog.manage` | ● | ● | ● | | | |
| `billing.manage` | ● | | | | ● | |
| `apikeys.manage` | ● | ● | | | | |
| `audit.read` | ● | ● | | | | |

### 3.2 Implementation status (Phase 3)

- Checks live in `identity/services/authorization.py` and are called from services, not routes, so a future Celery task or AI tool uses the identical path.
- **A member who lacks a permission gets 403. A caller with no membership in the tenant gets 404**, so an identifier cannot be probed for existence. This is deliberate; see ADR-0015.
- The seeded rows are the operative copy of the grant matrix. An integration test compares them against the Python `DEFAULT_ROLE_GRANTS` table per role, so the database and the code cannot drift silently.
- Only an owner may grant the owner role.
- An API key's scopes are intersected against the creator's permissions at issue time, so a key can never carry more authority than the person who minted it. An unknown scope is 422; a real scope the caller does not hold is 403.
- Unknown role slugs resolve to no permissions rather than raising — the resolver fails closed.
- A suspended membership keeps its row but resolves to no principal, so access stops without destroying history.
- **Plan entitlement is not part of the check yet** — the billing module does not exist. The line stays in the model above because entitlement will slot into the same choke point.

---

## 4. Tenant isolation controls

| Surface | Control | Status |
|---|---|---|
| SQL | Tenant-scoped repositories inject `tenant_id`; unscoped access to tenant tables is prohibited outside reviewed admin paths | **Implemented (Phase 3)** — `TenantScopedRepository`; scoping is structural, so a caller cannot express an unscoped query. Globally-scoped lookups (user by e-mail, session by digest, API key by `key_id`) are separate, explicitly named classes |
| Redis | Key namespace `oc:{env}:t:{tenant_id}:{purpose}:{key}` | **Implemented (Phase 2, used in Phase 3)** by the RBAC role cache |
| Celery | Every payload carries `tenant_id`; the task rebuilds the trusted context before touching data | Implemented (Phase 2); no identity tasks exist yet |
| Object storage | Keys prefixed `tenants/{tenant_id}/...`; signed URLs issued only after an authorisation check | Pending — module not started |
| Vector search | `tenant_id` predicate applied before ranking, in the repository — not in caller code | Pending — module not started |
| AI tools | Tools receive the tenant context from the runtime, never from model-supplied arguments | Pending — module not started |
| Webhooks | Tenant resolved from the verified integration mapping, never from payload fields | Pending — Phase 4+ |
| Caches | Tenant id is part of every cache key | Implemented for the RBAC cache |
| Audit logs | Tenant-scoped, read gated by `audit.read` | Partially — rows are tenant-scoped and the permission exists; no read endpoint yet |
| Exports/reports | Scoped and rate limited; large exports run as tenant-scoped jobs | Pending |

**Testing:** every module with tenant data ships tests asserting that tenant A cannot read, update, delete, retrieve, or reference tenant B — including through AI tools and signed URLs.

Phase 3 covers the SQL surface for identity: a real, active membership in another tenant resolves to `None` by id and by user, never appears in a listing, and cross-tenant revocation of a real API key answers 404. Redis, object storage, retrieval and tools remain to be covered as those modules land.

RLS is planned as an additional layer once the schema stabilises (ADR-0002).

---

## 5. AI-specific security

1. **The LLM has no database access.** It sees conversation context, retrieved snippets and tool schemas.
2. **Tools are the only path to effects.** Every tool independently enforces tenant scope, permission, input validation, business rules, rate limits and audit logging — it must be safe even if the model is fully adversarial.
3. **Retrieved and crawled content is data, not instructions.** It is delivered in a clearly delimited, labelled context section with an explicit instruction that content inside it is untrusted reference material.
4. **Output is validated** before it reaches a customer: no leaked system prompt, no cross-tenant references, no fabricated prices/stock/policies, no unauthorised commitments, no injected links or markup.
5. **Authoritative facts come from tools/retrieval only.** If the model lacks a tool-backed answer, it must say so or escalate.
6. **Cost and abuse limits** are enforced per tenant and per conversation, with entitlement checks before a run starts.
7. Prompt-injection fixtures are part of the regression suite.

---

## 6. Untrusted content and SSRF (crawler)

Controls required before any fetch (ADR-0012):

| Control | Detail |
|---|---|
| Scheme/port allowlist | `http`/`https` on standard ports only |
| Address blocking | Private, loopback, link-local (incl. `169.254.169.254`), reserved, multicast, IPv6 equivalents and IPv4-mapped forms |
| DNS validation | Resolve first, validate **every** returned address |
| Rebinding defence | Pin the validated IP for the connection; do not re-resolve between check and fetch |
| Redirects | Re-validate every hop; cap the count |
| Limits | Max pages, depth, response size, content types, total duration, concurrency |
| Resource limits | Container CPU, memory, disk quotas |
| Ownership | Verify domain ownership before crawling |
| Network isolation | Crawler workers have no route to PostgreSQL, Redis, internal APIs, the Docker socket or metadata endpoints; results are written through a narrow authenticated ingestion interface |
| Content handling | Sanitised, stored flagged untrusted, quarantined from instruction context |

---

## 7. Secrets management

- Secrets come from the environment or a secret manager; never from source control.
- CI runs secret scanning; a hit blocks the pipeline.
- Tenant provider credentials (Meta tokens, etc.) are stored encrypted at rest or as references to a secret store, decrypted only in the process that needs them, and never logged or returned by an API.
- Signing keys, database credentials and provider keys have documented rotation procedures (`operations.md`).
- Secrets are excluded from logs, traces, Sentry payloads and error messages by centralised redaction.

### 7.1 Credential handling in the identity module (Phase 3)

No credential material is ever stored in a recoverable form, and none is returned after creation:

| Credential | Stored as | Returned |
|---|---|---|
| Password | Argon2id hash | Never |
| Session token | SHA-256 digest | Only in the `Set-Cookie` at issue |
| CSRF token | SHA-256 digest | Only in the `Set-Cookie` at issue |
| API key secret | SHA-256 digest | Exactly once, in the 201 that created it |
| E-mail token | SHA-256 digest | Only in the (not yet wired) message |

Supporting controls: response schemas are built field by field rather than from ORM attributes, so adding a column cannot leak it by default; audit context is scrubbed against a marker list (`pass`, `secret`, `token`, `credential`, `authorization`, `cookie`, `digest`, `hash`, `api_key`) before it is written, with values truncated; anything sensitive in an error goes in `internal_message`, which ADR-0013 guarantees is logged and never serialised. An integration test asserts a failed login's audit context does not contain the attempted password, and an API test asserts no response body ever contains a stored digest.

---

## 8. Transport, headers and network

TLS 1.2+ (prefer 1.3) with HSTS; HTTP redirected to HTTPS. Security headers: `Strict-Transport-Security`, `Content-Security-Policy`, `X-Content-Type-Options`, `Referrer-Policy`, `Permissions-Policy`, `X-Frame-Options`/frame-ancestors. Only NGINX is exposed publicly; PostgreSQL, Redis, Prometheus, Grafana and `/metrics` are bound to internal networks. Host firewall default-deny inbound; SSH by key only, no root login, ideally restricted by source.

---

## 9. File upload security

Size caps enforced at NGINX and in the application · content-type and magic-byte validation (never trust the extension) · stored in object storage with generated keys, never with user-supplied paths · never written to an executable path · served only via short-lived signed URLs after an authorisation check · downloads use `Content-Disposition: attachment` and a strict content type · optional malware scanning before the document enters the knowledge pipeline.

---

## 10. Audit logging

Recorded: authentication events, session revocations, API key lifecycle, membership and role changes, channel connect/disconnect, AI configuration changes, knowledge and catalog mutations, billing changes, entitlement overrides, data exports, admin/impersonation actions, tool calls with business effects.

Each entry: tenant, actor type and id, action, resource type and id, metadata, IP, `correlation_id`, timestamp. Audit logs are append-only, retained at least 12 months, and readable only with `audit.read`.

### 10.1 Implementation status (Phase 3)

The `audit_logs` table exists with tenant, actor, action, outcome, resource, context, IP, correlation id and timestamp, indexed for the three queries that matter (by tenant over time, by actor over time, by action over time). Actions emitted today:

`auth.login` · `auth.logout` · `auth.logout_all` · `identity.register` · `tenant.create` · `membership.invite` · `membership.assign_role` · `apikey.create` · `apikey.revoke`

Each carries an outcome, and failures carry a reason (`locked_out`, `bad_credentials`, `inactive_account`). Correlation and request ids are pulled from the ambient context by the audit service itself, so no call site can forget them.

Not yet implemented: append-only enforcement at the database level (the table is append-only by convention, not by permission or trigger), the 12-month retention policy, and a read endpoint gated by `audit.read`. The remaining action list belongs to modules that do not exist yet.

---

## 11. Threat model (STRIDE, abbreviated)

| Threat | Vector | Mitigation |
|---|---|---|
| **Spoofing** | Forged webhook | HMAC verification over the raw body, replay window |
| | Session theft | `HttpOnly` + `Secure` + `__Host-` cookies, rotation, revocation, CSP |
| | Credential stuffing | Argon2id, rate limits, breach list, alerting, future MFA |
| **Tampering** | Client-supplied `tenant_id` | Server-derived trusted context |
| | Parameter tampering on tools | Tool-side validation and authorisation |
| | Malicious upload | Type/size validation, isolated storage |
| **Repudiation** | Disputed agent or AI action | Audit logs, AI run records, tool-call records |
| **Information disclosure** | Cross-tenant leak | Scoped repositories, isolation tests, RLS later |
| | Leak via AI context | Tenant-scoped retrieval, output validation |
| | Secrets in logs | Centralised redaction, secret scanning |
| | Public object URLs | Private buckets, short-lived signed URLs |
| | **Account enumeration** | Identical responses for unknown vs. registered address on login and registration; 404 rather than 403 for cross-tenant objects |
| **Denial of service** | Webhook flood | Edge + application rate limits, thin endpoints, async processing |
| | AI cost exhaustion | Entitlements, per-tenant quotas, cost alerts |
| | Crawler resource abuse | Hard limits and container quotas |
| **Elevation of privilege** | Missing authorisation check | Service-layer enforcement, per-permission tests |
| | **API key minted with more scope than its creator** | Scopes intersected against the creator's permissions at issue time |
| | Prompt injection → tool abuse | Tools authorise independently of the model |
| | SSRF → internal access | IP/DNS/redirect validation, IP pinning, egress restriction |

---

## 12. Dependency and container security

Pinned dependencies with lock files · vulnerability scanning in CI with a documented severity policy · image scanning · minimal base images · non-root containers · read-only root filesystem where possible · dropped capabilities · no Docker socket mounts · resource limits on every container · regular base-image rebuilds.

---

## 13. Launch security checklist

No box below is ticked. Phase 3 was authored in an environment with no network and no installed dependencies, so `pytest`, `ruff`, `mypy` and Docker could not be executed there; CI is the first authoritative run. Items are annotated with what exists today.

- [ ] Cross-tenant isolation tests passing across DB, Redis, storage, retrieval and tools — *DB surface written for identity in Phase 3 (negative cross-tenant cases by id, by user, in listings, and on revocation); Redis, storage, retrieval and tools pending those modules*
- [ ] All webhooks signature-verified with replay protection — *Phase 4+*
- [ ] Argon2id parameters tuned and benchmarked — *parameters are configurable with a production floor enforced by settings validation; not yet benchmarked on production hardware*
- [ ] Session rotation, revocation and epoch invalidation verified — *implemented and covered by written tests; awaiting a green CI run*
- [ ] CSRF and CORS verified with a hostile-origin test — *double-submit implemented and tested; `Origin`/`Referer` validation not implemented*
- [ ] Rate limits on auth, webhooks, AI and uploads — *per-account lockout only; no shared rate limiter yet*
- [ ] Secret scanning and dependency scanning green — *not yet added to CI*
- [ ] Log redaction verified for tokens, keys and PII (including Sentry) — *Phase 1 redaction plus Phase 3 audit-context scrubbing; not yet verified end to end*
- [ ] TLS, HSTS and security headers verified — *headers implemented in Phase 1; no TLS terminator deployed*
- [ ] Audit logging covers the launch-critical action list — *identity actions covered; the rest belong to later modules*
- [ ] Prompt-injection fixtures passing — *AI module not started*
- [ ] Restore rehearsal completed within the RTO target — *not attempted*
