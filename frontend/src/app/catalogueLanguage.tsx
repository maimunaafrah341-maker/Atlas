import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";
import { DEFAULT_LANGUAGE, isLanguage, type Language } from "../lib/catalogue.ts";

/**
 * The language catalogue content (product names and descriptions) is requested in.
 * It does not translate the interface itself; that arrives with the UI catalogues in M7.
 * The choice is remembered per browser when storage is available.
 */

const STORAGE_KEY = "atlas.catalogueLanguage";

interface CatalogueLanguageValue {
  language: Language;
  setLanguage: (language: Language) => void;
}

const CatalogueLanguageContext = createContext<CatalogueLanguageValue | null>(null);

function readStored(): Language {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return isLanguage(stored) ? stored : DEFAULT_LANGUAGE;
  } catch {
    return DEFAULT_LANGUAGE; // storage blocked (private mode, policy): fall back silently
  }
}

export function CatalogueLanguageProvider({ children, initial }: { children: ReactNode; initial?: Language }) {
  const [language, setState] = useState<Language>(() => initial ?? readStored());

  const setLanguage = useCallback((next: Language) => {
    setState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Not remembered, but still applied for this visit.
    }
  }, []);

  const value = useMemo(() => ({ language, setLanguage }), [language, setLanguage]);
  return <CatalogueLanguageContext value={value}>{children}</CatalogueLanguageContext>;
}

export function useCatalogueLanguage(): CatalogueLanguageValue {
  const value = useContext(CatalogueLanguageContext);
  if (!value) throw new Error("useCatalogueLanguage must be used inside CatalogueLanguageProvider");
  return value;
}
