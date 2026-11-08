"""Upstream vocabularies mapped onto FLOPX entities, conservatively.

A live record only lands on a FLOPX GPU model when the upstream's name is an
exact variant of one of the six models (an H100 PCIe is not an H100 SXM and is
dropped rather than mislabelled). Geographies map to one of the eight sample
regions when the country or US state matches, and otherwise to a lower-case
country code (`gb`, `tw`) that the adjustment treats as an unknown cost level
(factor 1.0) and the heatmap leaves out of its region axis. Tickers are inferred
from text only through an explicit keyword table.
"""

from __future__ import annotations

import re

# upstream GPU name (upper-cased, spaces collapsed) -> FLOPX gpu model id
GPU_NAMES = {
    "H100 SXM": "h100-sxm-80gb", "H100 SXM5": "h100-sxm-80gb", "H100 80GB HBM3": "h100-sxm-80gb", "NVIDIA H100 80GB HBM3": "h100-sxm-80gb",
    "H200": "h200-sxm-141gb", "H200 SXM": "h200-sxm-141gb", "NVIDIA H200": "h200-sxm-141gb",
    "B200": "b200-sxm-192gb", "NVIDIA B200": "b200-sxm-192gb",
    "A100 SXM4": "a100-sxm-80gb", "A100 SXM": "a100-sxm-80gb", "NVIDIA A100-SXM4-80GB": "a100-sxm-80gb",
    "L40S": "l40s-48gb", "NVIDIA L40S": "l40s-48gb",
    "RTX 4090": "rtx-4090-24gb", "NVIDIA GEFORCE RTX 4090": "rtx-4090-24gb", "RTX4090": "rtx-4090-24gb",
}
# Minimum memory (GB) a mapped variant must carry, so a 40 GB A100 never lands on the 80 GB model:
GPU_MIN_MEMORY_GB = {"h100-sxm-80gb": 79, "h200-sxm-141gb": 140, "b200-sxm-192gb": 170, "a100-sxm-80gb": 79, "l40s-48gb": 44, "rtx-4090-24gb": 22}

US_STATE_REGIONS = {"texas": "us-tx", "tx": "us-tx", "virginia": "us-va", "va": "us-va", "oregon": "us-or", "or": "us-or"}
COUNTRY_REGIONS = {"IE": "eu-ie", "DE": "eu-de", "NO": "eu-no", "SG": "ap-sg", "JP": "ap-jp"}
COUNTRY_NAMES = {
    "united states": "US", "united states of america": "US", "usa": "US", "united kingdom": "GB", "germany": "DE", "ireland": "IE", "norway": "NO",
    "singapore": "SG", "japan": "JP", "france": "FR", "netherlands": "NL", "sweden": "SE", "finland": "FI", "denmark": "DK", "switzerland": "CH",
    "canada": "CA", "australia": "AU", "india": "IN", "china": "CN", "taiwan": "TW", "south korea": "KR", "brazil": "BR", "spain": "ES", "italy": "IT",
    "belgium": "BE", "austria": "AT", "poland": "PL", "czech republic": "CZ", "czechia": "CZ", "hungary": "HU", "iceland": "IS", "israel": "IL",
    "united arab emirates": "AE", "russia": "RU", "luxembourg": "LU", "portugal": "PT", "new zealand": "NZ", "mexico": "MX", "south africa": "ZA",
    "hong kong": "HK", "indonesia": "ID", "malaysia": "MY", "chile": "CL", "slovenia": "SI", "bulgaria": "BG", "estonia": "EE",
}
TICKER_KEYWORDS = {
    "NVDA": ("nvidia", "nvda"), "CRWV": ("coreweave", "crwv"), "NBIS": ("nebius", "nbis"), "IREN": ("iren ", "iris energy"),
    "APLD": ("applied digital", "apld"), "CORZ": ("core scientific", "corz"), "VRT": ("vertiv",), "SMCI": ("supermicro", "super micro"),
}
GPU_KEYWORDS = {"h100": "h100-sxm-80gb", "h200": "h200-sxm-141gb", "b200": "b200-sxm-192gb", "a100": "a100-sxm-80gb", "l40s": "l40s-48gb", "4090": "rtx-4090-24gb"}


def gpu_model_for(name: str | None, memory_gb: float | None = None) -> str | None:
    """The FLOPX model id for an upstream GPU name, or None when it is not an exact variant."""
    if not name:
        return None
    key = re.sub(r"\s+", " ", str(name)).strip().upper()
    model = GPU_NAMES.get(key)
    if model is None:
        return None
    if memory_gb is not None and memory_gb < GPU_MIN_MEMORY_GB[model]:
        return None
    return model


def region_for(country_code: str | None, state: str | None = None) -> str:
    """A sample region id when the geography matches one, else the lower-case country code, else `unknown`."""
    cc = (country_code or "").strip().upper()
    if cc == "US" and state:
        hit = US_STATE_REGIONS.get(state.strip().lower())
        if hit:
            return hit
    if cc in COUNTRY_REGIONS:
        return COUNTRY_REGIONS[cc]
    return cc.lower() if len(cc) == 2 else "unknown"


def country_code_for(country_name: str | None) -> str | None:
    if not country_name:
        return None
    return COUNTRY_NAMES.get(country_name.strip().lower())


def tickers_in(text: str | None) -> list[str]:
    lowered = f" {(text or '').lower()} "
    return [ticker for ticker, words in TICKER_KEYWORDS.items() if any(w in lowered for w in words)]


def gpu_models_in(text: str | None) -> list[str]:
    lowered = (text or "").lower()
    return [model for word, model in GPU_KEYWORDS.items() if word in lowered]
