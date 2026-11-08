"""The engine drift fixtures match the Python engine, case by case.

`tests/fixtures/engine/*.json` are the frozen answers a TypeScript port is tested
against. Re-running every case through the same engine and comparing with the
committed expected value is what stops the files drifting from the Python:
a change to adjust/indices/changes/forecast/scenario/procurement/ask/xray that
moves an answer fails here until `export.py` is re-run and the port re-checked.
"""

import importlib.util
import json
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "engine"


def _load_exporter():
    spec = importlib.util.spec_from_file_location("flopx_engine_export", FIXTURE_DIR / "export.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = _load_exporter()


@pytest.fixture(scope="module")
def data():
    return exporter.load_sample_uncached()


def _cases(engine: str) -> list[dict]:
    return json.loads((FIXTURE_DIR / f"{engine}.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("engine", exporter.ENGINES)
def test_fixture_file_exists_is_json_native_and_small(engine):
    path = FIXTURE_DIR / f"{engine}.json"
    assert path.exists(), f"missing {path.name}: run export.py"
    limit_kb = exporter.SIZE_LIMIT_KB.get(engine, exporter.DEFAULT_SIZE_LIMIT_KB)
    assert path.stat().st_size < limit_kb * 1024, f"{path.name} exceeds {limit_kb} KB"
    text = path.read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    rows = json.loads(text)
    assert rows and all(set(row) == {"name", "input", "expected"} for row in rows)
    assert len({row["name"] for row in rows}) == len(rows), "case names must be unique"


@pytest.mark.parametrize("engine", exporter.ENGINES)
def test_every_case_reproduces_from_the_python_engine(engine, data):
    for case in _cases(engine):
        actual = json.loads(exporter.render([{"x": exporter.run_case(engine, case["input"], data)}]))[0]["x"]
        assert actual == case["expected"], f"{engine}/{case['name']} drifted: re-run export.py and re-check the port"


def test_edge_cases_cover_the_required_shapes():
    names = {engine: {c["name"] for c in _cases(engine)} for engine in exporter.ENGINES}
    assert "edge-empty-offer-set" in names["fcpi"] and "edge-single-offer" in names["fcpi"]
    assert "edge-flat-series" in names["forecast"]
    assert any(n.endswith("-???!!!") for n in names["ask"]) and any(n.endswith("-empty") for n in names["ask"])
    refused = next(c for c in _cases("fcpi") if c["name"] == "edge-mixed-live-and-sample-refused")
    assert refused["expected"]["error"] == "flopx.mixed-provenance"
    empty = next(c for c in _cases("fcpi") if c["name"] == "edge-empty-offer-set")
    assert empty["expected"]["current"] is None and empty["expected"]["coverage"]["offerCount"] == 0
    # The inferred-state engines (architecture §10): an empty cell has coverage 0 and no "measured" label anywhere,
    # supply never counts announced MW, exposure documents only data-backed links, anomalies skip empty cells.
    assert "edge-empty-cell-coverage-zero" in names["pressure"] and "sample-grid-all-cells" in names["pressure"]
    for case in _cases("pressure"):
        cells = case["expected"] if isinstance(case["expected"], list) else [case["expected"]]
        assert all(c["uncertainty"]["label"] in ("exploratory", "indicative") and c["statementClass"] == "inference" for c in cells)
    never = next(c for c in _cases("effective_supply") if c["name"] == "edge-announced-and-reported-never-counted")
    assert never["expected"]["effectiveMw"] == 0 and never["expected"]["operatingMw"] == 0 and never["expected"]["announcedMw"] == 1300
    refused = next(c for c in _cases("effective_supply") if c["name"] == "edge-utilisation-outside-range-refused")
    assert refused["expected"]["error"] == "value-error"
    profile_only = next(c for c in _cases("exposure") if c["name"] == "edge-unknown-region-profile-only")
    assert profile_only["expected"] and all(not e["documented"] for e in profile_only["expected"])
    skipped = next(c for c in _cases("anomalies") if c["name"] == "edge-empty-cell-skipped")
    assert skipped["expected"] == []


def test_export_check_mode_agrees_with_the_committed_files():
    assert exporter.main(["--check"]) == 0
