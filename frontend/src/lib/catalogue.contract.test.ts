// @vitest-environment node
/// <reference types="node" />
import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { CATEGORIES, COLOURS, LANGUAGES, MAX_PRICE_MINOR, SIZES, SORT_ORDERS, TAGS } from "./catalogue.ts";

// Read directly from the repository (the frontend and backend live in one repo, and CI checks out both).
const backend = (file: string) =>
  readFileSync(new URL(`../../../backend/app/domain/catalog/${file}`, import.meta.url), "utf-8");
const vocabSource = backend("vocab.py");
const serviceSource = backend("service.py");

/** The quoted values of `Name = Literal[...]` in a backend module (the vocabulary by default). */
function literal(name: string, source = vocabSource): string[] {
  const match = new RegExp(`^${name} = Literal\\[([\\s\\S]*?)\\]`, "m").exec(source);
  if (!match?.[1]) throw new Error(`${name} not found`);
  const body = match[1].replace(/#.*$/gm, "");
  return [...body.matchAll(/"([^"]+)"/g)].map((m) => m[1] as string);
}

describe("catalogue contract", () => {
  it("matches the backend's categories", () => {
    expect([...CATEGORIES]).toEqual(literal("Category"));
  });

  it("matches the backend's languages", () => {
    expect([...LANGUAGES]).toEqual(literal("Language"));
  });

  it("matches the backend's sizes, in display order", () => {
    expect([...SIZES]).toEqual(literal("Size"));
  });

  it("matches the backend's colours", () => {
    expect([...COLOURS]).toEqual(literal("Colour"));
  });

  it("matches the backend's tags", () => {
    expect([...TAGS]).toEqual(literal("Tag"));
  });

  it("matches the backend's sort orders", () => {
    expect([...SORT_ORDERS]).toEqual(literal("SortOrder", serviceSource));
  });

  it("matches the backend's price filter limit", () => {
    const match = /^MAX_PRICE_MINOR = ([\d_]+)$/m.exec(serviceSource);
    expect(MAX_PRICE_MINOR).toBe(Number(match?.[1]?.replaceAll("_", "")));
  });
});
