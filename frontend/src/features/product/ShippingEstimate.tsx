import { useId, useState } from "react";
import { getShippingQuote } from "../../lib/api.ts";
import { countries, countryName } from "../../lib/countries.ts";
import { formatMoney } from "../../lib/format.ts";
import { useApiResource } from "../../lib/useApiResource.ts";

/**
 * A shipping estimate for a chosen destination, from GET /api/shipping/quote. Nothing is
 * assumed: no default country, no delivery times, no free shipping. Unsupported destinations
 * are said plainly, as the API reports them.
 */
export function ShippingEstimate() {
  const [country, setCountry] = useState("");
  const id = useId();

  return (
    <section aria-labelledby={`${id}-heading`} className="border-t border-rule pt-6">
      <h2 id={`${id}-heading`} className="label-mono text-ink-muted">
        Shipping estimate
      </h2>
      <label htmlFor={`${id}-country`} className="mt-4 block text-sm">
        Destination country
      </label>
      <select
        id={`${id}-country`}
        value={country}
        onChange={(event) => setCountry(event.target.value)}
        className="mt-2 w-full max-w-sm border-b border-ink bg-transparent py-2 text-sm focus:outline-none focus-visible:outline-2"
      >
        <option value="">Choose a country</option>
        {countries().map(({ code, name }) => (
          <option key={code} value={code}>
            {name}
          </option>
        ))}
      </select>
      <div className="mt-4 min-h-6 text-sm" aria-live="polite">
        {country && <Quote key={country} country={country} />}
      </div>
      <p className="mt-3 text-xs text-ink-subtle">
        Demo rates from this fictional shop’s shipping table. Delivery times aren’t available.
      </p>
    </section>
  );
}

function Quote({ country }: { country: string }) {
  const [quote, retry] = useApiResource(country, (signal) => getShippingQuote(country, signal));
  const name = countryName(country);

  if (quote.kind === "loading") return <p className="text-ink-muted">Checking shipping to {name}…</p>;
  if (quote.kind === "error") {
    const code = quote.error?.code;
    if (code === "unsupported_destination" || code === "invalid_country_code") {
      return <p>This demo shop doesn’t ship to {name}.</p>;
    }
    return (
      <p className="flex flex-wrap items-center gap-3 text-alert">
        Shipping couldn’t be checked. {quote.message}
        <button type="button" onClick={retry} className="border border-ink px-3 py-1 text-ink hover:bg-ink hover:text-paper">
          Try again
        </button>
      </p>
    );
  }
  return (
    <p>
      Shipping to {name}: <strong className="font-semibold">{formatMoney(quote.data.price)}</strong>{" "}
      <span className="text-ink-muted">({quote.data.zone_name} rate)</span>
    </p>
  );
}
