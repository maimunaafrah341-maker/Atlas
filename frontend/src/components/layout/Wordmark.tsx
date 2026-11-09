/** The Atlas mark and wordmark (Concept C). `tone` matches the surface it sits on. */
export function AtlasMark({ className = "" }: { className?: string }) {
  return (
    <svg viewBox="0 0 40 32" aria-hidden="true" className={className} fill="none" stroke="currentColor">
      <path d="M2 3l18 26L38 3M9.5 17h21" strokeWidth="1.5" strokeLinejoin="round" />
    </svg>
  );
}

/**
 * `compact` (the header) uses a slightly smaller mark below sm and drops the "Demo Outfitters"
 * line below 360px, where the header row would otherwise overflow; the link's accessible name
 * still carries the full brand.
 */
export function Wordmark({ tone = "light", compact = false }: { tone?: "light" | "dark"; compact?: boolean }) {
  const colour = tone === "dark" ? "text-on-dark" : "text-ink";
  return (
    <span className={`flex items-center ${compact ? "gap-2 sm:gap-3" : "gap-3"} ${colour}`}>
      <AtlasMark className={`shrink-0 ${compact ? "h-6 w-8 sm:h-7 sm:w-9" : "h-7 w-9"}`} />
      <span className="flex flex-col leading-none">
        <span className="font-display text-[1.75rem] leading-none tracking-tight">Atlas</span>
        <span className={`label-mono mt-1 text-[0.625rem] ${compact ? "max-[359px]:hidden" : ""}`}>Demo Outfitters</span>
      </span>
    </span>
  );
}
