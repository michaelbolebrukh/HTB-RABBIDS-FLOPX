"""The composed assessment behind the `brief` intent (architecture §10.10): not a language model.

`compose()` takes the evidence the product already holds for one target (a
region, a ticker or a GPU model): the cell's pressure factors, open anomalies,
effective supply, recent feed items, market quotes, the latest observations and
the evaluation verdicts, and writes a short assessment in fixed sentence
templates. Every sentence carries the ids of the evidence it rests on and its
statement class (observation, inference, forecast or decision), so a reader can
open each one. The payload is labelled `method: composed-from-evidence-v1`,
`statementClass: inference`, `notLanguageModel: true`.

A language-model drafting mode is specified to exist behind
`settings.ANTHROPIC_API_KEY` (the platform generative service), constrained to
rewording the composed assessment without adding claims and keeping the
citations. No key is set anywhere today, so `draft()` is a documented stub
that returns the composed form unchanged and says so; that is what ships.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urlencode

from product.flopx import domain
from product.flopx.provenance import Provenance

METHOD = "composed-from-evidence-v1"
MAX_FEED = 3
MAX_ANOMALIES = 3


@dataclass(frozen=True)
class Sentence:
    text: str
    statement_class: str
    evidence_ids: list[str]


@dataclass(frozen=True)
class EvidenceRef:
    """One cited piece of evidence. `provenance` is the evidence's own envelope (None only where the
    evidence has none, an evaluation verdict); `input_kinds` are the classes it rests on: the derived
    envelope's input kinds, or the observed record's own kind, so a sample-derived inference says so."""
    id: str
    kind: str
    href: str
    statement_class: str
    observed_at: datetime | None = None
    provenance_kind: str | None = None
    provenance: Provenance | None = None
    input_kinds: list[str] = field(default_factory=list)


def _ref(id: str, kind: str, href: str, statement_class: str, prov: Provenance | None, *, observed_at: datetime | None = None,
         input_kinds: list[str] | None = None) -> EvidenceRef:
    if input_kinds is None:
        input_kinds = [] if prov is None else (sorted(prov.input_kinds) if prov.kind == "derived" else [prov.kind])
    return EvidenceRef(id=id, kind=kind, href=href, statement_class=statement_class, observed_at=observed_at,
                       provenance_kind=prov.kind if prov is not None else None, provenance=prov, input_kinds=list(input_kinds))


@dataclass(frozen=True)
class Brief:
    target_kind: str
    target_id: str
    headline: str
    sentences: list[Sentence]
    evidence: list[EvidenceRef]
    follow_through: list[dict]
    caveats: list[str]
    method: str = METHOD
    statement_class: str = "inference"
    not_language_model: bool = True
    engine: str = "composed"
    language_model_note: str = "no language model was used; a drafting mode exists behind ANTHROPIC_API_KEY and no key is configured"


def series_href(series_id: str, target_kind: str, target_id: str) -> str:
    """Where a cited series opens with its context (V4 F22): power prices on the energy screen (in the region
    when the brief is about one), every other series in the research page's series lab."""
    if series_id.startswith("energy:"):
        return "/infra?" + urlencode({"r": target_id}) if target_kind == "region" else "/infra"
    return "/research?" + urlencode({"tab": "lab", "series": series_id})


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.1f}%"


def compose(
    *,
    target_kind: str,
    target_id: str,
    target_name: str,
    cells: list[domain.PressureCell],
    anomalies: list[domain.Anomaly],
    supply: domain.EffectiveSupply | None,
    feed: list[domain.IntelItem],
    markets: list[domain.PredictionMarket],
    latest: list[tuple[str, domain.PriceObservation]],
    evaluation: list[dict],
    exposures: list[domain.Exposure],
) -> Brief:
    sentences: list[Sentence] = []
    refs: dict[str, EvidenceRef] = {}

    def cite(ref: EvidenceRef) -> str:
        refs.setdefault(ref.id, ref)
        return ref.id

    kinds = sorted({k for c in cells for k in c.input_kinds} | {i.provenance.kind for i in feed})
    caveats = ["composed from the product's own evidence; sentences are templates, not reasoning"]
    if "sample" in kinds:
        caveats.append("sample inputs: the sample set is authored and observed nowhere")

    # Observations first: the latest sourced values.
    for label, obs in latest[:4]:
        rid = cite(_ref(obs.series_id, "series", series_href(obs.series_id, target_kind, target_id), "observation", obs.provenance, observed_at=obs.t))
        sentences.append(Sentence(f"{label} was last observed at {obs.value:,.4g} {obs.unit} on {obs.t.date().isoformat()} ({obs.provenance.kind} data from {obs.provenance.source}).",
                                  "observation", [rid]))

    # Inferred pressure, the cell or the strongest cell for the target.
    covered = [c for c in cells if c.uncertainty.coverage > 0]
    if covered:
        top = max(covered, key=lambda c: c.score)
        cid = cite(_ref(f"pressure:{top.region_id}:{top.gpu_model_id}", "pressure-cell", f"/xray?r={top.region_id}&g={top.gpu_model_id}",
                         "inference", top.provenance, observed_at=top.provenance.observed_at))
        # A driver pushes the score to the side of 50 it sits on; the others are the contradictions, never both (V4 F19).
        side = (top.score > 50) - (top.score < 50)
        drivers = sorted((f for f in top.factors if f.contribution and (f.contribution > 0) - (f.contribution < 0) == side),
                         key=lambda f: -abs(f.contribution))[:2]
        driver_text = " and ".join(f"{f.factor} ({f.normalised:+.2f})" for f in drivers) or "no factor on its side of 50"
        sentences.append(Sentence(
            f"Inferred compute pressure for {top.gpu_model_id} in {top.region_id} is {top.score:.0f} of 100 ({top.band}), driven mainly by {driver_text}; "
            f"this is an estimate from {top.uncertainty.factors_with_data} of 6 factors over {top.uncertainty.coverage} asks and is labelled {top.uncertainty.label}, not measured utilisation.",
            "inference", [cid]))
        if top.contradictions:
            sentences.append(Sentence("Arguing against that reading: " + ", ".join(f"{f.factor} ({f.normalised:+.2f})" for f in top.contradictions) + ".", "inference", [cid]))
        if len(covered) > 1:
            lo = min(covered, key=lambda c: c.score)
            if lo is not top:
                lid = cite(_ref(f"pressure:{lo.region_id}:{lo.gpu_model_id}", "pressure-cell", f"/xray?r={lo.region_id}&g={lo.gpu_model_id}",
                                 "inference", lo.provenance, observed_at=lo.provenance.observed_at))
                sentences.append(Sentence(f"The slackest cell in view is {lo.gpu_model_id} in {lo.region_id} at {lo.score:.0f} ({lo.band}).", "inference", [lid]))

    if supply is not None:
        sid = cite(_ref(f"supply:{supply.region_id}", "effective-supply", f"/xray?r={supply.region_id}&tab=supply", "inference",
                         supply.provenance, observed_at=supply.provenance.observed_at))
        utilisation = next((a.value for a in supply.assumptions if a.name == "utilisation"), None)
        sentences.append(Sentence(
            f"Facility records show {supply.operating_mw:,.0f} MW operating, {supply.under_construction_mw:,.0f} MW under construction and {supply.announced_mw:,.0f} MW announced or planned in {supply.region_id}; "
            f"at an assumed {utilisation:.0%} utilisation the effective supply is estimated at {supply.effective_mw:,.0f} MW. Announced megawatts are not live capacity.",
            "inference", [sid]))

    for a in anomalies[:MAX_ANOMALIES]:
        aid = cite(_ref(a.id, "anomaly", f"/xray/anomalies/{a.id}", "inference", a.provenance, observed_at=a.provenance.observed_at))
        linked = [e.ticker for e in a.linked_companies if e.documented]
        tail = f" Documented exposure: {', '.join(linked)}." if linked else " No company is linked to it by a facility record or filing."
        sentences.append(Sentence(f"Anomaly {a.type} (severity {a.severity} of 5): {a.headline}.{tail}", "inference", [aid]))

    for item in feed[:MAX_FEED]:
        fid = cite(_ref(item.id, "feed", f"/feed/{item.id}", "observation", item.provenance, observed_at=item.observed_at))
        sentences.append(Sentence(f"{item.observed_at.date().isoformat()}, {item.kind} ({item.classification}, severity {item.severity}): {item.headline}", "observation", [fid]))

    for m in markets[:2]:
        if not m.outcomes:
            continue
        mid = cite(_ref(m.id, "market", f"/markets/{m.id}", "observation", m.provenance, observed_at=m.last_updated))
        sentences.append(Sentence(f"The market \"{m.question}\" prices Yes at {m.outcomes[0].implied_probability:.0%} ({m.venue}); a quote, not a forecast of record.", "observation", [mid]))

    for row in evaluation[:2]:
        eid = cite(_ref(f"evaluation:{row['ticker']}:{row['horizonDays']}d", "evaluation", f"/research?tab=evaluation&target={row['ticker']}&horizon={row['horizonDays']}d",
                         "inference", None, input_kinds=sorted(row.get("inputKinds", []))))
        sentences.append(Sentence(
            f"Out of sample at {row['horizonDays']} days for {row['ticker']}, the compute-augmented model's verdict against the price baseline is {row['verdict']} "
            f"(n = {row['n']}, skill {row['skill'] if row['skill'] is not None else 'n/a'}); on {'/'.join(row['inputKinds'])} closes this tests the method, not the market.",
            "inference", [eid]))

    documented = [e for e in exposures if e.documented]
    if documented:
        ids = [cite(_ref(f"exposure:{e.ticker}:{target_id}", "exposure", f"/companies/{e.ticker}", "inference", e.provenance)) for e in documented[:4]]
        sentences.append(Sentence(
            "Companies with documented links here: " + ", ".join(f"{e.ticker} ({'/'.join(b for b in e.basis if b != 'sample-profile')})" for e in documented[:4]) +
            f"; sensitivity is an authored estimate ({documented[0].sensitivity:+.2f} per 1% demand), not a measured beta.", "inference", ids))

    if not sentences:
        sentences.append(Sentence(f"The product holds no evidence for {target_name} yet.", "observation", []))
    headline = f"{target_name}: " + (f"inferred pressure {max(covered, key=lambda c: c.score).score:.0f}/100, {len(anomalies)} open anomalies, {len(feed)} recent items"
                                     if covered else f"{len(feed)} recent items, {len(anomalies)} open anomalies")
    follow = [{"kind": "open", "route": r.href, "label": r.kind} for r in list(refs.values())[:6]]
    return Brief(target_kind=target_kind, target_id=target_id, headline=headline, sentences=sentences, evidence=list(refs.values()),
                 follow_through=follow, caveats=caveats)


def draft(brief: Brief, api_key: str | None) -> Brief:
    """The language-model drafting mode. Dormant: with no key the composed brief is returned as is, and
    with a key this stub still returns it, because the constrained rewrite (same sentences, same
    citations, no new claims) has not been built or evaluated and shipping an unverified rewrite would
    violate the honesty rules. The branch exists so the seam is visible, not so it can be mistaken for live."""
    if not api_key:
        return brief
    return Brief(**{**brief.__dict__, "language_model_note": "ANTHROPIC_API_KEY is set but the constrained rewrite is not implemented; the composed form is returned unchanged"})
