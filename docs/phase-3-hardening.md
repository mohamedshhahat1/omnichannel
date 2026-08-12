# Phase 3 hardening and closure

**Branch:** `fix/phase-3-hardening`, cut from `phase-2-infrastructure` at
`40fe382363bf1ed2fa472244ee3267af69f26c68`
**Date:** 2026-08-12
**Status of Phase 3 after this pass:** complete with limitations (§6)
**Phase 4:** not started. No `webhook_events`, no `outbox_events`, no event
dispatcher, no replay infrastructure, no invitation-delivery system.

This document records what the hardening pass changed and why, and - just as
importantly - what it did not verify. It is the companion to the audit that
opened it; where it disagrees with an older statement elsewhere in `docs/`,
§7 lists the disagreement explicitly rather than leaving the reader to find it.

---

## 1. The invariant this pass had to preserve

The Phase 3 RBAC correction made PostgreSQL authoritative for runtime
authorization. Every change below preserves that path unchanged:

```
membership -> membership_roles -> roles -> role_permissions -> permissions
           -> effective permission set -> cache -> Principal -> decision
```

Nothing added here reads `DEFAULT_ROLE_GRANTS` or calls
`permissions_for_roles()`. Two tests now enforce that mechanically; see §4.3.

---

## 2. What was implemented

### 2.1 Membership lifecycle: INVITED -> ACTIVE

`POST /members` has always created a membership as `INVITED`, and
`MembershipStatus` has always described `INVITED -> ACTIVE` as the expected
first transition, but nothing in the codebase performed it. The consequence was
visible in the test suite, which had to issue a raw `UPDATE memberships` to
reach any tenant-scoped assertion.

This is a Phase 3 gap rather than Phase 4 work. Phase 4 owns *invitation
delivery* - the emailed token, its expiry, its single use. It does not own the
state machine of a membership, which Phase 3 already defined, wrote a column
for, and depends on at login. Two routes into `ACTIVE` are now implemented,
both transactional and both leaving the membership selectable by
authentication:

| Route | Caller | Authorisation | Notes |
| --- | --- | --- | --- |
| `POST /invitations/accept` | the invitee | identity - the caller's own membership in the named tenant | Payload carries only a tenant slug, so there is no identifier to tamper with |
| `POST /members/{id}/activate` | an administrator | `members.manage` | For onboarding a colleague directly |

Both are idempotent on an already-active membership: they succeed and leave
`accepted_at` untouched. Any status other than `INVITED` or `ACTIVE`
(`SUSPENDED`, `REVOKED`) is refused with `409 membership_transition_invalid` -
suspension is a deliberate act and activation must not quietly undo it.
Reinstatement is a separate decision this phase does not expose.

A membership in another tenant answers `404 membership_not_found`, and an
invitation that was never issued answers 404 as well, so neither endpoint
confirms the existence of a tenant or a membership to someone who has no
standing in it. Both write an audit row (`membership.accept`,
`membership.activate`).

### 2.2 Role mutation

`POST /members/{id}/roles` existed. Its counterpart did not, so a role granted
by mistake could not be taken back through the API - the operationally
dangerous half of the pair. `DELETE /members/{id}/roles/{slug}` now exists,
requires `members.manage`, is tenant-scoped through the same repository as the
grant, invalidates the effective-permission cache entry for that membership,
writes `membership.remove_role`, and refuses to strip the last active owner
(`409 last_owner`) because a tenant with no owner cannot be administered by
anyone, including us.

**`role_permissions` mutation is deliberately not implemented.** See §6.1.

### 2.3 Credential ambiguity (`docs/security.md` 2.8)

A request that presents both a session cookie and an `Authorization: Bearer`
credential is now refused with `401 ambiguous_credentials`, before either
credential is examined - so the refusal reveals nothing about whether either
was valid. Previously the bearer key won, which is wrong in three distinct
ways:

* **Identity confusion.** The browser attaches the cookie to every request to
  this origin. Anything that also attaches a key - an SDK with a stale value in
  its environment, a proxy, a debugging header - acted as the key's tenant
  while the person at the keyboard was signed in as somebody else, and the
  audit trail recorded the wrong actor.
* **CSRF bypass.** CSRF and origin checks apply to the cookie path only.
  Preferring the bearer credential meant that adding any `Authorization` header
  to a cookie-carrying request routed around them.
* **No safe precedence exists.** Preferring the cookie instead merely moves the
  confusion. Two credentials means the client did not mean one of them.

Single-credential behaviour is unchanged: cookie only authenticates, bearer
only authenticates, neither is `401 authentication_failed`.

### 2.4 Origin and Referer validation (`docs/security.md` 2.4, layer 3)

Applied to cookie-authenticated unsafe methods only, evaluated before the
double-submit token. A bearer client has no ambient credential for an
attacker's page to borrow, so subjecting it to a browser-origin rule would
break legitimate machine clients while protecting nobody.

It is worth having next to the CSRF token because the two fail differently: the
token lives in a cookie any same-site context can read, while `Origin` is
written by the browser and cannot be forged by page script. A subdomain
takeover or an XSS on a sibling host defeats the first and not the second.

* Allowed: any origin in `security.cors_origins` or
  `security.csrf_trusted_origins`, plus the application's own `Host` -
  a single-origin deployment therefore needs no configuration.
* Refused: `null`, unparsable values, and any other origin. `Referer` is used
  only when `Origin` is absent.
* The same-host rule compares **host and port**. A different port is a
  different origin, so a service on 8443 does not inherit the writes of the
  dashboard on 443. The first version of this code compared only the host and
  was caught by its own unit test on the first CI run; see §5.
* Missing entirely: refused when
  `Settings.require_origin_on_cookie_writes` is true, which is
  **unconditionally true in production** and cannot be configured off. Outside
  production it defaults to false so that curl and the test suite can drive the
  API.
* In production, `*` in `csrf_trusted_origins` fails settings validation at
  startup, so the protection cannot be disabled by configuration.

### 2.5 Per-source-address login throttling

The existing per-account lockout counts failures in `users.failed_login_count`.
It is defeated by spreading attempts across many accounts, which is what
credential stuffing does. The new limiter is a fixed window in Redis keyed by a
SHA-256 digest of the source address - never the address in clear - and counts
**all** login attempts from that source, successful ones included, so a valid
credential cannot be used to keep a hostile budget topped up. It complements
the account lockout and replaces nothing.

Defaults are deliberately conservative because no threshold was specified
anywhere in the repository: **20 attempts per 300 seconds per source**, roughly
two orders of magnitude above a human's rate and far below a useful spray. A
NAT'd office shares one address, which is the reason the window is not tighter.
All four values are configurable (§3).

Failure behaviour is explicit: if Redis is unreachable the limiter **fails
open** by default and logs `rate_limit.backend_unavailable`. Failing closed
would turn a cache outage into a total authentication outage, and the
per-account lockout - which lives in PostgreSQL - is unaffected by the same
outage. `login_rate_limit_fail_open=false` is available for deployments that
prefer the opposite trade.

The source address is the peer address. A forwarding header is read only when
`server.trusted_proxy_hops` is greater than zero, and then only at the position
the configured number of proxies implies. With the default of zero, no
forwarding header is trusted at all - otherwise a caller could choose their own
rate-limit bucket, and forge the address written to the audit log, with one
line of text.

### 2.6 Breached-password screening

Applied where passwords are created: registration and member invitation, in
`ProvisioningService`, immediately after the length and composition policy and
before hashing. There is no password-change endpoint in Phase 3, so there is
nothing else to apply it to (§6.3).

The corpus is local. No external service is contacted, no hash prefix leaves
the process, and the plaintext is never logged, stored, or included in the
rejection - which is `422` through the existing validation contract and names
no password material. A local list is a smaller net than a k-anonymity lookup
against a breach corpus, but it introduces no new dependency, no egress and no
latency in the registration path, and it is honest about what it is:
`BreachedPasswordScreen` accepts an arbitrary corpus, so a deployment that
wants Pwned Passwords can supply one without touching a call site.

The switch is named `auth.breach_screen_enabled` rather than anything
containing "password", because `tests/unit/test_auth_settings.py` asserts that
the only `AuthSettings` field names mentioning a password are the two length
bounds. That rule keeps credential *material* out of configuration; a boolean
is not material, but the rule is worth more than the nicer name.

### 2.7 `permissions_for_roles()` and `DEFAULT_ROLE_GRANTS`

Neither is deleted, because migration `0002_identity_and_access` seeds
`roles`, `permissions` and `role_permissions` from them, and they remain the
reviewable statement of what the six system roles are intended to grant.
Deleting them would move that statement into a migration file where nobody
reads it.

What must never return is a *runtime* read. Two tests now enforce that
(§4.3): an AST scan of every module under `app/`, and a behavioural test that
replaces `permissions_for_roles` with a function that raises and then resolves
a principal's permissions successfully anyway.

---

## 3. New configuration

| Setting | Default | Purpose |
| --- | --- | --- |
| `server.trusted_proxy_hops` | `0` | Number of trusted reverse proxies. `0` means no forwarding header is read |
| `server.forwarded_for_header` | `X-Forwarded-For` | Header consulted when hops > 0 |
| `security.csrf_trusted_origins` | `()` | Extra origins accepted on cookie writes |
| `security.require_origin_on_cookie_writes` | `false` | Forced to true in production |
| `auth.breach_screen_enabled` | `true` | Breached-password screening. Must be true in production |
| `auth.login_rate_limit_enabled` | `true` | Must be true in production |
| `auth.login_rate_limit_max_attempts` | `20` | Per source, per window |
| `auth.login_rate_limit_window_seconds` | `300` | Window length |
| `auth.login_rate_limit_fail_open` | `true` | Behaviour when Redis is unreachable |

Production settings validation additionally rejects `*` in
`csrf_trusted_origins`, `breach_screen_enabled=false` and
`login_rate_limit_enabled=false`.

**All nine are documented in `backend/.env.example`** as of the closure audit
(§7). They were absent from it when this table was first written, which meant
the one setting an operator must change for a proxied deployment -
`server.trusted_proxy_hops` - was discoverable only by reading the source.

---

## 4. Tests added

### 4.1 `tests/unit/test_identity_hardening.py`

Origin normalisation and the accept/reject matrix (allowlisted, foreign,
same-origin, port mismatch, an explicit default port, `Referer` fallback,
`null`, unparsable, missing with and without mandatory presence, insecure
scheme); the breach screen including an explicit assertion that a multi-word
passphrase is **not** flagged; fixed-window counting, per-bucket independence,
window reset, hashed buckets, no-backend degradation, and both fail-open and
fail-closed Redis outage behaviour; credential-ambiguity detection including an
empty bearer value and an unrelated cookie.

### 4.2 `tests/integration/test_identity_hardening_api.py`

Against real PostgreSQL, in the rolled-back-transaction style of
`test_identity_api.py`: the full membership lifecycle including a tenant-scoped
session established after activation; idempotency; uninvited, cross-tenant and
suspended refusals; activation without `members.manage`; grant and revoke with
an authorization decision taken from the API on either side of each mutation;
last-owner protection; all seven credential combinations from TASK 4 including
two valid credentials belonging to different tenants; origin accept, reject,
missing, `Referer` fallback and bearer exemption; and per-source throttling
across two accounts and two source addresses.

One trap for anyone extending this file: `_sign_in` clears the client's cookies
and starts a new session, so every CSRF token issued before it becomes stale.
Sending a stale one produces a 403 `csrf_validation_failed` that is easily
mistaken for the refusal a test was hoping to observe - which is exactly how
two of the faults in §5 arose.

### 4.3 The two structural guards

`test_no_runtime_module_references_the_static_grant_table` parses every module
under `app/` and fails if anything except `modules/identity/domain.py` names
`DEFAULT_ROLE_GRANTS` or `permissions_for_roles`.
`test_permissions_resolve_from_the_database_even_if_the_grant_table_explodes`
monkeypatches `permissions_for_roles` to raise and asserts that permission
resolution still succeeds, returning exactly what the repository reported for
the membership it was asked about.

---

## 5. Verification: what has run, and what still has not

The implementation commits in this branch were written without a single gate
being executed - the authoring environment had no network access and none of
`ruff`, `mypy`, `pytest`, `alembic`, `docker`, `fastapi`, `sqlalchemy`, `redis`,
`argon2-cffi`, `httpx` or `asyncpg` installed. Nothing here was verified at the
time it was written, and this section previously said so.

The CI trigger was then widened to run on every branch, and the workflow
executed against commit `88df434f`. **The integration job really ran** - 122
tests against the PostgreSQL and Redis service containers, not skipped for
missing `OC_TEST_DATABASE_URL` / `OC_TEST_REDIS_URL`. It found five distinct
faults:

| Gate | Result on `88df434f` | Cause |
| --- | --- | --- |
| `ruff check` | 7 errors | Unused arguments in test doubles |
| `ruff format --check` | 2 files | An over-long comprehension; redundant parentheses |
| `pytest -m "not integration"` | 3 failed, 443 passed | One real bug, one field name, one obsolete workflow assertion |
| `pytest -m "integration"` | 2 failed, 120 passed | Two faulty tests |
| `mypy`, `docker build`, `docker compose config` | not observed | Output not available to the author of this note |

The five causes, and what was done about each:

1. **A real bug.** `_host_matches` compared hostnames but not ports, so
   `https://app.example.com:8443` was accepted as same-origin for a request to
   `app.example.com`. The test was right and the code was wrong; the code was
   fixed and a second test now pins the explicit-default-port case.
2. **A field name.** `breached_password_check_enabled` tripped a pre-existing
   assertion that no `AuthSettings` field name may mention a password. The
   setting was renamed (§2.6). The assertion was not touched.
3. **An obsolete workflow assertion.** `test_ci_workflow.py` required the push
   trigger to name `main` and `phase-2-infrastructure`; the trigger had
   deliberately been widened to all branches. This is the only assertion in the
   pass that changed, and it is now stricter than before.
4. **Lint.** Test doubles took arguments they never read. The Redis stub's are
   now underscore-prefixed; the fake repository instead *records* the membership
   id it was asked for, and the test asserts on it.
5. **Two faulty integration tests.** Both used a CSRF token invalidated by a
   later `_sign_in`. One failed outright; the other **passed for the wrong
   reason**, because it accepted `403 or 404` and got the CSRF 403. It now
   requires 404 specifically. A third test asked an `apikeys.manage` key to mint
   a `conversations.read` key, which the API correctly refuses - a key may not
   grant more access than it holds.

A second run was then observed against commit `093e0b9a`. The unit gate
improved to **1 failed, 446 passed**, and `ruff format --check` still reported
one file. Both were faults in the new unit test file, not in application code:
the explicit-default-port test asserted `same_origin` for an origin that is
also in the test's allowlist, so it was matched by the allowlist branch first
and never exercised the port comparison at all. It now uses a host that is
deliberately not allowlisted. No integration, MyPy, Docker or Compose output
was available for that run.

No assertion was weakened, no test was deleted, and no expected value was
changed to match observed behaviour except where the requirement itself had
moved (item 3). The faults were in this branch's own code or tests; the one
policy change was requested deliberately.

**What this section still cannot claim.** The gates have not been observed
passing. The fixes above are pushed but no green run has been read, and `mypy`,
`docker build` and `docker compose config` have not been seen at all, on any
commit of this branch. Until someone can point at a green run of every job on
this branch's head, the correct description remains "fixed in response to real
failing runs", not "verified".

No migration was added: nothing in this pass changes the schema.

---

## 6. Gaps that remain open

### 6.1 `role_permissions` mutation has no API - deliberate

TASK 2 asked whether Phase 3 should expose granting and revoking permissions
*on roles*. It should not, and this pass did not build it.

The six roles are seeded as system roles and are shared across every tenant.
An endpoint that edited `role_permissions` would therefore be a cross-tenant
mutation by construction: one tenant's administrator adding `billing.manage` to
`agent` would change what every other tenant's agents can do. Making that safe
requires per-tenant custom roles - a schema change (`roles.tenant_id`, a
uniqueness rule per tenant, a migration, and a resolution rule for name
collisions with system roles) that is not in the Phase 3 scope and would be
unreviewable folded into a hardening pass.

The honest statement is therefore: in Phase 3, *which* roles a membership holds
is mutable through the API; *what a role means* is fixed by migration and
changed by migration. Membership-level role assignment - which this pass
completed in both directions - is the mutation surface the current architecture
supports.

### 6.2 Invitation delivery is still Phase 4

`POST /members` sets an initial password chosen by the inviting administrator
and nothing is emailed. There is no invitation token, no expiry and no single
use. `POST /invitations/accept` closes the *state* gap, not the *delivery* gap.

### 6.3 No password-change or reset flow

Breached-password screening is applied at every point where Phase 3 accepts a
password. There are only two, both at creation. When a change or reset endpoint
is added it must call the same `_enforce_password_policy` path; that is the
only place the check needs to be wired.

### 6.4 Cache invalidation is per-membership, not per-role

Role mutation invalidates the affected membership's effective-permission entry.
If per-tenant custom roles ever arrive (§6.1), editing a role will need to
invalidate every membership holding it; the current cache key layout supports
that but no such fan-out is implemented, because no such mutation exists.

### 6.5 Documentation amended in part

The closure audit amended `TODO.md` and `backend/.env.example` in place.
`docs/security.md`, `CURRENT_STATE.md` and `DECISIONS.md` were **not** amended;
the exact required changes remain enumerated in §7 rather than applied, because
the tooling available replaces a file wholesale and a blind rewrite of a 36 KB
security specification is a worse outcome than a precise list.

---

## 7. Statements elsewhere that this branch supersedes

Each item below is the exact change required. Items marked **APPLIED** were
amended during the closure audit; the rest still describe the pre-hardening
state.

**`docs/security.md`** - outstanding

1. §2.4 - the CSRF section should record that layer 3 (Origin/Referer) is now
   implemented for cookie-authenticated unsafe methods, evaluated before the
   double-submit token, with the allowlist and production posture in §2.4 above.
2. §2.5 - add per-source login throttling alongside the account lockout, with
   the defaults and fail-open rationale from §2.5 above.
3. §2.8 - the credential-precedence rule is now enforced; the sentence
   describing bearer preference is obsolete.
4. §2.10 - the deviation table should move "cookie + bearer precedence",
   "no per-source rate limiting", "no Origin/Referer validation" and "no
   breached-password checking" out of Outstanding, and should gain the two
   deliberate limitations in §6.1 and §6.2 of this document.
5. §10.1 - the audit action list should gain `membership.accept`,
   `membership.activate` and `membership.remove_role`.

**`CURRENT_STATE.md`** - outstanding. The membership lifecycle should be
described as implemented in both directions into `ACTIVE`; the
credential-precedence, origin, throttling and breach-screening rows should move
to implemented, each qualified by §5.

**`TODO.md`** - **APPLIED.** Four P1 items and three technical-debt rows
described this branch's own code as missing: credential ambiguity, the
`invited -> active` transition, per-source throttling and role revocation. The
first two are now ticked; the throttling and role-mutation items remain open
with their scope corrected to what is genuinely left (per-endpoint limiting, and
editing a role's grants). The CI trigger claim was corrected from
`main`/`phase-2-infrastructure` to every branch, and a dated note states that
the work sits on an unmerged branch whose pipeline has not been observed green -
so "implemented" is not read as "verified".

**`backend/.env.example`** - **APPLIED.** It documented none of the nine
settings in §3. All nine are now present with their real defaults, and
`OC_SERVER__TRUSTED_PROXY_HOPS` carries the warning that its safe default of
`0` is the wrong value behind a proxy. A stale comment describing
`SESSION_CACHE_TTL_SECONDS` as the TTL of "the Redis role-slug cache" was
corrected: that cache holds resolved effective permissions, and the old wording
implied something still re-derives permissions from slugs.

**`DECISIONS.md` / ADR-0015** - outstanding. The historical text must stand.
The addendum to append, and the limit of what the repository proves:

> **2026-08-12 - verification note.** The Phase 3 RBAC correction described
> above was implemented on `fix/rbac-database-authoritative` and merged into
> `main` as squash commit `5fe587a1d016fb56209b531a6530bb1f945653a4`
> (PR #1). Runtime authorization resolves from `role_permissions` in
> PostgreSQL; `DEFAULT_ROLE_GRANTS` and `permissions_for_roles()` remain in
> `modules/identity/domain.py` as migration seed data and reference only, and
> `tests/unit/test_identity_hardening.py` now fails if any module under `app/`
> references either. This note deliberately does **not** assert that CI passed:
> the merge is visible in repository history, a green pipeline is not, and it
> should be recorded here only by someone who can point at the run.

**`backend/tests/integration/test_identity_api.py`** - outstanding. The
docstring of `_activate_membership` states that the INVITED -> ACTIVE transition
"has no endpoint until Phase 4". That is no longer true; the raw `UPDATE`
remains a reasonable fixture shortcut, but the comment should now point at
`POST /members/{id}/activate`. Left unamended deliberately: it is a docstring
inside a 58-test file that the available tooling can only rewrite wholesale, and
the risk of corrupting a passing integration suite outweighs the value of
correcting a comment.

---

## 8. Recommended before Phase 4

1. Read the CI run for this branch's head and record the result here. §5
   documents two failing runs and the fixes made in response; it does not
   document a passing one, and `mypy`, `docker build` and `docker compose
   config` have not been observed at all.
2. Apply the three outstanding amendments in §7 - `docs/security.md`,
   `CURRENT_STATE.md` and `DECISIONS.md`.
3. Decide §6.1 explicitly - per-tenant custom roles, or a documented statement
   that role definitions are migration-managed for the foreseeable future.
   Phase 4's event work should not be built on an unstated assumption either
   way.
4. Configure `server.trusted_proxy_hops` for each deployed environment. Left at
   `0` behind a reverse proxy, the login limiter buckets every request under
   the proxy's address, which turns a per-source control into a global one.
   `backend/.env.example` now says so at the point of configuration.
5. Consider seeding `COMMON_BREACHED_PASSWORDS` from a fuller corpus at deploy
   time; the interface already accepts one.
