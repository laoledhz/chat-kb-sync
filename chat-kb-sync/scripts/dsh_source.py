"""Read explicitly approved DeepSeek Harness v4 conversations without side effects."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any

FILENAME = "session.v4.jsonl.zstd"
SESSION_ID = re.compile(r"session-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
MESSAGE_ID = re.compile(r"[a-zA-Z0-9_.:-]{1,160}\Z")
MAX_COMPRESSED_BYTES = 32 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 250_000
MAX_TEXT_CHARS = 4_000_000
MAX_DIRECTORIES = 100_000
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class _Skip(Exception):
    """Static reason only; never carries data from the source."""


def _plain(info: os.stat_result) -> bool:
    return not (
        stat.S_ISLNK(info.st_mode)
        or getattr(info, "st_file_attributes", 0) & REPARSE_POINT
    )


def _validate_dirs(path: Path) -> None:
    for folder in (path, *path.parents):
        info = folder.lstat()
        if not _plain(info) or not stat.S_ISDIR(info.st_mode):
            raise _Skip("unsafe_path")


def _signature(info: os.stat_result) -> tuple[int, ...]:
    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        info.st_ctime_ns, info.st_mode, info.st_nlink,
    )


def _checked_root(root: Path) -> Path:
    root = Path(root).expanduser().absolute()
    if ".." in root.parts:
        raise ValueError("DeepSeek Harness root must not contain '..' components.")
    try:
        _validate_dirs(root)
    except (_Skip, OSError):
        raise ValueError("DeepSeek Harness root is missing, unreadable, or unsafe.") from None
    return root


def _find(root: Path, allowed: set[str] | None) -> dict[str, Path | None]:
    candidates: dict[str, Path | None] = {}
    for n, (base, dirs, files) in enumerate(os.walk(root, followlinks=False), 1):
        if n > MAX_DIRECTORIES:
            raise RuntimeError("DeepSeek Harness source directory limit exceeded.")
        folder = Path(base)
        safe_dirs = []
        for name in sorted(dirs):
            try:
                info = (folder / name).lstat()
            except OSError:
                continue
            if _plain(info) and stat.S_ISDIR(info.st_mode):
                safe_dirs.append(name)
        dirs[:] = safe_dirs
        sid = folder.name
        wanted = SESSION_ID.fullmatch(sid) if allowed is None else sid in allowed
        if wanted and FILENAME in files:
            # Never choose arbitrarily between two copies of a session.
            candidates[sid] = folder / FILENAME if sid not in candidates else None
    return candidates


def discover(root: Path) -> set[str]:
    """Discover filenames only in an explicitly approved single-user sessions root."""
    found = set()
    for sid, file in _find(_checked_root(root), None).items():
        if file is None:
            continue
        try:
            _validate_dirs(file.parent)
            info = file.lstat()
        except (_Skip, OSError):
            continue
        if _plain(info) and stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            found.add(sid)
    return found


def _read(path: Path, zstd: Any, min_idle_seconds: int) -> tuple[bytes, os.stat_result]:
    _validate_dirs(path.parent)
    before = path.lstat()
    if not _plain(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise _Skip("unsafe_path")
    if not 0 < before.st_size <= MAX_COMPRESSED_BYTES:
        raise _Skip("size_limit")
    if min_idle_seconds and time.time() - before.st_mtime < min_idle_seconds:
        raise _Skip("not_idle")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
                 | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as file:
        if _signature(os.fstat(file.fileno())) != _signature(before):
            raise _Skip("source_changed")
        raw = file.read(MAX_COMPRESSED_BYTES + 1)
        if len(raw) != before.st_size or _signature(os.fstat(file.fileno())) != _signature(before):
            raise _Skip("source_changed")
    _validate_dirs(path.parent)
    if _signature(path.lstat()) != _signature(before):
        raise _Skip("source_changed")
    decoder = zstd.ZstdDecompressor(max_window_size=MAX_EXPANDED_BYTES)
    with decoder.stream_reader(raw, read_across_frames=True) as reader:
        data = reader.read(MAX_EXPANDED_BYTES + 1)
    if len(data) > MAX_EXPANDED_BYTES:
        raise _Skip("size_limit")
    # A v4 record is newline terminated. Never publish a truncated JSONL suffix.
    if not data or not data.endswith(b"\n"):
        raise _Skip("invalid_format")
    return data, before


def _visible_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    return "\n".join(
        block["text"] for block in content
        if isinstance(block, dict) and block.get("type") == "text"
        and isinstance(block.get("text"), str) and block["text"].strip()
    ).strip()


def _only_visible(sid: str, data: bytes) -> list[dict[str, str]]:
    # entries keyed by source-stable IDs; earliest position is the topic anchor,
    # while later user edits with the same ID replace the visible text.
    entries: dict[str, tuple[tuple[int, int, int], int, dict[str, str]]] = {}
    has_header = False
    total_chars = 0

    def add(role: str, msg: Any, seq: int, line_no: int, slot: int, priority: int) -> None:
        nonlocal total_chars
        if not isinstance(msg, dict):
            return
        if role == "user":
            source = msg.get("source")
            if msg.get("role") != "user" or not isinstance(source, dict) or source.get("kind") != "user":
                return
        elif msg.get("role", "assistant") != "assistant":
            return
        upstream = msg.get("id")
        if isinstance(upstream, str) and MESSAGE_ID.fullmatch(upstream):
            stable_id = f"{role}:id:{upstream}"
        elif seq >= 0 and priority > 0 and slot == 0:
            stable_id = f"{role}:seq:{seq}"
        else:
            return
        body = _visible_text(msg.get("content"))
        if not body:
            return
        body.encode("utf-8")
        old = entries.get(stable_id)
        if old and old[1] > priority:
            return
        total_chars += len(body) - (len(old[2]["text"]) if old else 0)
        if total_chars > MAX_TEXT_CHARS:
            raise _Skip("size_limit")
        order = (seq if seq >= 0 else line_no, line_no, slot)
        entries[stable_id] = (
            min(order, old[0]) if old else order, priority,
            {"id": stable_id, "role": role, "text": body},
        )

    # Do not allow JSON parser error messages to escape with session contents.
    from io import BytesIO
    stream = BytesIO(data)
    for n in range(1, MAX_RECORDS + 2):
        raw = stream.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        if n > MAX_RECORDS or len(raw) > MAX_LINE_BYTES:
            raise _Skip("size_limit")
        if not raw.strip():
            continue
        row = json.loads(raw.decode("utf-8"))
        if not isinstance(row, dict):
            raise _Skip("invalid_format")
        kind = row.get("type")
        if not has_header:
            if kind != "session" or row.get("id") != sid:
                raise _Skip("identity_mismatch")
            if (type(row.get("delegationDepth")) is not int or row["delegationDepth"] != 0
                    or row.get("version", 4) not in (4, "4", "v4")):
                raise _Skip("not_supported_main_session")
            has_header = True
            continue
        if kind == "session":
            raise _Skip("invalid_format")
        event = row.get("data")
        if not isinstance(event, dict):
            continue
        seq = row.get("seq")
        seq = seq if type(seq) is int and seq >= 0 else -1
        if kind == "user/message":
            add("user", event, seq, n, 0, 1)
        elif kind == "agent/inbox/spliced" and isinstance(event.get("inserted"), list):
            for slot, message in enumerate(event["inserted"]):
                add("user", message, seq, n, slot, 0)
        elif kind == "assistant/message":
            add("assistant", event.get("message"), seq, n, 0, 1)

    ordered = sorted(entries.values(), key=lambda item: item[0])
    first = next((i for i, item in enumerate(ordered) if item[2]["role"] == "user"), len(ordered))
    return [item[2] for item in ordered[first:]]


def collect(
    root: Path, allowed_ids: set[str], *, min_idle_seconds: int = 0,
    on_skip: Callable[[str], None] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield normalized conversations. Skip events contain a reason code only."""
    if not allowed_ids:
        return
    if any(not isinstance(sid, str) or not SESSION_ID.fullmatch(sid) for sid in allowed_ids):
        raise ValueError("DeepSeek Harness approvals must use exact session UUID directory names.")
    if type(min_idle_seconds) is not int or not 0 <= min_idle_seconds <= 86400:
        raise ValueError("Invalid DeepSeek Harness idle duration.")
    try:
        import zstandard
    except ImportError:
        raise RuntimeError("DeepSeek Harness v4 requires the optional zstandard package.") from None
    candidates = _find(_checked_root(root), allowed_ids)
    for sid in sorted(allowed_ids):
        file = candidates.get(sid)
        if file is None:
            if on_skip:
                on_skip("ambiguous_copy" if sid in candidates else "unavailable")
            continue
        try:
            data, before = _read(file, zstandard, min_idle_seconds)
            messages = _only_visible(sid, data)
            if not messages:
                raise _Skip("no_visible_user_message")
            _validate_dirs(file.parent)
            if _signature(file.lstat()) != _signature(before):
                raise _Skip("source_changed")
            result = {
                "source": "deepseek-harness", "id": sid,
                "modified": datetime.fromtimestamp(before.st_mtime, timezone.utc).isoformat(),
                "messages": messages,
            }
        except _Skip as exc:
            if on_skip:
                on_skip(str(exc))
            continue
        except (OSError, ValueError, OverflowError, RecursionError, zstandard.ZstdError):
            if on_skip:
                on_skip("unreadable_or_invalid")
            continue
        yield result
