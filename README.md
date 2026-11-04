# Project Atlas

Multilingual AI commerce copilot for global small businesses: conversational product discovery, grounded merchant knowledge, validated carts, PayPal Sandbox checkout and Make.com automation. *Atlas* is an internal codename.

**Status: M0 (foundation).** This build contains scaffolding, configuration, the database and migration foundation, health endpoints, CI and deployment configuration. **No AI, retrieval, checkout, PayPal or Make features are implemented yet.** See [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

| Doc | Purpose |
|---|---|
| [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) | What we're building and the rules it must follow |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Structure, data flow, trust boundaries, language architecture |
| [DECISIONS.md](docs/DECISIONS.md) | Architecture decisions and the alternatives we rejected |
| [IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Milestones, accounts, risks, demo |
| [MULTILINGUAL_REUSE_AUDIT.md](docs/MULTILINGUAL_REUSE_AUDIT.md) | Audit of prior multilingual code |

## Stack

FastAPI · SQLAlchemy 2 + Alembic · PostgreSQL (deployed, Neon free) / SQLite (local, tests) · React 19 + Vite 8 + TypeScript 6 + Tailwind 4 · Pytest · Vitest · Docker on Render (free plan).

## Prerequisites

- Python **3.11**
- Node **24** and npm

## Local development

```sh
# Backend
cd backend
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt   # macOS/Linux: .venv/bin/python
.venv/Scripts/alembic upgrade head                             # creates backend/var/atlas.db (SQLite)
.venv/Scripts/python -m uvicorn app.main:app_factory --factory --reload --port 8000

# Frontend (second terminal) — proxies /api to 127.0.0.1:8000
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. Copy `.env.example` to `.env` to override settings. With no `DATABASE_URL`, development uses SQLite at `backend/var/atlas.db`.

> **OneDrive note:** this folder is under OneDrive. Syncing `frontend/node_modules` and `backend/.venv` is slow and can lock files. Consider excluding them from sync, or moving the repo outside OneDrive.

## Checks

```sh
cd backend
.venv/Scripts/ruff check . && .venv/Scripts/ruff format --check . && .venv/Scripts/python -m mypy
.venv/Scripts/python -m pytest                        # SQLite
TEST_DATABASE_URL=postgresql://user:pass@localhost:5432/atlas_test .venv/Scripts/python -m pytest  # PostgreSQL (database is reset)

cd frontend
npm run lint && npm run typecheck && npm test && npm run build
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs four checks:
- the backend checks on **SQLite and PostgreSQL 16**;
- the frontend checks;
- a production-mode **Docker** container against PostgreSQL, which runs the smoke test and then repeats it after a restart;
- a check that production refuses SQLite.

## Smoke test

`scripts/smoke_test.py` uses only the Python standard library.

```sh
# Serve the built SPA from the backend, then:
python scripts/smoke_test.py http://127.0.0.1:8000 --expect-dialect sqlite
# Deployed (allows ~2 min for a sleeping free instance to wake):
python scripts/smoke_test.py https://<service>.onrender.com --expect-dialect postgresql
```

## Health endpoints

- `GET /healthz` checks liveness only and doesn't touch the database. Render's health check uses it.
- `GET /api/health` checks readiness: database reachability, dialect, latency, and whether the schema is at the migration head. It returns 503 when degraded.

## Deploying (free tier only)

1. **Database.** Create a free **Neon** project in **AWS US West 2 (Oregon)**, the same region as the Render service (`region: oregon` in `render.yaml`). Copy its connection string, which must include `sslmode=require`.
2. **Code.** Push this repository to GitHub (`maimunaafrah341-maker/Atlas`).
3. **Render.** Choose *New → Blueprint* and select the repo. Render reads [render.yaml](render.yaml), which defines one **free** Docker web service. When prompted, paste the Neon URL as `DATABASE_URL`.
4. **Startup.** On boot, the container runs `alembic upgrade head` and then starts uvicorn. Production refuses to start without a PostgreSQL `DATABASE_URL`.
5. **Verify.** Run the deployed smoke test above.

Free-tier behaviour: the service sleeps after inactivity, and the first request wakes it, which can take about a minute. Data is in Neon, so restarts and redeploys don't lose it.

## License

[MIT](LICENSE)

## Security

Secrets come only from environment variables, and `.env` is git-ignored. Logs are JSON with redaction of credentials, tokens and email addresses. Settings validation errors never echo input values.
