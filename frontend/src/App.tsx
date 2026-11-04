import { HealthStatus } from "./features/health/HealthStatus.tsx";

export function App() {
  return (
    <div className="mx-auto flex min-h-screen max-w-3xl flex-col gap-8 px-4 py-10">
      <header>
        <p className="text-sm font-medium tracking-wide text-teal-700 uppercase dark:text-teal-400">Project Atlas</p>
        <h1 className="mt-1 text-3xl font-semibold">Multilingual commerce copilot</h1>
        <p className="mt-2 text-stone-600 dark:text-stone-400">
          Foundation build (M0). The shopping assistant, checkout and merchant dashboard are not implemented yet.
        </p>
      </header>
      <main>
        <HealthStatus />
      </main>
    </div>
  );
}
