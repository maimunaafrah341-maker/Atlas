import { Link } from "react-router";
import { CATEGORY_TINTS } from "../../components/catalogue/categoryTints.ts";
import { ImagePlaceholder } from "../../components/ui/ImagePlaceholder.tsx";
import { ArrowLink, PrimaryLink } from "../../components/ui/links.tsx";
import { CATEGORIES, CATEGORY_LABELS } from "../../lib/catalogue.ts";

const SECTION = "mx-auto max-w-[90rem] px-4 sm:px-8";

/** Split hero (Concept C): campaign placeholder left, statement and actions right. */
export function Hero() {
  return (
    <section className={`${SECTION} pt-6 sm:pt-8`}>
      <div className="grid border border-rule lg:grid-cols-[1.65fr_1fr]">
        <ImagePlaceholder
          label="Campaign image"
          ratio="16:10"
          tone="bg-forest-600"
          dark
          className="aspect-[16/10] lg:aspect-auto lg:min-h-[34rem]"
        />
        <div className="flex flex-col justify-center bg-paper-raised px-6 py-12 sm:px-10 lg:py-16">
          <p className="label-mono text-ink-muted">Independent outdoor label · India</p>
          <h1 className="mt-6 font-display text-5xl leading-[0.98] tracking-tight sm:text-6xl xl:text-7xl">
            Made for the long way through.
          </h1>
          <p className="mt-6 max-w-sm text-base leading-relaxed text-ink-muted">
            An independent outdoor label from India, shaped by changing ground, high passes and considered use.
          </p>
          <div className="mt-10 flex flex-wrap items-center gap-x-8 gap-y-5">
            <PrimaryLink to="/shop">Explore the catalogue</PrimaryLink>
            <a href="#categories" className="border-b border-ink pb-1 text-sm">
              Browse by category
            </a>
          </div>
        </div>
      </div>
    </section>
  );
}

/** "Equipment, organised by use": the five real catalogue categories. */
export function CategoryGrid() {
  return (
    <section id="categories" aria-labelledby="categories-heading" className={`${SECTION} scroll-mt-8 pt-24 sm:pt-32`}>
      <div className="grid gap-6 lg:grid-cols-[1fr_2fr]">
        <p className="label-mono text-ink-muted">Shop by category</p>
        <h2 id="categories-heading" className="font-display text-5xl leading-[1.02] tracking-tight sm:text-6xl">
          Equipment, organised by use.
        </h2>
      </div>
      <ul className="mt-14 grid grid-cols-2 gap-x-4 gap-y-10 md:grid-cols-3 lg:grid-cols-5">
        {CATEGORIES.map((category, index) => (
          <li key={category}>
            <Link to={`/shop?category=${category}`} className="group block">
              <ImagePlaceholder
                label={`Category / ${String(index + 1).padStart(2, "0")}`}
                ratio="1:1"
                tone={CATEGORY_TINTS[category]}
                className="aspect-square transition-opacity group-hover:opacity-90"
              />
              <span className="mt-4 flex items-center justify-between text-base">
                <span className="group-hover:underline">{CATEGORY_LABELS[category]}</span>
                <span aria-hidden="true">→</span>
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * A short, static brand statement. Atlas has no editorial content, so this section invents
 * no field reports or articles; it says what the label is and that it is a demonstration.
 */
export function BrandStory() {
  return (
    <section aria-labelledby="story-heading" className="mt-24 bg-forest-950 text-on-dark sm:mt-32">
      <div className="mx-auto grid max-w-[90rem] lg:grid-cols-2">
        <div className="flex flex-col justify-center px-4 py-16 sm:px-8 lg:py-24 lg:pr-16">
          <p className="label-mono text-on-dark-muted">About this label</p>
          <h2 id="story-heading" className="mt-6 font-display text-5xl leading-[1.02] tracking-tight sm:text-6xl">
            Considered equipment for changing weather.
          </h2>
          <p className="mt-6 max-w-md text-base leading-relaxed text-on-dark-muted">
            Atlas Demo Outfitters is a fictional outdoor label from India, created to show how the Atlas commerce
            assistant works with a real catalogue. Every product, price and stock status on this site comes from that
            catalogue. Nothing here is for sale.
          </p>
          <div className="mt-10">
            <ArrowLink to="/shop" tone="dark">
              See the full catalogue
            </ArrowLink>
          </div>
        </div>
        <ImagePlaceholder label="Brand image" ratio="4:3" tone="bg-forest-800" dark className="aspect-[4/3] lg:aspect-auto" />
      </div>
    </section>
  );
}

/**
 * The future shopping assistant, presented only as a concept. It has no controls: nothing on
 * this page implies that an assistant exists or that AI is answering anything.
 */
export function AssistantTeaser() {
  return (
    <section aria-labelledby="assistant-heading" className={`${SECTION} py-24 sm:py-32`}>
      <div className="grid gap-10 lg:grid-cols-[1.2fr_1fr] lg:items-end">
        <div>
          <p className="label-mono text-ink-muted">Atlas guide / Concept preview</p>
          <h2 id="assistant-heading" className="mt-6 font-display text-5xl leading-[1.02] tracking-tight sm:text-6xl">
            A quieter way to find the right equipment.
          </h2>
        </div>
        <div>
          <p className="label-mono inline-block border border-ink px-2 py-1">Concept · not available yet</p>
          <p className="mt-5 max-w-md text-base leading-relaxed text-ink-muted">
            A guided-shopping assistant is planned for a later release. It isn’t part of this demo and nothing here is
            powered by AI yet. When it arrives, every price and stock status it mentions will come from this catalogue.
          </p>
        </div>
      </div>
    </section>
  );
}
