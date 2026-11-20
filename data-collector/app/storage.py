"""Short-lived SQLite connections; all callers run in worker threads."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .schemas import Report


def utc_text(value):
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class Storage:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY,
                    host_id TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reports_host_time
                    ON reports(host_id, collected_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS hosts (
                    host_id TEXT PRIMARY KEY,
                    latest_report_id INTEGER NOT NULL REFERENCES reports(id),
                    last_received_at TEXT NOT NULL
                );
            """)

    def save(self, report: Report):
        payload = report.model_dump(mode="json")
        # Reject non-finite numbers even inside additive/unknown fields.
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self.connect() as db:
            # Acquire the write lock before taking the receipt timestamp.
            db.execute("BEGIN IMMEDIATE")
            received = utc_text(datetime.now(timezone.utc))
            cursor = db.execute(
                "INSERT INTO reports(host_id, hostname, collected_at, received_at, payload) VALUES(?,?,?,?,?)",
                (str(report.host_id), report.hostname, utc_text(report.collected_at), received, serialized),
            )
            report_id = cursor.lastrowid
            db.execute("""
                INSERT INTO hosts(host_id, latest_report_id, last_received_at) VALUES(?,?,?)
                ON CONFLICT(host_id) DO UPDATE SET
                    last_received_at=excluded.last_received_at,
                    latest_report_id=CASE WHEN
                        (SELECT collected_at FROM reports WHERE id=excluded.latest_report_id) >=
                        (SELECT collected_at FROM reports WHERE id=hosts.latest_report_id)
                        THEN excluded.latest_report_id ELSE hosts.latest_report_id END
            """, (str(report.host_id), report_id, received))
        return report_id

    @staticmethod
    def unpack(row):
        if row is None:
            return None
        result = dict(row)
        result["report"] = json.loads(result.pop("payload"))
        return result

    def list_hosts(self, search, page, size):
        # Treat search as literal text, not SQL wildcard syntax.
        pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where = "WHERE r.hostname LIKE ? ESCAPE '\\' OR h.host_id LIKE ? ESCAPE '\\'"
        with self.connect() as db:
            count = db.execute(f"SELECT count(*) FROM hosts h JOIN reports r ON r.id=h.latest_report_id {where}",
                               (pattern, pattern)).fetchone()[0]
            rows = db.execute(f"""SELECT r.*, h.last_received_at FROM hosts h
                JOIN reports r ON r.id=h.latest_report_id {where}
                ORDER BY r.hostname COLLATE NOCASE, h.host_id LIMIT ? OFFSET ?""",
                (pattern, pattern, size, (page - 1) * size)).fetchall()
        return [self.unpack(row) for row in rows], count

    def host(self, host_id):
        with self.connect() as db:
            row = db.execute("""SELECT r.*, h.last_received_at FROM hosts h
                JOIN reports r ON r.id=h.latest_report_id WHERE h.host_id=?""", (host_id,)).fetchone()
        return self.unpack(row)

    def history(self, host_id, page, size):
        with self.connect() as db:
            count = db.execute("SELECT count(*) FROM reports WHERE host_id=?", (host_id,)).fetchone()[0]
            rows = db.execute("""SELECT * FROM reports WHERE host_id=?
                ORDER BY collected_at DESC, id DESC LIMIT ? OFFSET ?""",
                (host_id, size, (page - 1) * size)).fetchall()
        return [self.unpack(row) for row in rows], count

    def previous(self, record):
        with self.connect() as db:
            row = db.execute("""SELECT * FROM reports WHERE host_id=? AND
                (collected_at < ? OR (collected_at = ? AND id < ?))
                ORDER BY collected_at DESC, id DESC LIMIT 1""",
                (record["host_id"], record["collected_at"], record["collected_at"], record["id"])).fetchone()
        return self.unpack(row)

    def report(self, report_id):
        if not 0 < report_id <= 2**63 - 1:
            return None
        with self.connect() as db:
            row = db.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
        return self.unpack(row)

    def healthy(self):
        with self.connect() as db:
            db.execute("SELECT id FROM reports LIMIT 1").fetchone()
