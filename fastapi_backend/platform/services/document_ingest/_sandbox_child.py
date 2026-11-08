"""The child half of the disarm sandbox. Never imported by the API process.

Runs as a fresh interpreter: reads PDF bytes on stdin, writes safe PDF bytes on
stdout, exits with a code that tells the parent which kind of failure occurred.
It sets its own resource limits before it touches the input, so the untrusted
document is parsed by a process that is already boxed in.

Limits are applied HERE rather than through `subprocess(preexec_fn=...)` because
the parent calls into this from a worker thread, and `preexec_fn` is documented
as unsafe in a multithreaded process — it runs between fork and exec, where only
async-signal-safe calls are legal. Setting them in the child after exec costs a
few milliseconds of unbounded interpreter startup, before any attacker-controlled
byte has been read, and avoids that hazard entirely.
"""

from __future__ import annotations

import os
import resource
import sys


# Exit codes. Anything else — including a signal — is a crash, which is exactly
# the case this sandbox exists to survive.
EXIT_OK = 0
EXIT_INVALID_DOCUMENT = 10
EXIT_DISARM_FAILED = 11
EXIT_BAD_INVOCATION = 12

# The child's stderr also carries Python logging — the ladder logs a warning
# with a traceback every time it degrades a rung — so the parent cannot treat
# stderr as the user-facing message. It would put internal file paths and
# stack frames into a 422 body. The one line meant for a person is marked.
MESSAGE_MARKER = "\x1fSANDBOX-MESSAGE\x1f"
# Which resource limits actually applied. A control that silently fails to
# apply is worse than none, so the child states what it managed to set.
LIMITS_MARKER = "\x1fSANDBOX-LIMITS\x1f"


def emit_message(text: str) -> None:
    """Marking the single line the parent may show to a member."""
    sys.stderr.write(f"\n{MESSAGE_MARKER}{text}")
    sys.stderr.flush()


def emit_limits(applied: list[str]) -> None:
    """Reporting which limits took, so the parent can log a host that refuses."""
    sys.stderr.write(f"\n{LIMITS_MARKER}{','.join(applied) or 'NONE'}\n")
    sys.stderr.flush()

# Address space. The ladder's own budgets already bound the legitimate peak
# (~400MB for a 40-page render per `_MAX_TOTAL_PIXELS`); this is the backstop
# for the allocation those budgets cannot see coming, and it converts an
# OOM-killed worker into a MemoryError in a process nobody else depends on.
#
# It has to be sized against the INSTANCE, not just the workload, or it cannot
# do that job. `rebuild_limiter` admits two concurrent rebuilds, so the ceiling
# is paid twice over, alongside the API process's own ~226MB — and production
# runs on 2GB (1 CPU / 2GB standard, per the render ledger). A limit set above
# what the box can supply is not a backstop at all: the kernel's OOM killer
# arrives first and takes the worker, which is the exact outcome this exists to
# prevent. 600MB leaves real headroom over the 400MB peak while keeping the
# worst case (2 x 600MB + parent) inside the instance with room to spare.
_ADDRESS_SPACE_BYTES = 600 * 1024 * 1024

# CPU seconds. Soft must stay BELOW hard: with soft == hard the kernel goes
# straight to SIGKILL, and the grace between them is what lets the process die
# with a reportable error instead of vanishing. This is a backstop only — the
# parent's wall-clock deadline is the real bound, since CPU time does not
# advance while the process is blocked.
_CPU_SECONDS_SOFT = 40
_CPU_SECONDS_HARD = 50

# Open files. Generous next to the handful the ladder uses, tight next to what
# an fd-exhaustion payload would want.
_MAX_OPEN_FILES = 256


def _apply_limits(max_bytes: int) -> list[str]:
    """Boxing the process in before it reads a single untrusted byte.

    Returns the limits that were actually applied. A limit the host refuses is
    survivable — the parent's deadline and kill still bound this process — but
    it must not be SILENT. These are the controls that turn a runaway parse into
    a contained failure, and a version of this that swallowed every refusal
    would have reported a box it had not built. The caller reports the result so
    a host where they do not apply is visible in the logs rather than assumed.
    """
    # File writes are BOUNDED, not forbidden. The raster rungs hand PDFium a
    # path, so they legitimately write the source document to a temp file —
    # RLIMIT_FSIZE=0 would break rasterisation outright. A ceiling a little
    # above the document cap still stops a runaway write from filling the disk.
    file_ceiling = max(max_bytes * 4, 64 * 1024 * 1024)
    applied: list[str] = []

    for name, limit, value in (
        ("AS", resource.RLIMIT_AS, (_ADDRESS_SPACE_BYTES, _ADDRESS_SPACE_BYTES)),
        ("CPU", resource.RLIMIT_CPU, (_CPU_SECONDS_SOFT, _CPU_SECONDS_HARD)),
        ("FSIZE", resource.RLIMIT_FSIZE, (file_ceiling, file_ceiling)),
        ("NOFILE", resource.RLIMIT_NOFILE, (_MAX_OPEN_FILES, _MAX_OPEN_FILES)),
    ):
        try:
            soft_cap, hard_cap = resource.getrlimit(limit)
            wanted_soft, wanted_hard = value
            # Never raise a limit the platform already set lower, and never try
            # to raise a hard limit — unprivileged processes may only lower it.
            if hard_cap != resource.RLIM_INFINITY:
                wanted_soft = min(wanted_soft, hard_cap)
                wanted_hard = min(wanted_hard, hard_cap)
            resource.setrlimit(limit, (wanted_soft, wanted_hard))
            applied.append(f"{name}={wanted_soft}")
        except (ValueError, OSError):
            # A limit the host will not let us set is not a reason to refuse the
            # upload — the parent's deadline and kill still bound this process.
            # It IS a reason to say so: see the caller.
            continue

    # Precondition for any future seccomp filter, and free to set: it only
    # prevents this process gaining privileges it does not already have.
    try:
        import ctypes

        ctypes.CDLL("libc.so.6", use_errno=True).prctl(38, 1, 0, 0, 0)  # PR_SET_NO_NEW_PRIVS
    except Exception:
        pass

    return applied


def main() -> int:
    try:
        max_bytes = int(sys.argv[1])
    except (IndexError, ValueError):
        emit_message("sandbox child requires max_bytes as argv[1]")
        return EXIT_BAD_INVOCATION

    applied = _apply_limits(max_bytes)
    emit_limits(applied)

    data = sys.stdin.buffer.read()

    # Imported AFTER the limits are in place, and deliberately not at module
    # level: pikepdf and PDFium load their native libraries on first use, and
    # that should happen inside the box too.
    from platform.exceptions import DocumentDisarmFailedException, InvalidDocumentException
    from platform.services.document_ingest.disarm import disarm_pdf_to_safe_bytes

    try:
        safe = disarm_pdf_to_safe_bytes(data, max_bytes=max_bytes)
    except InvalidDocumentException as exc:
        emit_message(str(exc.detail if hasattr(exc, "detail") else exc))
        return EXIT_INVALID_DOCUMENT
    except DocumentDisarmFailedException as exc:
        emit_message(str(exc.detail if hasattr(exc, "detail") else exc))
        return EXIT_DISARM_FAILED

    with os.fdopen(sys.stdout.fileno(), "wb", closefd=False) as out:
        out.write(safe)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
