/** Typed client for the Atlas API. Same origin in every environment; no secrets here. */

export interface DatabaseHealth {
  status: "ok" | "error";
  dialect: string;
  latency_ms: number | null;
  migration_revision: string | null;
  migration_head: string | null;
  schema_up_to_date: boolean;
}

export interface HealthReport {
  status: "ok" | "degraded";
  version: string;
  environment: string;
  database: DatabaseHealth;
}

export class ApiError extends Error {
  readonly status: number | null;

  constructor(message: string, status: number | null) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

// A sleeping Render free instance can take about a minute to wake, so allow well beyond that.
const DEFAULT_TIMEOUT_MS = 90_000;

function isHealthReport(value: unknown): value is HealthReport {
  if (typeof value !== "object" || value === null) return false;
  const report = value as Record<string, unknown>;
  return (
    (report.status === "ok" || report.status === "degraded") &&
    typeof report.version === "string" &&
    typeof report.database === "object" &&
    report.database !== null
  );
}

/**
 * Readiness report. A 503 still carries a valid report (status "degraded"), so it is
 * returned rather than thrown; network failures and malformed responses throw ApiError.
 */
export async function getHealth(signal?: AbortSignal, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<HealthReport> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const combined = signal ? AbortSignal.any([signal, timeout]) : timeout;
  let response: Response;
  try {
    response = await fetch("/api/health", { signal: combined, headers: { Accept: "application/json" } });
  } catch (error) {
    if (signal?.aborted) throw error;
    throw new ApiError(timeout.aborted ? "The server took too long to respond." : "Could not reach the server.", null);
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // Fall through to the shape check below.
  }
  if ((response.ok || response.status === 503) && isHealthReport(body)) return body;
  throw new ApiError(`Unexpected response from the server (HTTP ${response.status}).`, response.status);
}
