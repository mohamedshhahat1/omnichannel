# Integrations

> Every external dependency sits behind an internal interface. Business modules never import a vendor SDK.

---

## 1. Integration inventory

| Integration | Purpose | Internal interface | Criticality |
|---|---|---|---|
| WhatsApp Cloud API | Inbound/outbound messages | `ChannelProvider` | Critical |
| Instagram Messaging | Inbound/outbound DMs | `ChannelProvider` | Critical |
| Facebook Messenger | Inbound/outbound DMs | `ChannelProvider` | Critical |
| Instagram Comments | Comment events and replies | `ChannelProvider` | High |
| Facebook Comments | Comment events and replies | `ChannelProvider` | High |
| LLM provider | Completions, tool calling | `LLMProvider` | Critical |
| Embedding provider | Vectors for RAG | `EmbeddingProvider` | High |
| Paddle | Subscriptions and payments | `BillingProvider` | High |
| S3-compatible storage | Binaries | `ObjectStorage` | Critical |
| Email provider | Verification, reset, notifications | `EmailProvider` | High |
| Sentry | Error tracking | SDK at the edge | Medium |
| OTLP collector | Traces | OpenTelemetry exporter | Medium |

---

## 2. Adapter rules

1. One adapter package per provider; vendor types never escape it.
2. Adapters translate to internal models and an internal error taxonomy (`retryable`, `rate_limited`, `auth_failed`, `invalid_request`, `provider_error`, `ambiguous`).
3. Every outbound call has a timeout, a retry policy with jittered backoff, and a circuit breaker.
4. Every call emits a span with provider, operation, status and retry count.
5. Credentials are resolved per tenant integration at call time — never global mutable state.
6. Adapters are covered by contract tests against **recorded real payloads**, so provider changes fail in CI rather than in production.
7. Provider payloads are validated with Pydantic before use; unknown shapes are stored and flagged, never guessed.

---

## 3. Meta channels

**Shared model:** app-level webhook subscription, per-page/per-account tokens stored per tenant integration, HMAC `X-Hub-Signature-256` verification over the raw body, and a verification challenge on subscription.

| Concern | Handling |
|---|---|
| Tenant resolution | `(provider, provider_account_id)` → `channel_integrations`, unique |
| Duplicate deliveries | Unique `(provider, provider_event_id)` on `webhook_events` |
| Echo events (our own sends) | Detected and ignored during normalisation |
| Messaging window | Capability-driven; template required outside the WhatsApp window |
| Media | Downloaded asynchronously to object storage, never inline in the webhook |
| Rate limits | Redis token bucket per integration; honour `Retry-After` |
| Token expiry / revoked permission | Integration marked `degraded`, sending paused, tenant admin notified |
| Comment specifics | Edits/deletes arrive as separate events; private reply may be one-shot and time-limited |
| API version changes | Version pinned in configuration; upgrades validated by contract tests first |

**Onboarding flow:** tenant admin authorises via Meta OAuth → we store the token reference and account metadata → subscribe to webhooks → verify with a test event → mark the integration `active`.

---

## 4. AI providers

`LLMProvider`: `complete`, `stream`, `capabilities`. `EmbeddingProvider`: `embed`, `dimensions`, `model_id`.

Adapters normalise tool-calling schemas, token accounting, finish reasons, content filtering signals, rate limits and errors. Model choice is configuration per tenant/plan/task. Cost is computed from a pricing table in configuration and recorded per run.

**Failure handling:** timeouts sized to the channel's response expectations; retries only for transient classes; on sustained failure the run escalates to a human rather than retrying indefinitely; a future secondary provider can be added behind the same interface for failover.

**Data handling:** the provider receives only the assembled context needed for the turn — no raw database dumps, no cross-tenant data, no secrets. Provider data-retention settings must be reviewed and recorded before launch.

---

## 5. Paddle

Covered in `billing.md`. Integration-specific rules: signature verification over the raw body with a timestamp window, provider identifiers stored in dedicated columns, normalised event vocabulary, and a reconciliation sweep to repair missed webhooks.

---

## 6. Object storage

`ObjectStorage`: `put`, `get`, `delete`, `presign_get`, `presign_put`, `head`, `copy`.

Keys: `tenants/{tenant_id}/{category}/{object_id}/{filename}` where category ∈ `documents | products | inbound_media | crawler | exports`. Buckets are private; access is by short-lived signed URL after authorisation. Direct browser uploads use presigned PUT with content-type and size constraints, followed by server-side validation. Provider portability (MinIO ↔ S3 ↔ R2 ↔ Spaces) is an adapter swap plus a copy job.

---

## 7. Email

`EmailProvider`: `send(template, to, context)`. Transactional only at launch: verification, password reset, invitations, security notifications, billing warnings, handoff alerts. Requirements: SPF/DKIM/DMARC configured, bounce and complaint handling, no secrets or tokens in plaintext beyond single-use links, and rate limiting per recipient.

---

## 8. Adding a new integration — checklist

- [ ] Define or reuse an internal interface
- [ ] Implement the adapter with no vendor types leaking
- [ ] Map errors into the internal taxonomy
- [ ] Add timeouts, retries, backoff and circuit breaking
- [ ] Add spans and metrics
- [ ] Verify webhook signatures and replay protection (if inbound)
- [ ] Add idempotency for any non-idempotent operation
- [ ] Store credentials per tenant, encrypted or by reference
- [ ] Write contract tests from recorded payloads
- [ ] Document the provider's rate limits and quotas
- [ ] Add a runbook entry for the provider being down
- [ ] Record an ADR if it changes architecture
