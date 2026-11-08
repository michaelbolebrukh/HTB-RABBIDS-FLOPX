"""No request path reaches a real brokerage (falsifier V3 F17, 2026-11-07).

`sources/alpaca.py` is a dormant adapter. The deployed stack has PRODUCT_ALPACA_* set
(pointing at Alpaca's paper API), so the adapter reports itself configured there:
the only thing keeping orders on the simulated broker is that nothing a router can
reach imports it. This pins that, by importing the product router aggregate in a
clean interpreter and checking the module never loaded, so an indirect import
through any service is caught as well as a direct one.
"""

import os
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]
ADAPTER = "product.flopx.sources.alpaca"


def _loaded_after_import(module: str) -> bool:
    probe = f"import sys, {module}; print({ADAPTER!r} in sys.modules)"
    result = subprocess.run([sys.executable, "-c", probe], cwd=BACKEND_ROOT, env=dict(os.environ), capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip().splitlines()[-1] == "True"


def test_no_product_router_imports_the_brokerage_adapter():
    assert _loaded_after_import("product.routers") is False


def test_the_probe_does_see_the_adapter_when_it_is_imported():
    # Guards the probe itself: a broken check that always answered False would pass the test above.
    assert _loaded_after_import(ADAPTER) is True
