import { Link } from "react-router";
import type { Product } from "../../lib/api.ts";
import { CATEGORY_LABELS } from "../../lib/catalogue.ts";
import { STOCK_LABELS, formatPriceRange, productAvailability } from "../../lib/format.ts";
import { ImagePlaceholder } from "../ui/ImagePlaceholder.tsx";
import { CATEGORY_TINTS } from "./categoryTints.ts";
import { TranslationNote } from "./TranslationNote.tsx";

const DOT: Record<string, string> = { in_stock: "bg-ok", low_stock: "bg-warn", out_of_stock: "bg-ink-subtle" };

/** A product summary: placeholder image, category, name, live price and stock status. */
export function ProductCard({
  product,
  index,
  matchingOnly = false,
}: {
  product: Product;
  index: number;
  /**
   * Summarise only the variants that match the active size, colour, price or stock filters
   * (`matches_filters`), as the price range already does. Off by default: product-wide.
   */
  matchingOnly?: boolean;
}) {
  const matching = matchingOnly ? product.variants.filter((variant) => variant.matches_filters) : [];
  // If nothing is flagged (stock changed between search and load), fall back to every variant, as the API does.
  const scoped = matchingOnly && matching.length > 0;
  const availability = productAvailability(scoped ? matching : product.variants);
  const numeral = String(index + 1).padStart(2, "0");
  return (
    <article className="group relative flex flex-col">
      <ImagePlaceholder
        label={`Product image / ${numeral}`}
        ratio="4:5"
        numeral={numeral}
        tone={CATEGORY_TINTS[product.category]}
        className="aspect-[4/5] transition-opacity group-hover:opacity-90"
      />
      <p className="label-mono mt-4 text-ink-muted">{CATEGORY_LABELS[product.category]}</p>
      <h3 className="mt-1.5 text-lg leading-snug">
        {/* The whole card is clickable through this link's stretched hit area. */}
        <Link to={`/products/${product.slug}`} className="after:absolute after:inset-0 hover:underline focus-visible:underline">
          {product.name}
        </Link>
      </h3>
      <p className="mt-1 text-sm">{formatPriceRange(product.price_range)}</p>
      <p className="mt-2 flex items-center gap-2 text-xs text-ink-muted">
        <span aria-hidden="true" className={`size-1.5 rounded-full ${DOT[availability]}`} />
        {STOCK_LABELS[availability]}
        {scoped && " · matching options"}
      </p>
      <TranslationNote product={product} className="mt-2" />
    </article>
  );
}
