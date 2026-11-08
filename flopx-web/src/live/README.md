# Live data layer

The founder's design ships synthetic data in `src/data/*.ts`. This folder swaps in real values
from the FLOPX backend where it has them, without changing a single component's look. Every
value that stays synthetic carries the design's own `illustrative` freshness badge.

## How it fits together

- **Client** (`client.ts`): base URL from `VITE_API_BASE_URL` (default
  `https://flopx-backend.onrender.com`), JSON only, per-request timeout (12 s default). It never
  throws: every call resolves to `{ ok: true, data } | { ok: false, error }`.
- **Dev proxy**: the backend's CORS allowlist admits `*.vercel.app` and `localhost:3000`, not
  arbitrary Vite ports, so under `vite` dev the client uses a relative base (`/flopx/...`) and `vite.config.ts`
  proxies it to the backend (honouring `VITE_API_BASE_URL`). Restart your dev server once to pick
  the proxy up. Builds and previews call the backend directly.
- **Boot** (`boot.ts`, started once from `main.tsx`): runs every module in `modules.ts` in
  parallel. Each GET path is fetched once however many modules ask for it (`get` is shared and
  memoised), so two modules reading `/flopx/overview` cost one request. The design renders
  immediately with its illustrative values; nothing waits on the network.
- **Modules** (`gpu.ts` is the pattern): a `LiveModule` is `{ id, load(get, apply) }`. `load`
  fetches through `get`, mutates the design exports **in place** (same arrays, same objects), and
  calls `apply()` whenever part of its data has landed. Return `false` or throw to mark the domain
  failed; the design values then stay, still badged illustrative.
- **Store** (`store.ts`): a version counter plus per-domain status (`pending | live | failed`).
  `lib/store.tsx` subscribes, so every `useStore()` consumer re-renders when anything lands. The
  store exposes the version as `live`: add it to the dependency list of any `useMemo` over design
  data (`useMemo(() => OFFERS.filter(...), [gpu, live])`), or the memo keeps the stale value.
- **AS_OF** (`data/core.ts`) is an `export let` live binding set from the newest backend
  `freshness.generatedAt` that has landed (`asof.ts`). Before the first payload it reads the client
  clock. Read it at render time; never copy it into a module-level constant.

## Adding a domain (W1 to W3)

1. Write `src/live/<domain>.ts` exporting a `LiveModule`. Keep the mapping functions pure and
   exported, and test them beside the module (`<domain>.test.ts`) against a trimmed real payload.
2. Append it to `LIVE_MODULES` in `modules.ts` with a targeted one-line edit.
3. Mutate in place: `arr.length = 0; arr.push(...next)` for arrays, `Object.assign` or field writes
   for objects. Never reassign an exported `const`, and never replace an object a component may
   hold by reference without also replacing it in its array.
4. Mark provenance with `freshOf(provenance)` from `provenance.ts`. It is the only mapping from
   backend provenance to the design's `Freshness`:
   - `live` -> `live`; `archived` -> `historical`; `sample` -> `illustrative` (never live);
   - `derived` follows its inputs: all live -> `live` labelled "Live (derived)"; any sample ->
     `illustrative`; otherwise `historical`.
   `isObserved(provenance)` says whether a value may carry the OBS glyph.
5. Design data that has no backend counterpart: set its freshness to `illustrative` at module load
   (see `PROVIDERS.forEach(...)` and the design offers in `core.ts`). A synthetic value the design
   shipped as `live` or `delayed` is a lie until you do.
6. Backend region ids are not the design's. `regionById` in `core.ts` returns a named stub (quality
   `none`, all numbers zero) for an unknown id, so `regionById(x)!` never crashes; names come from
   `/flopx/map/regions` (`regions.ts`, e.g. "US East and Midwest (Ashburn hub)") or, for ISO
   country codes on Vast.ai offers, `Intl.DisplayNames`. W1: keep this fallback if you rework
   `REGIONS`.

## The date model (one rule for every month-axis chart)

The design draws monthly charts on a fixed 39-slot axis, `MONTHS`, with "now" at `NOW_IDX = 26`.
We keep the slot count and `NOW_IDX`, so all design index arithmetic still works, and re-anchor the
**labels** on the real calendar: slot 26 is the current UTC month (from the backend's
`generatedAt`), slots 0 to 25 are the 26 months before it, slots 27 to 38 the 12 after. `MONTHS` is
relabelled in place if the backend's month differs from the client's.

Real points go onto that axis by `placeOnAxis(design, points)` in `dates.ts`, and only that way:

1. Each backend point lands in the slot of its UTC calendar month.
2. Several points in one slot: the **last** by time wins (month to date for the current month).
3. Points in future slots (after `NOW_IDX`) or before slot 0 are ignored. The past axis never
   shows a forecast and nothing is extrapolated backwards.
4. Slots with no backend point keep the design value and are flagged not-real in the returned
   `real[]`. Placement itself never rescales them.

Then the second step, **`joinToNow(placed, design, mode)`** (decision G14, recorded in
`planning/instance-briefs/flopx.md`): where a series has a real present and only an illustrative
past or future, the illustrative slots are scaled so the curve joins the real points
continuously. Real points never change. The design curve keeps its shape:

- `'multiply'` for prices, indices and $/MWh: illustrative slot = design value x f.
- `'shift'` for 0..100 scores (pressure): illustrative slot = design value + f, clamped to 0..100.
- f is anchored at every real slot (real / design, or real - design). Slots before the first real
  slot take the first anchor, slots after the last real slot (the forecast months) take the last
  anchor, slots between two real slots interpolate between their anchors, so the curve is
  continuous through every join however many real points the archive has supplied. A real slot
  whose design value is NaN or zero cannot anchor a multiply and is skipped; with no usable
  anchor nothing is scaled.
- The uncertainty band scales with the series: multiply its width by `bandFactor` (1 under shift).
- `placeAndJoin(design, points, mode)` does both steps in one call.

Every series family with a real Now runs it: region pressure and `GLOBAL_CPI` (`adapt-w1.ts`,
shift), every GPU index (`gpu.ts`, multiply), the energy zones (`adapt-w3.ts`, multiply), and the
weekly equity closes (`adapt-w2.ts`, `placeWeekly` + `joinToNow`, multiply) once an observed
close lands. Fund NAV would take the same call if it ever became real.

Before G14 the rule was the opposite (design values never rescaled, the step at Now left
visible). The founder asked for one consistent picture: an unscaled synthetic curve beside a real
point reads as a contradiction ("now it is low, but in both past and future it is high"), while a
scaled one still reads as illustrative because it is badged. So every chart whose illustrative
slots were scaled carries one marker in its eyebrow slot, "history and forecast scaled to
today's reading, illustrative" (`scaled: true` on `GpuLive`, `RegionLive.pressure`,
`EnergyMarket`, `PRESSURE_MODEL.globalScaled`). A forecast drawn from design values stays
illustrative and says why (the backend's `/flopx/forecast` refuses until a series has twice the
horizon in history; today that is `flopx.insufficient-history`), and no validation claim
("Validated at 1M", "MAE 2.1 pts (n=25)") may come from the design's tables: signal statuses
come from `/flopx/research/evaluation` verdicts and the forecast refusal (`applySignalValidation`
in `adapt-w2.ts`); today they all read insufficient.

The backend's own GPU index history starts on 08 Nov 2026. For the models the backend also archives
GetDeploying's weekly medians for (`gpu:<model>:getdeploying:weekly-median`, CC BY 4.0, each week
stamped at its end), `gpu.ts` draws those as the chart's past months: one mean per calendar month of
the week-end stamps, real and badged "GetDeploying weekly median · CC BY 4.0 · archived", with the
Now slot GetDeploying's month to date so the drawn line is one source end to end; the FLOPX index's
own days are kept as separate marks (`GpuLive.indexMarks`), the design forecast joins the line's
present, and the attribution line (`GDH_ATTRIBUTION`) is printed once under the chart. The 30-day
change on a card is FLOPX's own once its index spans 30 days, else GetDeploying's, labelled
"30d · GetDeploying" (`GpuLive.chg30`). A model without that series behaves as before: only slot 26
is real and every other slot is the design curve scaled to meet it.

## Honesty checklist

- A value shown as OBS or `live` came from the backend with that provenance.
- Backend `sample` data is `illustrative`, never `live`.
- No invented numbers: a missing backend field renders `n/a`, not a design value.
- A narrative sentence naming a mock fact is replaced by one composed from real data, or sits
  behind the illustrative badge.

## Status by domain

| Module | Reads | Writes | Owner |
|---|---|---|---|
| `gpu` | gpu/index, gpu/series (FCPI history and the GetDeploying weekly medians per model), gpu/offers, map/regions (names), overview (index change), pressure (median regional cpx per GPU) | `GPUS[*].index`, `GPUS[*].live` (+ `.history`, `.indexMarks`, `.chg30`), `GPUS[*].pressure` + `.pressureLive`, `OFFERS`, `PROVIDERS` | W0 |

The shared getter caps requests in flight at 6 and retries a 502/503/504 once after 3 s: the
backend has returned 502 on every route under bursts of parallel boots.
| `finance` (`adapt-w2.ts`) | companies, companies/{t} (SEC EDGAR revenue), research/evaluation, research/evaluation/portfolio; gpu/series?id=equity:T:close only when a close is observed, not sample | `SECURITIES[*].revGrowth`, `.px` (weekly slots with an observed close, `placeWeekly`, the weekly twin of `placeOnAxis`), `.computeLinks` (modelled rows), `MODEL_COMPARE`, `MODEL_COMPARE_META`, `BACKTEST_STATS`, `FAILURES`, `WEEKS`/`WEEK_DATES` labels, `W2_LIVE` | W2 |
| `procure` (`adapt-w5.ts`) | POST procure/plan (three variants per spec: run now, flexible, interruptible), POST forecast for the selected GPU's index (today a 422 `flopx.insufficient-history` refusal, read as data) | nothing in `data/*`: the Procurement page reads `useProcurePlan(spec)` (debounced, cached by workload; the boot module only warms the page's default spec). Plans match `OFFERS` live ids for provenance; live plans hide FLOPX sample asks, sample asks show only when no live ask fits and read illustrative | W5 |
