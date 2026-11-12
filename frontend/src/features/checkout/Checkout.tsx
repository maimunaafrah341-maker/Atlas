import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useCart } from "../../app/cart.tsx";
import { useCatalogueLanguage } from "../../app/catalogueLanguage.tsx";
import {
  ApiError,
  cancelOrder,
  capturePayment,
  createOrder,
  getCheckoutConfig,
  getOrder,
  reconcilePayment,
  startPayment,
  type Cart,
  type CheckoutConfig,
  type Order,
  type PaymentResult,
} from "../../lib/api.ts";
import { countries, countryName } from "../../lib/countries.ts";
import { formatMoney } from "../../lib/format.ts";
import { formatCountdown, useCountdown } from "../cart/useCountdown.ts";
import { loadPayPal, type PayPalButtons } from "./paypal.ts";

const OUTLINE = "border border-ink px-4 py-2 text-sm hover:bg-ink hover:text-paper";
const PRIMARY = "w-full bg-forest-900 px-6 py-4 text-sm text-on-dark hover:bg-forest-950 disabled:opacity-60";
const DEFAULT_COUNTRY = "US";
/** Orders still waiting for the buyer: show the review and the PayPal buttons. */
const PAYABLE = new Set(["pending_approval", "awaiting_payment"]);
/** PayPal has an outcome Atlas is still confirming. */
const CONFIRMING = new Set(["capturing", "payment_pending"]);

const message = (error: unknown) => (error instanceof ApiError ? error.message : "Something went wrong.");

/**
 * Checkout (M3.4): choose where to ship, review the server's order snapshot (subtotal, shipping,
 * total, all from the API), and pay with PayPal Sandbox buttons. The backend creates the PayPal
 * order, captures it and verifies the capture with PayPal; this component only relays the
 * buyer's approval and shows the server's result. No real money: PayPal Sandbox only.
 */
export function Checkout({ cart, onResult }: { cart: Cart; onResult: (result: PaymentResult) => void }) {
  const { language } = useCatalogueLanguage();
  const { reload } = useCart();
  const [config, setConfig] = useState<CheckoutConfig | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const openRef = cart.checkout?.order_ref ?? null;

  useEffect(() => {
    const controller = new AbortController();
    getCheckoutConfig(controller.signal).then(setConfig, () => setConfig({ payments_enabled: false, paypal_client_id: null, currency: "USD", intent: "capture" }));
    return () => controller.abort();
  }, []);

  // An order already open for this bag (e.g. after a reload): show it.
  useEffect(() => {
    if (openRef === null || order?.ref === openRef) return;
    getOrder(openRef, language).then(setOrder, (problem: unknown) => setError(message(problem)));
  }, [openRef, order?.ref, language]);

  async function run<T>(action: () => Promise<T>): Promise<T | null> {
    setBusy(true);
    setError(null);
    try {
      return await action();
    } catch (problem) {
      setError(message(problem));
      return null;
    } finally {
      setBusy(false);
    }
  }

  async function review(country: string) {
    const created = await run(() => createOrder(country, language));
    if (created) {
      setOrder(created);
      void reload();
    }
  }

  async function cancel() {
    if (!order) return;
    if (await run(() => cancelOrder(order.ref, language))) {
      setOrder(null);
      void reload();
    }
  }

  async function settle(result: PaymentResult) {
    setOrder(result.order);
    if (result.result === "paid" || result.order.status === "paid") {
      onResult(result);
      void reload();
    } else if (result.result === "failed" || result.result === "closed") {
      setError("PayPal declined this payment and the order was closed. Your items are still in your bag.");
      setOrder(null);
      void reload();
    } else {
      onResult(result);
    }
  }

  async function checkAgain() {
    if (!order) return;
    const result = await run(() => reconcilePayment(order.ref, language));
    if (result) await settle(result);
  }

  if (order === null) {
    if (openRef !== null) return <p className="text-sm text-ink-muted">Loading your order…</p>;
    return <StartCheckout busy={busy} error={error} onReview={(country) => void review(country)} />;
  }

  return (
    <div>
      <p className="label-mono text-ink-muted">Order review</p>
      <OrderReview order={order} />
      {PAYABLE.has(order.status) && (
        <>
          <HoldTimer order={order} onExpired={() => void getOrder(order.ref, language).then(setOrder, () => undefined)} />
          {config === null ? (
            <p className="mt-6 text-sm text-ink-muted">Loading payment options…</p>
          ) : config.payments_enabled && config.paypal_client_id ? (
            <PayPalCheckout
              clientId={config.paypal_client_id}
              order={order}
              onError={setError}
              onResult={(result) => void settle(result)}
            />
          ) : (
            <p className="mt-6 bg-paper p-4 text-sm text-ink-muted">PayPal Sandbox payments aren’t enabled on this server.</p>
          )}
          <button type="button" onClick={() => void cancel()} disabled={busy} className={`mt-4 w-full ${OUTLINE}`}>
            Cancel order
          </button>
        </>
      )}
      {CONFIRMING.has(order.status) && (
        <div className="mt-6">
          <p className="text-sm">PayPal is confirming this payment. Your items stay held meanwhile.</p>
          <button type="button" onClick={() => void checkAgain()} disabled={busy} className={`mt-3 w-full ${OUTLINE}`}>
            Check payment status
          </button>
        </div>
      )}
      {error && (
        <p role="alert" className="mt-4 text-sm text-alert">
          {error}
        </p>
      )}
    </div>
  );
}

function StartCheckout({ busy, error, onReview }: { busy: boolean; error: string | null; onReview: (country: string) => void }) {
  const list = useMemo(() => countries(), []);
  const [country, setCountry] = useState(DEFAULT_COUNTRY);
  const id = useId();
  return (
    <div className="mt-6 border-t border-rule pt-6">
      <p className="label-mono text-ink-muted">Checkout</p>
      <label htmlFor={id} className="mt-3 block text-sm">
        Ship to
      </label>
      <select
        id={id}
        value={country}
        onChange={(event) => setCountry(event.target.value)}
        className="mt-1 w-full border border-rule bg-paper px-3 py-2.5 text-sm"
      >
        {list.map((option) => (
          <option key={option.code} value={option.code}>
            {option.name}
          </option>
        ))}
      </select>
      <button type="button" onClick={() => onReview(country)} disabled={busy} className={`mt-4 ${PRIMARY}`}>
        {busy ? "Preparing your order…" : "Review order"}
      </button>
      <p className="mt-2 text-xs text-ink-muted">The server prices shipping and totals the order. Pay with PayPal Sandbox: no real money.</p>
      {error && (
        <p role="alert" className="mt-3 text-sm text-alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function OrderReview({ order }: { order: Order }) {
  return (
    <dl className="mt-4 grid grid-cols-[1fr_auto] gap-y-2 text-sm">
      {order.lines.map((line) => (
        <div key={line.sku} className="contents">
          <dt className="text-ink-muted">
            {line.name} × {line.quantity}
          </dt>
          <dd className="text-right tabular-nums">{formatMoney(line.line_total)}</dd>
        </div>
      ))}
      <dt className="border-t border-rule pt-2">Subtotal</dt>
      <dd className="border-t border-rule pt-2 text-right tabular-nums">{formatMoney(order.subtotal)}</dd>
      <dt>Shipping to {countryName(order.shipping.country)}</dt>
      <dd className="text-right tabular-nums">{formatMoney(order.shipping.price)}</dd>
      <dt className="text-base font-semibold">Total</dt>
      <dd className="text-right text-base font-semibold tabular-nums">{formatMoney(order.total)}</dd>
    </dl>
  );
}

function HoldTimer({ order, onExpired }: { order: Order; onExpired: () => void }) {
  const remaining = useCountdown(order.hold_expires_at, onExpired) ?? 0;
  return (
    <p className="mt-4 text-xs text-ink-muted">
      Stock held for this order for <span className="tabular-nums">{formatCountdown(remaining)}</span>.
    </p>
  );
}

function PayPalCheckout({
  clientId,
  order,
  onError,
  onResult,
}: {
  clientId: string;
  order: Order;
  onError: (message: string | null) => void;
  onResult: (result: PaymentResult) => void;
}) {
  const { language } = useCatalogueLanguage();
  const container = useRef<HTMLDivElement>(null);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const latest = useRef({ order, onError, onResult, language });
  useEffect(() => {
    latest.current = { order, onError, onResult, language };
  });

  useEffect(() => {
    let buttons: PayPalButtons | null = null;
    let cancelled = false;
    loadPayPal(clientId).then(
      (paypal) => {
        if (cancelled || !container.current) return;
        buttons = paypal.Buttons({
          style: { layout: "vertical", shape: "rect", label: "paypal" },
          createOrder: async () => {
            const { order: current, language: lang } = latest.current;
            latest.current.onError(null);
            const started = await startPayment(current.ref, current.version_hash, lang);
            return started.paypal_order_id;
          },
          onApprove: async (data, actions) => {
            const { order: current, language: lang } = latest.current;
            try {
              const result = await capturePayment(current.ref, data.orderID, lang);
              if (result.recoverable) {
                latest.current.onError("PayPal couldn’t use that payment method. Please choose another one.");
                return actions.restart();
              }
              latest.current.onResult(result);
            } catch (problem) {
              latest.current.onError(message(problem));
            }
            return undefined;
          },
          onCancel: () => latest.current.onError("You closed PayPal before paying. Your order is still held; you can pay or cancel."),
          onError: () => latest.current.onError("PayPal couldn’t complete the payment. Please try again."),
        });
        void buttons.render(container.current).then(
          () => !cancelled && setState("ready"),
          () => !cancelled && setState("failed"),
        );
      },
      () => !cancelled && setState("failed"),
    );
    return () => {
      cancelled = true;
      void buttons?.close?.();
    };
  }, [clientId]);

  return (
    <div className="mt-6">
      <div ref={container} data-testid="paypal-buttons" />
      {state === "loading" && <p className="text-sm text-ink-muted">Loading PayPal…</p>}
      {state === "failed" && (
        <p role="alert" className="text-sm text-alert">
          PayPal’s checkout couldn’t be loaded. Check your connection or ad blocker and reload the page.
        </p>
      )}
      <p className="mt-2 text-xs text-ink-muted">PayPal Sandbox · test accounts only, no real money.</p>
    </div>
  );
}

/** The outcome after paying, shown even though the bag is now empty. */
export function PaymentReceipt({ result, onDismiss }: { result: PaymentResult; onDismiss: () => void }) {
  const paid = result.result === "paid" || result.order.status === "paid";
  return (
    <section aria-labelledby="receipt-heading" className="mb-10 border border-ok bg-paper-raised p-6 sm:p-8">
      <p className={`label-mono ${paid ? "text-ok" : "text-warn"}`}>{paid ? "Payment complete" : "Payment processing"}</p>
      <h2 id="receipt-heading" className="mt-2 font-display text-3xl">
        {paid ? "Thank you, your order is paid." : "PayPal is still processing your payment."}
      </h2>
      <p className="mt-3 text-sm text-ink-muted">
        Order <span className="font-mono">{result.order.ref}</span>
        {paid
          ? " was captured and verified with PayPal Sandbox. Your stock is committed and your bag is empty."
          : ". Your items stay held until PayPal confirms it; check your bag again shortly."}
      </p>
      <div className="max-w-sm">
        <OrderReview order={result.order} />
      </div>
      <button type="button" onClick={onDismiss} className={`mt-6 ${OUTLINE}`}>
        Close
      </button>
    </section>
  );
}
