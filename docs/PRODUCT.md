# PRODUCT.md - FLOPX

The single source of truth for the product: what FLOPX is, every feature it ships, and how
the product layer is wired to the reusable platform and infrastructure layers it runs on.
Paths are repository-relative.

**Status: built; the frontend is live at https://flopx.vercel.app, on the backend at
https://flopx-backend.onrender.com** (build started 2026-11-07; refined brief received the
same day). The product trees are FLOPX's own: the repository was seeded from a reusable
platform template and the template's product layer was replaced wholesale on 2026-11-07.

---

## 1. What FLOPX is

**FLOPX is a Bloomberg-like terminal for the global AI compute market.** It connects GPU
rental prices and capacity, data-centre infrastructure, energy constraints, compute-exposed
company fundamentals and stock prices, prediction markets and news, to surface signals,
explain their implications, explore scenarios, and let a user act through simulated equity
trading and compute-procurement decisions. The chain is observe → compare → interpret →
forecast → decide → execute/simulate → evaluate.

**Audience:** analysts, investors and operators of the compute economy, and the reviewers of
a live demonstration. The product is public to read (overview, map, GPU market, companies,
prediction markets, intelligence feed, Market X-Ray, research lab, procurement desk) and
private to act (paper-trading desk, saved workloads, followed strategies).

**Identity** (`fastapi_backend/infrastructure/instance/instance.json`): brand `FLOPX`,
wordmark `FLOPX`, domain `flopx.vercel.app` (the Vercel hostname standing in for a bought
domain), contact `hello@flopx.vercel.app`, currency `USD`.

**Data posture:** every record carries a provenance class, `live` (fetched from a named
source with observation time), `archived` (stored earlier, timestamped) or `sample`
(authored, labelled, never mixed into live aggregates), and a source plus observation time
wherever a number, place, article or event is shown.

## 2. Feature map

The served frontend is `flopx-web/` (the founder's design app) wired to the backend. An
earlier Next.js frontend was retired from production on 2026-11-08 and is not part of this
repository.

Status words: **built** (code, tests, verified), **dormant** (wired, needs a credential or a
host).

| # | Area | Backend | Frontend | Status |
|---|---|---|---|---|
| 1 | Global overview | `/flopx/overview` | Overview | built |
| 2 | Compute markets (offers, comparable prices, heatmap, FCPI index with basket breaks, changes, cost forecasts) | `/flopx/gpu/*`, `/flopx/forecast` | GPU market | built |
| 3 | Energy and data-centre infrastructure | `/flopx/infra/*`, `/flopx/map/*` | Energy, Map | built |
| 4 | Equities, clusters and derivatives | `/flopx/companies/*`, `/flopx/exposure` | Securities | built |
| 5 | Prediction markets and news intelligence | `/flopx/markets/*`, `/flopx/feed` | Markets, Feed | built |
| 6 | Research and forecast evaluation | `/flopx/research/*`, walk-forward evaluation harness | Research | built |
| 7 | Paper trading and portfolio (Systematic Compute Fund) | `/flopx/paper/*`, `/flopx/fund/*`, decision ledger with replays | Trading desk, Fund | built |
| 8 | Compute procurement analysis | `/flopx/procure/*` | Procurement | built |
| 9 | Contextual assistant | `/flopx/ask` (grammar and composed brief; language-model mode dormant) | Ask FLOPX, command palette | built; model mode dormant |
| A | Compute Economy X-Ray (flagship) | `/flopx/pressure`, `/flopx/anomalies`, `/flopx/scenario/infra`, `/flopx/exposure` | X-Ray | built |
| B | Systematic Compute Fund (flagship) | `/flopx/research/evaluation`, `/flopx/research/signals`, `/flopx/fund/summary`, decision accept and replay | Fund | built |
| - | Source attribution | licence table | Sources | built |

Verification during the build: seven independent review passes (V1 to V7), two re-walks (R1,
R2) and two fix rounds, all against the running deployment.

## 3. Wiring

| Wire | State |
|---|---|
| Frontend hosting: Vercel project `flopx`, alias `https://flopx.vercel.app`, framework preset none, static upload of `flopx-web/dist` (see `flopx-web/README.md`) | live since 2026-11-08 02:10Z (the tours build, with one consistent time axis and backend-derived validation claims, since 06:10Z), calling `flopx-backend` directly |
| Backend hosting: Render `flopx-backend` (starter, Frankfurt) at https://flopx-backend.onrender.com, with a Render Postgres; one uvicorn process since 2026-11-08 05:52Z (background threads at low priority so the readiness probe is never starved on the 0.5-CPU instance) | live |
| Backend product boundary: `product.routers.router` mounts `/flopx`; `product.startup.STARTUP_TASKS` = sample-data validation, the licensed-archive loads, and the per-process live-source refresher (`product/flopx/refresher.py`: reads answer from a warm snapshot and never wait on an upstream); no seeded organisations; no payment catalogue; no question flows | live |
| Backend tables: `flopx_observations` (archive), `flopx_paper_accounts/orders/positions`, `flopx_workloads`, `flopx_strategy_decisions`; the template's earlier migrations kept as no-ops with their ids; `20261107_01` adds the FLOPX tables | live |
| Capabilities: no FLOPX-specific grant; the paper desk and the fund are gated by sign-in only | live |
| Frontend composition: every screen is public; the desk runs a browser-local ledger without an account; the fund's dashboard tab asks a signed-out reader to log in while its research tabs stay open; dark is the default theme | live |
| Frontend data: `flopx-web/src/live/` fetches the backend in parallel with timeouts and swaps real values into the design's data modules as they land; anything not backed by data keeps the design's `illustrative` badge (`flopx-web/src/live/README.md` is the contract) | live |
| Design: the founder's Figma Make design is the visual source (palette, Manrope display, JetBrains Mono, the sidebar, ticker and Ask FLOPX frame), filled only with FLOPX data | live |
| Email: platform transactional templates only (14) | live |
| Live sources (keyless): RunPod, Akash, Polymarket, Kalshi, SEC EDGAR, Energy-Charts DE-LU, Elexon GB, Hacker News, four investor-relations RSS feeds, Wikidata, Yahoo chart (display only, never archived). Vast.ai asks are shown as a current market reading only, never archived and never in an index, because its terms forbid an index or price comparison built on its data. Use, licence and attribution per source come from one table, `fastapi_backend/product/flopx/sources/licences.json`, shown publicly on the Sources screen and in every panel's sources line | live; each source degrades to `unavailable`, never to sample |
| Licensed archives (loaded at startup from files or keys the deployment holds, never committed): WRDS CRSP daily closes and Compustat quarterly fundamentals for eight tickers (raw rows to signed-in accounts only; derived results public), Alpaca Market Data 2026 daily bars (same gate), GetDeploying weekly GPU price history (CC BY 4.0, public) | live on the deployment; absent locally unless the files or keys are supplied |
| EIA Form EIA-930 hourly demand (ERCOT, PJM): `EIA_API_KEY` | dormant without the key |
| AI provider (assistant language-model mode): `ANTHROPIC_API_KEY` | dormant; no key set |
| Alpaca paper brokerage: `PRODUCT_ALPACA_*` | dormant; the built-in simulated broker is the default |

## 4. Content and data model

The sample set is authored JSON under `fastapi_backend/product/flopx/data/sample/` (six GPU
models, eight regions, six fictional providers, two offer snapshots, forty facilities on public
data-centre geographies, eight companies, twelve prediction markets, forty feed items,
twenty-one series), regenerated by `sample_generator.py` and validated at boot. Every record
carries a provenance block; sample rows are never mixed into a live aggregate. Live observations
are archived in `flopx_observations` with series ids that embed source and entity, so history
accrues from the first deploy. The inferred-state layer (pressure index, effective supply,
anomalies, exposure), the evaluation harness, the signal and decision ledger and the fund
analytics are built in the backend and pinned by drift fixtures under
`fastapi_backend/product/flopx/tests/`. Archive reads refuse rows from sources that are off or
display-only and rows dated after their own fetch; an index point records its basket, and a
basket change is a series break no comparison reads across. The regional pressure grid takes a
model's live asks only when one of them is placed in a region (RunPod and Akash publish none);
otherwise it keeps the sample asks, labelled sample.

## 5. Status ledger

| Date | Event |
|---|---|
| 2026-11-06 | Repository seeded from the platform template; template identity deployed. |
| 2026-11-07 | FLOPX brief received; identity switched to FLOPX; product layer replaced; backend live sources and frontend data layer landed; refined brief received and specified; the two flagship engines started. |
| 2026-11-07 (evening) | Engines and screens landed; review passes V1 to V7; first fix round; re-walks R1 and R2; pressure-grid fix; second fix round. |
| 2026-11-08 | The founder design app became the served frontend; the three judge tours shipped; licensed archives (CRSP, Compustat, Alpaca, GetDeploying) loaded; research screen gained the information coefficient and per-ticker coverage. |

updated_at: 2026-11-08
