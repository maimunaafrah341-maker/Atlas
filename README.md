# Project Atlas

Multilingual AI commerce copilot for global small businesses: conversational product discovery, grounded merchant knowledge, validated carts, PayPal Sandbox checkout and Make.com automation. *Atlas* is an internal codename.

**Live demo:** https://atlas-uema.onrender.com/ (Render free plan: the first request after a quiet period can take up to a minute while the service wakes up). Screenshots: [Demo](#demo).

**Status: M3.2 (PayPal Sandbox payments).** On top of the M0 foundation, this build has:
- **Catalogue (M1):** the demo catalogue (20 products, 60 variants, 5 languages), deterministic search, a product detail API, audited inventory with a never-negative available-stock invariant, and shipping-rate lookup.
- **Cart (M2):** a cookie-identified cart API with time-limited stock reservations ([ADR-024](docs/DECISIONS.md)), and the frontend shopping bag.
- **Orders (M3.1):** an immutable, hashed order snapshot that adopts the cart's reservation as its stock hold ([ADR-025](docs/DECISIONS.md)).
- **PayPal Sandbox payments (M3.2):** the backend creates the PayPal order, captures it on the server, verifies the capture against PayPal's own record, reconciles unknown outcomes, and finalizes each payment once ([ADR-026](docs/DECISIONS.md)). It is verified with a real Sandbox purchase (see [Demo](#demo)). Sandbox only, USD only.

**Not implemented yet:**
- the frontend checkout step (the PayPal JavaScript buttons, M3.4); today a payment is driven through the API;
- PayPal webhooks (M3.3);
- Make.com automation;
- the AI and retrieval features.

See [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md).

> **Demo data disclosure.** "Atlas Demo Outfitters" is a fictional, temporary brand. Products, specifications and prices are invented. The es, fr, de and pt-BR product text is **machine-generated and not reviewed by native speakers**; it is flagged `machine_translated` in the data and API. Shipping rates are **fictional demo pricing assumptions, not real or carrier-validated shipping costs**. The fictional merchant ships from India to international destinations only. This story detail makes no claim about PayPal account capabilities.

| Doc | Purpose |
|---|---|
| [PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) | What we're building and the rules it must follow |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | Structure, data flow, trust boundaries, language architecture |
| [DECISIONS.md](docs/DECISIONS.md) | Architecture decisions and the alternatives we rejected |
| [IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Milestones, accounts, risks, demo |
| [MULTILINGUAL_REUSE_AUDIT.md](docs/MULTILINGUAL_REUSE_AUDIT.md) | Audit of prior multilingual code |

## Demo

Screens 1–6 were captured on 2026-11-12 from the app at the deployed commit (`ae6990e`), running locally against a freshly seeded database; screen 6 is PayPal's own **Sandbox** checkout for an order the Atlas backend created. Screens 7–8 are the evidence of the end-to-end Sandbox purchase run the same day: one Ridgeline Rain Shell (S, navy), USD 129.00 plus USD 20.00 shipping to FR, **USD 149.00** in total, with test accounts and no real money. The backend captured the approved order on the server, checked the capture against PayPal's record ([ADR-026](docs/DECISIONS.md)), committed the stock reservation and emptied the bag.

| | |
|---|---|
| **1. Homepage**: establishes the visual identity.<br>![Atlas homepage](docs/screenshots/demo/01-homepage.png) | **2. Shop catalogue**: the product collection, with search and filters.<br>![Shop catalogue](docs/screenshots/demo/02-shop-catalogue.png) |
| **3. Product detail**: real variant selection (colour, size, live stock state).<br>![Product detail with navy, size M selected](docs/screenshots/demo/03-product-detail.png) | **4. Shopping bag**: quantity and server-computed pricing.<br>![Shopping bag](docs/screenshots/demo/04-shopping-bag.png) |
| **5. Active reservation**: the bag's stock is held, with a countdown.<br>![Bag with an active stock reservation](docs/screenshots/demo/05-active-reservation.png) | **6. PayPal checkout**: the USD 149.00 order on PayPal's Sandbox checkout.<br>![PayPal Sandbox checkout showing USD 149.00](docs/screenshots/demo/06-paypal-checkout.png) |
| **7. Atlas paid order**: the backend's final result, order `paid`.<br>![Atlas order with status paid](docs/screenshots/demo/07-atlas-paid-order.png) | **8. PayPal Business activity**: independent evidence, payment **Completed**.<br>![PayPal Sandbox business activity: USD 149.00, Completed](docs/screenshots/demo/08-paypal-business-activity.png) |

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
.venv/Scripts/python -m app.seed                               # demo catalogue + shipping zones (create-only, idempotent)
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
- the backend checks on **SQLite and PostgreSQL 18**;
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

## Catalogue and shipping API (M1)

| Endpoint | Purpose |
|---|---|
| `GET /api/catalog/products` | Search and filter. Parameters: `q`, `lang` (`en es fr de pt-BR`), `category`, `tag` (all must match), `size`, `colour`, `min_price_minor`, `max_price_minor`, `in_stock`, `sort` (`featured relevance price_asc price_desc`), `page`, `page_size` (≤ 50) |
| `GET /api/catalog/products/{slug}?lang=` | One product with its active variants |
| `GET /api/shipping/quote?country=FR` | Flat zone rate in USD cents; an unsupported country returns `unsupported_destination` |

- **Money:** always `{"amount_minor": <int cents>, "currency": "USD"}`.
- **Price range:** in search results, `price_range` covers only the variants matching the filters (those with `matches_filters: true`). `price_asc` sorts by its minimum and `price_desc` by its maximum. The product detail range covers all active variants.
- **Stock:** shown only as `in_stock`, `low_stock` or `out_of_stock`, never as a count.
- **Errors:** every error is `{"error": {"code", "message", "details"}}`. Server errors (5xx) return a generic message and `details: [{"reference": "<id>"}]`; the real error is logged once under that reference and never sent to the client.
- **Search limitation:** search matches whole words and word starts, with no stemming ([ADR-021](docs/DECISIONS.md)).

## Seeding

`python -m app.seed` creates only missing rows. It never deletes anything, never writes stock to existing variants, runs in a single transaction, and refuses to run before migrations. `--update` also refreshes names, descriptions and prices; it's for local use only. In the container, `SEED_CATALOG_ON_START=1` runs the create-only seed after migrations; a failure stops startup ([ADR-022](docs/DECISIONS.md)).

## Deploying (free tier only)

1. **Database.** Create a free **Neon** project in **AWS US West 2 (Oregon)**, the same region as the Render service (`region: oregon` in `render.yaml`). Copy its connection string, which must include `sslmode=require`.
2. **Code.** Push this repository to GitHub (`maimunaafrah341-maker/Atlas`).
3. **Render.** Choose *New → Blueprint* and select the repo. Render reads [render.yaml](render.yaml), which defines one **free** Docker web service. When prompted, paste the Neon URL as `DATABASE_URL`.
4. **Startup.** On boot, the container runs `alembic upgrade head`, then the create-only seed if `SEED_CATALOG_ON_START=1`, then starts uvicorn. Any failure stops startup. Production refuses to start without a PostgreSQL `DATABASE_URL`.
5. **Verify.** Run the deployed smoke test above.

Free-tier behaviour: the service sleeps after inactivity, and the first request wakes it, which can take about a minute. Data is in Neon, so restarts and redeploys don't lose it.

## License

[MIT](LICENSE)

## Security

Secrets come only from environment variables, and `.env` is git-ignored. Logs are JSON with redaction of credentials, tokens and email addresses. Settings validation errors never echo input values.
