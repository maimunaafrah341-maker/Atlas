import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { formatCountdown, useCountdown } from "./useCountdown.ts";

describe("formatCountdown", () => {
  it.each([
    [15 * 60 * 1000, "15:00"],
    [65_000, "1:05"],
    [1, "0:01"], // rounds up: never 0:00 while time remains
    [0, "0:00"],
  ])("shows %i ms as %s", (ms, text) => {
    expect(formatCountdown(ms)).toBe(text);
  });
});

describe("useCountdown", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-11-09T14:31:00Z"));
  });
  afterEach(() => vi.useRealTimers());

  it("counts down each second and reports expiry exactly once", () => {
    const onExpire = vi.fn();
    const { result } = renderHook(() => useCountdown("2026-11-09T14:31:03Z", onExpire));
    expect(result.current).toBe(3000);

    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toBe(2000);
    act(() => vi.advanceTimersByTime(2000));
    expect(result.current).toBe(0);
    expect(onExpire).toHaveBeenCalledTimes(1);

    act(() => vi.advanceTimersByTime(5000));
    expect(result.current).toBe(0);
    expect(onExpire).toHaveBeenCalledTimes(1);
  });

  it("is idle without a deadline", () => {
    const onExpire = vi.fn();
    const { result } = renderHook(() => useCountdown(null, onExpire));
    act(() => vi.advanceTimersByTime(5000));
    expect(result.current).toBeNull();
    expect(onExpire).not.toHaveBeenCalled();
  });

  it("restarts for a new deadline", () => {
    const onExpire = vi.fn();
    const { result, rerender } = renderHook(({ at }) => useCountdown(at, onExpire), {
      initialProps: { at: "2026-11-09T14:31:01Z" as string | null },
    });
    act(() => vi.advanceTimersByTime(1000));
    expect(onExpire).toHaveBeenCalledTimes(1);
    rerender({ at: "2026-11-09T14:46:01Z" });
    act(() => vi.advanceTimersByTime(1000));
    expect(result.current).toBe(15 * 60 * 1000 - 1000);
    expect(onExpire).toHaveBeenCalledTimes(1);
  });
});
