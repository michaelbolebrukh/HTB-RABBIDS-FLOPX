"""Anomaly honesty after the second fix wave (re-walk R1-7, R1-8 and decision G11-b), at the route composition.

The adapters are stubbed the way the real ones answer since Vast was switched off: Vast is off, RunPod and Akash
both publish their asks with region "global" (neither names a datacentre region), everything else is unavailable.
Before 7c03906 this shape emptied every regional cell; here it must leave the sample grid scannable, fire more
than one anomaly, and every anomaly must say what it rests on.
"""

from dataclasses import replace

import pytest

from product.exceptions import UnknownEntityError
from product.flopx import domain, terminal
from product.flopx.provenance import live
from product.flopx.sources import akash, available, runpod
from product.flopx.tests.test_inferred_honesty import H100, LIVE_OFFERS, T_LIVE, _next_window, clock, db, live_sources  # noqa: F401


@pytest.fixture
def global_asks(monkeypatch, live_sources):  # noqa: F811
    runpod_asks = [replace(o, region_id="global") for o in LIVE_OFFERS]
    akash_asks = [replace(o, id=f"akash-{i}", provider_id="akash", region_id="global", gpu_model_id="a100-sxm-80gb") for i, o in enumerate(LIVE_OFFERS)]
    monkeypatch.setattr(runpod, "fetch_gpu_offers", lambda: available("runpod", runpod_asks, live("runpod", T_LIVE, licence="test")))
    monkeypatch.setattr(akash, "fetch_gpu_offers", lambda: available("akash", akash_asks, live("akash", T_LIVE, licence="test")))
    _next_window()


def test_global_live_asks_leave_more_than_one_anomaly_each_stating_its_basis(db, clock, global_asks):  # noqa: F811
    payload = terminal.anomalies_view(db, None, None, None)
    found = payload["anomalies"]
    assert payload["cells_scanned"] > 0, "the regional grid kept its sample asks"
    assert len(found) > 1, [a.id for a in found]
    divergences = [a for a in found if a.type == "divergence"]
    assert divergences, "with no documented link, divergence rests on the labelled authored basket (G11-b)"
    for a in divergences:
        assert "not a filing" in a.headline and "linked by authored sample" in a.headline, a.headline
        assert a.measures["basketBasis"] == "authored"
        assert "sample" in a.provenance.input_kinds, a.id
    for a in found:
        supporting = {f.factor for f in a.supporting}
        assert not [f.factor for f in a.contradicting if f.factor in supporting], f"{a.id}: a factor in both lists (R1-7)"


def test_the_skipped_reason_names_the_links_actual_basis(db, clock, global_asks):  # noqa: F811
    detail = terminal.anomaly_detail(db, f"anomaly:announcement-mismatch:us-va:{H100}")
    owners = [e for e in detail["linked_companies"] if "facility-owner" in e.basis]
    assert owners, "the Virginia sample facilities name owners"
    note = detail["signal"].skipped["note"]
    assert "linked by authored sample facility records" in note, note
    assert "only by authored profile" not in note


def test_an_id_no_tier_holds_is_an_unknown_entity(db, clock, global_asks):  # noqa: F811
    with pytest.raises(UnknownEntityError):
        terminal.anomaly_detail(db, f"anomaly:bottleneck:us-va:{H100}")
