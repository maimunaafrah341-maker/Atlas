import { useMemo, useRef, useState, type ReactNode } from "react";
import { Link, useSearchParams } from "react-router";
import { useCatalogueLanguage } from "../app/catalogueLanguage.tsx";
import { CardSkeletons } from "../components/catalogue/CardSkeletons.tsx";
import { ProductCard } from "../components/catalogue/ProductCard.tsx";
import { PageIntro } from "../components/ui/PageIntro.tsx";
import { FilterPanel } from "../features/shop/FilterPanel.tsx";
import { ActiveFilters, Pagination, SearchForm, SortSelect } from "../features/shop/ShopControls.tsx";
import {
  NO_FILTERS,
  SHOP_PAGE_SIZE,
  activeFilterCount,
  changeFilters,
  hasAnyFilter,
  hasVariantFilters,
  parseShopParams,
  shopParams,
  toProductQuery,
  type ShopFilters,
} from "../features/shop/shopQuery.ts";
import { listProducts, productQueryString, type ProductPage } from "../lib/api.ts";
import { CATEGORY_LABELS } from "../lib/catalogue.ts";
import { useApiResource, type Resource } from "../lib/useApiResource.ts";

const GRID = "grid grid-cols-2 gap-x-3 gap-y-12 sm:gap-x-4 xl:grid-cols-3";
const FILTERS_ID = "shop-filters";

/**
 * The catalogue: search, filters, sort and pages, all held in the URL and answered live by
 * GET /api/catalog/products. The request is keyed by its exact query string, so it is sent once
 * per distinct query and language, and a stale response is never shown.
 */
export function ShopPage() {
  const [params, setParams] = useSearchParams();
  const filters = useMemo(() => parseShopParams(params), [params]);
  const { language } = useCatalogueLanguage();
  const query = toProductQuery(filters, language);
  const [result, retry] = useApiResource(productQueryString(query), (signal) => listProducts(query, signal));
  const [filtersOpen, setFiltersOpen] = useState(false);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const resultsHeading = useRef<HTMLHeadingElement>(null);

  // Filter changes keep the scroll position; each one is a history entry, so back undoes it.
  const apply = (next: ShopFilters) => setParams(shopParams(next), { preventScrollReset: true });
  const update = (patch: Partial<ShopFilters>) => apply(changeFilters(filters, patch));
  const clearAll = () => apply(changeFilters(NO_FILTERS, { sort: filters.sort }));

  const onlyCategory = filters.categories.length === 1 && !filters.q ? filters.categories[0] : undefined;
  const filterCount = activeFilterCount(filters);

  function closeFilters() {
    setFiltersOpen(false);
    toggleRef.current?.focus();
  }

  return (
    <div className="mx-auto max-w-[90rem] px-4 pt-16 pb-24 sm:px-8 sm:pt-20">
      <PageIntro eyebrow="Catalogue" title={onlyCategory ? CATEGORY_LABELS[onlyCategory] : "Shop all"}>
        Every product, price and stock state here comes live from the demo catalogue. Prices are in US dollars.
      </PageIntro>

      <div className="mt-12 flex flex-wrap items-end justify-between gap-x-8 gap-y-6 border-t border-rule pt-8">
        {/* Re-mount on a new search (e.g. back/forward or the header search) so the field follows the URL. */}
        <SearchForm key={filters.q} q={filters.q} update={update} />
        <div className="flex w-full items-center justify-between gap-4 sm:w-auto sm:gap-6">
          <button
            ref={toggleRef}
            type="button"
            aria-expanded={filtersOpen}
            aria-controls={FILTERS_ID}
            aria-label={filterCount > 0 ? `Filters (${filterCount} active)` : "Filters"}
            onClick={() => setFiltersOpen((open) => !open)}
            className="label-mono flex items-center gap-2 border border-ink px-4 py-2.5 hover:bg-ink hover:text-paper lg:hidden"
          >
            Filters
            {filterCount > 0 && <span className="text-[0.625rem]">({filterCount})</span>}
          </button>
          <SortSelect filters={filters} update={update} />
        </div>
      </div>

      <div className="mt-6">
        <ActiveFilters filters={filters} update={update} onClear={clearAll} />
      </div>

      <div className="mt-8 lg:grid lg:grid-cols-[14rem_minmax(0,1fr)] lg:gap-12">
        <aside
          id={FILTERS_ID}
          aria-label="Filters"
          onKeyDown={(event) => event.key === "Escape" && filtersOpen && closeFilters()}
          className={`${filtersOpen ? "block" : "hidden"} mb-12 lg:mb-0 lg:block`}
        >
          <FilterPanel filters={filters} update={update} />
          <button
            type="button"
            onClick={closeFilters}
            className="mt-8 w-full bg-forest-900 px-5 py-3 text-sm text-on-dark hover:bg-forest-950 lg:hidden"
          >
            {result.kind === "loaded" ? `Show ${countLabel(result.data.total)}` : "Show results"}
          </button>
        </aside>

        <section aria-labelledby="results-heading">
          <div className="flex items-baseline justify-between gap-4">
            <h2 id="results-heading" ref={resultsHeading} tabIndex={-1} className="label-mono text-ink-muted focus:outline-none">
              Results
            </h2>
            <p className="label-mono" aria-live="polite">
              {result.kind === "loaded" && summary(result.data, filters)}
            </p>
          </div>
          <div className="mt-6">
            <Results
              result={result}
              filters={filters}
              retry={retry}
              clearAll={clearAll}
              onPageChange={() => resultsHeading.current?.focus({ preventScroll: true })}
            />
          </div>
        </section>
      </div>
    </div>
  );
}

function countLabel(total: number): string {
  return `${total} ${total === 1 ? "product" : "products"}`;
}

function summary(data: ProductPage, filters: ShopFilters): string {
  const pages = data.total_pages > 1 ? ` · Page ${filters.page} of ${data.total_pages}` : "";
  return `${countLabel(data.total)}${filters.q ? ` for “${filters.q}”` : ""}${pages}`;
}

function Results({
  result,
  filters,
  retry,
  clearAll,
  onPageChange,
}: {
  result: Resource<ProductPage>;
  filters: ShopFilters;
  retry: () => void;
  clearAll: () => void;
  onPageChange: () => void;
}) {
  if (result.kind === "loading") return <CardSkeletons count={6} className={GRID} />;

  if (result.kind === "error") {
    // 422: the API rejected the request itself (e.g. a hand-edited URL); retrying can't help.
    if (result.error?.code === "invalid_request") {
      return (
        <Notice role="alert" message={`These filters can’t be applied. ${result.message}`}>
          <button type="button" onClick={clearAll} className={BUTTON}>
            Clear all filters
          </button>
        </Notice>
      );
    }
    return (
      <Notice role="alert" message={`The catalogue couldn’t be loaded. ${result.message}`}>
        <button type="button" onClick={retry} className={BUTTON}>
          Try again
        </button>
      </Notice>
    );
  }

  const { items, total, total_pages } = result.data;
  if (total === 0) {
    return hasAnyFilter(filters) ? (
      <Notice message="No products match these filters. Try removing one, or start again.">
        <button type="button" onClick={clearAll} className={BUTTON}>
          Clear all filters
        </button>
      </Notice>
    ) : (
      <Notice message="The catalogue is empty right now." />
    );
  }
  if (items.length === 0) {
    // A page past the end (an old link, or the catalogue shrank).
    const last = shopParams({ ...filters, page: total_pages }).toString();
    return (
      <Notice message={`There’s no page ${filters.page}. These results end at page ${total_pages}.`}>
        <Link to={`/shop?${last}`} className={BUTTON}>
          Go to page {total_pages}
        </Link>
      </Notice>
    );
  }

  const offset = (filters.page - 1) * SHOP_PAGE_SIZE;
  return (
    <>
      <ul className={GRID}>
        {items.map((product, index) => (
          <li key={product.slug}>
            <ProductCard product={product} index={offset + index} matchingOnly={hasVariantFilters(filters)} />
          </li>
        ))}
      </ul>
      <Pagination filters={filters} totalPages={total_pages} onNavigate={onPageChange} />
    </>
  );
}

const BUTTON = "border border-ink px-4 py-2 text-sm hover:bg-ink hover:text-paper";

function Notice({ message, role, children }: { message: string; role?: "alert"; children?: ReactNode }) {
  return (
    <div role={role} className="flex flex-wrap items-center gap-4 bg-paper-raised p-6">
      <p className={role === "alert" ? "text-alert" : "text-ink-muted"}>{message}</p>
      {children}
    </div>
  );
}
