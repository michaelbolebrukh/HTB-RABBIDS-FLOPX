# Platform services

Reusable startup-factory business logic — auth, users, email, payments, inbox, notifications,
storage, and other capabilities meant to survive a startup clone.

## user_service

Owns the user CRUD lifecycle: signup, email verification/reset codes, display-name changes,
avatar upload, and account deletion. Signup rejects the reserved service-account address
(indistinguishably from a duplicate email, so the address is never enumerable). Display names
are deterministically re-cased and moderated (`name_moderation`) on every change except the
first (signup doesn't count, so it's free); renames beyond that are rate-limited per
`NAME_CHANGE_INTERVAL_DAYS`/`NAME_CHANGE_ALLOWANCE`, with `interval=None` meaning unlimited and
`interval=0` meaning self-serve disabled entirely. Avatars are content-sniffed from magic bytes
(never the client-declared content type) before being handed to object storage; the previous
avatar is deleted only after the new one is committed, and only from the uploading user's own
key prefix. `delete_user` explicitly purges the AI service's owner-keyed rows first (that
service is a deliberate FK-less island — a plain string `owner_id`, so the `users` cascade
never reaches it) and best-effort deletes stored avatars after the commit, so a storage hiccup
never blocks account erasure.

## auth_services

`auth_service.py` implements login, refresh-token rotation, and logout on top of
`RefreshSession` rows. Refresh tokens rotate on every use with a short grace window
(`REFRESH_PREVIOUS_JTI_GRACE_SECONDS`) so two near-simultaneous refreshes both succeed;
presenting anything else (an already-superseded token) is treated as reuse/theft and revokes
the whole session immediately. Sessions are capped per user (`MAX_SESSIONS_PER_USER`); over the
cap, the oldest is silently revoked to make room. Two transports share one code path: the
httpOnly refresh cookie (CSRF-checked, since it rides ambiently) and a `X-Auth-Mode: header`
Bearer refresh token for clients that can't hold cookies (no CSRF needed — possession of the
signed token is the credential; the rotated token travels back in the response body instead of
a cookie).

`oauth_service.py` wires Google and Apple via OIDC discovery, each provider registered only if
its client id/secret pair is present (env-gated per provider, not globally). OAuth-created
accounts are auto-verified and skip name moderation (a trusted IdP identity), but the reserved
service-account address is refused here too — as a generic OAuth failure, so it isn't
enumerable via this path either.

## email_services

Jinja2-templated transactional email (verification, password reset, contact notifications,
payment receipts). Templates are generated from React email components in
`next-pwa/scripts/email-build/` — never hand-edit the generated `.html`/`.txt` files here.
`assert_templates_exist()` runs at import time, so a missing template fails the process at
startup rather than at first send. The provider is chosen by `EMAIL_PROVIDER` (Brevo default,
SendGrid supported for existing deployments); marketing-flavoured sends carry RFC 8058
`List-Unsubscribe`/`List-Unsubscribe-Post` headers built from the unsubscribe URLs the caller
supplies, while ordinary transactional sends omit them by omitting those arguments. For what
actually triggers each of the twelve templates — automatic vs manual, duplicate-send risk,
sync vs background, suppression, delayed/scheduled timing, and failure behavior — see
`email_services/DOCS.md`, which tracks this at the per-template level this summary does not.

## contact_service

Contact-form submissions plus the admin notification email. `submit_contact_form` attaches the
signed-in user's token-derived identity (not the editable form fields) to the admin email when
a user is logged in. Notification send is synchronous only when no `BackgroundTasks` is
supplied; otherwise it's queued so the request returns immediately.

## marketing_service

Signed, auth-free marketing consent/suppression, keyed by address (`marketing_suppressions`) so
it covers non-users, mirrored onto `User.marketing_consent` for registered accounts. Two HMAC
token flavours (user-id-based and raw-email-based) share one verify path, both derived only
from `HASH_KEY` so an ops tool (`mailbox-ops`) can mint an identical token client-side with no
shared endpoint call and no new credential. Tokens deliberately never expire — an old email's
unsubscribe link must always work. Also exposes a timestamped, replay-bounded (5-minute skew)
HMAC signature scheme so an ops tool holding `HASH_KEY` can pull the current suppression list
without a dedicated machine credential.

## name_moderation

Offline (`better_profanity`-based) display-name appropriateness check, tuned hard against the
Scunthorpe problem: names are split into whole tokens (never naive substring matching) and any
token on an explicit allowlist (Dick, Cummings, Scunthorpe, Kaur, …) is dropped before the
profanity engine judges the rest. Fails OPEN on any internal error — a moderation-engine hiccup
must never block a legitimate rename.

## organisation_service

Organisations are DATA, not a second authorization primitive: gating stays exactly
`require_capability(...)`, and an organisation's membership only *confers* capabilities onto
its members while the org is active. Conferral is computed at check time via
`effective_capabilities()` — direct grants ∪ grants from every active org a user belongs to —
and is never materialized onto `users.capabilities`, so a conferred grant never needs syncing
or reconciling; revocation is just deleting the membership row. This is the one function every
capability reader (`User.has_capability`, `verify_capability`, `UserResponse`) resolves
through, by design. Only capabilities in `CONFERRABLE_CAPABILITIES` can be granted this way (a
stricter allowlist than `ALL_CAPABILITIES`, since a conferral applies to an open-ended,
later-growing set of accounts). `create_organisation`/`add_member` are idempotent on slug /
(org, user) so an agent-driven one-off script is safe to replay.

## accessibility_service

Thin CRUD over per-user accessibility preferences. `get_preferences` returns an unsaved default
row (not a DB write) when none exists yet, because the response schema forbids `None` on
non-nullable fields that the ORM's `server_default` wouldn't otherwise populate before an
INSERT.

## error_diagnosis

Correlates a Sentry issue's frontend and backend events by the shared `request_id` into one
full-stack snapshot (stacks, frame locals, breadcrumbs), triggered by a Sentry webhook.
Env-gated dormant unless both `SENTRY_WEBHOOK_SECRET` and `SENTRY_API_TOKEN` are set (the
org/projects are auto-discovered from the token, config-minimalism); fully fail-open, and runs
off a background task so the webhook responds immediately regardless of pipeline outcome. **The
final delivery step is a temporary placeholder** — today the snapshot is emailed to the admin
(`notifier.py`); the permanent design routes it into a custom AI diagnosis pipeline being built
separately (single marked call site in `service.py`). Do not build product features on the
admin-email sink. See `.cursor/rules/backend-error-diagnosis.mdc` for the full enable/runbook
detail.

## inbox_service

Admin webmail (read, reply, compose, schedule-send) over two provider-neutral ports
(`ports.py`): a `MailboxProvider` and a `MailSender`. Today both roles run on Forward Email
(one credential, `INBOX_IMAP_*`, no tracking pixels), with Brevo bridging outbound-only sends
via `BridgingSender` while a fresh instance's domain awaits Forward Email's outbound approval —
the moment approval lands, sends silently upgrade with no config change
(`OutboundNotApprovedError` just stops being raised). The whole provider pairing is composed in
`service.py` — the only file that knows which providers are wired, deliberately not an env var
(a single real pairing makes a provider-choice env var a liability, not a convenience).

Inbound HTML is sanitized server-side with `nh3` (`sanitize.py`) before ever reaching the
frontend, which additionally renders it in a sandboxed iframe — two independent layers.
`connections.py` lets an `inbox:credentials` holder reveal the raw mailbox credential or a
one-tap Apple `.mobileconfig`; every reveal is audit-logged (who + which connector, never the
credential) since the credential grants exactly what the caller's existing access already
allows. `scheduled.py` implements schedule-send as an outbox table dispatched by a
5-second-poll daemon thread that optimistically claims due rows and replays them through the
normal send path (so sanitization, threading, and the Brevo bridge all apply identically); a
dispatch failure stays visible as `status=failed` with an ERROR log rather than silently
vanishing. `monitors.py` runs a boot probe (auth check), a periodic full-loop canary (send a
heartbeat out, verify it arrives back in, `INBOX_CANARY_INTERVAL_HOURS`; `0` disables it) to
catch a silently-broken receive path that looks identical to "no new mail", and a new-mail
watcher that pushes web-push alerts to inbox readers using an optimistic compare-and-set
watermark so exactly one worker announces each batch.

## notification_service

Web-push engine (VAPID + `push_subscriptions` + delivery), generalized from an inbox-only
origin to also serve user-facing broadcasts. The VAPID keypair is generated once on first use
and stored in `platform_keys` — no per-instance env var; a concurrent first-boot race across
workers is settled by the DB's primary-key constraint. Delivery is fail-open per device: an
unreachable push service only logs, and a gone endpoint (404/410) prunes its subscription row.
`Audience.INBOX_READERS` targeting is capability-aware — it must include organisation-conferred
`inbox:read` holders, not just direct grantees, so it SQL-narrows to
admins/direct-capability-holders/any-org-member and then applies `has_capability()` in Python
(the conferred set can't be expressed as a portable JSON containment query). `send_broadcast`
additionally accepts an explicit email list as a universal targeting escape hatch for cohorts
no built-in filter covers.

## payment_services

Stripe-backed catalog, checkout, entitlements, and webhook-driven fulfillment.
`stripe_gateway.py` is the sole SDK access point and is deliberately **fail-closed on partial
config**: `stripe_is_configured()` requires both `STRIPE_SECRET_KEY` and
`STRIPE_WEBHOOK_SECRET` together, because an API key alone could take money that webhook-driven
fulfillment could never record — a half-wired state judged worse than fully off.
`validate_stripe_config()` logs an ERROR (Sentry alert) at every boot while exactly one of the
two is set. It also forces IPv4-only DNS resolution (some hosts hand out a black-holed IPv6
route that stalls Stripe calls to the socket timeout) and eagerly touches the SDK's lazy-loaded
resource classes once at single-threaded import time to avoid a `partially initialized module`
race under the threadpool.

`catalog.py` is an in-process registry populated by the product boundary at startup and
validated (duplicate keys, invalid currency/interval, `compare_at_minor` below list price) with
a hard failure rather than a silent bad catalog. `sync_service.py` idempotently reconciles that
catalog into live Stripe Products/Prices/Features at startup, matched by `lookup_key`; removing
a catalog item stops selling it but never archives the Stripe objects. `checkout_service.py` is
the only purchase surface — Stripe-hosted Checkout, no card data or Stripe.js touching this
stack — and re-validates against Stripe directly (not just the local entitlement cache) that a
subscribing user has no non-terminal subscription already, since the local cache can lag behind
payment. Checkout session creation degrades gracefully: if Stripe rejects the cosmetic
`branding_settings` or the bank-statement descriptor suffix, it retries without that one param
rather than blocking the sale. `webhook_service.py` is the **only writer** of
purchase/entitlement state, deduplicated via a `stripe_events` table; on a shared Stripe
account it explicitly scopes itself to events whose `item_key`/subscription belongs to this
instance's own catalog, since Stripe delivers every account event to every registered endpoint
across sibling instances.
`entitlement_service.py`/`reconcile_service.py`/`reconcile_scheduler.py` layer webhooks
(primary, real-time) with an on-read reconcile (empty cache + still-active-subscription) and a
low-frequency background sweep under a Postgres advisory lock (belt-and-suspenders failsafe for
missed deliveries, notably a downgrade a stale-non-empty cache can't self-heal).
`branding_service.py` resolves per-instance Checkout branding (colors/font/logo) from a
frontend-generated manifest so several product instances sharing one Stripe account can each
show their own brand at checkout, degrading to the dashboard brand when the manifest or logo is
absent. **`reset_own_subscription` in `checkout_service.py` is a marked TEMP QA harness**
(gated by `payments:qa-reset`, a capability granted to nobody — effectively admin-only) meant
to be removed before launch, alongside its router route and the frontend's unused
`resetSubscription`; its pricing-page button went with the pricing table on 2026-09-22, so
nothing in the UI reaches it now.

## storage

Pluggable object storage (`ObjectStorage`), auto-detected from config with no manual wiring:
explicit `STORAGE_S3_BUCKET` if set (any S3-compatible provider; `AWS_*` credentials win over
the token derivation on this path), else R2 via one-token derivation when a Cloudflare token
(`CLOUDFLARE_R2_API_TOKEN` or `CLOUDFLARE_API_TOKEN` with R2 Edit) is present — endpoint,
`<APP_DOMAIN-slug>-public/private` buckets and the S3 credential all derived at runtime —
else local disk served from `/uploads`. This is the production path on R2: one token, no
hand-set `AWS_*` or `STORAGE_S3_*` vars. Public URLs come from `STORAGE_S3_PUBLIC_BASE_URL`;
with a custom endpoint (R2/MinIO) and no public base URL the S3 backend fails closed — a boot
ERROR alarm plus a refused public save — rather than concatenating the SigV4 API endpoint into
a link anonymous GETs can never fetch (bare AWS keeps its virtual-hosted public URL form).
`delete_prefix` is the erasure hook object storage exposes to
`user_service.delete_user` and is idempotent (deleting a missing prefix is a no-op); the S3
backend surfaces any per-key deletion failure from a batch delete rather than swallowing it, so
a partial erasure is never silently declared complete.

## document_ingest

Fail-closed **safe PDF ingest** for any product that must store a member-uploaded PDF and
later let someone download it. Pipeline: validate → allowlist rebuild (a new document carrying
each page's marks and the resources they name, text layer intact; `flatten=True` selects the
older rasterising rebuild) with the as-received bytes held only in process memory / ephemeral `/tmp`
→ `save_private` of **safe bytes only** under `{key_prefix}/safe/{uuid}.pdf`. Rebuild
failure raises `DocumentDisarmFailedException` (422); there is no fail-open path that stores
the original. Product consumers (e.g. talent `store_cv`) must call `ingest_private_safe_pdf`
and must not `save_private` as-received upload bytes. The sibling `document_sanitize.sanitize_pdf`
helper is a structural strip that **fails open** — it is not a share-plane gate, has no production
caller, and must not be wired as one. Inject `rebuild=` in tests to fake the seam without
exercising PDFium.

Two resource bounds are load-bearing rather than incidental, because a PDF declares its own page
geometry and the caller cannot see it before rendering. The render scale is clamped per page, so a
tiny file claiming a 200-inch page cannot rasterise to gigabytes; and `rebuild_limiter` caps how
many rebuilds run at once, because the peak is per-rebuild and the work runs in a thread pool that
would otherwise happily run forty. A caller offloading `ingest_private_safe_pdf` to a thread must
pass that limiter — the bound lives with the capability precisely so a new consumer inherits it
instead of rediscovering the OOM.

## question_flow_service

Structured question sets — onboarding, surveys, intake — captured as append-only answer
snapshots. Flow DEFINITIONS are code, registered at import time from the product boundary
(`platform/question_flows.py` is the registry; `product/question_flows.py` holds this
instance's flows); only ANSWERS are data. There is no definitions table, so a flow ships and
rolls back with the deploy that changed it and can never drift from the schema validating it.

**Flows are granular: an account is asked only the questions it has not answered.** That is
the property the whole design turns on, and it is what makes a flow safe to extend after
launch. The questions ARE the fields of the flow's pydantic answers model — the field name is
the question's stable id, and declaration order is the order they are asked — so there is no
second list to keep in step. A question counts as answered when the account's latest snapshot
holds a value under that id **which still validates against that question's current
annotation**. Nothing is stored per question; it is derived on read (`questions.py`, the one
definition, which every gate and summary resolves through).

Four behaviours fall out of that single rule, with no schema change and no migration behind
any of them. **Adding a question**: nobody has a value for the new id, so it is outstanding
for existing members and new ones alike, and everything already answered stays answered.
**Breaking a question's vocabulary** (retiring an answer option): the stored value stops
validating, so that one question goes outstanding again while its neighbours are untouched —
the granular replacement for "bump the version and make everyone start over". **Removing a
question**: it is asked of nobody and reported to nobody, and old rows keep the value
unread. **Retiring a whole flow**: its snapshots have no current schema to owe anything
against, so they report complete rather than re-gating people over something that no longer
exists. A field with a default is an OPTIONAL question — offered, reported when answered,
never outstanding — which is how to add a question without interrupting existing members.

**Submissions are partial and merge forward.** A client posts whatever subset it just asked
(for an existing member after a question is added, that is one question). Each value is
validated against its own question, the account's still-valid stored answers are merged
underneath, and a NEW row is appended holding the merged position. Append-only therefore
survives partial answering, and nothing is lost by answering one thing. When the merge covers
every required question the merged set is validated against the full model as well, so a
complete snapshot is normalised exactly as it was before flows were granular (defaults
filled, model-level validators run).

**`version` is a stamp, not a gate.** It used to be a 409 on mismatch. On an additive change
that is precisely wrong — it refuses answers that are still perfectly valid because a question
appeared behind an open tab. A stale client is now caught per question instead (an unknown id,
or an answer whose option was retired, is one 422 entry naming that question), and everything
still valid is kept. The integer survives as the contract GENERATION: recorded on every row so
history says which contract wrote it, and used by clients to invalidate a local draft. Bump it
only when a change makes an existing STORED answer unreadable. **Adding a question is not such
a change; do not bump for one.**

Two reads serve the API, and the difference between them is load-bearing. `completion_summary`
is keyed on flows the account has ANSWERED at least once, so a never-touched flow is absent —
which is exactly how the existing frontend gate reads "never onboarded", and why
never-answered flows must not be added to it. `question_progress` covers every REGISTERED
flow including ones with no rows, so a client deciding what to ask can read the outstanding
list for a brand-new account. They surface as `UserResponse.flows` and
`UserResponse.flow_questions`, both computed only by `GET /user/` and null everywhere else
(null means "not computed", never "not completed" — otherwise a login response would bounce a
finished account back through onboarding).

Reminders were deliberately NOT re-pointed at outstanding questions. The sweep's audience is
still "never submitted anything for this flow", because "has something outstanding" would mail
the entire membership every time a question was added — the blast the registry's
`remind_accounts_created_after` epoch guard exists to prevent. Registering reminder stages
without that epoch is refused at import time.

The one module that deletes flow state is `qa_reset.py`, a temporary QA harness behind
`flows:qa-reset` (granted to nobody). It is also what the product's operator reset calls to
clear onboarding, rather than writing its own deletion SQL — so the append-only carve-out
stays one deletable file. `leads.py` is a dormant pre-account capture module that 404s unless
a flow opts in; read the warning at the top of the router's lead endpoint before enabling it.

**Adding a question, end to end:** append a field to the flow's answers model with a new
stable id and a closed answer vocabulary, and build the frontend page keyed on that same id.
There is no migration, no version bump, and no backfill. Existing members are asked the one
new question next time the gate runs; new accounts are asked all of them.

## link_service

Stable short-link registry for NFC/QR/campaign URLs: public `key` → mutable
`destination` (+ optional operator `label`). Resolve fails closed on missing or
inactive keys (inactive looks like missing — no 410 oracle). Create/update
validate slug keys (reserved words blocked) and destinations (same-origin `/…`
paths or absolute `http(s)` only — the open-redirect boundary). Soft-off via
`is_active` for temporary pause; hard `DELETE` removes the row when an operator
intentionally recycles or abandons a key. HTTP surface:
`platform/routers/links.py` (public resolve + `links:manage` CRUD including DELETE).

## generative_ai_service

A large, actively-specified provider-independent AI platform layer (chat, tools, structured
output, streaming, cost-metered budgets, a durable transcript store, RAG, agent-authored
memory, realtime voice, a sandboxed code/skill runtime) built on Pydantic AI behind a strict
facade boundary. It is genuinely extensive — do not duplicate its documentation here; read
`README.md` (goals, finalized decisions, phase status) in this directory first, then
`PHASE1.md`–`PHASE9.md`, `MEDIA_PROVIDERS.md`, and `memory/MEMORY.md`/`memory/BENCHMARK.md` as
needed. **Status: mounted (its router is included in `main.py` via the platform router
aggregation) but has no frontend consumer** — nothing in `next-pwa` currently calls its `/ai/*`
endpoints. Treat it as built-but-unwired platform capability, not as a live product feature.
