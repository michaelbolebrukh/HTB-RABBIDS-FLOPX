"""A divergence headline says which series it quotes (re-check R3-02).

The momentum factor reads a regional on-demand series only where one exists; every sample region
falls back to the model's global series. A headline that said "h200 in Japan: index moved +8.0%"
read as a Tokyo price move. Region names state the catchment they hold (R3-05): Iowa and Illinois
campuses sit in the Ashburn-hub region, so its name cannot be "Northern Virginia".
"""

import json
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "engine" / "anomalies.json"
REGIONS = Path(__file__).resolve().parents[1] / "data" / "sample" / "regions.json"
FACILITIES = Path(__file__).resolve().parents[1] / "data" / "sample" / "facilities.json"


def _divergences() -> list[dict]:
    cases = json.loads(FIXTURE.read_text())
    return [a for case in cases for a in case["expected"] if a.get("type") == "divergence"]


def test_a_divergence_on_the_global_series_says_so_in_its_headline():
    rows = _divergences()
    assert rows, "the fixture cases fire no divergence: the guard would pass vacuously"
    for row in rows:
        assert "global" in row["headline"] and "no regional series" in row["headline"], row["headline"]
        assert "index moved" not in row["headline"].split(":", 1)[1].split("global", 1)[0], row["headline"]


def test_region_names_describe_the_catchment_their_facilities_sit_in():
    names = {r["id"]: r["name"] for r in json.loads(REGIONS.read_text())["items"]}
    states = {f["name"].split(", ")[-1].split(" ")[0] for f in json.loads(FACILITIES.read_text())["items"] if f["regionId"] == "us-va"}
    assert {"IA", "IL"} & states, "the Ashburn-hub region no longer holds Midwest campuses; revisit its name"
    assert "Midwest" in names["us-va"] and "Virginia" not in names["us-va"]
    assert "UK" in names["eu-ie"]
