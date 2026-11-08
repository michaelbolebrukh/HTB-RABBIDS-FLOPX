"""Product-owned organisation seed data: FLOPX ships none.

infrastructure/seeder.py reads `SEEDED_ORGANISATIONS` at boot and logs
"No organisations declared for seeding; skipping." for an empty list. The
organisations capability stays dormant platform machinery for this instance
(brief F15). `SPONSOR_TIER_CAPABILITIES` keeps the documented shape (tier name
-> conferrable capabilities) and is empty for the same reason.
"""

SPONSOR_TIER_CAPABILITIES: dict[str, list[str]] = {}

SEEDED_ORGANISATIONS: list[dict] = []
