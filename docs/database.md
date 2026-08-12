# Database

> Conceptual data model and database engineering policy.
>
> **Implemented so far:** Phase 2 added the infrastructure foundation — async SQLAlchemy/asyncpg runtime, async Alembic configuration, and one extension-only migration. **Phase 3 added the eleven identity and access tables** (§4.1) in migration `0002_identity_access`. Every other section remains a conceptual model for tables that do not exist yet.

---

## 1. Strategy

One PostgreSQL database, one shared schema, `tenant_id` on every tenant-owned row (ADR-0002). Isolation is enforced in the application layer through tenant-scoped repositories fed by a trusted `TenantContext`. PostgreSQL RLS is a planned defence-in-depth layer, not the primary mechanism.

Required extensions: `pgcrypto` (or application-side UUIDv7), `pg_stat_statements`, `pg_trgm` (fuzzy/product search), `vector` (pgvector).

**Implemented in Phase 2:** the initial Alembic migration enables exactly those four extensions and nothing else. The application uses SQLAlchemy 2.x in async mode via `asyncpg`, with bounded pools, UTC/timeouts, and deterministic metadata naming conventions.

**Phase 3 note:** application-side UUIDv7 generation is used for primary keys. `pgcrypto`'s `gen_random_uuid()` is used only inside migration `0002` to seed reference rows, because a migration cannot import application code.

---

## 2. Table classification

Every table must be classified in one of four categories, and the classification recorded here when it is created.

| Class | Meaning | Examples |
|---|---|---|
| **A. Global** | Platform-wide, no tenant | `users`, `plans`, `features`, `permissions` |
| **B. Tenant-owned** | Non-null `tenant_id`, always scoped | `conversations`, `messages`, `documents`, `products`, `usage_events` |
| **C. Join / association** | Links A and B | `memberships`, `membership_roles` |
| **D. Deferred-tenant** | Written before the tenant is known, resolved immediately after | `webhook_events` (nullable `tenant_id` until the integration is resolved) |

Class D exists so that an unverifiable or unmapped provider delivery can still be recorded for debugging without being silently attributed to the wrong tenant.

As of Phase 3, classes A, B and C all have real instances. Class D has none until the event backbone lands in Phase 4.

---

## 3. Conventions

- **Primary keys:** UUID (v7 preferred for index locality). No sequential integers in public identifiers.
- **Timestamps:** `timestamptz` in UTC. `created_at` everywhere; `updated_at` on mutable tables.
- **Money:** integer minor units + ISO currency code. Never floats.
- **Enums:** application-level string enums with a check constraint (cheaper to evolve than PostgreSQL enum types).
- **JSON:** `jsonb` for provider payloads and flexible metadata only — never for data that is queried or constrained relationally.
- **Naming:** `snake_case`, plural tables, `<table>_<cols>_idx`, `<table>_<cols>_uq`, `fk_<table>_<ref>`.
- **Deletion:** hard delete by default. Soft deletion only where justified (see §9).

Phase 3 follows all of these: every identity primary key is a UUIDv7 generated in the application, every timestamp is `timestamptz`, and every status column is a `text` column with a `CHECK` constraint rather than a PostgreSQL enum type — so adding a status later is an `ALTER ... DROP/ADD CONSTRAINT` rather than an enum migration that locks.

---

## 4. Domain model overview

### 4.1 Identity & Access — **implemented (Phase 3, migration `0002_identity_access`)**

Every table below inherits `id uuid primary key` (UUIDv7, application-generated) and `created_at timestamptz not null default now()`. Mutable tables also carry `updated_at timestamptz` maintained on update.

| Table | Class | Columns beyond the base |
|---|---|---|
| `tenants` | A\* | `name`, `slug` (unique), `status` (`active`/`suspended`), `deleted_at` |
| `users` | A | `email` (unique), `password_hash`, `display_name`, `status` (`active`/`suspended`/`deactivated`), `session_epoch`, `failed_logins`, `locked_until`, `email_verified_at`, `last_login_at` |
| `memberships` | C | `tenant_id`, `user_id`, `status` (`invited`/`active`/`suspended`), `invited_by_id`, `accepted_at` — unique `(tenant_id, user_id)` |
| `roles` | A/B | `slug`, `name`, `description`, `tenant_id` (nullable; null = system role), `is_system` |
| `permissions` | A | `slug` (unique), `description` |
| `role_permissions` | C | `role_id`, `permission_id` — unique `(role_id, permission_id)` |
| `membership_roles` | C | `membership_id`, `role_id` — unique `(membership_id, role_id)` |
| `sessions` | A | `user_id`, `tenant_id` (nullable), `token_digest` (unique), `csrf_digest`, `session_epoch`, `issued_at`, `last_seen_at`, `idle_expires_at`, `absolute_expires_at`, `revoked_at`, `ip`, `user_agent` |
| `api_keys` | B | `tenant_id`, `key_id` (unique), `secret_digest`, `name`, `scopes`, `created_by_id`, `last_used_at`, `revoked_at`, `expires_at` |
| `email_tokens` | A | `user_id`, `purpose` (`email_verification`/`password_reset`), `token_digest`, `expires_at`, `consumed_at` |
| `audit_logs` | B | `tenant_id`, `actor_type`, `actor_user_id`, `actor_api_key_id`, `action`, `outcome`, `resource_type`, `resource_id`, `context` (jsonb), `ip`, `correlation_id`, `request_id` |

\* `tenants` is the tenant root, so it carries `id` rather than `tenant_id`.

**Divergences from the original sketch, and why.**

| Sketch | Built | Reason |
|---|---|---|
| `users.email` is `citext` | `text` with a `CHECK (email = lower(email))` | ADR-0015. Avoids widening the extension surface and keeps the invariant explicit in the schema. The application normalises on the way in; the constraint means no future code path can create `Ada@` beside `ada@` and give one mailbox two accounts |
| `roles.name`, `permissions.code` | `roles.slug`, `permissions.slug` | The stored value is the string the domain layer, the API and `security.md` §3 already use (`conversations.reply`). `name` is kept on `roles` as the human label |
| One unique constraint on roles | Two **partial** unique indexes | A system role has `tenant_id IS NULL`, and NULLs do not collide in a plain unique index — which would allow unlimited duplicate system roles. `roles_slug_idx` is unique `WHERE tenant_id IS NULL`; `roles_tenant_id_slug_idx` is unique `WHERE tenant_id IS NOT NULL` |
| `sessions` keyed to a user only | `sessions.tenant_id` is nullable | A session exists before a tenant is chosen, and signing in against a tenant you do not belong to must produce a tenantless session rather than an error (ADR-0015) |

**Foreign keys and `ON DELETE` semantics.** `RESTRICT` is the default, per §6. The deliberate exceptions:

| Child | Parent | Behaviour | Reason |
|---|---|---|---|
| `memberships` | `tenants`, `users` | `CASCADE` | A membership has no meaning without both ends |
| `membership_roles` | `memberships` | `CASCADE` | Part of the membership aggregate |
| `membership_roles` | `roles` | **`RESTRICT`** | Deleting a role out from under its holders would silently strip their permissions instead of failing loudly. Covered by an integration test |
| `role_permissions` | `roles`, `permissions` | `CASCADE` / `RESTRICT` | A role owns its grants; a permission in use may not vanish |
| `sessions`, `email_tokens` | `users` | `CASCADE` | Credentials do not outlive the account |
| `api_keys` | `tenants` | `CASCADE` | Tenant-owned |
| `api_keys.created_by_id`, `memberships.invited_by_id` | `users` | `SET NULL` | Attribution should degrade, not block deleting a user |
| `audit_logs` | anything | `SET NULL` / no FK on `resource_id` | An audit record must survive the thing it describes |

**Indexes.** `tenants_status_idx` · `memberships_user_id_idx` · `memberships_tenant_id_status_idx` · `roles_slug_idx` (partial unique) · `roles_tenant_id_slug_idx` (partial unique) · `role_permissions_permission_id_idx` · `membership_roles_role_id_idx` · `sessions_user_id_idx` · `sessions_tenant_id_user_id_idx` · `sessions_absolute_expires_at_idx` (for the future reaper) · `api_keys_tenant_id_created_at_idx` · `email_tokens_user_id_purpose_idx` · `audit_logs_tenant_id_created_at_idx` · `audit_logs_actor_user_id_created_at_idx` · `audit_logs_action_created_at_idx`.

The unique index on `sessions.token_digest` and on `api_keys.key_id` is what makes authentication a single indexed lookup rather than a scan that hashes every stored row.

**Seeded reference data.** The migration inserts the 15 permissions of `security.md` §3, the six system roles, and their grants. This is reference data that the authorisation code cannot function without, not domain data — no tenant, user or membership row is created. An integration test asserts the seeded grants match the domain table exactly, so the two cannot drift.

### 4.2 Event backbone

| Table | Class | Purpose |
|---|---|---|
| `webhook_events` | D | Durable record of every inbound provider delivery |
| `outbox_events` | B/D | Atomic publication intent, written in the producing transaction |
| `processed_events` | B | Consumer-side idempotency ledger |
| `delivery_attempts` | B | Outbound provider attempts and their outcomes |

**`webhook_events`** — provider, provider_account_id, provider_event_id, event_type, signature_verified, payload (jsonb), headers_subset (jsonb), received_at, tenant_id (nullable), integration_id (nullable), status, error.
Unique: `(provider, provider_event_id)`.

**`outbox_events`** — id, tenant_id (nullable for class D), aggregate_type, aggregate_id, event_type, payload (jsonb), dedup_key (unique), ordering_key, status (`pending|dispatching|dispatched|failed|dead_letter`), available_at, attempts, max_attempts, lease_expires_at, last_error, traceparent, correlation_id, created_at, dispatched_at.
Indexes: partial `(available_at, created_at) WHERE status = 'pending'`; partial `(lease_expires_at) WHERE status = 'dispatching'`; `(status, created_at)`.

**`processed_events`** — consumer, outbox_event_id, tenant_id, processed_at. Unique `(consumer, outbox_event_id)`.

**`delivery_attempts`** — tenant_id, message_id, provider, idempotency_key (unique), attempt_no, request_fingerprint, status, provider_message_id, provider_status_code, error, started_at, completed_at.

Full semantics: `messaging.md` §3–§5. **This is the Phase 4 scope.**

### 4.3 Channels

`channel_integrations` (tenant_id, channel_type, provider_account_id, display_name, status, credentials_ref, capabilities, connected_by, connected_at) — unique `(provider, provider_account_id)` so an inbound event resolves to exactly one tenant.
`provider_message_map` (tenant_id, provider, provider_message_id, message_id) — unique `(provider, provider_message_id)`.

Credentials are stored as references to secret storage or encrypted at rest — never as plaintext columns (`security.md` §7).

### 4.4 Conversations

`contacts` (tenant_id, channel_type, provider_user_id, display_name, locale, profile jsonb, first_seen_at, last_seen_at) — unique `(tenant_id, channel_type, provider_user_id)`.
`conversations` (tenant_id, contact_id, channel_integration_id, subject_ref, mode, status, assigned_membership_id, last_message_at, opened_at, closed_at, version).
`messages` (tenant_id, conversation_id, direction, sender_type, sender_id, body, content_type, provider_message_id, provider_timestamp, sequence_no, status, ai_run_id, created_at) — unique `(tenant_id, provider_message_id)` where present.
`message_attachments` (tenant_id, message_id, media_object_id, kind, provider_media_id).
`handoff_requests`, `conversation_summaries`, `assignments` follow the same tenant-scoped pattern.

Ordering uses `provider_timestamp` plus a monotonic `sequence_no` assigned on insert; late-arriving events are inserted in order, not appended blindly.

### 4.5 Knowledge (RAG)

`knowledge_sources` (tenant_id, type: upload|url|manual|catalog, status, config jsonb).
`documents` (tenant_id, source_id, title, media_object_id, content_hash, status, trust_level, language).
`document_versions` (tenant_id, document_id, version, content_hash, extracted_at).
`document_chunks` (tenant_id, document_id, version_id, ordinal, text, token_count, metadata jsonb).
`chunk_embeddings` (tenant_id, chunk_id, model, dimensions, embedding `vector`, created_at) — unique `(chunk_id, model)`.
`processing_jobs` (tenant_id, document_id, stage, status, attempts, error).

Indexes: HNSW on `embedding` with the appropriate operator class, plus a B-tree on `tenant_id`. **Every vector query filters on `tenant_id`** — filter first, then rank. Embeddings are versioned by model so a model change is a backfill, not a breaking change.

### 4.6 Catalog

`products` (tenant_id, external_ref, title, description, status, category_id) · `product_variants` (tenant_id, product_id, sku unique per tenant, attributes jsonb) · `prices` (tenant_id, variant_id, currency, amount_minor, valid_from, valid_to) · `inventory_items` (tenant_id, variant_id, quantity_available, quantity_reserved, updated_at, version) · `categories`, `product_attributes`, `product_images` (tenant_id, product_id, media_object_id, position, alt_text).

Future `product_embeddings` (text and image) is deliberately anticipated but not created now.

### 4.7 Billing, entitlements, usage

`plans`, `features`, `plan_features` (Class A) · `billing_customers`, `subscriptions`, `billing_events` (unique `(provider, provider_event_id)`), `invoices`, `entitlement_overrides`, `usage_events` (unique `idempotency_key`), `usage_aggregates` (unique `(tenant_id, usage_type, period_start)`).

Details in `billing.md`.

### 4.8 Media

`media_objects` (tenant_id, bucket, object_key unique, content_type, size_bytes, checksum, category, scan_status, uploaded_by, created_at).

---

## 5. Indexing policy

1. Composite indexes lead with `tenant_id` unless documented otherwise.
2. Index every foreign key used in joins or cascade paths.
3. Use partial indexes for hot filtered subsets (pending outbox rows, open conversations, active subscriptions).
4. Support keyset pagination with an index matching the exact sort order (e.g. `(tenant_id, conversation_id, sequence_no)`).
5. Create indexes `CONCURRENTLY` in production migrations.
6. Review `pg_stat_statements` and unused-index reports each release; drop indexes that earn nothing.

Rule 5 does not apply to `0002_identity_access`: it creates the tables and their indexes in the same migration, on tables that are empty by definition, so there is nothing to lock. `CONCURRENTLY` becomes mandatory the first time an index is added to a populated identity table.

---

## 6. Constraints and integrity

- Foreign keys everywhere, with deliberate `ON DELETE` semantics (`RESTRICT` by default; `CASCADE` only within an aggregate).
- Uniqueness that encodes business rules: `(provider, provider_event_id)`, `(tenant_id, channel_type, provider_user_id)`, `(consumer, outbox_event_id)`, `(tenant_id, sku)`.
- Check constraints for enums, non-negative quantities and amounts, and valid date ranges.
- Composite foreign keys including `tenant_id` where practical, so a child row cannot reference a parent in another tenant.

Phase 3 adds to that list: `(tenant_id, user_id)` on `memberships` — a person cannot join the same tenant twice — plus `email = lower(email)` on `users`, non-negative checks on `session_epoch` and `failed_logins`, and status checks on `tenants`, `users`, `memberships`, `email_tokens` and `audit_logs`.

---

## 7. Transactions and concurrency

- Transactions are short. **Never** perform network I/O (provider, LLM, storage) inside one.
- The outbox pattern is the standard way to turn "commit" into "do work later".
- Dispatcher claims use `SELECT ... FOR UPDATE SKIP LOCKED` with a batch limit and a lease.
- Optimistic locking (`version` column) for conversation state and inventory.
- Pessimistic locking (`SELECT ... FOR UPDATE`) only for short, critical state transitions such as subscription changes.
- Per-conversation serialisation uses an advisory lock keyed by conversation id, so concurrent inbound events cannot interleave.
- Default isolation is `READ COMMITTED`; anything stricter must be justified in review.

Identity writes are all short single-statement or few-statement transactions and need none of the locking machinery above. Argon2id hashing is deliberately expensive, so it is performed **outside** the database transaction, never while holding a row lock.

---

## 8. Migration policy (Alembic)

1. All schema change goes through Alembic. No manual production DDL, ever.
2. **Expand → Migrate → Contract** for anything destructive:
   - *Expand:* add the new nullable column/table/index; deploy code that writes both and reads the old.
   - *Migrate:* backfill in batches; switch reads to the new shape; verify.
   - *Contract:* in a **later release**, drop the old column/table and tighten constraints.
3. Never combine expand and contract in one release.
4. Migrations must be safe while the previous application version is still serving traffic.
5. Lock-avoidance: `CREATE INDEX CONCURRENTLY`, `ADD CONSTRAINT ... NOT VALID` then `VALIDATE`, batched backfills with a statement timeout.
6. Every destructive migration ships with: a written plan, a fresh verified backup, a rehearsal against a production-like dataset, and a rollback/recovery note.
7. CI runs the full upgrade path; production deploy runs migrations before the new image serves traffic, and blocks on failure.
8. **Implemented in Phase 2:** the Alembic environment is async, reads the separate migration DSN from validated settings, and the initial migration only enables required extensions.
9. **Implemented in Phase 3:** `0002_identity_access` (down revision `0001_initial_infra`). It is a pure **expand** step — it only creates tables, indexes and reference rows, touches nothing that exists, and is safe to apply while the previous version serves traffic, so rule 2 is satisfied trivially and rule 3 is not at risk. Its `downgrade()` drops the eleven tables in dependency order. Migration 0001 was **not** modified. Migrations are never run from application startup; the Compose topology and CI both run `alembic upgrade head` as a separate one-shot step using the migration role.

---

## 9. Soft deletion policy

Default is **hard delete**. Soft deletion (`deleted_at`) is permitted only where there is a stated purpose:

| Table | Reason |
|---|---|
| `conversations`, `messages` | Dispute resolution, audit and compliance |
| `documents` | Reversible removal from the knowledge base; embeddings must be deleted or excluded immediately |
| `products` | Historical order references and analytics |
| `tenants`, `memberships` | Offboarding grace period and recovery |

Everywhere else, delete. Soft-deleted rows must be excluded by the repository layer by default and purged by a retention job.

**Phase 3:** `tenants.deleted_at` is the only soft-delete column implemented, and `TenantRepository` filters `deleted_at IS NULL` by default, so a soft-deleted tenant is invisible without any caller opting in. `memberships` uses a `status` column instead — a suspended membership keeps its row and its history but resolves to no principal, which serves the same offboarding purpose with clearer semantics than a nullable timestamp. `users` are deactivated by status, never deleted, because audit records reference them. No purge job exists yet.

---

## 10. Connection management

Bounded pools sized per process type (API vs worker vs beat), with `pool_pre_ping` and sane recycle. Total pooled connections across all processes must stay well under `max_connections`. Statement and lock timeouts are set at the session level; long analytical work uses a separate, larger timeout profile. PgBouncer is introduced only when connection count becomes the constraint (`architecture.md` §10, step 7).

---

## 11. Retention

| Data | Retention (initial) |
|---|---|
| `webhook_events` payloads | 30–90 days, then payload pruned, metadata kept |
| `outbox_events` dispatched | 14–30 days |
| `processed_events` | 30–90 days (must exceed the maximum retry window) |
| `delivery_attempts` | 90 days |
| `audit_logs` | 12 months minimum |
| `usage_events` | Current + previous billing period detail, then aggregates |
| Conversations/messages | Tenant-configurable, subject to a platform minimum |

Retention jobs run in the `maintenance` queue, in batches, off-peak.

**Not yet enforced.** No retention job exists. Two identity tables will need one: `audit_logs` grows without bound, and `sessions` accumulates expired rows — `sessions_absolute_expires_at_idx` exists so that reaper can be cheap when it is written.
