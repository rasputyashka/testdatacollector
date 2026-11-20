"""Exercise the unmodified stdlib sender against a real ephemeral HTTP listener."""
import importlib.util
from pathlib import Path
import socket
import sys
import threading
import time

import uvicorn

from app.main import create_app


def test_actual_sender_send_report_to_loopback_uvicorn(settings, report, monkeypatch):
    path = Path(__file__).resolve().parents[2] / "data-sender" / "workload_collector.py"
    spec = importlib.util.spec_from_file_location("receiver_test_sender", path)
    sender = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, sender)
    spec.loader.exec_module(sender)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    application = create_app(settings)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1", port=port,
                                           log_level="error", access_log=False, lifespan="on"))
    errors = []

    def run_server():
        try:
            server.run(sockets=[listener])
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_server, name="receiver-test-uvicorn", daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started, f"Uvicorn did not start: {errors!r}"
        sender_settings = sender.Settings(
            collector_url=f"http://127.0.0.1:{port}/api/v1/reports", http_timeout_seconds=3,
        )
        assert sender.send_report(report, sender_settings) is None
        stored = application.state.storage.host(report["host_id"])
        assert stored is not None
        assert stored["report"] == report
        assert application.state.storage.history(report["host_id"], 1, 25)[1] == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=2)
        listener.close()
    assert not thread.is_alive(), "Uvicorn thread did not stop"
    assert errors == []
