"""Display helpers: unknown is never silently converted to zero."""
from datetime import datetime, timezone


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def timestamp(value):
    return parse_time(value).strftime("%d.%m.%Y %H:%M:%S UTC")


def byte_size(value):
    if value is None:
        return "Нет данных"
    number = value
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ", "ПиБ"):
        if abs(number) < 1024 or unit == "ПиБ":
            return f"{number:.1f} {unit}" if unit != "Б" else f"{number} Б"
        number /= 1024


def percent(value):
    return "Нет данных" if value is None else f"{value:.1f}%"


def memory_percent(block):
    if not block or not block["total_bytes"]:
        return None
    return 100 * (block["total_bytes"] - block["available_bytes"]) / block["total_bytes"]


def uptime(value):
    if value is None:
        return "Нет данных"
    days, remainder = divmod(value, 86400)
    hours, remainder = divmod(remainder, 3600)
    return f"{days} д. {hours} ч. {remainder // 60} мин."


def is_stale(received_at, threshold):
    return (datetime.now(timezone.utc) - parse_time(received_at)).total_seconds() > threshold


def network_rows(current, previous):
    report = current["report"]
    entries = report["network"]
    if entries is None:
        return None
    old = previous["report"] if previous else None
    valid = old is not None
    seconds = 0
    if valid:
        seconds = (parse_time(report["collected_at"]) - parse_time(old["collected_at"])).total_seconds()
        valid = seconds > 0 and not (
            old["boot_id"] is not None and report["boot_id"] is not None
            and old["boot_id"] != report["boot_id"]
        )
    baselines = {item["interface"]: item for item in (old["network"] or [])} if valid else {}
    result = []
    for entry in entries:
        row = dict(entry, rx_rate=None, tx_rate=None)
        before = baselines.get(entry["interface"])
        if before and all(entry[key] >= before[key] for key in ("rx_bytes_total", "tx_bytes_total")):
            row["rx_rate"] = (entry["rx_bytes_total"] - before["rx_bytes_total"]) / seconds
            row["tx_rate"] = (entry["tx_bytes_total"] - before["tx_bytes_total"]) / seconds
        result.append(row)
    return result
