# Project Atlas — Architecture

Status: Approved direction (revision 2) · Companion docs: [PRODUCT_SPEC.md](PRODUCT_SPEC.md), [DECISIONS.md](DECISIONS.md), [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md), [MULTILINGUAL_REUSE_AUDIT.md](MULTILINGUAL_REUSE_AUDIT.md)

## 1. Shape of the system

Atlas is a **modular monolith**. One FastAPI process serves both the API and the built React app, from a Docker image on a **Render free web service**. Deployed data lives in a **free managed PostgreSQL** database (Neon, ADR-002). That database is outside Render, so data survives restarts, redeploys and free-tier sleep. SQLite is used for local development and fast tests only, and the deployed app refuses to start with it. Background work (outbox dispatch, M4) runs as an in-process asyncio loop whose state lives entirely in the database. Reservation expiry has no loop: it is applied lazily when a cart or the stock it needs is next used (ADR-024). The external parties are PayPal Sandbox, Gemini (behind a provider interface) and Make.

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
│          (reservation expiry is lazy: no worker, ADR-024)                                            │
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
│   │   └── workers/             # outbox dispatcher (reservation expiry is lazy, ADR-024)
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

Isolation is PostgreSQL's default, **READ COMMITTED**: each statement sees rows committed before it started, and an `UPDATE` that waited for a row lock re-checks its `WHERE` clause against the newly committed row. SQLite allows one writer at a time; every write transaction starts with an `UPDATE` or `INSERT`, so it holds the write lock before it reads anything it acts on. Time comes from the injectable clock and is compared in SQL.

**Units of work.** Every cart and reservation write runs in `app.db.unit_of_work`: one transaction that commits on success and rolls back on any exception, and that refuses to join a transaction already open. A domain primitive that fails after a partial write (a multi-item hold with one item short) marks the transaction **rollback-only** before raising, so the partial work can't be committed even if a caller catches the error: `unit_of_work` checks the mark itself, and the project's session factory refuses such a commit. The mark is cleared when the transaction ends.

**Global lock order.** A transaction that locks more than one row takes them in this order: **(1) the cart row, (2) the reservation row, (3) variant rows in ascending `variant_id`**. It may skip a level but never goes back to an earlier one. The expiry sweep (rule 8) is the one documented exception: it never locks a cart, only its reservation and then that reservation's variants. No transaction waits for a cart while holding a reservation or variant lock, and variants are always locked in the same order, so these transactions cannot deadlock each other.

1. **Cart edits.** Every edit (set quantity, remove a line) first advances `carts.version` (`UPDATE carts SET version = version + 1 … WHERE id = :id`), which takes the cart row lock. **Only after that update** does it look up the cart's active reservation and end it (rule 7), in the same transaction. Under READ COMMITTED, a reservation committed while the edit waited for the lock is visible to that later lookup. A lookup made before the update could miss it and leave a stale hold.
2. **Repeated reservation requests.** A reservation request reads the cart's version and its active reservation **in one SQL statement**, so both come from the same snapshot. If the reservation is active, unexpired and has `cart_version = carts.version`, it is returned unchanged and nothing is written.
3. **The cart's own overdue reservation.** If the active reservation has `expires_at <= now`, it is expired (rule 7) in its **own short transaction, before** the main reservation transaction starts. Expiring it inside the main transaction would lock the reservation and its variants before the cart, the reverse of a concurrent cart edit's order.
4. **New reservations, atomically.** The main transaction advances the version with a compare-and-set (`UPDATE carts SET version = version + 1 … WHERE id = :id AND version = :seen`), which takes the cart lock. It then re-reads the cart lines and the active reservation, holds every line's variant in ascending order, and writes the reservation, its items and the hold movements. **The version change, every stock hold, the reservation, its items and every movement commit together or not at all.** If any variant can't be held, the whole transaction rolls back; no partial hold or movement is ever committed.
5. **Lost races.** If the compare-and-set changes no row, another request won the race. Re-read the cart and its reservation (one statement, as in rule 2): return the active, unexpired reservation if it matches the current version; otherwise return a conflict.
6. **Fresh reads after locking.** Cart edits, reservation creation and customer release all serialise on the cart row. Cart contents and the active reservation are re-read **after** the lock or version update; a reservation is never built from a snapshot read before it. Raw SQL updates bypass the ORM's identity map, so values they write are read back with `RETURNING`, or with `populate_existing()`, and never taken from objects loaded earlier.
7. **Ending a reservation is a compare-and-set.** Customer release takes the cart row lock first (as in rule 1), then `UPDATE reservations SET status = …, ended_at = :now WHERE id = :id AND status = 'active'`. A release or cart edit that finds the reservation already past `expires_at` records it as **expired** (`reservation_expiry`), not released. Expiry alone is `… WHERE id = :id AND status = 'active' AND expires_at <= :now`. **The successful transition and its stock release happen in one transaction.** If the compare-and-set changes no row, the reservation was already ended, or isn't due yet, and stock is **not** released again. A release request with nothing active succeeds and changes nothing.
8. **Sweeps.** Overdue reservations of *other* carts (those holding variants that a reservation or availability check needs) are swept **before** the current cart's lock is taken, **one reservation per short transaction**: the reservation's compare-and-set, then its variants in ascending order. The sweep never locks a cart, and a request never holds a cart lock while sweeping.
9. **Release quantities.** Reservation item quantities are the immutable record of what was held. Release and expiry use them; they are never recomputed from the current cart. A hold requires an active variant and product. A release never does, so stock held on a since-archived variant is always released.
10. **Stock and audit together.** Every change to `variants.stock_reserved` writes an `inventory_movements` row with the matching `reservation_id` in the same transaction. The services maintain `stock_reserved = SUM(quantity of items in active reservations)`; the schema cannot check a cross-table sum, so the M2 tests assert it.
11. **Availability checks are advisory.** Setting a cart quantity checks that the quantity is available at that moment, after the cart's own reservation has been ended (rule 1), but holds nothing. Only a reservation holds stock, and availability can change in between. The answer is yes or no; exact counts are never returned (D-5).

**Implemented behaviour (M2.2, `app/domain/reservations/service.py`).** These details refine the rules above; the decisions were approved before implementation:
- **Customer release always advances the cart version** to take the lock, even when nothing turns out to be active (decision B). A reservation request racing a release therefore loses its compare-and-set and gets a conflict. Release locks the cart even past its 30-day expiry, so stock held for a lapsed cart can still be released.
- **A successful reservation refreshes the cart's 30-day expiry** (decision C), in the same compare-and-set, so a cart with live holds can't lapse. This extends ADR-024 D8 ("30 days after its last change"): reserving counts as a change for expiry. It is rolled back with everything else if the reservation fails.
- **`active_reservation` can write:** reading an overdue reservation expires it in its own transaction and returns nothing (lazy expiry, D6).
- **Sweeps** take at most 20 reservations per request (`DEFAULT_SWEEP_LIMIT`), oldest `expires_at` first, and exclude the requesting cart, whose own overdue reservation is handled by rule 3. A sweep failure propagates; the main transaction doesn't start.
- **The partial unique index backs rule 4's ordering:** a new reservation can't be inserted while the cart's leftover reservation is still active, so ending the leftover first is enforced by the schema as well as the code.

**Implemented cart writes (M2.2, `app/domain/cart/service.py`).**
- **Tokens:** `new_cart_token()` returns `secrets.token_urlsafe(32)` and its SHA-256 hex digest; only the digest is stored. The raw token is returned once, in `CartChange.new_token` (excluded from `repr`), when `set_quantity` creates a cart. `find_cart` hashes the presented token and looks the digest up in the database (no application-level comparison), returns only unexpired carts, and never extends expiry. Tokens longer than 128 characters are rejected without hashing.
- **`set_quantity(cart_id | None, sku, quantity)`** replaces a line's quantity (1–10). One transaction: create the cart if `cart_id` is None, lock it (version + 1, `updated_at` and 30-day expiry refreshed), end and release its active reservation, re-read the variant and product (both must be active), enforce the 20-line limit (archived lines count), run the advisory availability check (after the release, holding nothing), upsert the line. Any failure rolls back everything, including a new cart (no orphan carts), the version change and the release. Errors: `invalid_quantity` (422), `variant_not_found` (404), `variant_inactive` (409, also for an archived product), `cart_line_limit` (409), `insufficient_available_stock` (409; SKU and the requested quantity, never a count), `cart_not_found` (404, unknown or expired cart id).
- **`remove_line(cart_id, sku)`** works for archived variants and products and never touches other lines. Removing a line that isn't in the cart, or an unknown SKU, is an idempotent no-op: the version change is rolled back, no reservation is released and no movement is written. This refines rule 1: a request that changes nothing is not an edit.
- **Setting a line to its current quantity is a no-op** (a repeated `PUT`), like removing a missing line: no version change, no expiry refresh, no reservation ended, no movement, no line change; the current cart is returned. The decision is made under the cart lock but **before** the reservation is touched, and the lock's version change is rolled back. A line that is already unavailable but unchanged is also returned as is; it still blocks reserving.
- **Sweeps:** both writes sweep overdue reservations of other carts holding the variant before taking the cart lock (bounded by `DEFAULT_SWEEP_LIMIT`; a new cart has nothing to exclude).
- The cart service ends reservations through the reservation service's own `_end_active_reservation_locked`, so the CASE compare-and-set exists in one place.

**Leftover release and new holds lock variants in one ascending pass (D1, found in the M2.2 audit).** When `reserve_cart`'s main transaction finds a leftover active reservation (one whose `cart_version` no longer matches the cart; the services never create one, but a direct database change can), it releases the leftover's variants and then holds the new lines' variants. Two separate ascending passes could lock variant 9 (release) and then variant 3 (hold), and deadlock against a transaction locking 3 then 9. The main transaction therefore reads and validates the lines first, ends the leftover with its compare-and-set (the reservation lock), then locks the **union** of both variant sets in one ascending `SELECT … FOR NO KEY UPDATE` (the same lock strength the stock updates take, so foreign-key checks are not blocked) before releasing and holding. SQLite omits the clause (single writer).

**M2.2 verification status (as of commit `fa5cb73`, PR #5).**

| Area | Status |
|---|---|
| `unit_of_work`, rollback-only transactions, hold/release primitives, reservation service (commits up to `7c43066`) | Implemented; full backend suite passed on SQLite and PostgreSQL 18 in CI (PR #5, run 42469440478) |
| Cart write service, unchanged-quantity no-op, D1 lock-order fix (commits `a5533f0`, `fa5cb73`) | Implemented; full backend suite passed locally on SQLite and on PostgreSQL 18 in CI (run 42472442771: 392 passed, 0 failed, 0 errors, 0 skipped) |
| Deterministic PostgreSQL concurrency suite (`tests/integration/test_reservation_concurrency.py`, P1–P11, the D1 regression and the old-order deadlock control) | Executed and passed on PostgreSQL 18 in CI (run 42472442771). All 15 required `postgres`-marked tests ran (the CI guard's minimum); none skipped. PostgreSQL-only: skipped by design in local SQLite runs, which no PostgreSQL concurrency claim relies on |
| Product archived during a concurrent hold | Accepted limitation (documented on `hold_reservation_stock`) |
| Bounded global sweeper; `/api/cart` endpoints, cookies, CSRF; frontend | Deferred: optional sweeper per ADR-024 D6; HTTP and UI are M2.2 PR 2 / later |

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
| Browser → backend | Untrusted input | Pydantic validation; server recomputes every total; httpOnly SameSite cookies (the cart token is stored only as a SHA-256 hash); rate limit on chat; CSRF protection on cart, checkout and admin mutations (same-origin `Origin` required, cross-site `Sec-Fetch-Site` refused; SameSite is not relied on alone) |
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
