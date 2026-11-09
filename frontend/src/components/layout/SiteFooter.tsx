import { Link } from "react-router";
import { CATEGORIES, CATEGORY_LABELS } from "../../lib/catalogue.ts";
import { Wordmark } from "./Wordmark.tsx";

/** Only truthful, working links: the catalogue, its categories and the system status page. */
export function SiteFooter() {
  return (
    <footer className="bg-forest-950 text-on-dark">
      <div className="mx-auto grid max-w-[90rem] gap-12 px-4 py-14 sm:px-8 md:grid-cols-[2fr_1fr_1fr]">
        <div className="max-w-sm">
          <Wordmark tone="dark" />
          <p className="mt-6 text-sm leading-relaxed text-on-dark-muted">
            A fictional outdoor apparel brand, created to demonstrate the Atlas commerce assistant. Products, prices
            and stock come from the live demo catalogue; nothing can be purchased yet.
          </p>
        </div>
        <nav aria-label="Catalogue">
          <h2 className="label-mono text-on-dark-muted">Catalogue</h2>
          <ul className="mt-4 space-y-2.5 text-sm">
            <li>
              <Link to="/shop" className="hover:underline">
                Shop all
              </Link>
            </li>
            {CATEGORIES.map((category) => (
              <li key={category}>
                <Link to={`/shop?category=${category}`} className="hover:underline">
                  {CATEGORY_LABELS[category]}
                </Link>
              </li>
            ))}
          </ul>
        </nav>
        <nav aria-label="About this demo">
          <h2 className="label-mono text-on-dark-muted">This demo</h2>
          <ul className="mt-4 space-y-2.5 text-sm">
            <li>
              <Link to="/status" className="hover:underline">
                System status
              </Link>
            </li>
          </ul>
        </nav>
      </div>
      <div className="border-t border-white/15">
        <div className="mx-auto flex max-w-[90rem] flex-wrap justify-between gap-2 px-4 py-5 sm:px-8">
          <p className="label-mono text-on-dark-muted">© {new Date().getFullYear()} Atlas Demo Outfitters</p>
          <p className="label-mono text-on-dark-muted">Fictional demonstration · India</p>
        </div>
      </div>
    </footer>
  );
}
