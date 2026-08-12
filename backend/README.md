# Omnichannel Backend

FastAPI application for the omnichannel AI customer conversation platform.

**Phase 3 — Identity & Access.** This package contains the application
foundation (configuration, logging, correlation, error handling, health checks,
tracing bootstrap, security headers), the infrastructure foundation
(PostgreSQL, Redis, Celery, Alembic, Docker) and the identity module (users,
tenants, memberships, RBAC, sessions, API keys, audit logging). There is still
no conversation, channel, AI, catalog or billing functionality. See
[`../CURRENT_STATE.md`](../CURRENT_STATE.md) for what exists today and
[`../TODO.md`](../TODO.md) for what comes next.

---

## Requirements

- Python 3.12 or 3.13

That is enough to install the package and run the unit suite.

The integration suite additionally needs a real **PostgreSQL** (with the
extensions from migration `0001`) and a real **Redis**. Both are provided by
the Compose stack. SQLite is never an acceptable substitute — the fixtures
refuse to run against it.

---

## Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\\Scripts\\activate
pip install --upgrade pip
pip install -e ".[dev]"
```

Then create your local configuration:

```bash
cp .env.example .env
```

`.env` is git-ignored. `.env.example` contains no secrets and never should —
it documents variable names and safe defaults only.

---

## Running the application

Bring up PostgreSQL and Redis (from the repository root):

```bash
docker compose up -d postgres redis
```

Apply migrations. **Migrations never run automatically from application
startup** — this is always a separate, deliberate step, executed with the
migration role rather than the application role:

```bash
cd backend && alembic upgrade head
```

Then start the API:

```bash
uvicorn app.main:app --reload
```

| URL | Purpose |
| --- | --- |
| `http://127.0.0.1:8000/health/live` | Liveness probe |
| `http://127.0.0.1:8000/health/ready` | Readiness probe (PostgreSQL + Redis) |
| `http://127.0.0.1:8000/docs` | Interactive API docs (disabled in production) |

Quick check:

```bash
curl -i http://127.0.0.1:8000/health/live
```

The response carries `X-Request-ID` and `X-Correlation-ID`. Quote the request
id when reporting a problem — it appears on every log line for that request.

---

## Identity endpoints

All under `/api/v1`.

| Method | Path | Permission required |
| --- | --- | --- |
| `POST` | `/auth/register` | — (always `202`, whether or not the address is taken) |
| `POST` | `/auth/login` | — |
| `GET` | `/auth/me` | authenticated |
| `POST` | `/auth/logout` | authenticated |
| `POST` | `/auth/logout-all` | authenticated |
| `POST` | `/tenants` | authenticated |
| `GET` | `/roles` | `tenant.read` |
| `GET` | `/members` | `tenant.read` |
| `POST` | `/members` | `members.invite` |
| `POST` | `/members/{membership_id}/roles` | `members.manage` |
| `GET` | `/api-keys` | `apikeys.manage` |
| `POST` | `/api-keys` | `apikeys.manage` |
| `DELETE` | `/api-keys/{api_key_id}` | `apikeys.manage` |

### Trying it by hand

Sign in and keep the cookies. The response sets an `HttpOnly` session cookie
and a readable CSRF cookie:

```bash
curl -s -c jar.txt -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"correct horse battery staple"}'
```

Safe methods need only the cookie:

```bash
curl -s -b jar.txt http://127.0.0.1:8000/api/v1/auth/me
```

Unsafe methods additionally need the CSRF token echoed in a header — that is
the double-submit check, and omitting it is supposed to fail:

```bash
CSRF=$(grep oc_csrf jar.txt | awk '{print $7}')
curl -s -b jar.txt -X POST http://127.0.0.1:8000/api/v1/api-keys \
  -H "X-CSRF-Token: ${CSRF}" -H 'Content-Type: application/json' \
  -d '{"name":"deploy","scopes":["conversations.read"]}'
```

The key's plaintext is in that response and **nowhere else, ever again**.
Service clients use it as a bearer token, which needs no CSRF header because
it is not a cookie:

```bash
curl -s -H "Authorization: Bearer oc_dev_..." \
  http://127.0.0.1:8000/api/v1/auth/me
```

In production the cookies are named `__Host-oc_session` and `__Host-oc_csrf`;
the `__Host-` prefix requires HTTPS, so the plain names above are development
defaults.

---

## Quality gates

Run all four before pushing. CI (`.github/workflows/ci.yml`) enforces them on every pull request and on pushes to `main` and `phase-2-infrastructure`, alongside integration tests against real PostgreSQL/Redis services, a production Docker image build, and Compose config validation.

```bash
pytest                 # test suite
ruff check .           # lint
ruff format --check .  # formatting
mypy                   # static types (strict)
```

To fix formatting and the auto-fixable lint findings:

```bash
ruff format .
ruff check --fix .
```

The **unit** suite is hermetic: no network, no database, no external services.
`OC_`-prefixed environment variables are stripped before each test (except
`OC_TEST_*`), so a stray shell export cannot change the result.

The **integration** suite is opt-in and runs only when both of these are set:

```bash
export OC_TEST_DATABASE_URL=postgresql+asyncpg://oc_app:oc_app_password@127.0.0.1:5432/omnichannel
export OC_TEST_REDIS_URL=redis://127.0.0.1:6379/15
pytest -m integration
```

Run `alembic upgrade head` first — the integration tests assume the schema and
the seeded RBAC rows already exist. They deliberately create no tables and
truncate nothing, so the unprivileged `oc_app` role is sufficient; each test
runs in a transaction that is rolled back.

---

## Configuration

Settings are typed, validated at startup and read from the environment.
Every variable uses the `OC_` prefix; nested sections use a double underscore:

```bash
OC_ENVIRONMENT=development
OC_SERVER__PORT=8000
OC_LOGGING__FORMAT=console
OC_OBSERVABILITY__TRACING_ENABLED=false
OC_SECURITY__CORS_ORIGINS=["http://localhost:3000"]
OC_DATABASE__URL=postgresql+asyncpg://oc_app:oc_app_password@127.0.0.1:5432/omnichannel
OC_REDIS__URL=redis://127.0.0.1:6379/0
OC_AUTH__SESSION_IDLE_TTL_SECONDS=604800
OC_AUTH__COOKIE_SECURE=false
```

See [`.env.example`](.env.example) for the full list, including the variables
reserved for later phases.

Misconfiguration fails at startup rather than at the first request. In
production the application additionally refuses to start with debug enabled,
non-JSON logging, wildcard CORS origins, or unset trusted hosts.

Phase 3 adds five more production refusals, all in the same validator. In
production the application will not start unless:

- `OC_AUTH__COOKIE_SECURE` is enabled
- the session and CSRF cookie names both carry the `__Host-` prefix
- `OC_AUTH__ARGON2_MEMORY_KIB` is at least 65536
- `OC_AUTH__ARGON2_TIME_COST` is at least 3

These are floors, not warnings. Lowering Argon2 cost for a slow test run is
fine in `development` or `test`; the same value in `production` is a refusal to
boot rather than a quietly weaker hash.

---

## Layout

```
backend/
├── alembic/
│   ├── env.py                async migration environment
│   └── versions/
│       ├── ...0001_initial_infra.py          extensions only
│       └── ...0002_identity_and_access.py    identity tables + RBAC seed
├── app/
│   ├── main.py               ASGI entrypoint
│   ├── worker.py             Celery worker entrypoint
│   ├── core/                 cross-cutting infrastructure
│   │   ├── settings.py       typed configuration
│   │   ├── logging.py        structured logging + redaction
│   │   ├── observability.py  OpenTelemetry bootstrap
│   │   ├── errors.py         error taxonomy
│   │   ├── security.py       Argon2id, token generation, digests, API keys
│   │   ├── database.py       async engine and session factory
│   │   ├── redis.py          async client and tenant-safe key helper
│   │   ├── celery_app.py     queues and routing
│   │   └── infrastructure.py lifespan resource wiring
│   ├── platform/             shared building blocks
│   │   ├── correlation.py    request/correlation identifiers
│   │   ├── health.py         health check registry
│   │   ├── task_context.py   tenant/correlation/trace propagation
│   │   ├── ids.py            UUIDv7 generation
│   │   └── clock.py          UTC now, expiry helpers
│   ├── api/                  HTTP delivery layer
│   │   ├── application.py    application factory
│   │   ├── middleware.py     correlation, security headers, body limit
│   │   ├── exception_handlers.py
│   │   ├── dependencies.py
│   │   ├── schemas.py        error envelope, health responses
│   │   └── routes/           health/, v1/
│   └── modules/              business modules
│       └── identity/         Phase 3
│           ├── domain.py     permissions, roles, Principal, normalisation
│           ├── errors.py     domain errors over the ADR-0013 envelope
│           ├── models.py     the eleven tables
│           ├── repositories.py  TenantScopedRepository and friends
│           ├── services/     authentication, sessions, api_keys,
│           │                 permissions, provisioning, audit,
│           │                 authorization, validation
│           └── api/          routes, schemas, dependency providers
└── tests/
    ├── unit/                 pure logic, no I/O
    └── integration/          real PostgreSQL/Redis, opt-in
```

Dependencies point inward: `api` → `platform`/`core`, `modules` → `core`.
Nothing imports `api`.

---

## Conventions

See [`../ENGINEERING.md`](../ENGINEERING.md) for the full engineering
standards. The rules that shape this package:

- **No business logic in route handlers.** Handlers translate HTTP; services
  decide.
- **Strict typing.** `mypy --strict` passes with no ignores outside the
  third-party stub override.
- **No secrets in source.** Configuration comes from the environment.
- **Log events, not sentences.** `logger.info("application.startup", extra={...})`
  — a dotted event name and structured fields, so logs are queryable.
- **Never log credentials.** The logging layer redacts credential-shaped keys
  automatically, but do not rely on it as a first line of defence.

Four more that arrived with the identity module and apply to every module
after it:

- **Tenant-owned data is reached through a tenant-scoped repository**, never
  through an ad-hoc query with a `tenant_id` filter. Extend
  `TenantScopedRepository`; do not reinvent scoping.
- **Authorise in the service layer**, not the route, so a future Celery task or
  AI tool goes through the identical check. A member lacking a permission gets
  `403`; a caller with no membership gets `404`.
- **Never let a caller distinguish "exists but is not yours" from "does not
  exist".** Cross-tenant access to a real object returns `404`.
- **Credential material is stored as a one-way digest** and returned exactly
  once, at creation. Never add it to a response schema, a log line, an audit
  context or an exception message.
