"""Opt-in live contract check using one disposable local dataset and fictional notes only."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat-kb-sync" / "scripts"))
import ragflow_inplace as inplace
import ragflow_local as rf


def require(value, label):
    if not value:
        raise rf.ArchiveError("Fictional acceptance failed: " + label)


def fixture(origin, token):
    client = rf.Client(origin, token)
    name = "ChatKB-Synthetic-SingleDoc-" + uuid.uuid4().hex[:12]
    dataset = rf.create_archive_dataset(origin, token, name, "Chinese")
    base = f"/api/v1/datasets/{dataset}"
    mode = "inplace-index"
    try:
        with tempfile.TemporaryDirectory(prefix="chat-kb-synthetic-") as tmp:
            root = Path(tmp)
            outbox = root / "outbox"
            outbox.mkdir()
            robot = hashlib.sha256(b"fictional-robot").hexdigest()
            water = hashlib.sha256(b"fictional-water").hexdigest()
            path = outbox / (robot + ".md")
            old = ("# Fictional Robot\n\n" + "".join(
                "fictoldzebra " + " ".join(f"unreal_{i}_{word}" for word in range(65)) + "\n"
                for i in range(11))).encode()
            # Multiple old chunks verify that stale companion blocks get retired.
            path.write_bytes(old)

            def sync():
                deadline = time.monotonic() + 90
                while True:
                    report = rf.publish(root, dataset, origin, token, update_mode=mode)
                    require(report["failed"] == 0, "publication")
                    if not report["pending"]:
                        return report
                    require(time.monotonic() < deadline, "queued parse timeout")
                    time.sleep(2)

            def find(document, term):
                response = client.call("POST", "/api/v1/retrieval", {
                    "question": term, "dataset_ids": [dataset], "document_ids": [document],
                    "page": 1, "page_size": 10, "similarity_threshold": 0,
                    "keywords_similarity_weight": 1, "vector_similarity_weight": 0,
                    "rerank_id": "", "keyword": False, "highlight": False,
                    "cross_languages": [], "use_kg": False, "include_knowledge_compilation": False,
                })
                return [item["content"] for item in response["chunks"]
                        if item.get("document_id") == document and item.get("dataset_id") == dataset]

            sync()
            docs = rf.documents(client, base)
            require(len(docs) == 1, "first note count")
            document = rf.checked_id(docs[0]["id"])
            previous_file = client.call("GET", f"{base}/documents/{document}", download=True)
            url = f"{base}/documents/{document}/chunks"
            first = inplace.chunks(client, url, dataset, document)
            require(len(first) > 1, "fictional note generated companion chunks")
            require(any("fictoldzebra" in text for text in find(document,"fictoldzebra")), "baseline marker indexed")

            for turn, marker, cn in ((1,"ficnewwalrus","\u865a\u6784\u661f\u6cb3\u8235\u673a"),
                                      (2,"ficnewotter","\u865a\u6784\u84dd\u6e2f\u56de\u65cb\u8235")):
                updated = (f"# Fictional Robot\n\n{marker}\n\n{cn}\n\n" +
                           ("expand the fictional note. " * (25 if turn == 1 else 1))
                           + f"tailmarker{marker}\n").encode()
                path.write_bytes(updated)
                change = sync()
                require(change["published"] == 1, "one updated topic")
                rows = rf.documents(client, base)
                require(len(rows) == 1 and rows[0]["id"] == document, "same document ID and count")
                last = inplace.chunks(client, url, dataset, document)
                require(len(last) == len(first) and sum(item["available"] for item in last.values()) == 1,
                        "same chunk IDs with only one current searchable block")
                require(last.keys() == first.keys(), "no new chunks")
                require(any(marker in text and cn in text for text in find(document,cn)), "new Chinese text indexed")
                require(any("tailmarker"+marker in text for text in find(document,"tailmarker"+marker)),
                        "new ending indexed")
                require(not any("fictoldzebra" in text for text in find(document,"fictoldzebra")),
                        "old text no longer retrievable")
                require(sync()["unchanged"] == 1, "unchanged replay")
            require(client.call("GET", f"{base}/documents/{document}", download=True) == previous_file,
                    "download is still the initially uploaded file by design")
            (outbox / (water + ".md")).write_bytes(b"# Fictional water project\n\nfictwaterblue\n")
            sync()
            docs = rf.documents(client, base)
            require(len(docs) == 2 and document in {row["id"] for row in docs}, "a different topic adds one document")
            require(sync()["unchanged"] == 2, "both topics stable")
    finally:
        still_ours = rf.validate_dataset(client, dataset)
        require(still_ours["name"] == name, "test dataset identity before cleanup")
        client.call("DELETE", "/api/v1/datasets", {"ids": [dataset]})
    return {"passed": True, "same_topic_new_documents": 0, "different_topic_new_documents": 1,
            "chinese_and_latin_retrieval": True, "unchanged_replay": True,
            "only_test_dataset_cleaned": True, "download_is_initial_file": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--execute", action="store_true", help="Create and clean one disposable fictional dataset.")
    args = parser.parse_args(argv)
    if not args.execute:
        parser.error("add --execute to authorize creating and cleaning one fictional dataset")
    return fixture(args.origin, os.environ.get("RAGFLOW_API_KEY", ""))


if __name__ == "__main__":
    try:
        print(json.dumps(main(), sort_keys=True))
    except (rf.ArchiveError, OSError, KeyError, TypeError, ValueError) as error:
        print(str(error) if isinstance(error, rf.ArchiveError) else "Fictional integration check failed safely.",
              file=sys.stderr)
        sys.exit(1)

