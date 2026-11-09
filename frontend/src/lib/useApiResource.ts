import { useCallback, useEffect, useEffectEvent, useState } from "react";
import { ApiError } from "./api.ts";

export type Resource<T> =
  | { kind: "loading" }
  | { kind: "loaded"; data: T }
  | { kind: "error"; error: ApiError | null; message: string };

type Settled<T> = { key: string } & ({ kind: "loaded"; data: T } | { kind: "error"; error: ApiError | null; message: string });

/**
 * Load an API resource for `key` (e.g. the query and language), aborting the previous request
 * whenever the key changes or the component unmounts. Results are tagged with the key they
 * belong to, so a stale response can never be shown for a newer request, and "loading" is
 * derived rather than set from inside the effect.
 */
export function useApiResource<T>(key: string, load: (signal: AbortSignal) => Promise<T>): [Resource<T>, () => void] {
  const [attempt, setAttempt] = useState(0);
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const requestKey = `${attempt}|${key}`;
  const run = useEffectEvent((signal: AbortSignal) => load(signal));

  useEffect(() => {
    const controller = new AbortController();
    run(controller.signal)
      .then((data) => setSettled({ key: requestKey, kind: "loaded", data }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const apiError = error instanceof ApiError ? error : null;
        setSettled({ key: requestKey, kind: "error", error: apiError, message: apiError?.message ?? "Something went wrong." });
      });
    return () => controller.abort();
  }, [requestKey]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  if (!settled || settled.key !== requestKey) return [{ kind: "loading" }, retry];
  const resource: Resource<T> =
    settled.kind === "loaded"
      ? { kind: "loaded", data: settled.data }
      : { kind: "error", error: settled.error, message: settled.message };
  return [resource, retry];
}
