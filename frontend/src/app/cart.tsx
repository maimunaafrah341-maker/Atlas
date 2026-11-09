import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ApiError,
  getCart,
  releaseCart,
  removeCartLine,
  reserveCart,
  setCartQuantity,
  type Cart,
} from "../lib/api.ts";
import { useCatalogueLanguage } from "./catalogueLanguage.tsx";

/**
 * The customer's bag, shared by the header, product pages and the bag page.
 *
 * The server is the only source of truth: every change returns the full cart, which replaces the
 * local copy. Nothing here computes totals or availability, and the cart cookie is HttpOnly, so
 * no token ever reaches this code (or storage).
 *
 * Changes run one at a time, in order. A read that started before a change is discarded, so a
 * slow GET can never overwrite a newer cart.
 */

export type CartAction = "load" | "add" | "quantity" | "remove" | "reserve" | "release";

export interface CartProblem {
  action: CartAction;
  code: string | null;
  message: string;
  /** SKUs the API named (e.g. not enough stock, no longer available). */
  skus: string[];
  /** Offered for failures a repeat might fix: no connection, timeouts, server errors. */
  retry: (() => void) | null;
}

export interface CartContextValue {
  cart: Cart | null;
  /** The first load (and reloads after a language change) before any cart is known. */
  loading: boolean;
  /** A change is in flight; controls should wait. */
  busy: boolean;
  problem: CartProblem | null;
  reload: () => Promise<void>;
  /** Add one unit of a variant (sets the line to its current quantity + 1). */
  add: (sku: string) => Promise<boolean>;
  setQuantity: (sku: string, quantity: number) => Promise<boolean>;
  remove: (sku: string) => Promise<boolean>;
  reserve: () => Promise<boolean>;
  release: () => Promise<boolean>;
  clearProblem: () => void;
}

const CartContext = createContext<CartContextValue | null>(null);

const MESSAGES: Record<string, string> = {
  insufficient_available_stock: "There isn’t enough stock for that quantity.",
  variant_inactive: "This item is no longer available.",
  variant_not_found: "This item is no longer in the catalogue.",
  cart_line_limit: "Your bag can hold up to 20 different items.",
  invalid_quantity: "Quantities can be from 1 to 10.",
  cart_empty: "Your bag is empty.",
  cart_has_unavailable_items: "Some items are no longer available. Remove them, then try again.",
  reservation_conflict: "Your bag changed while it was being reserved. Please try again.",
  cart_not_found: "Your bag has expired. Add items to start a new one.",
  cross_origin_request: "That request was blocked. Reload the page and try again.",
};

/** These mean the local copy may be out of date: fetch the cart again after reporting them. */
const RESYNC = new Set([
  "insufficient_available_stock",
  "variant_inactive",
  "cart_has_unavailable_items",
  "reservation_conflict",
  "cart_not_found",
  "cart_empty",
]);

function problemFrom(action: CartAction, error: unknown, retry: () => void): CartProblem {
  const apiError = error instanceof ApiError ? error : null;
  const code = apiError?.code ?? null;
  const skus = (apiError?.details ?? []).flatMap((detail) => (typeof detail.sku === "string" ? [detail.sku] : []));
  const retryable = !apiError || apiError.status === null || apiError.status >= 500;
  const message = (code && MESSAGES[code]) || apiError?.message || "Something went wrong.";
  return { action, code, message, skus, retry: retryable ? retry : null };
}

export function CartProvider({ children }: { children: ReactNode }) {
  const { language } = useCatalogueLanguage();
  const [cart, setCartState] = useState<Cart | null>(null);
  const [loadedFor, setLoadedFor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<CartProblem | null>(null);

  const languageRef = useRef(language);
  const latest = useRef<Cart | null>(null); // the newest cart, readable by queued changes
  const setCart = useCallback((next: Cart) => {
    latest.current = next;
    setCartState(next);
  }, []);
  const generation = useRef(0); // bumped by every change; a read from an older generation is dropped
  const queue = useRef<Promise<unknown>>(Promise.resolve());
  const pending = useRef(0);
  const retryLoad = useRef<() => void>(() => undefined);

  const load = useCallback(async (signal?: AbortSignal) => {
    const started = generation.current;
    try {
      const lang = languageRef.current;
      const next = await getCart(lang, signal);
      if (signal?.aborted || started !== generation.current) return;
      setCart(next);
      setLoadedFor(lang);
      setProblem((current) => (current?.action === "load" ? null : current));
    } catch (error) {
      if (signal?.aborted || started !== generation.current) return;
      setProblem(problemFrom("load", error, () => retryLoad.current()));
    }
  }, [setCart]);

  useEffect(() => {
    retryLoad.current = () => void load();
  }, [load]);

  useEffect(() => {
    languageRef.current = language;
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [language, load]);

  // Loading until the cart is known in the current language (and not failed).
  const loading = loadedFor !== language && problem?.action !== "load";

  /** Run a change after any earlier ones; its returned cart replaces the local copy. */
  const change = useCallback(
    (action: CartAction, run: (lang: typeof language) => Promise<Cart>): Promise<boolean> => {
      const attempt = (): Promise<boolean> => {
        pending.current += 1;
        setBusy(true);
        const result = queue.current.then(async () => {
          generation.current += 1;
          try {
            setCart(await run(languageRef.current));
            setProblem(null);
            return true;
          } catch (error) {
            const next = problemFrom(action, error, () => void attempt());
            setProblem(next);
            if (next.code && RESYNC.has(next.code)) await load();
            return false;
          } finally {
            pending.current -= 1;
            if (pending.current === 0) setBusy(false);
          }
        });
        queue.current = result.catch(() => undefined);
        return result;
      };
      return attempt();
    },
    [load, setCart],
  );

  const value = useMemo<CartContextValue>(
    () => ({
      cart,
      loading,
      busy,
      problem,
      reload: () => load(),
      add: (sku) =>
        change("add", (lang) => {
          // Read the quantity when the change runs, after earlier changes have landed.
          const current = latest.current?.lines.find((line) => line.sku === sku)?.quantity ?? 0;
          return setCartQuantity(sku, current + 1, lang);
        }),
      setQuantity: (sku, quantity) => change("quantity", (lang) => setCartQuantity(sku, quantity, lang)),
      remove: (sku) => change("remove", (lang) => removeCartLine(sku, lang)),
      reserve: () => change("reserve", async (lang) => (await reserveCart(lang)).cart),
      release: () => change("release", (lang) => releaseCart(lang)),
      clearProblem: () => setProblem(null),
    }),
    [cart, loading, busy, problem, load, change],
  );

  return <CartContext value={value}>{children}</CartContext>;
}

export function useCart(): CartContextValue {
  const value = useContext(CartContext);
  if (!value) throw new Error("useCart must be used inside CartProvider");
  return value;
}
