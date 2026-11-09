import { afterEach, describe, expect, it, vi } from "vitest";
import { ISO_COUNTRY_CODES, buildCountries, countries, countryName } from "./countries.ts";

afterEach(() => vi.restoreAllMocks());

describe("ISO 3166-1 country list", () => {
  it("has exactly the 249 assigned codes, each unique and two capital letters", () => {
    expect(ISO_COUNTRY_CODES).toHaveLength(249);
    expect(new Set(ISO_COUNTRY_CODES).size).toBe(249);
    for (const code of ISO_COUNTRY_CODES) expect(code).toMatch(/^[A-Z]{2}$/);
  });

  it("includes known codes, territories among them", () => {
    for (const code of ["GB", "IN", "US", "EH", "DE", "BR", "JP", "AQ", "BV", "GS", "HM", "TF", "PR", "HK", "AX"]) {
      expect(ISO_COUNTRY_CODES).toContain(code);
    }
  });

  it("excludes codes that are not assigned ISO countries", () => {
    for (const code of ["EU", "UN", "UK", "XK", "ZZ", "CS", "DD", "SU", "AN"]) {
      expect(ISO_COUNTRY_CODES).not.toContain(code as never);
    }
  });
});

describe("countries()", () => {
  it("names every code in English and sorts by name", () => {
    const list = countries();
    expect(list).toHaveLength(249);
    expect(list.find((country) => country.code === "GB")?.name).toBe("United Kingdom");
    const names = list.map((country) => country.name);
    expect(names).toEqual([...names].sort((a, b) => a.localeCompare(b, "en-US")));
  });

  it("shows the code when the browser has no name for it, never dropping the country", () => {
    // As Edge 157 does for EH (Western Sahara).
    const list = buildCountries({ of: (code) => (code === "EH" ? undefined : `Name ${code}`) });
    expect(list).toHaveLength(249);
    expect(list).toContainEqual({ code: "EH", name: "EH" });
    expect(list).toContainEqual({ code: "IN", name: "Name IN" });
  });

  it("falls back to the code in countryName too", () => {
    expect(countryName("IN")).toBe("India");
    vi.spyOn(Intl.DisplayNames.prototype, "of").mockReturnValue(undefined);
    expect(countryName("EH")).toBe("EH");
  });
});
