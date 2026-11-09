import { createBrowserRouter, RouterProvider } from "react-router";
import { CatalogueLanguageProvider } from "./app/catalogueLanguage.tsx";
import { routes } from "./app/routes.tsx";

const router = createBrowserRouter(routes);

export function App() {
  return (
    <CatalogueLanguageProvider>
      <RouterProvider router={router} />
    </CatalogueLanguageProvider>
  );
}
