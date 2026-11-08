"""Strategy backtests say what they trade and how much evidence a hit rate rests on (falsifier V2, F15 to F17)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from product.flopx import domain, strategies
from product.flopx.data.loader import load_sample_uncached
from product.flopx.provenance import sample

T0 = datetime(2026, 1, 1, 21, tzinfo=timezone.utc)


def _series(sid: str, values: list[float]) -> domain.Series:
    return domain.Series(id=sid, unit="USD", points=[domain.PriceObservation(series_id=sid, t=T0 + timedelta(days=i), value=v, unit="USD",
                                                                              provenance=sample(T0 + timedelta(days=i))) for i, v in enumerate(values)])


def _item(day: int) -> domain.IntelItem:
    at = T0 + timedelta(days=day)
    return domain.IntelItem(id=f"evt-{day}", kind="announcement", headline="h", why_it_matters="", published_at=at, observed_at=at, severity=4,
                            classification="verified", provenance=sample(at), entities=domain.IntelEntities(tickers=["CRWV"]))


def test_round_trips_count_closed_pairs_not_fills():
    # 80 daily dates; the backtest starts after a 61-day warm-up, so it decides on days 61 to 79.
    # A CRWV announcement on day 62 is bought that day at close 162 and sold 10 days later, on day 72, at 172: one
    # closed round trip, a winner. A second announcement on day 74 is bought and still held at the end.
    # So three fills (buy, sell, buy), one round trip, a hit rate of 1.0 resting on n = 1.
    data = SimpleNamespace(series={strategies.INDEX_SERIES: _series(strategies.INDEX_SERIES, [2.0] * 80),
                                   "equity:CRWV:close": _series("equity:CRWV:close", [100.0 + i for i in range(80)])},
                           feed=[_item(62), _item(74)])
    bt = strategies.backtest(strategies.STRATEGIES["event-reaction"], data)
    assert bt.trades == 3 and bt.round_trips == 1 and bt.hit_rate == 1.0
    assert bt.benchmark_ticker == "NVDA"      # the first universe name, whose buy-and-hold benchmark_return_pct is


def test_every_strategy_trades_only_what_its_rule_names():
    data = load_sample_uncached()
    traded_names = {"divergence": {"CRWV"}, "supply-regime": {"IREN"}}
    for spec in strategies.STRATEGIES.values():
        for variant in strategies.VARIANTS:
            bt = strategies.backtest(spec, data, variant=variant)
            # Every decision, not only the ten kept on the summary:
            acted = set()
            index = data.series[strategies.INDEX_SERIES]
            holding: dict = {}
            for at in [p.t for p in index.points][61:]:
                d = strategies.decide(spec, data, at, holding=holding, variant=variant)
                if d.action == "buy" and d.ticker and d.ticker not in holding:
                    holding[d.ticker] = at
                    acted.add(d.ticker)
                elif d.action == "sell" and d.ticker in holding:
                    holding.pop(d.ticker)
                    acted.add(d.ticker)
            assert acted <= set(spec.universe), (spec.id, variant, acted)
            if spec.id in traded_names:
                assert acted <= traded_names[spec.id], (spec.id, variant, acted)
                for name in traded_names[spec.id]:
                    assert f"buy {name}" in spec.rule and f"sell {name}" in spec.rule
                assert bt.benchmark_ticker in traded_names[spec.id]
            if spec.id == "divergence":
                assert acted == {"CRWV"}, (variant, acted)       # it does trade, and only CRWV: NBIS sits in the spread only
    assert "basket" not in strategies.STRATEGIES["divergence"].rule
    assert "trading days" not in strategies.STRATEGIES["event-reaction"].rule      # the hold is counted in calendar days


def test_the_strategy_page_and_the_portfolio_evaluation_state_one_result_per_strategy():
    # Falsifier V4: the divergence page once read 35.0% at $0 costs in one panel and 34.8% at $182 in the
    # other for the same window. Both now come from one costed run, so every reported figure is equal.
    from product import schemas
    from product.flopx import terminal

    portfolio = schemas.PortfolioEvaluationResponse.model_validate(terminal.evaluation_portfolio(None)).model_dump(by_alias=False)
    compute_rows = {row["strategy_id"]: row for row in portfolio["strategies"] if row["variant"] == "compute"}
    for strategy_id in strategies.STRATEGIES:
        view = schemas.StrategyView.model_validate(terminal.strategy_view(None, strategy_id, None)).model_dump(by_alias=False)
        bt, row = view["backtest"], compute_rows[strategy_id]
        for key in ("start", "end", "trades", "round_trips", "total_return_pct", "benchmark_return_pct", "benchmark_ticker", "max_drawdown_pct",
                    "hit_rate", "turnover", "turnover_ratio", "cost_bps", "costs_usd"):
            assert bt[key] == row[key], (strategy_id, key, bt[key], row[key])
        assert bt["cost_bps"] == 5.0 and bt["variant"] == "compute"
    divergence = compute_rows["divergence"]
    assert divergence["costs_usd"] > 0 and divergence["benchmark_ticker"] == "CRWV"
