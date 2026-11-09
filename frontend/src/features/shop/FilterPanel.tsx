import { useId, useState, type FormEvent, type ReactNode } from "react";
import {
  CATEGORIES,
  CATEGORY_LABELS,
  COLOURS,
  COLOUR_SWATCHES,
  SIZES,
  type Category,
  type Colour,
  type Size,
} from "../../lib/catalogue.ts";
import { colourLabel, sizeLabel } from "../../lib/format.ts";
import { MAX_PRICE_USD, type ShopFilters } from "./shopQuery.ts";

type Update = (patch: Partial<ShopFilters>) => void;

function toggle<T>(values: readonly T[], value: T): T[] {
  return values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
}

/**
 * Category, size, colour, price and availability filters. Every value comes from the backend's
 * vocabulary; each change is applied at once (and becomes a history entry).
 */
export function FilterPanel({ filters, update }: { filters: ShopFilters; update: Update }) {
  return (
    <div className="flex flex-col gap-8">
      <Group legend="Category">
        <ul className="flex flex-col gap-2.5">
          {CATEGORIES.map((category) => (
            <li key={category}>
              <Check
                checked={filters.categories.includes(category)}
                onChange={() => update({ categories: toggle<Category>(filters.categories, category) })}
              >
                {CATEGORY_LABELS[category]}
              </Check>
            </li>
          ))}
        </ul>
      </Group>

      <Group legend="Size">
        <ul className="grid grid-cols-3 gap-1.5">
          {SIZES.map((size) => (
            <li key={size}>
              <SizeToggle
                checked={filters.sizes.includes(size)}
                onChange={() => update({ sizes: toggle<Size>(filters.sizes, size) })}
              >
                {sizeLabel(size)}
              </SizeToggle>
            </li>
          ))}
        </ul>
      </Group>

      <Group legend="Colour">
        <ul className="grid grid-cols-2 gap-x-4 gap-y-2.5 lg:grid-cols-1">
          {COLOURS.map((colour) => (
            <li key={colour}>
              <Check
                checked={filters.colours.includes(colour)}
                onChange={() => update({ colours: toggle<Colour>(filters.colours, colour) })}
              >
                <span
                  aria-hidden="true"
                  className="size-3 shrink-0 rounded-full ring-1 ring-ink/20"
                  style={{ backgroundColor: COLOUR_SWATCHES[colour] }}
                />
                {colourLabel(colour)}
              </Check>
            </li>
          ))}
        </ul>
      </Group>

      <PriceFilter
        // Re-mount when the URL's prices change (e.g. back/forward), so the inputs follow it.
        key={`${filters.minPrice ?? ""}-${filters.maxPrice ?? ""}`}
        min={filters.minPrice}
        max={filters.maxPrice}
        update={update}
      />

      <Group legend="Availability">
        <Check checked={filters.inStock} onChange={() => update({ inStock: !filters.inStock })}>
          In stock only
        </Check>
        <p className="mt-1.5 pl-6.5 text-xs text-ink-subtle">Includes items with low stock.</p>
      </Group>
    </div>
  );
}

function Group({ legend, children }: { legend: string; children: ReactNode }) {
  return (
    <fieldset className="border-t border-rule pt-5">
      <legend className="label-mono float-left mb-4 w-full text-ink-muted">{legend}</legend>
      <div className="clear-left">{children}</div>
    </fieldset>
  );
}

function Check({ checked, onChange, children }: { checked: boolean; onChange: () => void; children: ReactNode }) {
  return (
    <label className="flex cursor-pointer items-center gap-2.5 text-sm">
      <input type="checkbox" checked={checked} onChange={onChange} className="size-4 shrink-0 accent-forest-900" />
      {children}
    </label>
  );
}

/** A checkbox drawn as a size box; the native input stays focusable and announced. */
function SizeToggle({ checked, onChange, children }: { checked: boolean; onChange: () => void; children: ReactNode }) {
  return (
    <label className="block cursor-pointer">
      <input type="checkbox" checked={checked} onChange={onChange} className="peer sr-only" />
      <span className="block border border-rule py-2 text-center text-sm peer-checked:border-forest-900 peer-checked:bg-forest-900 peer-checked:text-on-dark peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-forest-900 hover:border-ink">
        {children}
      </span>
    </label>
  );
}

const WHOLE_DOLLARS = /^\d+$/;

/** "", or a whole number of dollars within the API's limit; undefined when invalid. */
function parseDollars(raw: string): number | null | undefined {
  const value = raw.trim();
  if (!value) return null;
  if (!WHOLE_DOLLARS.test(value)) return undefined;
  const dollars = Number(value);
  return dollars <= MAX_PRICE_USD ? dollars : undefined;
}

function PriceFilter({ min, max, update }: { min: number | null; max: number | null; update: Update }) {
  const [error, setError] = useState<string | null>(null);
  const id = useId();

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const minPrice = parseDollars(String(data.get("min") ?? ""));
    const maxPrice = parseDollars(String(data.get("max") ?? ""));
    if (minPrice === undefined || maxPrice === undefined) {
      setError(`Enter whole dollars between 0 and ${MAX_PRICE_USD.toLocaleString("en-US")}.`);
    } else if (minPrice !== null && maxPrice !== null && minPrice > maxPrice) {
      setError("The minimum can’t be more than the maximum.");
    } else {
      setError(null);
      update({ minPrice, maxPrice });
    }
  }

  const input =
    "w-full min-w-0 border-b border-ink bg-transparent py-1.5 pl-4 text-sm placeholder:text-ink-subtle aria-[invalid=true]:border-alert focus:outline-none focus-visible:outline-none";

  return (
    <form onSubmit={submit} noValidate>
      <Group legend="Price (USD)">
        <div className="flex items-end gap-3">
          {(["min", "max"] as const).map((name) => (
            <label key={name} className="flex-1 text-xs text-ink-muted">
              {name === "min" ? "Minimum" : "Maximum"}
              <span className="relative mt-1 block">
                <span aria-hidden="true" className="absolute top-1.5 left-0 text-sm text-ink">
                  $
                </span>
                <input
                  name={name}
                  inputMode="numeric"
                  autoComplete="off"
                  defaultValue={(name === "min" ? min : max) ?? ""}
                  placeholder={name === "min" ? "0" : "Any"}
                  aria-invalid={error ? true : undefined}
                  aria-describedby={error ? `${id}-error` : undefined}
                  className={input}
                />
              </span>
            </label>
          ))}
          <button type="submit" className="border border-ink px-3 py-1.5 text-sm hover:bg-ink hover:text-paper">
            Apply
          </button>
        </div>
        {error && (
          <p id={`${id}-error`} role="alert" className="mt-2 text-xs text-alert">
            {error}
          </p>
        )}
      </Group>
    </form>
  );
}
