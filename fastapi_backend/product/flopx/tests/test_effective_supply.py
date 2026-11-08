"""Effective supply against a hand-computed region (architecture §10.2).

  operating 200 MW (100,000 GPUs)                 -> 200 x 0.70 = 140 MW
  under construction 100 MW, expected 2025-11-06, as_of 2026-11-06 = 12 months late
                                                  -> p = (1 - 12/24) x 0.8 = 0.4 -> 40 MW
  under construction 100 MW, no date              -> p = 0.25 -> 25 MW
  announced 1,000 MW and reported 500 MW          -> listed, never counted
  effective = 205 MW; GPU equivalents withheld because one counted facility has no estimate
"""

from datetime import datetime, timezone

import pytest

from product.flopx import domain, effective_supply as es
from product.flopx.provenance import sample

AS_OF = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)


def facility(fid, status, mw, *, expected=None, gpus=None):
    return domain.Facility(id=fid, name=fid, lat=0, lon=0, region_id="us-tx", status=status, capacity_mw=mw, gpu_count_estimate=gpus,
                           timeline=domain.FacilityTimeline(expected_online=expected), provenance=sample(AS_OF))


FACILITIES = [
    facility("op", "operating", 200, gpus=100_000),
    facility("late", "under-construction", 100, expected="2025-11-06", gpus=50_000),
    facility("undated", "under-construction", 100),
    facility("ann", "announced", 1000),
    facility("rep", "reported", 500),
    facility("elsewhere", "operating", 999),
]
FACILITIES[-1] = domain.Facility(**{**FACILITIES[-1].__dict__, "region_id": "eu-de"})


def test_hand_computed_region():
    out = es.effective_supply("us-tx", FACILITIES, AS_OF)
    assert (out.operating_mw, out.under_construction_mw, out.announced_mw, out.reported_mw) == (200, 200, 1000, 500)
    assert out.effective_mw == pytest.approx(205.0)
    by = {f.facility_id: f for f in out.facilities}
    assert by["late"].months_late == 12.0 and by["late"].completion_probability == pytest.approx(0.4) and by["late"].contributes_mw == 40.0
    assert by["undated"].completion_probability == 0.25 and by["undated"].contributes_mw == 25.0
    assert by["ann"].contributes_mw == 0 and by["rep"].contributes_mw == 0 and by["op"].contributes_mw == 140.0
    assert out.effective_gpu_equivalents is None and out.gpu_equivalents_coverage.startswith("2 of 3")
    assert "elsewhere" not in by
    assert out.statement_class == "inference" and out.input_kinds == ["sample"]
    assert {a.name: a.value for a in out.assumptions}["utilisation"] == 0.7
    assert all(a.range for a in out.assumptions)


def test_gpu_equivalents_only_when_every_counted_facility_has_an_estimate():
    out = es.effective_supply("us-tx", [FACILITIES[0], FACILITIES[1]], AS_OF)
    assert out.effective_gpu_equivalents == round(100_000 * 0.7 + 50_000 * 0.4)


def test_completion_probability_formula():
    assert es.completion_probability("2028-05-13", AS_OF) == (0.8, 0.0)         # future: not late, full ceiling
    assert es.completion_probability("2024-11-06", AS_OF) == (0.0, 24.0)        # two years late: zero
    assert es.completion_probability(None, AS_OF) == (0.25, None)
    assert es.months_late("2026-10-06", AS_OF) == 1.0 and es.months_late("2026-10-21", AS_OF) == pytest.approx(0.5, abs=0.01)


def test_utilisation_outside_the_stated_range_is_refused():
    with pytest.raises(ValueError):
        es.effective_supply("us-tx", FACILITIES, AS_OF, utilisation=0.95)
    high = es.effective_supply("us-tx", FACILITIES, AS_OF, utilisation=0.9)
    assert high.effective_mw == pytest.approx(200 * 0.9 + 40 + 25)


def test_overrides_replace_completion_probability_for_scenarios():
    out = es.effective_supply("us-tx", FACILITIES, AS_OF, overrides={"late": (1.0, "completed")})
    assert out.effective_mw == pytest.approx(140 + 100 + 25)


# -- one provenance class per region (V1 F1) -------------------------------------------------------

def _wikidata(fid, status, mw, region="us-tx"):
    from product.flopx.provenance import live
    return domain.Facility(id=fid, name=fid, lat=0, lon=0, region_id=region, status=status, capacity_mw=mw,
                           provenance=live("wikidata", AS_OF))


def test_a_region_with_sample_records_counts_only_those_and_names_the_live_ones_left_out():
    mixed = [*FACILITIES, _wikidata("Q1", "operating", 5000), _wikidata("Q2", "announced", 7000)]
    out = es.effective_supply("us-tx", mixed, AS_OF)
    # The sample-only figures of test_hand_computed_region, unchanged by 12,000 live MW in the same region:
    assert (out.operating_mw, out.under_construction_mw, out.announced_mw, out.reported_mw) == (200, 200, 1000, 500)
    assert out.effective_mw == pytest.approx(205.0)
    assert {f.facility_id for f in out.facilities} == {"op", "late", "undated", "ann", "rep"}
    assert out.input_kinds == ["sample"]
    assert "2 observed records for the region listed apart and not mixed in" in out.provenance.note


def test_a_region_the_sample_does_not_cover_uses_the_live_records_alone():
    rows = [*FACILITIES, _wikidata("Q3", "operating", 300, region="eu-ie"), _wikidata("Q4", "under-construction", 100, region="eu-ie")]
    out = es.effective_supply("eu-ie", rows, AS_OF)
    assert out.operating_mw == 300 and out.under_construction_mw == 100
    assert out.input_kinds == ["live"]
    assert "listed apart" not in out.provenance.note


def test_the_capacity_pipeline_factor_reads_one_class_too():
    from product.flopx import pressure
    mixed = [*FACILITIES, _wikidata("Q1", "operating", 5000), _wikidata("Q2", "announced", 7000)]
    factor = pressure.capacity_pipeline(mixed, "us-tx")
    # Sample only: pipeline = under construction 200 + announced 1000 = 1200 over 200 operating = 6.0.
    assert factor.raw == pytest.approx(6.0)
    assert {p.kind for p in factor.evidence} == {"sample"}
    assert "2 observed records" in factor.note
    live_only = pressure.capacity_pipeline([_wikidata("Q5", "operating", 400, region="eu-ie"), _wikidata("Q6", "planned", 200, region="eu-ie")], "eu-ie")
    assert live_only.raw == pytest.approx(0.5) and {p.kind for p in live_only.evidence} == {"live"}
