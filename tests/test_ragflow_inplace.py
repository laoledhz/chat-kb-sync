"""Synthetic regression tests; no service, network, credentials, or chat access."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

SCRIPTS = Path(__file__).resolve().parents[1] / "chat-kb-sync" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import ragflow_inplace as inplace
import ragflow_local as rf


DATASET = "1" * 32
DOCUMENT = "2" * 32
FOREIGN = "3" * 32
TOPIC = "4" * 64
ANCHOR, COMPANION, DISABLED = "a" * 16, "b" * 16, "c" * 16
ORIGIN = "http://127.0.0.1:8080"
BASE = f"/api/v1/datasets/{DATASET}"
CHUNK_PATH = f"{BASE}/documents/{DOCUMENT}/chunks"
ANCHOR_PATH = f"{CHUNK_PATH}/{ANCHOR}"
RETRIEVAL = "/api/v1/retrieval"
OLD_NOTE = b"# A fictional pump, revision one."
NEW_NOTE = "# 虚构水泵记录：修订校准结果".encode()


def sha(note):
    return hashlib.sha256(note).hexdigest()


def index_text(note):
    return f"chatkbrevision {sha(note)}\n\n{note.decode('utf-8')}"


class FakeRagflow:
    host, port = "127.0.0.1", 8080

    def __init__(self):
        self.dataset = {"id": DATASET, "name": "ChatKB-Synthetic", "permission": "me",
                        "parser_id": "general", "parser_config": rf.config(), "document_count": 1}
        self.document = {"id": DOCUMENT, "name": rf.revision_name(TOPIC, sha(OLD_NOTE)),
                         "dataset_id": DATASET, "parser_id": "general", "parser_config": rf.config(),
                         "status": 1, "run": "DONE", "chunk_count": 3}
        self.rows = {
            key: {"id": key, "dataset_id": DATASET, "document_id": DOCUMENT,
                  "content": content, "available": available}
            for key, content, available in (
                (ANCHOR, index_text(OLD_NOTE), True),
                (COMPANION, "Old companion text", True),
                (DISABLED, "Already disabled", False),
            )
        }
        self.total_override = None
        self.bad_retrieval = False
        self.ignore_deactivate_once = False
        self.calls = []

    def call(self, method, path, data=None, **kwargs):
        self.calls.append((method, path, deepcopy(data)))
        if kwargs:
            raise AssertionError(f"Unexpected transport options for {method} {path}")
        parsed = urlsplit(path)
        if method == "GET" and path == BASE:
            return deepcopy(self.dataset)
        if method == "GET" and parsed.path == BASE + "/documents":
            query = parse_qs(parsed.query)
            if self.document and query.get("id") != [DOCUMENT]:
                raise AssertionError("In-place update must look up the original document ID")
            return {"docs": [deepcopy(self.document)] if self.document else []}
        if method == "GET" and parsed.path == CHUNK_PATH:
            query = parse_qs(parsed.query)
            if query.get("page_size") != ["100"] or "page" not in query:
                raise AssertionError("Managed chunk read-back must use bounded pages")
            page = int(query["page"][0])
            rows = list(reversed(self.rows.values()))  # Service order is not anchor order.
            start = (page - 1) * 100
            total = len(rows) if self.total_override is None else self.total_override
            return {"chunks": deepcopy(rows[start:start + 100]), "total": total}
        if method == "PATCH" and path == ANCHOR_PATH:
            if set(data) != {"content", "available", "important_keywords", "questions", "positions"}:
                raise AssertionError("Unexpected anchor PATCH fields")
            self.rows[ANCHOR].update(content=data["content"], available=data["available"])
            return None
        if method == "PATCH" and path == CHUNK_PATH:
            if set(data) != {"chunk_ids", "available_int"} or data["available_int"] != 0:
                raise AssertionError("Unexpected chunk deactivation PATCH")
            if self.ignore_deactivate_once:
                self.ignore_deactivate_once = False
                return None  # An accepted response is not proof of deactivation.
            for key in data["chunk_ids"]:
                self.rows[key]["available"] = False
            return None
        if method == "POST" and path == RETRIEVAL:
            if (data["dataset_ids"], data["document_ids"]) != ([DATASET], [DOCUMENT]):
                raise AssertionError("Search must be restricted to the original document")
            if (data["keywords_similarity_weight"], data["vector_similarity_weight"], data["rerank_id"]) != (1, 0, ""):
                raise AssertionError("Search must be full text and model-free")
            if data["use_kg"] or data["include_knowledge_compilation"] or data["cross_languages"]:
                raise AssertionError("Unexpected model-assisted search configuration")
            found = [deepcopy(row) for row in self.rows.values()
                     if row["available"] and data["question"] in row["content"]]
            if self.bad_retrieval:
                for row in found:
                    row["document_id"] = FOREIGN  # Even matching text from another doc is not proof.
            return {"chunks": found}
        raise AssertionError(f"Unexpected request: {method} {path}")

    def writes(self):
        return [(method, path, data) for method, path, data in self.calls
                if method != "GET" and path != RETRIEVAL]


class InplacePublishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.outbox = self.root / "outbox"
        self.outbox.mkdir()
        self.note = self.outbox / (TOPIC + ".md")
        self.note.write_bytes(NEW_NOTE)
        self.state_path = self.root / "ragflow-state.json"
        self.client = FakeRagflow()
        self.active = {"id": DOCUMENT, "digest": sha(OLD_NOTE)}
        rf.save_state(self.state_path, {
            "version": 1, "target": {"id": DATASET, "name": self.client.dataset["name"],
                                     "host": self.client.host, "port": self.client.port},
            "update_mode": "inplace-index", "topics": {TOPIC: {"active": self.active, "pending": None}},
        })
        client_patch = patch.object(rf, "Client", return_value=self.client)
        client_patch.start()
        self.addCleanup(client_patch.stop)

    def publish(self):
        return rf.publish(self.root, DATASET, ORIGIN, "fake-token", allowed_topics={TOPIC},
                          update_mode="inplace-index")

    def ledger(self):
        return json.loads(self.state_path.read_bytes())["topics"].get(TOPIC)

    def assert_failure(self):
        result = self.publish()
        self.assertEqual((result["published"], result["failed"], result["pending"]), (0, 1, 0))
        self.assertEqual(self.ledger(), {"active": self.active, "pending": None})

    def test_original_document_updated_and_only_active_companions_disabled(self):
        self.assertEqual(self.publish(), {"published": 1, "unchanged": 0, "pending": 0, "failed": 0})
        writes = self.client.writes()
        self.assertEqual([(m, p) for m, p, _ in writes], [("PATCH", ANCHOR_PATH), ("PATCH", CHUNK_PATH)])
        self.assertEqual(writes[0][2]["content"], index_text(NEW_NOTE))
        self.assertEqual(writes[1][2], {"chunk_ids": [COMPANION], "available_int": 0})
        self.assertEqual({k for k, row in self.client.rows.items() if row["available"]}, {ANCHOR})
        before_disable = self.client.calls.index(writes[1])
        self.assertTrue(any(p == RETRIEVAL for _, p, _ in self.client.calls[:before_disable]))
        self.assertEqual(self.ledger(), {"pending": None, "active": {
            "id": DOCUMENT, "digest": sha(NEW_NOTE), "source_digest": sha(OLD_NOTE),
        }})
        self.assertEqual(self.client.document["name"], rf.revision_name(TOPIC, sha(OLD_NOTE)))

        self.client.calls.clear()
        self.assertEqual(self.publish(), {"published": 0, "unchanged": 1, "pending": 0, "failed": 0})
        self.assertEqual(self.client.calls, [("GET", BASE, None)])

    def test_failed_scoped_search_restores_original_anchor_then_retries_same_id(self):
        before = deepcopy(self.client.rows)
        self.client.bad_retrieval = True
        self.assert_failure()
        self.assertEqual(self.client.rows, before)
        writes = self.client.writes()
        self.assertEqual([p for _, p, _ in writes], [ANCHOR_PATH, ANCHOR_PATH])
        self.assertEqual([data["content"] for _, _, data in writes], [index_text(NEW_NOTE), index_text(OLD_NOTE)])
        self.assertEqual(self.note.read_bytes(), NEW_NOTE)

        self.client.calls.clear()
        self.client.bad_retrieval = False
        self.assertEqual(self.publish()["published"], 1)
        self.assertEqual([p for _, p, _ in self.client.writes()], [ANCHOR_PATH, CHUNK_PATH])
        self.assertEqual(self.ledger()["active"]["id"], DOCUMENT)

    def test_unverified_companion_deactivation_leaves_ledger_pending_retry(self):
        self.client.ignore_deactivate_once = True
        self.assert_failure()
        self.assertTrue(self.client.rows[COMPANION]["available"])
        self.assertEqual(self.client.rows[ANCHOR]["content"], index_text(NEW_NOTE))

        self.client.calls.clear()
        self.assertEqual(self.publish()["published"], 1)
        self.assertEqual(self.client.writes(), [("PATCH", CHUNK_PATH, {"chunk_ids": [COMPANION], "available_int": 0})])
        self.assertEqual(self.ledger()["active"]["id"], DOCUMENT)

    def test_foreign_or_suspicious_remote_data_is_read_only_and_fails_closed(self):
        for field, bad_value in (("dataset_id", FOREIGN), ("document_id", FOREIGN),
                                 ("id", "not-a-chunk-id"), ("id", COMPANION),
                                 ("available", 1), ("content", None)):
            with self.subTest(chunk_field=field, value=bad_value), \
                 patch.dict(self.client.rows[ANCHOR], {field: bad_value}):
                self.client.calls.clear()
                self.assert_failure()
                self.assertEqual(self.client.writes(), [])
                self.assertNotIn(RETRIEVAL, [p for _, p, _ in self.client.calls])
        for field, bad_value in (("dataset_id", FOREIGN), ("name", "foreign-note.txt"), ("status", 0),
                                 ("parser_id", "other"), ("parser_config", {})):
            with self.subTest(doc_field=field, value=bad_value), \
                 patch.dict(self.client.document, {field: bad_value}):
                self.client.calls.clear()
                self.assert_failure()
                self.assertEqual(self.client.calls[0][1], BASE)
                self.assertEqual(len(self.client.calls), 2)
                self.assertEqual(self.client.writes(), [])

    def test_oversize_updates_and_chunk_list_fail_before_mutation(self):
        for notes in (b"x" * inplace.MAX_CHARS, "泵".encode() * (inplace.MAX_BYTES // 3 + 1)):
            with self.subTest(note_bytes=len(notes)):
                self.note.write_bytes(notes)
                self.client.calls.clear()
                self.assert_failure()
                self.assertEqual(self.note.read_bytes(), notes)
                self.assertEqual(self.client.calls, [("GET", BASE, None)])
        self.note.write_bytes(NEW_NOTE)
        self.client.calls.clear()
        self.client.total_override = inplace.MAX_CHUNKS + 1
        self.assert_failure()
        self.assertEqual(self.client.writes(), [])

    def test_oversize_first_publication_does_not_upload(self):
        self.state_path.unlink()
        self.client.dataset["document_count"] = 0
        self.client.document = None
        self.note.write_bytes(b"x" * inplace.MAX_CHARS)
        self.assertEqual(self.publish()["failed"], 1)
        self.assertIsNone(self.ledger())
        self.assertEqual(self.client.writes(), [])


if __name__ == "__main__":
    unittest.main()
