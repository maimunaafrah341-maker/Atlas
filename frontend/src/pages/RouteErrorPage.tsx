import { Link } from "react-router";
import { AnnouncementBar } from "../components/layout/AnnouncementBar.tsx";
import { PageIntro } from "../components/ui/PageIntro.tsx";

/** Last-resort screen if a route fails to render. Never shows the raw error to customers. */
export function RouteErrorPage() {
  return (
    <div className="min-h-screen">
      <AnnouncementBar />
      <section className="mx-auto max-w-[90rem] px-4 py-24 sm:px-8">
        <PageIntro eyebrow="Something went wrong" title="This page couldn’t be shown.">
          Please try again. If it keeps happening, the demo may be restarting.
        </PageIntro>
        <Link to="/" className="mt-8 inline-block border-b border-ink pb-1 text-sm">
          Back to the homepage →
        </Link>
      </section>
    </div>
  );
}
