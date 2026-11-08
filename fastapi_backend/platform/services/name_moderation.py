"""Display-name appropriateness check.

Blocks clearly offensive display names (profanity, slurs, sexual/hate terms)
while keeping FALSE POSITIVES near zero on real names - the Scunthorpe problem.
Real names that collide with slang (Dick, Gaylord, Fanny, Willy, Semen, ...) and
names that merely *contain* an offensive substring (Cockburn, Scunthorpe,
Cummings) must pass.

Approach (per the payments/name research):
- Reuse `better_profanity`'s maintained wordlist + leetspeak handling as the
  offensive-term engine (pure-Python, ~40 KB, offline - no moderation API).
- Split the name into tokens on spaces / hyphens / apostrophes / dots so
  matching is whole-token, never naive substring.
- Drop any token on the SAFE-NAME ALLOWLIST before checking - this is the lever
  that keeps false positives near zero; the corpus test in the test suite is the
  gate that proves it.
- Fail OPEN: if the engine errors, allow the name (a moderation hiccup must never
  block a legitimate rename).
"""
import logging
import re

from better_profanity import profanity


logger = logging.getLogger(__name__)

# Load the default wordset once at import (idempotent):
profanity.load_censor_words()

# Clearly-offensive terms the (US-centric) default list misses - mostly British
# profanity/slurs. Added through the engine so its leetspeak variants apply too.
# Keep to UNAMBIGUOUS terms (never legitimate names); anything name-colliding
# belongs in the allowlist below, not here:
_EXTRA_BADWORDS = [
    "bollocks", "bellend", "wanker", "knobhead", "minger", "munter",
    "shite", "arsehole", "tosser", "slag", "slapper", "pikey",
    "chink", "spastic", "tranny", "kike", "gook",
]
profanity.add_censor_words(_EXTRA_BADWORDS)

# Real names / tokens that collide with the default profanity list or are mild
# enough to allow in a display name. Compared lowercased, whole-token. Extend
# here (not by weakening matching) when a legitimate name is ever reported.
_SAFE_NAME_ALLOWLIST = {
    "dick", "dicky", "dickie", "dickson", "dickinson", "dickens", "dicks",
    "gaylord", "gay", "gaye", "gayle", "kaur",
    "fanny", "fannie", "willy", "willie", "willey", "dong", "wang", "ho",
    "semen", "semyon", "cockburn", "cocke", "cox", "hancock", "babcock",
    "cummings", "cumming", "cum", "cummins", "scunthorpe", "assange", "bass",
    "analiese", "anais", "shitij", "phuc", "phat", "phuong", "dung", "bich",
    "hoare", "hoar", "titus", "titi", "bumgarner", "weiner", "wiener", "clitheroe",
}

# Separators inside a name that must break tokens (so "cock-burn" and "o'dick"
# are still whole-token matched, never substringed):
_SPLIT_RE = re.compile(r"[\s\-'.’_]+")


def is_name_appropriate(name: str) -> bool:
    """Returns True when the name is acceptable, False when it should be blocked."""
    if not name or not name.strip():
        return True  # emptiness is a validation concern, not a moderation one

    try:
        tokens = [t for t in _SPLIT_RE.split(name.strip()) if t]
        # Drop allowlisted tokens, then let the engine judge the remainder as a
        # single space-joined string (its own tokenizer + leet handling apply):
        remaining = " ".join(t for t in tokens if t.casefold() not in _SAFE_NAME_ALLOWLIST)
        if not remaining:
            return True
        return not profanity.contains_profanity(remaining)
    except Exception:
        logger.exception("Name moderation check failed; allowing name (fail-open).")
        return True
