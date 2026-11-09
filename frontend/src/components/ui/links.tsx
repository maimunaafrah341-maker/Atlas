import type { ReactNode } from "react";
import { Link } from "react-router";

/** Concept C's underlined text link with a trailing arrow. */
export function ArrowLink({ to, children, tone = "light" }: { to: string; children: ReactNode; tone?: "light" | "dark" }) {
  const colours = tone === "dark" ? "border-on-dark text-on-dark" : "border-ink text-ink";
  return (
    <Link to={to} className={`group inline-flex items-center gap-6 border-b pb-2 text-sm ${colours}`}>
      <span>{children}</span>
      <span aria-hidden="true" className="transition-transform group-hover:translate-x-1">
        →
      </span>
    </Link>
  );
}

/** The solid, square primary action. */
export function PrimaryLink({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      className="inline-flex items-center gap-4 bg-forest-900 px-6 py-4 text-sm text-on-dark transition-colors hover:bg-forest-950"
    >
      <span>{children}</span>
      <span aria-hidden="true">↗</span>
    </Link>
  );
}
