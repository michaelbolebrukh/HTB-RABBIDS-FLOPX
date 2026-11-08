"""Inbox reliability monitors: boot probe + full-loop canary.

Together with the credential-rejection escalation in the adapters, these close
every known silent-failure path of the mailbox:

- Boot probe: authenticates against the mailbox once per boot. Catches env
  drift and bad credential rotation on the deploy that caused it.
- Canary: sends a real heartbeat email to the mailbox through the OUTBOUND
  path (Forward Email SMTP, MX, spam filtering) and then verifies it arrives
  through the READ path - one loop proves send, receive, DNS/MX, and storage
  end to end. Broken receiving otherwise looks exactly like "no new mail",
  which is why no passive check can replace this.

Failures log at ERROR (Sentry alerts); successes log at INFO as boot/loop
markers. Runs in daemon threads so it never blocks or crashes boot.
"""
import logging
import threading
import time
import uuid

from infrastructure.cache import cache
from infrastructure.settings import settings as s
from platform.services.inbox_service import service as svc
from platform.services.inbox_service.ports import OutgoingMessage


logger = logging.getLogger(__name__)

_ARRIVAL_TIMEOUT_SECONDS = 300
_ARRIVAL_POLL_SECONDS = 15
_FIRST_RUN_DELAY_SECONDS = 180
_CANARY_SUBJECT_PREFIX = "Inbox canary"

# New-mail watcher: how often INBOX is checked for arrivals to announce via
# web push, and the platform_keys row holding the announce high-water mark:
_WATCH_INTERVAL_SECONDS = 30
_WATCH_WATERMARK_KEY = "inbox_watch_high_water"

# Best-effort cross-worker dedupe (exact on the Redis cache backend, per-worker
# on the memory backend, where duplicate canaries are harmless self-deleting
# heartbeats):
_LOCK_KEY = "inbox:canary:last-run"


def run_boot_probe() -> bool:
    """Authenticates against the mailbox once; ERROR (alert) on failure"""
    try:
        svc.get_mailbox().list_folders()
    except Exception:
        # The adapter has already logged the specific cause at the right level;
        # this marks the probe itself so the boot log shows a clear verdict:
        logger.error("Inbox boot probe FAILED: mailbox unreachable or credential rejected.")
        return False
    logger.info("Inbox boot probe OK: mailbox credential authenticated.")
    return True


def sweep_canaries(cap: int = 100) -> int:
    """Deletes lingering canary heartbeats from INBOX.

    A canary that arrives after its own run's arrival timeout - or whose run
    thread was killed by a redeploy mid-verify - is never cleaned up by that
    run and would otherwise pile up in the mailbox. This removes them so the
    self-check never clutters the inbox. Only exact subject-prefix matches are
    deleted, never a human email that merely matches the search. Called at the
    start of each canary cycle (no canary of the current run exists yet, and
    slot-claiming rules out a concurrent run) and once on boot."""
    mailbox = svc.get_mailbox()
    deleted = 0
    while deleted < cap:
        try:
            page = mailbox.list_messages(folder="INBOX", page=1, search=_CANARY_SUBJECT_PREFIX)
        except Exception:
            logger.warning("Canary sweep: list failed; will retry next cycle.")
            break
        targets = [m for m in page.messages if (m.subject or "").startswith(_CANARY_SUBJECT_PREFIX)]
        if not targets:
            break
        before = deleted
        for message in targets:
            try:
                mailbox.delete(message.id)
                deleted += 1
            except Exception:
                logger.warning("Canary sweep: failed to delete %s.", message.id)
        # If a full page yielded no successful delete, stop rather than re-listing
        # the same undeletable rows forever:
        if deleted == before:
            break
    if deleted:
        logger.info("Canary sweep removed %s stale heartbeat(s) from INBOX.", deleted)
    return deleted


def run_canary_once() -> bool:
    """Full send + receive loop: heartbeat out via the sender, arrival verified
    via the mailbox, then cleaned up. Returns True only if the loop closed."""
    # Clear any stragglers from prior cycles (late arrivals, redeploy-killed
    # runs) before sending a fresh one, so canaries never accumulate:
    sweep_canaries()

    marker = f"canary-{uuid.uuid4().hex[:12]}"
    identity = svc._mailbox_identity()
    heartbeat = OutgoingMessage(
        to=[identity],
        subject=f"{_CANARY_SUBJECT_PREFIX} {marker}",
        html=f"<p>Automated full-loop mailbox check {marker}. Deleted automatically.</p>",
        text=f"Automated full-loop mailbox check {marker}. Deleted automatically.",
    )

    try:
        svc.get_sender().send(heartbeat, identity)
    except Exception:
        logger.exception("Inbox canary FAILED on the send leg - outbound mail is broken.")
        return False

    mailbox = svc.get_mailbox()
    deadline = time.monotonic() + _ARRIVAL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(_ARRIVAL_POLL_SECONDS)
        try:
            page = mailbox.list_messages(folder="INBOX", page=1, search=marker)
        except Exception:
            # Transient read hiccups are retried until the deadline; a real
            # outage surfaces below as a non-arrival:
            continue
        if page.messages:
            try:
                mailbox.delete(page.messages[0].id)
            except Exception:
                logger.warning("Inbox canary arrived but cleanup failed (marker %s).", marker)
            logger.info("Inbox canary OK: send + receive loop verified.")
            return True

    logger.error(
        "Inbox canary FAILED: heartbeat did not arrive within %ss - the receive "
        "path (MX/DNS/provider/storage) may be silently broken.",
        _ARRIVAL_TIMEOUT_SECONDS,
    )
    return False


def check_new_mail_once(session_factory) -> int:
    """One watcher tick: announces INBOX arrivals past the high-water mark.

    The mark lives in platform_keys and advances via an optimistic
    compare-and-set, so across workers exactly one announces each batch.
    Canary heartbeats delete themselves and are excluded. Returns how many
    messages were announced."""
    from platform.models import PlatformKey
    from platform.services.notification_service import push

    page = svc.get_mailbox().list_messages(folder="INBOX", page=1, search=None)
    dates = [m.date for m in page.messages if m.date]
    if not dates:
        return 0
    latest = max(dates)

    db = session_factory()
    try:
        row = db.query(PlatformKey).filter(PlatformKey.key == _WATCH_WATERMARK_KEY).first()
        if row is None:
            # First run baselines silently, so a fresh boot never floods
            # subscribers with the whole existing inbox:
            db.add(PlatformKey(key=_WATCH_WATERMARK_KEY, value=latest))
            try:
                db.commit()
            except Exception:
                db.rollback()
            return 0

        previous = row.value
        if latest <= previous:
            return 0

        claimed = (
            db.query(PlatformKey)
            .filter(PlatformKey.key == _WATCH_WATERMARK_KEY, PlatformKey.value == previous)
            .update({"value": latest})
        )
        try:
            db.commit()
        except Exception:
            db.rollback()
            return 0
        if not claimed:
            return 0

        fresh = [
            m for m in page.messages
            if m.date and m.date > previous and not m.subject.startswith(_CANARY_SUBJECT_PREFIX)
        ]
        if not fresh:
            return 0

        if len(fresh) == 1:
            title = fresh[0].sender.name or fresh[0].sender.email
            body = fresh[0].subject or "(no subject)"
        else:
            title = f"{len(fresh)} new messages"
            body = ", ".join((m.subject or "(no subject)") for m in fresh[:3])
        push.notify_inbox_readers(db, title, body, url="/inbox")
        return len(fresh)
    finally:
        db.close()


def _new_mail_watch_loop(session_factory) -> None:
    while True:
        time.sleep(_WATCH_INTERVAL_SECONDS)
        try:
            check_new_mail_once(session_factory)
        except Exception:
            # Transient mailbox/database hiccups retry next tick; only log at
            # WARNING so a flaky minute never alarm-fatigues Sentry:
            logger.warning("Inbox new-mail watcher tick failed.", exc_info=True)


def _claim_canary_slot(interval_seconds: float) -> bool:
    """Best-effort cross-worker claim so one worker runs each cycle"""
    try:
        if cache.get(_LOCK_KEY) is not None:
            return False
        cache.set(_LOCK_KEY, time.time(), ttl_seconds=int(interval_seconds * 0.9))
        return True
    except Exception:
        # Fail open: a duplicate canary is a harmless self-deleting heartbeat;
        # a skipped one is not, so never let the lock block the loop entirely:
        return True


def _canary_loop() -> None:
    interval_seconds = s.INBOX_CANARY_INTERVAL_HOURS * 3600
    time.sleep(_FIRST_RUN_DELAY_SECONDS)
    while True:
        if _claim_canary_slot(interval_seconds):
            try:
                run_canary_once()
            except Exception:
                logger.exception("Inbox canary loop iteration crashed.")
        time.sleep(interval_seconds)


def _boot_canary_sweep() -> None:
    try:
        sweep_canaries()
    except Exception:
        logger.exception("Boot canary sweep crashed.")


def start_inbox_monitors() -> None:
    """Boot probe immediately + periodic canary; no-op when the inbox is off"""
    if not svc.inbox_is_configured():
        return

    threading.Thread(target=run_boot_probe, name="inbox-boot-probe", daemon=True).start()

    # One-shot sweep of canary heartbeats left by prior runs (late arrivals,
    # redeploy-killed verifies), so a deploy clears the pile immediately - even
    # when the periodic canary is disabled:
    threading.Thread(target=_boot_canary_sweep, name="inbox-canary-sweep", daemon=True).start()

    # Schedule-send dispatcher: fires due outbox rows through the normal send
    # path once a minute (optimistic row claims keep multi-worker safe):
    from infrastructure.database import SessionLocal
    from platform.services.inbox_service.scheduled import run_dispatcher_loop
    threading.Thread(
        target=run_dispatcher_loop, args=(SessionLocal,),
        name="inbox-scheduled-dispatch", daemon=True,
    ).start()

    # New-mail watcher: announces INBOX arrivals to web-push subscribers:
    threading.Thread(
        target=_new_mail_watch_loop, args=(SessionLocal,),
        name="inbox-new-mail-watch", daemon=True,
    ).start()

    if s.INBOX_CANARY_INTERVAL_HOURS > 0:
        threading.Thread(target=_canary_loop, name="inbox-canary", daemon=True).start()
    else:
        logger.info("Inbox canary disabled (INBOX_CANARY_INTERVAL_HOURS=0).")
