# Database

> Conceptual data model and database engineering policy. **No models or migrations exist yet** — field lists below are design intent, not implementation.

---

## 1. Strategy

One PostgreSQL database, one shared schema, `tenant_id` on every tenant-owned row (ADR-0002). Isolation is enforced in the application layer through tenant-scoped repositories fed by a trusted `TenantContext`. PostgreSQL RLS is a planned defence-in-depth layer, not the primary mechanism.

Required extensions: `pgcrypto` (or application-side UUIDv7), `pg_stat_statements`, `pg_trgm` (fuzzy/product search), `vector` (pgvector).

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

---

## 3. Conventions

- **Primary keys:** UUID (v7 preferred for index locality). No sequential integers in public identifiers.
- **Timestamps:** `timestamptz` in UTC. `created_at` everywhere; `updated_at` on mutable tables.
- **Money:** integer minor units + ISO currency code. Never floats.
- **Enums:** application-level string enums with a check constraint (cheaper to evolve than PostgreSQL enum types).
- **JSON:** `jsonb` for provider payloads and flexible metadata only — never for data that is queried or constrained relationally.
- **Naming:** `snake_case`, plural tables, `<table>_<cols>_idx`, `<table>_<cols>_uq`, `fk_<table>_<ref>`.
- **Deletion:** hard delete by default. Soft deletion only where justified (see §9).

---

## 4. Domain model overview

### 4.1 Identity & Access

| Table | Class | Notable fields |
|---|---|---|
| `users` | A | email (citext, unique), password_hash (Argon2id), email_verified_at, status, last_login_at |
| `sessions` | A | user_id, token_hash (unique), issued_at, last_seen_at, idle_expires_at, absolute_expires_at, revoked_at, ip, user_agent |
| `api_keys` | B | tenant_id, key_id (unique), secret_hash, scopes, created_by, last_used_at, revoked_at, expires_at |
| `email_tokens` | A | user_id, purpose (verify/reset), token_hash, expires_at, consumed_at |
| `tenants` | A* | name, slug (unique), status, timezone, settings | 
| `memberships` | C | user_id, tenant_id, status, invited_by — unique `(user_id, tenant_id)` |
| `roles` | A/B | name, tenant_id nullable (null = system role) |
| `permissions` | A | code (unique), description |
| `role_permissions` | C | role_id, permission_id |
| `membership_roles` | C | membership_id, role_id |
| `audit_logs` | B | tenant_id, actor_type, actor_id, action, resource_type, resource_id, metadata, ip, correlation_id, created_at |

\* `tenants` is the tenant root, so it carries `id` rather than `tenant_id`.

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

Full semantics: `messaging.md` §3–§5.

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

---

## 6. Constraints and integrity

- Foreign keys everywhere, with deliberate `ON DELETE` semantics (`RESTRICT` by default; `CASCADE` only within an aggregate).
- Uniqueness that encodes business rules: `(provider, provider_event_id)`, `(tenant_id, channel_type, provider_user_id)`, `(consumer, outbox_event_id)`, `(tenant_id, sku)`.
- Check constraints for enums, non-negative quantities and amounts, and valid date ranges.
- Composite foreign keys including `tenant_id` where practical, so a child row cannot reference a parent in another tenant.

---

## 7. Transactions and concurrency

- Transactions are short. **Never** perform network I/O (provider, LLM, storage) inside one.
- The outbox pattern is the standard way to turn "commit" into "do work later".
- Dispatcher claims use `SELECT ... FOR UPDATE SKIP LOCKED` with a batch limit and a lease.
- Optimistic locking (`version` column) for conversation state and inventory.
- Pessimistic locking (`SELECT ... FOR UPDATE`) only for short, critical state transitions such as subscription changes.
- Per-conversation serialisation uses an advisory lock keyed by conversation id, so concurrent inbound events cannot interleave.
- Default isolation is `READ COMMITTED`; anything stricter must be justified in review.

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
