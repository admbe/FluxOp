# Explore — SQL Console + Command Center

The `#/analytics` surface. Two tabs, both live against the governed semantic
layer — no sample data, no client-side SQL emulation.

## SQL Console · Ask Flux (default tab) — `SqlConsolePage.tsx`

- **Run** POSTs the editor's SQL to `POST /api/semantic/sql`. The backend
  validates it with `api/expert_explorer.validate_expert_sql` (single
  read-only SELECT, `semantic_*` views only, no file/system functions, DDL
  and writes rejected) and executes with a 2 000-row cap and a watchdog.
  Validator refusals come back as the inline warning — they name the
  governed views, so a wrong table name teaches the right one.
- **Ask Flux** POSTs a natural-language question to `POST /api/semantic/expert`.
  The model proposes SQL, the same validator gates it, the result comes back
  with an explanation, assumptions, and chart hints — and the generated SQL
  lands in the editor so the user can tweak and re-run it. When no AI
  provider is configured the panel says so (503) and the console keeps
  working manually.
- Results render as an auto-picked chart (DuckDB column types drive the
  encoding: time × measure → area/line, time × dimension × measure → stacked
  bands folded to 8 series + Other, dimension × measure → bars, single row →
  KPI tiles) above a paginated table.
- The **Governed views** rail reads `GET /api/semantic` — the same catalog
  the layer serves — with availability dots and starter queries per view.

## Command Center — `CanvasPage.tsx`

Executive canvas over the last 30/60/90 days (switcher in the header): KPI
strip with gradient sparklines (billed, effective, coverage, ESR), billed vs
effective hero with an average reference line, top services, 7d movers,
anomaly bars, estate donut. Every tile is a `POST /api/semantic/query`; a
failed tile renders its real error state (503 reads as "data is catching
up") and never substitutes invented numbers.

## Contracts

- `semanticTypes.ts` mirrors `api/semantic_layer.py` SEMANTIC_MODELS so
  model/measure/dimension literals are compile-checked.
- `chartTheme.ts` binds all chart chrome to the `--chart-*` theme tokens.
- Types: `SemanticQueryResult` (semantic queries), `SemanticSqlResult`
  (console SQL), `ExpertExplorerResult` (Ask Flux) in `src/types.ts`.

The 63-card catalog that used to live here (registry.ts) is gone: every card
was another query against the analytical plane, and one working console plus
one curated canvas answer the same questions. `?explore=legacy` still routes
to the old builder page for one release.
