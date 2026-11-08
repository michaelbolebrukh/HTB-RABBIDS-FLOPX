"""The index never mixes tiers, labels itself honestly, and the adjustment breakdown is exact."""

from datetime import datetime, timezone

import pytest

from product.exceptions import MixedProvenanceError
from product.flopx import adjust, domain
from product.flopx.indices import compute_index
from product.flopx.provenance import effective_kind, is_sample, live, sample

T = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)
H100 = domain.GpuModel(id="h100-sxm-80gb", vendor="NVIDIA", family="Hopper", memory_gb=80, interconnect="NVLink", fp16_tflops=989, release_year=2022)
H200 = domain.GpuModel(id="h200-sxm-141gb", vendor="NVIDIA", family="Hopper", memory_gb=141, interconnect="NVLink", fp16_tflops=989, release_year=2024)
REGIONS = {
    "us-va": domain.Region(id="us-va", name="Virginia", country_code="US", lat=39.0, lon=-77.5),
    "eu-de": domain.Region(id="eu-de", name="Frankfurt", country_code="DE", lat=50.1, lon=8.7),
}


def offer(i, price, *, contract="on-demand", region="us-va", prov=None, model="h100-sxm-80gb", provider="p"):
    return domain.GpuOffer(
        id=f"o{i}", provider_id=provider, region_id=region, gpu_model_id=model, gpu_count=8,
        price_per_gpu_hour_usd=price, availability="available", interruptible=contract == "spot",
        contract=contract, provenance=prov or sample(T),
    )


def test_mixing_sample_and_live_offers_is_refused():
    rows = [offer(1, 3.0), offer(2, 3.2, prov=live("vast-ai", T))]
    with pytest.raises(MixedProvenanceError) as exc:
        compute_index(rows, H100, REGIONS)
    assert exc.value.details["kinds"] == ["live", "sample"]


def test_index_over_sample_offers_is_labelled_sample_and_lists_the_limitation():
    ix = compute_index([offer(1, 3.0), offer(2, 3.4), offer(3, 2.8)], H100, REGIONS)
    assert ix.provenance.kind == "derived" and ix.provenance.input_kinds == ("sample",)
    assert effective_kind(ix.provenance) == "sample"
    assert any("sample" in limitation for limitation in ix.limitations)
    assert ix.current == 3.0                      # median of 2.8, 3.0, 3.4 (all on-demand, US)
    assert (ix.coverage.providers, ix.coverage.regions, ix.coverage.offer_count) == (1, 1, 3)


def test_index_over_live_offers_never_reads_as_sample():
    rows = [offer(i, 3.0 + i / 10, prov=live("runpod", T), provider=f"p{i}") for i in range(1, 7)]
    ix = compute_index(rows, H100, REGIONS)
    assert ix.provenance.input_kinds == ("live",)
    # A computed value stays `derived`; only derived-FROM-SAMPLE collapses to sample:
    assert effective_kind(ix.provenance) == "derived"
    assert not is_sample(ix.provenance)
    assert not any("sample" in limitation for limitation in ix.limitations)
    assert ix.coverage.providers == 6


def test_index_uses_adjusted_prices_so_a_cheap_spot_ask_does_not_drag_it_down():
    on_demand = [offer(1, 3.0), offer(2, 3.0), offer(3, 3.0)]
    with_spot = on_demand + [offer(4, 1.0, contract="spot")]
    assert compute_index(with_spot, H100, REGIONS).current == pytest.approx(
        # spot 1.0 grossed up by 1/(1-0.55) = 2.2222; median of (2.2222, 3, 3, 3) = 3.0
        3.0
    )
    four_spot = on_demand + [offer(4, 1.0, contract="spot"), offer(5, 1.0, contract="spot"), offer(6, 1.0, contract="spot")]
    # median of (2.2222 x3, 3.0 x3) = (2.2222 + 3.0) / 2
    assert compute_index(four_spot, H100, REGIONS).current == pytest.approx((1 / 0.45 + 3.0) / 2, abs=1e-4)


def test_adjustment_breakdown_multiplies_out_to_the_adjusted_price():
    spot_de = offer(1, 1.0, contract="spot", region="eu-de")
    result = adjust.adjust_offer(spot_de, H100, REGIONS["eu-de"])
    names = [s.name for s in result.steps]
    assert names == ["interruptible", "region", "memory"]
    factors = {s.name: s.factor for s in result.steps}
    assert factors["interruptible"] == pytest.approx(1 / (1 - adjust.INTERRUPTIBLE_DISCOUNT))
    assert factors["region"] == pytest.approx(1 / adjust.REGION_FACTORS["DE"])
    assert factors["memory"] == 1.0
    product = 1.0
    for f in factors.values():
        product *= f
    assert result.adjusted == pytest.approx(result.quoted * product, abs=1e-4)
    assert result.adjusted == pytest.approx(1.0 / 0.45 / 1.08, abs=1e-4)
    assert result.reference["interruptibleDiscount"] == adjust.INTERRUPTIBLE_DISCOUNT


def test_memory_variant_normalisation_uses_the_square_root():
    h200 = offer(1, 4.0, model="h200-sxm-141gb")
    result = adjust.adjust_offer(h200, H200, REGIONS["us-va"], reference_memory_gb=80)
    memory = next(s for s in result.steps if s.name == "memory")
    assert memory.factor == pytest.approx((80 / 141) ** 0.5)
    assert result.adjusted == pytest.approx(4.0 * (80 / 141) ** 0.5, abs=1e-4)


def test_on_demand_us_offer_is_unchanged_by_adjustment():
    result = adjust.adjust_offer(offer(1, 2.5), H100, REGIONS["us-va"])
    assert result.adjusted == result.quoted == 2.5
    assert all(s.factor == 1.0 for s in result.steps)
