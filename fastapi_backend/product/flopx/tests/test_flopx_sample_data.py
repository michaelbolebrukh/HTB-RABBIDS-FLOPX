"""The sample datasets validate, carry their provenance block, and have the §7 volumes;
the product seams hold the shape infrastructure composes against."""

import json
from pathlib import Path

import pytest

from product.exceptions import SampleDataInvalidError
from product.flopx import changes
from product.flopx.data import loader
from product.flopx.data.loader import load_sample_uncached, validate_cross_references


@pytest.fixture(scope="module")
def data():
    return load_sample_uncached()


def test_every_sample_file_carries_the_sample_provenance_block():
    for name, _ in loader.FILES.values():
        raw = json.loads((loader.SAMPLE_DIR / name).read_text())
        prov = raw["provenance"]
        assert prov["kind"] == "sample", name
        assert prov["source"] == "flopx-sample-v1", name
        assert prov["authoredAt"] == "2026-11-07", name
        assert prov["basis"] and prov["note"], name


def test_volumes_match_the_architecture(data):
    assert len(data.offers) == 60
    assert len(data.providers) == 6 and len(data.regions) == 8 and len(data.gpu_models) == 6
    assert len(data.facilities) == 40
    assert set(data.companies) == {"NVDA", "CRWV", "NBIS", "IREN", "APLD", "CORZ", "VRT", "SMCI"}
    assert len(data.markets) == 12
    assert len(data.feed) == 40
    for gid in data.gpu_models:
        assert len(data.series[f"gpu:{gid}:on-demand:global"].points) == 180
        assert len(data.series[f"gpu:{gid}:on-demand:global:monthly"].points) == 24


def test_prices_sit_at_plausible_levels_and_are_all_sample(data):
    h100 = [o.price_per_gpu_hour_usd for o in data.offers if o.gpu_model_id == "h100-sxm-80gb" and o.contract == "on-demand"]
    assert h100 and all(2.0 <= p <= 4.5 for p in h100)
    assert all(o.provenance.kind == "sample" for o in data.offers)
    assert all(f.provenance.kind == "sample" for f in data.facilities)


def test_cross_references_resolve(data):
    assert validate_cross_references(data) == []


def test_a_broken_file_is_refused_with_the_file_named(tmp_path: Path):
    for name, _ in loader.FILES.values():
        (tmp_path / name).write_text((loader.SAMPLE_DIR / name).read_text())
    offers = json.loads((tmp_path / "gpu_offers.json").read_text())
    offers["items"][0]["providerId"] = "no-such-provider"
    (tmp_path / "gpu_offers.json").write_text(json.dumps(offers))
    with pytest.raises(SampleDataInvalidError) as exc:
        load_sample_uncached(tmp_path)
    assert "no-such-provider" in exc.value.details["problem"]

    offers["items"][0]["pricePerGpuHourUsd"] = -1
    (tmp_path / "gpu_offers.json").write_text(json.dumps(offers))
    with pytest.raises(SampleDataInvalidError) as exc:
        load_sample_uncached(tmp_path)
    assert exc.value.details["file"] == "gpu_offers.json"


def test_change_detection_finds_the_authored_differences(data):
    detected = changes.detect_changes(data.offers_previous, data.offers)
    by_kind = {}
    for c in detected:
        by_kind.setdefault(c.kind, []).append(c)
    assert {c.offer_id for c in by_kind["price"]} == {"offer-003", "offer-011", "offer-027", "offer-040"}
    assert [c.offer_id for c in by_kind["availability"]] == ["offer-015"]
    assert [c.offer_id for c in by_kind["new"]] == ["offer-058"]
    assert [c.offer_id for c in by_kind["removed"]] == ["offer-900"]
    price = next(c for c in by_kind["price"] if c.offer_id == "offer-003")
    assert price.before["pricePerGpuHourUsd"] != price.after["pricePerGpuHourUsd"]
    assert price.pct_change == pytest.approx(10.0, abs=0.3)
    assert [e.kind for e in price.evidence] == ["offer", "series"]


def test_product_seams_hold_their_factory_shape():
    from product import organisations, payments, startup
    from product.routers import router
    import product.question_flows  # noqa: F401
    from platform.question_flows import all_flows

    assert organisations.SEEDED_ORGANISATIONS == [] and organisations.SPONSOR_TIER_CAPABILITIES == {}
    assert payments.CATALOG == []
    assert [t.__name__ for t in startup.STARTUP_TASKS] == ["check_sample_data", "load_research_archive", "start_refresher", "start_history_backfill"]
    assert all_flows() == ()
    assert all(route.path.startswith("/flopx") for route in router.routes)
