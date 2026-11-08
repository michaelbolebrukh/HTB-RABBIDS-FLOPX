"""The human interface's deterministic query grammar.

A query is tokenised and matched against four lexicons built from the data:
entities (tickers, GPU models, regions, providers), measures (price, index,
supply, forecast, change, facilities, markets, feed, correlation,
procurement), periods (`last 30 days`, `6 months`, `ytd`, `this week`) and
actions (buy, sell, compare, forecast, close). The result is an intent, the
screen route with filters applied as URL search params, and an explanation
listing exactly which tokens matched what, so the routing is inspectable. No
LLM: when a provider key exists a separate layer may upgrade this; the grammar
stays the fallback and the test oracle.

Order and close actions are returned as actions that require confirmation;
the grammar never executes anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlencode

from product.flopx.data.loader import SampleData

GPU_ALIASES = {
    "h100": "h100-sxm-80gb", "h-100": "h100-sxm-80gb", "hopper": "h100-sxm-80gb",
    "h200": "h200-sxm-141gb", "h-200": "h200-sxm-141gb",
    "b200": "b200-sxm-192gb", "b-200": "b200-sxm-192gb", "blackwell": "b200-sxm-192gb",
    "a100": "a100-sxm-80gb", "a-100": "a100-sxm-80gb", "ampere": "a100-sxm-80gb",
    "l40s": "l40s-48gb", "l40": "l40s-48gb",
    "4090": "rtx-4090-24gb", "rtx4090": "rtx-4090-24gb", "rtx-4090": "rtx-4090-24gb",
}
REGION_ALIASES = {
    "texas": "us-tx", "tx": "us-tx", "dallas": "us-tx", "abilene": "us-tx", "ercot": "us-tx",
    "virginia": "us-va", "ashburn": "us-va", "va": "us-va",
    "oregon": "us-or", "or": "us-or", "hillsboro": "us-or",
    "ireland": "eu-ie", "dublin": "eu-ie",
    "germany": "eu-de", "frankfurt": "eu-de",
    "norway": "eu-no", "oslo": "eu-no", "nordics": "eu-no",
    "singapore": "ap-sg", "japan": "ap-jp", "tokyo": "ap-jp",
}
# Country/continent words map to a group of regions:
REGION_GROUPS = {
    "us": ["us-tx", "us-va", "us-or"], "usa": ["us-tx", "us-va", "us-or"], "america": ["us-tx", "us-va", "us-or"],
    "eu": ["eu-ie", "eu-de", "eu-no"], "europe": ["eu-ie", "eu-de", "eu-no"],
    "asia": ["ap-sg", "ap-jp"], "apac": ["ap-sg", "ap-jp"],
}
COMPANY_ALIASES = {
    "nvidia": "NVDA", "coreweave": "CRWV", "nebius": "NBIS", "iren": "IREN", "iris": "IREN",
    "applied": "APLD", "core": "CORZ", "vertiv": "VRT", "supermicro": "SMCI", "smci": "SMCI",
}
MEASURES = {
    "price": ["price", "prices", "cost", "costs", "rate", "rates", "cheap", "cheapest", "expensive", "per", "hour", "hourly"],
    "index": ["index", "fcpi", "benchmark"],
    "supply": ["supply", "offers", "offer", "availability", "available", "capacity", "listings"],
    "change": ["change", "changes", "changed", "moved", "move", "moves", "drop", "dropped", "rise", "rose", "fell", "spike", "trend"],
    "forecast": ["forecast", "forecasts", "predict", "prediction", "projection", "project", "outlook", "expect", "next"],
    "scenario": ["scenario", "scenarios", "if", "assume", "assuming", "elasticity"],
    "facilities": ["facility", "facilities", "datacenter", "datacenters", "data", "centre", "centres", "center", "centers", "site", "sites", "campus", "construction", "building", "built", "map", "where"],
    "markets": ["market", "markets", "odds", "probability", "probabilities", "contract", "contracts", "resolve", "resolves"],
    "feed": ["news", "feed", "announcement", "announcements", "headlines", "events", "happened", "latest"],
    "correlation": ["correlation", "correlate", "correlated", "versus", "vs", "against", "overlay", "xray", "x-ray", "relationship", "lead", "lag", "diverge", "divergence"],
    "fundamentals": ["revenue", "capex", "earnings", "fundamentals", "margin", "guidance"],
    "procure": ["procure", "procurement", "rent", "renting", "run", "job", "workload", "train", "training", "plan", "budget", "gpu-hours", "gpuhours"],
    "strategy": ["strategy", "strategies", "signal", "signals", "backtest", "research"],
    "portfolio": ["portfolio", "positions", "position", "holdings", "pnl", "p&l", "desk", "account"],
    "brief": ["brief", "briefing", "happening", "assess", "assessment", "situation", "sitrep", "summarise", "summarize", "summary", "overview"],
}
# Phrases that ask for an assessment of one entity; the composed brief (assistant.py) answers them:
BRIEF_PHRASES = ("what is happening", "what's happening", "whats happening", "state of", "tell me about", "how is", "how are", "going on")
ACTIONS = {
    "buy": ["buy", "long", "purchase"],
    "sell": ["sell", "short", "dump"],
    "close": ["close", "flatten", "exit", "liquidate"],
    "compare": ["compare", "comparison", "rank", "ranking", "best", "cheapest", "top"],
    "show": ["show", "list", "display", "give", "find", "what", "which", "how"],
}
PERIOD_UNITS = {"d": 1, "day": 1, "days": 1, "w": 7, "week": 7, "weeks": 7, "m": 30, "mo": 30, "month": 30, "months": 30, "y": 365, "yr": 365, "year": 365, "years": 365}
WORD_NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12}


@dataclass
class ParsedQuery:
    query: str
    intent: str
    route: str
    filters: dict = field(default_factory=dict)
    explanation: dict = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)
    engine: str = "grammar"
    brief: object | None = None


def _tokens(query: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9&$.+-]+", query.lower()) if t]


def _period_days(tokens: list[str], query: str) -> tuple[int | None, str | None]:
    text = query.lower()
    match = re.search(r"(\d+)\s*(d|days?|w|weeks?|mo|months?|y|yrs?|years?)\b", text)
    if match:
        return int(match.group(1)) * PERIOD_UNITS[match.group(2)], match.group(0)
    match = re.search(r"\b(one|two|three|four|five|six|seven|eight|nine|ten|twelve)\s+(days?|weeks?|months?|years?)\b", text)
    if match:
        return WORD_NUMBERS[match.group(1)] * PERIOD_UNITS[match.group(2)], match.group(0)
    if "ytd" in tokens or "year-to-date" in text:
        return 310, "ytd"
    if "this week" in text or "past week" in text or "last week" in text:
        return 7, "week"
    if "this month" in text or "past month" in text or "last month" in text:
        return 30, "month"
    if "quarter" in tokens:
        return 90, "quarter"
    if "today" in tokens or "now" in tokens:
        return 1, "today"
    return None, None


def _numbers(tokens: list[str]) -> list[float]:
    found = []
    for t in tokens:
        cleaned = t.replace("$", "").replace(",", "")
        if re.fullmatch(r"\d+(\.\d+)?", cleaned):
            found.append(float(cleaned))
    return found


def _quantities(text: str) -> dict:
    """Workload numbers by their unit words: GPUs, hours, GPU-hours, budget."""
    found: dict = {}
    match = re.search(r"(\d+)\s*(?:x\s*)?(?:gpus?|h100s?|h200s?|b200s?|a100s?|cards?|nodes?)\b", text)
    if match:
        found["gpus"] = int(match.group(1))
    match = re.search(r"(\d+)\s*(?:gpu-?hours?|gpu hrs?)\b", text)
    if match:
        found["gpuHours"] = int(match.group(1))
    elif (match := re.search(r"(?:for\s+)?(\d+)\s*(?:hours?|hrs?|h)\b", text)):
        found["hours"] = int(match.group(1))
    match = re.search(r"budget\s*(?:of\s*)?\$?\s*(\d[\d,]*)|\$\s*(\d[\d,]*)\s*budget|under\s*\$\s*(\d[\d,]*)", text)
    if match:
        found["budget"] = int(next(g for g in match.groups() if g).replace(",", ""))
    return found



LAB_RANGES = {30: "30d", 90: "90d", 180: "180d", 365: "1y"}


def lab_params(series_id: str, overlay: str | None, days: int | None) -> dict:
    """The research page's series lab opened on a series (and one overlay): `tab=lab&series=...`. The lab
    reads its own keys, not the terminal context, so the route carries exactly those; a period becomes the
    lab's range only when it is one the lab offers."""
    params = {"tab": "lab", "series": series_id}
    if overlay:
        params["overlay"] = overlay
    if days in LAB_RANGES:
        params["range"] = LAB_RANGES[days]
    return params

def parse(query: str, data: SampleData) -> ParsedQuery:
    tokens = _tokens(query)
    text = query.lower()
    matched: dict[str, list[str]] = {"tickers": [], "gpuModels": [], "regions": [], "providers": [], "measures": [], "actions": [], "period": []}

    tickers: list[str] = []
    gpu_models: list[str] = []
    regions: list[str] = []
    providers: list[str] = []

    for raw_token in tokens:
        # Plurals ("H100s", "GPUs") match their singular alias:
        token = raw_token[:-1] if raw_token.endswith("s") and raw_token[:-1] in GPU_ALIASES else raw_token
        upper = token.upper().strip("$")
        if upper in data.companies and upper not in tickers:
            tickers.append(upper)
            matched["tickers"].append(token)
        if token in COMPANY_ALIASES and COMPANY_ALIASES[token] not in tickers:
            tickers.append(COMPANY_ALIASES[token])
            matched["tickers"].append(token)
        if token in GPU_ALIASES and GPU_ALIASES[token] not in gpu_models:
            gpu_models.append(GPU_ALIASES[token])
            matched["gpuModels"].append(token)
        if token in data.gpu_models and token not in gpu_models:
            gpu_models.append(token)
            matched["gpuModels"].append(token)
        if token in REGION_ALIASES and REGION_ALIASES[token] not in regions:
            regions.append(REGION_ALIASES[token])
            matched["regions"].append(token)
        if token in REGION_GROUPS:
            for rid in REGION_GROUPS[token]:
                if rid not in regions:
                    regions.append(rid)
            matched["regions"].append(token)
        if token in data.regions and token not in regions:
            regions.append(token)
            matched["regions"].append(token)
    for provider in data.providers.values():
        first = provider.name.lower().split()[0]
        if provider.id in text or first in tokens:
            providers.append(provider.id)
            matched["providers"].append(first)

    measures = [m for m, words in MEASURES.items() if any(w in tokens for w in words)]
    actions = [a for a, words in ACTIONS.items() if any(w in tokens for w in words)]
    matched["measures"] = measures
    matched["actions"] = actions
    days, period_token = _period_days(tokens, query)
    if period_token:
        matched["period"] = [period_token]
    numbers = _numbers(tokens)
    quantities = _quantities(text)

    filters: dict = {}
    if tickers:
        filters["t"] = tickers[0]
        if len(tickers) > 1:
            filters["peers"] = ",".join(tickers[1:])
    if gpu_models:
        filters["g"] = gpu_models[0]
    if regions:
        filters["r"] = regions[0] if len(regions) == 1 else ",".join(regions)
    if providers:
        filters["provider"] = providers[0]
    if days:
        filters["range"] = f"{days}d"

    # -- intent resolution, most specific first -------------------------------
    intent = "overview"
    route = "/"
    result_actions: list[dict] = []
    trade = next((a for a in actions if a in ("buy", "sell", "close")), None)

    wants_brief = "brief" in measures or any(phrase in text for phrase in BRIEF_PHRASES)
    if trade and tickers:
        intent = f"paper-{trade}"
        quantity = next((n for n in numbers if n == int(n) and n > 0), None)
        filters["side"] = trade
        if quantity:
            filters["qty"] = int(quantity)
        route = "/desk?" + urlencode(filters)
        result_actions.append({"kind": f"paper-{trade}", "route": route, "requiresConfirmation": True})
    elif wants_brief and (tickers or regions or gpu_models):
        intent = "brief"
        if tickers:
            filters["target"] = f"ticker:{tickers[0]}"
        elif regions:
            filters["target"] = f"region:{regions[0]}"
        else:
            filters["target"] = f"gpu:{gpu_models[0]}"
        route = "/ask?" + urlencode({"q": query})
    elif "procure" in measures or quantities or ("price" in measures and numbers and ("gpu-hours" in tokens or "gpus" in tokens or "hours" in tokens)):
        intent = "procure"
        filters.update(quantities)
        route = "/procure?" + urlencode(filters)
    elif "forecast" in measures or "scenario" in measures:
        intent = "forecast" if "forecast" in measures else "scenario"
        target = (f"equity:{tickers[0]}:close" if tickers else f"gpu:{gpu_models[0]}:on-demand:global" if gpu_models else "index:fcpi:h100-sxm-80gb")
        filters["target"] = target
        filters["mode"] = intent
        # The series lab (its forecast and scenario panels) lives on the research page's lab tab, keyed `series`.
        route = "/research?" + urlencode(lab_params(target, None, days))
    elif "correlation" in measures and (tickers or gpu_models):
        intent = "xray"
        target = f"equity:{tickers[0]}:close" if tickers else f"gpu:{gpu_models[0]}:on-demand:global"
        overlays = []
        if tickers and gpu_models:
            overlays.append(f"gpu:{gpu_models[0]}:on-demand:global")
        for extra in tickers[1:]:
            overlays.append(f"equity:{extra}:close")
        filters["target"] = target
        if overlays:
            filters["overlay"] = ",".join(overlays)
        route = "/research?" + urlencode(lab_params(target, overlays[0] if overlays else None, days))
    elif "markets" in measures:
        intent = "markets"
        route = "/markets" + ("?" + urlencode(filters) if filters else "")
    elif "facilities" in measures:
        intent = "map"
        route = "/map" + ("?" + urlencode(filters) if filters else "")
    elif "feed" in measures:
        intent = "feed"
        route = "/feed" + ("?" + urlencode(filters) if filters else "")
    elif "strategy" in measures:
        intent = "research"
        route = "/research" + ("?" + urlencode(filters) if filters else "")
    elif "portfolio" in measures:
        intent = "desk"
        route = "/desk"
    elif tickers and ("fundamentals" in measures or not gpu_models):
        intent = "company"
        route = f"/companies/{tickers[0]}" + ("?" + urlencode({k: v for k, v in filters.items() if k != 't'}) if len(filters) > 1 else "")
    elif gpu_models or "price" in measures or "index" in measures or "supply" in measures or "change" in measures:
        intent = "gpu"
        if "index" in measures:
            filters["view"] = "index"
        elif "change" in measures:
            filters["view"] = "changes"
        elif "compare" in actions or "cheapest" in tokens:
            filters["view"] = "offers"
            filters["sort"] = "adjusted"
        route = ("/gpu/" + gpu_models[0] if gpu_models and len(filters) <= 2 and "view" not in filters else "/gpu")
        query_filters = {k: v for k, v in filters.items() if not (route.startswith("/gpu/") and k == "g")}
        if query_filters:
            route += "?" + urlencode(query_filters)
    elif regions:
        intent = "map"
        route = "/map?" + urlencode(filters)

    result_actions.append({"kind": "open", "route": route, "requiresConfirmation": False})
    explanation = {
        "matched": {k: v for k, v in matched.items() if v},
        "unmatchedTokens": [t for t in tokens if not any(t in v for v in matched.values()) and t not in sum(ACTIONS.values(), []) and t not in sum(MEASURES.values(), [])],
        "numbers": numbers,
        "periodDays": days,
    }
    return ParsedQuery(query=query, intent=intent, route=route, filters=filters, explanation=explanation, actions=result_actions)
