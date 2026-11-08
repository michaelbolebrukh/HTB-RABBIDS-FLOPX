# flopx-web: the FLOPX frontend

The founder's design (a Figma Make export: Vite, React 19, Tailwind 4), used as delivered. Its
layout, components and interactions are the specification; the only thing FLOPX adds is where the
numbers come from. `src/live/` fetches the backend in parallel with timeouts and swaps real values
into the design's own data modules as they land; anything not backed by data keeps the design's
`illustrative` badge. `src/live/README.md` is the data contract (date model, module pattern,
status by domain).

## Run, test, build

```bash
npm install
PORT=5300 npx vite --port 5300 --strictPort   # dev; /flopx/* is proxied to the backend
npx vitest run                                 # adapter tests against recorded payloads
npx tsc --noEmit
npx vite build                                 # dist/
```

`VITE_API_BASE_URL` (default `https://flopx-backend.onrender.com`) is the backend origin for
production builds; dev uses the proxy so any port works against the backend's CORS list.

## Deploy (production)

The Vercel project `flopx` (team egunseli4-gmailcoms-projects, framework preset: none) serves the
built `dist/` as a static site; routing is hash-based so no rewrites are needed. From a copy of
`dist/` that carries the project link (`.vercel/project.json` from the repo root):

```bash
npx vercel@latest deploy --prod --yes --scope egunseli4-gmailcoms-projects
```

Deploy without `--prod` first for a preview URL and run the side-by-side falsifier against it
(`scratchpad/flopx-run/briefs/w-falsify.md` is the brief shape) before promoting.

## What is real and what is illustrative

See `src/live/README.md`, status by domain. In short: GPU indices and offers, eight X-Ray regions
(pressure, drivers, evidence, anomalies, exposure), the Global Intelligence headline, fund
evaluation and backtests, SEC revenue growth, the feed, Polymarket contracts and DE-LU power are
real; prices and valuations for securities the backend does not cover, the fund account figures,
options, most energy markets and the design's narrative panels are illustrative and badged.
