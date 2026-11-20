import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app


@pytest.fixture
def report():
    return {
        "schema_version": 1,
        "agent_version": "0.1.0",
        "host_id": "a6e1bd26-f21f-42db-baa5-451019a58c89",
        "hostname": "study-server",
        "boot_id": "90354f14-d9cf-46ae-86f2-0cfaa27f8003",
        "collected_at": "2026-11-17T12:00:00Z",
        "uptime_seconds": 86400,
        "env": {"type": "vm", "tech": "kvm"},
        "cpu": {"logical_count": 4, "usage_percent": 27.5},
        "memory": {"total_bytes": 8589934592, "available_bytes": 5368709120},
        "swap": {"total_bytes": 2147483648, "used_bytes": 0},
        "filesystems": [
            {
                "mountpoint": "/",
                "fstype": "ext4",
                "total_bytes": 107374182400,
                "used_bytes": 42949672960,
                "available_bytes": 59055800320,
            }
        ],
        "network": [
            {"interface": "eth0", "rx_bytes_total": 1234567890, "tx_bytes_total": 987654321}
        ],
    }


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "test.sqlite3",
        allowed_hosts=("testserver", "localhost", "127.0.0.1"),
    )


@pytest.fixture
def application(settings):
    return create_app(settings)


@pytest.fixture
def client(application):
    with TestClient(application, follow_redirects=False) as client:
        yield client
