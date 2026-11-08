"""The single partnership-status registry: every valid state a partnership holds.

The same idiom as the capability registry (platform/capabilities.py): a tuple of
plain strings validated in Python, never a database enum. The column is a plain
String on purpose — a partnership row is a record of a DECISION, and historical
rows must keep the state they were written with even if the workflow later gains
or drops a step. A DB enum would turn that into a migration every time.

The backend never interprets these strings beyond validating them. What a status
MEANS to a reader (which surface it unlocks, what copy it renders) is a frontend
decision; the same is true of `season`, `offering_label` and `snapshot_key`,
which the backend stores verbatim and never parses.

Expiry is deliberately NOT one of these states. `ends_on` vs today is computed at
read time (Partnership.is_expired / days_remaining); moving a partnership to
"ended" is always an explicit act, so the row records a decision rather than a
clock tick.
"""


# Every valid partnership status, in workflow order:
PARTNERSHIP_STATUSES: tuple[str, ...] = ("draft", "sent", "signed", "ended")

# What a partnership starts as when the caller does not say:
DEFAULT_PARTNERSHIP_STATUS: str = PARTNERSHIP_STATUSES[0]

# The one status with a MEANING the backend acts on: a draft is our internal
# thinking about a firm and is withheld from that firm's own people until
# somebody moves it on. Named separately from the default it happens to equal —
# "what a new row starts as" and "what we do not show a sponsor yet" are two
# decisions, and only one of them would move if the workflow gained a step:
DRAFT_PARTNERSHIP_STATUS: str = "draft"
