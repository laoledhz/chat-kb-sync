"""Only fake conversations in temporary directories; no network or model."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "chat-kb-sync" / "scripts"))
from chat_kb import scan


class ArchiveFixture(unittest.TestCase):
    def test_backfill_edit_new_topic_and_new_chat(self):
        with tempfile.TemporaryDirectory() as where:
            base = Path(where)
            source, work = base / "exports", base / "work"
            source.mkdir()
            cfg = {"min_idle_seconds": 0, "sources": [
                {"type": "export", "namespace": "synthetic", "root": str(source),
                 "scope": "approved-export-folder"}]}
            first = json.loads((REPO / "fixtures" / "synthetic-demo.json").read_text(encoding="utf-8"))
            active = source / "synthetic-demo.json"

            def write(path, payload):
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

            def run():
                keys = ("allowed", "not_yielded", "waiting", "unchanged", "changed", "docs_written",
                        "needs_review", "unclassified", "withheld_turns", "source_errors")
                stats = dict.fromkeys(keys, 0)
                scan(cfg, work, stats)
                self.assertEqual(stats["source_errors"], 0)
                return stats

            def notes():
                return {p.name: p.read_text(encoding="utf-8") for p in (work / "outbox").glob("*.md")}

            write(active, first)
            self.assertEqual(run()["docs_written"], 2)
            previous = notes()
            self.assertEqual(len(previous), 2)
            robot = next(k for k, v in previous.items() if "分类：机器人" in v)
            self.assertEqual(run()["docs_written"], 0)

            edited = deepcopy(first)
            edited["messages"][1]["text"] = "虚构更正：限位数量为四。"
            write(active, edited)  # Intentionally preserve the source's modification timestamp.
            self.assertEqual(run()["docs_written"], 1)
            self.assertEqual(notes().keys(), previous.keys())
            self.assertIn("限位数量为四", notes()[robot])

            edited["messages"] += [
                {"id": "u3", "role": "user", "text": "换个话题，数据库的索引是什么？"},
                {"id": "a3", "role": "assistant", "text": "这是虚构的索引笔记。"}]
            write(active, edited)
            self.assertEqual(run()["docs_written"], 1)
            self.assertEqual(len(notes()), 3)

            another = deepcopy(first)
            another["id"] = "synthetic-second"
            write(source / "synthetic-second.json", another)
            self.assertEqual(run()["docs_written"], 2)
            self.assertEqual(len(notes()), 5)
            active.unlink()
            run()
            ready = json.loads((work / "publish-ready.json").read_text(encoding="utf-8"))
            self.assertEqual(len(ready), 2)
            self.assertEqual(len(notes()), 5)  # No automatic deletion of earlier versions.


if __name__ == "__main__":
    unittest.main()
