# Security

Low-level auth primitives — JWTs, cookies, CSRF, password/code hashing — with no session model
or business logic of their own.

## Scope and dependencies

This package holds **primitives only**: it mints and decodes JWTs, sets/clears auth cookies,
validates CSRF, hashes passwords/codes, and checks password strength. It does **not** implement
the session table, refresh-token rotation, or reuse detection — those live in
`platform/services/auth_services/`, built on top of what this package exports. It is **not
dependency-free**: every module reads `infrastructure/settings` for its knobs (`HASH_KEY`,
TTLs, cookie flags, allowed origins), and `csrf.py`/`tokens.py` raise
`platform.exceptions`/consume `platform.enums.TokenUse` — a prior claim of zero dependencies
was wrong.

## Token model (tokens.py)

Access and refresh tokens are separate signed JWTs (`jose`, `HASH_KEY`/`HASH_ALGORITHM`), each
carrying `sub` (user id), `jti`, `token_use`, and — critically — `sid` (session id), which is
what lets the session layer above tie a refresh token to one DB-backed session row for rotation
and reuse detection. `decode_token` optionally enforces `expected_use`, so an access token can
never be replayed where a refresh token is expected or vice versa. Access tokens are
short-lived (`TOKEN_TTL`, minutes) and meant for the `Authorization` header; refresh tokens are
long-lived (`REFRESH_TOKEN_TTL`) and meant to travel only as the httpOnly cookie this package
also manages.

## Cookies (cookies.py)

Three cookies, each with a distinct purpose and matching attributes: `refresh_token` (httpOnly,
scoped to `/auth` only — never readable by JS, never sent to unrelated routes), `csrf_token`
(JS-readable, root-scoped, the double-submit half of CSRF), and `session_present` (JS-readable
hint so the frontend can detect "signed in" without parsing a JWT). `Secure`/`SameSite` derive
from `settings.COOKIE_SECURE`/`COOKIE_SAMESITE`, never hardcoded here. Clearing a cookie
replays the *exact* attributes it was set with (`_expire_cookie`), not Starlette's
`delete_cookie` defaults — under `SameSite=None` (cross-site preview/native setups) a
mismatched expiry is silently rejected by the browser, which is what caused the historical
"have to sign out twice" bug.

## CSRF (csrf.py)

`validate_csrf` accepts a request if *either* its `Origin` header is on the allowed-origins
list (trusted-origin bypass) *or* the double-submit cookie/header pair matches — never
requiring both. Login and signup are intentionally exempt from this check upstream (no session
to protect yet); refresh and logout are not. Origin matching also supports a configurable
suffix/regex for preview deployments (`ALLOWED_ORIGIN_SUFFIX`), so a wildcard preview host
doesn't need to be enumerated per deploy.

## Passwords and hashing (passwords.py, hashing.py)

Passwords hash with bcrypt via one shared `bcrypt_context` (also reused by `codes.py` for
verification codes). Strength is enforced by `zxcvbn`, scored against `user_inputs` (e.g. the
account's own name/email) so a password isn't merely long but isn't trivially guessable *given
what an attacker already knows about the account*; the minimum score is a settings knob
(`PASSWORD_STRENGTH_THRESHOLD`), not hardcoded.

## Verification/reset codes (codes.py)

Numeric and alphanumeric one-time codes, generated with `secrets` (CSPRNG, not `random`), with
the alphanumeric alphabet excluding visually ambiguous characters (`O`/`0`, `I`/`L`/`1`) since
these are meant to be typed by a human from an email. Codes are hashed with bcrypt before
storage — the same as passwords — despite being short-lived and short, so a DB read never
exposes a valid code in plaintext.
