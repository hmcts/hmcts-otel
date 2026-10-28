"""The export path's own log records are never exported (no feedback loop).

The Azure Monitor exporter logs every upload at INFO, and so does the HTTP logging
policy it uploads with. Exported, each record caused another upload, which logged
again: thousands of records in a few seconds (found by transcribe-api, its #25).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import pytest

from hmcts_otel._logging import EXPORT_PATH_LOGGERS, ExportPathFilter
from hmcts_otel.testing import capture_telemetry

EXPORT_PATH_RECORDS = [
    "azure.monitor.opentelemetry.exporter.export._base",
    "azure.monitor.opentelemetry.exporter",
    "azure.core.pipeline.policies.http_logging_policy",
    "opentelemetry.sdk._logs._internal.export",
]


@pytest.mark.parametrize("name", EXPORT_PATH_RECORDS)
def test_export_path_loggers_are_not_exported(name: str) -> None:
    with capture_telemetry() as tel:
        logging.getLogger("app").warning("application record")
        logging.getLogger(name).warning("export path record")
        bodies = [str(r.body) for r in (getattr(i, "log_record", i) for i in tel.log_records())]
    assert any("application record" in b for b in bodies)
    assert not any("export path record" in b for b in bodies)


def test_export_path_records_still_reach_other_handlers() -> None:
    """Only exported telemetry drops them: stdout (any other handler) still gets them."""
    seen: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.name)

    collect = Collect()
    logging.getLogger().addHandler(collect)
    try:
        with capture_telemetry():
            logging.getLogger("azure.monitor.opentelemetry.exporter").warning("upload failed")
    finally:
        logging.getLogger().removeHandler(collect)
    assert "azure.monitor.opentelemetry.exporter" in seen


@pytest.mark.parametrize(
    ("name", "kept"),
    [
        ("azure.monitor.opentelemetry.exporter._base", False),
        ("opentelemetry", False),
        ("azure.storage.blob", True),  # the app's own SDK clients are not the export path
        ("azure.monitoring_app", True),  # a prefix match needs the dot
        ("opentelemetry_app", True),
        ("hmcts_otel", True),
        ("app", True),
    ],
)
def test_the_filter_matches_logger_namespaces_not_prefixes(name: str, kept: bool) -> None:
    record = logging.LogRecord(name, logging.INFO, __file__, 1, "m", None, None)
    assert ExportPathFilter().filter(record) is kept


def test_the_names_are_namespaces() -> None:
    assert all(not n.endswith(".") for n in EXPORT_PATH_LOGGERS)


def test_the_real_exporter_does_not_export_its_own_upload_logs(run_isolated: Callable[[str], str]) -> None:
    """End to end, in a subprocess: the real distro and exporter, uploading to a local
    endpoint that accepts every batch. One application record must be exported once,
    not followed by the exporter's and HTTP policy's records about each upload."""
    script = r"""
import gzip, json, logging, os, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
messages = []

class Ingest(BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        if body.lstrip().startswith(b"["):
            lines = [json.dumps(e) for e in json.loads(body)]
        else:
            lines = body.decode().splitlines()
        count = 0
        for line in lines:
            envelope = json.loads(line)
            count += 1
            if envelope.get("data", {}).get("baseType") == "MessageData":
                messages.append(envelope["data"]["baseData"]["message"])
        reply = json.dumps({"itemsReceived": count, "itemsAccepted": count, "errors": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *args):
        pass

server = HTTPServer(("127.0.0.1", 0), Ingest)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["OTEL_BLRP_SCHEDULE_DELAY"] = "200"
os.environ["APPLICATIONINSIGHTS_STATSBEAT_DISABLED_ALL"] = "true"
from pydantic import SecretStr
from hmcts_otel import TelemetrySettings, setup_telemetry, shutdown_telemetry
# A fresh key: the exporter's offline storage is keyed by it, and would otherwise replay
# batches that an earlier run (with the same key) failed to upload.
import uuid
conn = f"InstrumentationKey={uuid.uuid4()};IngestionEndpoint=http://127.0.0.1:{server.server_port}/"
setup_telemetry(TelemetrySettings(service_name="t", sentry_dsn=None, connection_string=SecretStr(conn)))
# As verbose as a service that has not quietened the Azure SDK.
for name in ("azure", "azure.monitor.opentelemetry.exporter", "azure.core.pipeline.policies.http_logging_policy"):
    logging.getLogger(name).setLevel(logging.INFO)
logging.getLogger("app").warning("the one application record")
time.sleep(4)
shutdown_telemetry(2000)
print("RESULT", sum("the one application record" in m for m in messages), len(messages))
"""
    line = run_isolated(script)
    assert line == "RESULT 1 1", line
