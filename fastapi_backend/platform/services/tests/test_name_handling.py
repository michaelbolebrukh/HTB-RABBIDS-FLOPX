"""Display-name casing + appropriateness checks.

The appropriateness corpus is the gate the product owner asked for: 120+ real
English and foreign given/family names and casual nicknames - including the
tricky ones that collide with slang (Dick, Gaylord, Fanny, Willy, Semen) or
merely contain an offensive substring (Cockburn, Scunthorpe, Cummings) - must
NOT be flagged (the Scunthorpe problem). Clear profanity/slurs must be blocked.
"""
from platform.services.name_moderation import is_name_appropriate
from platform.utils.name_format import format_display_name


# 120+ names/nicknames that must all pass moderation. Mainstream English +
# foreign given/family names + casual nicknames, plus the notorious real names
# that a naive filter false-positives on:
SAFE_NAMES = [
    # English names that collide with slang or contain offensive substrings:
    "Dick", "Richard", "Rick", "Dickie", "Dickson", "Dickinson", "Dickens", "Dicks",
    "Gaylord", "Gaye", "Gayle", "Fanny", "Fannie", "Willy", "Willie", "Willey",
    "Cockburn", "Cox", "Cocker", "Hancock", "Babcock", "Cummings", "Cumming", "Cummins",
    "Scunthorpe", "Assange", "Semen", "Analiese", "Anais", "Titus", "Hoare", "Weiner",
    "Wiener", "Clitheroe", "Bumgarner", "Kuntz", "Kunz", "Bass", "Slade",
    # Mainstream English given + family names:
    "John", "Jane", "Emma", "Oliver", "Amelia", "Noah", "Ava", "Liam", "Sophia",
    "Mason", "Isla", "Jack", "Grace", "Harry", "Charlotte", "George", "Mia",
    "McDonald", "MacArthur", "O'Brien", "O'Neill", "D'Angelo", "Fitzgerald",
    "Llewellyn", "Siobhan", "Aoife", "Niamh", "Macey", "Macklin", "Mack", "Mackie",
    "Mackenzie", "Jean-Luc", "Anne-Marie",
    # Foreign given/family names:
    "Phuc", "Phat", "Phuong", "Nguyen", "Dung", "Bich", "Hoang", "Thanh", "Wang",
    "Dong", "Xiuying", "Jian", "Semyon", "Dmitri", "Sviatoslav", "Bjorn", "Sigrun",
    "Thordis", "Gunnar", "Freya", "Mohammed", "Fatima", "Aisha", "Ali", "Hassan",
    "Yusuf", "Zainab", "Omar", "Layla", "Ibrahim", "Kaur", "Singh", "Priya",
    "Rajesh", "Lakshmi", "Arjun", "Ananya", "Chidi", "Ngozi", "Kwame", "Adaeze",
    "Jose", "Maria", "Guadalupe", "Rocio", "Consuelo", "Joaquin", "Ximena",
    "Rodrigo", "Concepcion", "Giuseppe", "Francesca", "Bjork", "Renee", "Zoe",
    "Francois", "Soren", "Hakon", "Mateusz", "Wojciech", "Olawale", "Temitope",
    "Chiamaka", "Oluwaseun", "Ayodele", "Kofi", "Thabo", "Lerato",
    # Casual nicknames:
    "Bob", "Chuck", "Buddy", "Ace", "Red", "Skip", "Duke", "Peanut", "Boomer",
    "Ziggy", "Coco", "Bubba", "Ho", "Ping",
]

# Clearly-offensive display names that must be blocked (incl. leetspeak and the
# British terms the default list needs supplementing for):
BAD_NAMES = [
    "Fuck", "fuck you", "Shithead", "Cunt", "Asshole", "Bastard", "Motherfucker",
    "Dickhead", "Fuckface", "Wanker", "Twat", "Bollocks", "Bellend", "Arsehole",
    "Shite", "Pikey", "Chink", "Spastic", "Tranny", "Kike",
    "f4ck", "sh1t", "b1tch", "pr1ck",
]


def test_at_least_120_safe_names_covered():
    assert len(SAFE_NAMES) >= 120


def test_no_real_name_is_flagged():
    flagged = [n for n in SAFE_NAMES if not is_name_appropriate(n)]
    assert flagged == [], f"real names were wrongly flagged: {flagged}"


def test_clear_profanity_is_blocked():
    allowed = [n for n in BAD_NAMES if is_name_appropriate(n)]
    assert allowed == [], f"offensive names slipped through: {allowed}"


def test_empty_and_whitespace_pass_moderation():
    # Emptiness is a validation concern, not a moderation one:
    assert is_name_appropriate("") is True
    assert is_name_appropriate("   ") is True


# --- deterministic casing ---------------------------------------------------

CASING_CASES = {
    "mcdonald": "McDonald",
    "MACARTHUR": "MacArthur",
    "o'brien": "O'Brien",
    "van der berg": "Van der Berg",
    "de la cruz": "de la Cruz",
    "jean-luc": "Jean-Luc",
    "anne-marie": "Anne-Marie",
    "henry iii": "Henry III",
    "JOHN SMITH": "John Smith",
    "emma watson": "Emma Watson",
    "macey": "Macey",       # not a Mac patronymic - must not become "MacEy"
    "MACKLIN": "Macklin",
}


def test_deterministic_casing():
    for raw, expected in CASING_CASES.items():
        assert format_display_name(raw) == expected, f"{raw!r} -> {format_display_name(raw)!r}, want {expected!r}"


def test_casing_is_idempotent():
    for expected in CASING_CASES.values():
        assert format_display_name(expected) == expected


def test_casing_empty_input():
    assert format_display_name("") == ""
    assert format_display_name("   ") == ""
