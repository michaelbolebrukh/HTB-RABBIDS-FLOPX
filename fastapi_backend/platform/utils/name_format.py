"""Deterministic display-name casing.

Replaces Python's `str.title()`, which mangles real names (McDonald -> Mcdonald,
O'Brien -> O'Brien only by luck, hyphenated and particle names, roman numerals).
Uses `nameparser` (pure-Python, no runtime deps) for the hard cases - Mc/Mac,
apostrophe prefixes, lowercase particles (van/de/von/der), hyphen segments, and
roman-numeral suffixes - with `force=True` so mixed/upper/lower input is always
re-cased consistently.

nameparser over-splits a few genuine "Mac..." words that are not Scottish
patronymics (Macey -> MacEy, Mack -> MacK); a small exception set restores those.
"""
import re

from nameparser import HumanName
from nameparser.config import CONSTANTS


# Re-case even already-mixed-case input so the output is fully deterministic:
CONSTANTS.force_mixed_case_capitalization = True

# Genuine names beginning "Mac"/"Mc" that are NOT patronymic prefixes and must
# not get an internal capital (kept lowercase after the prefix). Compared on the
# whole lowercased word:
_MAC_NOT_PREFIX = {
    "mac", "mace", "macey", "mack", "macki", "mackie", "macklin", "macon",
    "macedo", "machado", "macbeth", "machin", "maclin", "macy", "mackrell",
    "mcguffin", "mck",
}

_WORD_RE = re.compile(r"[A-Za-z]+")


def _fix_mac_word(word: str) -> str:
    """Undoes nameparser's Mac/Mc split for known non-patronymic words."""
    if word.lower() in _MAC_NOT_PREFIX:
        return word[:1].upper() + word[1:].lower()
    return word


def format_display_name(raw: str) -> str:
    """Returns a deterministically, correctly-cased display name.

    Idempotent and stable: the same input always yields the same output,
    regardless of the input's original case. Empty/whitespace input returns the
    stripped input unchanged (validation handles emptiness elsewhere).
    """
    stripped = (raw or "").strip()
    if not stripped:
        return stripped

    name = HumanName(stripped)
    name.capitalize(force=True)
    result = str(name)

    # Repair the handful of Mac/Mc words nameparser over-splits:
    return _WORD_RE.sub(lambda m: _fix_mac_word(m.group(0)), result)
