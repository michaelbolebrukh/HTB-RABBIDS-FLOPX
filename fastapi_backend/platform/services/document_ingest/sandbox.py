"""Running the disarm ladder in a throwaway process, so a parser bug cannot
reach the API worker.

WHY THIS EXISTS. Everything downstream of the ladder is allowlist-based and
safe by construction, but the ladder itself has to PARSE the hostile document
before any of that applies — qpdf through pikepdf, then PDFium. Both are large
C/C++ codebases, and a memory-corruption bug in either runs with the worker's
memory and the worker's credentials. No Python-level budget in `rebuild.py` or
`disarm.py` can prevent that, because the bug would be underneath them. This is
the gap containers solve for tools like Dangerzone, and nested containers are
not available on a Render web service.

WHAT IT ACTUALLY BUYS, stated honestly:

- A crash, an OOM, or a hang in the parser kills a child nobody depends on
  rather than the worker. That is not hypothetical here: production was moved
  off the starter tier after an out-of-memory restart at fewer than ten active
  users. Production runs `WEB_CONCURRENCY=2`, so a worker death costs half the
  serving capacity and every request in flight on that worker — checked against
  the live service rather than assumed from gunicorn's single-worker default,
  which does not apply here.
- A successful exploit lands in a process holding NO credentials. The child is
  spawned with a scrubbed environment — no DATABASE_URL, no HASH_KEY, no
  SERVICE_TOKENS, no payment keys — and a fresh interpreter, so there is no
  inherited parent memory to read them out of either. That is why this spawns
  rather than forks.
- Every document gets a process that has never seen another document, and that
  is gone afterwards. Nothing carries between two members' uploads.

WHAT IT DOES NOT BUY. This is process isolation, not a sandbox in the strong
sense. The child shares the kernel and the filesystem, and can still make
network calls. A syscall filter (seccomp) would close most of that, and
`PR_SET_NO_NEW_PRIVS` is already set as its precondition, but no permissively
licensed turnkey filter exists — the available libraries are LGPL — so it is
deliberately left for later rather than hand-rolled here.

THE DEADLINE. The ladder's per-rung budgets do not compose: rungs 2 and 3 each
retry a three-step DPI ladder and the 25-second clock restarts on every step,
and the assembly steps carry no clock at all, so the checked worst case runs to
roughly 170 seconds against a 30-second gunicorn worker timeout. Rather than
re-tune each rung, the parent here enforces ONE wall-clock deadline over the
whole thing and kills the process group when it expires. A hard kill from
outside bounds even the paths that have no budget of their own.
"""

from __future__ import annotations

import logging
import os
import selectors
import signal
import subprocess
import sys
import time
from typing import Optional

from platform.services.document_ingest import _sandbox_child as child_protocol


logger = logging.getLogger(__name__)

# One bound for the whole ladder, sized against gunicorn's 30s worker timeout
# (not overridden in `gunicorn.conf.py`, so the built-in default applies). The
# margin is what lets the request return a real 422 instead of losing the race
# and being SIGKILLed mid-response — which, at one worker, is the whole backend.
SANDBOX_DEADLINE_SECONDS = 22.0

# After SIGKILL, how long to wait for the group to actually die before giving up
# on reaping it. A kill is not instantaneous, and a zombie left behind is worse
# than a slow one.
_REAP_GRACE_SECONDS = 5.0

# The child needs an interpreter, the backend on its path, and nothing else.
# Everything absent from this dict is a credential the parser cannot leak.
_ALLOWED_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "TZ")


def _child_environment() -> dict[str, str]:
    """A deliberately near-empty environment for the parser process."""
    env = {key: os.environ[key] for key in _ALLOWED_ENV_KEYS if key in os.environ}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    # The backend root, so `platform.…` resolves. Derived from this module's own
    # location rather than from an env var, so it stays correct under any
    # working directory and needs no configuration.
    env["PYTHONPATH"] = str(_backend_root())
    # Nothing in the child benefits from writing bytecode, and the directories
    # it would write to are shared.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _backend_root() -> os.PathLike[str] | str:
    from pathlib import Path

    # …/fastapi_backend/platform/services/document_ingest/sandbox.py
    return Path(__file__).resolve().parents[3]


def _pump(
    process: subprocess.Popen,
    data: bytes,
    deadline_seconds: float,
    max_bytes: int,
) -> tuple[bytes, bytes, Optional[str]]:
    """Feeding the child and reading it back under a HARD cap and a real clock.

    `Popen.communicate(timeout=...)` cannot be used here, and the reason is the
    whole point of the module. It accumulates the child's stdout in the PARENT's
    memory with no ceiling, so a child that simply writes in a loop drives the
    worker to gigabytes — measured at ~2.2GB within six seconds — which is an
    out-of-memory kill of the very process this sandbox exists to protect,
    reachable by any exploit that manages even trivial code execution. Its
    timeout is unreliable under that same load too: a sustained-write child was
    measured overrunning a 22s deadline to 48.7s, because the draining work
    starves the deadline check.

    Three things here are less obvious than they look, and each was a measured
    bug before it was a precaution:

    - **Writes are non-blocking.** A pipe holds ~64KB and an upload is megabytes,
      so a child that reads a little and then stalls leaves `os.write` blocked
      inside the kernel where no deadline can reach it. Measured: a 6s deadline
      returning after 30s, and never returning at all against a child that
      stalled permanently. Waiting for writability is not enough on its own.
    - **EOF is not the only way to finish.** The loop cannot wait for both pipes
      to close, because anything the child forked inherits those pipes and holds
      them open after the child exits. Measured: a child that finished in 10ms
      reported as a 22-second DEADLINE FAILURE — a refusal on an upload that had
      actually succeeded. Once the child is gone, its output is complete, so a
      short drain and then stop is correct.
    - **The pipes are closed explicitly.** On the exception path the traceback
      keeps the frame, and the frame keeps the `Popen`, so refcounting cannot
      free the pipes until the cyclic collector runs. Measured: three leaked
      descriptors per raise.

    Returns `(stdout, stderr, overrun_reason)`; `overrun_reason` is None on a
    normal completion.
    """
    # The ladder never legitimately returns more than the document cap; the
    # margin only avoids a race with its own final size check.
    output_ceiling = max_bytes + (1024 * 1024)
    # Bounded independently: stderr carries a short message, never a payload.
    stderr_ceiling = 64 * 1024
    # How long to keep reading after the child has exited, so output already in
    # the pipe is not lost when a survivor holds the write end open.
    drain_grace = 0.25

    deadline = time.monotonic() + deadline_seconds
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    stdout_len = 0
    stderr_len = 0
    pending = memoryview(data)
    drain_until: Optional[float] = None

    selector = selectors.DefaultSelector()

    def _finish_writing() -> None:
        if process.stdin in selector.get_map():
            selector.unregister(process.stdin)

    # Everything after this point is inside the try, INCLUDING the registration
    # and the non-blocking flip. Doing setup outside it meant a raise there
    # skipped the cleanup entirely and leaked the pipes.
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "out")
        selector.register(process.stderr, selectors.EVENT_READ, "err")
        if pending:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, "in")
        else:
            _close_quietly(process.stdin)

        while True:
            now = time.monotonic()
            if now >= deadline:
                return b"", b"", f"exceeded its {deadline_seconds:.0f}s deadline"
            if not selector.get_map():
                break

            # The child being gone means its output is complete, whatever else
            # is still holding the pipe open.
            if process.poll() is not None and drain_until is None:
                drain_until = now + drain_grace
            if drain_until is not None and now >= drain_until:
                break

            timeout = min(deadline - now, 0.5)
            if drain_until is not None:
                timeout = min(timeout, max(0.0, drain_until - now))

            for key, _ in selector.select(timeout=timeout):
                if key.data == "in":
                    try:
                        written = os.write(process.stdin.fileno(), pending[:65536])
                    except BlockingIOError:
                        continue
                    except (BrokenPipeError, OSError):
                        # The child stopped reading. It may already have what it
                        # needs, or it may be dying; either way we stop writing.
                        _finish_writing()
                        _close_quietly(process.stdin)
                        continue
                    pending = pending[written:]
                    if not pending:
                        _finish_writing()
                        _close_quietly(process.stdin)
                    continue

                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except (BlockingIOError, InterruptedError):
                    continue
                except OSError:
                    selector.unregister(key.fileobj)
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "out":
                    stdout_len += len(chunk)
                    if stdout_len > output_ceiling:
                        return b"", b"", (
                            f"wrote more than {output_ceiling} bytes to stdout, "
                            "which no legitimate rebuild does"
                        )
                    stdout_chunks.append(chunk)
                else:
                    stderr_len += len(chunk)
                    if stderr_len <= stderr_ceiling:
                        stderr_chunks.append(chunk)
    finally:
        selector.close()
        # Explicit, not left to refcounting: see the docstring's third point.
        for stream in (process.stdin, process.stdout, process.stderr):
            _close_quietly(stream)

    try:
        process.wait(timeout=max(0.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        return b"", b"", f"did not exit within its {deadline_seconds:.0f}s deadline"

    return b"".join(stdout_chunks), b"".join(stderr_chunks), None


def _close_quietly(stream) -> None:
    """Closing a pipe that may already be broken, without raising."""
    if stream is None or getattr(stream, "closed", False):
        return
    try:
        stream.close()
    except (BrokenPipeError, OSError, ValueError):
        pass


def _spawn_child(max_bytes: int) -> subprocess.Popen:
    """Starting the parser process, in its own session and holding nothing."""
    return subprocess.Popen(
        [sys.executable, "-m", f"{__package__}._sandbox_child", str(max_bytes)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_environment(),
        cwd=str(_backend_root()),
        # Its own session and process group, so a hung child plus anything it
        # spawned can be killed as a unit.
        start_new_session=True,
    )


def _reported_limits(stderr_text: str) -> str:
    """What the child managed to actually apply, straight from the child.

    Whether `setrlimit` takes is a property of the HOST, not of this code, and
    it cannot be established from a developer machine — the limits were measured
    in a container running as root, which is not what production runs as. So the
    child states what it set and it is logged on every rebuild. A deployment
    where these silently do not apply is then visible in one log line instead of
    being an assumption nobody can check.
    """
    if child_protocol.LIMITS_MARKER not in stderr_text:
        return "unreported"
    reported = stderr_text.rsplit(child_protocol.LIMITS_MARKER, 1)[1]
    # The marker is followed by the message marker on a failure path; take the
    # first line only.
    reported = reported.splitlines()[0].strip() if reported.strip() else "NONE"
    if reported == "NONE":
        logger.warning(
            "Disarm sandbox applied NO resource limits on this host; the child "
            "is bounded only by the parent's deadline and kill"
        )
    return reported


def _kill_process_group(process: subprocess.Popen) -> None:
    """Killing the whole group, then reaping it.

    `start_new_session=True` put the child in its own process group, so this
    reaches anything it spawned. Killing the pid alone would leave a grandchild
    orphaned and, on an init that does not reap, permanently zombied.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        process.wait(timeout=_REAP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        logger.error(
            "Disarm sandbox process group did not die after SIGKILL; "
            "leaving it rather than blocking the request further"
        )


def disarm_pdf_in_sandbox(
    data: bytes,
    *,
    max_bytes: Optional[int] = None,
    deadline_seconds: float = SANDBOX_DEADLINE_SECONDS,
) -> bytes:
    """Disarming `data` in a separate, credential-free, resource-bounded process.

    Same contract as `disarm_pdf_to_safe_bytes`: returns safe PDF bytes, or
    raises `InvalidDocumentException` / `DocumentDisarmFailedException`. Blocking
    — call it off the event loop, through the existing `rebuild_limiter`, exactly
    as the in-process version is called.
    """
    from platform.exceptions import DocumentDisarmFailedException, InvalidDocumentException

    if max_bytes is None:
        from infrastructure.settings import settings as s

        max_bytes = int(s.MAX_DOCUMENT_BYTES)

    started = time.monotonic()
    try:
        process = _spawn_child(max_bytes)
    except OSError as exc:
        # A broken interpreter path is an operational fault, not a bad
        # upload, but it must still leave the endpoint by a documented door
        # rather than as an unhandled 500.
        logger.exception("Could not start the disarm sandbox")
        raise DocumentDisarmFailedException(
            "This PDF could not be processed safely. Please try again shortly."
        ) from exc


    try:
        safe, detail, overran = _pump(process, data, deadline_seconds, max_bytes)
    except Exception:
        _kill_process_group(process)
        raise

    if overran is not None:
        _kill_process_group(process)
        logger.warning(
            "Disarm sandbox %s on a %d-byte upload and was killed; refusing "
            "rather than risking the worker",
            overran, len(data),
        )
        raise DocumentDisarmFailedException(
            "This PDF took too long to process safely. Please export a new PDF and try again."
        )

    elapsed = time.monotonic() - started
    code = process.returncode
    # ONLY the marked line. The rest of the child's stderr is Python logging,
    # tracebacks included, and echoing it into an HTTPException detail would
    # ship internal file paths to whoever uploaded the file.
    stderr_text = detail.decode("utf-8", "replace")
    message = ""
    if child_protocol.MESSAGE_MARKER in stderr_text:
        message = stderr_text.rsplit(child_protocol.MESSAGE_MARKER, 1)[1].strip()

    if code == child_protocol.EXIT_OK:
        # Nothing downstream should ever see the input echoed back. The ladder
        # guarantees this itself; re-checking costs one comparison and makes the
        # guarantee hold across a process boundary the ladder cannot see.
        if safe == data:
            raise DocumentDisarmFailedException(
                "This PDF could not be processed safely. Please export a new PDF and try again."
            )
        logger.info(
            "Disarm sandbox rebuilt a %d-byte upload into %d bytes in %.2fs [limits: %s]",
            len(data), len(safe), elapsed, _reported_limits(stderr_text),
        )
        return safe

    if code == child_protocol.EXIT_INVALID_DOCUMENT:
        raise InvalidDocumentException(message or "The uploaded file is not a valid document")
    if code == child_protocol.EXIT_DISARM_FAILED:
        raise DocumentDisarmFailedException(
            message or "This PDF could not be processed safely. Please export a new PDF and try again."
        )

    # Anything else is the case this module exists for: a crash, a signal, an
    # allocation the limits refused. Logged at ERROR because a parser dying on a
    # real member's upload is worth looking at, even though the member gets a
    # clean refusal and the worker is untouched.
    logger.error(
        "Disarm sandbox died with returncode=%s after %.2fs on a %d-byte upload: %s",
        code, elapsed, len(data), stderr_text.strip()[-600:] or "(no output)",
    )
    raise DocumentDisarmFailedException(
        "This PDF could not be processed safely. Please export a new PDF and try again."
    )
