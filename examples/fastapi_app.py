"""Minimal FastAPI service using hmcts-otel.

Run: OTEL_SERVICE_NAME=example-api uv run uvicorn examples.fastapi_app:app
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request

from hmcts_otel import (
    EventClass,
    capture_trace_context,
    default_schema,
    journey,
    setup_telemetry,
    shutdown_telemetry,
    stage,
)
from hmcts_otel.fastapi import create_telemetry_relay_router, instrument_fastapi

setup_telemetry()

default_schema.attribute("audio.format", allowed_values={"wav", "mp3"}, description="Uploaded audio format")
default_schema.attribute("ui.mode", allowed_values={"dictation", "upload"}, description="Screen mode")
default_schema.event("dictation.no_transcript", browser=True, description="Session ended with no text")
default_schema.event("feature.opened", event_class=EventClass.ANALYTICS, attributes=["ui.mode"], browser=True)


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    shutdown_telemetry()


app = FastAPI(lifespan=lifespan)
instrument_fastapi(app)
app.include_router(
    create_telemetry_relay_router(
        path="/api/telemetry",
        analytics_consent=lambda request: request.cookies.get("analytics") == "yes",
        # Journeys the browser owns. Without this, events carrying a journey are rejected.
        journey_types={"dictation.session"},
    )
)

JOBS: dict[str, str | None] = {}


@app.post("/api/jobs")
async def create_job(request: Request) -> dict[str, str]:
    job_id = str(uuid.uuid4())
    with journey("example.job", job_id):
        with stage("upload", attributes={"audio.format": "wav"}):
            JOBS[job_id] = capture_trace_context()  # store with the job for the worker
    return {"id": job_id}
