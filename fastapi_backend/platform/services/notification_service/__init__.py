"""Centralized notification capability (platform).

Today this owns **web push** — generalized from the admin-inbox-only origin to a
reusable channel serving two audiences:

- **inbox readers** — operational pings (new inbox mail, scheduled-send failure).
- **all opted-in users** — broadcast announcements ("new season live now"),
  triggered from code/ops via `send_broadcast`.

The VAPID keypair self-provisions once (platform_keys) so no per-instance env var
exists. One `push_subscriptions` row per browser/device; dead endpoints prune on
delivery. Payloads support the full Web Notification API surface (icon/badge/image/
action buttons/deep-link/tag/requireInteraction/…), assembled via `PushNotification`.

Email and any future in-app channel are deliberately out of scope here for now;
this package is the seam they would slot into.
"""

from platform.services.notification_service.push import (  # noqa: F401
    Audience,
    PushNotification,
    get_vapid_keys,
    notify_inbox_readers,
    send_broadcast,
    subscribe,
    unsubscribe,
)
