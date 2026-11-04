import { useCallback, useEffect, useState } from "react";
import { ApiError, getHealth, type HealthReport } from "../../lib/api.ts";

type State =
  | { kind: "loading" }
  | { kind: "loaded"; report: HealthReport }
  | { kind: "error"; message: string };

export function HealthStatus() {
  const [state, setState] = useState<State>({ kind: "loading" });
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    getHealth(controller.signal)
      .then((report) => setState({ kind: "loaded", report }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ kind: "error", message: error instanceof ApiError ? error.message : "Something went wrong." });
      });
    return () => controller.abort();
  }, [attempt]);

  const retry = useCallback(() => {
    setState({ kind: "loading" });
    setAttempt((n) => n + 1);
  }, []);

  return (
    <section
      aria-labelledby="system-status"
      className="rounded-xl border border-stone-200 bg-white p-5 shadow-sm dark:border-stone-800 dark:bg-stone-900"
    >
      <h2 id="system-status" className="text-lg font-semibold">
        System status
      </h2>
      <div aria-live="polite" className="mt-3">
        {state.kind === "loading" && (
          <div className="text-stone-600 dark:text-stone-400">
            <p>Checking the backend…</p>
            <p className="mt-1 text-sm">If the server was asleep, this can take up to a minute.</p>
          </div>
        )}

        {state.kind === "error" && (
          <div role="alert" className="flex flex-wrap items-center gap-3">
            <p className="text-red-700 dark:text-red-400">{state.message}</p>
            <RetryButton onClick={retry} />
          </div>
        )}

        {state.kind === "loaded" && <Report report={state.report} onRetry={retry} />}
      </div>
    </section>
  );
}

function Report({ report, onRetry }: { report: HealthReport; onRetry: () => void }) {
  const healthy = report.status === "ok";
  const { database } = report;
  return (
    <div className="space-y-3">
      <p className="flex items-center gap-2 font-medium">
        <span
          aria-hidden="true"
          className={`inline-block size-2.5 rounded-full ${healthy ? "bg-emerald-500" : "bg-amber-500"}`}
        />
        {healthy ? "Backend and database operational" : "Degraded: the database is unavailable or not migrated"}
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-stone-500">Version</dt>
        <dd className="font-mono">{report.version}</dd>
        <dt className="text-stone-500">Environment</dt>
        <dd>{report.environment}</dd>
        <dt className="text-stone-500">Database</dt>
        <dd>
          {database.dialect} · {database.status}
          {database.latency_ms !== null && ` · ${database.latency_ms} ms`}
        </dd>
        <dt className="text-stone-500">Schema</dt>
        <dd>{database.schema_up_to_date ? `up to date (${database.migration_revision})` : "not at latest migration"}</dd>
      </dl>
      {!healthy && <RetryButton onClick={onRetry} />}
    </div>
  );
}

function RetryButton({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="rounded-md border border-stone-300 px-3 py-1.5 text-sm font-medium hover:bg-stone-100 focus-visible:outline-2 focus-visible:outline-teal-600 dark:border-stone-700 dark:hover:bg-stone-800"
    >
      Try again
    </button>
  );
}
