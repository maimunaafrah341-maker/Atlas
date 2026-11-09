import { useId, type ReactNode } from "react";
import { Link, useParams, useSearchParams } from "react-router";
import { useCatalogueLanguage } from "../app/catalogueLanguage.tsx";
import { CATEGORY_TINTS } from "../components/catalogue/categoryTints.ts";
import { TranslationNote } from "../components/catalogue/TranslationNote.tsx";
import { ImagePlaceholder } from "../components/ui/ImagePlaceholder.tsx";
import { PageIntro } from "../components/ui/PageIntro.tsx";
import { AddToBag } from "../features/cart/AddToBag.tsx";
import { ShippingEstimate } from "../features/product/ShippingEstimate.tsx";
import { VariantPicker } from "../features/product/VariantPicker.tsx";
import { chooseColour, chooseSize, findVariant, settle, type Selection } from "../features/product/variantSelection.ts";
import { getProduct, type Product, type Variant } from "../lib/api.ts";
import { CATEGORY_LABELS, TAG_LABELS, isTag } from "../lib/catalogue.ts";
import { STOCK_LABELS, colourLabel, formatMoney, formatPriceRange, sizeLabel } from "../lib/format.ts";
import { useApiResource } from "../lib/useApiResource.ts";

const DOT = { in_stock: "bg-ok", low_stock: "bg-warn", out_of_stock: "bg-ink-subtle" } as const;

/**
 * One product, live from GET /api/catalog/products/{slug} in the catalogue language. The chosen
 * size and colour live in the URL (?size=&colour=, replacing the entry), so a link or refresh
 * keeps them without adding history entries for each click.
 */
export function ProductPage() {
  const { slug = "" } = useParams();
  const { language } = useCatalogueLanguage();
  const [result, retry] = useApiResource(`${slug}|${language}`, (signal) => getProduct(slug, language, signal));

  if (result.kind === "loading") return <ProductSkeleton />;
  if (result.kind === "error") {
    const error = result.error;
    // 404, or a slug the API rejects outright (422): either way, no such product.
    if (error?.code === "product_not_found" || error?.status === 404 || error?.code === "invalid_request") {
      return <ProductNotFound />;
    }
    return (
      <Shell>
        <div role="alert" className="flex flex-wrap items-center gap-4 bg-paper-raised p-6">
          <p className="text-alert">This product couldn’t be loaded. {result.message}</p>
          <button type="button" onClick={retry} className="border border-ink px-4 py-2 text-sm hover:bg-ink hover:text-paper">
            Try again
          </button>
        </div>
      </Shell>
    );
  }
  return <ProductDetail product={result.data} />;
}

function Shell({ children }: { children: ReactNode }) {
  return <div className="mx-auto max-w-[90rem] px-4 pt-8 pb-24 sm:px-8 sm:pt-10">{children}</div>;
}

function ProductDetail({ product }: { product: Product }) {
  const [params, setParams] = useSearchParams();
  const { variants } = product;
  const selection = settle(variants, { size: params.get("size"), colour: params.get("colour") });
  const selected = findVariant(variants, selection.size, selection.colour);

  const select = (next: Selection) => {
    const search = new URLSearchParams();
    if (next.size) search.set("size", next.size);
    if (next.colour) search.set("colour", next.colour);
    setParams(search, { replace: true, preventScrollReset: true });
  };

  return (
    <Shell>
      <title>{`${product.name} · Atlas Demo Outfitters`}</title>
      <Breadcrumb product={product} />

      <div className="mt-8 grid gap-10 lg:grid-cols-2 lg:gap-16">
        {/*
          4:5 at any size. From lg the width is capped by the viewport height, so the whole image
          fits on screen (no cropping or distortion of future photography) and can stay in view
          while the details column scrolls past it.
        */}
        <ImagePlaceholder
          label="Product image"
          ratio="4:5"
          tone={CATEGORY_TINTS[product.category]}
          className="aspect-[4/5] w-full lg:sticky lg:top-8 lg:w-[min(100%,max(18rem,calc((100svh_-_13rem)_*_0.8)))] lg:self-start"
        />

        <div className="flex flex-col gap-8">
          <div>
            <p className="label-mono text-ink-muted">{CATEGORY_LABELS[product.category]}</p>
            <h1 className="mt-3 font-display text-4xl leading-[1.05] tracking-tight sm:text-5xl">{product.name}</h1>
            <p className="mt-4 text-xl" aria-live="polite">
              {selected ? formatMoney(selected.price) : formatPriceRange(product.price_range)}
            </p>
            <TranslationNote product={product} className="mt-3" />
          </div>

          <p className="max-w-prose leading-relaxed text-ink-muted">{product.description}</p>

          <VariantPicker
            variants={variants}
            selection={selection}
            onSize={(size) => select(chooseSize(variants, selection, size))}
            onColour={(colour) => select(chooseColour(variants, selection, colour))}
          />

          <SelectedAvailability selected={selected} selection={selection} />
          <AddToBag key={selected?.sku ?? "none"} selected={selected} />
          <ShippingEstimate />
          <Details product={product} selected={selected} />
        </div>
      </div>

      <VariantTable variants={variants} selected={selected} />
    </Shell>
  );
}

function Breadcrumb({ product }: { product: Product }) {
  return (
    <nav aria-label="Breadcrumb">
      <ol className="label-mono flex flex-wrap items-center gap-2 text-ink-muted">
        <li>
          <Link to="/shop" className="hover:text-ink hover:underline">
            Shop all
          </Link>
        </li>
        <li aria-hidden="true">/</li>
        <li>
          <Link to={`/shop?category=${product.category}`} className="hover:text-ink hover:underline">
            {CATEGORY_LABELS[product.category]}
          </Link>
        </li>
        <li aria-hidden="true">/</li>
        <li aria-current="page" className="text-ink">
          {product.name}
        </li>
      </ol>
    </nav>
  );
}

/** The chosen variant's own status: never a product-wide summary, never a count (D-5). */
function SelectedAvailability({ selected, selection }: { selected: Variant | null; selection: Selection }) {
  let text: ReactNode;
  if (selected) {
    text = (
      <>
        <span aria-hidden="true" className={`size-2 rounded-full ${DOT[selected.stock_status]}`} />
        <span>
          {colourLabel(selected.colour)}, {sizeLabel(selected.size)}: <strong className="font-semibold">{STOCK_LABELS[selected.stock_status]}</strong>
        </span>
      </>
    );
  } else {
    const missing = [selection.colour ? null : "colour", selection.size ? null : "size"].filter(Boolean).join(" and ");
    text = <span className="text-ink-muted">Choose a {missing} to see its availability.</span>;
  }
  return (
    <p className="flex items-center gap-2.5 text-sm" aria-live="polite">
      {text}
    </p>
  );
}

function Details({ product, selected }: { product: Product; selected: Variant | null }) {
  const tags = product.tags.filter(isTag);
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-3 border-t border-rule pt-6 text-sm">
      {tags.length > 0 && (
        <>
          <dt className="label-mono pt-0.5 text-ink-muted">Tags</dt>
          <dd>{tags.map((tag) => TAG_LABELS[tag]).join(", ")}</dd>
        </>
      )}
      <dt className="label-mono pt-0.5 text-ink-muted">SKU</dt>
      <dd className="font-mono text-xs">{selected ? selected.sku : "Choose an option"}</dd>
    </dl>
  );
}

/** Every variant's own price and availability, so no summary stands in for a specific option. */
function VariantTable({ variants, selected }: { variants: readonly Variant[]; selected: Variant | null }) {
  const id = useId();
  const cell = "py-3 pr-4 text-left align-middle last:pr-0";
  return (
    <section aria-labelledby={id} className="mt-20 border-t border-rule pt-6">
      <h2 id={id} className="label-mono text-ink-muted">
        All options
      </h2>
      {/* Spans the content width; a contained scroll (never the page) if a narrow screen ever needs it. */}
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[18rem] text-sm">
          <caption className="sr-only">Price and availability of each size and colour</caption>
          <thead>
            <tr className="label-mono border-b border-rule text-ink-muted">
              <th scope="col" className={`${cell} font-normal`}>Colour</th>
              <th scope="col" className={`${cell} font-normal`}>Size</th>
              <th scope="col" className={`${cell} font-normal`}>Price</th>
              <th scope="col" className={`${cell} font-normal`}>Availability</th>
              <th scope="col" className={`${cell} hidden font-normal sm:table-cell`}>SKU</th>
            </tr>
          </thead>
          <tbody>
            {variants.map((variant) => {
              const isSelected = variant === selected;
              return (
                <tr key={variant.sku} className={`border-b border-rule ${isSelected ? "bg-paper-raised" : ""}`}>
                  <td className={`${cell} ${isSelected ? "font-semibold" : ""}`}>
                    <span className="inline-flex items-center gap-2 whitespace-nowrap">
                      {colourLabel(variant.colour)}
                      {isSelected && <span className="bg-forest-900 px-1.5 py-0.5 text-xs font-normal text-on-dark">Selected</span>}
                    </span>
                  </td>
                  <td className={cell}>{sizeLabel(variant.size)}</td>
                  <td className={cell}>{formatMoney(variant.price)}</td>
                  <td className={cell}>
                    <span className="inline-flex items-center gap-2 whitespace-nowrap">
                      <span aria-hidden="true" className={`size-1.5 rounded-full ${DOT[variant.stock_status]}`} />
                      {STOCK_LABELS[variant.stock_status]}
                    </span>
                  </td>
                  <td className={`${cell} hidden font-mono text-xs text-ink-muted sm:table-cell`}>{variant.sku}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ProductNotFound() {
  return (
    <section className="mx-auto max-w-[90rem] px-4 py-24 sm:px-8">
      <PageIntro eyebrow="Product not found" title="This product isn’t in the catalogue.">
        The link may be out of date, or the product may have been removed.
      </PageIntro>
      <Link to="/shop" className="mt-8 inline-block border-b border-ink pb-1 text-sm hover:border-forest-900">
        Browse the catalogue →
      </Link>
    </section>
  );
}

function ProductSkeleton() {
  return (
    <Shell>
      <div role="status" className="grid animate-pulse gap-10 lg:grid-cols-2 lg:gap-16">
        <span className="sr-only">Loading product…</span>
        <div aria-hidden="true" className="aspect-[4/5] w-full bg-tint-sand lg:w-[min(100%,max(18rem,calc((100svh_-_13rem)_*_0.8)))]" />
        <div aria-hidden="true" className="flex flex-col gap-4">
          <div className="h-3 w-1/4 bg-tint-sand" />
          <div className="h-10 w-3/4 bg-tint-sand" />
          <div className="h-5 w-1/5 bg-tint-sand" />
          <div className="mt-6 h-24 bg-tint-sand" />
        </div>
      </div>
    </Shell>
  );
}
