#!/usr/bin/env python3

import json
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

HOST = "127.0.0.1"
PORT = 8080

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "metrics.db"


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id TEXT NOT NULL,
                hostname TEXT,
                collected_at TEXT,
                received_at TEXT NOT NULL,
                report_json TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE INDEX IF NOT EXISTS idx_reports_host
            ON reports(host_id, id)
        """)


def save_report(report):
    host_id = report.get("host_id")

    if not isinstance(host_id, str) or not host_id:
        raise ValueError("Missing or invalid host_id")

    hostname = report.get("hostname")
    collected_at = report.get("collected_at")

    received_at = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            INSERT INTO reports (
                host_id,
                hostname,
                collected_at,
                received_at,
                report_json
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            host_id,
            hostname,
            collected_at,
            received_at,
            json.dumps(report, ensure_ascii=False)
        ))


def get_latest_reports():
    with sqlite3.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row

        rows = db.execute("""
            SELECT r.report_json, r.received_at
            FROM reports r
            INNER JOIN (
                SELECT host_id, MAX(id) AS latest_id
                FROM reports
                GROUP BY host_id
            ) latest ON r.id = latest.latest_id
            ORDER BY r.hostname
        """).fetchall()

    result = []

    for row in rows:
        report = json.loads(row["report_json"])
        report["server_received_at"] = row["received_at"]
        result.append(report)

    return result


HTML = """
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workload Collector</title>

<style>
body {
    font-family: Arial, sans-serif;
    background: #f1f5f9;
    margin: 0;
    padding: 30px;
    color: #1e293b;
}

h1 {
    margin-bottom: 5px;
}

.subtitle {
    color: #64748b;
    margin-bottom: 25px;
}

.card {
    background: white;
    padding: 22px;
    margin-bottom: 20px;
    border-radius: 12px;
    box-shadow: 0 2px 8px #00000010;
}

.metrics {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 15px;
    margin-top: 20px;
}

.metric {
    background: #f8fafc;
    padding: 15px;
    border-radius: 8px;
}

.metric small {
    color: #64748b;
}

.metric h2 {
    margin: 8px 0 0;
    font-size: 23px;
}

pre {
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    background: #0f172a;
    color: #e2e8f0;
    padding: 15px;
    border-radius: 8px;
    overflow-x: auto;
}

.status {
    color: #16a34a;
    font-weight: bold;
}

.error {
    color: #dc2626;
}
</style>
</head>

<body>

<h1>Workload Collector</h1>
<p class="subtitle">Мониторинг системных показателей компьютеров</p>

<div id="status">Подключение к серверу...</div>
<div id="reports"></div>

<script>
function formatBytes(bytes) {
    if (bytes === null || bytes === undefined) return "Нет данных";

    const units = ["B", "KB", "MB", "GB", "TB"];
    let value = bytes;
    let index = 0;

    while (value >= 1024 && index < units.length - 1) {
        value /= 1024;
        index++;
    }

    return value.toFixed(2) + " " + units[index];
}

function metric(title, value) {
    const div = document.createElement("div");
    div.className = "metric";

    const label = document.createElement("small");
    label.textContent = title;

    const result = document.createElement("h2");
    result.textContent = value;

    div.append(label, result);
    return div;
}

function showReports(reports) {
    const container = document.getElementById("reports");
    container.replaceChildren();

    if (!reports.length) {
        container.textContent = "Пока сервер не получил ни одного отчёта.";
        return;
    }

    for (const report of reports) {
        const card = document.createElement("div");
        card.className = "card";

        const title = document.createElement("h2");
        title.textContent = report.hostname || "Неизвестный компьютер";

        const id = document.createElement("p");
        id.textContent = "Host ID: " + report.host_id;

        const time = document.createElement("p");
        time.textContent = "Последний отчёт: " +
            (report.collected_at || "Неизвестно");

        const metrics = document.createElement("div");
        metrics.className = "metrics";

        const cpu = report.cpu || {};
        const memory = report.memory || {};
        const swap = report.swap || {};

        metrics.append(
            metric(
                "Загрузка CPU",
                cpu.usage_percent == null
                    ? "Нет данных"
                    : cpu.usage_percent + "%"
            ),
            metric(
                "Оперативная память",
                memory.total_bytes == null
                    ? "Нет данных"
                    : formatBytes(
                        memory.total_bytes - memory.available_bytes
                    ) + " / " + formatBytes(memory.total_bytes)
            ),
            metric(
                "Доступная RAM",
                formatBytes(memory.available_bytes)
            ),
            metric(
                "Использование Swap",
                swap.used_bytes == null
                    ? "Нет данных"
                    : formatBytes(swap.used_bytes)
            ),
            metric(
                "Время работы",
                report.uptime_seconds == null
                    ? "Нет данных"
                    : report.uptime_seconds + " сек."
            )
        );

        const detailsTitle = document.createElement("h3");
        detailsTitle.textContent = "Полный JSON-отчёт";

        const details = document.createElement("pre");
        details.textContent = JSON.stringify(report, null, 2);

        card.append(title, id, time, metrics, detailsTitle, details);
        container.append(card);
    }
}

async function updateDashboard() {
    const status = document.getElementById("status");

    try {
        const response = await fetch("/api/v1/reports/latest");

        if (!response.ok) {
            throw new Error("HTTP " + response.status);
        }

        const reports = await response.json();

        status.className = "status";
        status.textContent = "Сервер работает. Компьютеров: " + reports.length;

        showReports(reports);

    } catch (error) {
        status.className = "error";
        status.textContent = "Ошибка получения данных: " + error.message;
    }
}

updateDashboard();
setInterval(updateDashboard, 5000);
</script>

</body>
</html>
"""


class RequestHandler(BaseHTTPRequestHandler):

    def send_json(self, status, data):
        body = json.dumps(
            data, ensure_ascii=False
        ).encode("utf-8")

        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path

        if path == "/":
            body = HTML.encode("utf-8")

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/v1/reports/latest":
            self.send_json(200, get_latest_reports())

        elif path == "/health":
            self.send_json(200, {"status": "ok"})

        else:
            self.send_json(404, {"error": "Not found"})

    def do_POST(self):
        path = urlsplit(self.path).path

        if path != "/api/v1/reports":
            self.send_json(404, {"error": "Not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))

            if length <= 0 or length > 1_000_000:
                self.send_json(413, {"error": "Invalid report size"})
                return

            raw_data = self.rfile.read(length)

            report = json.loads(
                raw_data.decode("utf-8"),
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError("Invalid JSON number: " + value)
                )
            )

            if not isinstance(report, dict):
                raise ValueError("JSON report must be an object")

            save_report(report)

            print(
                f"Report received: {report.get('hostname')} "
                f"({report.get('host_id')})",
                flush=True
            )

            self.send_json(201, {"status": "accepted"})

        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})

    def log_message(self, format, *args):
        print(
            f"[HTTP] {self.address_string()} - {format % args}",
            flush=True
        )


if __name__ == "__main__":
    init_db()

    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)

    print(f"Server started: http://{HOST}:{PORT}", flush=True)
    print(f"Database: {DB_PATH}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Server stopped")
    finally:
        server.server_close()
