import { useEffect, useId, useRef, useState, type FormEvent } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { useCatalogueLanguage } from "../../app/catalogueLanguage.tsx";
import { CATEGORIES, CATEGORY_LABELS, LANGUAGES, LANGUAGE_NAMES, isLanguage } from "../../lib/catalogue.ts";
import { Wordmark } from "./Wordmark.tsx";

const NAV_LINKS = [
  { to: "/shop", label: "Shop all" },
  ...CATEGORIES.map((category) => ({ to: `/shop?category=${category}`, label: CATEGORY_LABELS[category] })),
];

export function SiteHeader() {
  // Each panel remembers the location it was opened at, so any navigation closes it.
  const location = useLocation();
  const [menuOpenAt, setMenuOpenAt] = useState<string | null>(null);
  const [searchOpenAt, setSearchOpenAt] = useState<string | null>(null);
  const menuOpen = menuOpenAt === location.key;
  const searchOpen = searchOpenAt === location.key;
  const setMenuOpen = (open: boolean) => setMenuOpenAt(open ? location.key : null);
  const setSearchOpen = (open: boolean) => setSearchOpenAt(open ? location.key : null);
  const menuId = useId();

  return (
    <header className="border-b border-rule bg-paper">
      <div className="mx-auto flex max-w-[90rem] items-center justify-between gap-3 px-4 py-3 sm:gap-4 sm:px-8 sm:py-4 xl:py-5">
        <Link to="/" aria-label="Atlas Demo Outfitters, home" className="shrink-0">
          <Wordmark compact />
        </Link>

        <nav aria-label="Main" className="hidden xl:block">
          <ul className="flex items-center gap-7 text-sm">
            {NAV_LINKS.map((link) => (
              <li key={link.to}>
                <NavItem to={link.to} label={link.label} />
              </li>
            ))}
          </ul>
        </nav>

        {/* Below sm, Search and Menu are 40px icon buttons (their labels stay for screen readers). */}
        <div className="flex items-center gap-1 sm:gap-5">
          <button
            type="button"
            onClick={() => setSearchOpen(!searchOpen)}
            aria-expanded={searchOpen}
            className="flex size-10 items-center justify-center gap-2 text-sm hover:underline sm:size-auto"
          >
            <SearchIcon />
            <span className="sr-only sm:not-sr-only">Search</span>
          </button>
          <LanguageSelect className="hidden sm:block" />
          <BagUnavailable />
          <button
            type="button"
            className="flex size-10 items-center justify-center gap-2 text-sm sm:size-auto xl:hidden"
            aria-expanded={menuOpen}
            aria-controls={menuId}
            onClick={() => setMenuOpen(!menuOpen)}
          >
            <MenuIcon open={menuOpen} />
            <span className="sr-only sm:not-sr-only">{menuOpen ? "Close" : "Menu"}</span>
          </button>
        </div>
      </div>

      {searchOpen && <HeaderSearch onClose={() => setSearchOpen(false)} />}
      {menuOpen && <MobileMenu id={menuId} onClose={() => setMenuOpen(false)} />}
    </header>
  );
}

function NavItem({ to, label }: { to: string; label: string }) {
  const location = useLocation();
  // Category links carry a query, so compare path and query (NavLink compares paths only).
  const params = new URLSearchParams(location.search);
  const target = new URL(to, "http://x");
  const categories = params.getAll("category");
  const targetCategory = target.searchParams.get("category");
  const current =
    location.pathname === target.pathname &&
    (targetCategory ? categories.length === 1 && categories[0] === targetCategory : categories.length === 0) &&
    !params.has("q");
  return (
    <Link
      to={to}
      aria-current={current ? "page" : undefined}
      className={`underline-offset-4 hover:underline ${current ? "underline" : ""}`}
    >
      {label}
    </Link>
  );
}

function HeaderSearch({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate();
  const inputRef = useRef<HTMLInputElement>(null);
  const inputId = useId();

  useEffect(() => inputRef.current?.focus(), []);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const q = (new FormData(event.currentTarget).get("q") ?? "").toString().trim();
    navigate(q ? `/shop?q=${encodeURIComponent(q)}` : "/shop");
  }

  return (
    <div className="border-t border-rule bg-paper-raised">
      <form
        role="search"
        onSubmit={submit}
        onKeyDown={(event) => event.key === "Escape" && onClose()}
        className="mx-auto flex max-w-[90rem] items-center gap-4 px-4 py-4 sm:px-8"
      >
        <label htmlFor={inputId} className="label-mono shrink-0 text-ink-muted">
          Search the catalogue
        </label>
        <input
          ref={inputRef}
          id={inputId}
          name="q"
          type="search"
          maxLength={200}
          placeholder="e.g. waterproof jacket"
          className="min-w-0 flex-1 border-b border-ink bg-transparent py-2 text-base placeholder:text-ink-subtle focus:outline-none focus-visible:outline-none"
        />
        <button type="submit" className="bg-forest-900 px-5 py-2.5 text-sm text-on-dark hover:bg-forest-950">
          Search
        </button>
      </form>
    </div>
  );
}

function MobileMenu({ id, onClose }: { id: string; onClose: () => void }) {
  const firstLink = useRef<HTMLAnchorElement>(null);

  useEffect(() => {
    firstLink.current?.focus();
    const onKey = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div id={id} className="border-t border-rule bg-paper xl:hidden">
      <nav aria-label="Main menu" className="mx-auto max-w-[90rem] px-4 py-6 sm:px-8">
        <ul className="flex flex-col">
          {NAV_LINKS.map((link, index) => (
            <li key={link.to} className="border-b border-rule">
              <Link
                ref={index === 0 ? firstLink : undefined}
                to={link.to}
                className="block py-3 font-display text-2xl"
              >
                {link.label}
              </Link>
            </li>
          ))}
        </ul>
        <LanguageSelect className="mt-6 sm:hidden" />
      </nav>
    </div>
  );
}

function LanguageSelect({ className = "" }: { className?: string }) {
  const { language, setLanguage } = useCatalogueLanguage();
  const id = useId();
  return (
    <div className={className}>
      <label htmlFor={id} className="sr-only">
        Catalogue language (product names and descriptions)
      </label>
      <select
        id={id}
        value={language}
        title="Changes product names and descriptions only"
        onChange={(event) => isLanguage(event.target.value) && setLanguage(event.target.value)}
        className="cursor-pointer border-0 bg-transparent py-1 pr-1 text-sm text-ink"
      >
        {LANGUAGES.map((code) => (
          <option key={code} value={code} lang={code}>
            {LANGUAGE_NAMES[code]}
          </option>
        ))}
      </select>
    </div>
  );
}

/** The bag is shown so the layout is complete, but it is plainly unavailable until the cart API ships. */
function BagUnavailable() {
  return (
    <span
      className="flex items-center gap-1.5 px-1 text-sm whitespace-nowrap text-ink-subtle sm:gap-2 sm:px-0"
      aria-disabled="true"
      title="The bag is not available yet"
    >
      <span>Bag</span>
      <span className="label-mono border border-rule px-1 py-0.5 text-[0.625rem] sm:px-1.5">Soon</span>
      <span className="sr-only">(not available yet)</span>
    </span>
  );
}

function SearchIcon() {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.5">
      <circle cx="8.5" cy="8.5" r="6" />
      <path d="M13 13l5 5" />
    </svg>
  );
}

function MenuIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.5">
      {open ? <path d="M4 4l12 12M16 4L4 16" /> : <path d="M2 6h16M2 14h16" />}
    </svg>
  );
}
