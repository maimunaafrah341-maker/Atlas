import type { ReactNode } from "react";

/** A mono eyebrow label, a serif display heading and optional supporting text (Concept C). */
export function PageIntro({
  eyebrow,
  title,
  children,
  as: Heading = "h1",
}: {
  eyebrow: string;
  title: ReactNode;
  children?: ReactNode;
  as?: "h1" | "h2";
}) {
  return (
    <div>
      <p className="label-mono text-ink-muted">{eyebrow}</p>
      <Heading className="mt-4 font-display text-5xl leading-[1.02] tracking-tight sm:text-6xl">{title}</Heading>
      {children && <div className="mt-5 max-w-xl text-base leading-relaxed text-ink-muted">{children}</div>}
    </div>
  );
}
