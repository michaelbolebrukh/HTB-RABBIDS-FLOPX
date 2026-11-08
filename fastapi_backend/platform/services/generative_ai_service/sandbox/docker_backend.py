"""
Docker backend — the first sandbox implementation (Phase 6, D38).

Drives the `docker` CLI through asyncio subprocesses (no docker SDK
dependency, no daemon socket handling of our own). One session = one
container running `sleep infinity`, with the PHASE6 hard-boundary posture
baked into `docker run` flags:

- `--network none` (default; the "allowlist" mode is not implementable with
  plain docker flags and fails loudly rather than approximating);
- non-root (`--user 65534:65534`), `--cap-drop ALL`,
  `--security-opt no-new-privileges`;
- memory/CPU/pids ceilings from SandboxLimits;
- the workspace is a size-capped tmpfs owned by the sandbox user — nothing
  ever touches a host path (files move via tar streams over stdin/stdout).

Dev machines and any docker-capable host only; Render's runtime has no
daemon (the port stays dormant there — see port.py).
"""

from __future__ import annotations

import asyncio
import io
import logging
import tarfile
import time
import uuid

from .types import ExecResult, SandboxExecutionError, SandboxLimits, WorkspaceSpec
from .workspace import materialize_files
from infrastructure.settings import settings

from .port import SandboxSession


logger = logging.getLogger(__name__)

_WORKDIR = "/workspace"
# The sandbox user: `nobody` by uid so it exists in any image:
_SANDBOX_USER = "65534:65534"


async def _run(*argv: str, stdin: bytes | None = None, timeout: float | None = None) -> tuple[int, bytes, bytes]:
    """Running one host-side docker CLI call; returns (exit_code, stdout, stderr):"""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode or 0, stdout, stderr


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    """Packing {path: content} into an in-memory tar (how files enter the container):"""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for path, content in files.items():
            info = tarfile.TarInfo(name=path)
            info.size = len(content)
            info.uid = info.gid = 65534    # Owned by the sandbox user, not root
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(content))
    return buffer.getvalue()


class DockerBackend:
    """The SandboxBackend implementation over the docker CLI."""

    async def start(self, workspace: WorkspaceSpec, limits: SandboxLimits) -> "DockerSandboxSession":
        """Creating the locked-down container and materializing the workspace into it:"""
        if limits.network != "none":
            # Approximating an allowlist with plain docker flags would be a
            # silent security downgrade — refuse until a real implementation
            # (proxy sidecar / hosted backend) exists:
            raise SandboxExecutionError("docker backend supports network='none' only (allowlist: not implemented)")

        name = f"ai-sandbox-{uuid.uuid4().hex[:12]}"
        image = settings.AI_SANDBOX_IMAGE
        code, _, stderr = await _run(
            "docker", "run", "-d", "--name", name,
            "--network", "none",
            "--user", _SANDBOX_USER,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--memory", f"{limits.max_memory_mb}m",
            "--cpus", "1",
            "--pids-limit", "256",
            # The workspace is an in-memory, size-capped mount owned by the
            # sandbox user — writable, executable, gone with the container:
            "--tmpfs", f"{_WORKDIR}:rw,exec,size={limits.max_memory_mb}m,uid=65534,gid=65534",
            "--workdir", _WORKDIR,
            image, "sleep", "infinity",
            timeout=60,
        )
        if code != 0:
            raise SandboxExecutionError(f"docker run failed: {stderr.decode(errors='replace')[:500]}")

        session = DockerSandboxSession(name, limits)
        try:
            files = materialize_files(workspace)
            if files:
                await session._put_tar(_tar_bytes(files))
        except Exception:
            await session.close()          # Never leak a container on a failed start
            raise
        return session


class DockerSandboxSession(SandboxSession):
    """One running sandbox container (destroyed on close)."""

    def __init__(self, name: str, limits: SandboxLimits):
        self._name = name
        self._limits = limits
        self._closed = False

    async def exec(self, command: list[str], timeout_s: int | None = None) -> ExecResult:
        """Executing one command in the workspace under the session's ceilings:"""
        timeout = timeout_s or self._limits.max_seconds
        started = time.monotonic()
        try:
            code, stdout, stderr = await _run(
                "docker", "exec", "-w", _WORKDIR, self._name, *command,
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # A timed-out exec may leave a runaway process — the container is
            # torn down rather than trusted again (nothing persists anyway):
            await self.close()
            duration_ms = int((time.monotonic() - started) * 1000)
            return ExecResult(
                exit_code=124, stdout="",
                stderr=f"execution timed out after {timeout}s (sandbox destroyed)",
                duration_ms=duration_ms, truncated=False,
            )
        duration_ms = int((time.monotonic() - started) * 1000)

        cap = self._limits.max_output_bytes
        truncated = len(stdout) > cap or len(stderr) > cap
        return ExecResult(
            exit_code=code,
            stdout=stdout[:cap].decode(errors="replace"),
            stderr=stderr[:cap].decode(errors="replace"),
            duration_ms=duration_ms,
            truncated=truncated,
        )

    async def put_file(self, path: str, content: bytes) -> None:
        """Writing one file into the workspace (tar over stdin — no host paths):"""
        await self._put_tar(_tar_bytes({path: content}))

    async def _put_tar(self, tar: bytes) -> None:
        """Streaming a tar into the workspace via `docker cp -`:"""
        code, _, stderr = await _run(
            "docker", "cp", "-", f"{self._name}:{_WORKDIR}", stdin=tar, timeout=60,
        )
        if code != 0:
            raise SandboxExecutionError(f"docker cp in failed: {stderr.decode(errors='replace')[:500]}")

    async def get_file(self, path: str) -> bytes:
        """Reading one file back out (tar over stdout — no host paths):"""
        code, stdout, stderr = await _run(
            "docker", "cp", f"{self._name}:{_WORKDIR}/{path}", "-", timeout=60,
        )
        if code != 0:
            raise SandboxExecutionError(f"docker cp out failed: {stderr.decode(errors='replace')[:500]}")
        with tarfile.open(fileobj=io.BytesIO(stdout)) as tar:
            member = tar.getmembers()[0]
            extracted = tar.extractfile(member)
            if extracted is None:
                raise SandboxExecutionError(f"{path!r} is not a regular file")
            return extracted.read()

    async def close(self) -> None:
        """Destroying the container (idempotent; force-remove kills any runaway process):"""
        if self._closed:
            return
        self._closed = True
        code, _, stderr = await _run("docker", "rm", "-f", self._name, timeout=60)
        if code != 0:
            logger.warning("sandbox container %s removal failed: %s", self._name, stderr.decode(errors="replace"))
