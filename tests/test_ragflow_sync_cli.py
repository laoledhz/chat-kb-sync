"""Fake exports and mocked readiness/publishing only; no live network or launcher."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "chat-kb-sync" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import chat_kb
import ragflow_local as rf
import ragflow_ready as readiness


DATASET = "5" * 32
ORIGIN = "http://127.0.0.1:8085"
TOKEN = "synthetic-private-key-must-not-appear"
ARCHIVE_NAME = "synthetic-approved"
FOREIGN_NAME = "synthetic-not-approved"
STALE_KEY = "f" * 64
SUCCESS = {"published": 1, "unchanged": 0, "pending": 0, "failed": 0}


class SyncCliTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.work = self.root / "private-work"
        self.source = self.root / "fictional-exports"
        self.source.mkdir()
        allowed = self.root / "allowlist.json"
        self.write_json(allowed, [ARCHIVE_NAME])
        self.fake_export(ARCHIVE_NAME, "虚构问题：机器人怎么校准？", "虚构答复：用标尺校准。")
        self.fake_export(FOREIGN_NAME, "虚构的未选定水利问题？", "这段资料禁止进入发布范围。")
        self.config_path = self.root / "config.json"
        self.config = {
            "work_dir": str(self.work), "min_idle_seconds": 0,
            "sources": [{"type": "export", "root": str(self.source), "namespace": "synthetic",
                         "scope": "allowlist", "allowlist": str(allowed)}],
            "ragflow": {"origin": ORIGIN, "dataset_id": DATASET, "readiness": {"total_timeout": 3},
                        "update_mode": "inplace-index"},
        }
        probe_patch = patch.object(readiness, "ensure_ready", autospec=True)
        publish_patch = patch.object(rf, "publish", autospec=True, return_value=SUCCESS)
        network_patch = patch.object(rf, "HTTPConnection", side_effect=AssertionError("Unexpected real network access"))
        run_patch = patch.object(readiness.subprocess, "run", side_effect=AssertionError("Unexpected real launcher"))
        self.probe = self.start_patch(probe_patch)
        self.publisher = self.start_patch(publish_patch)
        self.start_patch(network_patch)
        self.start_patch(run_patch)

    def start_patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    @staticmethod
    def write_json(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def fake_export(self, identity, question, response):
        self.write_json(self.source / (identity + ".json"), {
            "id": identity, "modified": "2024-01-01T00:00:00Z",
            "messages": [{"id": "u1", "role": "user", "text": question},
                         {"id": "a1", "role": "assistant", "text": response}],
        })

    def invoke(self, *args, token=TOKEN):
        self.write_json(self.config_path, self.config)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict("os.environ", {"RAGFLOW_API_KEY": token}), redirect_stdout(stdout), redirect_stderr(stderr):
            status = chat_kb.main(["--config", str(self.config_path), *args])
        self.assertNotIn(TOKEN, stdout.getvalue() + stderr.getvalue())
        stats = json.loads(stdout.getvalue()) if stdout.getvalue() else None
        return status, stats, stderr.getvalue()

    def note_key(self):
        keys = json.loads((self.work / "publish-ready.json").read_bytes())
        self.assertEqual(len(keys), 1)
        return keys[0]

    def test_sync_stages_offline_then_ready_publishes_only_selected_draft(self):
        def assess_at_probe(origin, *, allow_start, **_):
            self.assertEqual(origin, ORIGIN)
            self.assertIs(allow_start, False)
            self.assertTrue((self.work / "outbox" / (self.note_key() + ".md")).is_file())
            return readiness.ReadinessResult("timeout", "readiness_deadline", probes=2, last_status="offline")

        self.probe.side_effect = assess_at_probe
        status, offline, stderr = self.invoke("sync")
        self.assertEqual((status, stderr), (1, ""))
        self.assertEqual((offline["allowed"], offline["docs_written"], offline["ragflow"]["failed"]), (1, 1, 1))
        self.assertFalse(offline["ragflow_readiness"]["ready"])
        self.assertEqual(offline["ragflow_readiness"]["status"], "timeout")
        self.publisher.assert_not_called()

        key = self.note_key()
        draft = self.work / "outbox" / (key + ".md")
        before = draft.read_bytes()
        self.assertIn("机器人怎么校准".encode(), before)
        self.assertNotIn("未选定水利问题".encode(), before)
        stale = self.work / "outbox" / (STALE_KEY + ".md")
        stale.write_text("# Fictional, stale and not in publish-ready.json", encoding="utf-8")

        self.probe.side_effect = None
        self.probe.return_value = readiness.ReadinessResult("ready", "healthy", probes=1)
        status, online, stderr = self.invoke("sync")
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual((online["unchanged"], online["docs_written"]), (1, 0))
        self.assertEqual(online["ragflow"], SUCCESS)
        self.assertTrue(online["ragflow_readiness"]["ready"])
        self.assertEqual(self.note_key(), key)
        self.assertEqual(draft.read_bytes(), before)
        self.assertTrue(stale.is_file())
        self.publisher.assert_called_once_with(self.work, DATASET, ORIGIN, TOKEN,
                                               allowed_topics={key}, update_mode="inplace-index")

    def test_scan_never_probes_publishes_or_needs_key_even_if_readiness_configured(self):
        status, stats, stderr = self.invoke("scan", token="")
        self.assertEqual((status, stderr), (0, ""))
        self.assertEqual(stats["docs_written"], 1)
        self.assertNotIn("ragflow_readiness", stats)
        self.assertNotIn("ragflow", stats)
        self.probe.assert_not_called()
        self.publisher.assert_not_called()

        status, stats, stderr = self.invoke("--allow-start", "scan", token="")
        self.assertEqual((status, stats), (1, None))
        self.assertTrue(stderr)
        self.probe.assert_not_called()
        self.publisher.assert_not_called()

    def test_allow_start_requires_configured_launcher_and_explicit_opt_in(self):
        status, _, _ = self.invoke("--allow-start", "sync")
        self.assertEqual(status, 1)
        self.probe.assert_not_called()
        self.publisher.assert_not_called()

        launcher = self.root / "fictional-launcher.exe"
        launcher.write_bytes(b"not an executable; launcher is mocked")
        self.config["ragflow"]["readiness"]["launcher_argv"] = [str(launcher)]
        self.probe.return_value = readiness.ReadinessResult("timeout", "readiness_deadline", probes=1)
        self.assertEqual(self.invoke("sync")[0], 1)
        self.assertIs(self.probe.call_args.kwargs["allow_start"], False)
        self.publisher.assert_not_called()

        self.probe.reset_mock()
        self.probe.return_value = readiness.ReadinessResult(
            "launcher_failed", "launcher_failed", probes=1, launcher_attempted=True, launcher_returncode=17,
        )
        status, stats, stderr = self.invoke("--allow-start", "sync")
        self.assertEqual((status, stderr), (1, ""))
        self.probe.assert_called_once()
        call = self.probe.call_args
        self.assertEqual(call.args, (ORIGIN,))
        self.assertIs(call.kwargs["allow_start"], True)
        self.assertEqual(call.kwargs["launcher_argv"], [str(launcher)])
        self.assertNotIn(TOKEN, repr(call))
        self.assertEqual(stats["ragflow_readiness"]["launcher_returncode"], 17)
        self.assertTrue(stats["ragflow_readiness"]["launcher_attempted"])
        self.assertNotIn(json.dumps(str(launcher))[1:-1], json.dumps(stats))
        self.publisher.assert_not_called()


if __name__ == "__main__":
    unittest.main()
