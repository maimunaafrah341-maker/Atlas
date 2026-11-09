import { useCatalogueLanguage } from "../../app/catalogueLanguage.tsx";
import { CardSkeletons } from "../../components/catalogue/CardSkeletons.tsx";
import { ProductCard } from "../../components/catalogue/ProductCard.tsx";
import { ArrowLink } from "../../components/ui/links.tsx";
import { listProducts } from "../../lib/api.ts";
import { useApiResource } from "../../lib/useApiResource.ts";

const COUNT = 4;

/** "Selected equipment": the catalogue's first products in its own featured order, live from the API. */
export function SelectedEquipment() {
  const { language } = useCatalogueLanguage();
  const [page, retry] = useApiResource(language, (signal) =>
    listProducts({ lang: language, sort: "featured", page_size: COUNT }, signal),
  );

  return (
    <section aria-labelledby="selected-heading" className="mx-auto max-w-[90rem] px-4 pt-24 sm:px-8 sm:pt-32">
      <div className="flex flex-wrap items-end justify-between gap-6">
        <div>
          <p className="label-mono text-ink-muted" aria-live="polite">
            {page.kind === "loaded" ? `Current catalogue / ${page.data.total} products` : "Current catalogue"}
          </p>
          <h2 id="selected-heading" className="mt-4 font-display text-5xl leading-[1.02] tracking-tight sm:text-6xl">
            Selected equipment
          </h2>
        </div>
        <ArrowLink to="/shop">View all products</ArrowLink>
      </div>

      <div className="mt-12">
        {page.kind === "loading" && (
          <CardSkeletons count={COUNT} className="grid grid-cols-1 gap-x-4 gap-y-12 sm:grid-cols-2 lg:grid-cols-4" />
        )}
        {page.kind === "error" && (
          <div role="alert" className="flex flex-wrap items-center gap-4 border border-rule bg-paper-raised p-6">
            <p className="text-alert">The catalogue couldn’t be loaded. {page.message}</p>
            <button type="button" onClick={retry} className="border border-ink px-4 py-2 text-sm hover:bg-ink hover:text-paper">
              Try again
            </button>
          </div>
        )}
        {page.kind === "loaded" && page.data.items.length === 0 && (
          <p className="border border-rule bg-paper-raised p-6 text-ink-muted">The catalogue is empty right now.</p>
        )}
        {page.kind === "loaded" && page.data.items.length > 0 && (
          <ul className="grid grid-cols-1 gap-x-4 gap-y-12 sm:grid-cols-2 lg:grid-cols-4">
            {page.data.items.map((product, index) => (
              <li key={product.slug}>
                <ProductCard product={product} index={index} />
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  );
}
