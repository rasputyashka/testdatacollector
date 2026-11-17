import concurrent.futures
from dataclasses import replace
import http.server
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch
import urllib.error
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workload_collector as agent


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.proc = self.root / "proc"
        self.settings = replace(agent.Settings(), state_directory=self.root / "state")
        self.write("stat", "cpu  100 0 100 800 0 0 0 0 20 0\ncpu0 0\ncpu1 0\ncpu2 0\ncpu3 0\n")
        self.write("meminfo", "MemTotal: 8388608 kB\nMemAvailable: 5242880 kB\nSwapTotal: 2097152 kB\nSwapFree: 2097152 kB\n")
        self.write("uptime", "86400.99 123456.00\n")
        self.write("sys/kernel/random/boot_id", "90354f14-d9cf-46ae-86f2-0cfaa27f8003\n")
        self.write("self/mountinfo", "1 0 8:1 / / rw,relatime - ext4 /dev/sda1 rw\n")
        self.write("net/dev", "Inter-| Receive | Transmit\n face |bytes packets errs drop fifo frame compressed multicast|bytes packets errs drop fifo colls carrier compressed\n lo: 999 0 0 0 0 0 0 0 999 0 0 0 0 0 0 0\n eth0: 1000 1 0 0 0 0 0 0 2000 1 0 0 0 0 0 0\n")

    def write(self, name, text):
        target = self.proc / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def collector(self):
        return agent.MetricsCollector(self.settings, "a6e1bd26-f21f-42db-baa5-451019a58c89", self.proc,
                                      {"type": "vm", "tech": "kvm"})

    @unittest.skipUnless(sys.platform == "linux", "Atomic hard-link creation is tested on the Linux target")
    def test_identity_persists_and_concurrent_starts_share_id(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            values = list(pool.map(lambda _: agent.load_host_id(self.settings.state_directory), range(16)))
        self.assertEqual(len(set(values)), 1)
        self.assertEqual(str(uuid.UUID(values[0])), values[0])
        self.assertEqual(agent.load_host_id(self.settings.state_directory), values[0])

    def test_existing_identity_persists(self):
        self.settings.state_directory.mkdir()
        expected = str(uuid.uuid4())
        (self.settings.state_directory / "host_id").write_text(expected + "\n", encoding="ascii")
        self.assertEqual(agent.load_host_id(self.settings.state_directory), expected)
        self.assertEqual(agent.load_host_id(self.settings.state_directory), expected)

    def test_invalid_saved_id_is_not_replaced(self):
        self.settings.state_directory.mkdir()
        path = self.settings.state_directory / "host_id"
        path.write_text("broken", encoding="ascii")
        with self.assertRaises(ValueError):
            agent.load_host_id(self.settings.state_directory)
        self.assertEqual(path.read_text(), "broken")

    def test_cpu_excludes_guest_double_count_and_iowait(self):
        first = agent.read_cpu(self.proc)
        self.assertEqual(sum(first.counters), 1000)
        self.assertEqual(first.logical_count, 4)
        self.assertIsNone(agent.cpu_usage(None, first))
        self.write("stat", "cpu  120 0 105 865 10 0 0 0 30 0\ncpu0 0\ncpu1 0\ncpu2 0\ncpu3 0\n")
        self.assertEqual(agent.cpu_usage(first, agent.read_cpu(self.proc)), 25.0)

    def test_cpu_reset_hotplug_and_zero_delta(self):
        first = agent.read_cpu(self.proc)
        self.assertIsNone(agent.cpu_usage(first, first))
        self.assertIsNone(agent.cpu_usage(first, agent.CpuSnapshot((0,) * 8, 4)))
        self.assertIsNone(agent.cpu_usage(first, agent.CpuSnapshot((1000,) * 8, 8)))

    def test_memory_conversion_and_swap(self):
        memory, swap = agent.read_memory(self.proc)
        self.assertEqual(memory, {"total_bytes": 8589934592, "available_bytes": 5368709120})
        self.assertEqual(swap, {"total_bytes": 2147483648, "used_bytes": 0})
        self.write("meminfo", "MemTotal: 1024 kB\nMemAvailable: 256 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
        self.assertEqual(agent.read_memory(self.proc)[1], {"total_bytes": 0, "used_bytes": 0})

    def test_filesystem_reservation_and_mount_escape(self):
        self.write("self/mountinfo", "1 0 8:1 / / rw - ext4 /dev/sda1 rw\n2 1 8:2 / /data\\040set rw - xfs /dev/sdb1 rw\n")
        stat = types.SimpleNamespace(f_blocks=100, f_bfree=60, f_bavail=55, f_frsize=1024)
        with patch.object(agent.os, "statvfs", return_value=stat, create=True):
            entries = agent.read_filesystems(self.proc, ("/", "/data set"))
        self.assertEqual(entries[0], {"mountpoint": "/", "fstype": "ext4", "total_bytes": 102400,
                                      "used_bytes": 40960, "available_bytes": 56320})
        self.assertEqual(entries[1]["fstype"], "xfs")

    def test_network_counters_and_loopback_filter(self):
        self.assertEqual(agent.read_network(self.proc), [{"interface": "eth0", "rx_bytes_total": 1000,
                                                         "tx_bytes_total": 2000}])

    def test_report_contract_and_partial_failure_recovery(self):
        collector = self.collector()
        with patch.object(agent, "read_filesystems", return_value=[]):
            first = collector.collect()
            self.assertEqual(set(first), {"schema_version", "agent_version", "host_id", "hostname", "boot_id",
                                         "collected_at", "uptime_seconds", "env", "cpu", "memory", "swap",
                                         "filesystems", "network"})
            self.assertIsNone(first["cpu"]["usage_percent"])
            self.assertEqual(first["uptime_seconds"], 86400)
            self.assertTrue(first["collected_at"].endswith("Z"))
            self.write("stat", "cpu  120 0 105 875 0 0 0 0\ncpu0 0\ncpu1 0\ncpu2 0\ncpu3 0\n")
            self.assertEqual(collector.collect()["cpu"]["usage_percent"], 25.0)
            (self.proc / "meminfo").unlink()
            (self.proc / "stat").unlink()
            with self.assertLogs(agent.LOG, level="WARNING"):
                broken = collector.collect()
            self.assertIsNone(broken["memory"])
            self.assertIsNone(broken["cpu"]["usage_percent"])
            self.assertEqual(broken["network"][0]["rx_bytes_total"], 1000)
            json.dumps(broken, allow_nan=False)
            self.write("stat", "cpu  120 0 105 875 0 0 0 0\ncpu0 0\ncpu1 0\ncpu2 0\ncpu3 0\n")
            with self.assertLogs(agent.LOG, level="WARNING"):
                self.assertIsNone(collector.collect()["cpu"]["usage_percent"])

    def test_environment_detection(self):
        result = lambda code, text: subprocess.CompletedProcess([], code, stdout=text, stderr="")
        cases = [
            ([result(1, "none\n")], {"type": "bare_metal", "tech": None}),
            ([result(0, "kvm\n"), result(1, "none\n")], {"type": "vm", "tech": "kvm"}),
            ([result(0, "docker\n"), result(0, "docker\n")], {"type": "container", "tech": "docker"}),
            ([result(2, "")], {"type": "unknown", "tech": None}),
        ]
        for outputs, expected in cases:
            with self.subTest(expected=expected), patch.object(agent.subprocess, "run", side_effect=outputs):
                self.assertEqual(agent.detect_environment(), expected)

    def server(self, status=204):
        received = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, self.headers["Content-Type"], json.loads(raw)))
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/elsewhere")
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()

        def close():
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)

        self.addCleanup(close)
        settings = replace(self.settings, collector_url="http://127.0.0.1:%s/api/v1/reports" % server.server_port)
        return settings, received

    def test_real_http_post_of_full_report(self):
        settings, received = self.server()
        with patch.object(agent, "read_filesystems", return_value=[]):
            report = self.collector().collect()
        # Explicitly bypass machine proxy settings for the local test server.
        with patch.dict("os.environ", {"no_proxy": "127.0.0.1"}):
            self.send_or_skip_restricted_network(report, settings)
        self.assertEqual(received, [("/api/v1/reports", "application/json", report)])

    def test_http_error_and_redirect_are_not_success(self):
        for status in (500, 302):
            settings, received = self.server(status)
            with patch.dict("os.environ", {"no_proxy": "127.0.0.1"}):
                with self.assertRaises(urllib.error.HTTPError):
                    self.send_or_skip_restricted_network({"schema_version": 1}, settings)
            self.assertEqual(len(received), 1)

    def send_or_skip_restricted_network(self, report, settings):
        try:
            agent.send_report(report, settings)
        except urllib.error.URLError as error:
            if getattr(error.reason, "winerror", None) == 10013:
                self.skipTest("This Windows sandbox blocks socket connections (10013)")
            raise

    def test_http_request_body_headers_timeout_and_redirect_handler(self):
        response = MagicMock()
        response.__enter__.return_value.status = 204
        opener = MagicMock()
        opener.open.return_value = response
        report = {"schema_version": 1, "cpu": {"usage_percent": None}}
        with patch.object(agent.urllib.request, "build_opener", return_value=opener) as build:
            agent.send_report(report, self.settings)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.full_url, self.settings.collector_url)
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(json.loads(request.data), report)
        self.assertEqual(opener.open.call_args.kwargs["timeout"], self.settings.http_timeout_seconds)
        handler = build.call_args.args[0]
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "http://elsewhere/"))

    def test_http_non_success_status_is_rejected(self):
        response = MagicMock()
        response.__enter__.return_value.status = 503
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(agent.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(RuntimeError):
                agent.send_report({}, self.settings)

    def test_delivery_failure_does_not_stop_sampling(self):
        stop = threading.Event()
        reports = []

        def send(report, settings):
            reports.append(report)
            if len(reports) == 1:
                raise urllib.error.URLError("offline")
            stop.set()

        settings = replace(self.settings, interval_seconds=0.01)
        with patch.object(agent, "send_report", side_effect=send), \
             patch.object(agent, "read_filesystems", return_value=[]), \
             self.assertLogs(agent.LOG, level="WARNING"):
            worker = threading.Thread(target=agent.run_daemon, args=(self.collector(), settings, stop), daemon=True)
            worker.start()
            worker.join(timeout=2)
            stop.set()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(reports), 2)

    def test_stopped_daemon_does_not_send(self):
        stop = threading.Event()
        stop.set()
        with patch.object(agent, "send_report") as send:
            agent.run_daemon(self.collector(), self.settings, stop)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
