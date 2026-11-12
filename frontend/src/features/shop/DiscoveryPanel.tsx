import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { CardSkeletons } from "../../components/catalogue/CardSkeletons.tsx";
import { ProductCard } from "../../components/catalogue/ProductCard.tsx";
import { ApiError, MAX_DISCOVERY_LENGTH, discoverProducts, type Discovery } from "../../lib/api.ts";
import { CATEGORY_LABELS, TAG_LABELS, isTag, type Language } from "../../lib/catalogue.ts";
import { colourLabel, sizeLabel } from "../../lib/format.ts";
import type { ShopFilters } from "./shopQuery.ts";

const GRID = "grid grid-cols-2 gap-x-3 gap-y-12 sm:gap-x-4 xl:grid-cols-3";
const EXAMPLES = ["A lightweight navy jacket in medium", "Warm merino socks for winter hiking", "Waterproof trousers, size L"];

type State =
  | { kind: "idle" }
  | { kind: "loading"; query: string }
  | { kind: "error"; message: string }
  | { kind: "done"; query: string; discovery: Discovery };

/**
 * Natural-language discovery: the shopper describes what they need, the backend turns it into
 * catalogue filters (with Gemini, or keyword matching when Gemini is unavailable) and returns
 * real products. The panel only shows and links; it never changes the bag.
 */
export function DiscoveryPanel({ language, onApply }: { language: Language; onApply: (filters: Partial<ShopFilters>) => void }) {
  const [state, setState] = useState<State>({ kind: "idle" });
  const [text, setText] = useState("");
  const [validation, setValidation] = useState<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const id = useId();

  useEffect(() => () => controller.current?.abort(), []);

  async function run(query: string) {
    controller.current?.abort();
    const current = new AbortController();
    controller.current = current;
    setState({ kind: "loading", query });
    try {
      const discovery = await discoverProducts(query, language, current.signal);
      if (!current.signal.aborted) setState({ kind: "done", query, discovery });
    } catch (error) {
      if (current.signal.aborted) return;
      const message = error instanceof ApiError ? error.message : "Something went wrong.";
      setState({ kind: "error", message });
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const query = text.trim();
    if (query.length < 2) {
      setValidation("Describe what you’re looking for in a few words.");
      return;
    }
    setValidation(null);
    void run(query);
  }

  function close() {
    controller.current?.abort();
    setState({ kind: "idle" });
  }

  return (
    <section aria-labelledby={`${id}-heading`} className="mt-12 border border-rule bg-paper-raised p-5 sm:p-8">
      <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
        <h2 id={`${id}-heading`} className="label-mono text-ink-muted">
          Describe what you need
        </h2>
        <p className="label-mono text-[0.625rem] text-ink-subtle">AI by Google Gemini · Groq fallback</p>
      </div>
      <form onSubmit={submit} noValidate className="mt-4">
        <label htmlFor={id} className="sr-only">
          Describe what you’re looking for
        </label>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
          <input
            id={id}
            type="text"
            value={text}
            onChange={(event) => setText(event.target.value)}
            maxLength={MAX_DISCOVERY_LENGTH}
            placeholder="e.g. I’m looking for a lightweight navy jacket in medium"
            aria-invalid={validation !== null}
            aria-describedby={validation ? `${id}-error` : undefined}
            className="min-h-12 w-full border-b border-ink bg-transparent px-1 text-lg placeholder:text-ink-subtle focus:outline-none sm:flex-1"
          />
          <button
            type="submit"
            disabled={state.kind === "loading"}
            className="min-h-12 shrink-0 bg-forest-900 px-6 text-sm text-on-dark hover:bg-forest-950 disabled:opacity-60"
          >
            {state.kind === "loading" ? "Finding…" : "Find products"}
          </button>
        </div>
        {validation && (
          <p id={`${id}-error`} role="alert" className="mt-2 text-sm text-alert">
            {validation}
          </p>
        )}
      </form>
      {state.kind === "idle" && (
        <ul className="mt-4 flex flex-wrap gap-2" aria-label="Examples">
          {EXAMPLES.map((example) => (
            <li key={example}>
              <button
                type="button"
                onClick={() => {
                  setText(example);
                  setValidation(null);
                  void run(example);
                }}
                className="border border-rule px-3 py-1.5 text-sm text-ink-muted hover:border-ink hover:text-ink"
              >
                {example}
              </button>
            </li>
          ))}
        </ul>
      )}
      <div aria-live="polite">
        {state.kind === "loading" && (
          <div className="mt-8">
            <p role="status" className="label-mono text-ink-muted">
              Matching “{state.query}” to the catalogue…
            </p>
            <CardSkeletons count={3} className={`mt-6 ${GRID}`} />
          </div>
        )}
        {state.kind === "error" && (
          <div role="alert" className="mt-8 flex flex-wrap items-center gap-4">
            <p className="text-alert">The products couldn’t be matched. {state.message}</p>
            <button type="button" onClick={() => void run(text.trim())} className="border border-ink px-4 py-2 text-sm hover:bg-ink hover:text-paper">
              Try again
            </button>
          </div>
        )}
        {state.kind === "done" && <DiscoveryResults discovery={state.discovery} onApply={onApply} onClose={close} />}
      </div>
    </section>
  );
}

function sourceNote(discovery: Discovery): string {
  if (discovery.source === "gemini") return `Understood by Google Gemini (${discovery.model ?? "Gemini"}), checked against the catalogue.`;
  if (discovery.source === "groq") return `Understood by Groq (${discovery.model ?? "Groq"}) because Gemini was unavailable, checked against the catalogue.`;
  if (discovery.fallback_reason === "not_configured") return "AI isn’t configured on this server, so your words were matched to the catalogue directly.";
  return "The AI models couldn’t answer just now, so your words were matched to the catalogue directly.";
}

function DiscoveryResults({
  discovery,
  onApply,
  onClose,
}: {
  discovery: Discovery;
  onApply: (filters: Partial<ShopFilters>) => void;
  onClose: () => void;
}) {
  const { intent, results, relaxed } = discovery;
  const chips = [
    ...intent.categories.map((value) => CATEGORY_LABELS[value]),
    ...intent.sizes.map((value) => `Size ${sizeLabel(value)}`),
    ...intent.colours.map(colourLabel),
    ...intent.tags.map((value) => (isTag(value) ? TAG_LABELS[value] : value)),
    ...intent.keywords.map((value) => `“${value}”`),
  ];
  const variantScoped = intent.sizes.length > 0 || intent.colours.length > 0;
  const filters: Partial<ShopFilters> = {
    categories: intent.categories,
    sizes: intent.sizes,
    colours: intent.colours,
    q: relaxed.includes("keywords") ? "" : intent.keywords.join(" "),
  };
  return (
    <div className="mt-8">
      <p className={`text-sm ${discovery.source !== "keywords" ? "text-ink" : "text-ink-muted"}`}>
        <span className={`mr-2 inline-block h-2 w-2 rounded-full align-middle ${discovery.source !== "keywords" ? "bg-ok" : "bg-warn"}`} aria-hidden />
        {sourceNote(discovery)}
      </p>
      <div className="mt-4 flex flex-wrap items-center gap-2">
        <span className="label-mono text-ink-muted">Looking for</span>
        {chips.length > 0 ? (
          <ul className="flex flex-wrap gap-2" aria-label="Interpreted request">
            {chips.map((chip) => (
              <li key={chip} className="border border-ink px-2.5 py-1 text-sm">
                {chip}
              </li>
            ))}
          </ul>
        ) : (
          <span className="text-sm text-ink-muted">anything in the catalogue</span>
        )}
      </div>
      {relaxed.length > 0 && (
        <p className="mt-3 text-sm text-ink-muted">
          Nothing matched every detail, so {relaxed.map((field) => (field === "keywords" ? "the extra words" : "the features")).join(" and ")} were left out.
        </p>
      )}
      <div className="mt-6 flex flex-wrap gap-3">
        <button type="button" onClick={() => onApply(filters)} className="border border-ink px-4 py-2 text-sm hover:bg-ink hover:text-paper">
          Apply as shop filters
        </button>
        <button type="button" onClick={onClose} className="px-4 py-2 text-sm underline underline-offset-4 hover:text-ink-muted">
          Close
        </button>
      </div>
      {results.total === 0 ? (
        <p className="mt-8 bg-paper p-6 text-ink-muted">No products match that yet. Try fewer details, or browse the catalogue below.</p>
      ) : (
        <>
          <p className="label-mono mt-8 text-ink-muted">
            {results.total} {results.total === 1 ? "match" : "matches"}
            {results.total > results.items.length ? ` · showing ${results.items.length}` : ""}
          </p>
          <ul className={`mt-6 ${GRID}`} aria-label="Matching products">
            {results.items.map((product, index) => (
              <li key={product.slug}>
                <ProductCard product={product} index={index} matchingOnly={variantScoped} />
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
