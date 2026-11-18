"""Publish approved topic notes to a dedicated, loopback-only RAGFlow 1.0 archive."""

from __future__ import annotations

import argparse
from bisect import bisect
import contextlib
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from urllib.parse import urlencode, urlsplit
import uuid


HASH = re.compile(r"[0-9a-f]{64}")
ID = re.compile(r"[0-9a-f]{32}")
MAX_NOTE = 1_000_000
MAX_JSON = 8_000_000
BATCH = 40
PARSER = "Parser:HipSignsRhyme"
CHUNKER = "GeneralChunker:SixApplesFall"
TOKENIZER = "Tokenizer:LegalReadersDecide"
EXTRACTOR = "Extractor:AutoExtractDefault"


class ArchiveError(RuntimeError):
    """An error safe for a user-visible log (no body, paths or credential)."""


def config() -> dict:
    """Explicitly disable the model-assisted pieces of the General parser."""
    return {
        PARSER: {
            "enable_vision_enhancement": False,
            "vlm": {"llm_id": ""},
            "doc": {"preprocess": ["main_content"], "suffix": ["doc"]},
            "docx": {"flatten_media_to_text": False, "preprocess": ["main_content"],
                     "suffix": ["docx"], "vlm": {}},
            "html": {"preprocess": ["main_content"], "suffix": ["htm", "html"]},
            "markdown": {"flatten_media_to_text": False, "preprocess": ["main_content"],
                         "suffix": ["md", "markdown", "mdx"], "vlm": {}},
            "pdf": {"flatten_media_to_text": False, "parse_method": "DeepDOC",
                    "preprocess": ["main_content"], "suffix": ["pdf"], "vlm": {},
                    "pages": [[1, 100000]]},
            "spreadsheet": {"flatten_media_to_text": False, "parse_method": "DeepDOC",
                            "preprocess": ["main_content"], "suffix": ["xls", "xlsx", "csv"],
                            "vlm": {}},
            "text&code": {"preprocess": ["main_content"],
                          "suffix": ["txt", "py", "js", "java", "c", "cpp", "h", "php",
                                     "go", "ts", "sh", "cs", "kt", "sql"]},
            "image": {"ocr_enabled": True, "preprocess": ["main_content"],
                      "suffix": ["bmp", "gif", "jpeg", "jpg", "png", "svg", "tif", "tiff", "webp"],
                      "ocr_model_id": ""},
            "email": {"fields": ["from", "to", "cc", "bcc", "date", "subject", "body", "attachments"],
                      "preprocess": ["main_content"], "suffix": ["eml", "msg"]},
        },
        CHUNKER: {
            "children_delimiters": [], "chunk_token_size": 512,
            "delimiters": ["\n", "!", "?", ";", "。", "；", "！", "？"],
            "image_context_size": 0, "overlapped_percent": 0, "table_context_size": 0,
        },
        TOKENIZER: {"fields": "text", "filename_embd_weight": 0.1, "search_method": ["full_text"]},
        EXTRACTOR: {
            "llm_id": "", "keywords": {"top_n": 0, "enabled": False},
            "questions": {"top_n": 0, "enabled": False},
            "tags": {"top_n": 0, "tag_file_id": "", "enabled": False},
            "summary": {"enabled": False},
            "metadata": {"enabled": False, "metadata": [], "built_in_metadata": []},
        },
    }


def assert_config(actual: object) -> None:
    if not isinstance(actual, dict):
        raise ArchiveError("Parser settings cannot be verified.")
    canonical = dict(actual)
    if canonical.get("File") == {}:
        canonical.pop("File")
    chunker = canonical.get(CHUNKER)
    if isinstance(chunker, dict):
        canonical[CHUNKER] = dict(chunker)
        if chunker.get("parent_child") == {"children_delimiter": "\n", "use_parent_child": False}:
            canonical[CHUNKER].pop("parent_child")
    extractor = canonical.get(EXTRACTOR)
    if isinstance(extractor, dict) and isinstance(extractor.get("llm_id"), str):
        # v1.0 may populate a tenant default even for a wholly disabled Extractor.
        # All disabling controls are still required in the exact comparison below.
        canonical[EXTRACTOR] = {**extractor, "llm_id": ""}
    # JSON serialization keeps false distinct from 0; no unknown model or execution node is accepted.
    if json.dumps(canonical, sort_keys=True) != json.dumps(config(), sort_keys=True):
        raise ArchiveError("Full-text parser read-back differs from the dedicated archive profile.")


def checked_id(value: object) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ArchiveError("Invalid dataset or document identifier.")
    return value


def valid_name(name: object) -> bool:
    return (isinstance(name, str) and name.startswith("ChatKB-") and
            7 <= len(name) <= 72 and all(ord(c) >= 32 and ord(c) != 127 for c in name))


def local_origin(origin: str) -> tuple[str, int]:
    if not isinstance(origin, str) or not re.fullmatch(r"http://(?:127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?/?", origin):
        raise ArchiveError("Use an explicit HTTP loopback origin, not a host name or proxy.")
    parsed = urlsplit(origin)
    try:
        port = parsed.port if parsed.port is not None else 80
    except ValueError:
        raise ArchiveError("Invalid RAGFlow loopback port.") from None
    if not 1 <= port <= 65535:
        raise ArchiveError("Invalid RAGFlow loopback port.")
    return str(parsed.hostname), port


class Client:
    def __init__(self, origin: str, token: str):
        self.host, self.port = local_origin(origin)
        if not isinstance(token, str) or not re.fullmatch(r"[\x21-\x7e]{6,1024}", token):
            raise ArchiveError("Read the local RAGFlow API key from a private credential store.")
        self.token = token

    def call(self, method: str, path: str, data=None, *, raw=None, content_type="application/json",
             download=False):
        if data is not None:
            raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        connection = HTTPConnection(self.host, self.port, timeout=15)
        try:
            connection.request(method, path, body=raw, headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": content_type,
                "Accept": "*/*" if download else "application/json",
            })
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                # http.client doesn't follow redirects or honor proxy environment variables.
                raise ArchiveError("RAGFlow HTTP status " + str(response.status) + ".")
            limit = MAX_NOTE + 512 if download else MAX_JSON
            body = response.read(limit + 1)
            if len(body) > limit:
                raise ArchiveError("RAGFlow response exceeded its size limit.")
        except (OSError, HTTPException):
            raise ArchiveError("Local RAGFlow connection interrupted.") from None
        finally:
            connection.close()
        if download:
            return body
        try:
            decoded = json.loads(body)
        except (UnicodeError, ValueError):
            raise ArchiveError("RAGFlow returned an invalid API response.") from None
        if not isinstance(decoded, dict) or type(decoded.get("code")) is not int or decoded["code"] != 0:
            raise ArchiveError("RAGFlow rejected the archive API operation.")
        return decoded.get("data")


def validate_dataset(client: Client, dataset_id: str) -> dict:
    row = client.call("GET", f"/api/v1/datasets/{checked_id(dataset_id)}")
    if (not isinstance(row, dict) or row.get("id") != dataset_id
            or row.get("permission") != "me" or not valid_name(row.get("name"))
            or row.get("parser_id") != "general" or row.get("pipeline_id") not in (None, "")
            or row.get("parse_type") not in (None, 1, "1")
            or row.get("embedding_model") not in (None, "")):
        raise ArchiveError("Target is not a dedicated private General archive without embedding.")
    assert_config(row.get("parser_config"))
    return row


def create_archive_dataset(origin: str, token: str, name: str, language: str = "Chinese") -> str:
    if not valid_name(name):
        raise ArchiveError("Choose a fresh dataset name starting with ChatKB-.")
    if language not in ("Chinese", "English"):
        raise ArchiveError("Choose Chinese or English tokenization.")
    client = Client(origin, token)
    match = client.call("GET", "/api/v1/datasets?" + urlencode({"name": name, "page_size": 2}))
    rows = match.get("datasets") if isinstance(match, dict) else match
    if not isinstance(rows, list) or rows:
        raise ArchiveError("Cannot confirm the archive name is unused; no dataset was changed.")
    row = client.call("POST", "/api/v1/datasets", {
        "name": name, "permission": "me", "language": language,
        "parse_type": 1, "parser_id": "general", "parser_config": config(),
    })
    if not isinstance(row, dict):
        raise ArchiveError("Dataset creation returned an unexpected response; inspect the UI.")
    dataset_id = checked_id(row.get("id"))
    validate_dataset(client, dataset_id)
    return dataset_id


def no_link(s: os.stat_result) -> bool:
    return not (stat.S_ISLNK(s.st_mode) or getattr(s, "st_file_attributes", 0) & 0x400)


def safe_directory(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if path.drive.startswith(("\\\\", "//")) or any(
        not (no_link(s) and stat.S_ISDIR(s.st_mode))
        for s in (p.lstat() for p in (*reversed(path.parents), path))
    ):
        raise ArchiveError("Use a normal private directory, not a link, junction or network share.")
    return path


def read_file(path: Path, limit: int, missing=False):
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing:
            return None
        raise
    if not no_link(before) or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ArchiveError("Archive file is not a normal unlinked file.")
    fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    with path.open("rb") as file:
        opened = os.fstat(file.fileno())
        if fingerprint(opened) != fingerprint(before):
            raise ArchiveError("Archive file changed during read; rerun.")
        data = file.read(limit + 1)
        after = os.fstat(file.fileno())
    if len(data) > limit or fingerprint(after) != fingerprint(before):
        raise ArchiveError("Archive file changed or exceeded its limit; rerun.")
    return data


@contextlib.contextmanager
def exclusive_lock(root: Path):
    lock = root / "ragflow-publish.lock"
    if lock.exists() or lock.is_symlink():
        read_file(lock, 1)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as file:
        if not no_link(lock.lstat()) or os.fstat(file.fileno()).st_nlink != 1:
            raise ArchiveError("Unsafe archive lock file.")
        try:
            if os.name == "nt":
                import msvcrt
                if os.fstat(file.fileno()).st_size == 0:
                    file.write(b"0")
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ArchiveError("Another archive delivery is in progress.") from None
        yield  # Closing the file releases the OS lock, including after a process exits.


def save_state(path: Path, state: dict) -> None:
    encoded = (json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_JSON:
        raise ArchiveError("Archive state reached its size limit.")
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".ragflow-")
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def revision_name(topic: str, digest: str) -> str:
    return f"chatkb_{topic}_{digest}.txt"


def documents(client: Client, base: str, **filters) -> list:
    result = client.call("GET", base + "/documents?" + urlencode(
        {"page": 1, "page_size": 5, **filters}))
    rows = result.get("docs") if isinstance(result, dict) else result
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ArchiveError("RAGFlow returned unexpected document-list fields.")
    return rows


def lookup(client: Client, base: str, dataset_id: str, name: str, document_id: str | None = None):
    rows = documents(client, base, **({"id": document_id} if document_id else {"name": name}))
    if len(rows) > 1:
        raise ArchiveError("Ambiguous archive revision in RAGFlow; manual review required.")
    if not rows:
        return None
    row = rows[0]
    checked_id(row.get("id"))
    if (row.get("dataset_id", row.get("knowledgebase_id")) != dataset_id
            or row.get("name") != name or (document_id and row["id"] != document_id)):
        raise ArchiveError("Document identity did not match the archive revision.")
    return row


def upload(client: Client, base: str, name: str, contents: bytes):
    boundary = "chatkb" + uuid.uuid4().hex
    header = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
              f"filename=\"{name}\"\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n")
    body = header.encode("ascii") + contents + f"\r\n--{boundary}--\r\n".encode("ascii")
    client.call("POST", base + "/documents?type=local", raw=body,
                content_type=f"multipart/form-data; boundary={boundary}")


def status(row: dict) -> str:
    value = str(row.get("ingestion_status", row.get("run", ""))).upper()
    return {"0": "UNSTART", "1": "RUNNING", "2": "CANCEL", "3": "DONE", "4": "FAIL",
            "5": "SCHEDULE", "COMPLETED": "DONE", "FAILED": "FAIL", "STOPPED": "CANCEL",
            "SCHEDULED": "SCHEDULE", "CREATED": "SCHEDULE", "QUEUED": "SCHEDULE"}.get(value, value)


def indexed(client: Client, dataset_id: str, row: dict, digest: str) -> bool:
    if row.get("chunk_count", 0) == 0:
        return False
    identifier = checked_id(row["id"])
    result = client.call("POST", "/api/v1/retrieval", {
        "question": "chatkbrevision " + digest,
        "dataset_ids": [dataset_id], "document_ids": [identifier], "page": 1, "page_size": 5,
        "similarity_threshold": 0, "keywords_similarity_weight": 1, "vector_similarity_weight": 0,
        "rerank_id": "", "keyword": False, "highlight": False, "cross_languages": [],
        "use_kg": False, "include_knowledge_compilation": False,
    })
    chunks = result.get("chunks") if isinstance(result, dict) else None
    if not isinstance(chunks, list):
        raise ArchiveError("RAGFlow did not return verifiable retrieval fields.")
    return any(isinstance(item, dict) and item.get("dataset_id") == dataset_id and
               item.get("document_id") == identifier and isinstance(item.get("content"), str)
               and digest in item["content"] for item in chunks)


def enabled(row: dict) -> bool:
    value = row.get("status")
    if str(value) not in ("0", "1"):
        raise ArchiveError("Unrecognized RAGFlow document availability.")
    return str(value) == "1"


def check_map(state: dict) -> None:
    rows = state.get("topics")
    if not isinstance(rows, dict):
        raise ArchiveError("Invalid archive state.")
    for topic, rec in rows.items():
        if not HASH.fullmatch(topic) or not isinstance(rec, dict) or set(rec) != {"active", "pending"}:
            raise ArchiveError("Invalid topic record in archive state.")
        for kind, item in rec.items():
            if item is None:
                continue
            keys = {"digest", "id"} if kind == "active" else {"digest", "payload", "not_before"}
            if (not isinstance(item, dict) or set(item) != keys or
                    any(not isinstance(item[k], str) or not HASH.fullmatch(item[k])
                        for k in keys & {"digest", "payload"}) or
                    ("not_before" in keys and type(item["not_before"]) is not int)):
                raise ArchiveError("Invalid revision record in archive state.")
            if kind == "active":
                checked_id(item["id"])


def advance(client: Client, base: str, dataset_id: str, topic: str, note: bytes,
            state: dict, state_path: Path) -> str:
    digest = hashlib.sha256(note).hexdigest()
    derivative = ("chatkbrevision " + digest + "\n\n").encode("ascii") + note
    payload_digest = hashlib.sha256(derivative).hexdigest()
    rec = state["topics"].setdefault(topic, {"active": None, "pending": None})
    active = rec["active"]
    pending = rec["pending"]
    if pending is None:
        pending = rec["pending"] = {"digest": digest, "payload": payload_digest, "not_before": 0}
        save_state(state_path, state)
    name = revision_name(topic, pending["digest"])
    row = lookup(client, base, dataset_id, name)
    if row is None:
        if time.time() < pending["not_before"]:
            return "pending"
        if pending["digest"] != digest:
            # The source advanced before this version was uploaded. No prior remote
            # revision was found after a retry window, so move to the current bytes.
            rec["pending"] = None
            save_state(state_path, state)
            return "pending"
        if pending["payload"] != payload_digest:
            raise ArchiveError("Pending revision does not match approved bytes.")
        # Persist before mutation so a lost response is reconciled by exact name and bytes.
        pending["not_before"] = int(time.time()) + 120
        save_state(state_path, state)
        upload(client, base, name, derivative)
        row = lookup(client, base, dataset_id, name)
        if row is None:
            return "pending"
    identifier = checked_id(row["id"])
    content = client.call("GET", f"{base}/documents/{identifier}", download=True)
    if hashlib.sha256(content).hexdigest() != pending["payload"]:
        raise ArchiveError("Remote bytes differ from the pending private note; no switch made.")
    parse_status = status(row)
    if parse_status == "UNSTART":
        # Upload should be unqueued; verify model-free settings before explicitly parsing.
        validate_dataset(client, dataset_id)
        if row.get("parser_id") != "general" or row.get("pipeline_id") not in (None, ""):
            raise ArchiveError("Uploaded text did not inherit the expected General parser.")
        client.call("PATCH", f"{base}/documents/{identifier}", {"parser_config": config()})
        after = lookup(client, base, dataset_id, name, identifier)
        if after is None or after.get("parser_id") != "general" or status(after) != "UNSTART":
            raise ArchiveError("Document parser read-back did not match; not queued.")
        assert_config(after.get("parser_config"))
        client.call("POST", base + "/chunks", {"document_ids": [identifier]})
        return "pending"
    if parse_status in ("SCHEDULE", "RUNNING", "PENDING", "STOPPING"):
        return "pending"
    if parse_status != "DONE":
        raise ArchiveError("Revision did not parse successfully; old revision was retained.")
    if row.get("parser_id") != "general":
        raise ArchiveError("Unexpected parser on completed archive revision.")
    assert_config(row.get("parser_config"))
    if not enabled(row):
        client.call("PATCH", f"{base}/documents/{identifier}", {"enabled": 1})
        row = lookup(client, base, dataset_id, name, identifier)
        if row is None or not enabled(row):
            raise ArchiveError("New revision did not become available.")
    validate_dataset(client, dataset_id)
    if not indexed(client, dataset_id, row, pending["digest"]):
        raise ArchiveError("New revision did not pass scoped search; old revision was retained.")
    if active is not None and active["id"] != identifier:
        old_name = revision_name(topic, active["digest"])
        old = lookup(client, base, dataset_id, old_name, active["id"])
        if old is None:
            raise ArchiveError("Prior revision disappeared; automatic switch was paused.")
        if enabled(old):
            client.call("PATCH", f"{base}/documents/{active['id']}", {"enabled": 0})
            old = lookup(client, base, dataset_id, old_name, active["id"])
            if old is None or enabled(old):
                raise ArchiveError("Prior revision availability could not be verified.")
    rec["active"], rec["pending"] = {"id": identifier, "digest": pending["digest"]}, None
    save_state(state_path, state)
    return "published" if pending["digest"] == digest else "pending"


def publish(root: Path, dataset_id: str, origin: str, token: str, *, allowed_topics: set[str] | None = None) -> dict:
    """Advance a bounded number of approved RAGFlow writes. Reinvoke to confirm queued parses."""
    if allowed_topics is not None and any(not isinstance(t, str) or not HASH.fullmatch(t) for t in allowed_topics):
        raise ArchiveError("Invalid topic allowlist.")
    root = safe_directory(root)
    outbox = safe_directory(root / "outbox")
    state_path = root / "ragflow-state.json"
    client = Client(origin, token)
    base = f"/api/v1/datasets/{checked_id(dataset_id)}"
    counts = {"published": 0, "unchanged": 0, "pending": 0, "failed": 0}
    with exclusive_lock(root):
        dataset = validate_dataset(client, dataset_id)
        target = {"host": client.host, "port": client.port, "id": dataset_id, "name": dataset["name"]}
        raw_state = read_file(state_path, MAX_JSON, missing=True)
        if raw_state is None:
            if dataset.get("document_count") != 0 or documents(client, base):
                raise ArchiveError("A new state can only bind to a fresh dedicated archive dataset.")
            state = {"version": 1, "target": target, "topics": {}}
            save_state(state_path, state)
        else:
            try:
                state = json.loads(raw_state)
            except (UnicodeError, ValueError):
                raise ArchiveError("Archive state could not be read; preserve it for recovery.") from None
        if not isinstance(state, dict) or state.get("version") != 1 or state.get("target") != target:
            raise ArchiveError("State belongs to another archive or incompatible version.")
        check_map(state)
        paths = sorted(p for p in outbox.glob("*.md")
                       if HASH.fullmatch(p.stem) and (allowed_topics is None or p.stem in allowed_topics))
        cursor = state.get("resume_after", "")
        if not isinstance(cursor, str) or (cursor and not HASH.fullmatch(cursor)):
            raise ArchiveError("Archive delivery cursor is invalid.")
        index = bisect([p.stem for p in paths], cursor)
        processed = 0
        if allowed_topics is not None:
            counts["failed"] += len(allowed_topics) - len(paths)
        for path in paths[index:] + paths[:index]:
            try:
                note = read_file(path, MAX_NOTE)
                if not note.strip() or b"\x00" in note:
                    raise ArchiveError("Only nonempty text notes can be indexed.")
                note.decode("utf-8", errors="strict")
                rec = state["topics"].get(path.stem)
                needs_write = (not rec or rec["pending"] is not None or rec["active"] is None
                               or rec["active"]["digest"] != hashlib.sha256(note).hexdigest())
                if needs_write:
                    if processed >= BATCH:
                        counts["pending"] += 1
                        continue
                    processed += 1
                    state["resume_after"] = path.stem
                # No routine API polling on already committed, unchanged notes.
                key = advance(client, base, dataset_id, path.stem, note, state, state_path) if needs_write else "unchanged"
                counts[key] += 1
            except (ArchiveError, OSError, UnicodeError):
                counts["failed"] += 1
                # Safe reason goes to diagnostic field; never include source text, its path or transport body.
                if "note" not in counts:
                    counts["note"] = "Some notes could not be advanced; review the local dataset and archive status."
        if processed:
            save_state(state_path, state)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("create-dataset")
    init.add_argument("name")
    init.add_argument("--language", choices=["Chinese", "English"], default="Chinese")
    send = commands.add_parser("publish")
    send.add_argument("root", type=Path)
    send.add_argument("dataset_id")
    args = parser.parse_args()
    token = os.environ.get("RAGFLOW_API_KEY", "")
    try:
        if args.command == "create-dataset":
            result = {"dataset_id": create_archive_dataset(args.origin, token, args.name, args.language)}
        else:
            result = publish(args.root, args.dataset_id, args.origin, token)
    except (ArchiveError, OSError) as error:
        print(str(error) if isinstance(error, ArchiveError) else "Cannot access the private archive directory.")
        return 1
    print(json.dumps(result))
    return 2 if result.get("pending") or result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
