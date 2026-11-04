import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { HealthReport } from "../../lib/api.ts";
import { HealthStatus } from "./HealthStatus.tsx";

const healthy: HealthReport = {
  status: "ok",
  version: "dev",
  environment: "development",
  database: {
    status: "ok",
    dialect: "sqlite",
    latency_ms: 1.2,
    migration_revision: "0001_baseline",
    migration_head: "0001_baseline",
    schema_up_to_date: true,
  },
};

const degraded: HealthReport = {
  ...healthy,
  status: "degraded",
  database: { ...healthy.database, status: "error", latency_ms: null, migration_revision: null, schema_up_to_date: false },
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("HealthStatus", () => {
  it("shows a loading state, then a healthy report", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(healthy));
    render(<HealthStatus />);
    expect(screen.getByText("Checking the backend…")).toBeInTheDocument();
    expect(screen.getByText("If the server was asleep, this can take up to a minute.")).toBeInTheDocument();
    expect(await screen.findByText("Backend and database operational")).toBeInTheDocument();
    expect(screen.getByText("up to date (0001_baseline)")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Try again" })).not.toBeInTheDocument();
  });

  it("shows a degraded report from a 503 with a retry", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(jsonResponse(degraded, 503));
    render(<HealthStatus />);
    expect(await screen.findByText(/Degraded/)).toBeInTheDocument();
    expect(screen.getByText("not at latest migration")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("shows an error when the server cannot be reached, and recovers on retry", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockRejectedValueOnce(new TypeError("Failed to fetch"))
      .mockResolvedValueOnce(jsonResponse(healthy));
    render(<HealthStatus />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not reach the server.");

    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Backend and database operational")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("treats a malformed response as an error", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("<html>proxy error</html>", { status: 502 }));
    render(<HealthStatus />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Unexpected response from the server (HTTP 502).");
  });
});
