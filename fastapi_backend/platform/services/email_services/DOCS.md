# Email services — trigger, suppression, timing, failure

This is the operating reference for the registered `EmailTemplateType` members. It intentionally
skips what the happy path already tells you (`user_verification` sends on signup); it exists to
record what would surprise someone who only read that path. Every claim below was confirmed
against the call sites, not inferred — where a call site did not answer a question, that gap is
stated rather than filled with a guess.

`email_sender.py` renders nothing; it only submits to the configured provider (Brevo default,
SendGrid fallback) and raises `EmailSendException` on any failure. `__init__.py`
(`send_templated_email`/`render_template`) does the Jinja2 render and subject extraction. Neither
module has retry logic anywhere — a provider failure is exactly one attempt, and every caller
below decides for itself whether that failure is swallowed, logged, or propagated.

Local brand images render against the backend deployment's normalized `PUBLIC_FRONTEND_ORIGIN`, which already falls back to `https://APP_DOMAIN`. The generator reserves `email_asset_origin` only for those image sources, and the renderer overrides any caller value. Explicit external/CDN assets keep their authored hosts. Recipient text, subjects, plaintext and CTA/home/legal links are not rewritten. This lets a branch backend use its matching frontend artwork before that artwork is published at the production domain; verify the image URLs emitted by a real local render against the deployed frontend without sending mail.

## The matrix

The frontend registry and admin **Email templates** gallery cover every enum member. The generic `privacy_policy_change` notice is manual-only and has no automatic trigger. Render it with `name`, `effective_date`, `change_summary` (plain text), and `policy_url` (approved absolute HTTPS URL); approve notice content/date before any send. Preparing it neither sends mail nor activates proposed legal wording.

| Email | Trigger (who/what) | Automatic/manual — duplicate risk | Sync/background | What suppresses it | Timing | On send failure |
|---|---|---|---|---|---|---|
| `user_verification` | Signup (`create_user`/`create_passwordless_user`) or a signed-in unverified user calling `POST /user/request-verification` | Automatic on signup; the resend endpoint is user-initiated but reachable by ANY signed-in account regardless of verification state (see note below) — cooldown (`EMAIL_VERIFICATION_COOLDOWN`) plus a route rate limit (1/s, 5/min, 15/hr, 50/day) is the only thing stopping repeats | Background task (`BackgroundTasks.add_task`) | Nothing — always sent; not suppression-eligible | Immediate | Swallowed: `send_user_email` catches everything and logs (`logger.error`, by user id only); the cooldown timestamp is already committed before the send is attempted, so a failed send still blocks the user from retrying until the cooldown expires |
| `password_reset` | `POST` reset-request endpoint → `request_password_reset` | Automatic; non-enumerating (unknown email returns a generic cooldown response with nothing sent, indistinguishable from a real send) — cooldown (`RESET_PASSWORD_EMAIL_COOLDOWN`) gates repeats | Background task | Nothing address-level; a nonexistent account is a silent no-op (by design, not a suppression) | Immediate | Same as above: swallowed and logged, cooldown already committed |
| `contact_admin_notification` | Contact form submission (any visitor, signed in or not) | Automatic, one per submission; no dedupe needed since it is direct user action, not a retryable delivery | Background when the router supplies `BackgroundTasks` (it always does); synchronous only in the untested from-code path with no `BackgroundTasks` | Nothing — internal alert, not consent-gated | Immediate | Raises `ContactFormUnavailableException` inside `send_contact_admin_notification`. When queued as a background task, that exception surfaces only as a framework-level log after the HTTP response (already 200, submission already committed) — the submitter is told it worked and the admin can silently never see it (see Findings) |
| `admin_notification` | Generic operator-alert template; currently has exactly one live caller, the interview-question low-runway alarm (`interview_daily._maybe_send_low_runway_alert`) | Automatic, system-detected (runway threshold crossed while serving a daily question) — de-duplicated to **one per UK calendar day** by a DB unique constraint on `InterviewRunwayAlert.day_key`, claimed by INSERT before the send, so two concurrent requests crossing the threshold cannot both send | Synchronous and RAISES on failure (`send_admin_notification` deliberately does not swallow); every caller wraps its own failure policy | Nothing — CONTACT_EMAIL only, no consent state | Runs inline inside whatever request path calls it; for the runway alarm that is "each time the daily question is resolved," fail-open end to end (see next column) | The runway alarm wraps the whole call in `try/except Exception` and logs — a failed send costs that day's warning only; the day's claim row still stands so nothing resends and nothing retries until tomorrow's threshold check |
| `payment_order_confirmation` | Stripe webhook `checkout.session.completed`, one-time item, `payment_status == "paid"` | Automatic; gated by `paid and is_new` where `is_new` is "no `Purchase` row existed for this checkout session before this handler ran" — see the cross-cutting webhook risk in Findings | Fire-and-forget daemon thread (`notification_service._dispatch`), not a `BackgroundTasks` task — detached from the request/DB session entirely | Nothing — transactional, sent "regardless of marketing consent" per the module's own docstring; never checked against `marketing_suppressions` | Immediate (inside webhook processing, off-thread) | Exception caught inside the thread and logged (`logger.exception`); nothing surfaces to the webhook response (which has already returned or is about to) and there is no retry — a failed send is a permanently lost receipt unless an operator notices and resends manually |
| `payment_subscription_welcome` | Same webhook event, subscription item | Same `paid and is_new` gate | Fire-and-forget daemon thread | Nothing — transactional | Immediate | Same as above |
| `payment_subscription_renewal` | Stripe webhook `invoice.paid` with `billing_reason == "subscription_cycle"` (the initial invoice is `subscription_create` and is explicitly excluded — it is covered by the welcome email instead) | Automatic; gated by the outer per-event dedup (`stripe_events` table) only — the handler itself has no per-purchase "already emailed this invoice" guard | Fire-and-forget daemon thread | Nothing — transactional | Whenever Stripe delivers the renewal invoice event | Same fire-and-forget swallow |
| `payment_refund_confirmation` | Stripe webhook `charge.refunded` | Automatic; same outer per-event dedup only, no handler-level guard | Fire-and-forget daemon thread | Nothing — transactional | Whenever Stripe delivers the refund event; a partial refund followed by a second partial refund is two distinct Stripe events and legitimately sends two emails | Same fire-and-forget swallow |
| `talent_cv_received` | The 24h-delayed talent-programme nudge sweep (`talent_nudge_scheduler` → `talent_service.send_due_nudge_emails`) — **not** an immediate CV-upload receipt despite the template name (see Findings) | Automatic, system-scheduled | Runs synchronously inside the sweep tick (which itself runs off the event loop via `asyncio.to_thread`); not a per-request `BackgroundTasks` task | **Both** the talent programme's own withdrawal (query requires a live `CvDocument` + unrevoked `CvShareConsent`) **and** the separate marketing-suppression list (`marketing_service.is_suppressed`, checked explicitly per recipient) — an unsubscribed member's row is still claimed (see Timing) but the email is skipped | Sent once, 24h after CV submission (`NUDGE_DELAY_HOURS`), only inside a 09:00–18:00 UTC send window (`NUDGE_SEND_WINDOW_UTC`) — a submission just before the window closes can wait up to ~15h for its first eligible sweep tick, not exactly 24h | Two-layer duplicate protection: a PostgreSQL advisory lock (`_LOCK_ID`) ensures only one worker/instance runs a given sweep tick, AND each row's `nudge_email_sent_at` is claimed (set + committed) **before** the send is attempted, per-row, not batched — so a crash mid-batch costs unsent emails for the remaining rows, never a duplicate for a row already claimed. A claim is never reversed: if `is_suppressed` is true the claim still stands and the email is permanently skipped, not deferred. `send_user_email`'s swallow-and-log applies here too — a provider failure after the claim is committed is a silently lost nudge, logged by user id, with no retry |
| `payment_order_notification` (operator alert) | Same `checkout.session.completed` event, one-time item | Same `paid and is_new` gate as the customer email — both customer and operator emails share one gate, so they can only ever double-send together, never independently | Fire-and-forget daemon thread | Nothing — internal ops alert | Immediate | Same fire-and-forget swallow |
| `payment_subscription_notification` (operator alert) | Same event, subscription item | Same gate | Fire-and-forget daemon thread | Nothing | Immediate | Same fire-and-forget swallow |
| `payment_dispute_notification` (operator alert) | Stripe webhook `charge.dispute.created` | Automatic, outer per-event dedup only; sent unconditionally even when the local `Purchase` row cannot be matched (a dispute needs a timely response either way) — **Stripe also emails the account owner directly for the same dispute**, so seeing two separate dispute emails (ours + Stripe's native one) is expected, not a bug | Fire-and-forget daemon thread | Nothing | Immediate | Same fire-and-forget swallow |

## Non-obvious behaviors worth knowing before you touch this area

- **OAuth signup never sends `user_verification`.** `find_or_create_oauth_user` sets
  `is_verified=True` at creation (the IdP already asserted the email), so the entire
  verification-email code path is dead for Google/Apple signups. Only password-based signup and
  the passwordless-funnel path (`create_passwordless_user`, e.g. the talent landing page) send it.
- **The verification-resend endpoint does not check `is_verified`.** `unverified_user_dependency`
  means "verification is not REQUIRED to call this," not "the caller must be unverified" — it is
  the same relaxed auth dependency `GET /user/` uses. An already-verified, signed-in account can
  call `POST /user/request-verification` and receive a fresh code; entering it just re-sets
  `is_verified=True` (a no-op). Nothing downstream in the service layer re-checks verification
  status either. Bounded only by the cooldown and the route's rate limit — see Findings.
- **The cooldown clock starts on attempt, not on delivery.** For both `user_verification` and
  `password_reset`, `last_verification_email_sent`/`last_reset_password_email_sent` is written and
  committed synchronously, before the background task that actually calls the provider even runs.
  If the provider send then fails (network blip, provider outage), the user has received nothing
  but is locked out of retrying until the cooldown lapses — indistinguishable, from the user's
  side, from an email that is simply slow.
- **`talent_cv_received` is not a CV-upload receipt.** Nothing sends an email at CV upload time;
  `store_cv` is a pure DB/storage operation. The only email this template ever appears on is the
  next-day nudge. A reader going by the template name would reasonably expect an immediate
  confirmation and would be wrong.
- **Payment emails are two delivery mechanisms wearing one name.** `_dispatch` in
  `notification_service.py` starts a **daemon thread** per send (`threading.Thread(..., daemon=True)`),
  not a FastAPI `BackgroundTasks` job. It carries no DB session (deliberately — the webhook request
  may finish and its session close before the thread completes) and there is nothing that waits
  for it, retries it, or records that it ran. A worker process restart/redeploy mid-send silently
  drops the email with no trace beyond an ordinary shutdown log.
- **Transactional payment emails are explicitly exempt from marketing suppression**, per the
  module's own docstring — a customer who unsubscribed from marketing still gets order
  confirmations, welcomes, renewals, and refund receipts. Only the talent nudge (of the twelve)
  checks `marketing_service.is_suppressed`.
- **The talent nudge send window can turn "24 hours" into up to ~39 hours** in the worst case: a
  submission at 18:01 UTC is 24h-due at 18:01 the next day, one minute after that day's window
  closes, so it waits for the following day's 09:00 window to open — roughly 15 extra hours on
  top of the nominal delay. This is a property of the fixed daytime window, not a bug, but is easy
  to get wrong when reasoning about "when will this member's nudge arrive."
- **The interview low-runway `admin_notification` claim is committed before the send, not after.**
  A failed send on a given day is not retried that day — the claim row stands, so the alarm is
  silent until tomorrow's threshold re-check. This is deliberate (documented in
  `interview_daily.py`) to avoid a resend storm from every request for the rest of the day, but it
  means a single bad send during a genuinely low-runway period can mean a full day passes with no
  visible warning.
- **`error_diagnosis`'s admin email is not one of the twelve templates.** It calls
  `email_sender.send_email` directly with hand-built HTML/text, bypassing `send_templated_email`
  and `EmailTemplateType` entirely. It is a temporary scaffold (see
  `platform/services/error_diagnosis/notifier.py`) unrelated to this template set — mentioned here
  only so it is not mistaken for a thirteenth template when auditing call sites.

## Could not confirm from code

- Whether the configured email provider (Brevo/SendGrid) performs any retry or queuing on its own
  side after this backend's single `send_email` call returns success/failure is outside this
  repository's code and was not checked.
- Whether Starlette/the ASGI server logs, reports to Sentry, or silently drops an exception raised
  from inside a `BackgroundTasks` callback (relevant to `contact_admin_notification`'s
  `ContactFormUnavailableException` and to `user_service.send_user_email`'s already-caught
  exceptions, which never reach that layer) was not traced through the framework internals here —
  stated as "framework-level" above rather than asserting a specific outcome.

## Known defects in this area

Recorded here rather than in a review comment, because the next person to touch
this code needs them and a review comment does not survive.

1. **The Stripe-webhook emails are not atomic with the event being marked
   processed.** Each handler starts its fire-and-forget send thread BEFORE the
   surrounding commit. If that commit fails for any reason other than the
   already-handled IntegrityError race, the transaction rolls back including the
   `Purchase` row the dedup depends on, Stripe retries the same event, and the
   handler runs again and sends a second receipt. The window is narrow and it has
   not been observed; the design is what makes it possible.
2. **A failed contact-notification send is invisible.** No retry, no dead letter,
   no admin-visible marker beyond a log line, while the visitor has already been
   told their message went through.

A third was found and is fixed: `request_user_verification` did not check
`is_verified`, so a verified account could keep receiving verification codes.
See `user_service.request_user_verification` and
`platform/services/tests/test_verification_resend.py`.
