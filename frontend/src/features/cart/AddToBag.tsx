import { useId, useState } from "react";
import { Link } from "react-router";
import { useCart } from "../../app/cart.tsx";
import type { Variant } from "../../lib/api.ts";

/**
 * Adds one unit of the chosen variant. The variant's own server-reported status decides whether it
 * can be offered (out of stock is not); the server decides whether the add succeeds.
 * Keyed by SKU by its parent, so its "added" message belongs to one variant.
 */
export function AddToBag({ selected }: { selected: Variant | null }) {
  const { cart, busy, problem, add, clearProblem } = useCart();
  const [added, setAdded] = useState(false);
  const [mine, setMine] = useState(false); // the shared problem came from this button
  const id = useId();

  const inBag = selected ? (cart?.lines.find((line) => line.sku === selected.sku)?.quantity ?? 0) : 0;
  const blocked = !selected
    ? "Choose a colour and size first."
    : selected.stock_status === "out_of_stock"
      ? "This option is out of stock."
      : null;
  const shownProblem = mine && problem?.action === "add" ? problem : null;

  async function onAdd() {
    if (!selected || blocked || busy) return;
    clearProblem();
    setAdded(false);
    setMine(true);
    setAdded(await add(selected.sku));
  }

  return (
    <div>
      <button
        type="button"
        onClick={onAdd}
        aria-disabled={blocked || busy ? "true" : undefined}
        aria-describedby={`${id}-note`}
        className={`w-full px-6 py-4 text-sm text-on-dark ${
          blocked ? "cursor-not-allowed bg-forest-900/35" : busy ? "cursor-progress bg-forest-900/70" : "bg-forest-900 hover:bg-forest-950"
        }`}
      >
        {busy && mine ? "Adding…" : "Add to bag"}
      </button>
      <div id={`${id}-note`} className="mt-2 min-h-5 text-xs" aria-live="polite">
        {blocked ? (
          <p className="text-ink-muted">{blocked}</p>
        ) : shownProblem ? (
          <p className="flex flex-wrap items-center gap-x-3 gap-y-1 text-alert">
            {shownProblem.message}
            {shownProblem.retry && (
              <button type="button" onClick={shownProblem.retry} className="text-ink underline underline-offset-4">
                Try again
              </button>
            )}
          </p>
        ) : added ? (
          <p className="text-ok">
            Added to your bag.{" "}
            <Link to="/bag" className="text-ink underline underline-offset-4">
              View bag
            </Link>
          </p>
        ) : inBag > 0 ? (
          <p className="text-ink-muted">{inBag} in your bag.</p>
        ) : (
          <p className="text-ink-muted">Adds one. Nothing is held until you reserve your bag.</p>
        )}
      </div>
    </div>
  );
}
