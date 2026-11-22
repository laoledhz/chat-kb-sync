"""Credential-free RAGFlow 1.0 readiness, optionally starting a trusted launcher.

Integration: call ensure_ready before the existing publisher. Only continue when
result.ready; on all other results preserve the same outbox and transaction state
for a later invocation. This module never reads, writes, or resets archive data.

Only a literal loopback HTTP origin and the fixed healthz route are supported.
The dependency schema comes from RAGFlow 1.0 Go HealthzResponse, not generic HTTP
success. This is not cryptographic server identity, authentication, or a complete
archive permission check; the subsequent publisher must retain those checks.

Startup requires allow_start=True plus caller-approved absolute argv for an
existing no-browser launcher. The caller must review its behavior: this module
does not install/restart Docker or discover launchers. Windows CMD/BAT and inline
shell commands are refused. Linux executable scripts and absolute interpreters
with absolute script paths are supported. No command-line string or shell=True.

Deadlines bound network IO (including slow headers/body) and launcher waiting.
OS process creation/path resolution cannot be preempted portably. On a launcher timeout,
subprocess.run kills/waits for its direct child, not services/descendants it may
have started; report the uncertain result, do not automatically launch again.
To start, caller must also supply launcher_stderr, an already-open private regular
file (binary file object or fd >= 3). Both child streams go there, never to public
results; the caller owns permissions, retention and closing it. No error is silently
discarded. Results contain fixed reasons and real numeric exit codes.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import errno
from http.client import HTTPConnection, HTTPException
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import time
from urllib.parse import urlsplit


HEALTH_PATH = "/api/v1/system/healthz"
MAX_HEALTH_BYTES = 16_384
DEPENDENCIES = ("db", "redis", "doc_engine", "storage", "message_queue")


@dataclass(frozen=True)
class ReadinessResult:
    status: str
    reason: str
    http_status: int | None = None
    probes: int = 1
    launcher_attempted: bool = False
    launcher_returncode: int | None = None
    last_status: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"

    @property
    def retryable(self) -> bool:
        return self.status in ("offline", "unhealthy", "timeout")


def _duration(value, maximum: float) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError("invalid_duration")
    return float(value)


def _origin(origin) -> tuple[str, int]:
    if not isinstance(origin, str) or not re.fullmatch(r"http://(?:127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?/?", origin):
        raise ValueError("invalid_origin")
    parsed = urlsplit(origin)
    port = parsed.port if parsed.port is not None else 80
    if not 1 <= port <= 65535:
        raise ValueError("invalid_origin")
    return str(parsed.hostname), port


class _DeadlineSocket(socket.socket):
    """SocketIO.makefile reads call recv_into, reapplying the absolute deadline."""
    def __init__(self, family, deadline):
        super().__init__(family, socket.SOCK_STREAM)
        self.deadline = deadline

    def _set_remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("deadline")
        self.settimeout(remaining)

    def recv_into(self, *args, **kwargs):
        self._set_remaining()
        return super().recv_into(*args, **kwargs)

    def sendall(self, *args, **kwargs):
        self._set_remaining()
        return super().sendall(*args, **kwargs)


class _DeadlineConnection(HTTPConnection):
    def __init__(self, host, port, deadline):
        self.deadline = deadline
        super().__init__(host, port, timeout=max(0.001, deadline - time.monotonic()))

    def connect(self):
        # No hostname resolution, proxies, TLS, redirects, or credential headers.
        family = socket.AF_INET6 if self.host == "::1" else socket.AF_INET
        sock = _DeadlineSocket(family, self.deadline)
        try:
            sock._set_remaining()
            target = (self.host, self.port, 0, 0) if family == socket.AF_INET6 else (self.host, self.port)
            sock.connect(target)
        except BaseException:
            sock.close()
            raise
        self.sock = sock


def _classify_response(response) -> ReadinessResult:
    status = response.status
    if status in (401, 403):
        return ReadinessResult("auth_required", "health_requires_auth", status)
    if 300 <= status < 400:
        return ReadinessResult("foreign_server", "redirect_refused", status)
    if status == 404:
        return ReadinessResult("foreign_server", "health_route_missing", status)
    if status in (502, 504):
        # A front proxy may warm before the API. This never authorizes launching
        # into an occupied port, and cannot be mistaken for verified readiness.
        return ReadinessResult("offline", "gateway_unavailable", status)
    if status not in (200, 500, 503):
        return ReadinessResult("unexpected_http", "unexpected_health_status", status)
    media_type = str(response.getheader("Content-Type", "")).split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        if status == 503:
            return ReadinessResult("offline", "gateway_unavailable", status)
        return ReadinessResult("foreign_server", "health_not_json", status)
    body = response.read(MAX_HEALTH_BYTES + 1)
    if len(body) > MAX_HEALTH_BYTES:
        return ReadinessResult("foreign_server", "health_body_too_large", status)
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        return ReadinessResult("foreign_server", "health_json_invalid", status)
    fields = (*DEPENDENCIES, "status")
    if not isinstance(data, dict) or any(data.get(key) not in ("ok", "nok") for key in fields):
        return ReadinessResult("foreign_server", "health_schema_unrecognized", status)
    all_ok = all(data[key] == "ok" for key in DEPENDENCIES)
    if (data["status"] == "ok") != all_ok or (all_ok and status != 200):
        return ReadinessResult("foreign_server", "health_status_inconsistent", status)
    return ReadinessResult("ready", "healthy", status) if all_ok else ReadinessResult("unhealthy", "dependencies_unhealthy", status)


def probe_health(origin: str, *, timeout: float = 3.0) -> ReadinessResult:
    """One unauthenticated probe; never starts anything or follows redirects."""
    try:
        host, port = _origin(origin)
        timeout = _duration(timeout, 30)
    except (TypeError, ValueError):
        return ReadinessResult("invalid_config", "invalid_probe_settings", probes=0)
    deadline = time.monotonic() + timeout
    connection = _DeadlineConnection(host, port, deadline)
    response = None
    try:
        connection.request("GET", HEALTH_PATH, headers={"Accept": "application/json", "Connection": "close"})
        response = connection.getresponse()
        result = _classify_response(response)
        return result if time.monotonic() <= deadline else ReadinessResult("offline", "probe_timeout")
    except TimeoutError:
        return ReadinessResult("offline", "probe_timeout")
    except OSError as error:
        if error.errno in (errno.ECONNREFUSED, 10061) or getattr(error, "winerror", None) == 10061:
            return ReadinessResult("offline", "connection_refused")
        if error.errno in (errno.ECONNRESET, errno.ECONNABORTED, errno.ETIMEDOUT, 10054, 10053, 10060):
            return ReadinessResult("offline", "connection_interrupted")
        return ReadinessResult("connection_error", "connection_failed")
    except HTTPException:
        return ReadinessResult("foreign_server", "http_protocol_invalid")
    finally:
        if response is not None:
            response.close()
        connection.close()


def _is_windows() -> bool:
    return os.name == "nt"


def _absolute_existing(value, *, directory=False) -> Path:
    if not isinstance(value, str) or not value or any(c in value for c in ("\x00", "\r", "\n")) or value.startswith(("\\\\", "//")):
        raise ValueError("invalid_path")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("absolute_path_required")
    resolved = path.resolve(strict=True)
    # Reject symlinks/junctions and traversal instead of silently changing target.
    if os.path.normcase(str(path)) != os.path.normcase(str(resolved)):
        raise ValueError("redirected_path")
    if not (resolved.is_dir() if directory else resolved.is_file()):
        raise ValueError("missing_launcher_path")
    return resolved


def _checked_launcher(argv, cwd) -> tuple[list[str], str]:
    if not isinstance(argv, (list, tuple)) or not 1 <= len(argv) <= 32 or any(not isinstance(a, str) or not a or any(c in a for c in ("\x00", "\r", "\n")) for a in argv):
        raise ValueError("explicit_argv_required")
    if sum(len(a) for a in argv) > 8192:
        raise ValueError("argv_too_long")
    if any(a.casefold() in ("-c", "/c", "-command", "-encodedcommand", "-enc", "-commandwithargs") for a in argv[1:]):
        raise ValueError("inline_command_refused")
    executable = _absolute_existing(argv[0])
    if executable.suffix.lower() in (".cmd", ".bat") or executable.name.casefold() in ("cmd", "cmd.exe"):
        raise ValueError("implicit_shell_refused")
    if _is_windows() and executable.suffix.lower() != ".exe":
        raise ValueError("windows_executable_required")
    if not _is_windows() and not os.access(executable, os.X_OK):
        raise ValueError("executable_permission_required")
    parent = executable.parent
    if executable.name.casefold() in ("powershell.exe", "pwsh.exe", "pwsh"):
        lower = [a.casefold() for a in argv]
        prefix = ["-noprofile", "-noninteractive", "-file"] if "-noninteractive" in lower[:4] else ["-noprofile", "-file"]
        if lower[1:len(prefix) + 1] != prefix or len(argv) < len(prefix) + 2:
            raise ValueError("powershell_file_only")
        script = _absolute_existing(argv[len(prefix) + 1])
        if script.suffix.lower() != ".ps1":
            raise ValueError("powershell_script_required")
        parent = script.parent
    else:
        name = executable.stem.casefold() if executable.suffix.lower() == ".exe" else executable.name.casefold()
        interpreters = {"sh", "bash", "dash", "zsh", "ksh", "fish", "node", "nodejs", "ruby", "perl", "php", "lua"}
        if name in interpreters or re.fullmatch(r"python(?:[23](?:\.\d+)?)?", name):
            # No combined -lc/-ec, module lookup or inline eval: a concrete file
            # must be the first interpreter argument; remaining args belong to it.
            if len(argv) < 2:
                raise ValueError("interpreter_script_required")
            script = _absolute_existing(argv[1])
            parent = script.parent
    for arg in argv[1:]:
        if not arg.startswith("-") and Path(arg).suffix.lower() in (".ps1", ".py", ".sh", ".bash"):
            _absolute_existing(arg)
    directory = _absolute_existing(cwd, directory=True) if cwd is not None else parent
    return list(argv), str(directory)


def _launcher_environment():
    # Retain Docker discovery and Windows settings, not archive/provider tokens.
    exact = {"RAGFLOW_API_KEY", "DOCKER_AUTH_CONFIG", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
    return {key: value for key, value in os.environ.items()
            if key.upper() not in exact and not key.upper().endswith(("_API_KEY", "_TOKEN", "_PASSWORD", "_SECRET"))}


def _checked_diagnostics(destination):
    if hasattr(destination, "writable") and not destination.writable():
        raise ValueError("writable_private_diagnostics_required")
    fd = destination if type(destination) is int else destination.fileno() if hasattr(destination, "fileno") else None
    if type(fd) is not int or fd < 3 or not stat.S_ISREG(os.fstat(fd).st_mode):
        raise ValueError("private_regular_diagnostics_required")
    return fd


def _run_launcher(argv, cwd, timeout, *, deadline=None, launcher_stderr=None) -> tuple[str, int | None]:
    deadline = min(deadline, time.monotonic() + timeout) if deadline is not None else time.monotonic() + timeout
    try:
        command, directory = _checked_launcher(argv, cwd)
        diagnostics = _checked_diagnostics(launcher_stderr)
    except (ValueError, TypeError, OSError):
        return "invalid_config", None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "launcher_timeout", None
    try:
        result = subprocess.run(command, cwd=directory, env=_launcher_environment(), shell=False,
                                stdin=subprocess.DEVNULL, stdout=diagnostics, stderr=diagnostics,
                                timeout=remaining, check=False,
                                creationflags=0x08000000 if _is_windows() else 0)
    except subprocess.TimeoutExpired:
        return "launcher_timeout", None
    except (OSError, ValueError):
        return "launcher_failed", None
    return ("ok", 0) if result.returncode == 0 else ("launcher_failed", result.returncode)


def ensure_ready(origin: str, *, total_timeout: float = 60.0, probe_timeout: float = 3.0,
                 poll_interval: float = 1.0, allow_start: bool = False, launcher_argv=None,
                 launcher_timeout: float = 30.0, launcher_cwd: str | None = None,
                 launcher_stderr=None) -> ReadinessResult:
    """Wait within one total budget; optionally run one explicitly trusted launcher.

    Only an unambiguous connection refusal permits starting. An existing unhealthy
    service is waited on, never restarted. Auth, redirects, foreign responses and
    launcher failures are terminal. No successful process exit is treated as health.
    """
    try:
        _origin(origin)
        total_timeout = _duration(total_timeout, 300)
        probe_timeout = _duration(probe_timeout, 30)
        poll_interval = _duration(poll_interval, 30)
        launcher_timeout = _duration(launcher_timeout, 120)
        if type(allow_start) is not bool or poll_interval < 0.05:
            raise ValueError("invalid_readiness_settings")
    except (ValueError, TypeError):
        return ReadinessResult("invalid_config", "invalid_readiness_settings", probes=0)
    deadline = time.monotonic() + total_timeout
    probes, launched, exit_code = 0, False, None
    last_status = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ReadinessResult("timeout", "readiness_deadline", probes=probes, launcher_attempted=launched,
                                   launcher_returncode=exit_code, last_status=last_status)
        result = probe_health(origin, timeout=min(probe_timeout, remaining))
        probes += 1
        last_status = result.status
        result = replace(result, probes=probes, launcher_attempted=launched, launcher_returncode=exit_code)
        if result.ready and time.monotonic() > deadline:
            continue
        if result.ready or not result.retryable:
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        if allow_start and not launched and result.status == "offline" and result.reason == "connection_refused":
            launched = True
            status, exit_code = _run_launcher(launcher_argv, launcher_cwd, min(launcher_timeout, remaining),
                                             deadline=deadline, launcher_stderr=launcher_stderr)
            if status != "ok":
                return ReadinessResult(status, status, probes=probes, launcher_attempted=True,
                                       launcher_returncode=exit_code, last_status=last_status)
            continue  # A successful launcher exit still requires a real health response.
        time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
