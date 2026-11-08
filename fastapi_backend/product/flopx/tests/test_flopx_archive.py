"""The archive is idempotent on (series, observation time), bounded for static values,
and reads back labelled `archived`; the unique constraint is exercised on a real SQLite DB."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import infrastructure.cache as cache_module
from infrastructure.cache import MemoryBackend
from infrastructure.database import Base
import platform.models  # noqa: F401
import product.models  # noqa: F401
from product.flopx import archive, domain
from product.flopx.provenance import live, sample
from product.flopx.sources import available
from product.models import Observation

T0 = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
NOW = T0 + timedelta(days=30)


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(256))
    # The archive refuses future-dated rows (R1-12), so its clock is pinned after every row these tests write:
    monkeypatch.setattr(archive, "utcnow", lambda: NOW)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.close()


def obs(series_id: str, t: datetime, value: float) -> domain.PriceObservation:
    # RunPod, an archivable source: Vast.ai is `off`, and the archive now refuses its rows at write and read (R1-5).
    return domain.PriceObservation(series_id=series_id, t=t, value=value, unit="USD/GPU-hour", provenance=live("runpod", t, fetched_at=t, licence="test"))


def test_record_is_idempotent_on_series_and_observation_time(db):
    rows = [obs("offer:runpod-1:price", T0, 2.4), obs("offer:runpod-1:price", T0, 2.4), obs("offer:runpod-2:price", T0, 2.6)]
    assert archive.record(db, rows) == 2
    assert archive.record(db, rows) == 0
    assert db.query(Observation).count() == 2


def test_an_unchanged_value_is_re_archived_at_most_hourly_and_every_change_is_kept(db):
    series = "offer:runpod-1:price"
    assert archive.record(db, [obs(series, T0, 2.4)]) == 1
    assert archive.record(db, [obs(series, T0 + timedelta(minutes=5), 2.4)]) == 0     # same value, five minutes later
    assert archive.record(db, [obs(series, T0 + timedelta(minutes=10), 2.5)]) == 1    # a move is always a new row
    assert archive.record(db, [obs(series, T0 + timedelta(minutes=5), 9.9)]) == 0     # older than the latest: never rewritten
    assert archive.record(db, [obs(series, T0 + timedelta(hours=2), 2.5)]) == 1       # unchanged but an hour on: a heartbeat row
    assert [r.value for r in db.query(Observation).order_by(Observation.t).all()] == [2.4, 2.5, 2.5]


def test_archived_series_reads_back_labelled_archived_and_extends_a_sample_series(db):
    archive.record(db, [obs("equity:NVDA:close", T0 + timedelta(days=d), 180.0 + d) for d in range(3)])
    read = archive.archived_series(db, "equity:NVDA:close")
    assert [p.value for p in read.points] == [180.0, 181.0, 182.0]
    assert all(p.provenance.kind == "archived" and p.provenance.source == "runpod" for p in read.points)
    since = archive.archived_series(db, "equity:NVDA:close", since=T0 + timedelta(days=1))
    assert [p.value for p in since.points] == [181.0, 182.0]
    assert archive.archived_series(db, "equity:XXXX:close") is None
    sample_points = [domain.PriceObservation("equity:NVDA:close", T0 + timedelta(days=d), 100.0, "USD", sample(T0)) for d in range(2)]
    extended = archive.extend(domain.Series(id="equity:NVDA:close", unit="USD", points=sample_points), db)
    assert [p.provenance.kind for p in extended.points] == ["sample", "sample", "archived"]   # only points newer than the sample series
    assert extended.points[-1].value == 182.0
    assert archive.latest_before(db, "equity:NVDA:close", T0 + timedelta(days=2)).value == 181.0


def test_archive_entry_point_never_raises_and_skips_a_repeat_observation_window(db, monkeypatch):
    offers = [domain.GpuOffer(id="runpod-9", provider_id="runpod", region_id="us-tx", gpu_model_id="h100-sxm-80gb", gpu_count=8, price_per_gpu_hour_usd=3.0,
                              availability="limited", interruptible=False, contract="on-demand", provenance=live("runpod", T0, licence="test"))]
    result = available("runpod", offers, live("runpod", T0, licence="test"))
    assert archive.archive(db, "offers", [result], archive.observations_from_offers) == 2       # price and availability
    assert archive.archive(db, "offers", [result], archive.observations_from_offers) == 0       # the cache marker short-circuits the repeat
    rows = {r.series_id: r.value for r in db.query(Observation).all()}
    assert rows == {"offer:runpod-9:price": 3.0, "offer:runpod-9:available": 0.5}

    def boom(records):
        raise RuntimeError("converter bug")

    monkeypatch.setattr(cache_module.cache, "_backend", MemoryBackend(256))
    assert archive.archive(db, "offers", [result], boom) == 0                                   # logged, not raised
    assert db.query(Observation).count() == 2
