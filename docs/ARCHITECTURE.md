# Project Atlas — Architecture

Status: Approved direction (revision 2) · Companion docs: [PRODUCT_SPEC.md](PRODUCT_SPEC.md), [DECISIONS.md](DECISIONS.md), [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), [MULTILINGUAL_REUSE_AUDIT.md](MULTILINGUAL_REUSE_AUDIT.md)

## 1. Shape of the system

Atlas is a **modular monolith**. One FastAPI process serves both the API and the built React app, from a Docker image on a **Render free web service**. Deployed data lives in a **free managed PostgreSQL** database (Neon, ADR-002). That database is outside Render, so data survives restarts, redeploys and free-tier sleep. SQLite is used for local development and fast tests only, and the deployed app refuses to start with it. Background work (outbox dispatch, reservation expiry) runs as in-process asyncio loops whose state lives entirely in the database. The external parties are PayPal Sandbox, Gemini (behind a provider interface) and Make.

```
                ┌──────────────────────── Browser (UNTRUSTED) ───────────────────────┐
                │  React SPA: Shop+Chat · Language selector · Order Review ·          │
                │  PayPal JS SDK · Merchant UI                                        │
                └───────────────┬───────────────────────────────┬────────────────────┘
                                │ HTTPS JSON (session cookie)   │ PayPal popup (payer ↔ PayPal Sandbox)
┌───────────────────────────────▼──────── Atlas backend on Render free (TRUSTED) ─────────────────────┐
│ api/ routers ─► domain services (deterministic, own all state)                                       │
│   chat ─► i18n (detect → resolve) ─► ai/ runtime ──(placeholders, no PII, no prices)──► Gemini API   │
│              intent (validated) ─► catalog / knowledge / cart  ─► compose ─► placeholder validator   │
│   cart, checkout ─► orders (snapshot, state machine) ─► inventory (reserve/commit/release)           │
│                                    └► payments ─► integrations/paypal ──OAuth2──► PayPal Sandbox API │
│   webhooks/paypal ◄──────────────── signed webhook ◄─────────────────────────── PayPal Sandbox      │
│   admin/* (admin session) ─► read models, retry, resolve                                             │
│   integrations/make/callback ◄──── secret header ◄─────────────────────────────── Make              │
│ workers: outbox dispatcher ──API key + HMAC──► Make webhook → router → Sheets / Gmail / Telegram     │
│          reservation expiry                                                                          │
└──────────────────────────────┬───────────────────────────────────────────────────────────────────────┘
                               │ TLS (sslmode=require)
                     Managed PostgreSQL (Neon free)   ·   local/tests: SQLite file
```

## 2. Repository structure

Items marked † exist after M0; the rest arrive in later milestones.

```
atlas/
├── README.md †                  # setup, env, run, deploy, smoke tests
├── .env.example †               # every variable, no values
├── .gitignore † .gitattributes † .editorconfig † .dockerignore †
├── Dockerfile †                 # multi-stage: build SPA (Node) → Python runtime
├── render.yaml †                # one free Docker web service; DATABASE_URL is a secret
├── .github/workflows/ci.yml †   # backend (SQLite + PostgreSQL), frontend, Docker smoke
├── docs/ †
├── scripts/
│   ├── start.sh †               # migrate, then start uvicorn
│   └── smoke_test.py †          # stdlib-only HTTP smoke test (local or deployed URL)
├── backend/
│   ├── pyproject.toml †         # project metadata + ruff/mypy/pytest config
│   ├── requirements.txt †       # exact runtime pins   · requirements-dev.txt †
│   ├── alembic.ini † alembic/ † # migrations; dialect-specific SQL isolated in revisions
│   ├── app/
│   │   ├── main.py †            # app factory, SPA mount, lifespan (workers from M2/M4)
│   │   ├── config.py †          # pydantic-settings; secrets only from env
│   │   ├── db.py †              # engine per dialect, naming conventions, session
│   │   ├── logging.py †         # JSON logs + redaction filter
│   │   ├── api/health.py †      # /healthz, /api/health
│   │   ├── api/                 # chat, catalog, cart, checkout, paypal_webhooks, make_callbacks, admin
│   │   ├── domain/              # catalog, inventory, cart, orders, payments, shipping,
│   │   │                        #   knowledge, conversations, automation, exceptions
│   │   ├── ai/                  # provider.py, providers/{gemini,fake}.py, schemas, prompts,
│   │   │                        #   orchestrator, guards
│   │   ├── i18n/                # detect.py, policy.py, placeholders.py, catalogs/
│   │   ├── integrations/        # paypal/, make/
│   │   └── workers/             # outbox dispatcher, reservation expiry
│   ├── seed/                    # outdoor apparel catalogue + knowledge docs (per language)
│   └── tests/ † {unit, integration, contract, fixtures}
├── frontend/ †
│   ├── package.json † vite.config.ts † tsconfig.json † eslint.config.js †
│   └── src/
│       ├── App.tsx † main.tsx † index.css †
│       ├── lib/api.ts †         # typed API client
│       ├── features/health/ †   # M0 status view (loading / ok / degraded / error)
│       ├── features/{chat,catalog,cart,checkout,merchant}/
│       ├── i18n/locales/{en,es,fr,de,pt-BR}.json
│       └── components/          # shared UI states
├── e2e/                         # Playwright (from M7)
└── make/                        # exported scenario blueprint + setup guide (no secrets)
```

## 3. Data model (core tables)

All money is stored as an **integer in minor units (USD cents) plus an ISO 4217 code**. All timestamps are UTC. The schema uses only types that work in both SQLite and PostgreSQL. Constraint names follow a fixed naming convention, so migrations behave the same on both. Anything specific to one database (full-text indexes) is isolated in migrations that branch on the dialect.

| Table | Key fields | Notes |
|---|---|---|
| `products` / `product_translations` | id, slug (unique), category, status, sort_order · product_id, lang, name, description, `machine_translated`, derived `search_name`/`search_text` | Prices are never on translations. Unique (product, lang). Missing language → English fallback |
| `product_tags` (M1) | product_id, tag (closed vocabulary) | Feature/activity attributes, filterable portably (D-4) |
| `variants` | id, product_id, sku (unique), size, colour, price_minor, currency, stock_on_hand, stock_reserved, low_stock_threshold, status | `available = on_hand − reserved`; CHECK `stock_reserved <= stock_on_hand`; unique (product, size, colour) |
| `inventory_movements` | variant_id, delta_on_hand, delta_reserved, on_hand_after, reserved_after, reason, reservation_id, ref, note | Append-only audit, written in the same transaction as every stock change. Stock adjustments move only on-hand stock and have no `reservation_id`; reservation movements (`reservation_hold` / `_release` / `_expiry`) move only reserved stock and always name their reservation (M2) |
| `reservations` / `reservation_items` | public_id, cart_id, cart_version, status, created_at, expires_at, ended_at · variant_id, qty | Owned by a cart (ADR-024). active → released / expired (M2); at most one active per cart; never deleted. Item quantities are the immutable record used for release. No prices: the price snapshot belongs to the M3 order. M3 adds the commit state |
| `shipping_rates` / `shipping_rate_countries` (M1) | zone_code (unique), name, price_minor, currency, status · country_code (PK), shipping_rate_id | Flat rate per zone. A country belongs to at most one zone; unknown → `unsupported_destination` (D-1) |
| `knowledge_docs` / `knowledge_chunks` | doc: title, topic, lang · chunk: text | Index: PostgreSQL `tsvector` (GIN index, per-language text-search config) or SQLite FTS5 virtual table |
| `conversations` | session_id, ui_locale, `preferred_lang` (null = Auto), `current_lang` | §8 |
| `messages` | role, `original_text` (immutable), detected_lang, detect_confidence, response_lang, `lang_source`, retrieval_text | §8 |
| `intents` | message_id, json, schema_version, valid | Audit and demo panel |
| `carts` / `cart_items` | token_hash, version, created_at, updated_at, expires_at · variant_id, qty (1–10) | Anonymous cart identified by a cookie token, stored hashed (ADR-024). `version` serialises edits and reservations (§3.1). No prices stored; expires 30 days after the last change |
| `orders` | id (public ref), status, version_hash, currency, subtotal/shipping/total_minor, ship_country, lang | State machine §5 |
| `order_items` | order_id, variant_id, sku, name_snapshot, unit_price_minor, qty, line_total_minor | **Immutable** snapshot |
| `payments` | order_id, paypal_order_id, capture_id, status, amount_minor, currency, raw (redacted) | |
| `paypal_webhook_events` | event_id **UNIQUE**, type, verified, processed_at | Dedup key |
| `outbox_events` | id (UUID = idempotency key), type, payload, status, attempts, max_attempts, next_attempt_at, lease_until, last_error, created_at, delivered_at | §4.3 |
| `automation_deliveries` / `automation_callbacks` | event_id, attempt, http_status, duration_ms, error · event_id, make_status, detail | History view |
| `exceptions` | type, severity, order_id?, context, status, resolved_note | Exception centre |

### 3.1 Cart and reservation service contract (M2, ADR-024)
The schema enforces the shape of carts and reservations. The cart and reservation services (M2.2) must keep these rules:

Isolation is PostgreSQL's default, **READ COMMITTED**: each statement sees rows committed before it started, and an `UPDATE` that waited for a row lock re-checks its `WHERE` clause against the newly committed row. SQLite allows one writer at a time. Time comes from the injectable clock and is compared in SQL.

1. **Cart edits.** Every edit (set quantity, remove a line) first advances `carts.version` (`UPDATE carts SET version = version + 1 … WHERE id = :id`), which takes the cart row lock. **Only after that update** does it look up the cart's active reservation and release it, in the same transaction. Under READ COMMITTED, a reservation committed while the edit waited for the lock is visible to that later lookup. A lookup made before the update could miss it and leave a stale hold.
2. **Repeated reservation requests.** A reservation request first checks for an **active, unexpired** reservation with `cart_version = carts.version`. If one exists, it is returned unchanged. If the active reservation has `expires_at <= now`, it is expired and released first (rule 7), and the request continues with rule 3.
3. **New reservations.** Otherwise the request advances the version with a compare-and-set (`UPDATE carts SET version = version + 1 … WHERE id = :id AND version = :seen`) and creates a reservation for the new version.
4. **Lost races.** If the compare-and-set changes no row, another request won the race. Re-read the cart and its reservation: return the active, unexpired reservation if it matches the current version; otherwise return a conflict.
5. **Serialise around the cart row.** Cart edits, reservation creation and customer release all serialise on the cart row: the version update takes the row lock on PostgreSQL, and SQLite allows one writer. Cart contents and the active reservation are re-read **after** the lock or version update; a reservation is never built from a snapshot read before it.
6. **Customer release.** Releasing a reservation on request takes the cart row lock first (as in rule 1), then releases with a compare-and-set on the reservation (`… WHERE id = :id AND status = 'active'`).
7. **Expiry.** Expiry is a compare-and-set on the reservation: `UPDATE reservations SET status = 'expired', ended_at = :now WHERE id = :id AND status = 'active' AND expires_at <= :now`. **The successful active-to-expired transition and its stock release happen in one transaction.** If the compare-and-set changes no row, the reservation was already released or expired, or isn't due yet, and stock is **not** released again. The same no-row rule applies to customer release.
8. **Sweeps of other carts.** Expired reservations that belong to *other* carts (for example, those holding variants a new reservation needs) are swept in their own short transactions, one reservation each, **before** the current cart's lock is taken. A request never holds one cart's lock while taking another's, so two carts sweeping each other can't deadlock.
9. **Release quantities.** Reservation item quantities are the immutable record of what was held. Release and expiry use them; they are never recomputed from the current cart.
10. **Stock and audit together.** Every change to `variants.stock_reserved` writes an `inventory_movements` row with the matching `reservation_id` in the same transaction. The services maintain `stock_reserved = SUM(quantity of items in active reservations)`; the schema cannot check a cross-table sum, so the M2 tests assert it.

## 4. Key data flows

### 4.1 Chat turn (AI proposes, backend decides)
1. `POST /api/chat` stores the message with `original_text` unchanged.
2. **Detect, then resolve the language** (§8). This step makes no model call.
3. **Extract**: Gemini returns JSON validated against `ShoppingIntent`. The fields are canonical and language-neutral: closed enums, ISO codes and numbers. On failure: retry once, then use the keyword fallback.
4. **Execute deterministically**: catalogue search, knowledge search and/or a cart operation via domain services. These return structured results with IDs.
5. **Compose**: Gemini writes the reply in `response_lang` from a payload where every protected value is a typed placeholder (§8.3). The payload never contains prices or stock counts.
6. **Validate**: the placeholder validator checks the reply (§8.3). It also drops unknown product IDs and requires knowledge citations to reference retrieved chunks. On failure: retry once, then use a template reply from the message catalogue.
7. Respond with `{text_with_placeholders, product_cards[], cart, citations[], intent, language}`. The frontend substitutes placeholder values from the structured fields and formats them with `Intl` for the UI locale.

### 4.2 Checkout and payment (USD, PayPal Sandbox)
```
Customer        SPA                 Backend                              PayPal Sandbox
  │ "Review"  ─►│ POST /checkout/review ─► snapshot order (pending_approval), version_hash
  │ ◄─ review screen (server totals in USD, locale formatting)
  │ "Approve" ─►│ POST /checkout/approve {order_id, version_hash}
  │             │        ─► verify hash, reserve stock (TTL) ─► status approved
  │             │ SDK createOrder ─► POST /checkout/paypal-order
  │             │        ─► POST /v2/checkout/orders (USD, custom_id=order_id, items+breakdown, PayPal-Request-Id) ─►
  │ PayPal popup (international Sandbox buyer approves) ◄─────────────────────────────────────────────── │
  │             │ SDK onApprove ─► POST /checkout/capture
  │             │        ─► POST /v2/checkout/orders/{id}/capture (PayPal-Request-Id) ─►
  │             │        ◄─ capture result → verify status, amount, currency = USD, custom_id
  │             │        ─► one DB txn: order paid, reservation committed, stock decremented,
  │             │                       outbox(order.paid), outbox(stock.low)?
  │ ◄─ confirmation (status read from DB)
                         PayPal ── webhook PAYMENT.CAPTURE.* ─► verify signature via PayPal API
                                   ─► dedupe by event_id ─► reconcile (no-op if already paid)
```
The capture is triggered by the customer's action in the PayPal UI and executed by the backend. The LLM has no path to create, approve or capture a payment. The demo uses a Sandbox merchant account and an **international Sandbox buyer** checking out in USD. Atlas makes no claim about domestic Indian PayPal payments.

### 4.3 Automation: transactional outbox → Make
**Write.** Every state change that matters to automation (order paid, stock crossed below threshold, exception raised) inserts an `outbox_events` row **in the same database transaction**. The event can't exist without the state change, and vice versa.

**Claim (crash-safe, works on both databases).** The dispatcher claims due events with a conditional update, which acts as a lease:
`UPDATE outbox_events SET status='in_flight', lease_until=:now+60s, attempts=attempts+1 WHERE id=:id AND ((status='pending' AND next_attempt_at<=:now) OR (status='in_flight' AND lease_until<:now))`.
An event is claimed only if this update changes exactly one row.

**Deliver.** `POST` to the single Make webhook URL with:
- an `x-make-apikey` header,
- `X-Atlas-Signature: sha256=HMAC(secret, timestamp + "." + body)` and `X-Atlas-Timestamp` headers,
- a body of `{event_id, type, occurred_at, data}`,
- a 10-second timeout.

**Idempotency.** `event_id` is stable across retries. The Make scenario checks a Make Data Store keyed by `event_id` before running any action. On the Atlas side, callbacks and delivery records are keyed by `event_id` and attempt number.

**Retry.** On a non-2xx response, timeout or network error, the dispatcher sets `status=pending` and `next_attempt_at = now + min(base × 2^(attempts−1), cap) ± jitter` (base 30 s, cap 30 min). After `max_attempts` (default 8), `status=dead`. A dead event raises an `exceptions` row (`automation.delivery_failed`) and appears in the dashboard.

**Visibility.** Every attempt is written to `automation_deliveries`. The dashboard shows pending, in-flight, delivered and dead events with their last error, and offers **manual retry**, which resets `status=pending, attempts=0`.

**Recovery after restart or sleep.** All dispatcher state lives in the database. On startup the dispatcher immediately scans for due `pending` events and for `in_flight` events whose lease has expired (in flight when the process died). When a Render free instance sleeps, the dispatcher pauses. It resumes on the next inbound request, and any backlog is delivered then. This delay is acceptable because Make is never on the critical path. Delivery is at-least-once; the `event_id` check in Make makes the effect exactly-once.

**Make side.** One scenario: Custom Webhook → API-key check → HMAC and timestamp filter → Data Store dedup → **Router** by `type` → branches for Google Sheets, Gmail and Telegram → HTTP callback to Atlas. Using a single scenario keeps the Free plan's active-scenario allowance free for anything else.

## 5. Order state machine

```
pending_approval ─approve─► approved ─paypal order created─► awaiting_payment ─capture COMPLETED+verified─► paid
       │                       │                                   │  ├─ capture PENDING ─► payment_pending ─webhook COMPLETED─► paid
       └─expire/cancel─► cancelled   └─reservation expired─► expired   ├─ DENIED/FAILED ─► payment_failed (retry allowed → awaiting_payment)
                                                                       └─ amount/currency mismatch ─► needs_review (exception)
```
Transitions happen only in `orders` service functions, guarded by the current status (compare-and-set `UPDATE … WHERE status = ?`). The same guard works on SQLite and PostgreSQL, and it makes concurrent capture-response and webhook processing safe.

## 6. Trust boundaries

| Boundary | Trust | Controls |
|---|---|---|
| Browser → backend | Untrusted input | Pydantic validation; server recomputes every total; httpOnly SameSite session cookie; rate limit on chat; CSRF protection on admin and checkout mutations |
| Backend → Gemini | Output untrusted; provider sees prompt data | No PII, prices or payment data in prompts; protected values sent as placeholders; schema validation; placeholder validator; timeouts. The LLM has **no write tools** |
| User text and knowledge docs → LLM | Prompt-injection source | Delimited as data in prompts; injection can at worst produce bad wording, because every effect goes through validated deterministic calls and approval needs a UI click |
| PayPal → backend | Trusted only after verification | Webhook signature verified via `/v1/notifications/verify-webhook-signature`; capture details re-checked against the snapshot; never trust browser-reported payment status |
| Backend → Make | Make is a downstream consumer | Minimal payloads; HMAC + API key; Make failure never blocks checkout |
| Make → backend | Authenticated but low privilege | Shared-secret header; callbacks are append-only and can't mutate orders, payments or inventory |
| Backend → PostgreSQL | Trusted store over the network | TLS required; credentials only in the `DATABASE_URL` secret; never logged |
| Merchant UI → admin API | Authenticated admin | Env credential, session cookie, every admin action audited |
| Secrets | Server only | Env vars; redaction filter in logging; `.env` git-ignored; only `PAYPAL_CLIENT_ID` goes to the frontend |

## 7. Operational notes

- **Database URLs.** `postgres://` and `postgresql://` URLs are normalised to the `psycopg` (v3) driver. PostgreSQL connections use `pool_pre_ping` and short recycling, because Neon suspends idle compute and drops connections. SQLite uses WAL, `foreign_keys=ON` and `busy_timeout`.
- **Migrations.** Alembic runs `upgrade head` in `scripts/start.sh` before uvicorn starts, because the deploy depends on no paid pre-deploy hook. Atlas runs a single instance, so there are no concurrent migrators.
- **Health.**
  - `/healthz` is liveness only, without database access, and Render's health check uses it.
  - `/api/health` reports database reachability, dialect, latency and whether the schema is at the migration head. It returns 503 when the database is unreachable or the schema isn't at head.
- **Render free tier.** The service sleeps after inactivity. The first request after that wakes it, which takes up to about a minute. PayPal retries webhooks, and the capture response is the primary payment path, so a cold start delays but doesn't lose payment state. Before a live demo, warm the service with the smoke test.
- **Dual-database discipline.** CI runs the backend test suite against both SQLite and PostgreSQL. Full-text retrieval has one interface and two implementations, and both pass the same contract tests (M6).
- **Observability.** Structured JSON logs with a request ID and order ID, and no PII. The dashboard doubles as the operational view.

## 8. Language architecture

Detailed findings from the audit are in [MULTILINGUAL_REUSE_AUDIT.md](MULTILINGUAL_REUSE_AUDIT.md). This section is authoritative where the two differ.

### 8.1 Separate concepts
`ui_locale` (BCP-47, formatting only) · `conversation.preferred_lang` (explicit user choice, or null for Auto) · `conversation.current_lang` (conversation context) · `message.detected_lang` + `confidence` · `message.response_lang` + `lang_source` · `currency` (USD) · `ship_country`. None of these is derived from another.

### 8.2 Detection and resolution (separate modules, no model call)
- `i18n/detect.py` uses **statistical character n-gram language identification** limited to `en, es, fr, de, pt`. It does **no alphabet or script-based detection** for these languages, because all five use Latin script. It runs on the text after URLs, SKUs, numbers and emoji have been removed. When there are fewer than about 3 words or 15 letters, or the confidence is below the threshold, it returns `undetermined`. `pt` maps to `pt-BR`. The library will be chosen in M7 by measuring candidates on the Atlas evaluation set; nothing is installed in M0.
- `i18n/policy.py` is a pure function that chooses `response_lang` in this precedence order:
  1. **Explicit user selection** (`preferred_lang`). It always wins until the user selects Auto.
  2. **Conversation language** (`current_lang`). It stays the same unless the current message is detected confidently (≥ threshold, ≥ minimum words) as a *different supported* language. In that case `current_lang` is updated.
  3. `ui_locale`'s language, if supported.
  4. `en`.

  A detected but unsupported language keeps the current language and adds a localised "I can reply in …" notice. Every decision records `lang_source ∈ {user, conversation, detected_switch, ui_locale, default}`.
- **Manual switching.** The selector is always visible. Choosing a language sets `preferred_lang` server-side through `PATCH /api/conversations/{id}/language` and re-renders the UI chrome. Earlier messages are never rewritten or re-translated.
- Intent extraction, product search and response composition receive `response_lang` as an input. None of them performs language detection.

### 8.3 Protected-placeholder validation (`i18n/placeholders.py`)
Before any text goes to the model for composition or translation, protected values are replaced with typed, numbered tokens:

| Class | Token | Examples |
|---|---|---|
| Product ID | `⟦PRODUCT:n⟧` | internal product/variant IDs |
| SKU | `⟦SKU:n⟧` | `ATL-JKT-RAIN-M-BLU` |
| Size | `⟦SIZE:n⟧` | `M`, `EU 42`, `US 9` |
| Quantity | `⟦QTY:n⟧` | `2` |
| URL | `⟦URL:n⟧` | links in knowledge docs |
| Numeric value | `⟦NUM:n⟧` | `30` (days), `2` (layers) |
| Monetary amount | `⟦MONEY:n⟧` | always rendered from minor units + USD |

The validator accepts model output only if all of the following hold:
1. Every token is well-formed and belongs to the supplied set (no invented tokens).
2. For **translation**, the token multiset equals the input's. For **composition**, it is a subset.
3. After the tokens are removed, the text contains **no** digits, currency symbols or codes ($, US$, USD, €, R$…), URLs, or anything shaped like an SKU.

On rejection it retries once, then falls back to a catalogue template. Tokens are replaced only at render time, from structured data: money via `Intl.NumberFormat(locale, {style:"currency", currency:"USD"})`, and sizes as canonical labels from the database. Translation can therefore never alter IDs, SKUs, sizes, quantities, URLs, numbers, prices or payment data.

### 8.4 UI chrome
Typed message catalogues `frontend/src/i18n/locales/{en,es,fr,de,pt-BR}.json` with ICU plurals. CI fails on missing keys or mismatched placeholders. The PayPal SDK `locale` comes from `ui_locale`.
