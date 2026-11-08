# Infrastructure

Runtime plumbing for the backend — boot sequence, DB/migrations, settings, caching, rate
limiting, request correlation/error tracking, service-to-service auth, and boot-time seeding —
kept free of product-specific behaviour.

## startup

`run_startup_tasks()` (called once from `main.py`) is the ordered boot sequence: run migrations
under an advisory lock, validate the admin-inbox config (loud ERROR on half-configuration,
never silent), start the inbox monitor daemon threads, then run the DB-backed boot tasks —
admin seeding, service-account seeding, test-user seeding, then `product.startup.STARTUP_TASKS`
— serialized under a second, separate advisory lock (`run_with_advisory_lock`, see `database`).

Migrations run **in-process at boot**, not as a separate pre-deploy step —
`create_and_migrate_with_lock()` is the first call this module makes. Every startup task is
wrapped in try/except-and-log by `_run_startup_db_task`, so a failing task never stops boot: a
malformed content file or a broken product startup hook boots fine and only fails later, at
request time, when something needs what it should have set up. Infrastructure reaches product
code only through `product.startup.STARTUP_TASKS`; it carries no product-specific logic itself.

## database

Owns the engine, `SessionLocal`, the declarative `Base`, and migration mechanics.
`create_and_migrate()` creates tables (idempotent) then runs Alembic to `head`, first clearing
any orphaned `alembic_version` rows with no matching local script — handling a renamed/removed
migration from before real data existed, which otherwise makes Alembic refuse to upgrade.

`create_and_migrate_with_lock()` wraps that in a PostgreSQL session-level advisory lock
(`pg_advisory_lock`, keyed by a CRC32 of `DATABASE_URL`) so concurrent gunicorn workers don't
race the migration; SQLite has no such primitive, runs as a single worker, and migrates
directly with no lock. `run_with_advisory_lock(name, work)` generalizes the same mechanism to
other boot-time DB tasks that are idempotent but not concurrency-safe (seeders, content sync):
without it, every worker on a fresh database finds the same row missing at once and races to
insert it, and the losers die on a unique constraint — a real failure mode that used to
self-heal only on the next deploy. This module also flips on SQLite's `PRAGMA foreign_keys=ON`
per connection (off there by default; an orphaned row is no longer just stale data now that
organisation membership *confers* capabilities — a recycled row id could otherwise inherit a
departed member's grants), and wires cache invalidation onto the session factory (see `cache`).

## settings

The single `Settings` (pydantic-settings) object, instantiated once as the `settings` singleton
at import time. Precedence for a derived value is **env > generated instance manifest >
code-level derivation > hard code default** — e.g. `SENDER_NAME`: explicit env wins, else the
manifest's brand name (see `instance_manifest`), else derived from `APP_DOMAIN`.
`DATABASE_URL`, `HASH_KEY`, and `APP_DOMAIN` are the only genuinely required values; nearly
everything else has a code default or is derived from them via `derive_domain_defaults` (a
`model_validator`), which fills in `PUBLIC_FRONTEND_ORIGIN`, `CONTACT_EMAIL`,
`INBOX_IMAP_EMAIL`, `SENDER_NAME`, and `SESSION_HINT_COOKIE_DOMAIN` when not set explicitly.

`validate_instance_domain()` runs once at import, against the app's `settings` singleton only
(never the ad-hoc `Settings()` instances tests construct with throwaway domains), and **fails
closed**: if `APP_DOMAIN` disagrees with the instance manifest's domain outside
development/local hosts, it raises rather than let a deploy point at the wrong domain. Feature
areas (Stripe, the admin inbox, Sentry, AI providers, object storage) are gated by the presence
of their own config, never a boolean flag.

## seeder

Boot-time account provisioning, run under `startup`'s advisory lock. Every seeder here is
**idempotent and add-only**: reruns never duplicate a row, and existing rows only gain flags or
grants — nothing is revoked as a side effect of booting.

`seed_admin_user_if_configured` seeds one admin per email in `settings.admin_emails` (the
merged union of `ADMIN_EMAIL` and `PRODUCT_ADMIN_EMAIL`); a row that is unverified **and**
password-bearing is never promoted, since that would hand the admin identity to whoever
registered the address first without proving mailbox ownership.
`seed_service_account_if_configured` seeds `service@<APP_DOMAIN>` whenever `SERVICE_TOKENS` is
set — headless (no password), seeded with `notifications:broadcast` — and likewise refuses to
adopt a password-bearing row at that address. `seed_test_user_if_configured` seeds/refreshes a
verified test account from `TEST_USER_EMAIL`/`TEST_USER_PASSWORD` for Playwright.

## service_auth

The machine-credential model for agent/ops access, deliberately separate from human sessions. A
bearer token is checked against the `SERVICE_TOKENS` CSV (one token revocable without rotating
the rest) via `match_token`, which compares against **every** configured token with
`hmac.compare_digest` and does not early-exit — response timing never leaks which token matched
or how close a wrong guess got.

A match resolves to the fingerprint `sha256(token)[:12]`, logged and attributed instead of the
token itself. The fingerprint resolves (via `require_capability`) to the persisted
service-account `User` row seeded by `seeder`; if `SERVICE_TOKENS` is set but that row doesn't
exist yet, `get_service_account` fails closed — logs and returns `None` — rather than minting
an identity on the fly. It is deliberately **not** signed with `HASH_KEY`: a leaked service
token only reaches whatever capabilities that account holds, never auth-token forgery or
unsubscribe/suppression signing, which live on a wholly separate secret.

## cache

One `cache` singleton, backend chosen purely by `CACHE_URL` (unset = bounded in-process LRU
memory; `redis://...` = shared Redis/Valkey) — never a code decision. Default invalidation
(`tags=Cache.AUTO`) auto-tags an entry with every DB table its loader read (tracked via
SQLAlchemy session events) and bumps that tag on any committed write to the table — O(1)
invalidation, no key scanning. `tags=None` is an explicit TTL-only opt-out for aggregates over
write-hot tables where table-tags would thrash the hit rate.

Every entry always carries a TTL ceiling regardless of tagging — the backstop for missed
invalidation and for writes that bypass the ORM, invisible to auto-tags. **Fail-open
throughout**: any backend error is logged at WARNING and the caller falls back to computing
uncached — a broken cache must never take the app down. With the memory backend each worker
holds its own cache, so a tag bump in one worker never reaches its siblings and TTL becomes the
cross-worker staleness bound; Redis makes both shared. `CacheHeaderMiddleware` stamps `X-Cache:
HIT|MISS` on responses.

The Redis client's connection pool is bounded (`RedisBackend._MAX_CONNECTIONS = 20`), because
redis-py's own inherited default (100 in the pinned version) is not safe here: with 2 gunicorn
workers and two independent redis clients in this codebase (this one and `rate_limiter`'s, each
with its own pool), the unbounded worst case is 2 × 2 × 100 = 400 connections against the Render
Key Value store's 250-connection ceiling — a stalled store amplifying into a store-side
connection-limit outage for every client on the account, not just this app. At 20 each, the
worst case is 2 × 2 × 20 = 80, comfortably under a third of the ceiling. The bound is deliberately
the plain (non-blocking) `ConnectionPool`, not a `BlockingConnectionPool`: exhausting it raises
`redis.exceptions.MaxConnectionsError` (a `ConnectionError` subclass) immediately rather than
waiting for a freed slot, so it cannot reintroduce the event-loop stall the socket timeouts exist
to prevent, and it is caught by the same fail-open `except Exception` paths as any other redis
error. Pinned by `infrastructure/tests/test_pool_max_connections.py`, which builds the real
`RedisBackend` client and proves the bound reaches it.

## rate_limiter

A `slowapi` `Limiter` keyed by user id (decoded from the Authorization bearer, when valid)
falling back to remote IP. Storage is `CACHE_URL` when set, else in-process memory — the same
env var and cross-worker caveat as `cache`: limits are exact only with a shared store,
otherwise the effective limit scales with worker count. `swallow_errors=True` and
`in_memory_fallback_enabled=True` make it fail-open — a dead shared store degrades to
per-worker in-memory counting rather than blocking requests.

Fail-open only helps once failure arrives, though, and that is why the store call carries an
explicit half-second `socket_timeout`/`socket_connect_timeout`. `slowapi`'s middleware calls the
store **synchronously from inside the event loop**, so the call does not stall one request — it
stalls every concurrent request on the worker. Left on redis-py's inherited 5-second read
timeout, a store that accepts connections and then stops answering serialises the worker into
5-second blocks, which is long enough to fail a health probe on its own; that is how a degraded
cache took production down on 2026-08-10 (the host read the failing probe as a dead service and
replaced the instance). For the same reason `/healthz` and `/readyz` are `@limiter.exempt`: a
liveness probe that consults a shared cache is not reporting on this process. Both properties
are pinned by `infrastructure/tests/test_rate_limit_store_isolation.py`, which reproduces the
hung-but-listening store rather than mocking it.

Be careful what that buys, because the overstated version of it is the tempting one. During an
outage the store is not simply bypassed: slowapi marks it dead and **re-probes it on a widening
backoff**, and since the call blocks the event loop rather than one request, an exempt probe
that lands in the same tick is still delayed — measured at ~0.5s against a 5s probe budget. So
a sick store degrades the app rather than taking it down, which is the achievable property; it
does not become invisible. Related, and worth not "optimising" away: `headers_enabled=True`
makes a second store call (`get_window_stats`) after each check, and it is safe only because
`_storage_dead` is already set by then, so it resolves to the in-memory fallback — an ordering
that current library behaviour gives us rather than an invariant slowapi guarantees.

Its store's connection pool is bounded the same way and for the same reason as `cache`'s
(`_STORE_MAX_CONNECTIONS = 20`, passed via `storage_options` since the `limits` library forwards
that dict straight to `redis.from_url`) — see `cache`'s paragraph above for the arithmetic against
the 250-connection store ceiling and why the non-blocking pool is the deliberate choice.
`infrastructure/tests/test_pool_max_connections.py` proves the bound reaches `limiter`'s own
`_storage_options` and the real storage object built from it, not just that the constant exists.

## observability

Composes logging config, Sentry error tracking, and request correlation for `main.py`.
`configure_logging()` sets a log format that always includes the current request id (via a
filter reading a contextvar) and installs the `log_noise` filter on the gunicorn logger.

Sentry is strictly env-gated on `SENTRY_DSN`: unset means the SDK never initializes and nothing
else changes; a bad DSN or init failure is caught and logged at WARNING rather than raised —
fail-open, the same contract as `cache`. `send_default_pii=False` throughout — never request
bodies, cookies, or emails; only the numeric user id is attached, via `set_user()`.
`RequestIDMiddleware` accepts a well-formed inbound `X-Request-ID` or generates one, stores it
in a contextvar for the request's life, echoes it on the response, and tags it onto any Sentry
event via `before_send` (works even for an unhandled 500 captured above the middleware).

## log_noise

Gunicorn logs a normal graceful-shutdown SIGTERM (emitted on every deploy) at ERROR, which
would otherwise alert on every routine restart. This filter downgrades only that exact message
to INFO — genuinely actionable deaths (OOM/SIGKILL, worker timeout, boot failure) keep their
original level and still alert. Installed on the `gunicorn.error` logger from both
`gunicorn.conf.py` (master, before the app imports) and `configure_logging()` (each worker),
matching on an exact message substring — never a blanket suppression of worker-lifecycle logs.

## handlers

`unhandled_exception_handler` exists because Starlette's `ServerErrorMiddleware` sits above
`CORSMiddleware`, so an uncaught exception would otherwise return a 500 with no CORS headers —
a cross-origin browser then sees a bare network failure instead of the real error. This handler
re-applies the CORS headers by mirroring the CORS middleware's origin-allow decision; it does
not swallow the exception, which error tracking still captures once upstream.
`validation_exception_handler` and `rate_limit_exception_handler` reformat FastAPI's and
slowapi's default error bodies into single, user-facing messages.

## db_types

Two dialect-portable SQLAlchemy column types for models that must run on both PostgreSQL
(production) and SQLite (the shared low-traffic gateway host): `JSONColumn` (JSONB on Postgres,
generic JSON elsewhere) and `Uuid` (native UUID on Postgres, CHAR(32) elsewhere). Safe only
because application code round-trips whole JSON values and never relies on JSONB-only operators
(`@>`, `->>`, `.astext`) — see `backend-database` for when reaching for Postgres-native types
directly is the better call.

## dependencies

FastAPI dependency aliases (`db_dependency`, `user_dependency`, `require_capability(...)`,
etc.) — the request-scoped plumbing routers use instead of inline `Depends(...)`.
`get_user`/`get_current_user` eager-load `User.organisation_memberships` (and their
organisations) on every authenticated request, because capability checks resolve the
*effective* grant set including org-conferred capabilities — without the eager load this would
N+1 on the hottest path in the app.

`stamp_last_accessed` records account activity at most once per 5-minute throttle window; it
commits without expiring the session so the just-loaded user and its eager-loaded relationships
survive, and any failure is rolled back and logged rather than failing the request.
`require_capability(capability)` is **the** authorization primitive: it resolves the caller to
either the service account (via a matched `SERVICE_TOKENS` bearer) or a verified human session,
then applies one identical capability check to both — a service token that can't resolve a
seeded account fails closed. `entitlement_dependency` is a narrower gate for paid Stripe
entitlements, bypassable by `feature:preview` holders (admins pass implicitly).

## instance / instance_manifest

`instance/instance.json` is a **generated** artifact — produced by a frontend build script
(`next-pwa/scripts/instance-manifest/build-instance-manifest.cjs`) from
`product/instance.config.ts` — never hand-edited; CI fails the build if it goes stale. It
mirrors per-instance identity only (brand name/wordmark, domain, contact local-part, currency)
and holds no secrets.

`instance_manifest.py` loads and `lru_cache`s it, fail-open: a missing or malformed file logs a
warning and returns `None`, letting `settings` fall back to its own domain-derivation rather
than blocking startup — identity is not treated as security-critical here. `settings` prefers
the manifest's brand name for `SENDER_NAME` (fixing a historical "Quant Soc" vs "QuantSoc"
drift) and default currency, and cross-checks `APP_DOMAIN` against the manifest's domain,
**failing closed** on a real production mismatch (dev/local hosts exempt).
