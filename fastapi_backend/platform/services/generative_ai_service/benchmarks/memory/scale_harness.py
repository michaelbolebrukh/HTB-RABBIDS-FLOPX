"""
Scale harness (D62) — recall latency + ranking quality as memory grows.

For N in {100, 1k, 10k} synthetic memories per scope, plants 10 known targets
and queries near each: measures recall@1/@5 (is the planted target found?) and
per-query latency, on BOTH engines over the SAME data:

- pgvector ANN path (HNSW + GIN — the production path, D62)
- exact Python-cosine scan (the non-PG fallback, forced via a support override)

Offline, $0: vectors are deterministic hash-expansions, no model calls. Needs
TEST_DATABASE_URL pointing at a Postgres with the vector extension:

    TEST_DATABASE_URL=postgresql+psycopg2://dev:dev@localhost:5432/memtest \
      .venv/bin/python platform/services/generative_ai_service/benchmarks/memory/scale_harness.py
"""
from __future__ import annotations

import hashlib
import importlib.util
import math
import os
import statistics
import sys
import time
from pathlib import Path

# Env + path before the service package imports Settings (backend root = parents[5]):
sys.path.insert(0, str(Path(__file__).resolve().parents[5]))
os.environ.setdefault("DATABASE_URL", "sqlite://")
os.environ.setdefault("HASH_KEY", "bench")
os.environ.setdefault("APP_DOMAIN", "test.local")
os.environ.setdefault("SENTRY_DSN", "")

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from infrastructure.database import Base
from platform.services.generative_ai_service.memory import pg_recall, store
from platform.services.generative_ai_service.models import AIMemory

_URL = os.environ.get("TEST_DATABASE_URL", "")
_MIGRATION = Path(__file__).resolve().parents[5] / "migrations" / "versions" / "20260713_13_add_pgvector_memory_index.py"

SIZES = [100, 1_000, 10_000]
N_TARGETS = 10
DIM = pg_recall.EMBEDDING_DIM


def _vec(seed: str) -> list[float]:
    out: list[float] = []
    counter = 0
    while len(out) < DIM:
        h = hashlib.sha256(f"{seed}:{counter}".encode()).digest()
        out.extend(b / 255.0 - 0.5 for b in h)
        counter += 1
    v = out[:DIM]
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _near(v: list[float], seed: str, noise: float = 0.05) -> list[float]:
    n = _vec(seed)
    return [a * (1 - noise) + b * noise for a, b in zip(v, n)]


def _setup(engine):
    Base.metadata.create_all(engine, tables=[AIMemory.__table__])
    spec = importlib.util.spec_from_file_location("m13", _MIGRATION)
    m13 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m13)
    with engine.connect() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            m13.upgrade()
        conn.commit()


def _bulk_insert(engine, scope: str, n: int) -> list[tuple[str, list[float]]]:
    """Bulk-loading n memories (targets planted at even spacing); returns the targets:"""
    targets = []
    rows = []
    for i in range(n):
        is_target = i % max(1, n // N_TARGETS) == 0 and len(targets) < N_TARGETS
        text_val = f"target fact {len(targets)} in {scope}" if is_target else f"filler fact {i} about topic {i % 97}"
        vec = _vec(f"{scope}:{'T' + str(len(targets)) if is_target else i}")
        if is_target:
            targets.append((text_val, vec))
        rows.append({"t": text_val, "v": "[" + ",".join(f"{x:.6f}" for x in vec) + "]",
                     "e": "[" + ",".join(f"{x:.6f}" for x in vec) + "]"})
    with engine.connect() as conn:
        for start in range(0, len(rows), 500):
            batch = rows[start:start + 500]
            conn.execute(text(
                "INSERT INTO ai_memories (id, owner_id, scope, text, content_hash, embedding,"
                " source_turn_ids, meta, recorded_at, embedding_vec)"
                " VALUES (gen_random_uuid(), 'u', :scope, :t, md5(:t), CAST(:e AS jsonb),"
                " '[]'::jsonb, '{}'::jsonb, now(), CAST(:v AS vector))"
            ), [{**r, "scope": scope} for r in batch])
        conn.commit()
    return targets


def _measure(db, scope: str, targets, engine_label: str):
    """Querying near each target; returning (recall@1, recall@5, p50 ms, p95 ms):"""
    hits1 = hits5 = 0
    times = []
    for i, (target_text, tvec) in enumerate(targets):
        q = _near(tvec, f"probe:{scope}:{i}")
        t0 = time.perf_counter()
        result = store.recall(db, owner_id="u", scope=scope, query=target_text.split()[0],
                              query_embedding=q, k=5)
        times.append((time.perf_counter() - t0) * 1000)
        got = [m.text for m, _ in result]
        hits1 += got[:1] == [target_text]
        hits5 += target_text in got
    p50 = statistics.median(times)
    p95 = sorted(times)[max(0, int(len(times) * 0.95) - 1)]
    return hits1 / len(targets), hits5 / len(targets), p50, p95


def main():
    if "postgresql" not in _URL:
        print("TEST_DATABASE_URL must point at Postgres with pgvector; aborting.")
        sys.exit(1)
    engine = create_engine(_URL)
    _setup(engine)
    Session = sessionmaker(bind=engine)

    print(f"{'N':>7} | {'engine':8} | {'r@1':>5} {'r@5':>5} | {'p50 ms':>8} {'p95 ms':>8}")
    print("-" * 55)
    for n in SIZES:
        scope = f"scale-{n}"
        with engine.connect() as conn:
            conn.execute(text("DELETE FROM ai_memories WHERE scope = :s"), {"s": scope})
            conn.commit()
        targets = _bulk_insert(engine, scope, n)

        db = Session()
        try:
            r1, r5, p50, p95 = _measure(db, scope, targets, "pgvector")
            print(f"{n:>7} | {'pgvector':8} | {r1:>5.0%} {r5:>5.0%} | {p50:>8.1f} {p95:>8.1f}")
            # Forcing the Python fallback over the same data (support override):
            key = id(db.get_bind().engine)
            pg_recall._SUPPORT_CACHE[key] = False
            r1, r5, p50, p95 = _measure(db, scope, targets, "python")
            pg_recall._SUPPORT_CACHE[key] = True
            print(f"{n:>7} | {'python':8} | {r1:>5.0%} {r5:>5.0%} | {p50:>8.1f} {p95:>8.1f}")
        finally:
            db.close()
    engine.dispose()


if __name__ == "__main__":
    main()
