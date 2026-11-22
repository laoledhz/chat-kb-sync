"""Synthetic transport/process fixtures only; never contact or start RAGFlow."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "chat-kb-sync" / "scripts"))
import ragflow_ready as ready

ORIGIN = "http://127.0.0.1:8081"
HEALTH = dict.fromkeys(("db", "redis", "doc_engine", "storage", "message_queue", "status"), "ok")


class FakeResponse:
    def __init__(self, status=200, payload=None, content_type="application/json", raw=None):
        self.status = status
        self.content_type = content_type
        self.body = raw if raw is not None else json.dumps(HEALTH if payload is None else payload).encode()
        self.read_sizes = []

    def getheader(self, name, default=None):
        return self.content_type if name.lower() == "content-type" else default

    def read(self, count):
        self.read_sizes.append(count)
        return self.body[:count]

    def close(self):
        pass


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, delay):
        self.value += delay


class HealthTests(unittest.TestCase):
    def probe(self, response=None, error=None, origin=ORIGIN):
        conn = Mock()
        conn.getresponse.side_effect = error
        conn.getresponse.return_value = response or FakeResponse()
        with patch.object(ready, "_DeadlineConnection", return_value=conn) as factory:
            result = ready.probe_health(origin, timeout=2)
        return result, conn, factory

    def test_valid_dependency_response_is_ready_without_token(self):
        result, conn, factory = self.probe()
        self.assertTrue(result.ready)
        args, kw = conn.request.call_args
        self.assertEqual(args, ("GET", "/api/v1/system/healthz"))
        self.assertNotIn("Authorization", kw["headers"])
        self.assertEqual(factory.call_args.args[:2], ("127.0.0.1", 8081))
        conn.close.assert_called_once()

    def test_proxy_environment_never_changes_transport_destination(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://secret@foreign.invalid", "RAGFLOW_API_KEY": "never-send"}):
            result, conn, factory = self.probe()
        self.assertTrue(result.ready)
        self.assertNotIn("never-send", repr(conn.request.call_args))
        self.assertEqual(factory.call_args.args[0], "127.0.0.1")

    def test_only_literal_loopback_origins(self):
        for origin in ("http://localhost:8081", "http://127.0.0.2", "https://127.0.0.1", "http://user:secret@127.0.0.1", "http://127.0.0.1/x", "http://127.0.0.1?x", "http://127.0.0.1:0", "http://127.0.0.1:65536"):
            result, _, factory = self.probe(origin=origin)
            self.assertEqual(result.status, "invalid_config")
            factory.assert_not_called()
            self.assertNotIn("secret", repr(result))
        result, _, factory = self.probe(origin="http://[::1]:8081/")
        self.assertTrue(result.ready)
        self.assertEqual(factory.call_args.args[0], "::1")

    def test_auth_redirect_and_missing_route_are_terminal(self):
        for code, status in ((401, "auth_required"), (403, "auth_required"), (302, "foreign_server"), (307, "foreign_server"), (404, "foreign_server")):
            response = FakeResponse(status=code, raw=b"private error body")
            result, conn, _ = self.probe(response)
            self.assertEqual(result.status, status)
            self.assertFalse(result.retryable)
            self.assertEqual(response.read_sizes, [])
            self.assertEqual(conn.request.call_count, 1)
            self.assertNotIn("private", repr(result))

    def test_generic_or_malformed_success_is_not_ragflow_ready(self):
        for value in ({"code": 0}, {"status": "ok"}, [], {**HEALTH, "db": True}, {**HEALTH, "status": "nok"}):
            result, _, _ = self.probe(FakeResponse(payload=value))
            self.assertEqual(result.status, "foreign_server")
        result, _, _ = self.probe(FakeResponse(raw=b"<html>secret</html>", content_type="text/html"))
        self.assertEqual(result.status, "foreign_server")

    def test_known_unhealthy_dependencies_are_retryable_not_startable(self):
        result, _, _ = self.probe(FakeResponse(status=500, payload={**HEALTH, "db": "nok", "status": "nok", "_meta": {"db": {"error": "password=secret"}}}))
        self.assertEqual(result.status, "unhealthy")
        self.assertTrue(result.retryable)
        self.assertNotIn("secret", repr(result))

    def test_large_body_is_refused(self):
        response = FakeResponse(raw=b"x" * (ready.MAX_HEALTH_BYTES + 1))
        result, _, _ = self.probe(response)
        self.assertEqual(result.status, "foreign_server")
        self.assertEqual(response.read_sizes, [ready.MAX_HEALTH_BYTES + 1])

    def test_warming_gateway_is_retryable_but_not_connection_refusal(self):
        for status in (502, 503, 504):
            result, _, _ = self.probe(FakeResponse(status=status, content_type="text/html", raw=b"private gateway details"))
            self.assertEqual((result.status, result.reason), ("offline", "gateway_unavailable"))
            self.assertTrue(result.retryable)
            self.assertNotIn("private", repr(result))

    def test_refused_and_ambiguous_timeout_are_distinguished(self):
        result, _, _ = self.probe(error=ConnectionRefusedError(errno.ECONNREFUSED, "private details"))
        self.assertEqual((result.status, result.reason), ("offline", "connection_refused"))
        result, _, _ = self.probe(error=TimeoutError("private details"))
        self.assertEqual((result.status, result.reason), ("offline", "probe_timeout"))
        self.assertNotIn("private", repr(result))

    def test_invalid_timeouts_do_not_connect(self):
        for value in (0, -1, float("nan"), float("inf"), True, "5"):
            with patch.object(ready, "_DeadlineConnection") as factory:
                result = ready.probe_health(ORIGIN, timeout=value)
            self.assertEqual(result.status, "invalid_config")
            factory.assert_not_called()


class EnsureTests(unittest.TestCase):
    def run_fixture(self, results, **kwargs):
        clock = FakeClock()
        with patch.object(ready, "probe_health", side_effect=results) as probe, \
             patch.object(ready.time, "monotonic", clock.monotonic), \
             patch.object(ready.time, "sleep", clock.sleep), \
             patch.object(ready, "_run_launcher", return_value=("ok", 0)) as launch:
            result = ready.ensure_ready(ORIGIN, total_timeout=2, probe_timeout=1, poll_interval=1, **kwargs)
        return result, probe, launch

    def test_ready_never_launches(self):
        result, _, launch = self.run_fixture([ready.ReadinessResult("ready", "healthy")], allow_start=True, launcher_argv=["unused"])
        self.assertTrue(result.ready)
        launch.assert_not_called()

    def test_offline_wait_preserves_outbox_no_launcher_by_default(self):
        offline = ready.ReadinessResult("offline", "connection_refused")
        result, probe, launch = self.run_fixture([offline] * 3)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.last_status, "offline")
        launch.assert_not_called()
        self.assertLessEqual(probe.call_count, 3)

    def test_only_explicit_opt_in_refusal_can_launch_once(self):
        states = [ready.ReadinessResult("offline", "connection_refused"), ready.ReadinessResult("offline", "connection_refused"), ready.ReadinessResult("ready", "healthy")]
        result, _, launch = self.run_fixture(states, allow_start=True, launcher_argv=["trusted"])
        self.assertTrue(result.ready)
        self.assertTrue(result.launcher_attempted)
        launch.assert_called_once()

    def test_unhealthy_or_timeout_never_launch(self):
        for status, reason in (("unhealthy", "dependencies_unhealthy"), ("offline", "probe_timeout")):
            result, _, launch = self.run_fixture([ready.ReadinessResult(status, reason)] * 3, allow_start=True, launcher_argv=["trusted"])
            self.assertEqual(result.status, "timeout")
            launch.assert_not_called()

    def test_auth_or_foreign_server_never_retries_or_launches(self):
        for status in ("auth_required", "foreign_server"):
            result, probe, launch = self.run_fixture([ready.ReadinessResult(status, "blocked")], allow_start=True, launcher_argv=["trusted"])
            self.assertEqual(result.status, status)
            self.assertEqual(probe.call_count, 1)
            launch.assert_not_called()

    def test_launcher_failure_and_timeout_propagate_without_retry(self):
        for status, code in (("launcher_failed", 17), ("launcher_timeout", None)):
            with patch.object(ready, "probe_health", return_value=ready.ReadinessResult("offline", "connection_refused")), \
                 patch.object(ready, "_run_launcher", return_value=(status, code)) as launch:
                result = ready.ensure_ready(ORIGIN, allow_start=True, launcher_argv=["trusted"])
            self.assertEqual(result.status, status)
            self.assertEqual(result.launcher_returncode, code)
            self.assertFalse(result.retryable)
            launch.assert_called_once()

    def test_no_truthy_implicit_authorization(self):
        with patch.object(ready, "probe_health") as probe:
            result = ready.ensure_ready(ORIGIN, allow_start="yes")
        self.assertEqual(result.status, "invalid_config")
        probe.assert_not_called()


class LauncherTests(unittest.TestCase):
    def setUp(self):
        output = patch.object(ready, "_checked_diagnostics", return_value=99)
        output.start()
        self.addCleanup(output.stop)

    def test_no_shell_no_secrets_hidden_and_bounded(self):
        with patch.object(ready, "_checked_launcher", return_value=(["C:/trusted/start.exe"], "C:/trusted")), \
             patch.object(ready.subprocess, "run", return_value=Mock(returncode=7)) as run, \
             patch.object(ready.time, "monotonic", return_value=0), \
             patch.object(ready, "_is_windows", return_value=True), \
             patch.dict(os.environ, {"RAGFLOW_API_KEY": "private-token", "DOCKER_HOST": "npipe://docker_engine", "SystemRoot": "C:/Windows"}):
            status, code = ready._run_launcher(["C:/trusted/start.exe"], None, 2.5)
        self.assertEqual((status, code), ("launcher_failed", 7))
        kw = run.call_args.kwargs
        self.assertFalse(kw["shell"])
        self.assertNotIn("RAGFLOW_API_KEY", kw["env"])
        self.assertEqual(kw["env"]["DOCKER_HOST"], "npipe://docker_engine")
        folded_env = {key.upper(): value for key, value in kw["env"].items()}
        self.assertEqual(folded_env["SYSTEMROOT"], "C:/Windows")
        self.assertEqual(kw["timeout"], 2.5)
        self.assertEqual(kw["creationflags"], 0x08000000)
        self.assertEqual((kw["stdout"], kw["stderr"]), (99, 99))

    def test_timeout_and_errors_do_not_expose_argv(self):
        for error, expected in ((subprocess.TimeoutExpired("private-argv", 1), "launcher_timeout"), (OSError("private-path"), "launcher_failed")):
            with patch.object(ready, "_checked_launcher", return_value=(["trusted"], "/trusted")), patch.object(ready.subprocess, "run", side_effect=error):
                result = ready._run_launcher(["private-argv"], None, 1)
            self.assertEqual(result[0], expected)
            self.assertNotIn("private", repr(result))

    def test_relative_string_command_batch_inline_and_missing_paths_refused(self):
        for argv in ("/trusted/start", ["relative.exe"], ["C:/missing.exe"], ["/missing"], [], ["/trusted", "\n"], ["/trusted", "-Command", "unsafe"]):
            with patch.object(ready.subprocess, "run") as run:
                status, _ = ready._run_launcher(argv, None, 1)
            self.assertEqual(status, "invalid_config")
            run.assert_not_called()

    def test_real_temporary_paths_windows_powershell_and_batch_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            exe = folder / "powershell.exe"
            script = folder / "start service.ps1"
            batch = folder / "start.cmd"
            for path in (exe, script, batch):
                path.write_text("synthetic only")
            with patch.object(ready, "_is_windows", return_value=True):
                argv, cwd = ready._checked_launcher([str(exe), "-NoProfile", "-NonInteractive", "-File", str(script)], None)
                self.assertEqual(argv[-1], str(script))
                self.assertEqual(cwd, str(folder))
                for bad in ([str(batch)], [str(exe), "-File", str(script)], [str(exe), "-NoProfile", "-Command", "unsafe"]):
                    with self.assertRaises(ValueError):
                        ready._checked_launcher(bad, None)

    def test_linux_absolute_executable_and_cwd(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            script = folder / "start service.sh"
            script.write_text("synthetic only")
            with patch.object(ready, "_is_windows", return_value=False), patch.object(ready.os, "access", return_value=True):
                argv, cwd = ready._checked_launcher([str(script)], str(folder))
                self.assertEqual((argv, cwd), ([str(script)], str(folder)))
                with self.assertRaises(ValueError):
                    ready._checked_launcher([str(script)], "relative")

    def test_known_interpreters_require_absolute_existing_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            shell, python = folder / "bash", folder / "python3"
            script, py_script = folder / "start.sh", folder / "start.py"
            for path in (shell, python, script, py_script):
                path.write_text("synthetic only")
            with patch.object(ready, "_is_windows", return_value=False), patch.object(ready.os, "access", return_value=True):
                for bad in ([str(shell), "-lc", "arbitrary"], [str(shell), "-ec", "arbitrary"], [str(python), "-m", "module"], [str(shell)], [str(shell), "relative.sh"]):
                    with self.assertRaises(ValueError):
                        ready._checked_launcher(bad, None)
                for good in ([str(shell), str(script)], [str(python), str(py_script)]):
                    self.assertEqual(ready._checked_launcher(good, None)[0], good)

    def test_slow_precheck_cannot_spend_original_timeout_again(self):
        clock = FakeClock()
        def precheck(*args):
            clock.sleep(2)
            return ["trusted"], "/trusted"
        with patch.object(ready.time, "monotonic", clock.monotonic), patch.object(ready, "_checked_launcher", side_effect=precheck), patch.object(ready.subprocess, "run") as run:
            result = ready._run_launcher(["trusted"], None, 1)
        self.assertEqual(result, ("launcher_timeout", None))
        run.assert_not_called()


class DeadlineTests(unittest.TestCase):
    def test_socket_deadline_checked_before_every_read_or_write(self):
        # Unconnected socket only: expired deadline must prevent any IO syscall.
        with ready._DeadlineSocket(ready.socket.AF_INET, 1.0) as sock, patch.object(ready.time, "monotonic", return_value=2.0):
            with self.assertRaises(TimeoutError):
                sock.recv_into(bytearray(1))
            with self.assertRaises(TimeoutError):
                sock.sendall(b"no-network")

    def test_connection_uses_literal_address_without_resolution(self):
        fake = Mock()
        with patch.object(ready, "_DeadlineSocket", return_value=fake), patch.object(ready.socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")):
            conn = ready._DeadlineConnection("::1", 8081, ready.time.monotonic() + 1)
            conn.connect()
            fake.connect.assert_called_once_with(("::1", 8081, 0, 0))
            conn.close()

    def test_total_deadline_includes_launcher_and_each_probe(self):
        clock = FakeClock()
        def probe(origin, timeout):
            self.assertLessEqual(timeout, 3 - clock.value)
            clock.sleep(0.5)
            return ready.ReadinessResult("offline", "connection_refused")
        def launch(argv, cwd, timeout, **kwargs):
            self.assertEqual(timeout, 2.5)
            clock.sleep(2.5)
            return "ok", 0
        with patch.object(ready.time, "monotonic", clock.monotonic), patch.object(ready.time, "sleep", clock.sleep), \
             patch.object(ready, "probe_health", side_effect=probe), patch.object(ready, "_run_launcher", side_effect=launch) as launcher:
            result = ready.ensure_ready(ORIGIN, total_timeout=3, allow_start=True, launcher_argv=["trusted"])
        self.assertEqual((result.status, clock.value), ("timeout", 3))
        launcher.assert_called_once()

    def test_late_healthy_response_is_not_accepted_after_deadline(self):
        clock = FakeClock()
        def late(*args, **kwargs):
            clock.sleep(2)
            return ready.ReadinessResult("ready", "healthy")
        with patch.object(ready.time, "monotonic", clock.monotonic), patch.object(ready, "probe_health", side_effect=late):
            result = ready.ensure_ready(ORIGIN, total_timeout=1)
        self.assertEqual(result.status, "timeout")


class DiagnosticsTests(unittest.TestCase):
    def test_private_regular_file_required_for_child_output(self):
        for destination in (None, 0, 1, 2, -1, "path-not-a-handle"):
            with self.assertRaises((ValueError, OSError)):
                ready._checked_diagnostics(destination)
        with tempfile.TemporaryFile(mode="w+b") as sink:
            self.assertEqual(ready._checked_diagnostics(sink), sink.fileno())

    def test_missing_diagnostics_prevents_child(self):
        with patch.object(ready, "_checked_launcher", return_value=(["trusted"], "/trusted")), patch.object(ready.subprocess, "run") as run:
            result = ready._run_launcher(["trusted"], None, 1)
        self.assertEqual(result, ("invalid_config", None))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
