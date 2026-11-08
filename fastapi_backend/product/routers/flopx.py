"""The FLOPX API (architecture §4), mounted under /flopx by product.routers.

Reads are public and rate-limited like platform routers; writes need the
signed-in user. Reads that merge live sources take the session too, only so
the archive can append what the live fetch observed (terminal.py, archive.py). Routers only translate: query params in, service call,
response model out, domain refusals mapped to HTTP by product.exceptions.
"""

import uuid
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Query
from starlette.requests import Request
from starlette.responses import Response

from infrastructure.dependencies import db_dependency, optional_user_dependency, user_dependency
from infrastructure.rate_limiter import limiter
from product import schemas
from product import services as svc
from product.exceptions import FlopxDomainError, to_http

router = APIRouter(tags=["FLOPX"])

READ_LIMIT = "120/minute"
WRITE_LIMIT = "30/minute"


def _guard(call, *args, **kwargs):
    try:
        return call(*args, **kwargs)
    except FlopxDomainError as exc:
        raise to_http(exc) from exc
    except ValueError as exc:
        raise to_http(FlopxDomainError("flopx.invalid-parameter", message=str(exc))) from exc


# -- overview ----------------------------------------------------------------------

@router.get("/overview", response_model=schemas.OverviewResponse)
@limiter.limit(READ_LIMIT)
def get_overview(db: db_dependency, user: optional_user_dependency, request: Request, response: Response):
    return _guard(svc.overview, db, user)


# -- gpu market -----------------------------------------------------------------------

@router.get("/gpu/offers", response_model=schemas.OffersResponse)
@limiter.limit(READ_LIMIT)
def get_gpu_offers(
    db: db_dependency, request: Request, response: Response,
    gpu: Optional[str] = None, region: Optional[str] = None, provider: Optional[str] = None,
    contract: Optional[str] = None, sort: Optional[str] = None,
):
    return _guard(svc.offers, db, gpu, region, provider, contract, sort)


@router.get("/gpu/heatmap", response_model=schemas.HeatmapResponse)
@limiter.limit(READ_LIMIT)
def get_gpu_heatmap(db: db_dependency, request: Request, response: Response, measure: str = "median-adjusted-price", axis: str = Query("region", pattern="^(region|provider)$")):
    return _guard(svc.heatmap, db, measure, axis)


@router.get("/gpu/series", response_model=schemas.SeriesResponse)
@limiter.limit(READ_LIMIT)
def get_gpu_series(db: db_dependency, user: optional_user_dependency, request: Request, response: Response, id: str, range: Optional[str] = None):
    # The optional user only widens the view: a signed-in account also receives the points of a
    # `research-archive` source (the WRDS CRSP closes, decision G13); anonymous readers get the public series.
    return _guard(svc.series, db, id, range, user)


@router.get("/gpu/index", response_model=schemas.IndexListResponse)
@limiter.limit(READ_LIMIT)
def get_gpu_indices(db: db_dependency, request: Request, response: Response):
    return _guard(svc.indices, db)


@router.get("/gpu/index/{gpu_model_id}", response_model=schemas.FcpiIndex)
@limiter.limit(READ_LIMIT)
def get_gpu_index(db: db_dependency, request: Request, response: Response, gpu_model_id: str):
    return _guard(svc.index, db, gpu_model_id)


@router.get("/gpu/changes", response_model=schemas.ChangesResponse)
@limiter.limit(READ_LIMIT)
def get_gpu_changes(db: db_dependency, request: Request, response: Response, since: Optional[datetime] = None):
    return _guard(svc.changes, db, since)


# -- map ------------------------------------------------------------------------------

@router.get("/map/facilities", response_model=schemas.FacilitiesResponse)
@limiter.limit(READ_LIMIT)
def get_facilities(request: Request, response: Response, status: Optional[str] = None, region: Optional[str] = None):
    return _guard(svc.facilities, status, region)


@router.get("/map/regions", response_model=schemas.RegionsResponse)
@limiter.limit(READ_LIMIT)
def get_regions(request: Request, response: Response):
    return _guard(svc.regions)


# -- companies -------------------------------------------------------------------------

@router.get("/companies", response_model=schemas.CompaniesResponse)
@limiter.limit(READ_LIMIT)
def get_companies(db: db_dependency, request: Request, response: Response):
    return _guard(svc.companies, db)


@router.get("/companies/{ticker}", response_model=schemas.CompanyDetail)
@limiter.limit(READ_LIMIT)
def get_company(db: db_dependency, user: optional_user_dependency, request: Request, response: Response, ticker: str):
    return _guard(svc.company, db, ticker, user)


@router.get("/companies/{ticker}/bars", response_model=schemas.BarsResponse)
@limiter.limit(READ_LIMIT)
def get_company_bars(db: db_dependency, request: Request, response: Response, ticker: str, range: str = "6mo", interval: str = "1d"):
    return _guard(svc.bars, db, ticker, range, interval)


# -- markets and feed -------------------------------------------------------------------

@router.get("/markets", response_model=schemas.MarketsResponse)
@limiter.limit(READ_LIMIT)
def get_markets(db: db_dependency, request: Request, response: Response):
    return _guard(svc.markets, db)


@router.get("/markets/{market_id}", response_model=schemas.PredictionMarket)
@limiter.limit(READ_LIMIT)
def get_market(db: db_dependency, request: Request, response: Response, market_id: str):
    return _guard(svc.market, db, market_id)


@router.get("/feed", response_model=schemas.FeedResponse)
@limiter.limit(READ_LIMIT)
def get_feed(
    db: db_dependency, request: Request, response: Response,
    topic: Optional[str] = None, ticker: Optional[str] = None, region: Optional[str] = None,
    severity: Optional[int] = Query(None, ge=1, le=5), since: Optional[datetime] = None,
):
    return _guard(svc.feed, db, topic, ticker, region, severity, since)


# -- lab ------------------------------------------------------------------------------------

@router.get("/xray", response_model=schemas.XrayResponse)
@limiter.limit(READ_LIMIT)
def get_xray(db: db_dependency, request: Request, response: Response, target: str, overlay: Optional[str] = None, range: Optional[str] = None):
    return _guard(svc.xray, db, target, overlay, range)


@router.post("/forecast", response_model=schemas.ForecastResponse)
@limiter.limit(READ_LIMIT)
def post_forecast(db: db_dependency, request: Request, response: Response, payload: schemas.ForecastRequest):
    return _guard(svc.forecast, db, payload.series_id, payload.horizon, payload.level)


@router.post("/scenario", response_model=schemas.ScenarioResponse)
@limiter.limit(READ_LIMIT)
def post_scenario(db: db_dependency, request: Request, response: Response, payload: schemas.ScenarioRequest):
    return _guard(svc.scenario, db, payload.target, payload.assumptions, payload.horizon)


@router.get("/research/factors", response_model=schemas.FactorsResponse)
@limiter.limit(READ_LIMIT)
def get_factors(request: Request, response: Response):
    return _guard(svc.factors)


@router.get("/research/strategies", response_model=schemas.StrategiesResponse)
@limiter.limit(READ_LIMIT)
def get_strategies(db: db_dependency, user: optional_user_dependency, request: Request, response: Response):
    return _guard(svc.strategies_view, db, user)


@router.get("/research/strategies/{strategy_id}", response_model=schemas.StrategyView)
@limiter.limit(READ_LIMIT)
def get_strategy(db: db_dependency, user: optional_user_dependency, request: Request, response: Response, strategy_id: str):
    return _guard(svc.strategy_view, db, strategy_id, user)


@router.post("/research/strategies/{strategy_id}/step", response_model=schemas.StrategyStepResponse)
@limiter.limit(WRITE_LIMIT)
def post_strategy_step(db: db_dependency, user: user_dependency, request: Request, response: Response, strategy_id: str,
                       payload: Optional[schemas.StrategyStepRequest] = None):
    return _guard(svc.strategy_step, db, strategy_id, user, payload.as_of if payload else None)


# -- paper desk (auth) -------------------------------------------------------------------------

@router.get("/paper/account", response_model=schemas.PaperAccountOut)
@limiter.limit(READ_LIMIT)
def get_paper_account(db: db_dependency, user: user_dependency, request: Request, response: Response):
    return _guard(svc.paper_account, db, user)


@router.post("/paper/orders", response_model=schemas.PaperOrderOut)
@limiter.limit(WRITE_LIMIT)
def post_paper_order(db: db_dependency, user: user_dependency, request: Request, response: Response, payload: schemas.PaperOrderRequest):
    return _guard(svc.paper_place_order, db, user, payload)


@router.get("/paper/orders", response_model=schemas.PaperOrdersResponse)
@limiter.limit(READ_LIMIT)
def get_paper_orders(db: db_dependency, user: user_dependency, request: Request, response: Response):
    return _guard(svc.paper_orders, db, user)


@router.post("/paper/orders/{order_id}/cancel", response_model=schemas.PaperOrderOut)
@limiter.limit(WRITE_LIMIT)
def post_paper_cancel(db: db_dependency, user: user_dependency, request: Request, response: Response, order_id: uuid.UUID):
    return _guard(svc.paper_cancel, db, user, order_id)


@router.post("/paper/positions/{ticker}/close", response_model=schemas.PaperOrderOut)
@limiter.limit(WRITE_LIMIT)
def post_paper_close(db: db_dependency, user: user_dependency, request: Request, response: Response, ticker: str):
    return _guard(svc.paper_close, db, user, ticker)


@router.post("/paper/positions/close-all", response_model=schemas.CloseAllResponse)
@limiter.limit(WRITE_LIMIT)
def post_paper_close_all(db: db_dependency, user: user_dependency, request: Request, response: Response, payload: schemas.CloseAllRequest):
    return _guard(svc.paper_close_all, db, user, payload.confirm)


# -- procurement and ask -------------------------------------------------------------------------

@router.post("/procure/plan", response_model=schemas.ProcurePlanResponse)
@limiter.limit(READ_LIMIT)
def post_procure_plan(db: db_dependency, request: Request, response: Response, payload: schemas.ProcurePlanRequest):
    from product.flopx import domain
    workload = domain.Workload(**payload.workload.model_dump(by_alias=False))
    return _guard(svc.procure_plan, db, workload)


@router.post("/procure/workloads", status_code=201)
@limiter.limit(WRITE_LIMIT)
def post_save_workload(db: db_dependency, user: user_dependency, request: Request, response: Response, payload: schemas.ProcurePlanRequest):
    row = _guard(svc.save_workload, db, user, payload.workload.model_dump(by_alias=True, mode="json"))
    return {"id": row.id, "createdAt": row.created_at}


@router.post("/ask", response_model=schemas.AskResponse)
@limiter.limit(READ_LIMIT)
def post_ask(db: db_dependency, request: Request, response: Response, payload: schemas.AskRequest):
    return _guard(svc.ask, payload.query, db)


# -- inferred state (architecture §10): pressure, supply, anomalies, exposure, scenarios -----------------

@router.get("/pressure", response_model=schemas.PressureGridResponse)
@limiter.limit(READ_LIMIT)
def get_pressure(db: db_dependency, request: Request, response: Response, gpu: Optional[str] = None, region: Optional[str] = None):
    return _guard(svc.pressure_grid, db, gpu, region)


@router.get("/pressure/{region}/{gpu}", response_model=schemas.PressureCellResponse)
@limiter.limit(READ_LIMIT)
def get_pressure_cell(db: db_dependency, request: Request, response: Response, region: str, gpu: str):
    return _guard(svc.pressure_cell, db, region, gpu)


@router.get("/supply", response_model=schemas.EffectiveSupplyResponse)
@limiter.limit(READ_LIMIT)
def get_effective_supply(db: db_dependency, request: Request, response: Response, region: Optional[str] = None):
    return _guard(svc.effective_supply_view, db, region)


@router.get("/anomalies", response_model=schemas.AnomaliesResponse)
@limiter.limit(READ_LIMIT)
def get_anomalies(db: db_dependency, request: Request, response: Response, region: Optional[str] = None, gpu: Optional[str] = None, kind: Optional[str] = None):
    return _guard(svc.anomalies_view, db, region, gpu, kind)


@router.get("/anomalies/{anomaly_id:path}", response_model=schemas.AnomalyDetailResponse)
@limiter.limit(READ_LIMIT)
def get_anomaly(db: db_dependency, request: Request, response: Response, anomaly_id: str):
    return _guard(svc.anomaly_detail, db, anomaly_id)


@router.post("/scenario/infra", response_model=schemas.InfraScenarioResponse)
@limiter.limit(READ_LIMIT)
def post_scenario_infra(db: db_dependency, request: Request, response: Response, payload: schemas.InfraScenarioRequest):
    return _guard(svc.scenario_infra, db, payload)


@router.get("/exposure", response_model=schemas.ExposureResponse)
@limiter.limit(READ_LIMIT)
def get_exposure(db: db_dependency, user: optional_user_dependency, request: Request, response: Response, region: str, gpu: Optional[str] = None):
    return _guard(svc.exposure_view_for, db, user, region, gpu)


# -- research: evaluation, signals, the decision ledger ----------------------------------------------------

@router.get("/research/evaluation", response_model=schemas.EvaluationResponse)
@limiter.limit(READ_LIMIT)
def get_evaluation(db: db_dependency, request: Request, response: Response, target: Optional[str] = None, horizon: Optional[str] = None):
    return _guard(svc.evaluation_view, db, target, horizon)


@router.get("/research/evaluation/portfolio", response_model=schemas.PortfolioEvaluationResponse)
@limiter.limit(READ_LIMIT)
def get_evaluation_portfolio(db: db_dependency, request: Request, response: Response):
    return _guard(svc.evaluation_portfolio, db)


@router.get("/research/signals", response_model=schemas.SignalsResponse)
@limiter.limit(READ_LIMIT)
def get_signals(db: db_dependency, user: optional_user_dependency, request: Request, response: Response, kind: Optional[str] = None):
    return _guard(svc.signals_view, db, user, kind)


@router.get("/research/signals/{signal_id:path}", response_model=schemas.SignalDetailResponse)
@limiter.limit(READ_LIMIT)
def get_signal(db: db_dependency, user: optional_user_dependency, request: Request, response: Response, signal_id: str):
    return _guard(svc.signal_detail, db, user, signal_id)


@router.post("/research/strategies/{strategy_id}/decisions/{decision_id}/accept", response_model=schemas.AcceptDecisionResponse)
@limiter.limit(WRITE_LIMIT)
def post_decision_accept(db: db_dependency, user: user_dependency, request: Request, response: Response, strategy_id: str, decision_id: int):
    return _guard(svc.decision_accept, db, strategy_id, decision_id, user)


# -- fund (auth) and infrastructure reads ---------------------------------------------------------------------

@router.get("/fund/summary", response_model=schemas.FundSummaryResponse)
@limiter.limit(READ_LIMIT)
def get_fund_summary(db: db_dependency, user: user_dependency, request: Request, response: Response):
    return _guard(svc.fund_summary, db, user)


@router.get("/infra/energy", response_model=schemas.InfraEnergyResponse)
@limiter.limit(READ_LIMIT)
def get_infra_energy(db: db_dependency, request: Request, response: Response):
    return _guard(svc.infra_energy, db)


@router.get("/infra/projects", response_model=schemas.InfraProjectsResponse)
@limiter.limit(READ_LIMIT)
def get_infra_projects(request: Request, response: Response, status: Optional[str] = None, region: Optional[str] = None):
    return _guard(svc.infra_projects, status, region)


@router.get("/infra/grid", response_model=schemas.InfraGridResponse)
@limiter.limit(READ_LIMIT)
def get_infra_grid(db: db_dependency, request: Request, response: Response):
    return _guard(svc.infra_grid, db)
