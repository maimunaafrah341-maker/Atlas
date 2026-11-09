import { useId, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { CATEGORY_LABELS, type SortOrder } from "../../lib/catalogue.ts";
import { colourLabel, formatMoney, sizeLabel } from "../../lib/format.ts";
import {
  MAX_QUERY_LENGTH,
  effectiveSort,
  isSearchable,
  shopParams,
  type ShopFilters,
} from "./shopQuery.ts";

type Update = (patch: Partial<ShopFilters>) => void;

/** Search within the catalogue. Submits on Enter or the button, never per keystroke. */
export function SearchForm({ q, update }: { q: string; update: Update }) {
  const [error, setError] = useState<string | null>(null);
  const id = useId();

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const next = String(new FormData(event.currentTarget).get("q") ?? "").trim();
    if (next && !isSearchable(next)) {
      setError("Search for a word of at least two letters or numbers.");
      return;
    }
    setError(null);
    update({ q: next });
  }

  return (
    <form role="search" onSubmit={submit} noValidate className="w-full max-w-md">
      <label htmlFor={id} className="label-mono text-ink-muted">
        Search the catalogue
      </label>
      <div className="mt-2 flex items-center gap-3">
        <input
          id={id}
          name="q"
          type="search"
          maxLength={MAX_QUERY_LENGTH}
          defaultValue={q}
          placeholder="e.g. merino socks"
          aria-invalid={error ? true : undefined}
          aria-describedby={error ? `${id}-error` : undefined}
          className="min-w-0 flex-1 border-b border-ink bg-transparent py-2 text-base placeholder:text-ink-subtle aria-[invalid=true]:border-alert focus:outline-none focus-visible:outline-none"
        />
        <button type="submit" className="bg-forest-900 px-5 py-2.5 text-sm text-on-dark hover:bg-forest-950">
          Search
        </button>
      </div>
      {error && (
        <p id={`${id}-error`} role="alert" className="mt-2 text-xs text-alert">
          {error}
        </p>
      )}
    </form>
  );
}

const SORT_LABELS: Record<SortOrder, string> = {
  relevance: "Best match",
  featured: "Featured",
  price_asc: "Price: low to high",
  price_desc: "Price: high to low",
};

/** The backend's sort orders; "Best match" (relevance) is offered only with a search, as the API requires. */
export function SortSelect({ filters, update }: { filters: ShopFilters; update: Update }) {
  const id = useId();
  const options: SortOrder[] = filters.q
    ? ["relevance", "featured", "price_asc", "price_desc"]
    : ["featured", "price_asc", "price_desc"];
  return (
    // min-w-0: on narrow screens the select may shrink beside the Filters button instead of overflowing the page.
    <div className="flex min-w-0 items-center gap-3">
      <label htmlFor={id} className="label-mono shrink-0 text-ink-muted">
        Sort
      </label>
      <select
        id={id}
        value={effectiveSort(filters)}
        onChange={(event) => update({ sort: event.target.value as SortOrder })}
        className="min-w-0 border-b border-ink bg-transparent py-1.5 pr-1 text-sm focus:outline-none focus-visible:outline-2"
      >
        {options.map((sort) => (
          <option key={sort} value={sort}>
            {SORT_LABELS[sort]}
          </option>
        ))}
      </select>
    </div>
  );
}

const usd = (dollars: number) => formatMoney({ amount_minor: dollars * 100, currency: "USD" }).replace(/\.00$/, "");

function priceLabel(min: number | null, max: number | null): string {
  if (min !== null && max !== null) return `${usd(min)} – ${usd(max)}`;
  return min !== null ? `From ${usd(min)}` : `Up to ${usd(max ?? 0)}`;
}

/** Each active filter as a removable chip, plus "Clear all". Renders nothing when unfiltered. */
export function ActiveFilters({ filters, update, onClear }: { filters: ShopFilters; update: Update; onClear: () => void }) {
  const chips: { key: string; label: string; remove: Partial<ShopFilters> }[] = [];
  if (filters.q) chips.push({ key: "q", label: `“${filters.q}”`, remove: { q: "" } });
  for (const category of filters.categories)
    chips.push({ key: `c-${category}`, label: CATEGORY_LABELS[category], remove: { categories: filters.categories.filter((c) => c !== category) } });
  for (const size of filters.sizes)
    chips.push({ key: `s-${size}`, label: `Size ${sizeLabel(size)}`, remove: { sizes: filters.sizes.filter((s) => s !== size) } });
  for (const colour of filters.colours)
    chips.push({ key: `o-${colour}`, label: colourLabel(colour), remove: { colours: filters.colours.filter((c) => c !== colour) } });
  if (filters.minPrice !== null || filters.maxPrice !== null)
    chips.push({ key: "price", label: priceLabel(filters.minPrice, filters.maxPrice), remove: { minPrice: null, maxPrice: null } });
  if (filters.inStock) chips.push({ key: "stock", label: "In stock only", remove: { inStock: false } });

  if (chips.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2" aria-label="Active filters" role="group">
      {chips.map((chip) => (
        <button
          key={chip.key}
          type="button"
          onClick={() => update(chip.remove)}
          aria-label={`Remove ${chip.key === "q" ? "search" : "filter"}: ${chip.label}`}
          className="flex items-center gap-2 bg-paper-raised px-3 py-1.5 text-sm ring-1 ring-rule hover:ring-ink"
        >
          {chip.label}
          <span aria-hidden="true" className="text-ink-muted">
            ×
          </span>
        </button>
      ))}
      <button type="button" onClick={onClear} className="px-2 py-1.5 text-sm underline underline-offset-4 hover:text-ink-muted">
        Clear all
      </button>
    </div>
  );
}

/** Page numbers to show: all of them when few, otherwise the ends and the current neighbourhood. */
export function pageItems(current: number, total: number): (number | "gap")[] {
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);
  const pages = new Set([1, total, current - 1, current, current + 1].filter((page) => page >= 1 && page <= total));
  const sorted = [...pages].sort((a, b) => a - b);
  const items: (number | "gap")[] = [];
  sorted.forEach((page, index) => {
    if (index > 0 && page - (sorted[index - 1] as number) > 1) items.push("gap");
    items.push(page);
  });
  return items;
}

/** Real links (so pages can be opened, bookmarked and navigated with back/forward). */
export function Pagination({
  filters,
  totalPages,
  onNavigate,
}: {
  filters: ShopFilters;
  totalPages: number;
  onNavigate: () => void;
}) {
  if (totalPages <= 1) return null;
  const current = filters.page;
  const href = (page: number) => {
    const search = shopParams({ ...filters, page }).toString();
    return `/shop${search ? `?${search}` : ""}`;
  };
  const step = "label-mono px-2 py-2 hover:underline";
  return (
    <nav aria-label="Pagination" className="mt-16 flex items-center justify-between gap-4 border-t border-rule pt-6">
      {current > 1 ? (
        <Link to={href(current - 1)} onClick={onNavigate} className={step}>
          ← Previous
        </Link>
      ) : (
        <span aria-hidden="true" className={`${step} text-ink-subtle hover:no-underline`}>
          ← Previous
        </span>
      )}
      <ol className="flex flex-wrap items-center justify-center gap-1">
        {pageItems(current, totalPages).map((item, index) =>
          item === "gap" ? (
            <li key={`gap-${index}`} aria-hidden="true" className="px-1 text-ink-subtle">
              …
            </li>
          ) : (
            <li key={item}>
              <Link
                to={href(item)}
                onClick={onNavigate}
                aria-label={`Page ${item}`}
                aria-current={item === current ? "page" : undefined}
                className={`grid size-9 place-items-center text-sm ${item === current ? "bg-forest-900 text-on-dark" : "hover:underline"}`}
              >
                {item}
              </Link>
            </li>
          ),
        )}
      </ol>
      {current < totalPages ? (
        <Link to={href(current + 1)} onClick={onNavigate} className={step}>
          Next →
        </Link>
      ) : (
        <span aria-hidden="true" className={`${step} text-ink-subtle hover:no-underline`}>
          Next →
        </span>
      )}
    </nav>
  );
}
