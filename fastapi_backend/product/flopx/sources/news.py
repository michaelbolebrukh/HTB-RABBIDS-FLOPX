"""News: Hacker News (Algolia) and the investor-relations RSS feeds (research R3, section E).

An IR press release is a primary source, so an RSS item is classified
`verified`; a Hacker News story is commentary and is `interpretation`
(architecture §3's rule). Never Google News RSS (its licence forbids it) and
never GDELT (rate-limited per shared egress). RSS is parsed with the standard
library after refusing any document that declares a DTD or entities.
Timestamps are the upstream's own (`created_at`, `pubDate`).
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime

from product.flopx import domain
from product.flopx.provenance import live
from product.flopx.sources import SourceResult, available, unavailable
from product.flopx.sources import licences
from product.flopx.sources import _http
from product.flopx.sources._mapping import gpu_models_in, tickers_in

logger = logging.getLogger(__name__)

HN = "hacker-news"
HN_ENDPOINT = "https://hn.algolia.com/api/v1/search_by_date"
HN_LICENCE = licences.licence(HN)
HN_QUERY = "GPU data center"
RSS_FEEDS = {
    "nvidia": ("https://nvidianews.nvidia.com/releases.xml", "NVDA", "NVIDIA newsroom"),
    "coreweave": ("https://investors.coreweave.com/rss/pressrelease.aspx", "CRWV", "CoreWeave investor relations"),
    "corescientific": ("https://investors.corescientific.com/news-events/press-releases/rss", "CORZ", "Core Scientific investor relations"),
    "applieddigital": ("https://ir.applieddigital.com/news-events/press-releases/rss", "APLD", "Applied Digital investor relations"),
}
RSS_LICENCE = licences.licence("rss")
CACHE_TTL_SECONDS = 600
EARNINGS = re.compile(r"\b(results|earnings|quarter|fiscal|financial)\b", re.IGNORECASE)
LAUNCH = re.compile(r"\b(launch|launches|unveil|unveils|introduc|announces .* (platform|gpu|chip|system))", re.IGNORECASE)
POWER = re.compile(r"\b(power|energy|megawatt|gigawatt|\bMW\b|\bGW\b|grid)\b", re.IGNORECASE)


def _kind(title: str, *, primary: bool) -> tuple[str, int]:
    if EARNINGS.search(title):
        return "earnings", 3 if primary else 2
    if POWER.search(title):
        return "power-event", 3 if primary else 2
    if LAUNCH.search(title):
        return "launch", 3 if primary else 2
    return "announcement", 2 if primary else 1


def parse_hn(payload: dict, fetched_at) -> list[domain.IntelItem]:
    items = []
    for hit in (payload or {}).get("hits") or []:
        title = str(hit.get("title") or "").strip()
        created = _http.iso(hit.get("created_at")) or _http.epoch(hit.get("created_at_i"))
        if not title or created is None:
            continue
        thread = f"https://news.ycombinator.com/item?id={hit.get('objectID')}"
        url = hit.get("url") or thread
        kind, severity = _kind(title, primary=False)
        evidence = [domain.Evidence(label="article", href=str(url), kind="article"), domain.Evidence(label="discussion", href=thread, kind="article")]
        items.append(domain.IntelItem(
            id=f"hn:{hit.get('objectID')}", kind=kind, headline=title,
            why_it_matters=f"Community attention ({hit.get('points') or 0} points, {hit.get('num_comments') or 0} comments): a signal of what practitioners are discussing, not a primary source.",
            published_at=created, observed_at=created, severity=severity, classification="interpretation",
            entities=domain.IntelEntities(tickers=tickers_in(title), gpu_model_ids=gpu_models_in(title)),
            evidence=evidence, provenance=live(HN, created, fetched_at=fetched_at, licence=HN_LICENCE, source_url=thread, note="Hacker News story; discussion, not a primary source"),
        ))
    return items


def fetch_hn(query: str = HN_QUERY, *, limit: int = 20) -> SourceResult:
    fetched = _http.fetch_json(HN, HN_ENDPOINT, key=f"flopx:hn:{query}", ttl_seconds=CACHE_TTL_SECONDS, params={"query": query, "tags": "story", "hitsPerPage": limit})
    if isinstance(fetched, _http.Failure):
        return unavailable(HN, fetched.reason)
    items = parse_hn(fetched.payload, fetched.fetched_at)
    if not items:
        return unavailable(HN, "empty-response")
    return available(HN, items, live(HN, items[0].published_at, fetched_at=fetched.fetched_at, licence=HN_LICENCE, note=f"search_by_date '{query}'"))


def _text(node, tag: str) -> str:
    child = node.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


def parse_rss(text: str, feed_key: str, fetched_at) -> list[domain.IntelItem]:
    url, ticker, label = RSS_FEEDS[feed_key]
    head = (text or "")[:2048].upper()
    if "<!DOCTYPE" in head or "<!ENTITY" in head:
        logger.warning("%s: RSS declares a DTD or entity; refused", feed_key)
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        logger.warning("%s: RSS did not parse", feed_key)
        return []
    items = []
    for node in root.iter("item"):
        title = _text(node, "title")
        link = _text(node, "link")
        published = _http.rfc822(_text(node, "pubDate")) or _http.iso(_text(node, "pubDate"))
        if not title or published is None:
            continue
        kind, severity = _kind(title, primary=True)
        summary = re.sub(r"<[^>]+>", "", _text(node, "description"))[:280].strip()
        items.append(domain.IntelItem(
            id=f"rss:{feed_key}:{re.sub(r'[^a-z0-9]+', '-', (link or title).lower())[-80:]}", kind=kind, headline=title,
            why_it_matters=summary or f"Primary-source release from {label}.",
            published_at=published, observed_at=published, severity=severity, classification="verified",
            entities=domain.IntelEntities(tickers=sorted({ticker, *tickers_in(title)}), gpu_model_ids=gpu_models_in(title)),
            evidence=[domain.Evidence(label=label, href=link or url, kind="article")],
            provenance=live(f"rss:{feed_key}", published, fetched_at=fetched_at, licence=RSS_LICENCE, source_url=link or url, note=f"{label} RSS; primary source"),
        ))
    items.sort(key=lambda i: i.published_at, reverse=True)
    return items


def fetch_rss(feed_key: str, *, limit: int = 10) -> SourceResult:
    url, _, _ = RSS_FEEDS[feed_key]
    source = f"rss:{feed_key}"
    fetched = _http.fetch_text(source, url, key=f"flopx:rss:{feed_key}", ttl_seconds=CACHE_TTL_SECONDS)
    if isinstance(fetched, _http.Failure):
        return unavailable(source, fetched.reason)
    items = parse_rss(fetched.payload, feed_key, fetched.fetched_at)[:limit]
    if not items:
        return unavailable(source, "empty-or-unparseable-feed")
    return available(source, items, live(source, items[0].published_at, fetched_at=fetched.fetched_at, licence=RSS_LICENCE))


def fetch_all_rss() -> list[SourceResult]:
    return [fetch_rss(key) for key in RSS_FEEDS]
