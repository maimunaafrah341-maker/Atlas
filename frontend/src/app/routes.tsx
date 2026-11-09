import type { RouteObject } from "react-router";
import { BagPage } from "../pages/BagPage.tsx";
import { HomePage } from "../pages/HomePage.tsx";
import { NotFoundPage } from "../pages/NotFoundPage.tsx";
import { ProductPage } from "../pages/ProductPage.tsx";
import { RouteErrorPage } from "../pages/RouteErrorPage.tsx";
import { ShopPage } from "../pages/ShopPage.tsx";
import { StatusPage } from "../pages/StatusPage.tsx";
import { AppLayout } from "./AppLayout.tsx";

/** All client routes. The backend serves index.html for any non-/api path (SPA fallback). */
export const routes: RouteObject[] = [
  {
    element: <AppLayout />,
    errorElement: <RouteErrorPage />,
    children: [
      { index: true, element: <HomePage /> },
      { path: "shop", element: <ShopPage /> },
      { path: "products/:slug", element: <ProductPage /> },
      { path: "bag", element: <BagPage /> },
      { path: "status", element: <StatusPage /> },
      { path: "*", element: <NotFoundPage /> },
    ],
  },
];
