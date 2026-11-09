import type { Product } from "../../lib/api.ts";
import { LANGUAGE_NAMES } from "../../lib/catalogue.ts";

/**
 * Says when product text isn't a reviewed text in the chosen language: either a machine
 * translation, or an English fallback because no translation exists. Renders nothing otherwise.
 */
export function TranslationNote({ product, className = "" }: { product: Pick<Product, "fallback" | "machine_translated" | "lang">; className?: string }) {
  let text: string | null = null;
  if (product.fallback) text = `Shown in ${LANGUAGE_NAMES[product.lang]} · no translation yet`;
  else if (product.machine_translated) text = "Machine-translated · not reviewed";
  if (!text) return null;
  return <p className={`label-mono text-[0.625rem] text-ink-subtle ${className}`}>{text}</p>;
}
