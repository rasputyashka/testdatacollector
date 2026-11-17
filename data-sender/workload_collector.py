#!/usr/bin/env python3
"""Linux system metrics agent. Python 3.9+, standard library only."""

import argparse
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import http.client
import json
import logging
import math
import os
from pathlib import Path
import platform
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import tomllib

# Temporary hardcoded configuration. Replace load_settings() with the TOML parser.
COLLECTOR_URL = "http://127.0.0.1:8080/api/v1/reports"
INTERVAL_SECONDS = 60
HTTP_TIMEOUT_SECONDS = 10
STATE_DIRECTORY = Path("/var/lib/workload-collector")
MOUNTPOINTS = ("/",)
AGENT_VERSION = "0.1.0"
LOG = logging.getLogger("workload-collector")
CONFIG_PATH = Path(__file__).resolve().parent / "config.toml"


@dataclass(frozen=True)
class Settings:
    collector_url: str = COLLECTOR_URL
    interval_seconds: float = INTERVAL_SECONDS
    http_timeout_seconds: float = HTTP_TIMEOUT_SECONDS
    state_directory: Path = STATE_DIRECTORY
    mountpoints: tuple = MOUNTPOINTS


def load_settings():
    """Load and validate settings from TOML configuration."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Configuration file not found: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)
    collector = config.get("collector", {})
    agent = config.get("agent", {})
    settings = Settings(
        collector_url=collector.get("url", COLLECTOR_URL),
        interval_seconds=collector.get("interval_seconds", INTERVAL_SECONDS),
        http_timeout_seconds=collector.get(
            "http_timeout_seconds", HTTP_TIMEOUT_SECONDS
        ),
        state_directory=Path(agent.get("state_directory", STATE_DIRECTORY)),
        mountpoints=tuple(
            agent.get("mountpoints", MOUNTPOINTS)
        ),
    )
    parsed = urllib.parse.urlsplit(settings.collector_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Invalid collector URL")
    for value in (
        settings.interval_seconds,
        settings.http_timeout_seconds,
    ):
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError(
                "Interval and HTTP timeout must be positive numbers"
            )
    if not settings.mountpoints:
        raise ValueError("At least one mountpoint must be configured")
    if not all(
        isinstance(path, str) and path.startswith("/")
        for path in settings.mountpoints
    ):
        raise ValueError("Mountpoints must be absolute paths")
    return settings


def load_host_id(state_directory):
    """Create an installation UUID atomically; concurrent starts reuse the winner."""
    state_directory = Path(state_directory)
    state_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = state_directory / "host_id"
    if not target.exists():
        fd, temporary = tempfile.mkstemp(prefix="host_id-", dir=state_directory)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as output:
                output.write(str(uuid.uuid4()) + "\n")
                output.flush()
                os.fsync(output.fileno())
            try:
                # Unlike replace(), link() never overwrites another installation ID.
                os.link(temporary, target)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
    # A corrupt saved ID is an error, not a reason to silently lose host history.
    return str(uuid.UUID(target.read_text(encoding="ascii").strip()))


def detect_environment():
    try:
        result = subprocess.run(
            ["systemd-detect-virt"], capture_output=True, text=True,
            timeout=3, check=False,
        )
        technology = result.stdout.strip()
        if result.returncode == 1 and technology == "none":
            return {"type": "bare_metal", "tech": None}
        if result.returncode != 0 or not technology:
            return {"type": "unknown", "tech": None}
        container = subprocess.run(
            ["systemd-detect-virt", "--container"], capture_output=True,
            text=True, timeout=3, check=False,
        )
        if container.returncode == 0:
            return {"type": "container", "tech": technology}
        if container.returncode == 1 and container.stdout.strip() == "none":
            return {"type": "vm", "tech": technology}
    except (OSError, subprocess.SubprocessError) as error:
        LOG.warning("Cannot detect environment: %s", error)
    return {"type": "unknown", "tech": None}


@dataclass(frozen=True)
class CpuSnapshot:
    counters: tuple
    logical_count: int


def read_cpu(proc):
    lines = (proc / "stat").read_text(encoding="ascii").splitlines()
    aggregate = next(line for line in lines if line.startswith("cpu "))
    values = [int(value) for value in aggregate.split()[1:]]
    if len(values) < 4 or any(value < 0 for value in values):
        raise ValueError("Invalid CPU counters")
    # user, nice, system, idle, iowait, irq, softirq, steal.
    # guest and guest_nice are already included in user and nice.
    counters = tuple((values + [0] * 8)[:8])
    logical_count = sum(bool(re.match(r"^cpu\d+\s", line)) for line in lines)
    if not logical_count:
        raise ValueError("No logical CPUs found")
    return CpuSnapshot(counters, logical_count)


def cpu_usage(previous, current):
    if previous is None or previous.logical_count != current.logical_count:
        return None
    delta = [new - old for old, new in zip(previous.counters, current.counters)]
    total = sum(delta)
    if any(value < 0 for value in delta) or total <= 0:
        return None
    busy = total - delta[3] - delta[4]
    return round(100.0 * busy / total, 2)


def read_memory(proc):
    values = {}
    for line in (proc / "meminfo").read_text(encoding="ascii").splitlines():
        name, raw = line.split(":", 1)
        if name in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            parts = raw.split()
            if len(parts) != 2 or parts[1] != "kB":
                raise ValueError("Unexpected memory units")
            values[name] = int(parts[0]) * 1024
    total, available = values["MemTotal"], values["MemAvailable"]
    swap_total, swap_free = values["SwapTotal"], values["SwapFree"]
    if not (0 <= available <= total and 0 <= swap_free <= swap_total):
        raise ValueError("Invalid memory counters")
    return (
        {"total_bytes": total, "available_bytes": available},
        {"total_bytes": swap_total, "used_bytes": swap_total - swap_free},
    )


def read_network(proc):
    interfaces = []
    for line in (proc / "net/dev").read_text(encoding="ascii").splitlines():
        if ":" not in line:
            continue
        name, raw = line.rsplit(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        fields = raw.split()
        if len(fields) < 16:
            raise ValueError("Invalid interface counters")
        received, sent = int(fields[0]), int(fields[8])
        if received < 0 or sent < 0:
            raise ValueError("Negative interface counter")
        interfaces.append({
            "interface": name,
            "rx_bytes_total": received,
            "tx_bytes_total": sent,
        })
    return sorted(interfaces, key=lambda item: item["interface"])


def unescape_mount(value):
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def read_filesystems(proc, mountpoints):
    types = {}
    for line in (proc / "self/mountinfo").read_text(encoding="utf-8").splitlines():
        before, after = line.split(" - ", 1)
        types[unescape_mount(before.split()[4])] = after.split()[0]
    filesystems = []
    for mountpoint in dict.fromkeys(mountpoints):
        entry = {"mountpoint": mountpoint, "fstype": types.get(mountpoint)}
        try:
            if mountpoint not in types:
                raise ValueError("Configured path is not a mounted filesystem")
            stat = os.statvfs(mountpoint)
            entry.update({
                "total_bytes": stat.f_blocks * stat.f_frsize,
                "used_bytes": (stat.f_blocks - stat.f_bfree) * stat.f_frsize,
                "available_bytes": stat.f_bavail * stat.f_frsize,
            })
        except (OSError, ValueError) as error:
            LOG.warning("Cannot collect filesystem %s: %s", mountpoint, error)
            entry.update(total_bytes=None, used_bytes=None, available_bytes=None)
        filesystems.append(entry)
    return filesystems


def safely(name, operation):
    try:
        return operation()
    except (OSError, ValueError, KeyError, IndexError, StopIteration) as error:
        LOG.warning("Cannot collect %s: %s", name, error)
        return None


class MetricsCollector:
    def __init__(self, settings, host_id, proc=Path("/proc"), environment=None):
        self.settings = settings
        self.host_id = host_id
        self.proc = proc
        self.environment = environment if environment is not None else detect_environment()
        self.previous_cpu = None

    def collect(self):
        collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        current = safely("CPU", lambda: read_cpu(self.proc))
        cpu = {
            "logical_count": current.logical_count if current else None,
            "usage_percent": cpu_usage(self.previous_cpu, current) if current else None,
        }
        # Update on collection, even when HTTP sending later fails.
        self.previous_cpu = current
        memory_swap = safely("memory", lambda: read_memory(self.proc))
        boot_id = safely("boot ID", lambda: str(uuid.UUID(
            (self.proc / "sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
        )))
        uptime = safely("uptime", lambda: int(float(
            (self.proc / "uptime").read_text(encoding="ascii").split()[0]
        )))
        return {
            "schema_version": 1,
            "agent_version": AGENT_VERSION,
            "host_id": self.host_id,
            "hostname": socket.gethostname(),
            "boot_id": boot_id,
            "collected_at": collected_at,
            "uptime_seconds": uptime,
            "env": self.environment,
            "cpu": cpu,
            "memory": memory_swap[0] if memory_swap else None,
            "swap": memory_swap[1] if memory_swap else None,
            "filesystems": safely("filesystems", lambda: read_filesystems(self.proc, self.settings.mountpoints)),
            "network": safely("network", lambda: read_network(self.proc)),
        }


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Do not silently turn a POST into GET or send host data elsewhere.
        return None


def send_report(report, settings):
    body = json.dumps(report, ensure_ascii=True, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(
        settings.collector_url, data=body, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "workload-collector/" + AGENT_VERSION},
    )
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=settings.http_timeout_seconds) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("Collector returned HTTP %s" % response.status)


def run_daemon(collector, settings, stop):
    # Monotonic scheduling: request duration does not add to every interval.
    deadline = time.monotonic()
    while not stop.is_set():
        report = collector.collect()
        if stop.is_set():
            break
        try:
            send_report(report, settings)
            LOG.info("Report delivered")
        except (OSError, urllib.error.URLError, http.client.HTTPException, RuntimeError) as error:
            # No unbounded queue or immediate retries. The next tick sends fresh data.
            LOG.warning("Report delivery failed: %s", error)
        deadline += settings.interval_seconds
        now = time.monotonic()
        if deadline <= now:
            missed = math.floor((now - deadline) / settings.interval_seconds) + 1
            deadline += missed * settings.interval_seconds
        stop.wait(max(0, deadline - time.monotonic()))
    LOG.info("Agent stopped")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--print-once", action="store_true", help="Print one JSON report without sending HTTP")
    parser.add_argument("--state-dir", type=Path, help="Override state directory for a manual/debug run")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if platform.system() != "Linux":
        parser.error("This agent requires Linux (/proc, statvfs and systemd)")
    try:
        settings = load_settings()
        if args.state_dir is not None:
            settings = replace(settings, state_directory=args.state_dir)
        host_id = load_host_id(settings.state_directory)
        collector = MetricsCollector(settings, host_id)
        if args.print_once:
            print(json.dumps(collector.collect(), indent=2, allow_nan=False))
            return 0
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda _signum, _frame: stop.set())
        LOG.info("Agent started: host_id=%s interval=%ss", host_id, settings.interval_seconds)
        run_daemon(collector, settings, stop)
    except (OSError, ValueError) as error:
        LOG.error("Cannot start agent: %s", error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
