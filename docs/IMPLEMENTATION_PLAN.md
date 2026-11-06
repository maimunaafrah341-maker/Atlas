# Project Atlas — Implementation Plan

Status: Approved direction (revision 2). An integration is marked done only once it has been exercised against the real Sandbox or Make account. Paid plans and paid resources are excluded; every external service below is used on a free tier.

## 1. Milestones

Order: deterministic core first, real payment second, AI layered on top of verified services. We deploy early because PayPal webhooks and Make need a public URL.

| # | Milestone | Acceptance criteria |
|---|---|---|
| M0 | Scaffold, CI, database foundation, deploy configuration | Backend (FastAPI, config, SQLAlchemy engine for SQLite and PostgreSQL, Alembic baseline, JSON logging with redaction, `/healthz` + `/api/health`, SPA serving) and frontend (Vite, React, TS, Tailwind, health view) skeletons. Ruff, mypy, ESLint, tsc, Pytest and Vitest pass locally. CI workflow runs the backend on SQLite **and** PostgreSQL, the frontend checks, and a Docker build + container smoke test. Production refuses SQLite. `render.yaml` (free plan, Docker) and `.env.example` complete. Local smoke test passes. A deployed smoke test passes against the Render URL with Neon PostgreSQL. No secrets in the repo. |
| M0.5 | Audit of existing multilingual codebases | **Done**: [MULTILINGUAL_REUSE_AUDIT.md](MULTILINGUAL_REUSE_AUDIT.md). |
| M1 | Catalogue, inventory, shipping | Migration `0002` (additive); "Atlas Demo Outfitters" outdoor apparel seed (20 products, 60 variants, 100 translations, USD cents; non-English text flagged machine-translated); `GET /api/catalog/products` with filters, search (ADR-021), sorting and pagination; `GET /api/catalog/products/{slug}`; `GET /api/shipping/quote`; audited `adjust_stock` with the available-stock invariant; the fictional India-origin shipping zones (ADR-023); opt-in create-only startup seed (ADR-022); one error format; CI on PostgreSQL 18. Tests cover the stock invariant, English fallback, unknown country errors, seed idempotency and startup ordering, on both databases. |
| M2 | Cart, order snapshot, approval, reservations | Cart operations with validation errors; review creates an immutable snapshot and hash; approving with a stale hash is rejected; reservation TTL expiry releases stock (also lazily on read). A concurrency test (parallel approvals on the last unit) never oversells, run against PostgreSQL. |
| M3 | PayPal Sandbox (USD) | Create order, JS SDK buttons, server capture, webhook signature verification, dedup by event ID, reconciliation, mismatch → `needs_review`. Verified by a real Sandbox purchase by an **international Sandbox buyer** in USD on the Render URL, plus contract tests with recorded (redacted) fixtures covering duplicate, out-of-order, `PENDING`/`DENIED` and non-USD-mismatch cases. |
| M4 | Outbox and Make | Outbox in the same transaction; lease-based claim; HMAC + API key; exponential backoff with jitter; `max_attempts` → `dead` + exception; recovery on startup; manual retry. One Make scenario (webhook → dedup → router → Sheets / Gmail / Telegram → callback) exported to `make/`. Tests: simulated crash mid-delivery is redelivered after restart; duplicate delivery produces a single Make action (verified live); Make returning 500 produces scheduled retries with growing delays; Make down never blocks checkout; dead events appear in the dashboard API. |
| Gate before M5 | Product images | Decide image sourcing and licensing before the first product-card UI. M1 stores no images (D-11). |
| M5 | AI runtime and discovery | `LLMProvider` protocol, Gemini adapter (`gemini-3.8-flash` from env), fake adapter; `ShoppingIntent` extraction; orchestrator. Tests: an injected price in model output is rejected; unknown product IDs are dropped; invalid JSON triggers retry then fallback. An evaluation set of ≥15 prompts per language reaches ≥90% schema-valid output and ≥80% field accuracy against the real model (run on demand). |
| M6 | Knowledge RAG | `KnowledgeIndex` with PostgreSQL FTS and SQLite FTS5 implementations passing the same contract tests; per-language retrieval with query-translation fallback; cited answers; "I don't know" when retrieval is empty. Evaluation set of 10 questions per language: hit@3 ≥ 80% on PostgreSQL, with 0 answers lacking citations. |
| M7 | Multilingual layer | Detector library chosen by measurement (accuracy and `undetermined` rate per language on ≥30 short shopping messages per language); `LanguagePolicy`; manual switch endpoint and selector; protected-placeholder module and validator; UI catalogues for 5 locales with a CI parity check; PayPal SDK locale mapping. Tests S1–S12 from the audit §14.1, plus the placeholder tests in §3 below. Playwright run in `es` with a switch to `de`. |
| M8 | Merchant dashboard | Login; orders with payment timeline; inventory with low-stock flags; automation history (attempts, next retry, dead events) with manual retry; exception centre with resolve. Every list has empty, loading and error states. |
| M9 | Hardening and demo | Playwright end-to-end flow (chat → cart → approve → Sandbox pay → paid → Make callback visible) passes against the deployed app; log redaction test; demo script rehearsed, including warming the instance; README works from a fresh clone. |

## 2. External accounts, keys and configuration (all free tier)

| Item | Purpose | Needed from | Notes |
|---|---|---|---|
| GitHub repo `maimunaafrah341-maker/Atlas` | Source, CI | M0 | Public repo → GitHub Actions minutes free |
| **Neon** (free) PostgreSQL | Deployed database | M0 deploy | Copy the pooled or direct connection string with `sslmode=require` into Render as `DATABASE_URL` |
| **Render** (free web service) | Hosting | M0 deploy | Blueprint from `render.yaml`; no paid plan, no disk |
| **Google AI Studio** API key | `GEMINI_API_KEY` | M5 | `LLM_PROVIDER=gemini`, `LLM_MODEL=gemini-3.8-flash`. Check the free-tier rate limits and data-use terms; no PII is sent in any case |
| **PayPal Developer** account | Sandbox REST app | M3 | Sandbox **business** account (merchant, USD) and Sandbox **personal** account registered in a non-Indian country (international buyer) |
| `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` / `PAYPAL_WEBHOOK_ID` / `PAYPAL_API_BASE` | Orders API + webhook verification | M3 | Webhook URL `https://<render-host>/api/webhooks/paypal`; events `CHECKOUT.ORDER.APPROVED`, `PAYMENT.CAPTURE.COMPLETED`, `PAYMENT.CAPTURE.PENDING`, `PAYMENT.CAPTURE.DENIED`, `PAYMENT.CAPTURE.REFUNDED`; base `https://api-m.sandbox.paypal.com` |
| **Make.com** (Free plan) | One scenario with a router | M4 | Custom webhook with API-key auth, a Data Store for `event_id` dedup, and the three app connections below. Check the operation allowance against the demo volume |
| Google account → **Sheets** + **Gmail** connections in Make | Order and stock log; customer emails | M4 | Use a dedicated demo Google account. Make may require a custom Google OAuth client for personal Gmail accounts; check this during M4 |
| **Telegram** bot (via @BotFather) + merchant chat ID | Merchant alerts | M4 | The bot token lives only in Make, never in Atlas |
| `MAKE_WEBHOOK_URL`, `MAKE_WEBHOOK_API_KEY`, `MAKE_SIGNING_SECRET`, `MAKE_CALLBACK_SECRET` | Outbox delivery and callback auth | M4 | |
| `SESSION_SECRET`, `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` | Sessions and merchant login | M2/M8 | |
| `STORE_CURRENCY=USD`, `RESERVATION_TTL_MINUTES`, `PUBLIC_BASE_URL` | Store config | M1–M3 | |
| `OUTBOX_MAX_ATTEMPTS`, `OUTBOX_BACKOFF_BASE_SECONDS`, `OUTBOX_BACKOFF_CAP_SECONDS` | Retry policy | M4 | Defaults 8 / 30 / 1800 |

## 3. Biggest risks and how we test them

| Risk | Mitigation | Test |
|---|---|---|
| LLM states a price, stock level or order ID, or recommends a non-existent product | ADR-004 + ADR-018 placeholders | Validator unit tests with adversarial fake-provider outputs; Playwright asserts that displayed prices equal DB values |
| Translation alters protected values | Typed placeholders; multiset check; residual digits, currency markers and URLs rejected | Property tests with random token sets; injected `$19.99`, `19,99 US$`, `US$ 1.299,00`, `nur noch 2`, unknown `⟦PRODUCT:9⟧`, dropped `⟦SKU:1⟧`, altered URL; all five languages |
| Wrong language: short replies, brand names, mixed text | ADR-017 stickiness, thresholds, explicit preference wins | Audit §14.1 S1–S12; detector evaluation per language |
| Payment state wrong under duplicates, races or out-of-order events | Compare-and-set transitions, `PayPal-Request-Id`, unique `event_id`, amount/USD/`custom_id` checks | Contract tests replaying fixtures twice, webhook before capture response, mismatched amount or currency, `PENDING` → `COMPLETED` |
| Oversell between approval and capture | Reservations, CHECK constraint, expiry conflict → exception | Parallel-request test on PostgreSQL |
| SQLite/PostgreSQL drift | Portable types, naming conventions, dialect-isolated FTS, CI matrix | Every backend test runs on both databases in CI from M0 |
| Render free sleep and cold start | `/healthz` cheap; capture response is the primary payment path; outbox resumes on wake; warm before demo | Smoke test with a generous timeout; outbox restart test |
| Neon idle suspend dropping connections | `pool_pre_ping`, short recycle, connect timeout | Health check after idle period (manual, M0 deploy) |
| Lost or duplicated automation | Outbox with lease, backoff, dead letter, Make dedup | M4 tests (crash and restart, 500 storms, duplicate delivery) |
| Make, Gmail or Telegram setup friction | Single scenario; setup guide in `make/`; dedicated demo accounts | Live run per event type before the demo |
| Gemini rate limits or model availability | Timeouts, one retry, deterministic fallbacks; model configurable by env | Fake-provider failure tests; on-demand evaluation run |
| Prompt injection | No mutating LLM tools; content delimited as data; approval only through the UI | Injection prompts in the evaluation set must cause no state change |
| PII or secret leakage | Redaction filter, payload allow-lists, placeholder prompts | Log-capture test over a full flow; snapshot tests of Make payloads |
| Overclaiming payment support | ADR-019 wording in UI, README and demo script | Review checklist before submission |

## 4. Required for a convincing end-to-end demo

Must be real, on the deployed Render URL backed by Neon:
1. **Spanish shopper**: "Busco una chaqueta impermeable para senderismo, talla M, menos de 150 dólares, envío a Francia". The extracted-intent panel is visible, and the outdoor apparel product cards show database prices in USD, formatted for `es`.
2. A **knowledge question** ("¿Cuál es la política de devoluciones?") gets a cited answer, and a question with no answer in the docs gets an honest "I don't know".
3. **Cart validation**: requesting more than the available stock shows the localised error.
4. **Manual language switch** to German before review. The cart and amounts are unchanged.
5. **Review & approve** with deterministic shipping to FR, followed by **PayPal Sandbox** payment by the international Sandbox buyer, in USD.
6. **Verified paid** confirmation. The dashboard shows the order, the PayPal order/capture IDs and the webhook timeline.
7. **Make**: a Sheets row, the customer's Gmail confirmation and the merchant's Telegram alert all appear live. The low-stock alert fires, and callback status shows in the automation history.
8. **Exception centre**: a real failure (e.g. Make webhook temporarily disabled → retries → dead → exception → manual retry succeeds) or a Sandbox negative-testing denial.
9. The scope statement is shown honestly: Sandbox, USD, international buyer.

Not needed for the demo: stretch languages, refunds, semantic retrieval, or multi-merchant support.

## 5. Decisions resolved in revision 2

USD · Gemini 3.8 Flash via env · Neon free PostgreSQL + Render free · outdoor apparel · Make with Sheets, Gmail and Telegram · language precedence · protected placeholders · outbox reliability · PayPal demo scope.
