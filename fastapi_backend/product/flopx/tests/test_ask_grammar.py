"""The deterministic query grammar on six representative questions (one per route family).

The founder's own six example questions are not in the repository (the brief's
intake record paraphrases R13 only), so these six are authored to cover the
grammar: GPU offers, X-Ray, paper order, forecast, map, procurement.
"""

from urllib.parse import parse_qs, urlparse

import pytest

from product.flopx import ask
from product.flopx.data.loader import load_sample_uncached


@pytest.fixture(scope="module")
def data():
    return load_sample_uncached()


def _route(parsed):
    url = urlparse(parsed.route)
    return url.path, {k: v[0] for k, v in parse_qs(url.query).items()}


def test_cheapest_gpu_in_a_region_routes_to_offers_sorted_by_comparable_price(data):
    parsed = ask.parse("cheapest H100 in Texas right now", data)
    path, q = _route(parsed)
    assert parsed.intent == "gpu" and path == "/gpu"
    assert q["g"] == "h100-sxm-80gb" and q["r"] == "us-tx" and q["sort"] == "adjusted" and q["view"] == "offers"
    assert parsed.explanation["matched"]["gpuModels"] == ["h100"]
    assert parsed.explanation["matched"]["regions"] == ["texas"]


def test_compare_ticker_against_index_over_a_period_routes_to_the_series_lab(data):
    # The series lab is the research page's lab tab (V4 F20: /xray is the cell X-Ray and ignores a series target).
    parsed = ask.parse("compare NVDA against the H100 index over the last 90 days", data)
    path, q = _route(parsed)
    assert parsed.intent == "xray" and path == "/research"
    assert q == {"tab": "lab", "series": "equity:NVDA:close", "overlay": "gpu:h100-sxm-80gb:on-demand:global", "range": "90d"}
    assert parsed.filters["target"] == "equity:NVDA:close" and parsed.explanation["periodDays"] == 90


def test_buy_with_a_quantity_routes_to_the_desk_and_requires_confirmation(data):
    parsed = ask.parse("buy 50 CRWV", data)
    path, q = _route(parsed)
    assert parsed.intent == "paper-buy" and path == "/desk"
    assert q == {"t": "CRWV", "side": "buy", "qty": "50"}
    confirmations = [a for a in parsed.actions if a["kind"] == "paper-buy"]
    assert confirmations and confirmations[0]["requiresConfirmation"] is True
    assert parsed.engine == "grammar"


def test_forecast_question_routes_to_the_lab_with_the_series_target(data):
    parsed = ask.parse("forecast H200 prices next 30 days", data)
    path, q = _route(parsed)
    assert parsed.intent == "forecast" and path == "/research"
    assert q == {"tab": "lab", "series": "gpu:h200-sxm-141gb:on-demand:global", "range": "30d"}
    assert parsed.filters["mode"] == "forecast"


def test_risk_scenarios_for_a_ticker_open_the_lab_on_its_close(data):
    parsed = ask.parse("risk scenarios for CRWV", data)
    path, q = _route(parsed)
    assert parsed.intent == "scenario" and path == "/research"
    assert q == {"tab": "lab", "series": "equity:CRWV:close"}


def test_facilities_question_routes_to_the_map_filtered_by_region(data):
    parsed = ask.parse("which data centres are under construction in Norway", data)
    path, q = _route(parsed)
    assert parsed.intent == "map" and path == "/map" and q["r"] == "eu-no"


def test_workload_question_routes_to_procurement_with_gpus_hours_and_budget(data):
    parsed = ask.parse("I need 64 H100s for 48 hours, budget 20000", data)
    path, q = _route(parsed)
    assert parsed.intent == "procure" and path == "/procure"
    assert q["g"] == "h100-sxm-80gb" and q["gpus"] == "64" and q["hours"] == "48" and q["budget"] == "20000"


def test_unknown_words_fall_back_to_the_overview_and_are_reported(data):
    parsed = ask.parse("hello there", data)
    assert parsed.intent == "overview" and parsed.route == "/"
    assert set(parsed.explanation["unmatchedTokens"]) == {"hello", "there"}


def test_company_name_aliases_and_news_route_to_the_feed(data):
    parsed = ask.parse("show the latest news about CoreWeave", data)
    path, q = _route(parsed)
    assert parsed.intent == "feed" and path == "/feed" and q["t"] == "CRWV"
