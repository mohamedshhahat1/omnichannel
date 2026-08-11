# Deployment

> Local development, CI/CD and production topology. **Nothing is implemented yet** — no Dockerfiles, no Compose files, no workflows. This is the target design.

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

---

## 3. Container design

- Two images from one codebase: **api** and **worker** (the crawler later reuses the worker image with a restricted network and its own limits).
- Multi-stage builds, slim base, no build toolchain in the runtime layer.
- **Non-root user**, read-only root filesystem where possible, dropped capabilities, no Docker socket.
- Health checks defined per container; resource limits (CPU, memory) always set.
- Images are tagged with the immutable commit SHA — never deployed by `latest`.

---

## 4. CI (GitHub Actions)

Pipeline on every pull request and on `main`:

1. Checkout, set up Python, restore cache
2. Install locked dependencies
3. **Format check** (Ruff format)
4. **Lint** (Ruff)
5. **Type check** (MyPy)
6. **Unit tests**
7. **Integration tests** against service containers (PostgreSQL + Redis)
8. **Migration validation** — run the full upgrade path on an empty database, and check for model/migration drift
9. **Security** — dependency vulnerability scan, secret scanning, static analysis
10. **Docker build** (and image scan)
11. Coverage report

All checks are required. **A failing required check blocks merge and blocks deployment.** Branch protection on `main`: no direct pushes, review required, checks required.

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
