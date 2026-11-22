"""Read selected Codex local rollout files; never scan a remote account."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator


_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_ID = re.compile(_UUID)
_ROLLOUT = re.compile(rf"rollout-[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}T[0-9]{{2}}-[0-9]{{2}}-[0-9]{{2}}-({_UUID})\.jsonl")
_VISIBLE_SOURCE = frozenset({"cli", "vscode", "desktop", "extension", "ide"})
_NON_HUMAN = re.compile(r"sub.?agent|automation|cron|schedul|memory.?consolidation|system", re.I)
_WRAPPER = re.compile(
    r"<\s*/?\s*(?:environment_context|instructions|skill|user_instructions|"
    r"safety_boundary|send_user_message_question_reply|user_shell_command|"
    r"system-reminder|hook_prompt|collaboration_mode|app-context|analysis|think)\b|"
    r"(?im:^\s*#\s*AGENTS\.md instructions\b|^\s*Message Type:\s)", re.I,
)
_MAX_DIR_ENTRIES = 100_000
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_LINE_BYTES = 2 * 1024 * 1024
_MAX_META_BYTES = 256 * 1024
_MAX_DISCOVERY_FILES = 10_000
_MAX_TEXT_CHARS = 15_000
_MAX_TOTAL_CHARS = 1_500_000
_MAX_MESSAGES = 2_000
_QUIET_SECONDS = 120


class CodexSourceError(ValueError):
    """Scope or enumeration failed. Messages have no paths, keys, or content."""


class _Deferred(Exception):
    """A particular selected rollout is unsafe, ambiguous, or not yet ready."""


def _stat(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    # An NTFS junction or other reparse point is not an approved source root.
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise _Deferred
    is_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not is_type(info.st_mode) or (not directory and info.st_nlink > 1):
        raise _Deferred
    return info


def _ancestors(path: Path) -> None:
    for directory in reversed((path, *path.parents)):
        _stat(directory, directory=True)


def _selected_paths(root: Path, allowed: set[str] | None) -> dict[str, Path | None]:
    candidates: dict[str, Path | None] = {}
    walked = 0

    def fail(_: OSError) -> None:
        raise CodexSourceError("Cannot enumerate an approved Codex source directory.")

    for name in ("sessions", "archived_sessions"):
        branch = root / name
        try:
            _stat(branch, directory=True)
        except FileNotFoundError:
            continue
        except (OSError, _Deferred):
            raise CodexSourceError("Codex source branch is inaccessible or linked.") from None
        for where, subdirs, filenames in os.walk(branch, followlinks=False, onerror=fail):
            walked += len(subdirs) + len(filenames)
            if walked > _MAX_DIR_ENTRIES:
                raise CodexSourceError("Codex source directory exceeded the inventory limit.")
            clean = []
            for name in subdirs:
                try:
                    _stat(Path(where) / name, directory=True)
                except (OSError, _Deferred):
                    continue
                clean.append(name)
            subdirs[:] = clean
            for name in filenames:
                match = _ROLLOUT.fullmatch(name)
                if not match:
                    continue
                thread_id = match[1].lower()
                if allowed is not None and thread_id not in allowed:
                    continue
                # Do not choose between conflicting copies, regardless of mtime.
                candidates[thread_id] = None if thread_id in candidates else Path(where) / name
                if len(candidates) > _MAX_DISCOVERY_FILES:
                    raise CodexSourceError("Codex source inventory exceeded the thread limit.")
    return candidates


def _entry(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return (*_entry(info), info.st_ctime_ns)


def _parse_line(raw: bytes) -> dict:
    if len(raw) > _MAX_LINE_BYTES or not raw.endswith(b"\n"):
        raise _Deferred
    try:
        obj = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, RecursionError):
        raise _Deferred from None
    if not isinstance(obj, dict):
        raise _Deferred
    return obj


def _check_meta(obj: dict, thread_id: str, owner: tuple[str, str] | None) -> None:
    meta = obj.get("payload")
    if obj.get("type") != "session_meta" or not isinstance(meta, dict):
        raise _Deferred
    source = meta.get("source")
    creator = (meta.get("creator_account_id"), meta.get("creator_user_id"))
    originator = meta.get("originator")
    if (
        not isinstance(meta.get("id"), str) or meta["id"].lower() != thread_id
        or not isinstance(source, str) or source.lower() not in _VISIBLE_SOURCE
        or meta.get("thread_source") != "user"
        or not all(isinstance(value, str) and value.strip() for value in creator)
        or (owner is not None and creator != owner)
        or (isinstance(originator, str) and _NON_HUMAN.search(originator))
    ):
        raise _Deferred


def _message(obj: dict) -> tuple[str, str | None, dict] | None:
    payload = obj.get("payload")
    if obj.get("type") != "response_item" or not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    if role == "assistant" and payload.get("phase") not in (None, "", "final_answer", "final"):
        return None
    parts = payload.get("content")
    permitted = ("input_text", "text") if role == "user" else ("output_text", "text")
    texts = []
    n_chars = 0
    if not isinstance(parts, list) or not 0 < len(parts) <= 100:
        return role, None, payload
    for part in parts:
        if not isinstance(part, dict) or part.get("type") not in permitted or not isinstance(part.get("text"), str):
            return role, None, payload
        text = part["text"]
        n_chars += len(text)
        if n_chars > _MAX_TEXT_CHARS:
            return role, None, payload
        try:
            text.encode("utf-8", errors="strict")
        except UnicodeEncodeError:
            return role, None, payload
        texts.append(text)
    body = "\n".join(texts).strip()
    return role, body if body and not _WRAPPER.search(body) else None, payload


def _message_id(obj: dict, payload: dict, counters: dict[str, int], role: str, ordinal: int) -> str:
    for container in (payload, obj):
        for key in ("message_id", "id"):
            value = container.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
                return "source-" + hashlib.sha256(f"{role}:{value}".encode("ascii")).hexdigest()[:40]
    stamp = obj.get("timestamp")
    if isinstance(stamp, str) and re.fullmatch(r"[0-9TZtz:+.\-]{10,45}", stamp):
        marker = role + ":" + stamp
        counters[marker] = counters.get(marker, 0) + 1
        anchor = f"{marker}:{counters[marker]}"
        return "time-" + hashlib.sha256(anchor.encode("ascii")).hexdigest()[:40]
    # Last resort only. Edits in place and appends preserve ordinals; adding an
    # earlier same-role message changes identities. Never key by content alone.
    return f"ordinal-{role}-{ordinal}"


def _read_one(path: Path, thread_id: str, owner: tuple[str, str] | None, *, meta_only: bool = False) -> dict:
    _ancestors(path.parent)
    before = _stat(path, directory=False)
    if not 0 < before.st_size <= _MAX_FILE_BYTES or (
        not meta_only and time.time_ns() - before.st_mtime_ns < _QUIET_SECONDS * 10**9
    ):
        raise _Deferred
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    # Use an unbuffered metadata read so foreign sessions' message bytes are
    # not prefetched into process memory before ownership has been checked.
    buffering = 0 if owner is not None else -1
    with os.fdopen(fd, "rb", buffering=buffering) as raw:
        opened = os.fstat(raw.fileno())
        if not stat.S_ISREG(opened.st_mode) or not opened.st_ino or _entry(opened) != _entry(before):
            raise _Deferred
        meta_row = raw.readline(_MAX_META_BYTES + 1)
        if len(meta_row) > _MAX_META_BYTES:
            raise _Deferred
        _check_meta(_parse_line(meta_row), thread_id, owner)
        if meta_only:
            _ancestors(path.parent)
            if _fingerprint(_stat(path, directory=False)) != _fingerprint(before) or _fingerprint(os.fstat(raw.fileno())) != _fingerprint(opened):
                raise _Deferred
            return {}
        # A wrapper on this same open handle starts at byte after the first
        # row. Do not reopen by pathname between metadata and body reads.
        handle = io.BufferedReader(raw) if owner is not None else raw
        counts = {"user": 0, "assistant": 0}
        timestamps: dict[str, int] = {}
        seen: set[str] = set()
        accepted: list[dict] = []
        turn: list[dict] = []
        withheld = False
        size = 0
        while (line := handle.readline(_MAX_LINE_BYTES + 1)):
            if handle.tell() > before.st_size:
                raise _Deferred
            if line in (b"\n", b"\r\n"):
                continue
            obj = _parse_line(line)
            visible = _message(obj)
            if visible is None:
                continue
            role, body, payload = visible
            counts[role] += 1
            if counts["user"] + counts["assistant"] > _MAX_MESSAGES:
                raise _Deferred
            anchor = _message_id(obj, payload, timestamps, role, counts[role])
            if anchor in seen:
                raise _Deferred
            seen.add(anchor)
            if role == "user":
                if not withheld:
                    accepted.extend(turn)
                turn = []
                withheld = body is None
            elif not turn:
                continue
            if body is None:
                withheld = True
                turn.clear()
            elif not withheld:
                size += len(body)
                if size > _MAX_TOTAL_CHARS:
                    raise _Deferred
                turn.append({"id": anchor, "role": role, "text": body})
        # An unfinished last exchange can be in-flight even after the quiet
        # interval. Defer that exchange, not the completed history before it.
        if not withheld and len(turn) > 1:
            accepted.extend(turn)
        if _fingerprint(os.fstat(handle.fileno())) != _fingerprint(opened):
            raise _Deferred
    _ancestors(path.parent)
    if not accepted or _fingerprint(_stat(path, directory=False)) != _fingerprint(before):
        raise _Deferred
    modified = datetime.fromtimestamp(before.st_mtime_ns / 1e9, timezone.utc).isoformat().replace("+00:00", "Z")
    return {"source": "codex", "id": thread_id, "modified": modified, "messages": accepted}


def _scope_root(root: Path) -> Path:
    path = Path(root)
    if not path.is_absolute() or str(path).startswith(("\\\\", "//")) or ".." in path.parts:
        raise CodexSourceError("An absolute, local Codex home with no parent traversal is required.")
    return path


def _owner_pair(account: str | None, user: str | None) -> tuple[str, str]:
    if not all(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) for value in (account, user)):
        raise CodexSourceError("Two exact, verified Codex creator identifiers are required.")
    return account, user


def discover(root: Path, owner_account: str, owner_user: str) -> set[str]:
    """Find human sessions by exact owner pair, reading only first-record metadata."""
    root = _scope_root(root)
    owner = _owner_pair(owner_account, owner_user)
    try:
        _ancestors(root)
        selected = _selected_paths(root, None)
    except (_Deferred, OSError):
        raise CodexSourceError("Cannot safely inventory the approved Codex home.") from None
    found = set()
    for thread_id, path in selected.items():
        if path is None:
            continue
        try:
            _read_one(path, thread_id, owner, meta_only=True)
        except (_Deferred, OSError, ValueError, OverflowError):
            continue
        found.add(thread_id)
    return found


def collect(
    root: Path, allowed_ids: set[str], *, owner_account: str | None = None, owner_user: str | None = None,
) -> Iterator[dict]:
    """Yield visible text for explicitly selected, previously owner-verified UUIDs.

    In owner mode, pass both exact creator values; ownership is checked on the
    same open handle before every body read. Values must be independently verified
    by the caller. Compare selected IDs to yielded IDs for pending inputs.
    """
    root = _scope_root(root)
    owner = None if owner_account is None and owner_user is None else _owner_pair(owner_account, owner_user)
    if not isinstance(allowed_ids, (set, frozenset)) or not 0 < len(allowed_ids) <= 10_000 or any(
        not isinstance(value, str) or _ID.fullmatch(value) is None for value in allowed_ids
    ):
        raise CodexSourceError("A non-empty, owner-verified Codex thread UUID allowlist is required.")
    allowed = {value.lower() for value in allowed_ids}
    if len(allowed) != len(allowed_ids):
        raise CodexSourceError("Codex thread allowlist contains equivalent UUIDs.")
    try:
        _ancestors(root)
        selected = _selected_paths(root, allowed)
    except (_Deferred, OSError):
        raise CodexSourceError("Cannot safely inspect the approved Codex home.") from None
    for thread_id, path in sorted(selected.items()):
        if path is None:
            continue
        try:
            yield _read_one(path, thread_id, owner)
        except (_Deferred, OSError, ValueError, OverflowError):
            continue
