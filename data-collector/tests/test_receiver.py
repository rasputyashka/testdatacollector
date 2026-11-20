import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import html
import json
import sqlite3
from uuid import UUID

from fastapi.testclient import TestClient
from markupsafe import escape
import pytest

from app import storage as storage_module
from app.main import create_app


ENDPOINT = "/api/v1/reports"


def post(client, report):
    response = client.post(ENDPOINT, json=report)
    assert response.status_code == 204, response.text
    return response


def latest(client, report):
    return client.app.state.storage.host(report["host_id"])


def test_sender_protocol_exact_204_without_redirect(client, report):
    response = post(client, report)
    assert response.content == b""
    assert "location" not in response.headers
    assert not response.history
    assert client.post(ENDPOINT + "/", json=report).status_code == 404
    assert client.get(ENDPOINT).status_code == 405
    assert client.get("/api/v1/reports/1").json() == report
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("content", [b"", b"{", b"null", b"[]", b"true", b'"report"', b"42", b"{}", b"\xff", b'{"hostname":'])
def test_malformed_json_or_non_report_rejected(client, content):
    response = client.post(ENDPOINT, content=content, headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert client.app.state.storage.list_hosts("", 1, 25) == ([], 0)


@pytest.mark.parametrize(("path", "value"), [
    (("schema_version",), 2), (("schema_version",), True), (("schema_version",), "1"),
    (("schema_version",), 1.0), (("host_id",), "not-a-uuid"), (("boot_id",), "not-a-uuid"),
    (("host_id",), None), (("hostname",), ""), (("hostname",), 123),
    (("agent_version",), ""), (("hostname",), "x" * 4097),
    (("collected_at",), "2026-11-17T12:00:00"), (("collected_at",), "yesterday"),
    (("collected_at",), 1794916800), (("collected_at",), True),
    (("uptime_seconds",), -1), (("uptime_seconds",), True), (("uptime_seconds",), "123"),
    (("uptime_seconds",), 1.5), (("env", "type"), "physical"),
    (("env", "tech"), 12), (("cpu", "logical_count"), 0),
    (("cpu", "logical_count"), False), (("cpu", "usage_percent"), "27.5"),
    (("cpu", "usage_percent"), True), (("cpu", "usage_percent"), -0.1),
    (("cpu", "usage_percent"), 100.1), (("memory", "available_bytes"), 10**15),
    (("memory", "total_bytes"), "8589934592"), (("memory", "available_bytes"), False),
    (("swap", "used_bytes"), 10**15), (("swap", "total_bytes"), -1),
    (("filesystems", 0, "used_bytes"), True), (("filesystems", 0, "available_bytes"), -1),
    (("network", 0, "rx_bytes_total"), "123"), (("network", 0, "tx_bytes_total"), -1),
    (("network", 0, "rx_bytes_total"), 1.5), (("network", 0, "interface"), ""),
    (("network",), {}), (("filesystems",), {}), (("cpu",), None), (("env",), None),
])
def test_invalid_wire_fields(client, report, path, value):
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    response = client.post(ENDPOINT, json=report)
    assert response.status_code == 422, response.text
    assert client.app.state.storage.history(report["host_id"], 1, 25)[1] == 0


@pytest.mark.parametrize("field", ["schema_version", "host_id", "collected_at", "cpu", "memory", "network", "boot_id"])
def test_required_fields_cannot_be_omitted(client, report, field):
    del report[field]
    assert client.post(ENDPOINT, json=report).status_code == 422


@pytest.mark.parametrize("field", ["network", "filesystems"])
def test_duplicate_names_and_oversized_arrays_rejected(client, report, field):
    item = report[field][0]
    report[field] = [item, copy.deepcopy(item)]
    assert client.post(ENDPOINT, json=report).status_code == 422
    report[field] = [item] * 1025
    assert client.post(ENDPOINT, json=report).status_code == 422


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("location", ["cpu", "additive"])
def test_nonfinite_numbers_rejected_even_in_additive_fields(client, report, value, location):
    if location == "cpu":
        report["cpu"]["usage_percent"] = value
    else:
        report["future"] = {"nested": [value]}
    response = client.post(ENDPOINT, content=json.dumps(report), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert client.app.state.storage.list_hosts("", 1, 25)[1] == 0


@pytest.mark.parametrize("location", ["cpu", "additive"])
def test_overflowing_json_number_rejected(client, report, location):
    if location == "cpu":
        report["cpu"]["usage_percent"] = "OVERFLOW"
    else:
        report["future"] = ["OVERFLOW"]
    body = json.dumps(report).replace('"OVERFLOW"', "1e9999")
    assert client.post(ENDPOINT, content=body, headers={"Content-Type": "application/json"}).status_code == 422


def test_timezone_and_uuid_normalization_and_additive_fields(client, report):
    report["host_id"] = report["host_id"].upper()
    report["boot_id"] = report["boot_id"].upper()
    report["collected_at"] = "2026-11-17T15:30:00+03:30"
    report["future"] = {"text": "новое", "values": [1, None, True]}
    report["cpu"]["future_counter"] = 42
    post(client, report)
    stored = client.get("/api/v1/reports/1").json()
    assert stored["host_id"] == report["host_id"].lower()
    assert stored["boot_id"] == report["boot_id"].lower()
    assert stored["collected_at"] == "2026-11-17T12:00:00Z"
    assert stored["future"] == report["future"]
    assert stored["cpu"]["future_counter"] == 42
    assert client.get(f'/hosts/{report["host_id"]}').status_code == 200


def test_partial_null_metrics_remain_unknown_not_zero(client, report):
    for key in ("boot_id", "uptime_seconds", "memory", "swap", "filesystems", "network"):
        report[key] = None
    report["cpu"] = {"logical_count": None, "usage_percent": None}
    report["env"] = {"type": "unknown", "tech": None}
    post(client, report)
    assert client.get("/api/v1/reports/1").json() == report
    detail = client.get(f'/hosts/{report["host_id"]}')
    assert detail.status_code == 200
    assert detail.context["network"] is None
    assert "Нет данных: агент не смог прочитать файловые системы." in detail.text
    assert "Нет данных: агент не смог прочитать сетевые счётчики." in detail.text
    assert "Не используется" not in detail.text
    assert "0.0%" not in detail.text
    assert client.get("/").status_code == 200


def test_empty_arrays_zero_swap_and_partial_filesystem(client, report):
    report["filesystems"] = []
    report["network"] = []
    report["swap"] = {"total_bytes": 0, "used_bytes": 0}
    post(client, report)
    detail = client.get(f'/hosts/{report["host_id"]}')
    assert detail.status_code == 200
    assert detail.context["network"] == []
    assert "Точек монтирования нет." in detail.text
    assert "Сетевых интерфейсов нет." in detail.text
    assert "Не используется" in detail.text
    report["filesystems"] = [{"mountpoint": "/offline", "fstype": None, "total_bytes": None,
                              "used_bytes": None, "available_bytes": None}]
    post(client, report)
    assert client.get(f'/hosts/{report["host_id"]}').status_code == 200
    assert client.get("/api/v1/reports/2").json()["filesystems"] == report["filesystems"]


def test_reserved_filesystem_space_does_not_require_sum_to_total(client, report):
    report["filesystems"] = [{"mountpoint": "/", "fstype": "ext4", "total_bytes": 100,
                              "used_bytes": 40, "available_bytes": 55}]
    post(client, report)
    assert client.get("/api/v1/reports/1").json()["filesystems"] == report["filesystems"]
    detail = client.get(f'/hosts/{report["host_id"]}')
    assert detail.status_code == 200
    assert "40 Б" in detail.text and "55 Б" in detail.text
    assert "часть пространства зарезервирована" in detail.text


@pytest.mark.parametrize("headers", [{}, {"Content-Type": "text/plain"}, {"Content-Type": "application/problem+json"},
                                       {"Content-Type": "application/json", "Content-Encoding": "gzip"}])
def test_unsupported_media_or_encoding(client, report, headers):
    response = client.post(ENDPOINT, content=json.dumps(report), headers=headers)
    assert response.status_code == 415


def test_json_media_type_parameters_accepted(client, report):
    response = client.post(ENDPOINT, content=json.dumps(report),
                           headers={"Content-Type": "Application/JSON; charset=utf-8"})
    assert response.status_code == 204


@pytest.mark.parametrize("length", ["bad", "-1"])
def test_invalid_content_length(client, report, length):
    response = client.post(ENDPOINT, content=json.dumps(report),
                           headers={"Content-Type": "application/json", "Content-Length": length})
    assert response.status_code == 400


def test_body_limit_declared_and_chunked_without_content_length(settings, report):
    body = json.dumps(report).encode()
    application = create_app(replace(settings, max_body_bytes=len(body)))
    with TestClient(application) as client:
        assert client.post(ENDPOINT, content=body, headers={"Content-Type": "application/json"}).status_code == 204
        too_large = client.post(ENDPOINT, content=body + b" ", headers={"Content-Type": "application/json"})
        assert too_large.status_code == 413
        streamed = client.post(ENDPOINT, content=iter([body[:100], body[100:], b" "]),
                               headers={"Content-Type": "application/json"})
        assert "content-length" not in streamed.request.headers
        assert streamed.status_code == 413
        valid_stream = client.post(ENDPOINT, content=iter([body[:100], body[100:]]),
                                   headers={"Content-Type": "application/json"})
        assert "content-length" not in valid_stream.request.headers
        assert valid_stream.status_code == 204
        # A lying, smaller length must not bypass the streamed byte count.
        assert client.post(ENDPOINT, content=iter([body, b" "]),
                           headers={"Content-Type": "application/json", "Content-Length": "1"}).status_code == 413
        assert application.state.storage.history(report["host_id"], 1, 25)[1] == 2


@pytest.mark.parametrize(("method", "path", "operation"), [
    ("post", ENDPOINT, "save"), ("get", "/healthz", "healthy"),
    ("get", "/", "list_hosts"), ("get", "/api/v1/reports/1", "report"),
    ("get", "/reports/1", "report"),
])
def test_sqlite_failure_is_503_without_leaking_error(client, report, monkeypatch, method, path, operation):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("sensitive database path /secret/database")
    monkeypatch.setattr(client.app.state.storage, operation, fail)
    response = client.request(method, path, **({"json": report} if method == "post" else {}))
    assert response.status_code == 503
    assert response.json() == {"detail": "Хранилище временно недоступно"}
    assert "/secret" not in response.text


def test_persistence_survives_app_recreation(settings, report):
    with TestClient(create_app(settings)) as first:
        post(first, report)
    with TestClient(create_app(settings)) as second:
        assert second.get("/healthz").json() == {"status": "ok"}
        assert second.get("/api/v1/reports/1").json() == report
        assert second.get("/").context["total"] == 1
        assert second.get(f'/hosts/{report["host_id"]}').context["total"] == 1
        post(second, report)
        assert second.get(f'/hosts/{report["host_id"]}').context["total"] == 2


def test_duplicates_out_of_order_equal_time_latest_and_receipt(client, report, monkeypatch):
    clock = datetime(2026, 11, 17, 13, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock

    monkeypatch.setattr(storage_module, "datetime", Clock)
    post(client, report)
    first = latest(client, report)
    clock += timedelta(seconds=10)
    post(client, report)  # Duplicates are retained, not deduplicated.
    assert latest(client, report)["id"] == 2
    clock += timedelta(seconds=10)
    report["collected_at"] = "2026-11-17T11:00:00Z"
    report["hostname"] = "old-name"
    post(client, report)
    current = latest(client, report)
    assert current["id"] == 2
    assert current["hostname"] == "study-server"
    assert current["last_received_at"] == "2026-11-17T13:00:20.000000Z"
    assert current["last_received_at"] > current["received_at"] > first["received_at"]
    assert client.get("/").context["records"][0]["last_received_at"] == current["last_received_at"]
    report["collected_at"] = "2026-11-17T12:00:00Z"
    report["hostname"] = "equal-time-new-name"
    clock += timedelta(seconds=10)
    post(client, report)
    current = latest(client, report)
    assert current["id"] == 4
    assert current["hostname"] == "equal-time-new-name"
    rows, total = client.app.state.storage.history(report["host_id"], 1, 25)
    assert total == 4
    assert [row["id"] for row in rows] == [4, 2, 1, 3]
    assert client.get(f'/hosts/{report["host_id"]}').context["record"]["id"] == 4


def test_search_literal_wildcards_uuid_and_host_pagination(client, report):
    names = ["literal%host", "literal_host", "literal\\host", "literalXhost"]
    names += [f"node-{i:02d}" for i in range(26)]
    for i, name in enumerate(names, 1):
        report.update(host_id=str(UUID(int=i)), hostname=name)
        post(client, report)
    for query, expected in [("%", names[0]), ("_", names[1]), ("\\", names[2]),
                            (str(UUID(int=4)), names[3]), ("LITERAL%", names[0])]:
        response = client.get("/", params={"q": query})
        assert response.status_code == 200
        assert response.context["total"] == 1
        assert response.context["records"][0]["hostname"] == expected
    first = client.get("/")
    second = client.get("/", params={"page": 2})
    assert first.context["total"] == second.context["total"] == 30
    assert len(first.context["records"]) == 25
    assert len(second.context["records"]) == 5
    first_ids = {r["host_id"] for r in first.context["records"]}
    assert first_ids.isdisjoint(r["host_id"] for r in second.context["records"])
    assert client.get("/", params={"q": "absent"}).context["total"] == 0
    assert client.get("/", params={"page": 3}).context["records"] == []
    assert client.get("/", params={"q": "node-", "page": 2}).context["total"] == 26
    assert "q=node-" in client.get("/", params={"q": "node-"}).text


def test_history_pagination(client, report):
    for i in range(27):
        report["collected_at"] = f"2026-11-17T12:00:{i:02d}Z"
        post(client, report)
    path = f'/hosts/{report["host_id"]}'
    first = client.get(path)
    second = client.get(path, params={"page": 2})
    assert first.context["total"] == second.context["total"] == 27
    assert [item["id"] for item in first.context["history"]] == list(range(27, 2, -1))
    assert [item["id"] for item in second.context["history"]] == [2, 1]
    assert client.get(path, params={"page": 3}).context["history"] == []


def test_html_escapes_all_report_strings_and_json_preview(client, report):
    values = {key: f'<script>{key}</script>&\"\''
              for key in ("hostname", "agent", "environment", "mountpoint", "fstype", "interface", "future")}
    report["hostname"] = values["hostname"]
    report["agent_version"] = values["agent"]
    report["env"]["tech"] = values["environment"]
    report["filesystems"][0].update(mountpoint=values["mountpoint"], fstype=values["fstype"])
    report["network"][0]["interface"] = values["interface"]
    report["future"] = values["future"]
    post(client, report)
    for path in ("/", f'/hosts/{report["host_id"]}', "/reports/1"):
        response = client.get(path)
        assert response.status_code == 200
        assert "<script>" not in response.text
        keys = ("hostname", "environment") if path == "/" else tuple(values)
        if path.startswith("/hosts/"):
            keys = tuple(key for key in keys if key != "future")
        for key in keys:
            # JSON preview contains JSON quoting, but HTML markup must always be escaped.
            assert f'&lt;script&gt;{key}&lt;/script&gt;&amp;' in response.text
            assert f'<script>{key}</script>&' in html.unescape(response.text)
        assert "script-src 'none'" in response.headers["content-security-policy"]
    assert client.get("/api/v1/reports/1").json() == report
    search = client.get("/", params={"q": values["hostname"]})
    assert values["hostname"] not in search.text
    assert str(escape(values["hostname"])) in search.text
    assert search.context["total"] == 1


def test_health_empty_state_and_not_found(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert "Ожидаем первый отчёт" in client.get("/").text
    for path in (f"/hosts/{UUID(int=999)}", "/reports/999", "/api/v1/reports/999", "/missing",
                 "/reports/9223372036854775808", "/api/v1/reports/9223372036854775808",
                 "/reports/-9223372036854775809", "/api/v1/reports/-9223372036854775809"):
        assert client.get(path).status_code == 404
    assert client.get("/hosts/not-a-uuid").status_code == 422
    assert client.get("/reports/not-an-integer").status_code == 422
    assert client.get("/healthz", headers={"Host": "untrusted.example"}).status_code == 400


@pytest.mark.parametrize("params", [{"page": 0}, {"page": -1}, {"page": "invalid"}, {"page": 1000001}])
def test_pagination_validation(client, report, params):
    assert client.get("/", params=params).status_code == 422
    assert client.get(f'/hosts/{report["host_id"]}', params=params).status_code == 422


def test_search_length_validation(client):
    assert client.get("/", params={"q": "x" * 257}).status_code == 422


@pytest.mark.parametrize("value", ["1794916800", "1794916800000", "0"])
def test_numeric_epoch_strings_are_not_iso_timestamps(client, report, value):
    report["collected_at"] = value
    assert client.post(ENDPOINT, json=report).status_code == 422


@pytest.mark.parametrize("path", [("uptime_seconds",), ("memory", "total_bytes"),
                                  ("filesystems", 0, "total_bytes"), ("network", 0, "rx_bytes_total")])
def test_uint64_counter_limit(client, report, path):
    target = report
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = 2**64 - 1
    post(client, report)
    target[path[-1]] = 2**64
    assert client.post(ENDPOINT, json=report).status_code == 422
    assert client.app.state.storage.history(report["host_id"], 1, 25)[1] == 1


@pytest.mark.parametrize("field", ["hostname", "future"])
def test_unpaired_unicode_surrogate_rejected_without_storage_error(client, report, field):
    report[field] = "\ud800"
    # ensure_ascii keeps this malformed Unicode representable in the request body.
    response = client.post(ENDPOINT, content=json.dumps(report), headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert client.app.state.storage.list_hosts("", 1, 25)[1] == 0
