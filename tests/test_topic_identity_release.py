"""Topic identity regression coverage using only temporary, fictional rollouts."""

import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / "chat-kb-sync" / "scripts"
sys.path.insert(0, str(SCRIPTS))
from chat_kb import digest, scan


THREAD_ID = "00000000-0000-4000-8000-000000000001"
THREAD_KEY = digest(["codex", "fictional-profile", THREAD_ID])
OLD_FILE_TIME_NS = 1_704_067_200_000_000_000
QUESTION = "虚构案例：机器人甲怎么校准？"
REVISED_QUESTION = "虚构案例：机器人乙怎么校准？"
ANSWER = "虚构结果：校验了机械臂。"
WATER_QUESTION = "换个话题，虚构水利闸门怎么验算？"
WATER_ANSWER = "虚构水利记录：根据样本水位验算。"


def record(kind, **payload):
    return {"type": kind, "payload": payload}


def message(message_id, role, text):
    return record(
        "response_item", type="message", message_id=message_id, role=role,
        content=[{"type": "input_text" if role == "user" else "output_text", "text": text}],
    )


def exchange(index, question, answer):
    return [message(f"q{index}", "user", question), message(f"a{index}", "assistant", answer)]


class TopicIdentityReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.source = base / "fake-codex-home"
        self.work = base / "fake-notes"
        filename = f"rollout-2024-01-01T00-00-00-{THREAD_ID}.jsonl"
        self.active = self.source / "sessions" / "2024" / "01" / "01" / filename
        self.archived = self.source / "archived_sessions" / filename
        for path in (self.active, self.archived):
            path.parent.mkdir(parents=True)
        self.config = {"min_idle_seconds": 0, "sources": [{
            "type": "codex", "namespace": "fictional-profile", "root": str(self.source),
            "scope": "verified-codex-owner", "owner_account": "fixture-account", "owner_user": "fixture-user",
        }]}

    def rollout(self, path, *events):
        meta = record("session_meta", id=THREAD_ID, source="vscode", thread_source="user",
                      creator_account_id="fixture-account", creator_user_id="fixture-user")
        data = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in (meta, *events))
        path.write_bytes(data.encode("utf-8"))
        os.utime(path, ns=(OLD_FILE_TIME_NS, OLD_FILE_TIME_NS))

    def run_scan(self):
        counters = ("allowed", "not_yielded", "waiting", "unchanged", "changed", "docs_written",
                    "needs_review", "unclassified", "withheld_turns", "source_errors")
        stats = dict.fromkeys(counters, 0)
        scan(self.config, self.work, stats)
        self.assertEqual(stats["source_errors"], 0)
        self.assertEqual(stats["allowed"], 1)
        self.assertEqual(stats["not_yielded"], 0)
        return stats

    def notes(self):
        return {p.stem: p.read_text(encoding="utf-8") for p in (self.work / "outbox").glob("*.md")}

    def previous(self):
        return json.loads((self.work / "scan-state.json").read_bytes())["threads"][THREAD_KEY]

    def ready(self):
        return json.loads((self.work / "publish-ready.json").read_bytes())

    def test_archiving_and_timestamp_only_edits_do_not_rewrite_same_note(self):
        self.rollout(self.active, *exchange(1, QUESTION, ANSWER))
        self.assertEqual(self.run_scan()["docs_written"], 1)
        before = self.notes()
        state = self.previous()
        key, = before
        note = self.work / "outbox" / (key + ".md")
        os.utime(note, ns=(OLD_FILE_TIME_NS, OLD_FILE_TIME_NS))
        old_note_mtime = note.stat().st_mtime_ns

        self.active.rename(self.archived)
        moved_time = OLD_FILE_TIME_NS + 10_000_000_000
        os.utime(self.archived, ns=(moved_time, moved_time))
        for _ in range(2):  # The daily run and a later repeat should both be no-ops for Markdown.
            stats = self.run_scan()
            self.assertEqual((stats["unchanged"], stats["docs_written"]), (1, 0))
            self.assertEqual(self.notes(), before)
            self.assertEqual(note.stat().st_mtime_ns, old_note_mtime)
            self.assertEqual(self.ready(), [key])
        self.assertNotEqual(self.previous()["modified"], state["modified"])
        self.assertEqual(self.previous()["hash"], state["hash"])

    def test_revised_question_reuses_note_but_genuinely_new_topic_splits(self):
        self.rollout(self.archived, *exchange(1, QUESTION, ANSWER))
        self.assertEqual(self.run_scan()["docs_written"], 1)
        old_stat = self.archived.stat()
        old_state = self.previous()
        robot, = self.notes()

        self.rollout(self.archived, *exchange(1, REVISED_QUESTION, ANSWER))
        new_stat = self.archived.stat()
        self.assertEqual((new_stat.st_size, new_stat.st_mtime_ns), (old_stat.st_size, old_stat.st_mtime_ns))
        self.assertEqual(self.run_scan()["docs_written"], 1)
        revised = self.notes()
        self.assertEqual(set(revised), {robot})
        self.assertTrue(revised[robot].startswith(f"# {REVISED_QUESTION}\n"))
        self.assertNotIn(QUESTION, revised[robot])
        self.assertNotEqual(self.previous()["hash"], old_state["hash"])
        self.assertEqual(self.previous()["topics"], old_state["topics"])

        self.rollout(self.archived, *exchange(1, REVISED_QUESTION, ANSWER),
                     *exchange(2, WATER_QUESTION, WATER_ANSWER))
        self.assertEqual(self.run_scan()["docs_written"], 1)
        now = self.notes()
        water, = now.keys() - {robot}
        self.assertEqual(now[robot], revised[robot])
        self.assertIn("分类：水利", now[water])
        self.assertNotIn(REVISED_QUESTION, now[water])
        self.assertCountEqual(self.ready(), [robot, water])
        self.assertEqual(self.run_scan()["docs_written"], 0)

    def test_possible_merge_of_existing_archived_topics_is_held_for_review(self):
        self.rollout(self.archived, *exchange(1, QUESTION, ANSWER), *exchange(2, WATER_QUESTION, WATER_ANSWER))
        self.assertEqual(self.run_scan()["docs_written"], 2)
        previous_notes, previous_state = self.notes(), self.previous()

        self.rollout(self.archived, *exchange(1, QUESTION, ANSWER),
                     *exchange(2, "虚构案例：机器人的底座怎么找平？", "虚构结果：核对了底座水平。"))
        stats = self.run_scan()
        self.assertEqual((stats["needs_review"], stats["docs_written"]), (1, 0))
        self.assertEqual(self.notes(), previous_notes)
        self.assertEqual(self.previous(), previous_state)
        self.assertEqual(self.ready(), [])


if __name__ == "__main__":
    unittest.main()
