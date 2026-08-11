# Deployment

> Local development, CI/CD and production topology. **Phase 2 implements the local infrastructure foundation** — Dockerfile, Compose services for API/PostgreSQL/Redis/worker/beat/migrate, a one-shot Alembic migration container, and a non-root runtime image. **CI is implemented** (`.github/workflows/ci.yml`; see §4). CD remains future work.

---

## 1. Environments

| Environment | Purpose | Data |
|---|---|---|
| `local` | Development on a laptop via Compose | Seeded fake data |
| `ci` | Ephemeral test execution | Throwaway services |
| `staging` | Pre-production verification | Anonymised or synthetic |
| `production` | Real customers | Real |

Configuration comes from environment variables validated by a settings object at startup; the process refuses to boot on invalid or missing required configuration. Sandbox provider credentials are used everywhere except production.

---

## 2. Local development (target)

Minimum services only — do not force engineers to run infrastructure they do not need:

| Service | Included by default |
|---|---|
| API (FastAPI, hot reload) | Yes |
| PostgreSQL (with pgvector) | Yes |
| Redis | Yes |
| Celery worker | Yes |
| Celery beat | Yes (needed for the outbox dispatcher) |
| MinIO (S3-compatible) | Yes |
| NGINX | Optional profile |
| Prometheus / Grafana | Optional profile |
| Mail catcher | Optional profile |

Developer workflow: `docker compose up` → run migrations → seed → develop. Provider webhooks are exercised locally with a tunnel or with recorded fixture replay. A `Makefile` (or `just`) wraps the common commands so the workflow is one line each.

**Implemented in Phase 2:** the committed `docker-compose.yml` starts `postgres`, `redis`, `migrate`, `api`, `worker`, and exactly one `beat` service. The migration container must finish successfully before API and worker processes start. The worker listens on `critical`, `default`, and `background` queues.

---

## 3. Container design

- Two images from one codebase: **api** and **worker** (the crawler later reuses the worker image with a restricted network and its own limits).
- Multi-stage builds, slim base, no build toolchain in the runtime layer.
- **Non-root user**, read-only root filesystem where possible, dropped capabilities, no Docker socket.
- Health checks defined per container; resource limits (CPU, memory) always set.
- Images are tagged with the immutable commit SHA — never deployed by `latest`.

**Implemented in Phase 2:** `backend/Dockerfile` builds a multi-stage Python image, installs dependencies into a virtual environment, copies the application as a non-root user, and exposes port 8000 with a liveness healthcheck.

---

## 4. CI (GitHub Actions)

**Implemented:** `.github/workflows/ci.yml` runs on every pull request and on pushes to `main` and `phase-2-infrastructure`, with least-privilege permissions (`contents: read`), pip caching keyed on `backend/requirements.lock`, no secrets, no registry pushes and no deployment. Each check is a separate required job:

1. **Ruff lint** (`ruff check .`)
2. **Ruff format** (`ruff format --check .`)
3. **MyPy** (`mypy`, strict)
4. **Unit tests** (`pytest -m "not integration"`)
5. **Integration tests** (`pytest -m "integration"`) against real service containers — `pgvector/pgvector:pg16` and `redis:7-alpine`, the same images as Compose — using the committed role bootstrap (`infra/postgres/init/10-roles.sql`) and `alembic upgrade head` first. SQLite is never substituted; Redis is never faked. The database is ephemeral with CI-only credentials; no repository secrets are used.
6. **Docker build** of the production `backend/Dockerfile` (build only; nothing is pushed)
7. **Compose validation** (`docker compose config`; the Compose file interpolates no environment variables, so no dummy values are required)

Python 3.13 is used, matching `backend/Dockerfile` (`python:3.13-slim`) within the `>=3.12,<3.14` range declared by `backend/pyproject.toml`. Dependencies install from `backend/requirements.lock`, the documented temporary offline-authored pin set; transitive dependencies float until the lock is regenerated in a networked environment.

**Interpreting failures:** a red job fails the pipeline and blocks merge. Reproduce locally with the same command (`cd backend && ruff check .`, `ruff format --check .`, `mypy`, `pytest`, or `docker build -t omnichannel-backend:ci backend`, `docker compose config`). **CI results are verified by the human operator; automated agents (including Opus) must not access GitHub Actions results or request CI credentials or tokens.**

Still future work: migration-drift validation, dependency vulnerability scanning, secret scanning, static analysis, image scanning and coverage reporting. Branch protection on `main` (no direct pushes, review required, checks required) is configured by the repository owner.

All checks are required. **A failing required check blocks merge and blocks deployment.**

---

## 5. CD

```
merge to main
  → CI passes
  → build immutable images tagged with the commit SHA
  → push to the registry
  → deploy to staging → migrations → smoke tests
  → manual approval gate
  → deploy to production:
       pull images
       run Alembic migrations (expand-only, backward compatible)
       start new containers, wait for /readyz
       shift traffic, stop old containers
       post-deploy health verification
  → on failure: automatic rollback to the previous image tag
```

**Migration rules at deploy time:** migrations run before the new version serves traffic; they must be compatible with the previous version still running; a failed migration aborts the deploy; destructive contract steps ship in a later, separate release (`database.md` §8).

**Rollback:** redeploy the previous image tag. Because migrations are expand-only within a release, the previous version remains schema-compatible. Data-affecting rollbacks follow the restore runbook in `operations.md`.

---

## 6. Production topology — launch

One adequately provisioned server running Docker Compose:

```
            Internet
               │ 443
            [ NGINX ]  TLS, headers, rate limit, body caps
               │
     ┌──────────────────────────┐
  [ api x N ]                 [ worker x M ]  [ beat x 1 ]
     │                              │              │
     └────────── internal docker network ─────────┘
                    │                    │
              [ PostgreSQL ]        [ Redis ]
                    │
              [ object storage — external ]
              [ Prometheus + Grafana ]
```

Rules: only NGINX is published; all other ports bind to the internal network. Every container has `restart: unless-stopped`, a health check and resource limits. **Exactly one beat instance** — duplicated beat means duplicated scheduled work.

Host hardening: firewall default-deny inbound (22/80/443 only), SSH by key with root login disabled, automatic security updates, monitoring agent, log rotation, and separate volumes for database data and object cache.

---

## 7. Production topology — growth path

Each step is triggered, not scheduled (`architecture.md` §10):

1. Vertical scale
2. Separate worker host (workers pull from the same Redis)
3. Separate API hosts behind NGINX or a managed load balancer
4. Managed PostgreSQL (backups, PITR, HA handled by the provider)
5. Managed Redis
6. Dedicated crawler host with restricted egress
7. Read replicas, PgBouncer, partitioning

**Nothing in the launch design blocks these**: the application is stateless, configuration is environment-driven, all state lives in PostgreSQL/Redis/object storage, and workers are queue-routed already.

---

## 8. Zero-downtime considerations

At launch a few seconds of downtime during deploy is acceptable and should be stated openly. To remove it later: run two API containers and shift NGINX upstreams after `/readyz` passes; ensure migrations are backward compatible; drain workers with a graceful shutdown period so in-flight tasks finish or requeue. Because the outbox holds unpublished work durably, a worker killed mid-deploy loses nothing.

---

## 9. Secrets in deployment

Secrets are injected as environment variables from a protected `.env` (0600, root-owned) or from a secret manager. They are never baked into images, never committed, and never printed in deploy logs. Rotation procedures live in `operations.md`.

**Phase 2 note:** `backend/.env.example` contains only local-development example values. The committed PostgreSQL role bootstrap file is local-only and must not be copied to production credential management.

---

## 10. Launch deployment checklist

- [ ] TLS with auto-renewal verified
- [ ] Security headers verified
- [ ] Firewall and SSH hardening verified
- [ ] Resource limits and restart policies on every container
- [ ] Health checks wired to the deploy gate
- [ ] Exactly one beat instance
- [ ] Migrations run automatically and block on failure
- [ ] Rollback rehearsed
- [ ] Encrypted off-server backups running and verified (`operations.md`)
- [ ] Monitoring, alerting and error tracking live
- [ ] Log rotation configured
- [ ] Runbooks published and linked from alerts
