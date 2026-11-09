/** An honest, persistent statement of what this site is (Concept C's top bar). */
export function AnnouncementBar() {
  return (
    <div className="bg-forest-950 text-on-dark">
      <div className="mx-auto flex max-w-[90rem] flex-wrap items-center justify-between gap-x-6 gap-y-1 px-4 py-2 sm:px-8">
        <p className="label-mono">Fictional brand demonstration — browsing only, nothing can be bought yet</p>
        <p className="label-mono hidden text-on-dark-muted md:block">Prices in USD · Live demo catalogue</p>
      </div>
    </div>
  );
}
