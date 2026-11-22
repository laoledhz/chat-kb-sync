"""Actual local process and socket smoke, with a fictional readiness server. No Docker."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "chat-kb-sync" / "scripts"))
from ragflow_ready import DEPENDENCIES, HEALTH_PATH, ensure_ready, probe_health


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path != HEALTH_PATH or self.headers.get("Authorization"):
            self.send_error(400)
            return
        body = json.dumps({key: "ok" for key in (*DEPENDENCIES, "status")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    with tempfile.TemporaryDirectory(prefix="chat-kb-launch-smoke-") as tmp:
        root = Path(tmp).resolve()
        signal = root / "start-signal"
        launcher = root / "fictional-launch.py"
        launcher.write_text(
            "import os,sys\nfrom pathlib import Path\n"
            "assert 'RAGFLOW_API_KEY' not in os.environ\n"
            "with Path(sys.argv[1]).open('x', encoding='utf-8') as f:\n"
            "    f.write('one launch')\n"
            "print('fictional startup diagnostic', file=sys.stderr)\n",
            encoding="utf-8",
        )
        server = HTTPServer(("127.0.0.1", 0), FixtureHandler, bind_and_activate=False)
        server.timeout = .1
        server.server_bind()  # Reserve the port without accepting connections.
        origin = f"http://127.0.0.1:{server.server_port}"
        stop = threading.Event()
        errors = []

        def listen_after_signal():
            try:
                while not stop.is_set() and not signal.exists():
                    stop.wait(.02)
                if not stop.is_set():
                    server.server_activate()
                    while not stop.is_set():
                        server.handle_request()
            except OSError as exc:
                errors.append(exc)

        # Windows can take about two seconds to return a refusal on loopback.
        first = probe_health(origin, timeout=4)
        assert first.reason == "connection_refused", first
        worker = threading.Thread(target=listen_after_signal, daemon=True)
        worker.start()
        try:
            with (root / "private-start.log").open("w+b") as log, patch.dict(
                os.environ, {"RAGFLOW_API_KEY": "fictional-not-an-actual-key"}
            ):
                result = ensure_ready(origin, allow_start=True,
                                      launcher_argv=[str(Path(sys.executable).resolve()), str(launcher), str(signal)],
                                      launcher_cwd=str(root), launcher_stderr=log,
                                      launcher_timeout=6, total_timeout=15, probe_timeout=4, poll_interval=.1)
                assert result.ready and result.launcher_attempted and result.launcher_returncode == 0, result
                assert result.probes >= 2
                log.seek(0)
                assert b"fictional startup diagnostic" in log.read()
            replay = ensure_ready(origin, total_timeout=1, probe_timeout=.5)
            assert replay.ready and not replay.launcher_attempted
            assert signal.read_text(encoding="utf-8") == "one launch"
        finally:
            stop.set()
            worker.join(timeout=2)
            server.server_close()
        assert not worker.is_alive() and not errors
    print(json.dumps({"initial_refused": True, "launched_once": True, "ready_after_launch": True,
                      "credential_not_passed": True, "private_stderr_preserved": True}))


if __name__ == "__main__":
    main()

