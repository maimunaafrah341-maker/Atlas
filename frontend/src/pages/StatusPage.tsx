import { HealthStatus } from "../features/health/HealthStatus.tsx";
import { PageIntro } from "../components/ui/PageIntro.tsx";

/** The M0 health check, kept as a diagnostics page for the demo. */
export function StatusPage() {
  return (
    <section className="mx-auto max-w-3xl px-4 py-20 sm:px-8">
      <PageIntro eyebrow="This demo" title="System status">
        Whether the Atlas backend and its database are reachable. A sleeping demo server can take up to a minute to wake.
      </PageIntro>
      <div className="mt-10">
        <HealthStatus />
      </div>
    </section>
  );
}
