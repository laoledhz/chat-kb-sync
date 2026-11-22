"""Index-only same-document updates for model-free, managed RAGFlow text notes."""

import hashlib
import re

# Keep both the UTF-8 request and the default keyword-field length bounded.
MAX_BYTES = 24 * 1024
MAX_CHARS = 9_500
MAX_CHUNKS = 300
CHUNK_ID = re.compile(r"[0-9a-f]{16,64}\Z")


def revised_text(note):
    from ragflow_local import ArchiveError
    value = "chatkbrevision " + hashlib.sha256(note).hexdigest() + "\n\n" + note.decode("utf-8")
    if len(value.encode("utf-8")) > MAX_BYTES or len(value) > MAX_CHARS:
        raise ArchiveError("The note exceeds the index-only update bound; its local draft was retained.")
    return value


def chunks(client, url, dataset_id, document_id):
    from ragflow_local import ArchiveError
    rows = {}
    total = None
    for page in range(1, (MAX_CHUNKS // 100) + 1):
        result = client.call("GET", f"{url}?page={page}&page_size=100")
        if not isinstance(result, dict):
            raise ArchiveError("Unrecognized managed chunk listing.")
        current, batch = result.get("total"), result.get("chunks")
        if type(current) is not int or not 0 < current <= MAX_CHUNKS or not isinstance(batch, list):
            raise ArchiveError("The managed document has no editable chunks or exceeds the safe chunk limit.")
        if total is not None and total != current:
            raise ArchiveError("Managed chunks changed during read-back; retry without editing the document.")
        total = current
        if len(batch) != min(total - len(rows), 100):
            raise ArchiveError("Managed chunk listing is incomplete.")
        for item in batch:
            if not isinstance(item, dict):
                raise ArchiveError("Unrecognized managed chunk row.")
            identifier = item.get("id")
            if (not isinstance(identifier, str) or not CHUNK_ID.fullmatch(identifier) or identifier in rows
                    or item.get("dataset_id") != dataset_id or item.get("document_id") != document_id
                    or type(item.get("available")) is not bool or not isinstance(item.get("content"), str)):
                raise ArchiveError("Chunk identity or availability could not be verified.")
            rows[identifier] = item
        if len(rows) == total:
            return rows
    raise ArchiveError("Managed chunk listing did not finish within its bound.")


def _write_anchor(client, path, text, available):
    # Only managed TXT notes; no imported PDFs, citations to pages, image fields or foreign chunks.
    client.call("PATCH", path, {"content": text, "available": available,
                                "important_keywords": [], "questions": [], "positions": []})


def update(client, base, dataset_id, topic, note, state, state_path):
    import ragflow_local as rf
    rec = state["topics"][topic]
    active = rec["active"]
    if rec["pending"] is not None:
        raise rf.ArchiveError("Resolve the previous publication before changing the index policy.")
    expected = revised_text(note)
    digest = hashlib.sha256(note).hexdigest()
    original = active.get("source_digest", active["digest"])
    identifier = active["id"]
    row = rf.lookup(client, base, dataset_id, rf.revision_name(topic, original), identifier)
    if row is None or not rf.enabled(row) or rf.status(row) != "DONE" or row.get("parser_id") != "general":
        raise rf.ArchiveError("The existing managed text document is not ready for in-place updates.")
    rf.assert_config(row.get("parser_config"))
    path = f"{base}/documents/{identifier}/chunks"
    current = chunks(client, path, dataset_id, identifier)
    anchor_id = min(current)
    anchor = current[anchor_id]
    anchor_path = f"{path}/{anchor_id}"
    changing = anchor["content"] != expected or not anchor["available"]
    try:
        if changing:
            _write_anchor(client, anchor_path, expected, True)
        reloaded = chunks(client, path, dataset_id, identifier)
        if (reloaded.keys() != current.keys() or reloaded[anchor_id]["content"] != expected
                or not reloaded[anchor_id]["available"] or not rf.indexed(client, dataset_id, row, digest)):
            raise rf.ArchiveError("The updated index did not pass scoped search verification.")
    except (rf.ArchiveError, OSError):
        # No old companion chunks have been changed yet. Restore only the
        # anchor written by this attempt, if its identity and bytes still match.
        if changing:
            try:
                snapshot = chunks(client, path, dataset_id, identifier)
                if snapshot.keys() == current.keys() and snapshot[anchor_id]["content"] == expected:
                    _write_anchor(client, anchor_path, anchor["content"], anchor["available"])
            except (rf.ArchiveError, OSError):
                pass  # Draft and last confirmed ledger stay intact; the next run reconciles the same ID.
        raise
    companions = sorted(key for key, item in reloaded.items() if key != anchor_id and item["available"])
    if companions:
        client.call("PATCH", path, {"chunk_ids": companions, "available_int": 0})
    final = chunks(client, path, dataset_id, identifier)
    if (final.keys() != current.keys() or final[anchor_id]["content"] != expected
            or {key for key, item in final.items() if item["available"]} != {anchor_id}):
        raise rf.ArchiveError("The index update is incomplete; retry the same managed document.")
    rec["active"] = {"id": identifier, "digest": digest, "source_digest": original}
    rf.save_state(state_path, state)
    return "published"

