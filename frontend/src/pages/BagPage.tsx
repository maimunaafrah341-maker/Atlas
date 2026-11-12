import { useState, type ReactNode } from "react";
import { Link } from "react-router";
import { useCart, type CartProblem } from "../app/cart.tsx";
import { TranslationNote } from "../components/catalogue/TranslationNote.tsx";
import { PageIntro } from "../components/ui/PageIntro.tsx";
import { PrimaryLink } from "../components/ui/links.tsx";
import { formatCountdown, useCountdown } from "../features/cart/useCountdown.ts";
import { Checkout, PaymentReceipt } from "../features/checkout/Checkout.tsx";
import type { Cart, CartLine, CartReservation, PaymentResult } from "../lib/api.ts";
import { colourLabel, formatMoney, sizeLabel } from "../lib/format.ts";

const MAX_QUANTITY = 10; // the API's per-line limit; the server still validates every change

/**
 * The bag: lines, quantities, the server's subtotal, and a stock reservation with a countdown.
 * Every number shown comes from the API; nothing is totalled here. Checkout (M3.4) pays with
 * PayPal Sandbox through the backend.
 */
export function BagPage() {
  const { cart, loading, problem, reload } = useCart();
  const [receipt, setReceipt] = useState<PaymentResult | null>(null);

  return (
    <div className="mx-auto max-w-[90rem] px-4 pt-16 pb-24 sm:px-8 sm:pt-20">
      <title>Bag · Atlas Demo Outfitters</title>
      <PageIntro eyebrow="Your bag" title="Bag">
        Live catalogue prices in US dollars. Reserve your bag to hold the stock, then check out with PayPal Sandbox (no real money).
      </PageIntro>
      <div className="mt-12 border-t border-rule pt-8">
        {receipt && <PaymentReceipt result={receipt} onDismiss={() => setReceipt(null)} />}
        {cart === null ? (
          loading ? (
            <div role="status" className="animate-pulse space-y-4" aria-label="Loading your bag">
              <span className="sr-only">Loading your bag…</span>
              <div aria-hidden="true" className="h-24 bg-tint-sand" />
              <div aria-hidden="true" className="h-24 bg-tint-sand" />
            </div>
          ) : (
            <div role="alert" className="flex flex-wrap items-center gap-4 bg-paper-raised p-6">
              <p className="text-alert">Your bag couldn’t be loaded. {problem?.message}</p>
              <button type="button" onClick={() => void reload()} className={OUTLINE}>
                Try again
              </button>
            </div>
          )
        ) : cart.lines.length === 0 ? (
          receipt ? null : <EmptyBag problem={problem?.action === "load" ? null : problem} />
        ) : (
          <FilledBag cart={cart} onResult={setReceipt} />
        )}
      </div>
    </div>
  );
}

const OUTLINE = "border border-ink px-4 py-2 text-sm hover:bg-ink hover:text-paper";

function EmptyBag({ problem }: { problem: CartProblem | null }) {
  return (
    <div className="flex flex-col items-start gap-6">
      {problem && <ProblemNote problem={problem} />}
      <p className="text-lg">Your bag is empty.</p>
      <PrimaryLink to="/shop">Explore the catalogue</PrimaryLink>
    </div>
  );
}

function FilledBag({ cart, onResult }: { cart: Cart; onResult: (result: PaymentResult) => void }) {
  const { busy, problem, setQuantity, remove } = useCart();
  const [notice, setNotice] = useState<string | null>(null);
  const short = new Set(problem?.code === "insufficient_available_stock" ? problem.skus : []);

  /** Any edit ends an active reservation on the server; say so when it happens. */
  async function edit(run: () => Promise<boolean>) {
    const held = cart.reservation !== null;
    setNotice(null);
    if ((await run()) && held) setNotice("Your bag changed, so the reservation was released. Reserve again to hold the new quantities.");
  }

  return (
    <div className="grid gap-12 lg:grid-cols-[minmax(0,1fr)_22rem] lg:gap-16">
      <section aria-labelledby="bag-lines">
        <h2 id="bag-lines" className="sr-only">
          Items
        </h2>
        <ul className="divide-y divide-rule border-y border-rule">
          {cart.lines.map((line) => (
            <li key={line.sku}>
              <BagLine
                line={line}
                short={short.has(line.sku)}
                busy={busy}
                onQuantity={(quantity) => void edit(() => setQuantity(line.sku, quantity))}
                onRemove={() => void edit(() => remove(line.sku))}
              />
            </li>
          ))}
        </ul>
      </section>
      <Summary cart={cart} notice={notice} onNotice={setNotice} onResult={onResult} />
    </div>
  );
}

function BagLine({
  line,
  short,
  busy,
  onQuantity,
  onRemove,
}: {
  line: CartLine;
  short: boolean;
  busy: boolean;
  onQuantity: (quantity: number) => void;
  onRemove: () => void;
}) {
  const options = `${colourLabel(line.colour)} · ${sizeLabel(line.size)}`;
  const href = `/products/${line.product_slug}?${new URLSearchParams({ size: line.size, colour: line.colour })}`;
  return (
    <article className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-x-4 gap-y-3 py-6 sm:grid-cols-[6rem_minmax(0,1fr)_auto] sm:gap-x-6">
      <div aria-hidden="true" className="row-span-2 aspect-[4/5] bg-tint-stone sm:row-span-1" />
      <div className="min-w-0">
        <h3 className="text-lg leading-snug">
          <Link to={href} className="hover:underline">
            {line.name}
          </Link>
        </h3>
        <p className="mt-1 text-sm text-ink-muted">{options}</p>
        <TranslationNote product={line} className="mt-1.5" />
        <p className="mt-1 text-sm text-ink-muted">{formatMoney(line.unit_price)} each</p>
        {!line.available && (
          <p className="mt-2 text-sm text-alert">No longer available. Not included in the subtotal; remove it to reserve.</p>
        )}
        {short && <p className="mt-2 text-sm text-alert">Not enough stock for this quantity.</p>}
      </div>
      <div className="col-start-2 flex flex-wrap items-center justify-between gap-4 sm:col-start-3 sm:row-start-1 sm:flex-col sm:items-end sm:justify-start">
        <p className="text-base font-semibold sm:order-first">
          <span className="sr-only">Line total: </span>
          {formatMoney(line.line_total)}
        </p>
        <div className="flex items-center gap-4">
          <QuantityStepper line={line} disabled={busy || !line.available} onChange={onQuantity} />
          <button
            type="button"
            onClick={() => !busy && onRemove()}
            aria-disabled={busy ? "true" : undefined}
            aria-label={`Remove ${line.name}, ${options}`}
            className="text-sm underline underline-offset-4 hover:text-ink-muted"
          >
            Remove
          </button>
        </div>
      </div>
    </article>
  );
}

function QuantityStepper({
  line,
  disabled,
  onChange,
}: {
  line: CartLine;
  disabled: boolean;
  onChange: (quantity: number) => void;
}) {
  const label = `${line.name}, ${colourLabel(line.colour)} · ${sizeLabel(line.size)}`;
  // aria-disabled, not disabled: a natively disabled button drops keyboard focus mid-use.
  const step = "grid size-10 place-items-center text-lg aria-disabled:cursor-not-allowed aria-disabled:text-ink-subtle";
  const canDecrease = !disabled && line.quantity > 1;
  const canIncrease = !disabled && line.quantity < MAX_QUANTITY;
  return (
    <div role="group" aria-label={`Quantity of ${label}`} className="flex items-center border border-rule">
      <button
        type="button"
        className={step}
        aria-disabled={canDecrease ? undefined : "true"}
        onClick={() => canDecrease && onChange(line.quantity - 1)}
        aria-label={`Decrease quantity of ${label}`}
      >
        −
      </button>
      <span className="min-w-8 text-center text-sm tabular-nums" aria-live="polite">
        <span className="sr-only">Quantity </span>
        {line.quantity}
      </span>
      <button
        type="button"
        className={step}
        aria-disabled={canIncrease ? undefined : "true"}
        onClick={() => canIncrease && onChange(line.quantity + 1)}
        aria-label={`Increase quantity of ${label}`}
      >
        +
      </button>
    </div>
  );
}

function Summary({
  cart,
  notice,
  onNotice,
  onResult,
}: {
  cart: Cart;
  notice: string | null;
  onNotice: (text: string | null) => void;
  onResult: (result: PaymentResult) => void;
}) {
  const { busy, problem, reserve, release, reload } = useCart();
  const editProblem = problem && problem.action !== "add" ? problem : null;

  async function onReserve() {
    if (busy) return;
    onNotice(null);
    await reserve();
  }

  async function onRelease() {
    if (busy) return;
    onNotice(null);
    if (await release()) onNotice("Your reservation was released.");
  }

  return (
    <aside aria-labelledby="bag-summary" className="self-start bg-paper-raised p-6 lg:sticky lg:top-8">
      <h2 id="bag-summary" className="label-mono text-ink-muted">
        Summary
      </h2>
      <dl className="mt-5 grid grid-cols-[1fr_auto] gap-y-3 text-sm">
        <dt>Items</dt>
        <dd className="text-right tabular-nums">{cart.item_count}</dd>
        <dt className="text-base font-semibold">Subtotal</dt>
        <dd className="text-right text-base font-semibold tabular-nums">{formatMoney(cart.subtotal)}</dd>
      </dl>
      <p className="mt-3 text-xs text-ink-muted">
        Shipping is priced at checkout; taxes aren’t included.
      </p>

      <div className="mt-6 border-t border-rule pt-6" aria-live="polite">
        {cart.checkout ? (
          <Checkout cart={cart} onResult={onResult} />
        ) : cart.reservation ? (
          <>
          <Reserved
            key={cart.reservation.id}
            reservation={cart.reservation}
            busy={busy}
            onRelease={() => void onRelease()}
            onExpired={() => {
              onNotice("Your reservation expired and the stock was released. Your items are still in your bag.");
              void reload();
            }}
          />
          <Checkout cart={cart} onResult={onResult} />
          </>
        ) : (
          <div>
            <button
              type="button"
              onClick={() => void onReserve()}
              aria-disabled={busy ? "true" : undefined}
              className={`w-full px-6 py-4 text-sm text-on-dark ${busy ? "cursor-progress bg-forest-900/70" : "bg-forest-900 hover:bg-forest-950"}`}
            >
              Reserve items
            </button>
            <p className="mt-2 text-xs text-ink-muted">
              Holds this stock for a short time so it can’t sell out while you decide. Changing your bag releases the hold.
            </p>
          </div>
        )}
        {notice && <p className="mt-4 text-sm">{notice}</p>}
        {editProblem && <ProblemNote problem={editProblem} />}
      </div>
    </aside>
  );
}

function Reserved({
  reservation,
  busy,
  onRelease,
  onExpired,
}: {
  reservation: CartReservation;
  busy: boolean;
  onRelease: () => void;
  onExpired: () => void;
}) {
  const remaining = useCountdown(reservation.expires_at, onExpired) ?? 0;
  const until = new Date(reservation.expires_at).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
  return (
    <div>
      <p className="label-mono text-ok">Reserved</p>
      <p className="mt-2 font-display text-4xl tabular-nums">
        <span role="timer" aria-label={`Time left on your reservation: ${formatCountdown(remaining)}`}>
          {formatCountdown(remaining)}
        </span>
      </p>
      <p className="mt-1 text-xs text-ink-muted">
        Held until about {until} (by this device’s clock). The items stay in your bag after that.
      </p>
      <button type="button" onClick={onRelease} aria-disabled={busy ? "true" : undefined} className={`mt-4 w-full ${OUTLINE}`}>
        Release reservation
      </button>
    </div>
  );
}

function ProblemNote({ problem }: { problem: CartProblem }): ReactNode {
  return (
    <div role="alert" className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm text-alert">
      <p>{problem.message}</p>
      {problem.retry && (
        <button type="button" onClick={problem.retry} className="text-ink underline underline-offset-4">
          Try again
        </button>
      )}
    </div>
  );
}
