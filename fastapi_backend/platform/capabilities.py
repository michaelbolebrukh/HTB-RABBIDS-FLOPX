"""The single capability registry: every named authorization grant in the system.

Authorization is one primitive — `require_capability("<namespace>:<verb>")` — and
these constants are the only valid grant strings; endpoints never gate on free
strings or on role directly. Grants are per-account string lists stored in
`users.capabilities`; admin is the catch-all break-glass identity that passes
every capability check implicitly (see `verify_capability`). Agents validate
grant strings against ALL_CAPABILITIES before writing them to the DB — a typo'd
grant silently gates nothing.
"""

# Admin inbox (mailbox content is PII-sensitive beyond "operational"):
INBOX_READ = "inbox:read"
INBOX_WRITE = "inbox:write"
# Exposing the mailbox credential itself; granted to nobody -> admin-only:
INBOX_CREDENTIALS = "inbox:credentials"

# Platform operator surfaces:
USERS_READ = "users:read"
CONTACT_MANAGE = "contact:manage"
MARKETING_MANAGE = "marketing:manage"
PAYMENTS_RECONCILE = "payments:reconcile"
PAYMENTS_QA_RESET = "payments:qa-reset"
# TODO TEMP (QA harness) — the question-flow reset, granted to nobody -> admin-only.
# Remove with POST /flows/{flow_key}/test/reset and question_flow_service/qa_reset.py:
FLOWS_QA_RESET = "flows:qa-reset"
# Seeded onto the service account so agents can push announcements:
NOTIFICATIONS_BROADCAST = "notifications:broadcast"
# Create/update partnership records; view all partnerships. Seeded onto the
# service account too, so an agent can mark a partnership signed over the API
# (the designed mechanism — see the access-control rule) instead of by hand:
PARTNERSHIPS_MANAGE = "partnerships:manage"

# Content preview: prerequisite-gate bypass + paid-entitlement bypass, for
# sponsor/demo accounts:
FEATURE_PREVIEW = "feature:preview"
# Unreleased surfaces (early-access pages/features):
EARLY_ACCESS = "feature:early-access"
# Dev-facing utilities; granted to nobody -> admin-only:
INTERNAL_TOOLS = "internal:tools"
# Instance brand-kit download surface (logos, profile GIF, favicons); granted to nobody -> admin-only:
BRAND_ASSETS = "brand:assets"

# Product-owned consumers keep their constants here too — one registry beats
# boundary purity for auth strings (see the access-control rule). The current
# instance declares none: its write surfaces gate on the signed-in user, and a
# capability is added here only with its first real consumer.

# Managed short links (NFC/QR/campaign redirects); granted to nobody ->
# effectively admin-only until an operator is explicitly granted:
LINKS_MANAGE = "links:manage"

# Every valid grant string, for tests and for agents validating writes:
ALL_CAPABILITIES = frozenset({
    INBOX_READ,
    INBOX_WRITE,
    INBOX_CREDENTIALS,
    USERS_READ,
    CONTACT_MANAGE,
    MARKETING_MANAGE,
    PAYMENTS_RECONCILE,
    PAYMENTS_QA_RESET,
    FLOWS_QA_RESET,
    NOTIFICATIONS_BROADCAST,
    PARTNERSHIPS_MANAGE,
    FEATURE_PREVIEW,
    EARLY_ACCESS,
    INTERNAL_TOOLS,
    BRAND_ASSETS,
    LINKS_MANAGE,
})


# Capabilities an ORGANISATION may confer on its members. Deliberately a narrow
# subset of the registry: a direct grant is one reviewed act per account, whereas
# a conferred grant applies to an unbounded and later-growing membership, so
# operator capabilities (inbox, users, payments, partnerships) are never
# conferrable - they stay one-account-at-a-time decisions.
CONFERRABLE_CAPABILITIES = frozenset({
    FEATURE_PREVIEW,
    EARLY_ACCESS,
})
