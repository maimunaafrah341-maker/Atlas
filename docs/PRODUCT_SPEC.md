# Project Atlas — Product Specification

Status: Approved direction (revision 2) · Last updated: 2026-11-04 · Codename only; final product name TBD.

Revision 2 changes: USD store currency; Gemini 3.8 Flash as the initial model; managed PostgreSQL for deployment; outdoor apparel catalogue; Make with Google Sheets, Gmail and Telegram; language-preference rules; protected-placeholder validation; PayPal demo scope.

## 1. Problem

Small businesses that sell internationally can't staff support in every language. Shoppers who can't get answers about sizing, shipping or returns in their own language leave without buying. When a shopper does buy, the merchant then has to handle follow-up work (notifications, fulfilment hand-off, stock alerts) by hand.

## 2. Product

Atlas is a multilingual AI commerce copilot with two surfaces:

| Surface | User | Purpose |
|---|---|---|
| Shopping assistant | Customer (anonymous) | Chat-based product discovery in the customer's language, merchant-grounded answers, validated cart, explicit order approval, PayPal checkout. |
| Operations dashboard | Merchant (single admin) | Orders and payment status, inventory and low-stock alerts, Make automation history, an exception centre. |

Scope is **one merchant per deployment**. Multi-tenancy is not in scope.

**Demo merchant:** a fictional independent **outdoor apparel** brand (jackets, base layers, trousers, hiking socks, accessories). Its products have sizes and colours, and it ships internationally from a single origin. All prices are in **USD**.

## 3. Principles (non-negotiable)

1. **The AI proposes and the backend decides.** The LLM interprets messages, extracts intent, recommends and explains. Catalogue, prices, stock, carts, orders and payments belong to deterministic services.
2. **The LLM never produces commercial facts.** Prices, stock levels, payment states and order IDs shown to the user always come from the database or PayPal and are rendered from structured data, never from model text.
3. **The customer approves explicitly.** Payment only starts after the customer clicks Approve on a server-generated order summary. Chat messages can't approve an order.
4. **PayPal is the payment authority.** Payment state is verified on the backend through the PayPal API (capture response plus verified webhooks).
5. **Make coordinates and never owns state.** Make receives events and runs downstream workflows. It isn't the source of truth for payments or inventory.

## 4. Languages

Initial: English (`en`), Spanish (`es`), French (`fr`), German (`de`), Brazilian Portuguese (`pt-BR`).
Stretch (not promised): Japanese (`ja`), Simplified Chinese (`zh-Hans`).

We track four separate dimensions and never infer one from another:

| Dimension | Example | Source |
|---|---|---|
| Conversation language | `es` | Explicit user choice first; otherwise the conversation's current language, changed only by confident statistical detection (ADR-017) |
| UI locale (formatting) | `es-ES` | Browser preference or user selection |
| Currency | `USD` | Store setting, fixed for the demo (ADR-003). Amounts are formatted for the locale but never converted |
| Shipping destination | `FR` | Explicit user input or the PayPal shipping address; never inferred from language |

**Language selection rules:**
- The user can switch the language manually at any time from a selector that is always visible. Explicit choice has the **highest priority** and lasts until the user selects "Auto".
- In Auto mode, the conversation keeps its current language. A message changes it only when a statistical language identifier is confident and the message has enough words. Short replies ("M", "ok", "sí"), product names and SKUs never change the language.
- Atlas never identifies Spanish, French, German or Portuguese by alphabet or script, because all five initial languages use Latin script.
- Changing language never changes the cart, prices, the order snapshot or the payment state.

## 5. Core features and requirements

### F1. Multilingual conversational shopping
- The customer chats in any initial language. The assistant replies in the detected language unless the user has overridden it.
- The original message text is stored verbatim and never overwritten. Its detected language, the language used for the reply, the reason that language was chosen, and any normalised or translated form used for retrieval are stored separately.
- Any model-written or translated text that refers to protected values goes through **protected-placeholder validation** (ADR-018). Protected values are product IDs, SKUs, sizes, quantities, URLs, numeric values and monetary amounts. If validation fails, Atlas uses a fixed template reply in the target language.
- The UI chrome (buttons, labels, states) is fully localised for the five initial languages.

### F2. Structured shopping-intent extraction
- Each customer turn produces a validated `ShoppingIntent` (Pydantic schema): `action` (browse, ask_info, add_to_cart, modify_cart, checkout, other), category, attributes (colour, material, use case), size, budget (amount plus currency, only if the user stated one), quantity, shipping country (only if stated), referenced product IDs, and the knowledge question if there is one.
- Model output that fails validation is retried once. If it fails again, the request falls back to keyword search and a clarifying question.
- The extracted intent is stored and shown in a debug or "why these results" panel for the demo.

### F3. Catalogue-grounded discovery and recommendations
- The backend runs the search (structured filters plus FTS over product text) using the extracted intent.
- The LLM may only recommend product IDs that appear in the backend result set. Any IDs outside that set are dropped.
- Product cards (name, image, price, stock badge) are rendered from database data. LLM text is checked by a guard that removes or rejects currency amounts and stock counts.

### F4. Variant, inventory and cart validation
- Every product has variants (SKU, size, colour) with a price in integer minor units, `stock_on_hand` and `stock_reserved`.
- Cart operations (add, update, remove) run through the cart service, which checks that the variant exists, is active, has enough available stock and stays within quantity limits. Invalid requests return typed errors that the assistant explains in the user's language.
- Shipping cost is calculated deterministically from a merchant-configured zone/rate table and the explicit destination country.

### F5. Merchant knowledge RAG
- Merchants supply knowledge documents (product details, size guides, shipping, returns, FAQ), tagged by language.
- Retrieval uses full-text search: PostgreSQL full-text search when deployed, SQLite FTS5 for local development and tests, both behind one interface (ADR-006). Answers must cite retrieved chunk IDs, and the UI shows the sources.
- If nothing relevant is retrieved, the assistant says it doesn't know and points to merchant contact details. It never invents a policy.
- Queries in a language with no docs are translated to the merchant's source language for retrieval. The answer is then written in the customer's language.

### F6. Explicit approval and PayPal Sandbox checkout
- "Review order" creates an immutable order draft that snapshots items, unit prices, quantities, shipping, totals, currency and destination. It has a version hash.
- The customer sees a localised review screen and clicks **Approve & pay**. That approval references the version hash. Stock is reserved with a TTL (15 minutes by default).
- The backend creates the PayPal order (Orders v2, `intent=CAPTURE`, **USD**, itemised breakdown that matches the snapshot). The PayPal JS SDK buttons handle payer approval. After that, `onApprove` calls the backend, and the backend captures.
- **Payment demonstration scope:** the demo uses PayPal **Sandbox only**. A Sandbox merchant (business) account receives payment from a Sandbox **international buyer** (personal) account, and the checkout is in **USD**. Atlas does not claim domestic Indian PayPal payment support, Indian-rupee checkout, or live (production) payments.

### F7. Verified payment and order state
- An order counts as `paid` only after the backend sees a `COMPLETED` capture whose amount and currency match the snapshot and whose `custom_id` matches our order. The capture can come from the capture API response or a signature-verified `PAYMENT.CAPTURE.COMPLETED` webhook.
- Webhook events are deduplicated by PayPal event ID. Capture calls use `PayPal-Request-Id`, so retries are idempotent.
- Pending, denied and mismatched captures move the order into an explicit state and create an exception.

### F8. Make.com workflows
The connected apps are **Google Sheets, Gmail and Telegram** only. There is no Slack and no SMTP.

Events are delivered from a database-backed transactional outbox to one authenticated Make custom webhook. A Make router then sends each event to its branch by event type:
1. **`order.paid`** → append an order row to Google Sheets, send the merchant a Telegram alert, and send the customer a confirmation email through Gmail in the order's language.
2. **`stock.low`** → send the merchant a Telegram alert when a variant's available stock falls below its threshold, and log it to a Sheets tab.
3. **`exception.raised`** → send the merchant a Telegram alert for payment mismatches or denials, oversell conflicts and repeated automation delivery failures, and log it to a Sheets tab.

Outbox delivery is idempotent (each event's ID is its idempotency key). Retries are limited, with exponential backoff. Failures are visible in the dashboard, and the outbox recovers after application restarts, including Render free-tier sleep. Make reports run results back to an authenticated callback endpoint. Those callbacks are informational only and can't change order, payment or stock state.

### F9. Merchant dashboard
- Login with a single admin credential (environment-configured).
- Orders list and detail: snapshot, payment events timeline, PayPal IDs.
- Inventory view: on hand, reserved, available, low-stock flags.
- Automation history: each outbox event with its delivery attempts, status and Make callback result. The merchant can manually retry delivery.
- Exception centre: open and resolved exceptions with context. The merchant can mark one resolved with a note.

## 6. UX states

Every async surface defines loading, empty, error and pending states, including:
- Chat: assistant typing; model timeout ("I'm having trouble, try again"); no matching products (shows nearest alternatives or asks a clarifying question).
- Checkout: creating the PayPal order; awaiting payer approval; capturing; payment pending (PayPal `PENDING`); payment failed (with retry); reservation expired (re-review needed).
- Dashboard: no orders yet; automation delivery pending, failed or retrying; Make not configured.

## 7. Privacy and security requirements

- No secrets in the frontend bundle. The only public config is the PayPal client ID.
- The LLM (initially Gemini 3.8 Flash, configurable) receives the conversation text and catalogue/knowledge excerpts, with protected values replaced by placeholders. It doesn't receive payer email, address, prices or payment identifiers.
- Logs redact tokens, secrets, emails and addresses. Raw PayPal payloads are stored with PII fields redacted.
- Make payloads contain only the fields each scenario needs.

## 8. Out of scope

Refund processing (refund webhooks are shown but not initiated), customer accounts, multi-merchant support, currencies other than USD, live FX conversion, live (non-Sandbox) PayPal, domestic Indian PayPal payments, Slack, SMTP, semantic or vector retrieval (unless measured as necessary), voice, recommendations based on purchase history, and a mobile app.

## 9. Success criteria

- A full end-to-end Sandbox purchase in USD by an international Sandbox buyer works in at least two non-English languages on the deployed Render instance, with data persisted in managed PostgreSQL.
- No price, stock figure, payment status or order ID shown to a user comes from model text (enforced by tests).
- Replaying duplicate PayPal webhooks and Make deliveries causes no double state transitions.
- The merchant dashboard shows the order, its verified payment, the Make run results and at least one exception.
