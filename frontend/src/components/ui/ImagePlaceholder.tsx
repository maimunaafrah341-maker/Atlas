import { AtlasMark } from "../layout/Wordmark.tsx";

/**
 * A neutral, honest stand-in for photography (Concept C). Atlas has no product imagery yet
 * (image sourcing is an open decision, D-11), so nothing here pretends to be a photo.
 * Decorative: hidden from assistive technology.
 */
export function ImagePlaceholder({
  label,
  ratio,
  numeral,
  tone = "bg-tint-stone",
  dark = false,
  className = "",
}: {
  label: string;
  ratio: string;
  numeral?: string;
  tone?: string;
  dark?: boolean;
  className?: string;
}) {
  const text = dark ? "text-on-dark" : "text-ink-muted";
  return (
    <div aria-hidden="true" className={`relative flex flex-col justify-between overflow-hidden p-4 ${tone} ${className}`}>
      <div className={`label-mono flex justify-between ${text}`}>
        <span>{label}</span>
        <span>{ratio}</span>
      </div>
      <div className={`flex flex-col items-center gap-2 ${text}`}>
        {numeral ? (
          <span className="font-display text-7xl leading-none opacity-40 sm:text-8xl">{numeral}</span>
        ) : (
          <AtlasMark className="h-8 w-10" />
        )}
        <span className="label-mono text-[0.625rem]">Photography placeholder</span>
      </div>
      <span className={`label-mono text-[0.625rem] ${text}`}>Image pending</span>
    </div>
  );
}
