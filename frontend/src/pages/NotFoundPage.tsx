import { Link } from "react-router";
import { PageIntro } from "../components/ui/PageIntro.tsx";

export function NotFoundPage() {
  return (
    <section className="mx-auto max-w-[90rem] px-4 py-24 sm:px-8">
      <PageIntro eyebrow="Error 404" title="This page isn’t on the map.">
        The address may be mistyped, or the page may have moved.
      </PageIntro>
      <Link to="/shop" className="mt-8 inline-block border-b border-ink pb-1 text-sm hover:border-forest-900">
        Browse the catalogue →
      </Link>
    </section>
  );
}
