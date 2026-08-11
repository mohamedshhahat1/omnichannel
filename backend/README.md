# Omnichannel Backend

FastAPI application for the omnichannel AI customer conversation platform.

**Phase 1 — Repository & Application Foundation.** This package contains the
application skeleton only: configuration, logging, correlation, error handling,
health checks, tracing bootstrap and security headers. There is no database, no
queue, no authentication and no business functionality yet. See
[`../CURRENT_STATE.md`](../CURRENT_STATE.md) for what exists today and
[`../TODO.md`](../TODO.md) for what comes next.

---

## Requirements

- Python 3.12 or 3.13

Nothing else. No database, no Redis, no Docker — those arrive in Phase 2.

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

```bash
uvicorn app.main:app --reload
```

| URL | Purpose |
| --- | --- |
| `http://127.0.0.1:8000/health/live` | Liveness probe |
| `http://127.0.0.1:8000/health/ready` | Readiness probe |
| `http://127.0.0.1:8000/docs` | Interactive API docs (disabled in production) |

Quick check:

```bash
curl -i http://127.0.0.1:8000/health/live
```

The response carries `X-Request-ID` and `X-Correlation-ID`. Quote the request
id when reporting a problem — it appears on every log line for that request.

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

The test suite is hermetic: no network, no database, no external services.
`OC_`-prefixed environment variables are stripped before each test, so a stray
shell export cannot change the result.

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
```

See [`.env.example`](.env.example) for the full list, including the variables
reserved for later phases.

Misconfiguration fails at startup rather than at the first request. In
production the application additionally refuses to start with debug enabled,
non-JSON logging, wildcard CORS origins, or unset trusted hosts.

---

## Layout

```
backend/
├── app/
│   ├── main.py              ASGI entrypoint
│   ├── core/                cross-cutting infrastructure
│   │   ├── settings.py      typed configuration
│   │   ├── logging.py       structured logging + redaction
│   │   ├── observability.py OpenTelemetry bootstrap
│   │   └── errors.py        error taxonomy
│   ├── platform/            shared building blocks
│   │   ├── correlation.py   request/correlation identifiers
│   │   └── health.py        health check registry
│   ├── api/                 HTTP delivery layer
│   │   ├── application.py   application factory
│   │   ├── middleware.py    correlation, security headers, body limit
│   │   ├── exception_handlers.py
│   │   ├── dependencies.py
│   │   ├── schemas.py       error envelope, health responses
│   │   └── routes/          health/, v1/
│   └── modules/             business modules (empty until Phase 3)
└── tests/
    ├── unit/                pure logic, no I/O
    └── integration/         through the ASGI application
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
