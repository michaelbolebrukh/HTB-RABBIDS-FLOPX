# FLOPX

**A Bloomberg-like terminal for the global AI compute market.**

FLOPX connects GPU rental prices and capacity, data-centre infrastructure, energy constraints,
compute-exposed company fundamentals and stock prices, prediction markets and news. It surfaces
signals, explains their implications, explores scenarios, and lets a user act through simulated
equity trading and compute-procurement decisions. The chain is observe, compare, interpret,
forecast, decide, execute or simulate, evaluate.

Nobody publishes GPU utilisation. FLOPX reads what is public, says which class of statement each
number is (observed, inferred, forecast, decision), shows the inference with its uncertainty, and
is explicit about what it refuses to claim.

| | |
|---|---|
| Live frontend | https://flopx.vercel.app |
| Live backend | https://flopx-backend.onrender.com (FastAPI; `/docs` is the OpenAPI explorer) |
| Product description | [`docs/PRODUCT.md`](docs/PRODUCT.md) |
| Data sources and licences | [`LICENSES.md`](LICENSES.md) and the Sources screen in the app |

## The three tours

The app carries three guided tours, started from the tour buttons in the top bar. Each is a
sequence of beats that navigates the real app and annotates what is on screen.

| Tour | Thesis | What it walks through |
|---|---|---|
| **G-Research** (make the invisible visible) | Nobody publishes GPU utilisation. FLOPX reads what is public, says which class of statement each number is, and shows the inference, its uncertainty and what it refuses to claim. | The market nobody can see (overview and ticker); the four classes of statement; six factors into one regional pressure score; the raw-to-inferred reveal on the X-Ray; the forecast evaluation ("tested, and honest about it"); one anomaly with its evidence; a scenario that changes only estimates; who is exposed; and what a buyer would do (ranked procurement options). |
| **HRT** (best use of data for predictions and decisions) | Every ask priced, every claim tested against a baseline, and no number that was not observed shown as if it had been. The desk proposes; a person decides; the paper fill is attributed back. | What FLOPX retrieved tonight (prediction-market quotes); every source, its licence and its last observation; the pre-registered evaluation ("edge in 0 of 24 is a result"); the forecast that refuses when history is thin; signal to proposal; a person presses Send to paper; paper fill, then attribution. |
| **Metaview** (the whole loop, reviewed) | Retrieval, analysis, modelling, estimation and decision on one truth system, ending with what is real today, what is still illustrative, and what comes next. | The G-Research and HRT beats in one pass, then the loop closing on the brief, the contextual agent answering from engine tools, and a closing review of what is real today. |

## What is live and what is illustrative

FLOPX never shows a synthetic number as observed. Every value carries one of four freshness
badges: `live` (fetched from a named source, with observation time), `historical` (archived
earlier, timestamped), `illustrative` (the design's own values, or the authored sample set), or
`n/a`. Where a chart has a real present but only an illustrative past or future, the illustrative
points are scaled to join the real reading and the chart says so in its eyebrow. The table below
is derived from `docs/PRODUCT.md` §3 and `flopx-web/src/live/README.md`.

| Area | Status today | Notes |
|---|---|---|
| GPU asks and comparable prices (RunPod, Akash; Vast.ai as a current reading) | **Live** | Keyless public APIs, polled on the backend at most every five minutes; archived except Vast.ai, which its terms keep out of any index. |
| FCPI compute price index | **Live, young** | FLOPX's own index history starts 8 November 2026. The past months on the chart are GetDeploying's weekly medians (CC BY 4.0, archived), drawn as one line and attributed under the chart. |
| Regional pressure, drivers, anomalies, exposure (X-Ray, eight regions) | **Live (derived)** | Inferred from live asks, archived series and public infrastructure records; each factor states its inputs. Where a region has no live ask placed in it, the grid keeps the sample asks and labels them sample. |
| Prediction markets | **Live** | Polymarket and Kalshi quotes, archived. |
| Company revenue growth | **Live** | SEC EDGAR XBRL facts, public domain. |
| Equity closes, fundamentals (eight tickers) | **Historical, signed-in only for raw rows** | CRSP and Compustat via WRDS (institutional research licence) and Alpaca Market Data (personal account), loaded from files and keys the deployment holds, never committed here. Derived results (evaluation, backtests, signals) are public. |
| Forecast evaluation and strategy backtests | **Live (derived)** | Walk-forward harness against a baseline; it reports negative and inconclusive results honestly. Today every signal status reads "insufficient history" and the compute-augmented model does not beat the baseline. |
| Point forecasts | **Refused today** | `/flopx/forecast` refuses until a series has twice the horizon in history; the app shows the refusal, not a design value. |
| Power markets | **Live for DE-LU (Energy-Charts) and GB (Elexon); illustrative elsewhere** | US grid demand (EIA) is wired but dormant without an `EIA_API_KEY`. No wholesale US price is carried. |
| Data-centre map | **Live, partial** | Notable facilities from Wikidata (CC0) plus forty authored facilities on public geographies, labelled sample. Never a complete map. |
| Intelligence feed | **Live** | Hacker News (Algolia) and four investor-relations RSS feeds; headline, date and link only. |
| Securities prices the backend does not cover, valuations, options | **Illustrative** | The design's values, badged. Yahoo Finance prices are display only, delayed, never archived. |
| Fund account figures (NAV, account value) | **Illustrative** | Badged; the decision ledger, paper fills and attribution are real and replayable. |
| Paper trading desk | **Simulated** | Built-in simulated broker; a browser-local ledger without an account, a server-side one when signed in. Alpaca paper brokerage is wired but dormant. |
| Ask FLOPX assistant | **Grammar mode live; language-model mode dormant** | Answers are composed from engine tools and the grammar; no model key is set on the deployment. |
| Narrative panels in the design | **Illustrative** | Badged where no real data backs them. |

## Data sources

Every source, its licence and the use FLOPX makes of it comes from one table,
`fastapi_backend/product/flopx/sources/licences.json`, rendered on the Sources screen and in every
panel's sources line. `LICENSES.md` lists them with their licence lines. In short:

- Public domain or open: SEC EDGAR, EIA (US government works); Wikidata (CC0); Energy-Charts
  (CC BY 4.0, Bundesnetzagentur | SMARD.de via Fraunhofer ISE); GetDeploying GPU price history
  (CC BY 4.0); Elexon BMRS (open data licence, attribution shown).
- Public APIs under the provider's terms, risk recorded: RunPod, Akash Network, Polymarket,
  Kalshi, Hacker News (Algolia), investor-relations RSS feeds, OpenRouter (one dated snapshot).
- Display only, never archived: Yahoo Finance (unofficial endpoint); Vast.ai (never an index).
- Research archives, raw rows to signed-in accounts only, removed after the hackathon: WRDS
  CRSP, WRDS Compustat, Alpaca Market Data. None of these files is in this repository.

## Run it locally

Two halves. The frontend works against the live backend with no local backend at all.

### Frontend (`flopx-web/`, Vite, React 19, Tailwind 4)

```bash
cd flopx-web
npm ci
npx vite --port 5300 --strictPort     # dev server; /flopx/* and /auth/* are proxied to the backend
npx vitest run                        # adapter tests against recorded payloads
npx vite build                        # production bundle in dist/
```

`VITE_API_BASE_URL` (default `https://flopx-backend.onrender.com`) is the backend origin. Point
it at `http://localhost:8000` to use a local backend.

### Backend (`fastapi_backend/`, FastAPI, Python 3.13, PostgreSQL)

```bash
cd fastapi_backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then set the three required values below
uvicorn main:app --host 0.0.0.0 --port 8000
```

Required in `.env`: `DATABASE_URL` (a PostgreSQL URL; migrations run at startup),
`HASH_KEY` (any long random string) and `APP_DOMAIN=flopx.vercel.app` (checked against the
instance manifest). Leave `ENV=development`. The licensed archives (CRSP, Compustat, Alpaca)
and the EIA series are absent locally unless you supply the files or keys; every other source
is keyless and starts fetching on boot. Tests: `python -m pytest product/flopx/tests`.

## Repository layout

```text
flopx-web/            the served frontend (the team's design, src/live/ swaps in real data)
fastapi_backend/      the running backend
  product/flopx/      FLOPX: sources, engines (pressure, anomalies, exposure, forecast,
                      evaluation, signals, fund, procurement), paper broker, sample data, tests
  platform/           reusable services the product runs on (auth, email, storage, payments)
  infrastructure/     settings, database, migrations, startup
docs/PRODUCT.md       the product description and wiring
LICENSES.md           every third-party data source and its licence line
```

## Team

FLOPX is Team RABBIDS' hackathon entry, built by Michael Bolebrukh, Erdem Gunseli, Emre and the rest of the team.
