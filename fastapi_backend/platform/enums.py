from enum import Enum


class CodeType(Enum):
    VERIFICATION = "verification"
    RESET_PASSWORD = "reset_password"


class EmailTemplateType(Enum):
    PRIVACY_POLICY_CHANGE = "privacy_policy_change"
    USER_VERIFICATION = "user_verification"
    PASSWORD_RESET = "password_reset"
    CONTACT_ADMIN_NOTIFICATION = "contact_admin_notification"
    # The generic operator alert any feature can send (subject/heading/body plus
    # optional severity chip and CTA) - see admin_notification_service:
    ADMIN_NOTIFICATION = "admin_notification"
    # Customer-facing transactional emails sent to the buyer:
    PAYMENT_ORDER_CONFIRMATION = "payment_order_confirmation"
    PAYMENT_SUBSCRIPTION_WELCOME = "payment_subscription_welcome"
    PAYMENT_SUBSCRIPTION_RENEWAL = "payment_subscription_renewal"
    PAYMENT_REFUND_CONFIRMATION = "payment_refund_confirmation"
    # Internal operator alerts for payment events (one template per event shape):
    PAYMENT_ORDER_NOTIFICATION = "payment_order_notification"
    PAYMENT_SUBSCRIPTION_NOTIFICATION = "payment_subscription_notification"
    PAYMENT_DISPUTE_NOTIFICATION = "payment_dispute_notification"
    # Question flows (platform/question_flows.py). Deliberately sterile,
    # non-promotional copy: these are unsolicited-by-timing reminders, so they
    # carry a working stop link and say nothing a marketing message would say.
    # Adding a value here REQUIRES the matching React component + registry entry
    # and a template rebuild — assert_templates_exist() fails boot otherwise:
    QUESTION_FLOW_REMINDER = "question_flow_reminder"
    QUESTION_FLOW_LEAD_RESUME = "question_flow_lead_resume"


class Role(Enum):
    """Identity for an account (see the access-control rule).

    Role is identity, not authorization: USER is a normal account, ADMIN is the
    catch-all break-glass identity that implicitly holds every capability.
    Authorization is per-resource capability grants on `users.capabilities`
    (registry in platform/capabilities.py), checked by `require_capability`.
    """
    USER = "user"
    ADMIN = "admin"


class TokenUse(Enum):
    ACCESS = "access"
    REFRESH = "refresh"


class ProductType(Enum):
    EVENT = "event"
    DIGITAL = "digital"
    SUBSCRIPTION = "subscription"
    MERCHANDISE = "merchandise"


class LaunchStage(Enum):
    """Visibility/purchasability stage for a catalog item"""
    PUBLIC = "public"
    ADMIN_ONLY = "admin_only"
    HIDDEN = "hidden"


class PurchaseStatus(Enum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    PARTIALLY_REFUNDED = "partially_refunded"
    REFUNDED = "refunded"
    DISPUTED = "disputed"


class UserListFilter(Enum):
    ALL = "all"
    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    MARKETING_SUBSCRIBED = "marketing_subscribed"
