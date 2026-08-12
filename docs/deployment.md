# Deployment

> Local development, CI/CD and production topology. **Phase 2 implements the local infrastructure foundation** — Dockerfile, Compose services for API/PostgreSQL/Redis/worker/beat/migrate, a one-shot Alembic migration container, and a non-root runtime image. **CI is implemented** (`.github/workflows/ci.yml`; see §4). CD remains future work.
>
> **Phase 3 adds no new infrastructure.** It adds a second migration and a block of authentication configuration, some of which the application refuses to start without in production — see §2.1.

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

**Phase 3:** the `migrate` container now applies `0002_identity_access` as well as `0001_initial_infra`, creating the identity tables and seeding the permission and role catalogue. That ordering matters and is already enforced — the API starts against a schema that exists, and authentication would fail on the first request otherwise. The mail catcher profile is still unused: `email_tokens` rows can be created, but no delivery path exists to send them anywhere.

### 2.1 Authentication configuration and production floors

Phase 3 introduces the `OC_AUTH__` settings section. Full list with defaults in `backend/.env.example`; the ones that matter at deploy time:

| Variable | Local default | Production requirement |
|---|---|---|
| `OC_AUTH__COOKIE_SECURE` | `false` | **must be `true`** |
| `OC_AUTH__SESSION_COOKIE_NAME` | `oc_session` | **must start with `__Host-`** |
| `OC_AUTH__CSRF_COOKIE_NAME` | `oc_csrf` | **must start with `__Host-`** |
| `OC_AUTH__ARGON2_MEMORY_KIB` | `65536` | **must be ≥ 65536** |
| `OC_AUTH__ARGON2_TIME_COST` | `3` | **must be ≥ 3** |
| `OC_AUTH__ARGON2_PARALLELISM` | `4` | tune to available cores |
| `OC_AUTH__SESSION_IDLE_TTL_SECONDS` | `604800` | policy decision |
| `OC_AUTH__SESSION_ABSOLUTE_TTL_SECONDS` | `2592000` | policy decision |
| `OC_AUTH__MAX_FAILED_LOGINS` | `10` | policy decision |
| `OC_AUTH__LOCKOUT_SECONDS` | `900` | policy decision |

The five marked in bold are **floors, not warnings**. With `OC_ENVIRONMENT=production` the settings validator raises at startup and the container fails its health check rather than serving traffic with a weaker configuration.

This is deliberate, and worth knowing before an incident rather than during one: if someone lowers Argon2 cost to make a slow login faster, production will refuse to boot. That is the correct outcome. The failure message names the offending setting. Lowering the cost is legitimate in `development` and `test` — the test suite does exactly that, because hashing at production cost in every fixture would make the suite unusably slow — and illegitimate in production, which is precisely the distinction the validator encodes.

The `__Host-` prefix requires HTTPS and forbids a `Domain` attribute, so **the dashboard must be served from the same origin as the API**. Plan the frontend hosting around that constraint (`architecture.md` §12, question 6).

---

## 3. Container design

- Two images from one codebase: **api** and **worker** (the crawler later reuses the worker image with a restricted network and its own limits).
- Multi-stage builds, slim base, no build toolchain in the runtime layer.
- **Non-root user**, read-only root filesystem where possible, dropped capabilities, no Docker socket.
- Health checks defined per container; resource limits (CPU, memory) always set.
- Images are tagged with the immutable commit SHA — never deployed by `latest`.

**Implemented in Phase 2:** `backend/Dockerfile` builds a multi-stage Python image, installs dependencies into a virtual environment, copies the application as a non-root user, and exposes port 8000 with a liveness healthcheck.

**Phase 3 note on resource limits:** Argon2id is configured to use 64 MiB per hash. Concurrent logins multiply that, so the API container's memory limit must leave headroom above the steady-state working set — roughly `argon2_memory_kib × expected concurrent logins` on top of it. Setting a limit that ignores this turns a burst of sign-ins into an OOM kill.

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

**Phase 3:** no workflow change was needed or made. Job 5 already runs `alembic upgrade head` before the suite, so it picks up `0002` and its seeded rows automatically, and the identity integration tests are written to fit that contract — they create no tables, drop nothing and truncate nothing, so the unprivileged `oc_app` role is sufficient and each test rolls back its own transaction. Phase 3 added two dependencies (`argon2-cffi`, `httpx`) to `pyproject.toml` and `requirements.lock`; nothing else about the pipeline moved. No gate was loosened, skipped or reconfigured.

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

**Phase 3 as a worked example.** `0002_identity_access` only creates tables, indexes and reference rows. It alters nothing that already exists, so a Phase 2 container keeps running correctly against the upgraded schema — it simply ignores eleven tables it does not know about. Rollback is therefore a plain image redeploy with no down migration, and no data is lost by leaving the tables in place. This is what the expand-only rule buys, and it is the pattern every later migration should follow.

---

## 6. Production topology — launch

One adequately provisioned server running Docker Compose:

```
            Internet
               │ 443
            [ NGINX ]  TLS, headers, rate limit, body caps
               │
     ┌────────────────────────┐
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

**NGINX must forward the client address** (`X-Forwarded-For` / `X-Real-IP`) and the application must be configured to trust it. Session rows and audit records store the caller's IP, and without correct forwarding every record will show the proxy's address, which silently destroys the value of the audit log for exactly the investigations it exists to support.

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

Phase 3 keeps that property. Sessions are server-side but stored in shared PostgreSQL with a shared Redis cache, so step 3 needs no sticky sessions and no session affinity at the load balancer.

---

## 8. Zero-downtime considerations

At launch a few seconds of downtime during deploy is acceptable and should be stated openly. To remove it later: run two API containers and shift NGINX upstreams after `/readyz` passes; ensure migrations are backward compatible; drain workers with a graceful shutdown period so in-flight tasks finish or requeue. Because the outbox holds unpublished work durably, a worker killed mid-deploy loses nothing.

Sessions survive a deploy without any special handling — they live in PostgreSQL, not in process memory, so restarting every API container signs nobody out.

---

## 9. Secrets in deployment

Secrets are injected as environment variables from a protected `.env` (0600, root-owned) or from a secret manager. They are never baked into images, never committed, and never printed in deploy logs. Rotation procedures live in `operations.md`.

**Phase 2 note:** `backend/.env.example` contains only local-development example values. The committed PostgreSQL role bootstrap file is local-only and must not be copied to production credential management.

**Phase 3 note — no new deployment secret was introduced.** This is a direct consequence of ADR-0009 choosing opaque sessions over JWTs: there is no signing key, so there is no signing key to distribute, protect or rotate. Every session token and API key is an independent random value that exists only as a SHA-256 digest in the database. Nothing under `OC_AUTH__` is confidential — the whole block is tuning and policy, and can be committed to configuration management in the clear.

The operational consequence is that credential rotation is a database operation, not a deploy: revoking a key or signing out every user is described in `operations.md`, needs no restart, and takes effect on the next request.

---

## 10. Launch deployment checklist

- [ ] TLS with auto-renewal verified
- [ ] Security headers verified
- [ ] Firewall and SSH hardening verified
- [ ] Resource limits and restart policies on every container
- [ ] API memory limit sized for Argon2 under concurrent logins (§3)
- [ ] Health checks wired to the deploy gate
- [ ] Exactly one beat instance
- [ ] Migrations run automatically and block on failure
- [ ] Rollback rehearsed
- [ ] Encrypted off-server backups running and verified (`operations.md`)
- [ ] Monitoring, alerting and error tracking live
- [ ] Log rotation configured
- [ ] Runbooks published and linked from alerts
- [ ] `OC_AUTH__COOKIE_SECURE` true and both cookie names `__Host-` prefixed (§2.1)
- [ ] Argon2 parameters benchmarked on the production host and at or above the floors
- [ ] NGINX forwards the client IP and the application trusts it (§6)
- [ ] Rate limiting in front of `/api/v1/auth/login` — **currently only per-account lockout exists; this gap must be closed before public exposure**
