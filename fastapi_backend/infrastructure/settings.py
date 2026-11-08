import logging
import re
from datetime import timedelta
from typing import Optional
from urllib.parse import urlparse

from pydantic import Field, field_validator, computed_field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from infrastructure.instance_manifest import get_instance_manifest


logger = logging.getLogger(__name__)


def _manifest_contact_local_part() -> str:
    """Default contact local-part from the instance manifest, else the code default."""
    manifest = get_instance_manifest()
    if manifest and manifest.contact_local_part:
        return manifest.contact_local_part
    return "hello"


def _manifest_supported_currencies() -> str:
    """Default supported-currencies CSV from the instance manifest, else GBP."""
    manifest = get_instance_manifest()
    if manifest and manifest.supported_currencies:
        return ",".join(manifest.supported_currencies)
    return "GBP"


class Settings(BaseSettings):
    """Application settings loaded from environment variables"""
    
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )
    
    # Core security and DB configuration:
    DATABASE_URL: str
    HASH_KEY: str
    # Comma-separated bearer tokens for service/agent access to admin ops
    # (see infrastructure/service_auth.py). Optional: unset = no service access,
    # human admin auth unaffected. A separate secret from HASH_KEY on purpose.
    SERVICE_TOKENS: Optional[str] = None
    HASH_ALGORITHM: str = "HS256"
    TOKEN_TTL: int = Field(default=30, description="Access token TTL in minutes")
    REFRESH_TOKEN_TTL: int = Field(default=43200, description="Refresh token TTL in minutes")
    MAX_TOKEN_LENGTH: int = 4096
    MAX_OAUTH_TOKEN_LENGTH: int = 4096
    
    # Password policy:
    MIN_PASSWORD_LENGTH: int = 8
    MAX_PASSWORD_LENGTH: int = 128
    PASSWORD_STRENGTH_THRESHOLD: int = 3
    
    # Name validation:
    MIN_NAME_LENGTH: int = 1
    # Display-name change policy: the user may change their name up to
    # NAME_CHANGE_ALLOWANCE times within each rolling NAME_CHANGE_INTERVAL_DAYS
    # window, after which further changes go through support/contact.
    #   INTERVAL None (empty env value) = unlimited self-serve renames;
    #   INTERVAL 0 = self-serve renames disabled (always via support);
    #   INTERVAL N > 0 = ALLOWANCE changes per N days.
    # Both ride on the user contract (UserResponse.name_change_policy) so the
    # client picks the matching flow and copy without a hardcoded policy:
    NAME_CHANGE_ALLOWANCE: int = 1
    NAME_CHANGE_INTERVAL_DAYS: Optional[int] = 30

    @field_validator("NAME_CHANGE_INTERVAL_DAYS", mode="before")
    @classmethod
    def _empty_interval_is_none(cls, v):
        # An explicitly empty env value means "no limit" (Optional[int] would
        # otherwise fail to parse the empty string):
        if isinstance(v, str) and v.strip() == "":
            return None
        return v
    
    # Email delivery:
    EMAIL_PROVIDER: str = "BREVO"
    BREVO_API_KEY: Optional[str] = None
    SENDGRID_API_KEY: Optional[str] = None
    APP_DOMAIN: str
    PUBLIC_FRONTEND_ORIGIN: Optional[str] = None

    # Object storage for uploaded files (auto-detected, no manual wiring):
    # preferred production path is CLOUDFLARE_API_TOKEN (or CLOUDFLARE_R2_API_TOKEN)
    # in the process env — R2 endpoint, bucket names (from APP_DOMAIN), and S3
    # credentials are derived at runtime (see platform/services/storage).
    # Explicit STORAGE_S3_* still wins when set (any S3-compatible provider);
    # otherwise files are written to local disk and served from /uploads.
    STORAGE_S3_BUCKET: Optional[str] = None
    STORAGE_S3_ENDPOINT_URL: Optional[str] = None
    STORAGE_S3_REGION: Optional[str] = None
    STORAGE_S3_PUBLIC_BASE_URL: Optional[str] = None
    # Private plane for documents that must never be publicly fetchable:
    # a SEPARATE bucket without public access. With the Cloudflare one-token
    # path this is derived as `<app-domain-slug>-private`. When STORAGE_S3_BUCKET
    # is set explicitly, STORAGE_S3_PRIVATE_BUCKET must be set too or private
    # saves fail loudly (never silently half-private). Without S3 the local
    # backend derives an unmounted sibling directory (uploads -> uploads-private):
    STORAGE_S3_PRIVATE_BUCKET: Optional[str] = None
    STORAGE_LOCAL_DIR: str = "uploads"
    PUBLIC_BACKEND_ORIGIN: Optional[str] = None
    MAX_AVATAR_BYTES: int = 5 * 1024 * 1024
    # Size cap for private document uploads — PDFs beyond this are almost
    # always scans or embedded images, not text documents:
    MAX_DOCUMENT_BYTES: int = 10 * 1024 * 1024
    CONTACT_EMAIL: Optional[str] = None
    # Prefer the instance manifest's contact local-part (single source of truth,
    # product/instance.config.ts) over the code default; an explicit env value
    # still wins (env > manifest > "hello"):
    CONTACT_EMAIL_LOCAL_PART: str = Field(default_factory=_manifest_contact_local_part)
    SENDER_NAME: Optional[str] = None
    SENDGRID_HOST: Optional[str] = None
    SENDGRID_DATA_RESIDENCY: Optional[str] = None
    SENDGRID_EU_RESIDENCY: bool = False
    TEST_USER_EMAIL: Optional[str] = None
    TEST_USER_EMAIL_LOCAL_PART: Optional[str] = None
    TEST_USER_PASSWORD: Optional[str] = None
    # Optional paper-brokerage integration for this instance (product-scoped, so
    # the PRODUCT prefix drops them on a fork). All three or nothing: the adapter
    # stays dormant unless every one is present, and the values are never logged.
    # The base URL is the one value the credential cannot reveal (paper vs live
    # endpoints share a key format), which is the documented reason it exists:
    PRODUCT_ALPACA_API_KEY: Optional[str] = None
    PRODUCT_ALPACA_SECRET_KEY: Optional[str] = None
    PRODUCT_ALPACA_BASE_URL: Optional[str] = None
    # EIA API v2 key (product/flopx/sources/eia.py): the adapter is dormant without it and
    # the key travels only as the api_key query parameter, never logged. Instance-owned
    # (the founder's registration); the name is the one the deployment sets.
    EIA_API_KEY: Optional[str] = None
    # Admin seeding. Each accepts a single email OR a comma-separated list.
    # ADMIN_EMAIL = platform owner(s), reusable across startup clones. Its key is
    # intentionally unprefixed so the existing value keeps working (legacy by
    # default). PRODUCT_ADMIN_EMAIL = admin(s) for this instance only; its PRODUCT
    # prefix marks it as product-scoped so render-ops drops it when forking to a
    # new startup. Both seed identical admin rights today (see infrastructure/seeder.py).
    ADMIN_EMAIL: Optional[str] = None
    PRODUCT_ADMIN_EMAIL: Optional[str] = None
    # Test/preview only - seeds a verified demo-user fleet on boot when set:
    DEMO_USERS_PASSWORD: Optional[str] = None
    
    # OAuth providers (optional):
    GOOGLE_CLIENT_ID: Optional[str] = None
    GOOGLE_CLIENT_SECRET: Optional[str] = None
    APPLE_CLIENT_ID: Optional[str] = None
    APPLE_CLIENT_SECRET: Optional[str] = None
    
    # AI providers (optional) — read by the generative AI service's engine and
    # passed explicitly at model construction (never relied on ambiently):
    OPENAI_API_KEY: Optional[str] = None
    OPENAI_ORGANIZATION: Optional[str] = None
    ANTHROPIC_API_KEY: Optional[str] = None
    GOOGLE_API_KEY: Optional[str] = None
    XAI_API_KEY: Optional[str] = None
    OPENROUTER_API_KEY: Optional[str] = None
    # Media lanes (P7, D43) — optional: each lane is dormant without its key
    # (picking a model that needs a missing key fails loudly at validation).
    # FAL_API_KEY = the media long-tail aggregator (TTS voices + image models);
    # ELEVENLABS_API_KEY = premium voices/cloning direct:
    FAL_API_KEY: Optional[str] = None
    ELEVENLABS_API_KEY: Optional[str] = None

    # Stripe payments (optional - payment features are disabled when unset):
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None
    # Low-frequency entitlement reconciliation sweep (the failsafe behind
    # webhooks - heals drift a missed event could leave). Seconds between sweeps;
    # 0 disables. Default 6h - a factory instance is low-traffic and webhooks are
    # the real-time path, so this only needs to be a slow safety net:
    ENTITLEMENT_RECONCILE_INTERVAL_SECONDS: int = 21600
    # Per-session Checkout branding (colors/font/shape/name from the generated
    # manifest; logo resolved at runtime from the bootstrap upload). On -> the
    # checkout page carries the product brand; off -> Stripe uses the account's
    # dashboard branding:
    STRIPE_CHECKOUT_BRANDING: bool = True

    # AI budgets (Phase 4) — INFORMATIVE BY DEFAULT (D32): cost tracking, the
    # spend ledger, and RunCompleted.cost_usd are ALWAYS on; pre-flight
    # blocking is opt-in. A product incorporating this service enables
    # enforcement explicitly and defines its own limits/windows (ai_budgets
    # rows or the defaults below). When enforcement IS on, a broken budget
    # check fails CLOSED (blocks rather than spends unmetered):
    AI_BUDGET_ENFORCEMENT: bool = False
    AI_DEFAULT_OWNER_DAILY_BUDGET_USD: Optional[float] = None
    AI_DEFAULT_OWNER_MONTHLY_BUDGET_USD: Optional[float] = None

    # AI sandbox (Phase 6, D38) — DORMANT BY DEFAULT: with no backend named,
    # the code_execution tool raises its typed error and nothing else changes
    # (Render's runtime has no Docker daemon; a hosted backend is a later
    # org-services decision). "docker" enables the Docker CLI backend on
    # docker-capable hosts; the image is only read when a backend is active:
    AI_SANDBOX_BACKEND: Optional[str] = None
    AI_SANDBOX_IMAGE: str = "python:3.12-slim"
    
    # Admin inbox (optional - dormant until the mailbox pair is set). The native
    # inbox reads AND sends over Forward Email's IMAP/SMTP: INBOX_IMAP_PASSWORD is
    # the only strictly per-instance value (the Forward Email-minted mailbox
    # secret); INBOX_IMAP_EMAIL is optional and defaults to CONTACT_EMAIL when a
    # password is set (see derive_domain_defaults), so a fresh instance only sets
    # the password. Created by mailbox provisioning (see domain-ops
    # references/provision-inbox.md). (The legacy WEBMAIL_* SnappyMail embed is
    # retired - its service and code are gone.)
    INBOX_IMAP_EMAIL: Optional[str] = None
    INBOX_IMAP_PASSWORD: Optional[str] = None
    # Full-loop mailbox canary cadence (send a heartbeat, verify it arrives).
    # Code default is right for production; override only to tune or disable
    # (0 = off) in unusual environments:
    INBOX_CANARY_INTERVAL_HOURS: float = 12.0
    # Undo-send window: an immediate send waits this long in the outbox before
    # the dispatcher fires it, so the composer can cancel it. Code default is
    # right for production; 0 sends synchronously (tests, unusual setups):
    INBOX_UNDO_SEND_SECONDS: int = 5

    # App/runtime flags:
    ENV: str = "production"
    COOKIE_SAMESITE: str = Field(
        default="lax",
        description="SameSite cookie policy; override only when not lax (e.g. none for cross-site embeds)",
    )
    REFRESH_PREVIOUS_JTI_GRACE_SECONDS: int = 30
    MAX_SESSIONS_PER_USER: int = 10
    SESSION_HINT_COOKIE_DOMAIN: Optional[str] = None
    
    # Extra frontend origins (comma-separated):
    EXTRA_FRONTEND_ORIGINS: str = ""
    
    # Preview origin allow (regex or suffix):
    ALLOWED_ORIGIN_SUFFIX: Optional[str] = None

    # Email/code settings:
    EMAIL_VERIFICATION_COOLDOWN: int = 3
    VERIFICATION_CODE_TTL: int = 15
    VERIFICATION_CODE_LENGTH: int = 6
    RESET_PASSWORD_CODE_TTL: int = 15
    RESET_PASSWORD_CODE_LENGTH: int = 8
    RESET_PASSWORD_EMAIL_COOLDOWN: int = 5
    
    # Supported currencies (comma-separated). Default comes from the instance
    # manifest (product/instance.config.ts); an explicit env value still wins
    # (env > manifest > "GBP"):
    SUPPORTED_CURRENCIES: str = Field(default_factory=_manifest_supported_currencies)

    # Observability (unset SENTRY_DSN = error tracking disabled entirely):
    SENTRY_DSN: Optional[str] = None
    # Performance tracing sample rate. Defaults to full (1.0) because factory
    # instances are low-traffic early-stage startups where full N+1/span fidelity
    # is worth far more than the tiny quota cost. It is a COST KNOB: dial it DOWN
    # per instance via env as traffic grows (Sentry quota warnings are the signal).
    # No-op unless SENTRY_DSN is set, so dev/CI are unaffected.
    SENTRY_TRACES_SAMPLE_RATE: float = Field(default=1.0, description="Sentry performance tracing sample rate (dial down per instance at scale)")

    # Error-snapshot pipeline (fully env-gated; dormant unless all three are set).
    # A Sentry issue webhook -> correlate the frontend + backend events by request_id
    # -> emit the assembled full-stack snapshot as an admin notification.
    # FIXME(ai-pipeline): swap the admin-notification sink for the custom AI pipeline.
    SENTRY_WEBHOOK_SECRET: Optional[str] = None          # HMAC secret to verify inbound webhooks (required)
    SENTRY_API_TOKEN: Optional[str] = None               # read events, read-only scope (required); org + projects are discovered from it
    SENTRY_ORG_SLUG: Optional[str] = None                # optional override; auto-discovered from the token when unset
    SENTRY_PROJECT_SLUGS: Optional[str] = None           # optional override (csv); defaults to every project the token can see
    SENTRY_API_BASE: str = "https://sentry.io"           # region API host (EU orgs still use sentry.io)

    # Caching (unset CACHE_URL = bounded in-process memory cache; redis://... = shared Redis/Valkey):
    CACHE_URL: Optional[str] = None
    CACHE_DEFAULT_TTL: int = Field(default=60, description="Default cache entry TTL ceiling in seconds")
    CACHE_MAX_MEMORY_ENTRIES: int = Field(default=1024, description="LRU bound for the in-process cache backend")
    
    
    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        """Deduplicates values while preserving declaration order"""
        seen: set[str] = set()
        deduped: list[str] = []
        for value in values:
            if value and value not in seen:
                deduped.append(value)
                seen.add(value)
        return deduped
    
    @staticmethod
    def _normalize_domain(value: str) -> str:
        """Normalizes a domain from a URL-like or domain-only value"""
        raw_value = (value or "").strip().lower()
        parsed = urlparse(raw_value if "://" in raw_value else f"//{raw_value}")
        domain = (parsed.hostname or raw_value).strip().lstrip(".")
        
        if "/" in domain:
            domain = domain.split("/", 1)[0]
        
        if not domain:
            raise ValueError("APP_DOMAIN must not be empty")
        
        return domain
    
    @staticmethod
    def _normalize_origin(value: str) -> str:
        """Normalizes frontend origin to scheme://host[:port]"""
        raw_value = (value or "").strip()
        if not raw_value:
            return ""
        
        parsed = urlparse(raw_value if "://" in raw_value else f"https://{raw_value}")
        scheme = parsed.scheme or "https"
        hostname = parsed.hostname
        
        if not hostname:
            return raw_value.rstrip("/")
        
        netloc = hostname.lower()
        if parsed.port:
            netloc = f"{netloc}:{parsed.port}"
        
        return f"{scheme.lower()}://{netloc}"
    
    @staticmethod
    def _derive_sender_name_from_domain(domain: str) -> str:
        """Deriving email sender display name from APP_DOMAIN when SENDER_NAME is unset"""
        host = domain.strip().lower()
        if host.startswith("www."):
            host = host[4:]
        
        parts = host.split(".")
        brand = parts[0] if len(parts) >= 2 else host
        brand = brand.replace("-", " ").replace("_", " ").strip()
        
        if not brand:
            raise ValueError("APP_DOMAIN must yield a non-empty sender name")

        return brand.title()

    @staticmethod
    def _parse_email_list(raw: Optional[str]) -> list[str]:
        """Splits a single-email-or-CSV env value into a normalized list: trimmed,
        lowercased, blanks dropped, de-duplicated with original order preserved."""
        if not raw:
            return []
        parsed: list[str] = []
        for part in raw.split(","):
            email = part.strip().lower()
            if email and email not in parsed:
                parsed.append(email)
        return parsed

    @property
    def platform_admin_emails(self) -> list[str]:
        """Admin(s) belonging to the reusable factory (carry across clones), from ADMIN_EMAIL."""
        return self._parse_email_list(self.ADMIN_EMAIL)

    @property
    def product_admin_emails(self) -> list[str]:
        """Admin(s) specific to this startup instance (dropped on fork), from PRODUCT_ADMIN_EMAIL."""
        return self._parse_email_list(self.PRODUCT_ADMIN_EMAIL)

    @property
    def admin_emails(self) -> list[str]:
        """Every email to seed as an admin — platform first, then product, de-duplicated.
        Privileges are identical today; the split is provenance/clone-hygiene only."""
        merged = list(self.platform_admin_emails)
        for email in self.product_admin_emails:
            if email not in merged:
                merged.append(email)
        return merged

    @property
    def primary_admin_email(self) -> Optional[str]:
        """Single address for one-recipient notifications (e.g. the interim error-snapshot
        sink): the first platform admin, else the first product admin, else None."""
        emails = self.admin_emails
        return emails[0] if emails else None

    @field_validator("COOKIE_SAMESITE")
    @classmethod
    def validate_samesite(cls, v: str) -> str:
        """Validates COOKIE_SAMESITE value"""
        v_lower = v.lower()
        if v_lower not in {"lax", "strict", "none"}:
            raise ValueError(f"Invalid COOKIE_SAMESITE value: '{v}'. Allowed: lax|strict|none")
        return v_lower
    
    @field_validator("ENV")
    @classmethod
    def validate_env(cls, v: str) -> str:
        """Normalizes and gates ENV to the only two supported values.

        Only 'production' and 'development' are valid — the whole codebase
        branches on exactly these. A near-miss like 'prod' would otherwise pass
        silently, behave as production (every check is `!= 'development'`), and
        mint a stray error-tracking environment tag; failing fast here prevents
        that.
        """
        v_lower = v.strip().lower()
        if v_lower not in {"production", "development"}:
            raise ValueError(
                f"Invalid ENV value: '{v}'. Allowed: production|development "
                "(unset defaults to production)."
            )
        return v_lower

    @field_validator("EMAIL_PROVIDER")
    @classmethod
    def validate_email_provider(cls, v: str) -> str:
        """Normalizes and validates configured email provider"""
        provider = v.strip().upper()
        if provider not in {"BREVO", "SENDGRID"}:
            raise ValueError("EMAIL_PROVIDER must be BREVO or SENDGRID")
        return provider
    
    @field_validator("APP_DOMAIN")
    @classmethod
    def validate_app_domain(cls, v: str) -> str:
        """Normalizes APP_DOMAIN to hostname only"""
        return cls._normalize_domain(v)
    
    @model_validator(mode="after")
    def derive_domain_defaults(self) -> "Settings":
        """Derives domain-based settings from canonical project values"""
        self.PUBLIC_FRONTEND_ORIGIN = self._normalize_origin(self.PUBLIC_FRONTEND_ORIGIN or f"https://{self.APP_DOMAIN}")
        
        if self.CONTACT_EMAIL:
            self.CONTACT_EMAIL = self.CONTACT_EMAIL.strip().lower()
        else:
            self.CONTACT_EMAIL = f"{self.CONTACT_EMAIL_LOCAL_PART.strip().lower()}@{self.APP_DOMAIN}"
        
        if self.TEST_USER_EMAIL:
            self.TEST_USER_EMAIL = self.TEST_USER_EMAIL.strip().lower()
        elif self.TEST_USER_EMAIL_LOCAL_PART:
            self.TEST_USER_EMAIL = f"{self.TEST_USER_EMAIL_LOCAL_PART.strip().lower()}@{self.APP_DOMAIN}"

        # The admin inbox reads the instance business mailbox, which is the
        # contact address (hello@<domain> by default). So an explicit
        # INBOX_IMAP_EMAIL is redundant with CONTACT_EMAIL for the common case:
        # derive it when a mailbox password is set but no address is given, so a
        # new instance only provisions the one value that cannot be derived - the
        # Forward Email-minted password (config-minimalism). An explicit
        # INBOX_IMAP_EMAIL still wins when the mailbox differs from the contact
        # address (e.g. contact@ forwards elsewhere):
        if self.INBOX_IMAP_EMAIL:
            self.INBOX_IMAP_EMAIL = self.INBOX_IMAP_EMAIL.strip().lower()
        elif self.INBOX_IMAP_PASSWORD:
            self.INBOX_IMAP_EMAIL = self.CONTACT_EMAIL

        # Sender display name: env wins; else the instance manifest brand name
        # (the single source of truth, product/instance.config.ts); else the
        # legacy domain-derivation. Preferring the manifest kills the historical
        # drift where the domain derived "Quant Soc" while the brand is "QuantSoc":
        if self.SENDER_NAME and self.SENDER_NAME.strip():
            self.SENDER_NAME = self.SENDER_NAME.strip()
        else:
            manifest = get_instance_manifest()
            if manifest and manifest.brand_name:
                self.SENDER_NAME = manifest.brand_name
            else:
                self.SENDER_NAME = self._derive_sender_name_from_domain(self.APP_DOMAIN)
        
        if self.SESSION_HINT_COOKIE_DOMAIN:
            self.SESSION_HINT_COOKIE_DOMAIN = f".{self._normalize_domain(self.SESSION_HINT_COOKIE_DOMAIN)}"
        elif self.ENV != "development":
            self.SESSION_HINT_COOKIE_DOMAIN = f".{self.APP_DOMAIN}"

        if self.COOKIE_SAMESITE == "none" and self.ENV == "development":
            raise ValueError(
                "COOKIE_SAMESITE=none requires Secure cookies; use ENV=production (HTTPS) or lax/strict in development"
            )
        
        return self
    
    @computed_field
    @property
    def COOKIE_SECURE(self) -> bool:
        """Derives Secure flag from deployment mode - false only in local development (HTTP)"""
        return self.ENV != "development"
    
    @computed_field
    @property
    def TOKEN_TTL_TIMEDELTA(self) -> timedelta:
        """Converts TOKEN_TTL to timedelta"""
        return timedelta(minutes=self.TOKEN_TTL)
    
    @computed_field
    @property
    def REFRESH_TOKEN_TTL_TIMEDELTA(self) -> timedelta:
        """Converts REFRESH_TOKEN_TTL to timedelta"""
        return timedelta(minutes=self.REFRESH_TOKEN_TTL)
    
    @computed_field
    @property
    def FRONTEND_ORIGINS_LIST(self) -> list[str]:
        """Builds frontend origins from canonical and override values"""
        origins = [self.PUBLIC_FRONTEND_ORIGIN or ""]
        
        # Always allowing both the apex and "www" variant of APP_DOMAIN - the
        # frontend host (Vercel, etc.) may serve from either, and requiring an
        # explicit EXTRA_FRONTEND_ORIGINS entry for this is an easy footgun:
        if self.APP_DOMAIN and self.APP_DOMAIN != "localhost":
            if self.APP_DOMAIN.startswith("www."):
                origins.append(f"https://{self.APP_DOMAIN[4:]}")
            else:
                origins.append(f"https://www.{self.APP_DOMAIN}")
        
        origins.extend(
            self._normalize_origin(origin)
            for origin in self.EXTRA_FRONTEND_ORIGINS.split(",")
            if origin.strip()
        )
        
        return self._dedupe(origins)
    
    @computed_field
    @property
    def FRONTEND_HOSTS(self) -> set[str | None]:
        """Extracts host names from frontend origins"""
        return {urlparse(origin).hostname for origin in self.FRONTEND_ORIGINS_LIST if origin}
    
    @computed_field
    @property
    def LOCAL_DEV_ORIGINS(self) -> set[str]:
        """Local development origins for CORS"""
        return {
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        }
    
    @computed_field
    @property
    def ALLOWED_ORIGINS(self) -> list[str]:
        """Builds allowed origins for CORS (stable order)"""
        return sorted({*self.LOCAL_DEV_ORIGINS, *self.FRONTEND_ORIGINS_LIST})
    
    @computed_field
    @property
    def SENDGRID_BASE_HOST(self) -> Optional[str]:
        """Resolves SendGrid host based on residency settings"""
        if self.SENDGRID_HOST:
            host = self.SENDGRID_HOST
            return host if host.startswith("http") else f"https://{host}"
        
        residency = (self.SENDGRID_DATA_RESIDENCY or "").strip().lower()
        if residency in {"eu", "eu1"} or self.SENDGRID_EU_RESIDENCY:
            return "https://api.eu.sendgrid.com"
        
        return None
    
    @computed_field
    @property
    def ALLOWED_ORIGIN_REGEX_COMPUTED(self) -> Optional[str]:
        if self.ALLOWED_ORIGIN_SUFFIX:
            # Normalizing suffix to not include protocol:
            suffix = self.ALLOWED_ORIGIN_SUFFIX.strip()
            suffix = suffix.replace("https://", "").replace("http://", "")
            escaped_suffix = re.escape(suffix)
            # Matching any https host whose hostname ends with the suffix:
            return rf"^https://[-a-z0-9]*{escaped_suffix}$"
        
        return None
    
    @computed_field
    @property
    def SUPPORTED_CURRENCIES_SET(self) -> set[str]:
        """Parses comma-separated currency codes into set"""
        codes: set[str] = set()
        for token in self.SUPPORTED_CURRENCIES.split(","):
            code = token.strip().upper()
            if len(code) == 3 and code.isalpha():
                codes.add(code)
        return codes

    @computed_field
    @property
    def DEFAULT_CURRENCY(self) -> str:
        """The instance's primary currency (catalog default and display currency).

        Prefers the instance manifest's declared default (single source of truth,
        product/instance.config.ts) when it is actually among the supported
        currencies, else the first configured supported currency, else GBP. This
        guarantees the default is always sellable even if SUPPORTED_CURRENCIES is
        overridden via env. Product pricing reads this instead of hardcoding a code."""
        supported = [
            token.strip().upper()
            for token in self.SUPPORTED_CURRENCIES.split(",")
            if len(token.strip()) == 3 and token.strip().isalpha()
        ]
        manifest = get_instance_manifest()
        if manifest and manifest.default_currency:
            if not supported or manifest.default_currency in supported:
                return manifest.default_currency
        if supported:
            return supported[0]
        return "GBP"

    def validate_instance_domain(self) -> "Settings":
        """Cross-checks APP_DOMAIN against the canonical instance-manifest domain.

        The manifest (product/instance.config.ts) is the single source of truth for
        instance identity; APP_DOMAIN is the one deploy-time argument. On a real
        deploy the two must agree, so a mismatch fails closed - a loud guard against
        a backend pointed at the wrong domain. Local/dev hosts and development mode
        are skipped (dev, CI, and unit tests legitimately use throwaway domains such
        as *.local against the committed manifest)."""
        manifest = get_instance_manifest()
        if not manifest or not manifest.domain:
            return self
        if self.APP_DOMAIN == manifest.domain:
            return self

        host = self.APP_DOMAIN
        is_local = (
            host == "localhost"
            or host.endswith(".local")
            or host.endswith(".localhost")
        )
        if self.ENV == "development" or is_local:
            logger.warning(
                "APP_DOMAIN %r differs from instance manifest domain %r; allowed in "
                "development/local but this would fail closed on a production deploy.",
                self.APP_DOMAIN,
                manifest.domain,
            )
            return self

        raise ValueError(
            f"APP_DOMAIN ({self.APP_DOMAIN!r}) does not match the instance manifest "
            f"domain ({manifest.domain!r}). The manifest (product/instance.config.ts) "
            f"is the canonical instance identity, so a mismatch means this deploy is "
            f"pointed at the wrong domain. Fix APP_DOMAIN or update instance.config.ts "
            f"and regenerate instance.json (`npm run build:instance-manifest`)."
        )


# Instantiating settings (will load from .env and environment):
settings = Settings()

# Fail closed at import if this deploy's APP_DOMAIN disagrees with the canonical
# instance-manifest domain. Applied to the app singleton only (not to ad-hoc
# Settings(...) constructions in unit tests, which use arbitrary throwaway domains):
settings.validate_instance_domain()
