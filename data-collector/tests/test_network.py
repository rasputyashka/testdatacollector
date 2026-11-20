import copy

import pytest

from app.presentation import network_rows


@pytest.mark.parametrize(("scenario", "expected"), [
    ("normal", (40, 20)),
    ("unchanged", (0, 0)),
    ("null_current", None),
    ("empty_current", []),
    ("null_previous", (None, None)),
    ("empty_previous", (None, None)),
    ("rx_reset", (None, None)),
    ("tx_reset", (None, None)),
    ("reboot", (None, None)),
    ("unknown_old_boot", (40, 20)),
    ("unknown_new_boot", (40, 20)),
    ("equal_time", (None, None)),
    ("new_interface", (None, None)),
])
def test_network_rates_on_host_detail(client, report, scenario, expected):
    before = copy.deepcopy(report)
    before["network"] = [{"interface": "eth0", "rx_bytes_total": 1000, "tx_bytes_total": 2000}]
    current = copy.deepcopy(before)
    current["collected_at"] = "2026-11-17T12:00:10Z"
    current["network"][0].update(rx_bytes_total=1400, tx_bytes_total=2200)
    if scenario == "unchanged":
        current["network"] = copy.deepcopy(before["network"])
    elif scenario == "null_current":
        current["network"] = None
    elif scenario == "empty_current":
        current["network"] = []
    elif scenario == "null_previous":
        before["network"] = None
    elif scenario == "empty_previous":
        before["network"] = []
    elif scenario == "rx_reset":
        current["network"][0]["rx_bytes_total"] = 1
    elif scenario == "tx_reset":
        current["network"][0]["tx_bytes_total"] = 1
    elif scenario == "reboot":
        current["boot_id"] = "f8c610e7-08ea-4f0b-9fc4-853e63ffb9d1"
    elif scenario == "unknown_old_boot":
        before["boot_id"] = None
    elif scenario == "unknown_new_boot":
        current["boot_id"] = None
    elif scenario == "equal_time":
        current["collected_at"] = before["collected_at"]
    elif scenario == "new_interface":
        current["network"][0]["interface"] = "eth1"
    for payload in (before, current):
        assert client.post("/api/v1/reports", json=payload).status_code == 204
    response = client.get(f'/hosts/{report["host_id"]}')
    assert response.status_code == 200
    rows = response.context["network"]
    if expected is None or expected == []:
        assert rows == expected
    else:
        assert len(rows) == 1
        assert (rows[0]["rx_rate"], rows[0]["tx_rate"]) == expected
        assert rows[0]["rx_bytes_total"] == current["network"][0]["rx_bytes_total"]
        if scenario == "normal":
            assert "40.0 Б/с" in response.text or "40 Б/с" in response.text


def test_first_network_sample_and_multiple_interfaces_are_not_summed(client, report):
    report["network"] = [
        {"interface": "eth0", "rx_bytes_total": 100, "tx_bytes_total": 200},
        {"interface": "eth1", "rx_bytes_total": 300, "tx_bytes_total": 400},
    ]
    assert client.post("/api/v1/reports", json=report).status_code == 204
    response = client.get(f'/hosts/{report["host_id"]}')
    assert response.status_code == 200
    assert [row["rx_rate"] for row in response.context["network"]] == [None, None]
    report["collected_at"] = "2026-11-17T12:00:10Z"
    report["network"][0].update(rx_bytes_total=200, tx_bytes_total=400)
    report["network"][1].update(rx_bytes_total=600, tx_bytes_total=800)
    assert client.post("/api/v1/reports", json=report).status_code == 204
    rows = client.get(f'/hosts/{report["host_id"]}').context["network"]
    assert [(row["interface"], row["rx_rate"], row["tx_rate"]) for row in rows] == [
        ("eth0", 10, 20), ("eth1", 30, 40),
    ]


def test_out_of_order_report_is_baseline_by_collection_not_receipt(client, report):
    for seconds, rx in [(0, 1000), (20, 2000), (10, 1800)]:
        report["collected_at"] = f"2026-11-17T12:00:{seconds:02d}Z"
        report["network"][0].update(rx_bytes_total=rx, tx_bytes_total=rx * 2)
        assert client.post("/api/v1/reports", json=report).status_code == 204
    detail = client.get(f'/hosts/{report["host_id"]}')
    assert detail.context["record"]["id"] == 2
    row = detail.context["network"][0]
    assert (row["rx_rate"], row["tx_rate"]) == (20, 40)


def test_network_helper_does_not_derive_negative_time_rates(report):
    previous = {"report": copy.deepcopy(report)}
    report["collected_at"] = "2026-11-17T11:59:59Z"
    report["network"][0]["rx_bytes_total"] += 100
    rows = network_rows({"report": report}, previous)
    assert rows[0]["rx_rate"] is None
    assert rows[0]["tx_rate"] is None
