"""Person identity extraction from an uploaded PDF (a CV): a strong
deterministic parser first, then an AI fallback for whatever it missed.

Three seams:

- `extract_name_from_pdf(pdf_bytes, email)` — deterministic name, or None.
- `extract_email_from_pdf(pdf_bytes)` — deterministic email, or None.
- `extract_identity_from_pdf(pdf_bytes)` — the parse-preview's whole pipeline:
  both deterministic results, then ONE AI call (only when at least one field
  is still missing) asking for exactly the missing field(s). The parser's
  result always wins per field; the AI only ever fills a gap. Either field
  may legitimately end up None — the client shows an empty, editable box.

The first two are deliberately cheap and deterministic — first page only, a
handful of layout/text signals, no model calls — so the same file always
yields the same answer and a bad guess costs nothing (callers prefill an
editable, reviewed field). `extract_identity_from_pdf` is the only seam that
ever calls out to a model, and it is fail-open throughout (see
`_ai_identity_fallback`): no API key, a network error, or a timeout all just
mean the deterministic result stands, never a broken upload.

Parsing and scoring are separate on purpose: `_read_first_page` is the only
pdfminer-touching code, while `pick_name`/`pick_email` are pure functions over
text lines, so the scoring behaviour is unit-testable without authoring PDFs.

Name signals, strongest first:

- EMAIL ANCHOR: the caller knows the account email; a candidate whose tokens
  match the local part ("Jane Doe" vs jane.doe@...) is almost certainly the
  name. This is what makes the extractor safe rather than merely plausible.
- FONT SIZE: CVs set the name in the largest type on page one.
- POSITION: earlier lines are likelier to be the header.
- PDF /Author metadata: a weak corroborator only (often "Microsoft Office
  User"), never trusted alone.

Before scoring, each candidate line is expanded into shape VARIANTS so messy
real-world layouts still parse (see `_name_variants`): "DOE, Jane" reorders to
"Jane Doe", "Dr. Jane Doe" drops the honorific, and "Jane Doe — Quantitative
Analyst" splits off the leading name from the trailing job title. Text is also
NFKC-normalized, which folds ligature glyphs and combining accents into their
plain-letter form.

Email signal: POSITION alone, now over BOTH a plain "user@domain" scan and an
obfuscated-address scan ("jane dot doe at gmail dot com", "[at]"/"[dot]"
brackets — see `_OBFUSCATED_EMAIL_RE`), merged and sorted by where each match
starts. A CV's own contact email is almost always the first address on the
page (a header/contact line); a referee's or a former employer's address
further down comes later, so the earliest plausible match wins. There is no
font-size signal for email the way there is for a name — contact lines are
rarely the largest text on the page — and generic shared mailboxes
("careers@…", "info@…") are refused outright rather than guessed at, since a
CV's own contact line is essentially never one of those.
"""

import base64
import io
import logging
import re
import unicodedata
import zlib
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI
from pydantic import BaseModel, Field

from infrastructure.settings import settings as s


logger = logging.getLogger(__name__)


# How many top-of-page lines are considered; a name deeper than this is not a
# header and the email fallback is the better answer:
MAX_CANDIDATE_LINES = 15

# A candidate must clear this to win; calibrated so that (largest font + first
# line) or (email match + any position) passes, while an arbitrary capitalized
# mid-page line does not:
SCORE_THRESHOLD = 0.35

# Words that mark a line as a heading, contact row, or address — never a name.
# Compared per lowercased token:
_STOP_TOKENS = frozenset({
    # Document headings:
    "curriculum", "vitae", "cv", "resume", "résumé",
    # Section headers:
    "education", "experience", "skills", "profile", "contact", "summary",
    "projects", "awards", "objective", "references", "employment",
    "qualifications", "interests", "languages", "achievements",
    "certifications", "publications", "personal", "statement", "portfolio",
    # Contact/address vocabulary:
    "email", "phone", "tel", "mobile", "address", "linkedin", "github",
    "street", "road", "avenue", "lane", "drive", "kingdom",
    # Places. A "Manchester, England" line reorders to the perfectly
    # name-shaped "England Manchester", and no general rule separates that
    # from "Doe, Jane" — so the common places are named. Not exhaustive by
    # nature; the reordered-variant tiering above is the general defence and
    # this list is the specific one:
    "london", "manchester", "birmingham", "leeds", "glasgow", "edinburgh",
    "bristol", "liverpool", "sheffield", "cardiff", "belfast", "oxford",
    "cambridge", "nottingham", "leicester", "coventry", "newcastle",
    "brighton", "york", "aberdeen", "dundee", "reading", "southampton",
    "england", "scotland", "wales", "ireland", "britain", "uk",
    "france", "germany", "spain", "italy", "netherlands", "belgium",
    "poland", "portugal", "greece", "sweden", "norway", "denmark",
    "finland", "switzerland", "austria", "romania", "hungary", "ukraine",
    "china", "india", "japan", "korea", "singapore", "malaysia", "indonesia",
    "vietnam", "thailand", "philippines", "pakistan", "bangladesh",
    "australia", "canada", "usa", "america", "states", "mexico", "brazil",
    "russia", "turkey", "egypt", "nigeria", "kenya", "emirates", "israel",
    # Institutions and degrees (an "Imperial College London" line is not a name):
    "university", "college", "school", "institute", "academy",
    "bsc", "msc", "ba", "ma", "phd", "mphil", "meng", "beng", "mba", "msci",
    # Months, in full and the abbreviated form date rows often use:
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec",
    # Personal-details rows (a "Nationality: British" style line, once a colon
    # or bare label is stripped by the shape check, is not a name):
    "nationality", "gender", "dob", "birth",
})

# Honorifics that precede a name on its own header line ("Dr. Jane Doe") —
# stripped as a candidate variant (see `_strip_honorific`) since the raw token
# ("Dr." or "Mr") is never itself alphabetic and would otherwise sink the
# whole line's shape check:
_HONORIFICS = frozenset({"mr", "mrs", "ms", "miss", "mx", "dr", "prof", "professor"})

# A name token: Unicode letters with optional internal hyphens/apostrophes
# (José, O'Brien, Smith-Jones). Checked after stripping those separators:
_TOKEN_SEPARATORS_RE = re.compile(r"[-'’]")
_LOCAL_PART_SPLIT_RE = re.compile(r"[._\-+0-9]+")
_WHITESPACE_RE = re.compile(r"\s+")

# Splits a "Name — Job Title" or "Name | Job Title" header at a dash/pipe/
# bullet/colon separator that has whitespace on BOTH sides — never a bare
# hyphen, which stays inside a hyphenated surname like Smith-Jones. The
# character class covers hyphen-minus plus the Unicode dash block (en/em
# dash and friends, ‐-―):
_TITLE_SEPARATOR_RE = re.compile(r"\s[\-‐-―|•:]\s")


@dataclass(frozen=True)
class TextLine:
    """One text line from the first page, in reading order."""

    text: str
    font_size: float
    order: int
    # Left edge in PDF user-space units — the tie-breaker that keeps a
    # two-column layout's columns from interleaving (see _read_first_page).
    # Defaulted so existing call sites/tests that build a TextLine directly
    # (single-column fixtures) do not need to supply it:
    x0: float = 0.0


def extract_name_from_pdf(data: bytes, email: Optional[str] = None) -> Optional[str]:
    """Best-effort person name from the first page of a PDF, or None.

    Deterministic: the same bytes and email always produce the same result.
    Never raises on malformed input — an unparseable file returns None.
    """
    try:
        lines, author = _read_first_page(data)
    except Exception:
        logger.warning("Could not parse PDF for name extraction", exc_info=True)
        return None
    return pick_name(lines, email=email, author=author)


def pick_name(
    lines: list[TextLine],
    email: Optional[str] = None,
    author: Optional[str] = None,
) -> Optional[str]:
    """The pure scoring core: the winning candidate's normalized text, or None.

    Each raw line is expanded into shape VARIANTS before the filter runs (see
    `_name_variants`) — a comma-reordered surname, an honorific stripped, a
    leading name split off a trailing job title — so a line that is not
    name-shaped as written can still win via a form that is, while keeping
    the SAME font size/position/order for scoring (those describe the line
    the text came from, not the substring)."""
    # Each candidate carries whether it came from REORDERING a comma, because
    # that variant is the speculative one and gets penalised below:
    candidates: list[tuple[TextLine, bool]] = []
    for line in lines[:MAX_CANDIDATE_LINES]:
        reordered = _comma_reordered(_normalize(line.text))
        for variant in _name_variants(line.text):
            if _is_name_shaped(variant):
                candidates.append((
                    TextLine(text=variant, font_size=line.font_size, order=line.order, x0=line.x0),
                    variant == reordered,
                ))
    if not candidates:
        return None

    sizes = [line.font_size for line in lines if line.font_size > 0]
    min_size, max_size = (min(sizes), max(sizes)) if sizes else (0.0, 0.0)
    email_tokens = _email_local_tokens(email)
    author_normalized = _normalize(author or "").lower()

    # Reordered variants are the SPECULATIVE reading, so they are ranked in
    # their own tier and consulted only when nothing read as a name directly.
    # A penalty cannot do this job: a genuine "Doe, Jane" header scores exactly
    # at the threshold, so docking it any amount deletes the case the variant
    # exists for. Location lines are handled by _STOP_TOKENS instead:
    best = best_reordered = None
    best_score = best_reordered_score = 0.0
    for candidate, is_reordered in candidates:
        score = _score(candidate, min_size, max_size, email_tokens, author_normalized)
        # Strictly-greater keeps the earlier line on ties (deterministic):
        if is_reordered:
            if score > best_reordered_score:
                best_reordered, best_reordered_score = candidate, score
        elif score > best_score:
            best, best_score = candidate, score

    if best is None or best_score < SCORE_THRESHOLD:
        best, best_score = best_reordered, best_reordered_score

    if best is None or best_score < SCORE_THRESHOLD:
        return None
    return _normalize(best.text)


# ============================================================================
# SCORING
# ============================================================================

def _score(
    line: TextLine,
    min_size: float,
    max_size: float,
    email_tokens: list[str],
    author_normalized: str,
) -> float:
    tokens = [token.lower() for token in _normalize(line.text).split()]
    score = 0.0

    # Email anchor (up to 0.5) — the decisive signal when present:
    if email_tokens:
        overlap = sum(1 for token in tokens if token in email_tokens)
        concatenated = "".join(tokens)
        local_concatenated = "".join(email_tokens)
        if overlap:
            score += 0.5 * (overlap / len(tokens))
        elif concatenated and (
            concatenated in local_concatenated or local_concatenated in concatenated
        ):
            # janedoe@… matching "Jane Doe" with no separator to split on:
            score += 0.5
        elif len(tokens) >= 2 and (tokens[0][0] + tokens[-1]) == local_concatenated:
            # Initial + surname local parts (jdoe@…):
            score += 0.45

    # Font size (up to 0.3): the page's largest text scores full marks:
    if max_size > min_size and line.font_size > 0:
        score += 0.3 * (line.font_size - min_size) / (max_size - min_size)
    elif line.font_size > 0:
        score += 0.15

    # Position (up to 0.2): earlier lines are likelier headers:
    score += 0.2 * (1 - line.order / MAX_CANDIDATE_LINES)

    # /Author agreement (0.15): corroboration only — on its own it cannot
    # clear the threshold, so "Microsoft Office User" never wins:
    if author_normalized and _normalize(line.text).lower() == author_normalized:
        score += 0.15

    return score


# The Unicode blocks a CJK/Hangul personal name is written in. Checked as whole
# ranges rather than a script name so no extra dependency is needed:
_CJK_RANGES = (
    (0x3040, 0x30FF),   # Hiragana + Katakana
    (0x3400, 0x4DBF),   # CJK Extension A
    (0x4E00, 0x9FFF),   # CJK Unified Ideographs
    (0xAC00, 0xD7AF),   # Hangul syllables
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
)


def _is_cjk_name_shaped(text: str) -> bool:
    """Whether text is a plausible CJK/Hangul name written as one token.

    Sized to real names (a Chinese name is typically 2-3 characters, a Korean
    one 2-4), which also keeps it from swallowing a heading or a sentence."""
    stripped = text.strip()
    if not 2 <= len(stripped) <= 6:
        return False
    return all(
        any(low <= ord(ch) <= high for low, high in _CJK_RANGES)
        for ch in stripped
    )


def _is_name_shaped(text: str) -> bool:
    """Hard filters: shape that could be a personal name, nothing else."""
    normalized = _normalize(text)
    if _is_cjk_name_shaped(normalized):
        return True
    if not 4 <= len(normalized) <= 40:
        return False
    if any(ch.isdigit() for ch in normalized):
        return False
    if "@" in normalized or "," in normalized:
        return False
    lowered = normalized.lower()
    if "http" in lowered or "www." in lowered:
        return False

    tokens = normalized.split()
    # CJK and Hangul names are conventionally written as ONE unspaced token
    # ("张伟", "김민준"), so a whitespace-token count rejects them outright
    # however long they are. That is not an edge case for a UK university
    # society: Chinese, Korean, Hong Kong and Singaporean students are a large
    # part of the cohort, and this is simply how their names are written. Those
    # scripts get a character-count rule instead, sized to real given+family
    # names (the 4-char minimum length check above is bypassed for the same
    # reason - two characters is a complete name here):
    if len(tokens) == 1 and _is_cjk_name_shaped(tokens[0]):
        return True
    if not 2 <= len(tokens) <= 4:
        return False
    for token in tokens:
        if token.lower() in _STOP_TOKENS:
            return False
        if not _TOKEN_SEPARATORS_RE.sub("", token).isalpha():
            return False
    return True


def _normalize(text: str) -> str:
    """NFKC-folds the text (ligature glyphs and combining accents collapse to
    their plain-letter form), collapses whitespace, strips decoration, and
    title-cases ALL-CAPS lines (many CVs set the name in caps)."""
    folded = unicodedata.normalize("NFKC", text or "")
    cleaned = _WHITESPACE_RE.sub(" ", folded).strip()
    cleaned = cleaned.strip("•·|:;.-— ").strip()
    if cleaned.isupper():
        cleaned = " ".join(word.capitalize() for word in cleaned.split())
    return cleaned


# ============================================================================
# NAME VARIANTS (messy real-world header shapes -> a plain "First Last")
# ============================================================================

def _name_variants(text: str) -> list[str]:
    """Every plausible plain-name reading of one raw line, original first.

    Cheap and order-preserving on purpose: each transform below is applied to
    the ORIGINAL text (never chained onto another transform's output, honorific
    stripping aside) so a messy "DR. DOE, JANE — QUANTITATIVE ANALYST" header
    still yields "Jane Doe" without needing every transform to compose."""
    normalized = _normalize(text)
    variants = [normalized]

    stripped = _strip_honorific(normalized) or normalized
    for candidate in (normalized, stripped):
        reordered = _comma_reordered(candidate)
        if reordered:
            variants.append(reordered)
        leading = _split_leading_segment(candidate)
        if leading:
            variants.append(leading)
            leading_reordered = _comma_reordered(leading)
            if leading_reordered:
                variants.append(leading_reordered)
    if stripped != normalized:
        variants.append(stripped)

    # Dedup, preserving first-seen order (the scoring loop tries them in
    # order and font/position are identical across variants of one line, so
    # order here only affects which literal string is scored — harmless):
    seen: set[str] = set()
    unique: list[str] = []
    for variant in variants:
        if variant and variant not in seen:
            seen.add(variant)
            unique.append(variant)
    return unique


def _strip_honorific(text: str) -> Optional[str]:
    """'Dr. Jane Doe' -> 'Jane Doe'. The raw honorific token ("Dr.", "Mr")
    is never itself alphabetic once its trailing period is considered, so
    without this the whole line fails the shape check on that token alone."""
    tokens = text.split()
    if len(tokens) < 2:
        return None
    first = tokens[0].rstrip(".").lower()
    if first not in _HONORIFICS:
        return None
    return " ".join(tokens[1:])


def _comma_reordered(text: str) -> Optional[str]:
    """'Doe, Jane' -> 'Jane Doe': a CV listing the family name first. Scoped
    tightly (exactly one comma, a single-token surname, a 1-2 token given
    name, every token alphabetic) so an address or section header with an
    incidental comma ("London, United Kingdom") does not get reshuffled into
    something that merely LOOKS name-shaped — those still carry their own
    stop-token (their content still hits `_STOP_TOKENS` either way once
    reordered, since the filter runs on the transform's output too)."""
    if text.count(",") != 1:
        return None
    surname, given = (part.strip() for part in text.split(",", 1))
    if not surname or not given:
        return None
    surname_tokens = surname.split()
    given_tokens = given.split()
    if len(surname_tokens) != 1 or not 1 <= len(given_tokens) <= 2:
        return None
    for token in surname_tokens + given_tokens:
        if not _TOKEN_SEPARATORS_RE.sub("", token).isalpha():
            return None
    return f"{given} {surname}"


def _split_leading_segment(text: str) -> Optional[str]:
    """'Jane Doe — Quantitative Analyst' -> 'Jane Doe': CVs often set the name
    beside a job title on one line. Only splits on a dash/pipe/bullet/colon
    with whitespace on BOTH sides (see `_TITLE_SEPARATOR_RE`) — a bare hyphen
    stays inside a hyphenated surname like Smith-Jones."""
    match = _TITLE_SEPARATOR_RE.search(text)
    if not match:
        return None
    leading = text[: match.start()].strip()
    return leading or None


def _email_local_tokens(email: Optional[str]) -> list[str]:
    """jane.doe@x → ["jane", "doe"]; jdoe04@x → ["jdoe"]. Lowercased, no blanks."""
    if not email or "@" not in email:
        return []
    local_part = email.split("@", 1)[0].lower()
    return [token for token in _LOCAL_PART_SPLIT_RE.split(local_part) if token]


# ============================================================================
# EMAIL EXTRACTION
# ============================================================================

# Local@domain, tolerant of whitespace/newlines around the "@" and each domain
# dot — pdfminer text can wrap an address mid-token when it splits across a
# line. The local part starts with an alphanumeric so a stray "mailto:"
# prefix or a leading bullet never gets swallowed into it; the match simply
# starts right after it:
_EMAIL_LOOSE_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._%+\-]*"
    r"\s*@\s*"
    r"[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\s*\.\s*[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)+"
)

# Generic shared mailboxes: a real address, but never the applicant's own —
# a CV's contact line is essentially never one of these, while a company's
# "careers@" line or a referee's "office@" line easily is. Checked on the
# local part with any "+tag" suffix stripped:
_ROLE_LOCAL_PARTS = frozenset({
    "info", "hr", "careers", "career", "contact", "admin", "administrator",
    "support", "sales", "office", "enquiries", "inquiries", "noreply",
    "no-reply", "donotreply", "jobs", "recruitment", "recruiting",
    "applications", "apply", "team", "hello", "webmaster", "postmaster",
    "media", "press", "marketing", "help", "service", "services",
})

# Obfuscated addresses ("jane dot doe at gmail dot com", "jane[at]gmail.com"):
# a WORD (letters/digits/hyphen/literal dot), optionally more WORDs joined by
# a spelled-out/bracketed "dot", then an "at", then a WORD with at least one
# MORE "dot"-joined WORD — that last requirement guarantees the domain has a
# TLD-shaped tail, which is also exactly what `_clean_email` demands, so a
# stray "graduated at Cambridge" (no trailing dot) can never produce a match
# that survives the same gate every other candidate passes through:
_OBFUS_WORD = r"[A-Za-z0-9][A-Za-z0-9.\-]*"
_OBFUS_DOT_RE = re.compile(r"\s*(?:\[dot\]|\(dot\)|\bdot\b)\s*", re.IGNORECASE)
_OBFUS_AT = r"\s*(?:\[at\]|\(at\)|\bat\b)\s*"
# The domain group allows ZERO explicit "dot"/"[dot]" separators (not one-or-
# more) because a WORD already swallows a LITERAL "." itself (e.g.
# "gmail.com" is one WORD with no separate dot-token) — "at least one dot
# somewhere in the domain" is enforced downstream by `_clean_email` instead,
# uniformly for every candidate regardless of which spelling produced it:
_OBFUSCATED_EMAIL_RE = re.compile(
    rf"(?i)\b({_OBFUS_WORD}(?:{_OBFUS_DOT_RE.pattern}{_OBFUS_WORD})*)"
    rf"{_OBFUS_AT}"
    rf"({_OBFUS_WORD}(?:{_OBFUS_DOT_RE.pattern}{_OBFUS_WORD})*)\b"
)


def extract_email_from_pdf(data: bytes) -> Optional[str]:
    """Best-effort applicant email from the first page of a PDF, or None.

    Deterministic: the same bytes always produce the same answer. Never
    raises on malformed input — an unparseable file returns None, exactly
    like extract_name_from_pdf."""
    try:
        lines, _author = _read_first_page(data)
    except Exception:
        logger.warning("Could not parse PDF for email extraction", exc_info=True)
        return None
    return pick_email(lines)


def pick_email(lines: list[TextLine]) -> Optional[str]:
    """The pure scoring core: the earliest plausible personal address, or
    None. Conservative on purpose — it is much better to return nothing than
    a confidently wrong address, since the caller shows the result in an
    editable field rather than trusting it outright.

    Scans for BOTH a plain "user@domain" address and a spelled-out/obfuscated
    one, then merges the two match streams by where each one STARTS in the
    page text — the same "earliest wins" rule either way, so a real address
    earlier on the page still beats an obfuscated one further down, and vice
    versa. Every candidate from either stream passes through the SAME
    `_clean_email` / `_is_plausible_personal_email` gate."""
    # Lines are already top-down (see _read_first_page); joining them with a
    # newline keeps that reading order while still letting the loose pattern
    # bridge an address pdfminer happened to split across two lines:
    text = unicodedata.normalize("NFKC", "\n".join(line.text for line in lines))

    plain: list[tuple[int, str]] = [
        (match.start(), match.group(0)) for match in _EMAIL_LOOSE_RE.finditer(text)
    ]
    obfuscated: list[tuple[int, str]] = [
        (match.start(), _deobfuscated_candidate(match))
        for match in _OBFUSCATED_EMAIL_RE.finditer(text)
    ]

    # A real "user@domain" ALWAYS beats a de-obfuscated one, wherever each sits
    # on the page. The obfuscated pattern is inherently a guess at prose: any
    # "WORD at WORD dot WORD" matches it, so an ordinary sentence ("...I
    # graduated at Cambridge. Dot com...") mints a plausible-looking address
    # that was never in the document. Merging both by position let such a
    # sentence outrank the actual contact line further down and return a
    # FABRICATED address, which is the one outcome this module exists to avoid.
    # Position still decides WITHIN each tier:
    for tier in (plain, obfuscated):
        for _position, raw in sorted(tier, key=lambda item: item[0]):
            candidate = _clean_email(raw)
            if candidate and _is_plausible_personal_email(candidate):
                return candidate
    return None


def _deobfuscated_candidate(match: re.Match) -> str:
    """Renders an `_OBFUSCATED_EMAIL_RE` match as a plain "local@domain"
    string so it can go through the exact same cleanup/validation as every
    other candidate."""
    local = _OBFUS_DOT_RE.sub(".", match.group(1))
    domain = _OBFUS_DOT_RE.sub(".", match.group(2))
    return f"{_WHITESPACE_RE.sub('', local)}@{_WHITESPACE_RE.sub('', domain)}"


def _clean_email(raw: str) -> Optional[str]:
    """Collapses whitespace a line wrap introduced, strips leftover bracket/
    punctuation decoration, and rejects anything still malformed."""
    collapsed = _WHITESPACE_RE.sub("", raw)
    collapsed = collapsed.strip(".,;:()<>[]{}\"'|")
    if collapsed.count("@") != 1:
        return None
    local, domain = collapsed.split("@")
    domain = domain.rstrip(".,;:()<>[]{}\"'|-")
    if not local or "." not in domain:
        return None
    tld = domain.rsplit(".", 1)[-1]
    if not (2 <= len(tld) <= 24) or not tld.isalpha():
        return None
    return f"{local}@{domain}".lower()


def _is_plausible_personal_email(email: str) -> bool:
    """Refuses generic shared mailboxes; everything else is accepted — this
    is a blocklist, not a positive identification, in keeping with "position
    decides, this only vetoes the obviously wrong ones"."""
    local = email.split("@", 1)[0].split("+", 1)[0]
    return local.lower() not in _ROLE_LOCAL_PARTS


# ============================================================================
# PDF PARSING (the only pdfminer-touching code)
# ============================================================================

# Row-banding tolerance for the two-column sort below, in PDF user-space
# points (~1/72 inch): two lines whose tops fall within one band are treated
# as the same visual row and ordered left-to-right instead of by exact y,
# which is what actually separates a two-column layout's columns rather than
# interleaving them line-by-line:
_ROW_BAND_POINTS = 3.0

# The most page-one drawing instructions this module will lay out, measured
# AFTER decompression.
#
# pdfminer's layout engine has no element ceiling, and the cost tracks the
# volume of operators almost linearly: measured on this codebase, roughly one
# second per 100KB of content stream, 20 seconds for 2MB. The file's SIZE
# predicts none of that — a 61KB upload carrying 1M text operators (515:1 under
# FlateDecode, entirely ordinary compression) runs for minutes. `/talent/parse`
# is anonymous, so that was one request away.
#
# Bounding the INPUT rather than the work is deliberate, and the alternative is
# worth recording because it was built and deployed first: running the parse in
# a forked child with a wall-clock budget does cap it exactly, and it worked —
# but a CPython fork privatises inherited pages as fast as it refcounts them, so
# on a 512MB instance a single parse OOM-killed the whole service. A ceiling
# that takes the process down with it is not a ceiling. This check costs one
# bounded decompression and no process at all.
#
# 128KB is generous for real work: a text CV's first page is 5-50KB. A heavily
# illustrated one can exceed it, and that is a graceful outcome rather than a
# failure — the identity pipeline simply falls through to the vision path,
# which renders the same page through pdfium in a fifth of a second.
_MAX_PAGE_CONTENT_BYTES = 128 * 1024

# Content streams in the wild are Flate-compressed or not compressed at all.
# Anything else (LZW, chiefly) is decades obsolete and has no bounded decoder
# to hand, so rather than guess at its expansion this module declines to parse
# it and lets the vision path answer instead:
_BOUNDABLE_CONTENT_FILTERS = {"/FlateDecode"}


def _read_first_page(data: bytes) -> tuple[list[TextLine], Optional[str]]:
    """First-page text lines in reading order (top-down, then left-to-right
    within a row — see `_ROW_BAND_POINTS`), plus /Author metadata.

    Refuses pages carrying more drawing instructions than a document could
    honestly need, because the file decides how much work this is and the file
    is a stranger's (see `_MAX_PAGE_CONTENT_BYTES`). Refusing yields no lines,
    which the caller already handles as "this PDF told us nothing" and passes
    to the AI fallback — so an unusually rich CV is answered, not rejected:"""
    if not _page_one_content_is_boundable(data):
        return [], None
    return _read_first_page_unbounded(data)


def _page_one_content_is_boundable(data: bytes) -> bool:
    """Whether page one's drawing instructions are provably under the ceiling.

    Proof, not estimate: the check decompresses at most one byte past the cap
    (`zlib.decompressobj.decompress` takes a `max_length`, so a 500:1 bomb
    costs the cap and stops), and answers False for anything it cannot measure
    that way. Reading the file with pikepdf is cheap — qpdf is C++ and lazy —
    and it is the only library here that can see a stream without decoding it.
    """
    try:
        import pikepdf
    except ImportError:
        # Without a way to measure, the parse runs as it always did. The
        # dependency is present in every deployment; this is not a silent
        # fallback so much as an import that cannot fail:
        return True

    try:
        with pikepdf.open(io.BytesIO(data)) as pdf:
            if len(pdf.pages) == 0:
                return True
            contents = pdf.pages[0].obj.get("/Contents")
            streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
            budget = _MAX_PAGE_CONTENT_BYTES
            for stream in streams:
                if stream is None:
                    continue
                measured = _decoded_length_within(stream, budget)
                if measured is None:
                    logger.info("Skipping the deterministic parse: page one's content is not measurable")
                    return False
                budget -= measured
                if budget < 0:
                    logger.info(
                        "Skipping the deterministic parse: page one carries over %dKB of drawing "
                        "instructions, which no CV needs and a crafted file uses to burn CPU",
                        _MAX_PAGE_CONTENT_BYTES // 1024,
                    )
                    return False
    except Exception:
        # A file pikepdf cannot open is one pdfminer will not get far with
        # either, so let the parse have its usual attempt and fail its usual
        # way rather than inventing a second rejection path here:
        return True
    return True


def _decoded_length_within(stream, cap: int) -> Optional[int]:
    """Decoded length of one content stream, or None when that cannot be
    bounded (an unsupported filter, or more bytes than `cap` allows)."""
    import pikepdf

    raw = stream.read_raw_bytes()
    declared = stream.get("/Filter")
    if declared is None:
        filters = []
    elif isinstance(declared, pikepdf.Array):
        filters = [str(item) for item in declared]
    else:
        filters = [str(declared)]

    if not filters:
        return len(raw) if len(raw) <= cap else None
    if len(filters) > 1 or filters[0] not in _BOUNDABLE_CONTENT_FILTERS:
        return None

    decompressor = zlib.decompressobj()
    decoded = decompressor.decompress(raw, cap + 1)
    # Anything left unconsumed means the stream had more to give than the cap
    # allowed, which is the answer without ever holding the rest of it:
    if len(decoded) > cap or decompressor.unconsumed_tail:
        return None
    return len(decoded)


def _read_first_page_unbounded(data: bytes) -> tuple[list[TextLine], Optional[str]]:
    from pdfminer.high_level import extract_pages
    from pdfminer.layout import LTChar, LTTextContainer, LTTextLine

    raw: list[tuple[float, float, float, str]] = []  # (top_y, x0, font_size, text)
    for page_layout in extract_pages(io.BytesIO(data), maxpages=1):
        for element in page_layout:
            if not isinstance(element, LTTextContainer):
                continue
            for text_line in element:
                if not isinstance(text_line, LTTextLine):
                    continue
                text = text_line.get_text().strip()
                if not text:
                    continue
                char_sizes = [
                    obj.size for obj in text_line if isinstance(obj, LTChar) and obj.size > 0
                ]
                font_size = max(char_sizes) if char_sizes else 0.0
                raw.append((text_line.y1, text_line.x0, font_size, text))

    # Top of the page first, then left column before right within a row —
    # pdfminer's container order is not reliably visual, and a plain y-sort
    # alone interleaves a two-column layout's columns whenever two lines
    # happen to land at almost (but not exactly) the same height:
    raw.sort(key=lambda item: (-round(item[0] / _ROW_BAND_POINTS), item[1]))
    lines = [
        TextLine(text=text, font_size=font_size, order=index, x0=x0)
        for index, (_, x0, font_size, text) in enumerate(raw)
    ]
    return lines, _read_author(data)


def _read_author(data: bytes) -> Optional[str]:
    from pdfminer.pdfdocument import PDFDocument
    from pdfminer.pdfparser import PDFParser
    from pdfminer.utils import decode_text

    try:
        document = PDFDocument(PDFParser(io.BytesIO(data)))
        for info in document.info or []:
            author = info.get("Author")
            if author:
                decoded = decode_text(author) if isinstance(author, bytes) else str(author)
                return decoded.strip() or None
    except Exception:
        # Metadata is a weak corroborator; a malformed info dict is not an error:
        pass
    return None


# ============================================================================
# AI FALLBACK (the parse-preview's identity pipeline: parser first, AI for
# the gap it leaves)
# ============================================================================

# Empirically verified (real API calls, not documentation) against every
# candidate in {gpt-5.4-nano, gpt-5.4-mini, gpt-5-nano, gpt-5-mini,
# gpt-4.1-nano, gpt-4.1-mini}: ALL SIX support both structured/parsed output
# (`chat.completions.parse` with a Pydantic `response_format`) and image
# input. gpt-5-nano is the cheapest that clears both bars — $0.05 / $0.40 per
# 1M input/output tokens, undercutting gpt-4.1-nano ($0.10 / $0.40) and
# gpt-5.4-nano ($0.20 / $1.25) on both legs. One constant so a future model
# swap never touches a call site:
_AI_MODEL = "gpt-5-nano"

# gpt-5 models reason before answering, and left to their own judgement they
# reason for far too long for a request somebody is waiting on: measured on a
# real CV, gpt-5-nano takes 6.5 seconds at its default effort — past this
# call's own budget and past the five-second promise the whole endpoint makes,
# so EVERY call was timing out and the fallback silently never worked. The
# same model at minimal effort answers the same question, correctly, in 1.5s.
#
# Minimal is the right setting on the merits too, not just for the clock: this
# is a reading task, not a reasoning one. The answer is written on the page.
_AI_REASONING_EFFORT = "minimal"

# A CV's identity lives at the top of the document; bounding the text sent to
# the model caps cost AND keeps signal-to-noise high — a full CV's later
# sections (past employers, referees) would only invite the model to mine
# someone else's details instead of the applicant's own:
_AI_TEXT_CHARS = 4000

# Generous enough for a real round trip, short enough that a stalled request
# can never hold up a parse response — this step must fail OPEN, never hang
# the request it is merely trying to improve:
# The whole parse must answer in under five seconds, and this call is the only
# genuinely variable part of it (the page parse and the capped render are each
# a fraction of a second). Four leaves the endpoint its margin while covering
# what three did not: measured against the deployed service, a warm call
# answers in 1.7-2.0s, but the FIRST call after an idle spell pays a TLS
# handshake on top and came in at 3.7s — so three seconds meant the first
# visitor after every quiet period silently got no help at all. Retries are
# disabled below, so this genuinely bounds the call:
_AI_TIMEOUT_SECONDS = 4.0

# Below this many non-whitespace characters, page-one "text" is not real text
# to reason over — almost certainly a scanned CV with no extractable text
# layer — so the fallback switches from sending text to rendering the page as
# an image (see `_render_first_page_to_data_url`):
_MIN_USABLE_TEXT_CHARS = 20

# ~144 DPI (a 72pt-per-inch PDF page scaled 2x) — enough for a vision model to
# read header-sized text without paying for a print-resolution image:
_RENDER_SCALE = 2.0

# The hard ceiling on the rendered bitmap's longest side. A page's dimensions
# come from the FILE, so they are attacker-controlled and a fixed scale turns
# a tiny upload into gigabytes of bitmap (see _render_first_page_to_data_url).
# 2000px keeps a normal A4 page comfortably legible to the model while capping
# the worst case at roughly 16MB of RGBA:
_RENDER_MAX_PX = 2000


class _CvIdentityFields(BaseModel):
    """Structured extraction target for the AI identity fallback. Both fields
    are nullable by design: the caller only asks for whichever field(s) the
    deterministic parser missed, and a null costs nothing (the client shows
    an empty, editable box) while a wrong guess actively misleads the
    applicant filling in the form behind it — always prefer null over a guess."""

    name: Optional[str] = Field(
        default=None,
        description=(
            "The CV owner's own full personal name (the applicant), exactly as "
            "it appears on the document. Never a referee, former manager, "
            "colleague, institution, or company name. Return null if the "
            "applicant's own name is not clearly and confidently stated, or if "
            "a name was not one of the fields you were asked to find."
        ),
    )
    email: Optional[str] = Field(
        default=None,
        description=(
            "The CV owner's own personal contact email address. Never a "
            "referee's, employer's, or other third party's address, and never "
            "a generic/shared mailbox such as careers@, info@, hr@, admin@, "
            "support@, enquiries@, jobs@, or noreply@ — a CV's own contact "
            "line is essentially never one of those. Return null if the "
            "applicant's own email is not clearly and confidently stated, or "
            "if an email was not one of the fields you were asked to find."
        ),
    )


def extract_identity_from_pdf(
    data: bytes, *, allow_ai_fallback: bool = False
) -> dict[str, Optional[str]]:
    """The CV-first parse preview's whole identity pipeline: the strong
    deterministic parser first for BOTH fields, then — only if at least one
    is still missing AND the caller opted in — ONE AI call asking for exactly
    the missing field(s).

    `allow_ai_fallback` defaults to FALSE, and the default is the point: the
    fallback sends part of the document to a third-party model, so a caller
    that has not thought about whether its user consented to that gets the
    local-only path. Opting in is a decision each call site makes explicitly
    (today: signed-in callers only — an anonymous visitor has agreed to
    nothing yet, and the deterministic pass is what they get).

    Per field the parser's result always wins; the AI only ever fills a gap
    the parser left. Either field may legitimately end up None — that is a
    valid outcome, and the client asks the visitor directly rather than
    trusting a guess. Never raises: a malformed PDF, a missing API key, a
    network error, or a timeout all just mean less got filled in, never a
    broken request (see `_ai_identity_fallback`)."""
    try:
        lines, author = _read_first_page(data)
    except Exception:
        logger.warning("Could not parse PDF for identity extraction", exc_info=True)
        lines, author = [], None

    email = pick_email(lines)
    name = pick_name(lines, email=email, author=author)
    need_name, need_email = name is None, email is None

    if (need_name or need_email) and allow_ai_fallback:
        page_text = "\n".join(line.text for line in lines)
        try:
            ai_name, ai_email = _ai_identity_fallback(data, page_text, need_name, need_email)
        except Exception:
            # _ai_identity_fallback already fails open internally; this is a
            # second, belt-and-suspenders guard so a parse request can never
            # break because of the AI step (parse is in-memory only; durable
            # CV shares use fail-closed document_ingest, not this path):
            logger.exception("AI identity fallback raised unexpectedly")
            ai_name, ai_email = None, None
        name = name or ai_name
        # The model's answer goes through the SAME gate the deterministic path
        # uses. Without this the design principle is enforced in code on one
        # path and only by prompt wording on the other: a hallucinated or
        # role-mailbox address would reach the client, which `pick_email`
        # structurally cannot produce. A rejected guess simply leaves the field
        # empty, which is the outcome this module always prefers:
        if email is None and ai_email:
            cleaned = _clean_email(ai_email)
            if cleaned and _is_plausible_personal_email(cleaned):
                email = cleaned

    return {"name": name, "email": email}


def _ai_identity_fallback(
    data: bytes, page_text: str, need_name: bool, need_email: bool,
) -> tuple[Optional[str], Optional[str]]:
    """Asks the model for ONLY the field(s) the deterministic parser missed.
    Fails open in every direction — no API key, no usable input, a request
    error, or a timeout all just return (None, None), leaving the
    deterministic result as the final answer. Never raises."""
    if not (need_name or need_email):
        return None, None
    if not s.OPENAI_API_KEY:
        return None, None

    wanted = " and ".join(
        field for field, asked in (("name", need_name), ("email", need_email)) if asked
    )
    instruction = (
        "Below is content from the top of an uploaded CV/resume. Find the "
        f"applicant's own {wanted}. Return null for any field you were not "
        "asked to find, and null — never a guess — for anything you cannot "
        "state with confidence from the document."
    )

    usable_text = page_text.strip()
    if len(usable_text) >= _MIN_USABLE_TEXT_CHARS:
        content: list[dict] = [
            {"type": "text", "text": f"{instruction}\n\n---\n{usable_text[:_AI_TEXT_CHARS]}\n---"}
        ]
    else:
        # No usable text layer — almost certainly a scanned CV. There is no
        # tesseract or poppler (pdftoppm) binary available in this
        # environment or on the Render host, and the pip-installable OCR
        # alternatives (easyocr, paddleocr) pull in torch (2GB+), which this
        # service has already OOMed at. pypdfium2 bundles its own pdfium
        # build in a self-contained wheel — no system binary required — so
        # rendering the page as an image and reading it with a VISION model
        # IS the OCR mechanism here, not a stopgap for a missing one. Do not
        # "fix" this by reaching for tesseract/poppler/torch-based OCR:
        image_url = _render_first_page_to_data_url(data)
        if not image_url:
            return None, None
        content = [
            {"type": "text", "text": instruction},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]

    try:
        response = _ai_client().chat.completions.parse(
            model=_AI_MODEL,
            messages=[{"role": "user", "content": content}],
            response_format=_CvIdentityFields,
            reasoning_effort=_AI_REASONING_EFFORT,
            timeout=_AI_TIMEOUT_SECONDS,
        )
        parsed = response.choices[0].message.parsed
    except Exception:
        logger.warning("AI identity fallback failed; deterministic result stands", exc_info=True)
        return None, None

    if parsed is None:
        return None, None
    return (parsed.name if need_name else None), (parsed.email if need_email else None)


_ai_client_cache: Optional[OpenAI] = None


def _ai_client() -> OpenAI:
    """One client for the life of the process, not one per request.

    Each `OpenAI(...)` builds an httpx client underneath it: a connection pool,
    an SSL context, a fresh set of buffers. Per request that is both wasted
    allocation on a small instance and a new TLS handshake for every parse,
    where a pooled connection would already be warm. The construction is
    keyed on nothing request-specific, so there is nothing to vary.
    """
    global _ai_client_cache
    if _ai_client_cache is None:
        _ai_client_cache = OpenAI(
            api_key=s.OPENAI_API_KEY,
            organization=s.OPENAI_ORGANIZATION,
            timeout=_AI_TIMEOUT_SECONDS,
            # The timeout only bounds ONE attempt: the SDK retries twice by
            # default, so a stalled endpoint would cost 3 x 8s plus backoff,
            # which is not what the timeout above promises. This step is a
            # best-effort improvement on an answer we already have, so it gets
            # exactly one attempt:
            max_retries=0,
        )
    return _ai_client_cache


def _render_first_page_to_data_url(data: bytes) -> Optional[str]:
    """Renders page one to a PNG data URL — the vision fallback's OCR step
    (see the comment in `_ai_identity_fallback` for why tesseract/poppler
    are absent and this is the intended mechanism, not a workaround).

    Two ceilings, because there are two ways to make this expensive and each
    one is blind to the other:

    - The PIXEL clamp below bounds the bitmap the page is drawn INTO, which is
      what stops a declared 14400x14400 MediaBox from becoming gigabytes.
    - The CONTENT ceiling bounds how much drawing is done to fill it, which the
      pixel clamp cannot see: a million operators on an ordinary A4 page render
      into a modest 1224x1584 bitmap and still cost 2.5 seconds and 386MB,
      measured. That is an OOM on a small instance, and it was one — a bomb
      refused by the parser (see `_read_first_page`) has no text, so it lands
      HERE, and the ceiling that saved the parse handed the same file to the
      more expensive path. Both entry points check it now.

    A page over the content ceiling has nothing to read anyway; no person put
    a million overlapping glyphs on their CV:"""
    if not _page_one_content_is_boundable(data):
        return None
    try:
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        try:
            page = pdf[0]
            # The scale MUST be derived from the page's own size, never taken
            # as a constant. A PDF declares its page dimensions, and the spec
            # allows them to be enormous: a 585-byte file with a
            # MediaBox of 14400x14400 renders at a fixed scale of 2.0 to a
            # 28800x28800 bitmap, which is ~5.7GB of RAM and 40 seconds of CPU
            # for an anonymous, unauthenticated request. The upload size limit
            # cannot catch it, because the file is tiny; the cost is entirely
            # in the declared geometry. So the scale is clamped to whatever
            # keeps the LONGEST side within _RENDER_MAX_PX:
            width, height = page.get_size()
            longest = max(width or 0, height or 0)
            if longest <= 0:
                return None
            scale = min(_RENDER_SCALE, _RENDER_MAX_PX / longest)
            bitmap = page.render(scale=scale)
            image = bitmap.to_pil()
        finally:
            pdf.close()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        logger.warning("Could not render the PDF page for the vision fallback", exc_info=True)
        return None
