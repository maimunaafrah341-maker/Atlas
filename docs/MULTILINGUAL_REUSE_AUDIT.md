# Multilingual Reuse Audit — Second Look, Raahat, Athena → Atlas

Status: Read-only audit, for approval · Date: 2026-11-04

## 0. Scope and method

| Repo | Commit audited | Stack | Licence |
|---|---|---|---|
| `maimunaafrah341-maker/second-look-ai` | `7968f16` | FastAPI + React 19/Vite/TS | MIT |
| `maimunaafrah341-maker/raahat` | `85a977b` | Flask + vanilla JS | none in repo (same owner) |
| `maimunaafrah341-maker/Athena` | `de64737` | FastAPI + vanilla JS | none in repo (same owner) |

Method: each repo was shallow-cloned into an isolated scratch directory and the source was read. No code from these repos was executed, installed or modified, and nothing was pushed. The test counts below come from counting test functions in the source. **The test suites were not run**, so this audit makes no claims about pass rates.

`github.com/maimunaafrah341-maker/Atlas` is currently empty (no commits).

## 1. Headline findings

1. **None of the existing language detection works for Atlas's languages.** All three projects detect language mainly through **Unicode script ranges** (Devanagari, Telugu, Perso-Arabic, Bengali). English, Spanish, French, German and Portuguese all use Latin script, so script counting can't tell any of them apart. The Latin-script fallbacks are a romanised-Hindi keyword list (Second Look) and a sentence-transformer similarity check against hand-written hi/te/en anchors (Athena). Neither has any signal for es/fr/de/pt. **Detection must be rebuilt.**
2. **Hindi, Urdu or Telugu results say nothing about Spanish, French, German or Portuguese quality.** None of the existing evaluation sets contain a single es/fr/de/pt example. Second Look's own eval files say the Indic results "are not evidence of accuracy" even for the Indic languages. Atlas needs its own evaluation set (§9).
3. **The most valuable reusable assets are patterns, not code:**
   - Original text is immutable, and any translation sits beside it, labelled as machine translation (Athena `cases.py`, Raahat UI).
   - A translation counts only if a preservation check passes (Raahat `translation._preserved`). Otherwise the system shows the source text. It never shows an unchecked translation.
   - LLM classification uses closed enums, so the model can't invent categories (Raahat `understanding.py`).
   - A results-language preference is stored with safe `localStorage` handling (Second Look).
   - CI-style translation-table integrity tests (Athena/Raahat `test_i18n.py`).
4. **The existing dictionaries can't be reused by swapping language codes.** Their keys and values describe a different domain (scam warnings, hospitals, helplines). Second Look's `API_TEXT_HINDI` is keyed by exact English sentences from its own API. Atlas needs new message catalogues written for commerce.
5. **Athena's runtime is unsuitable for Render.** Its README says it needs about 1–2 GB of RAM because of `sentence-transformers` (`multilingual-e5-small` loaded at import), `easyocr`, `librosa`, `onnxruntime` and `chromadb`. Raahat's `chromadb` plus Gemini embeddings duplicates what ADR-006 (FTS5) already covers.

## 2. Language detection

| Repo | Implementation | How it works | Fit for Atlas |
|---|---|---|---|
| Second Look | `backend/app/language.py` `estimate_language()` | Counts letters per script with a 75% dominance threshold. For Latin text it checks ≥2 distinct romanised-Hindi cue words making up ≥25% of words. It strips URLs before counting. Returns a tag, a coverage level (`supported`/`partial`/`unsupported`) and a user-facing notice. Deterministic, no network. | Script logic can't separate en/es/fr/de/pt. **The cue-word approach must not be extended** to European languages by adding word lists, because they share too much vocabulary and too many cognates. Worth keeping: the `{detected, coverage, notice, method}` result shape, honest "estimate" wording, URL stripping and the `mixed`/`unknown` outcomes. |
| Raahat | `backend/understanding.py` `script_language()` + Gemini `language` enum field | Native script is treated as certain. Otherwise it trusts the language the LLM reports in its structured output. | The precedence rule (deterministic signal first, model second) is sound, but for Latin-script languages the deterministic signal must come from an actual language-ID model, not script. |
| Athena | `understanding.py` `detect_language()` + `detect_script()` | Script ranges first, with careful fixes (danda excluded, unsupported Indic scripts sent to `en`). For Latin text: cosine similarity to anchor sentences using `multilingual-e5-small`. | Heavy (a transformer model in the web process) and anchored on domain sentences. The semantic-similarity-as-language-ID idea is unreliable, as Athena's own comments show (Tamil detected as Hindi with 100% confidence). **Do not reuse.** `detect_script` (native vs. romanised reply script) has no equivalent need in Atlas. |

## 3. Translation and provider dependencies

| Repo | Implementation | Providers | Notes |
|---|---|---|---|
| Athena | `translation.py`: `translate_to_english()` (inbound, for counsellors) and `translate_reply()` (outbound) | Via `response_engine.generate_response()`: Groq → Gemini (several models) → OpenRouter fallback chain | Excellent prompt discipline: translate literally, add nothing, keep numbers, IDs and names exactly. It is lazy and cached, never raises, and returns `None` meaning "no translation". However, **outputs aren't validated**: the prompt asks for IDs to be kept, but nothing checks that they were. |
| Raahat | `backend/translation.py` `translate_explanation()` | Gemini only, via `rag._generate_with` (`google-genai` SDK) | Adds the missing piece, `_preserved()`: bracketed citations must match as a multiset and every number in the source must appear in the output. Two attempts, then `None`. The English stays authoritative and the UI shows it alongside. |
| Second Look | none | — | All non-English text is **hand-written static content**. There's no runtime translation. Its optional LLM agent (Gemini → Groq through raw `urllib`, JSON mode, 429 `Retry-After` handling) is English-only. |

Provider coupling: Raahat imports `google.genai.types` directly into translation and understanding. Athena hard-wires a three-vendor chain inside `response_engine`. Neither matches Atlas's provider-agnostic `LLMProvider` protocol (ADR-005). Second Look's `PROVIDERS` tuple of `(name, callable)` comes closest to that idea.

## 4. Language and locale configuration

- **Second Look**: `ResultsLanguage = "en" | "hi"` (a TS union), and `LANGUAGE`/`COVERAGE`/`NOTICES` dicts on the backend. Dates are formatted with a hard-coded `en-IN`.
- **Raahat**: `LANGUAGES = {"en","hi","te","ur"}` with native names and `RTL = {"ur"}`, served to the browser via `/api/i18n`. Dates use `` `${lang}-IN` ``. The browser's language is read by slicing `navigator.languages` to two letters (`app.js:1406`), which would turn `pt-BR` into `pt`.
- **Athena**: `SUPPORTED_LANGUAGES`, plus `LANGUAGE_NAMES` duplicated in `translation.py`, and the UI language in `web/i18n.js` (`localStorage`, `dir=rtl` for `ur`).

None of them separates **language**, **locale/region**, **currency** and **shipping destination**. All of them assume India (`-IN`, rupees, 108/1930 helplines). Language tags are bare two-letter codes. There's no BCP-47 handling, no fallback chain (e.g. `pt-PT` → `pt-BR`), and no plural rules or ICU message formatting.

## 5. User-selectable response language

- **Second Look** (`lib/resultsLanguage.ts`, `components/ResultsLanguageSelect.tsx`): a "results language" separate from the interface language and from the message language. Changing it **never changes the message or the analysis**, only how results are presented. The choice persists in `localStorage` with `try/catch`. Non-English presentation falls back to English **per string** whenever a translation is missing (`localizeApiText`). For composite strings, it translates all of the parts or none of them (`localizeExplanation`).
- **Raahat**: the UI language is sent with every API call (`language: state.lang`). The server returns translated text plus the English original. The hospital desk card is shown in both the chosen language and English.
- **Athena**: the reply language follows the detected language and script of the report. Counsellors see English translations on demand.

For Atlas: the separation in Second Look (presentation language ≠ analysis) is exactly right. However, its mechanism (static lookup keyed by exact English text) doesn't fit a conversational product where the replies are generated.

## 6. Preserving original source content

| Pattern | Where | Atlas verdict |
|---|---|---|
| `original_text NOT NULL`, with `translated_text` in a separate column that is only ever written by `save_translation()` | Athena `cases.py:122-131, 403-426` | **Adapt.** Atlas `messages.original_text` is immutable. Derived fields (`detected_lang`, `retrieval_text`) go in separate columns or rows. |
| Show the user's own words verbatim with `dir="auto" lang=""` | Second Look `Results.tsx:73-74` | **Reuse directly** (the markup idiom) |
| Show machine translation labelled as such, next to the original | Raahat UI, Athena brief | **Adapt** for knowledge answers built from source-language docs |
| Reject the translation and show the source text when preservation fails | Raahat `translation.py` | **Adapt** (generalise to protected tokens, §10.4) |
| Never translate titles, publishers, dates or links (`lang="en"` on those nodes) | Second Look `Results.tsx`, `apiHindi.ts` header | **Reuse** as a rule, extended to SKUs, product IDs, URLs, prices and order references |

## 7. Frontend components and state patterns

| Item | Repo | Classification | Reason |
|---|---|---|---|
| `useResultsLanguage` hook (state + safe `localStorage`) | Second Look | **Adapt** | The pattern is good. It needs widening to a `LanguagePreference` covering the five BCP-47 tags and an "auto" mode, moving into a React context (as `ThemeProvider` does), and coordinating with the server-side conversation language. |
| `ResultsLanguageSelect` | Second Look | **Adapt** | Accessible (`useId`, `<label>`, per-option `lang`). Labels must be localised and the options driven by config. |
| `ThemeProvider` / theme storage | Second Look | **Reuse directly** (if Atlas wants a theme toggle) | Generic and correct. It isn't i18n. |
| `lib/text.ts` `countCharacters` (code points), `highlightEvidence` | Second Look | `countCharacters`: **Reuse directly**. `highlightEvidence`: **Do not reuse** | Code-point counting matters for message length limits. Evidence highlighting is specific to scam detection. |
| `formatDate` (hard-coded `en-IN`) | Second Look | **Rebuild** | It must take the locale as a parameter and use `Intl`. |
| `resultsText.ts`, `apiHindi.ts`, `reportHindi.ts`, `copy.ts` | Second Look | **Do not reuse** | Content for the scam domain. `apiHindi.ts` keys translations by exact English API sentences, which is fragile and only fails safe because the English shows through. Atlas will use message IDs. |
| `web/i18n.js` (`data-i18n` attributes, `applyTranslations`) | Athena | **Do not reuse** | DOM-mutation i18n for vanilla JS. React needs a typed, component-level message catalogue. |
| Raahat `app.js` `t()` / `tIn()` with English fallback, `html.lang`/`dir` sync | Raahat | **Adapt (pattern only)** | Fallback to English and keeping `lang`/`dir` in sync on `<html>` are both right. The code is vanilla JS. |
| Vitest + Testing Library setup, JSON fixtures per language | Second Look | **Adapt** | Same toolchain as Atlas, so the config and test style carry over. The fixtures are domain-specific. |

## 8. Backend abstractions

| Item | Repo | Classification | Reason |
|---|---|---|---|
| `LanguageEstimate` dataclass shape `{detected, coverage, notice, method}` | Second Look | **Adapt** | A good return type. Atlas adds `confidence` and `source` (detector / user override / inherited). |
| Script-counting detector (`estimate_tag`) | Second Look, Athena, Raahat | **Do not reuse** (as the detector) | Can't tell Latin-script languages apart. At most, keep a cheap "non-Latin script → unsupported language" pre-check for ja/zh later. |
| Romanised-Hindi cue list | Second Look | **Do not reuse** | Irrelevant to Atlas's languages, and the technique doesn't generalise. |
| Semantic language ID (`multilingual-e5-small`) | Athena | **Do not reuse** | Heavy, and unreliable by its own account. |
| Translation prompt rules ("translate only, add nothing, keep numbers/IDs") | Athena, Raahat | **Adapt** | Rewrite for commerce: protected tokens and no added claims about products or policies. |
| `_preserved()` post-translation validator | Raahat | **Adapt** | Generalise from citations and numbers to typed placeholders such as `⟦P:sku⟧` and `⟦URL:n⟧`, compared as a multiset. Make it provider-agnostic. |
| "Return `None`, never raise, caller shows the source" contract | Athena, Raahat | **Reuse directly** (as a contract) | Exactly the right behaviour when translation degrades. |
| Closed-enum structured extraction with Pydantic (`Understanding`) | Raahat | **Adapt** | The model can't invent categories, so the same applies to Atlas category, size, colour and `action` enums. The domain fields and the Gemini `response_schema` coupling must be rebuilt for the provider protocol. |
| `summary` "in the SAME language… so they can check it was understood" | Raahat | **Adapt** | Useful for the "here's what I understood" intent echo. |
| Provider fallback loop with 429 `Retry-After` (`call_llm`) | Second Look | **Adapt** | Good resilience logic, but it's built on `urllib` and is English-agent-specific. Port the idea into the Atlas provider adapter. |
| Athena `response_engine` Groq → Gemini → OpenRouter chain | Athena | **Rebuild** | Hard-wired vendors with `print` logging. Atlas uses one configured provider plus an optional fallback behind the protocol. |
| `i18n.t(lang, key, **kwargs)` with English fallback | Raahat | **Adapt** | For the few server-side strings Atlas needs (e.g. error text for Make or email templates). |
| Chroma + Gemini embeddings RAG | Raahat | **Do not reuse** | Superseded by ADR-006 (FTS5). The chunk-with-title-prefix idea is worth keeping for FTS ranking. |

## 9. Existing tests and coverage

Counts are test functions found in the source. None were run.

| Repo | Language-related tests | Other tests | What they cover | What they don't |
|---|---|---|---|---|
| Second Look | `test_language.py` (10), `test_classifier_language.py` (10), `test_guidance_hindi.py` (12), `test_guidance_multilingual.py` (9), `test_warning_signs_hindi.py` (17), `test_warning_signs_multilingual.py` (20), `test_multilingual_eval.py` (7), frontend `resultsLanguage.test.tsx` (16 cases) | ~200 backend + ~41 frontend in total | Script detection on clear examples; Hindi and Indic rule behaviour; the results-language toggle never changes the message or analysis; per-string fallback | No Latin-script language beyond English. No mid-session language switching. Evals were written by the project and not reviewed by native speakers (stated in the files). |
| Raahat | `test_i18n.py` (7) | `test_safety.py` (23) | Every language has every key; no value left untranslated; `{placeholder}` sets match; safety numbers (108) are present in every language | No tests for `translate_explanation`, `_preserved` or `understand()` (all depend on the network) |
| Athena | `test_i18n.py` (13), `test_understanding.py` (17), `test_response_prompt.py` (11) | ~41 more | Table integrity; keys used in markup and JS exist; script detection fixes; prompts carry language and script instructions | Translation output is never validated. Tests load the embedding model, so they're slow and heavy. |

**Directly reusable test ideas** (rewritten for Atlas, not copied): key parity across locales, no untranslated values (with an allow-list for brand terms), placeholder parity, "critical tokens must be present in every locale", and "changing the presentation language never changes the underlying data".

## 10. Hardcoded language-specific patterns that can't be reused

- Unicode ranges for Devanagari, Telugu, Bengali and Perso-Arabic; danda handling; Nastaliq OCR notes.
- `ROMAN_HINDI_CUES`, Hindi/Telugu anchor sentences, the romanised-reply logic (`detect_script` → "Respond ONLY in romanized Hindi").
- `RTL = {"ur"}`. None of Atlas's initial languages are RTL. Keep `dir` handling generic, but don't port RTL-specific CSS.
- `-IN` locale suffixes, ₹ and Indian digit grouping (`₹5,00,000`) in the number regex, 108/1930/14566 helplines, Hyderabad localities and the `_norm_place` transliteration rules.
- `apiHindi.ts` keyed by exact English sentences; `TRANSLATIONS` blocks inside `i18n.js`; Raahat's 1,384-line `STRINGS` dict. All domain content.
- Number preservation via `\d+(?:,\d+)*`. This breaks for European formats (`1.299,00 €`, `1 299,00 €`, `1'299.00`). Atlas avoids the problem structurally, because prices never pass through translation.

## 11. Dependencies unsuitable or too heavy for Render

| Dependency | Repo | Problem | Verdict |
|---|---|---|---|
| `sentence-transformers` + `multilingual-e5-small` (+ torch) | Athena | Hundreds of MB, loaded at import time; the 1–2 GB RAM need is documented | Exclude |
| `easyocr`, `librosa`/`numba`/`llvmlite`, `soundfile`, `onnxruntime` | Athena | OCR and voice are out of Atlas scope and very heavy | Exclude |
| `chromadb` (+ `kubernetes`, `grpcio`, `opentelemetry-*`, …) | Athena, Raahat | Large transitive tree, a persistence directory to rebuild on each deploy, overlaps with FTS5 | Exclude (ADR-006) |
| `scikit-learn`/`scipy`/`numpy` + pickled `model.joblib` | Second Look | A version-pinned pickle for an unrelated classifier | Exclude |
| `google-genai` SDK directly in domain code | Raahat, Athena | Vendor lock-in inside business logic | Only inside one provider adapter, if Gemini is chosen |
| `flask`, `gunicorn`, `psycopg` | Raahat | Different framework and DB | N/A |
| `fastapi`, `pydantic`, `httpx`, `pytest`, `vitest`, Testing Library | Second Look / Athena | Already in Atlas's stack | Fine |

## 12. Classification summary

| Component | Verdict |
|---|---|
| Original-text-immutable + separate translation column (Athena) | **Adapt** |
| `dir="auto"` verbatim display of the user's words (Second Look) | **Reuse directly** |
| "Translation returns None → show source" contract (Athena/Raahat) | **Reuse directly** |
| `_preserved()` translation validator (Raahat) | **Adapt** → protected-token validator |
| Translation prompt rules (Athena/Raahat) | **Adapt** |
| Closed-enum Pydantic extraction (Raahat) | **Adapt** |
| `LanguageEstimate` result shape (Second Look) | **Adapt** |
| Provider fallback + 429 handling (Second Look) | **Adapt** |
| `useResultsLanguage` + `ResultsLanguageSelect` (Second Look) | **Adapt** |
| `ThemeProvider`, `countCharacters` (Second Look) | **Reuse directly** |
| i18n integrity tests (Athena/Raahat) | **Adapt** (rewritten for TS catalogues) |
| Server `t()` with English fallback (Raahat) | **Adapt** |
| `formatDate` with fixed `en-IN` | **Rebuild** |
| Athena `response_engine` provider chain | **Rebuild** |
| Script-based detectors (all three) | **Do not reuse** |
| Romanised-Hindi cue words; Athena semantic language ID | **Do not reuse** |
| All existing translation dictionaries and content files | **Do not reuse** |
| `web/i18n.js` DOM i18n; Raahat `app.js` | **Do not reuse** (code); patterns noted above |
| Chroma/embedding RAG, OCR, voice, sklearn classifier | **Do not reuse** |

## 13. Proposed language architecture for Atlas

> **Superseded in detail by [ARCHITECTURE.md §8](ARCHITECTURE.md#8-language-architecture), ADR-017 and ADR-018** (revision 2). Those documents put explicit user preference first, keep the conversation language sticky, add manual switching, and extend placeholder protection to sizes, quantities, numeric values and monetary amounts. The tests in §14 below still apply.

### 13.1 Concepts (kept separate, never inferred from each other)

| Field | Type | Owner | Example |
|---|---|---|---|
| `ui_locale` | BCP-47 | Browser `navigator.languages`, matched to supported tags, or user choice | `es-ES` |
| `conversation.preferred_lang` | `en\|es\|fr\|de\|pt-BR` or `auto` | User override in the UI | `auto` |
| `message.detected_lang` + `confidence` | per message | Language detector | `fr`, 0.97 |
| `turn.response_lang` | resolved per turn | `LanguagePolicy` | `fr` |
| `currency` | ISO 4217 | Store config | `EUR` |
| `ship_country` | ISO 3166-1 | Explicit user input / PayPal | `FR` |

Normalisation: a detector result of `pt` maps to `pt-BR` (the only Portuguese variant in scope). European Portuguese users are served `pt-BR`, and the docs say so. Unsupported detected languages fall back to `ui_locale`'s language, then `en`, and the reply says so politely in that fallback language.

### 13.2 Pipeline: four separate stages, each with its own module and tests

```
message ─► [1] detect ─► [2] resolve language ─► [3] extract intent ─► [4] deterministic search/cart ─► [5] compose ─► [6] validate/localise ─► response
            i18n/detect   i18n/policy             ai/intent             domain/*                         ai/compose     ai/guards + i18n
```

1. **Detect** (`i18n/detect.py`): a lightweight, deterministic language-ID library restricted to the five candidate languages. The candidate is a small pure-Python identifier (to be chosen and **measured** in M7, ADR-pending; not installed now). It returns `{lang, confidence, method}`. Short or low-information inputs (`< ~20` letters, e.g. "M", "ok", "sí", an SKU, an emoji) return `undetermined` rather than guessing. The raw message is never altered.
2. **Resolve** (`i18n/policy.py`, pure function, the most heavily tested unit):
   `explicit user choice > confident detection of this message > conversation's current language > ui_locale language > en`.
   A switch requires a confident detection (e.g. ≥0.90, tuned on the eval set) **and** at least 2 words. This stops loanwords ("Danke!", "merci") and product names ("Nike Air", "Le Creuset") from flipping the conversation language. Every switch is recorded on the message (`lang_source`).
3. **Extract intent** (`ai/intent.py`): the LLM receives the **original text** plus `response_lang` and returns a `ShoppingIntent` whose fields are **language-neutral canonical values**: closed enums for category, colour and size, numbers for budget, ISO codes for country. Free-text fields (e.g. `search_terms`) are also returned in English for FTS over the canonical catalogue. The original-language query is kept for searching product translations.
4. **Search and cart** (`domain/catalog`, `domain/cart`): purely deterministic, operating on canonical IDs. Language only selects which `product_translations` row to display. **Translation never touches IDs, SKUs, variant IDs, prices, currency, URLs, order or payment data.**
5. **Compose** (`ai/compose.py`): the LLM writes the reply in `response_lang` from a structured payload where every product reference is a placeholder (`⟦P:42⟧`) and **no price, stock count or URL is present**. The frontend replaces placeholders with product cards built from DB data and the user's locale (`Intl.NumberFormat`).
6. **Validate and localise** (`ai/guards.py`): the protected-token validator (generalised from Raahat `_preserved`) checks that the placeholder multiset is a subset of what was supplied, with no unknown IDs. It strips any currency amount or stock figure. An optional language check confirms the reply is in `response_lang`. On failure it retries once, then falls back to a templated reply from the message catalogue in `response_lang`.

Knowledge answers: retrieve in `response_lang` first. If there are no hits, translate the **query** to the merchant's source language and retrieve there. The answer is composed in `response_lang` with citations. A cited chunk from another language is labelled "translated from {source language}", and the original chunk is viewable (the Athena/Raahat labelling pattern).

UI chrome: typed message catalogues `frontend/src/i18n/locales/{en,es,fr,de,pt-BR}.json` with ICU plurals. Library choice is deferred to scaffolding, since it's the only new frontend dependency in this area. PayPal JS SDK `locale` comes from `ui_locale` (`es_ES`, `pt_BR`, …). Server-side strings (Make email templates and fallback replies) use a parallel catalogue with English fallback.

### 13.3 Storage

`messages`: `original_text` (immutable), `detected_lang`, `detect_confidence`, `response_lang`, `lang_source` (`detected|inherited|user|ui_locale|default`), `retrieval_text_en` (derived). `conversations`: `preferred_lang` (nullable = auto), `current_lang`. Assistant messages store the final validated text and the language it was written in.

## 14. Required tests

### 14.1 Language switching during an existing conversation (`tests/unit/test_language_policy.py`, plus integration and Playwright)

| # | Scenario | Expected |
|---|---|---|
| S1 | en conversation, user writes a full Spanish sentence | `response_lang=es`, `lang_source=detected`, conversation `current_lang=es` |
| S2 | es conversation, next message is "M" or "ok" or "👍" | stays `es` (`inherited`) |
| S3 | fr conversation, message "Je veux les Nike Air Zoom" | stays `fr` (brand names don't flip it) |
| S4 | de conversation, message "Danke!" in an en conversation | stays `en` (below the word and confidence threshold) |
| S5 | User selects pt-BR in the UI mid-conversation, then writes English | `response_lang=pt-BR` (`user` beats detection) until reset to auto |
| S6 | User resets to auto after S5 and writes German | switches to `de` |
| S7 | Mixed message "Quiero la chaqueta in size M please" | deterministic outcome per threshold; never crashes; original preserved |
| S8 | Unsupported language (e.g. Italian) | falls back to the `ui_locale` language with a localised "I can reply in …" notice |
| S9 | Cart and pending order exist, the language switches es → de | cart items, prices, currency, order snapshot, `version_hash` and PayPal order **unchanged**. Review screen re-renders in German with identical amounts |
| S10 | Language switch between "Review" and "Approve" | approval still succeeds with the same `version_hash`. Approval isn't invalidated by presentation language |
| S11 | Conversation history after several switches | each stored `original_text` is byte-identical to what was sent. Each assistant message records its own language |
| S12 | Knowledge question in `pt-BR` with only English docs | answer in pt-BR, citations point to English chunks, "translated from English" label present |

### 14.2 Other multilingual tests
- **Protected tokens**: property test that random placeholders survive the validator; injected unknown `⟦P:999⟧` is rejected; injected "€19,99" / "19.99 EUR" / "nur noch 2 auf Lager" are stripped.
- **Catalogue parity**: every locale has every key; ICU placeholders match; no untranslated values except an allow-list (brand terms); PayPal locale mapping exists for all five.
- **Formatting**: `Intl` output for the same minor-unit amount in each locale, checked against snapshots. The amount sent to PayPal is identical regardless of locale.
- **Detector evaluation** (on demand, not CI-blocking): ≥30 short, realistic shopping messages per language, written for this purpose, including tricky cases (es/pt, fr/en cognates, product names). Report accuracy and the `undetermined` rate per language. No Indic results are cited as evidence.
- **Intent parity**: the same request in all five languages yields an equivalent `ShoppingIntent` (category, size, budget, country).
- **E2E (Playwright)**: full purchase in `es`, switch to `de` at the review step, pay, and assert the confirmation amounts equal the DB snapshot.

## 15. Changes this audit implies for the other docs (pending approval)

- `IMPLEMENTATION_PLAN.md` M0.5 is satisfied by this document. M7 gains: choose and measure the detector library, build the `LanguagePolicy`, add the §14.1 tests.
- `DECISIONS.md` ADR-016 status → "Audited; patterns adapted, no code ported verbatim except trivially generic helpers (`countCharacters`, theme provider) from the MIT-licensed Second Look."
