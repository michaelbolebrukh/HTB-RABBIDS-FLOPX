"""The product service layer, as the backend architecture rule names it.

FLOPX's business logic lives in the domain package `product.flopx`; this module
is the documented entry point and re-exports the request-shaped assembly
functions the routers call (`product.flopx.terminal`), so `product.services`
remains the one place a reader looks for "what does this product do".
"""

from product.flopx import terminal  # noqa: F401
from product.flopx.terminal import *  # noqa: F401,F403
