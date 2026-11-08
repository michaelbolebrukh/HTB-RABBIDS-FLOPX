"""The disarm sandbox: what it must guarantee when the parser misbehaves.

These exercise the real child process, not a mock of it. The whole point of the
module is what happens when a native parser crashes, hangs or eats memory, and
a stubbed subprocess would prove none of it.
"""

import errno
import fcntl
import hashlib
import io
import os
import resource
import signal
import socket
import subprocess
import sys
import time

import pytest

from platform.exceptions import DocumentDisarmFailedException, InvalidDocumentException
from platform.services.document_ingest import _sandbox_child as child_protocol
from platform.services.document_ingest.sandbox import (
    _backend_root,
    _child_environment,
    _pump,
    disarm_pdf_in_sandbox,
)


def _pdf(pages: int = 1, lines: int = 40) -> bytes:
    """A small, ordinary PDF — the shape a real CV takes."""
    reportlab = pytest.importorskip("reportlab", reason="reportlab not installed")
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    for _ in range(pages):
        c.setFont("Helvetica", 9)
        for i in range(lines):
            c.drawString(20 * mm, 260 * mm - i * 12, "quant python derivatives portfolio")
        c.showPage()
    c.save()
    return buf.getvalue()


def test_a_real_document_round_trips_and_is_never_echoed_back():
    data = _pdf()
    safe = disarm_pdf_in_sandbox(data)
    assert safe, "the sandbox returned nothing for an ordinary document"
    # The invariant the whole ingest path rests on: the bytes a partner firm
    # eventually downloads are never the bytes that were uploaded.
    assert safe != data
    assert safe.lstrip()[:5] == b"%PDF-"


def test_the_child_environment_carries_no_credentials(monkeypatch):
    """The reason this spawns instead of forking.

    A forked child would inherit the parent's memory and could read these out
    of it. A spawned child gets only what this dict contains, so the dict is
    the security boundary and is worth pinning.
    """
    for leaky in (
        "HASH_KEY",
        "DATABASE_URL",
        "SERVICE_TOKENS",
        "STRIPE_SECRET_KEY",
        "OPENAI_API_KEY",
        "CLOUDFLARE_API_TOKEN",
        "INBOX_IMAP_PASSWORD",
    ):
        monkeypatch.setenv(leaky, "must-not-be-inherited")

    env = _child_environment()

    assert "must-not-be-inherited" not in env.values()
    # An allowlist, not a denylist: a credential added to the app tomorrow is
    # excluded by default rather than needing to be remembered here.
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "TZ", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}


def test_work_that_outlasts_the_deadline_is_killed_rather_than_left_running():
    """The bound that keeps a slow parse from reaching the worker timeout."""
    data = _pdf(pages=3)
    with pytest.raises(DocumentDisarmFailedException):
        # Far below what even a fast run needs, so this measures the deadline
        # rather than the document.
        disarm_pdf_in_sandbox(data, deadline_seconds=0.05)


def test_a_killed_child_leaves_no_zombie():
    data = _pdf(pages=3)
    with pytest.raises(DocumentDisarmFailedException):
        disarm_pdf_in_sandbox(data, deadline_seconds=0.05)
    # Reap anything reapable; a correctly-killed group leaves nothing behind.
    reaped = []
    try:
        while True:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                break
            reaped.append(pid)
    except ChildProcessError:
        pass
    assert reaped == [], f"the sandbox left unreaped children: {reaped}"


def test_input_that_is_not_a_document_is_refused_as_invalid():
    with pytest.raises(InvalidDocumentException):
        disarm_pdf_in_sandbox(b"this is plainly not a pdf" * 40)


def test_an_empty_upload_is_refused_as_invalid():
    with pytest.raises(InvalidDocumentException):
        disarm_pdf_in_sandbox(b"")


def test_a_document_over_the_cap_is_refused_without_being_parsed():
    # max_bytes is threaded in rather than read from settings precisely so the
    # child needs no configuration; this pins that the threaded value is used.
    with pytest.raises(InvalidDocumentException):
        disarm_pdf_in_sandbox(_pdf(), max_bytes=128)


def test_the_child_refuses_to_run_without_its_size_cap():
    """The child's argv contract, checked against the real module.

    A child started without a cap must not silently pick one — it exits with a
    distinct code so the parent reports a bug rather than a bad upload.
    """
    result = subprocess.run(
        [sys.executable, "-m", "platform.services.document_ingest._sandbox_child"],
        input=b"",
        capture_output=True,
        env=_child_environment(),
        cwd=str(_backend_root()),
    )
    assert result.returncode == child_protocol.EXIT_BAD_INVOCATION


def test_the_child_runs_with_no_application_environment_at_all():
    """The property that makes the isolation worth having.

    If the ladder ever regains a dependency on settings, this fails — and it
    fails here, in the suite, rather than as a 500 on a member's upload.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import platform.services.document_ingest.disarm as d;"
            "print(bool(d.disarm_pdf_to_safe_bytes))",
        ],
        capture_output=True,
        # No DATABASE_URL, no HASH_KEY, no APP_DOMAIN: settings cannot construct
        # here, and the ladder must not need it to.
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PYTHONPATH": str(_backend_root())},
        cwd=str(_backend_root()),
    )
    assert result.returncode == 0, result.stderr.decode()[-600:]


def test_a_refusal_carries_no_internal_paths_or_tracebacks():
    """What a member is shown must not be the child's log stream.

    The ladder logs a warning WITH a traceback every time it degrades a rung,
    and that goes to the child's stderr. Treating stderr as the message put
    ~5.5KB of stack frames and server file paths into the 422 body.
    """
    with pytest.raises((InvalidDocumentException, DocumentDisarmFailedException)) as caught:
        disarm_pdf_in_sandbox(b"%PDF-1.4 " + b"\x00" * 4000)

    detail = str(getattr(caught.value, "detail", caught.value))
    assert "Traceback" not in detail
    assert "fastapi_backend" not in detail
    assert ".py" not in detail
    assert len(detail) < 300, f"refusal message is a log dump ({len(detail)} chars)"


def _rss_bytes(raw_maxrss: int, host: str) -> int:
    # getrusage(2) reports bytes on macOS, KiB on Linux. Comparing the raw
    # values made the intended 200 MiB guard accidentally permit 200 KiB on
    # macOS, while its failure message misreported KiB as MiB.
    return raw_maxrss if host == "darwin" else raw_maxrss * 1024


@pytest.mark.parametrize("raw,host", [(4096, "linux"), (4_194_304, "darwin")])
def test_max_rss_units_are_normalized_to_bytes(raw, host):
    assert _rss_bytes(raw, host) == 4_194_304


def test_a_child_that_floods_stdout_cannot_grow_the_parent():
    """The isolation's own channel must not become a memory sink.

    `communicate()` buffers the child's stdout in the PARENT with no ceiling; a
    child looping on write drove the parent to gigabytes, which is a worker OOM
    reachable from inside the box this module claims to contain. The read is
    capped instead.
    """
    firehose = "import os\nwhile True: os.write(1, b'x' * 1048576)\n"
    process = subprocess.Popen(
        [sys.executable, "-c", firehose],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        cap = 4 * 1024 * 1024
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        _, _, overrun = _pump(process, b"", 20.0, cap)
        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    finally:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=5)
        except Exception:
            pass

    assert overrun is not None, "the flood was accepted instead of being cut off"
    # The same real cap plus slack on both hosts, nowhere near unbounded.
    growth_bytes = _rss_bytes(after - before, sys.platform)
    assert growth_bytes < 200 * 1024 * 1024, f"parent grew {growth_bytes / 1024**2:.0f}MiB"


def test_the_deadline_holds_even_while_the_child_is_writing_hard():
    """A sustained-write child overran `communicate()`'s own 22s timeout to 48.7s."""
    slow_firehose = (
        "import os, time\n"
        "while True:\n"
        "    os.write(1, b'x' * 4096)\n"
        "    time.sleep(0.001)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", slow_firehose],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        started = time.monotonic()
        # A cap far above what it can write in the deadline, so this measures the
        # CLOCK rather than the byte ceiling.
        _, _, overrun = _pump(process, b"", 1.0, 512 * 1024 * 1024)
        elapsed = time.monotonic() - started
    finally:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            process.wait(timeout=5)
        except Exception:
            pass

    assert overrun is not None
    assert elapsed < 3.0, f"a 1s deadline took {elapsed:.1f}s to fire"


def _raw_child(code: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _kill(process: subprocess.Popen) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=5)
    except Exception:
        pass


def _open_fds() -> int:
    # Both are live kernel descriptor directories, including pipes and sockets
    # that a count of ordinary open files would miss. Keep the existing procfs
    # observation on Linux; macOS exposes the equivalent observation via fdesc.
    directory = "/dev/fd" if sys.platform == "darwin" else "/proc/self/fd"
    return len(os.listdir(directory))


def test_the_descriptor_counter_tracks_live_handles(tmp_path):
    """Calibrate the real observation before trusting a leak guard.

    A counter that misses pipes, sockets or high numbered descriptors could
    make the exception cleanup test pass without observing what it protects.
    """
    baseline = _open_fds()
    with (tmp_path / "descriptor-counter").open("w+b") as stream:
        assert _open_fds() == baseline + 1
        read_fd, write_fd = os.pipe()
        high_fd = None
        try:
            high_fd = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD, 64)
            assert high_fd >= 64
            assert _open_fds() == baseline + 4
            with socket.socket(socket.AF_UNIX) as unix_socket:
                assert unix_socket.fileno() >= 0
                assert _open_fds() == baseline + 5
            assert _open_fds() == baseline + 4
        finally:
            if high_fd is not None:
                os.close(high_fd)
            os.close(read_fd)
            os.close(write_fd)
        assert _open_fds() == baseline + 1
    assert _open_fds() == baseline


def test_a_survivor_holding_the_pipe_does_not_fail_a_finished_document():
    """A successful upload must not be reported as a deadline failure.

    Anything the child forks inherits its stdout, so waiting for EOF waits for
    the survivor, not the child. Measured before the fix: a child that finished
    in 10ms was refused after the full 22 seconds.
    """
    child = _raw_child(
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    time.sleep(60)\n"
        "    os._exit(0)\n"
        "os.write(1, b'done')\n"
        "sys.exit(0)\n"
    )
    try:
        started = time.monotonic()
        out, _, overrun = _pump(child, b"", 20.0, 1024 * 1024)
        elapsed = time.monotonic() - started
    finally:
        _kill(child)

    assert overrun is None, f"a completed document was refused: {overrun}"
    assert out == b"done"
    assert elapsed < 5.0, f"took {elapsed:.1f}s waiting on a survivor"


def test_a_child_that_stops_reading_midway_cannot_wedge_the_write():
    """The write must not block in the kernel past the deadline.

    A pipe holds ~64KB; an upload is megabytes. A child that reads a little then
    stalls left `os.write` blocked where no deadline could reach it — measured
    at 30s against a 6s deadline, and never returning at all when the child
    stalled permanently.
    """
    child = _raw_child(
        "import sys, time\nsys.stdin.buffer.read(1024)\nwhile True: time.sleep(1)\n"
    )
    try:
        started = time.monotonic()
        _, _, overrun = _pump(child, b"x" * (8 * 1024 * 1024), 2.0, 16 * 1024 * 1024)
        elapsed = time.monotonic() - started
    finally:
        _kill(child)

    assert overrun is not None
    assert elapsed < 6.0, f"a 2s deadline took {elapsed:.1f}s"


def test_a_large_upload_arrives_intact_across_many_partial_writes():
    payload = os.urandom(3_000_017)  # deliberately not a multiple of the chunk
    child = _raw_child(
        "import sys, hashlib\n"
        "h = hashlib.sha256()\n"
        "r = sys.stdin.buffer\n"
        "while True:\n"
        "    b = r.read(4096)\n"
        "    if not b: break\n"
        "    h.update(b)\n"
        "sys.stdout.write(h.hexdigest())\n"
    )
    try:
        out, _, overrun = _pump(child, payload, 30.0, 64 * 1024 * 1024)
    finally:
        _kill(child)

    assert overrun is None
    assert out.decode() == hashlib.sha256(payload).hexdigest()


def test_the_exception_path_does_not_leak_descriptors():
    """The traceback keeps the frame, which keeps the pipes, so refcounting
    cannot free them — they are closed explicitly instead."""
    baseline = _open_fds()
    failures = []
    for _ in range(15):
        child = _raw_child("import time; time.sleep(30)")
        try:
            # Force _pump to raise during setup, the path that used to skip
            # cleanup entirely.
            os.close(child.stdin.fileno())
            _pump(child, b"data", 5.0, 1024)
        except OSError as error:
            # Keep the traceback and its child pipes alive until measurement.
            # Discarding it lets refcount cleanup hide missing explicit closes.
            failures.append(error)
        finally:
            _kill(child)

    assert len(failures) == 15
    assert all(error.errno == errno.EBADF for error in failures)
    # No gc.collect(): the point is that cleanup does not depend on it.
    assert _open_fds() - baseline < 5, (
        f"leaked {_open_fds() - baseline} descriptors over 15 raises"
    )
