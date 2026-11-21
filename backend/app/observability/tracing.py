"""Optional OpenTelemetry tracing and LangSmith tracing. Both are strictly opt-in and never break startup:
if the packages (requirements-otel.txt) or endpoint are missing, a warning is logged and the app runs without them."""
from __future__ import annotations

import os

from app.core.config import Settings
from app.core.logging import get_logger

log = get_logger("nexus.observability")


def setup_tracing(app, settings: Settings) -> dict[str, str]:
    """Returns a status dict ({"otel": "...", "langsmith": "..."}) that /health/ready-style endpoints can surface."""
    status = {"otel": "disabled", "langsmith": "disabled"}
    if settings.otel_enabled:
        try:
            from opentelemetry import trace
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor

            provider = TracerProvider(resource=Resource.create({"service.name": settings.app_name, "deployment.environment": settings.env}))
            if settings.otel_endpoint:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

                provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_endpoint)))
                status["otel"] = f"exporting to {settings.otel_endpoint}"
            else:
                status["otel"] = "enabled (no exporter configured: spans are created but not exported)"
            trace.set_tracer_provider(provider)
            FastAPIInstrumentor.instrument_app(app, tracer_provider=provider, excluded_urls="health.*,metrics")
            app.state.tracer_provider = provider
        except ImportError as e:
            status["otel"] = f"requested but packages missing ({e.name}); install requirements-otel.txt"
            log.warning(status["otel"])
        except Exception as e:  # noqa: BLE001
            status["otel"] = f"failed to initialise: {type(e).__name__}"
            log.warning(status["otel"])
    if settings.langsmith_enabled:
        # LangChain/LangGraph read these variables themselves; we only switch them on explicitly and never log the key.
        if os.environ.get("LANGSMITH_API_KEY") or os.environ.get("LANGCHAIN_API_KEY"):
            os.environ["LANGSMITH_TRACING"] = "true"
            status["langsmith"] = "enabled"
        else:
            status["langsmith"] = "requested but LANGSMITH_API_KEY is not set"
            log.warning(status["langsmith"])
    return status
