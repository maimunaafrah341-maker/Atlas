/**
 * Destination countries for the shipping estimate: every officially assigned ISO 3166-1 alpha-2
 * code (249), including territories, since the standard lists them. This is a published
 * standard, not catalogue or shipping data: which of these the shop actually ships to is
 * answered by the shipping API, per country.
 *
 * Names come from the browser's Intl.DisplayNames. Browsers' built-in data differs (Edge, for
 * example, has no English name for EH), so a code without a name is shown as the code itself
 * and is never dropped.
 */

import { DISPLAY_LOCALE } from "./format.ts";

// ISO 3166-1 alpha-2, officially assigned codes (249). Cross-checked against Node 24 / Chrome 156
// ICU data and Windows' region data when this list was written.
export const ISO_COUNTRY_CODES = [
  "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT", "AU", "AW", "AX", "AZ",
  "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI", "BJ", "BL", "BM", "BN", "BO", "BQ", "BR", "BS",
  "BT", "BV", "BW", "BY", "BZ", "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN",
  "CO", "CR", "CU", "CV", "CW", "CX", "CY", "CZ", "DE", "DJ", "DK", "DM", "DO", "DZ", "EC", "EE",
  "EG", "EH", "ER", "ES", "ET", "FI", "FJ", "FK", "FM", "FO", "FR", "GA", "GB", "GD", "GE", "GF",
  "GG", "GH", "GI", "GL", "GM", "GN", "GP", "GQ", "GR", "GS", "GT", "GU", "GW", "GY", "HK", "HM",
  "HN", "HR", "HT", "HU", "ID", "IE", "IL", "IM", "IN", "IO", "IQ", "IR", "IS", "IT", "JE", "JM",
  "JO", "JP", "KE", "KG", "KH", "KI", "KM", "KN", "KP", "KR", "KW", "KY", "KZ", "LA", "LB", "LC",
  "LI", "LK", "LR", "LS", "LT", "LU", "LV", "LY", "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK",
  "ML", "MM", "MN", "MO", "MP", "MQ", "MR", "MS", "MT", "MU", "MV", "MW", "MX", "MY", "MZ", "NA",
  "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP", "NR", "NU", "NZ", "OM", "PA", "PE", "PF", "PG",
  "PH", "PK", "PL", "PM", "PN", "PR", "PS", "PT", "PW", "PY", "QA", "RE", "RO", "RS", "RU", "RW",
  "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM", "SN", "SO", "SR", "SS",
  "ST", "SV", "SX", "SY", "SZ", "TC", "TD", "TF", "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TO",
  "TR", "TT", "TV", "TW", "TZ", "UA", "UG", "UM", "US", "UY", "UZ", "VA", "VC", "VE", "VG", "VI",
  "VN", "VU", "WF", "WS", "YE", "YT", "ZA", "ZM", "ZW",
] as const;

export interface Country {
  code: string;
  name: string;
}

/** Something that names a region code, or returns undefined when it can't (Intl.DisplayNames with fallback "none"). */
export type RegionNamer = { of(code: string): string | undefined };

/** Every ISO country with a display name (the code when unnamed), sorted by that name. */
export function buildCountries(namer: RegionNamer, locale = DISPLAY_LOCALE): Country[] {
  return ISO_COUNTRY_CODES.map((code) => ({ code, name: namer.of(code) || code })).sort((a, b) =>
    a.name.localeCompare(b.name, locale),
  );
}

let cached: Country[] | null = null;

export function countries(): Country[] {
  cached ??= buildCountries(new Intl.DisplayNames([DISPLAY_LOCALE], { type: "region", fallback: "none" }));
  return cached;
}

export function countryName(code: string): string {
  return new Intl.DisplayNames([DISPLAY_LOCALE], { type: "region", fallback: "none" }).of(code) || code;
}
