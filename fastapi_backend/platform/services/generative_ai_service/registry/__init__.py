"""
Registry package — model capabilities, costs, and windows (Phase 3).

Public surface: `get_model_info` (the stable P2 contract, now registry-backed),
`find_models` (capability/budget queries), `all_models`, and the `ModelInfo`
superset schema. Data comes from the committed `data/models.json` (written
offline by sync.py, reviewed via PR) layered under `overrides.json` hand-pins.
No `pydantic_ai` imports anywhere in this package (D3).
"""

from .loader import all_models, get_model_info, registry_size
from .query import find_models
from .schema import Capabilities, Cost, Modalities, ModelInfo


__all__ = [
    "Capabilities", "Cost", "Modalities", "ModelInfo",
    "all_models", "find_models", "get_model_info", "registry_size",
]
