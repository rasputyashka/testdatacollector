import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import workload_collector as agent


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config.toml"
        self.state = self.root / "configured-state"
        self.override = self.root / "override-state"

    def write_config(self, text):
        self.config.write_text(text, encoding="utf-8")
        return self.config

    def valid_config(self):
        return (
            '[collector]\n'
            'url = "https://collector.example:8443/api/v1/reports?source=test"\n'
            'interval_seconds = 12.5\n'
            'http_timeout_seconds = 3\n'
            '[agent]\n'
            f'state_directory = {json.dumps(str(self.state))}\n'
            'mountpoints = ["/", "/data set"]\n'
        )

    def assert_invalid(self, text):
        with self.assertRaises(ValueError):
            agent.load_settings(self.write_config(text))
        self.assertFalse(self.state.exists())

    def cli(self, *arguments):
        return subprocess.run(
            [sys.executable, "-B", str(Path(agent.__file__).resolve()), *map(str, arguments)],
            cwd=self.root, capture_output=True, text=True, timeout=15, check=False,
        )

    def test_default_config_path(self):
        self.assertEqual(Path(agent.CONFIG_PATH), Path("/etc/data-collector/config.toml"))

    def test_omitted_and_none_path_use_config_path(self):
        self.write_config(self.valid_config())
        with patch.object(agent, "CONFIG_PATH", self.config):
            for load in (lambda: agent.load_settings(), lambda: agent.load_settings(None)):
                with self.subTest(load=load):
                    settings = load()
                    self.assertEqual(settings.state_directory, self.state)
                    self.assertEqual(settings.interval_seconds, 12.5)
        self.assertFalse(self.state.exists())

    def test_explicit_path_accepts_path_and_string(self):
        self.write_config(self.valid_config())
        for path in (self.config, str(self.config)):
            with self.subTest(path=path):
                settings = agent.load_settings(path)
                self.assertIsInstance(settings, agent.Settings)
                self.assertEqual(settings.collector_url,
                                 "https://collector.example:8443/api/v1/reports?source=test")
                self.assertEqual(settings.interval_seconds, 12.5)
                self.assertEqual(settings.http_timeout_seconds, 3)
                self.assertEqual(settings.state_directory, self.state)
                self.assertIsInstance(settings.state_directory, Path)
                self.assertEqual(settings.mountpoints, ("/", "/data set"))
        self.assertFalse(self.state.exists())

    def test_empty_file_and_empty_tables_use_defaults(self):
        for text in ("", "[collector]\n[agent]\n"):
            with self.subTest(text=text):
                self.assertEqual(agent.load_settings(self.write_config(text)), agent.Settings())

    def test_partial_tables_preserve_unspecified_defaults(self):
        settings = agent.load_settings(self.write_config('[collector]\ninterval_seconds = 2\n'))
        defaults = agent.Settings()
        self.assertEqual(settings.interval_seconds, 2)
        self.assertEqual(settings.collector_url, defaults.collector_url)
        self.assertEqual(settings.http_timeout_seconds, defaults.http_timeout_seconds)
        self.assertEqual(settings.state_directory, defaults.state_directory)
        self.assertEqual(settings.mountpoints, defaults.mountpoints)

    def test_shipped_config_loads_and_matches_documented_values(self):
        shipped = Path(__file__).resolve().parents[1] / "config.toml"
        with shipped.open("rb") as source:
            config = tomllib.load(source)
        settings = agent.load_settings(shipped)
        self.assertIsInstance(settings, agent.Settings)
        self.assertEqual(settings.collector_url, config["collector"]["url"])
        self.assertEqual(settings.interval_seconds, config["collector"]["interval_seconds"])
        self.assertEqual(settings.http_timeout_seconds,
                         config["collector"]["http_timeout_seconds"])
        self.assertEqual(settings.state_directory, Path(config["agent"]["state_directory"]))
        self.assertEqual(settings.mountpoints, tuple(config["agent"]["mountpoints"]))

    def test_missing_explicit_and_default_files_do_not_fall_back(self):
        with self.assertRaises(OSError):
            agent.load_settings(self.config)
        with patch.object(agent, "CONFIG_PATH", self.config):
            with self.assertRaises(OSError):
                agent.load_settings()
        self.assertFalse(self.config.exists())

    def test_directory_and_unreadable_file_fail(self):
        with self.assertRaises(OSError):
            agent.load_settings(self.root)
        self.write_config(self.valid_config())
        with patch.object(Path, "open", side_effect=PermissionError("permission denied")):
            with self.assertRaises(OSError):
                agent.load_settings(self.config)
        self.assertFalse(self.state.exists())

    def test_malformed_toml_and_invalid_encoding_fail(self):
        for text in ('[collector\n', 'interval_seconds =\n', 'key = 1\nkey = 2\n'):
            with self.subTest(text=text):
                self.assert_invalid(text)
        self.config.write_bytes(b"\xff\xfe")
        with self.assertRaises(ValueError):
            agent.load_settings(self.config)

    def test_collector_and_agent_must_be_tables(self):
        for table in ("collector", "agent"):
            for value in ('"table"', "1", "1.5", "true", "[]", '[{ key = "value" }]'):
                with self.subTest(table=table, value=value):
                    self.assert_invalid(f"{table} = {value}\n")

    def test_valid_http_and_https_urls(self):
        for url in (
            "http://localhost/reports", "https://collector.example/api/v1/reports",
            "http://127.0.0.1:1/reports", "https://collector.example:65535/reports",
            "http://[::1]:8080/reports", "https://collector.example/reports?source=host",
        ):
            with self.subTest(url=url):
                settings = agent.load_settings(self.write_config(
                    f"[collector]\nurl = {json.dumps(url)}\n"))
                self.assertEqual(settings.collector_url, url)

    def test_url_requires_a_string(self):
        for value in ("1", "1.5", "true", "[]", "{}"):
            with self.subTest(value=value):
                self.assert_invalid(f"[collector]\nurl = {value}\n")

    def test_url_rejects_bad_scheme_host_credentials_whitespace_fragment_and_port(self):
        for url in (
            "", "collector.example/reports", "//collector.example/reports",
            "ftp://collector.example/reports", "file:///tmp/report", "http:///reports",
            "http://", "http://:8080/reports", "http://[broken/reports",
            "http://user@collector.example/reports", "https://user:secret@collector.example/",
            "http://@collector.example/", "https://:secret@collector.example/",
            " https://collector.example/", "https://collector.example/ ",
            "https://collector.example/a b", "https://collector.example/\tpath",
            "https://collector.example/\npath", "https://collector.example/\u00a0path",
            "https://collector.example/reports#fragment", "http://collector.example:0/",
            "http://collector.example:-1/", "http://collector.example:65536/",
            "http://collector.example:not-a-port/",
        ):
            with self.subTest(url=url):
                self.assert_invalid(f"[collector]\nurl = {json.dumps(url)}\n")

    def test_timing_accepts_positive_integers_and_floats(self):
        for key in ("interval_seconds", "http_timeout_seconds"):
            for value in (1, 60, 0.125, 2.5):
                with self.subTest(key=key, value=value):
                    settings = agent.load_settings(self.write_config(
                        f"[collector]\n{key} = {value}\n"))
                    self.assertEqual(getattr(settings, key), value)

    def test_timing_rejects_nonpositive_nonfinite_bool_and_wrong_types(self):
        for key in ("interval_seconds", "http_timeout_seconds"):
            for value in (
                "0", "-1", "0.0", "-0.5", "inf", "+inf", "-inf", "nan", "+nan", "-nan",
                "true", "false", '"10"', "[]", "{}", "1979-05-27",
            ):
                with self.subTest(key=key, value=value):
                    self.assert_invalid(f"[collector]\n{key} = {value}\n")

    def test_state_directory_requires_absolute_string(self):
        for value in ('""', '"relative/state"', '"~/state"', '"C:/state"',
                      "1", "true", "[]", "{}", '"/bad\\u0000path"'):
            with self.subTest(value=value):
                self.assert_invalid(f"[agent]\nstate_directory = {value}\n")

    def test_mountpoints_require_nonempty_list_of_absolute_strings(self):
        for value in (
            '"/"', "1", "true", "{}", "[]", '[""]', '["relative"]', '["~/data"]',
            '["/", "relative"]', '["/", 1]', '["/", true]', '[{}]', '[["/"]]',
            '["/bad\\u0000path"]',
        ):
            with self.subTest(value=value):
                self.assert_invalid(f"[agent]\nmountpoints = {value}\n")

    def test_check_config_short_circuits_identity_collection_and_delivery(self):
        self.write_config(self.valid_config())
        arguments = ["workload_collector.py", "--config", str(self.config), "--check-config",
                     "--state-dir", str(self.override)]
        with patch.object(sys, "argv", arguments), \
             patch.object(agent.platform, "system", return_value="Linux"), \
             patch.object(agent, "load_host_id") as identity, \
             patch.object(agent, "MetricsCollector") as collector, \
             patch.object(agent, "send_report") as send, \
             patch.object(agent, "run_daemon") as daemon, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(agent.main(), 0)
        self.assertTrue(output.getvalue().strip())
        identity.assert_not_called()
        collector.assert_not_called()
        send.assert_not_called()
        daemon.assert_not_called()
        self.assertFalse(self.state.exists())
        self.assertFalse(self.override.exists())

    def test_print_once_selects_state_directory_and_never_sends(self):
        self.write_config(self.valid_config())
        host_id = "90354f14-d9cf-46ae-86f2-0cfaa27f8003"
        report = {"schema_version": 1, "host_id": host_id}
        for override in (None, self.override):
            with self.subTest(override=override):
                arguments = ["workload_collector.py", "--config", str(self.config), "--print-once"]
                if override is not None:
                    arguments.extend(["--state-dir", str(override)])
                with patch.object(sys, "argv", arguments), \
                     patch.object(agent.platform, "system", return_value="Linux"), \
                     patch.object(agent, "load_host_id", return_value=host_id) as identity, \
                     patch.object(agent, "MetricsCollector") as collector, \
                     patch.object(agent, "send_report") as send, \
                     patch.object(agent, "run_daemon") as daemon, \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    collector.return_value.collect.return_value = report
                    self.assertEqual(agent.main(), 0)
                expected_state = self.state if override is None else override
                identity.assert_called_once_with(expected_state)
                settings, actual_host_id = collector.call_args.args
                self.assertEqual(settings.state_directory, expected_state)
                self.assertEqual(settings.interval_seconds, 12.5)
                self.assertEqual(actual_host_id, host_id)
                collector.return_value.collect.assert_called_once_with()
                self.assertEqual(json.loads(output.getvalue()), report)
                send.assert_not_called()
                daemon.assert_not_called()

    @unittest.skipUnless(sys.platform == "linux", "The collector CLI requires Linux")
    def test_cli_check_config_succeeds_without_creating_state(self):
        self.write_config(self.valid_config())
        result = self.cli("--config", self.config, "--check-config", "--state-dir", self.override)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip())
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.state.exists())
        self.assertFalse(self.override.exists())

    @unittest.skipUnless(sys.platform == "linux", "The collector CLI requires Linux")
    def test_cli_bad_configs_fail_cleanly_without_state(self):
        cases = {
            "missing": None,
            "malformed": "[collector\n",
            "bad_table": 'collector = "not a table"\n',
            "bad_url": '[collector]\nurl = "http://collector.example:0/"\n',
            "bad_timing": "[collector]\ninterval_seconds = true\n",
            "bad_state": '[agent]\nstate_directory = "relative"\n',
            "bad_mounts": "[agent]\nmountpoints = []\n",
        }
        for name, text in cases.items():
            for mode in ("--check-config", "--print-once"):
                with self.subTest(case=name, mode=mode):
                    self.config.unlink(missing_ok=True)
                    if text is not None:
                        self.write_config(text)
                    result = self.cli("--config", self.config, mode,
                                      "--state-dir", self.override)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertTrue(result.stderr.strip())
                    self.assertEqual(result.stdout, "")
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertFalse(self.state.exists())
                    self.assertFalse(self.override.exists())

    @unittest.skipUnless(sys.platform == "linux", "The collector CLI requires Linux")
    def test_cli_print_once_uses_state_override_and_emits_json_without_http(self):
        self.write_config(self.valid_config())
        result = self.cli("--config", self.config, "--print-once", "--state-dir", self.override)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(str(uuid.UUID(report["host_id"])), report["host_id"])
        self.assertEqual((self.override / "host_id").read_text(encoding="ascii").strip(),
                         report["host_id"])
        self.assertFalse(self.state.exists())
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
