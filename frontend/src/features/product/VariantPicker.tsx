import { useId, type ReactNode } from "react";
import type { StockStatus, Variant } from "../../lib/api.ts";
import { COLOUR_SWATCHES, isColour } from "../../lib/catalogue.ts";
import { STOCK_LABELS, colourLabel, sizeLabel } from "../../lib/format.ts";
import { coloursOf, findVariant, sizesIn, sizesOf, type Selection } from "./variantSelection.ts";

/**
 * Size and colour as native radio groups: arrow keys move within a group, Tab moves between
 * groups, and each option announces its own stock status for the current selection.
 */
export function VariantPicker({
  variants,
  selection,
  onSize,
  onColour,
}: {
  variants: readonly Variant[];
  selection: Selection;
  onSize: (size: string) => void;
  onColour: (colour: string) => void;
}) {
  const name = useId();
  const sizesInColour = sizesIn(variants, selection.colour);

  return (
    <div className="flex flex-col gap-8">
      <Group legend="Colour" value={selection.colour && colourLabel(selection.colour)}>
        <div className="flex flex-wrap gap-x-4 gap-y-1">
          {coloursOf(variants).map((colour) => {
            const variant = findVariant(variants, selection.size, colour);
            // With a size chosen, say how this colour stands in that size (or that it isn't made in it).
            const note =
              selection.size === null ? null : variant ? status(variant.stock_status) : `Not in ${sizeLabel(selection.size)}`;
            return (
              // min-h-11: a 44px touch target per option.
              <label key={colour} className="flex min-h-11 cursor-pointer items-center gap-2.5 pr-1 text-sm whitespace-nowrap">
                <input
                  type="radio"
                  name={`${name}-colour`}
                  value={colour}
                  checked={selection.colour === colour}
                  onChange={() => onColour(colour)}
                  className="peer sr-only"
                />
                <span
                  aria-hidden="true"
                  className="size-6 rounded-full ring-1 ring-ink/20 ring-offset-2 ring-offset-paper peer-checked:ring-2 peer-checked:ring-forest-900 peer-focus-visible:outline-2 peer-focus-visible:outline-offset-4 peer-focus-visible:outline-forest-900"
                  style={{ backgroundColor: isColour(colour) ? COLOUR_SWATCHES[colour] : "transparent" }}
                />
                <span className="peer-checked:font-semibold">{colourLabel(colour)}</span>
                {note && <OptionNote>{note}</OptionNote>}
              </label>
            );
          })}
        </div>
      </Group>

      <Group legend="Size" value={selection.size && sizeLabel(selection.size)}>
        {/* Fixed-height boxes with a reserved status line: a stock label never resizes a box or moves its size text. */}
        <div className="grid grid-cols-[repeat(auto-fill,minmax(5.5rem,1fr))] gap-1.5">
          {sizesOf(variants).map((size) => {
            const offered = sizesInColour.includes(size);
            const variant = findVariant(variants, size, selection.colour);
            const out = variant?.stock_status === "out_of_stock";
            return (
              <label key={size} className={offered ? "cursor-pointer" : "cursor-not-allowed"}>
                <input
                  type="radio"
                  name={`${name}-size`}
                  value={size}
                  checked={selection.size === size}
                  disabled={!offered}
                  onChange={() => onSize(size)}
                  className="peer sr-only"
                />
                <span
                  className={`flex h-14 flex-col items-center justify-center border border-rule px-1 text-sm peer-checked:border-forest-900 peer-checked:bg-forest-900 peer-checked:text-on-dark peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-forest-900 peer-disabled:border-dashed peer-disabled:text-ink-subtle ${offered ? "hover:border-ink" : ""}`}
                >
                  <span className={out || !offered ? "line-through decoration-1" : ""}>{sizeLabel(size)}</span>
                  {!offered && selection.colour && <span className="sr-only">, not made in {colourLabel(selection.colour)}</span>}
                  <span className="mt-0.5 h-4 text-xs leading-4 whitespace-nowrap opacity-85">
                    {variant && variant.stock_status !== "in_stock" && (
                      <>
                        <span className="sr-only">, </span>
                        {STOCK_LABELS[variant.stock_status]}
                      </>
                    )}
                  </span>
                </span>
              </label>
            );
          })}
        </div>
      </Group>
    </div>
  );
}

function status(stock: StockStatus): string | null {
  return stock === "in_stock" ? null : STOCK_LABELS[stock];
}

function Group({ legend, value, children }: { legend: string; value: string | null; children: ReactNode }) {
  return (
    <fieldset>
      <legend className="label-mono mb-4 text-ink-muted">
        {legend}
        {value && <span className="text-ink"> · {value}</span>}
      </legend>
      {children}
    </fieldset>
  );
}

function OptionNote({ children }: { children: ReactNode }) {
  return (
    <span className="text-xs text-ink-muted">
      <span className="sr-only">, </span>
      {children}
    </span>
  );
}
