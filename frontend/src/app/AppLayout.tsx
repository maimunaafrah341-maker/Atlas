import { Outlet, ScrollRestoration } from "react-router";
import { AnnouncementBar } from "../components/layout/AnnouncementBar.tsx";
import { SiteFooter } from "../components/layout/SiteFooter.tsx";
import { SiteHeader } from "../components/layout/SiteHeader.tsx";

export function AppLayout() {
  return (
    <div className="flex min-h-screen flex-col">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:bg-paper-raised focus:px-4 focus:py-2"
      >
        Skip to content
      </a>
      <AnnouncementBar />
      <SiteHeader />
      <main id="main" tabIndex={-1} className="flex-1 focus:outline-none">
        <Outlet />
      </main>
      <SiteFooter />
      <ScrollRestoration />
    </div>
  );
}
