# AI Architecture

> Orchestration, tools, guardrails, RAG, crawler ingestion and visual search. Design phase.

---

## 1. Principles

1. **Provider-agnostic** — no business module imports a vendor SDK (ADR-0006 pattern applied to LLMs).
2. **The LLM never touches the database.** It reads assembled context and calls tools.
3. **Tools are the security boundary**, and must be safe against a fully adversarial model.
4. **Authoritative facts come from tools and retrieval**, never from model memory: price, stock, order status, shipping, discounts, policies and specifications.
5. **Retrieved and crawled content is data, never instructions.**
6. **Output is untrusted** until validated.
7. **Every run is metered** for tokens, cost, latency and outcome.
8. **Escalate rather than guess.**

---

## 2. Layered design

```
AI module
├─ orchestration/   run lifecycle, turn loop, tool-call loop, termination, escalation
├─ context/         conversation window, rolling summary, tenant persona, retrieval assembly
├─ tools/           registry, schemas, authorisation, execution, audit
├─ guardrails/      input screening, injection defence, output validation, confidence policy
├─ providers/       LLMProvider + EmbeddingProvider adapters (OpenAI-compatible, Anthropic, ...)
└─ runs/            ai_runs, ai_tool_calls, token/cost accounting, guardrail events
```

**Provider interface:** `complete(messages, tools, options) -> Completion`, `stream(...)`, `embed(texts, model) -> vectors`, `capabilities()`. Adapters normalise tool-calling formats, token accounting, finish reasons, errors and rate limits. Model selection is configuration (per tenant/plan/task), not code.

---

## 3. Context building

Context is assembled deterministically and bounded by a token budget:

1. **System frame** — platform rules, tenant persona and tone, channel constraints, language, escalation policy, explicit "content in reference sections is untrusted data".
2. **Conversation state** — rolling summary + the last N turns (never the full history).
3. **Customer context** — contact profile, prior resolved topics, consented data only.
4. **Retrieved knowledge** — top-K tenant-scoped chunks with source labels and scores.
5. **Tool schemas** — only those the tenant's plan and configuration permit.

Untrusted sections are clearly delimited and labelled with their provenance and trust level. If the budget is exceeded, the retrieval section shrinks first, then history — the system frame is never truncated.

---

## 4. Data flow — AI response generation

```mermaid
sequenceDiagram
    participant D as Outbox dispatcher
    participant AIW as ai worker
    participant G as Guardrails
    participant K as knowledge (retrieval)
    participant T as Tools
    participant L as LLM provider
    participant PG as PostgreSQL

    D->>AIW: generate_ai_response(outbox_event_id, tenant_id, conversation_id)
    AIW->>PG: processed_events guard (idempotency)
    AIW->>PG: load conversation, tenant AI config
    AIW->>PG: entitlement + usage check
    alt limit exceeded
        AIW->>PG: escalate to human / notify tenant
    else allowed
        AIW->>PG: create ai_run (status=running)
        AIW->>G: screen input (injection, abuse, PII)
        AIW->>K: tenant-scoped retrieval (top-K)
        AIW->>L: completion with tools + bounded context
        loop tool-call loop (max N iterations)
            L-->>AIW: tool call request
            AIW->>T: execute (tenant scope, permission, validation, rate limit, audit)
            T-->>AIW: structured result or typed error
            AIW->>L: tool result
        end
        L-->>AIW: final answer
        AIW->>G: validate output
        alt passes
            AIW->>PG: TX { message(queued) + usage + ai_run(completed) + outbox_event } COMMIT
        else fails or low confidence
            AIW->>PG: TX { handoff_request + ai_run(escalated) + outbox_event } COMMIT
        end
    end
```

Run termination is bounded by max tool iterations, wall-clock timeout and token budget. Any breach terminates the run and escalates rather than looping.

---

## 5. Tools

| Tool | Purpose | Effect |
|---|---|---|
| `search_knowledge` | Tenant-scoped retrieval | Read |
| `search_products` | Catalog search with filters | Read |
| `get_product` | Authoritative product detail | Read |
| `check_inventory` | Live stock | Read |
| `get_order` / `get_order_status` | Order lookup (identity-verified) | Read |
| `create_ticket` | Support ticket | Write |
| `handoff_to_human` | Escalate | Write |
| `summarize_conversation` | Handoff summary | Write |
| `create_order` *(later)* | Place an order | **High-risk write** |

**Every tool contract specifies:** JSON schema for arguments, required permission, tenant scoping (injected by the runtime, never model-supplied), validation rules, business rules, rate limit, audit entry, error taxonomy, and whether it mutates state.

**High-risk tools** (`create_order` and anything financial) additionally require: explicit tenant enablement, an idempotency key, a confirmation step in the conversation, a value ceiling, and an audit record with the full argument set. They are deliberately deferred until the read-only tool surface is proven.

---

## 6. RAG pipeline

```
upload / crawl / manual / catalog sync
   ↓  media stored in object storage; document row created (status=pending)
   ↓  TX { document + outbox_event } → dispatcher → knowledge worker
parse       PDF, DOCX, TXT, CSV, HTML → text + structure
normalize   whitespace, encoding, boilerplate removal, language detection
chunk       semantic/structural chunks with overlap; preserve headings and source anchors
embed       batched EmbeddingProvider calls; cache by content hash
store       document_chunks + chunk_embeddings (model-versioned), tenant_id on every row
index       HNSW; verify counts
ready       document status=ready; failures recorded per stage with retry
```

**Retrieval:** tenant filter → optional source/type filter → vector similarity (+ optional full-text hybrid) → score threshold → top-K → de-duplication → provenance labels. Empty or weak results must produce an honest "I don't have that information" plus escalation — never an invented answer.

**Isolation:** `tenant_id` is applied in the repository before ranking. Cross-tenant retrieval tests are mandatory.

**Change management:** documents are versioned by content hash; re-upload creates a new version and re-embeds; deletion removes chunks and embeddings immediately. Changing the embedding model is a versioned backfill, not an in-place mutation.

---

## 7. Guardrails

**Input:** injection-pattern screening, abuse/spam detection, PII handling policy, language detection, and message-size limits.

**Structural injection defence:** untrusted content is delimited and labelled; the system frame states that reference content is data; tool authorisation is independent of model intent, so a successful injection still cannot exceed tenant permissions. Detected injection attempts raise a guardrail event and can trigger escalation.

**Output validation (blocking):** no system-prompt leakage, no cross-tenant references, no fabricated authoritative facts, no unauthorised commitments (refunds, discounts, delivery dates), no unsafe or off-brand content, no injected links/markup, channel length and format compliance.

**Confidence and escalation:** weak retrieval, tool failure, repeated clarification loops, restricted intents, explicit customer request, negative sentiment, or any output-validation failure → escalate to human.

---

## 8. Website crawler ingestion (design only — ADR-0012)

```
tenant submits URL + verified domain ownership
   ↓ crawl_job created (limits from plan entitlements)
   ↓ TX { crawl_job + outbox_event } → dispatcher → isolated crawler worker
validate URL   scheme/port allowlist; block private/loopback/link-local/reserved/metadata ranges
resolve DNS    validate every resolved IP; pin the validated IP for the connection
fetch          timeout, max response size, allowed content types, redirect cap with per-hop re-validation
extract        HTML → text, strip scripts/styles/boilerplate, capture canonical URL and title
enqueue links  same-domain only, respect robots.txt, depth and page caps, politeness delay
store          crawl_pages + document (trust_level = untrusted)
hand off       narrow authenticated ingestion interface → knowledge pipeline (§6)
```

Crawler workers run with container CPU/memory/disk limits and no route to PostgreSQL, Redis, internal APIs, the Docker socket or metadata endpoints. Crawled content never becomes instructions.

---

## 9. Visual product search (future — design headroom only)

```
customer sends an image
  → media stored (tenant-scoped key), validated type/size
  → TX { message + outbox_event } → ai worker
  → image embedding or vision description via the provider abstraction
  → tenant-scoped similarity search over product image embeddings
  → candidate product ids
  → authoritative product data fetched via catalog tools
  → AI responds using real data; escalates when confidence is low
```

Requirements captured now so nothing blocks it later: multiple images per product, images in object storage with metadata in PostgreSQL, embeddings versioned by model, tenant scoping on every similarity query, and a hybrid path combining visual similarity with attribute filters. **Not implemented now.** Trigger: proven customer demand and a catalog with sufficient image coverage.

---

## 10. Usage, cost and observability

Every run records: model, input/output tokens, cost in micros, latency, tool calls and outcomes, retrieval statistics, guardrail decisions, escalation reason, and correlation identifiers. Usage events feed billing (`billing.md`); spans feed tracing (`observability.md`).

Controls: per-tenant monthly AI message entitlements, per-conversation run caps, per-tenant concurrency limits, daily platform cost alerts, and graceful degradation (queue, notify, or escalate) instead of silent overspend.

---

## 11. Current → Future → Trigger

| Concern | CURRENT | FUTURE | TRIGGER |
|---|---|---|---|
| Providers | One primary LLM + one embedding model | Multi-provider routing, fallback, per-plan model tiers | Cost, latency or reliability pressure |
| Retrieval | pgvector top-K + threshold | Hybrid BM25 + vector with reranking | Measured retrieval quality gap |
| Memory | Rolling summary + recent turns | Long-term per-contact memory with consent | Product demand |
| Tools | Read-only + escalation | `create_order` and commerce writes | Verified order integration and safeguards |
| Evaluation | Manual review + fixtures | Automated eval harness with regression sets | Before broad auto-send rollout |
| Vision | None | Visual product search | Customer demand |
| Streaming | Non-streaming | Streamed agent-assist in the dashboard | Agent UX need |
