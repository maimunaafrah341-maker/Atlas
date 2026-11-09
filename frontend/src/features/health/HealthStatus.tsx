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
      className="border border-rule bg-paper-raised p-6"
    >
      <h2 id="system-status" className="label-mono text-ink-muted">
        Live check
      </h2>
      <div aria-live="polite" className="mt-3">
        {state.kind === "loading" && (
          <div className="text-ink-muted">
            <p>Checking the backend…</p>
            <p className="mt-1 text-sm">If the server was asleep, this can take up to a minute.</p>
          </div>
        )}

        {state.kind === "error" && (
          <div role="alert" className="flex flex-wrap items-center gap-3">
            <p className="text-alert">{state.message}</p>
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
          className={`inline-block size-2.5 rounded-full ${healthy ? "bg-ok" : "bg-warn"}`}
        />
        {healthy ? "Backend and database operational" : "Degraded: the database is unavailable or not migrated"}
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-ink-subtle">Version</dt>
        <dd className="font-mono">{report.version}</dd>
        <dt className="text-ink-subtle">Environment</dt>
        <dd>{report.environment}</dd>
        <dt className="text-ink-subtle">Database</dt>
        <dd>
          {database.dialect} · {database.status}
          {database.latency_ms !== null && ` · ${database.latency_ms} ms`}
        </dd>
        <dt className="text-ink-subtle">Schema</dt>
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
      className="border border-ink px-4 py-2 text-sm hover:bg-ink hover:text-paper"
    >
      Try again
    </button>
  );
}
