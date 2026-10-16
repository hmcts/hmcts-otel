"""A polling worker using hmcts-otel, linked to the request that created each job.

Run: OTEL_SERVICE_NAME=example-worker uv run python -m examples.worker
"""

from __future__ import annotations

import signal
import threading
from dataclasses import dataclass

from hmcts_otel import TelemetrySettings, journey, setup_telemetry, shutdown_telemetry, stage

POLL_INTERVAL_S = 30


@dataclass
class Job:
    id: str
    traceparent: str | None
    speech_job_id: str | None = None


def fetch_pending() -> list[Job]:  # stand-in for a database query
    return []


def main() -> None:
    setup_telemetry(TelemetrySettings(service_name="example-worker"))
    stopping = threading.Event()
    # SIGTERM (Kubernetes) and SIGINT stop the loop immediately, not after the next sleep.
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    try:
        while not stopping.is_set():
            for job in fetch_pending():
                with journey("example.job", job.id):
                    with stage("transcription", link_from=job.traceparent) as s:
                        if job.speech_job_id:
                            s.set_dependency_ref(job.speech_job_id)
            stopping.wait(POLL_INTERVAL_S)
    finally:
        shutdown_telemetry()  # flushes within its time budget


if __name__ == "__main__":
    main()
