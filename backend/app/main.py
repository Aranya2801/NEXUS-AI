"""Application factory. All collaborators are built here and hung on `app.state` (dependency injection)."""
from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.agents.deps import AgentDeps
from app.api import health, routes_admin, routes_agentic, routes_auth, routes_chat, routes_connectors, routes_evaluation, routes_feedback, routes_graph, routes_kb, routes_learning, routes_llmops, routes_memory, routes_security, routes_multimodal, routes_reports, routes_search
from app.agentic.agent import AutonomousAgent
from app.agentic.tools import ToolRegistry
from app.connectors.service import ConnectorService
from app.core.config import Settings, get_settings
from app.core.exceptions import NexusError, RateLimited
from app.core.logging import configure_logging, get_logger, request_id_var
from app.core.ratelimit import build_limiter
from app.database.session import create_all, make_engine, make_session_factory
from app.embeddings.factory import build_embedder
from app.observability.metrics import build_registry
from app.observability.tracing import setup_tracing
from app.ingestion.runner import JobRunner
from app.ingestion.service import IngestionService
from app.knowledge_graph.factory import build_extractor
from app.knowledge_graph.indexer import GraphIndexer
from app.knowledge_graph.retrieval import GraphRetriever
from app.llm.factory import build_llm
from app.distributed.bus import build_bus, wire_cache_invalidation
from app.distributed.lease import build_lease, hold
from app.learning.experiments import ExperimentService
from app.learning.monitor import QualityMonitor
from app.llmops.stack import build_stack
from app.multimodal.vision import build_vision
from app.memory.service import MemoryService
from app.reporting.service import ReportService
from app.security import auditsig
from app.reranking.factory import build_reranker
from app.retrieval.pipeline import RetrievalPipeline
from app.retrieval.service import HybridRetriever
from app.services.storage import build_storage
from app.workflows.research_graph import build_research_graph
from app.version import __version__
from app.workflows.runner import ResearchRunner

log = get_logger("nexus.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        if settings.env != "prod":  # prod schema is managed by Alembic migrations
            await create_all(engine)
        factory = make_session_factory(engine)
        embedder = build_embedder(settings)
        auditsig.set_key(settings.jwt_secret)
        llmstack = build_stack(settings, factory, app.state.metrics, builder=getattr(app.state, "llm_builder", None))
        llm, recorder, policies = llmstack.llm, llmstack.recorder, llmstack.policies
        app.state.llmops = {"recorder": recorder, "prompts": llmstack.prompts, "cache": llmstack.cache}
        app.state.policies = policies
        lease, bus = build_lease(settings.redis_url), build_bus(settings.redis_url)
        app.state.lease, app.state.bus = lease, bus
        experiments = ExperimentService(factory)
        app.state.experiments = experiments
        await wire_cache_invalidation(bus, policies, llmstack.prompts, experiments)
        monitor = QualityMonitor(factory, lease, settings.quality_monitor_interval_seconds, app.state.metrics)
        app.state.quality_monitor = monitor
        monitor.start()
        graph = GraphIndexer(build_extractor(settings, llm), settings)
        vision = build_vision(settings)
        service = IngestionService(factory, build_storage(settings), settings, embedder, graph, vision)
        app.state.vision = vision
        service.policies = policies
        service.metrics = app.state.metrics
        memory = MemoryService(settings)
        app.state.memory = memory
        runner = JobRunner(service, settings.ingestion_workers, settings.ingestion_retry_backoff_seconds, external=settings.ingestion_mode == "external")
        app.state.settings, app.state.engine, app.state.session_factory = settings, engine, factory
        app.state.ingestion, app.state.runner = service, runner
        app.state.retriever = HybridRetriever(embedder, settings)
        app.state.llm, app.state.graph, app.state.graph_retriever = llm, graph, GraphRetriever(settings)
        app.state.pipeline = RetrievalPipeline(app.state.retriever, llm, build_reranker(settings), settings, app.state.graph_retriever)
        await runner.start()
        app.state.connectors = ConnectorService(factory, service, runner, settings, getattr(app.state, "connector_client_factory", None), lease)
        await app.state.connectors.start()
        async with AsyncExitStack() as stack:  # LangGraph checkpointer lives as long as the app
            if settings.checkpoint_backend == "postgres":
                from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

                from app.database.session import psycopg_dsn

                saver = await stack.enter_async_context(AsyncPostgresSaver.from_conn_string(psycopg_dsn(settings.database_url)))
                await saver.setup()   # idempotent: creates the checkpoint tables on first boot
            else:
                from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

                if settings.checkpoint_path != ":memory:":
                    Path(settings.checkpoint_path).parent.mkdir(parents=True, exist_ok=True)
                saver = await stack.enter_async_context(AsyncSqliteSaver.from_conn_string(settings.checkpoint_path))
            deps = AgentDeps(factory, app.state.pipeline, app.state.retriever, app.state.graph_retriever, llm, settings)
            app.state.agent_deps = deps
            app.state.tool_registry = ToolRegistry(metrics=app.state.metrics)
            app.state.autonomous = AutonomousAgent(deps, app.state.tool_registry)
            app.state.memory = memory
            app.state.runner_agents = ResearchRunner(build_research_graph(deps, saver), factory, settings, saver, memory)
            app.state.runner_agents.policies, app.state.runner_agents.metrics = policies, app.state.metrics
            app.state.runner_agents.experiments = experiments

            async def purge_loop() -> None:  # expired memory (session / document history) is deleted, not just hidden
                while True:
                    await asyncio.sleep(settings.memory_purge_interval_seconds)
                    try:
                        async with hold(lease, "housekeeping:memory-purge", settings.memory_purge_interval_seconds) as h:
                            if not h.acquired:       # another replica is purging this interval
                                continue
                            async with factory() as db:
                                await memory.purge_expired(db)
                                await db.commit()
                    except Exception:  # noqa: BLE001
                        log.exception("memory purge failed")

            app.state.reports = ReportService(deps, memory)
            await app.state.reports.recover()
            async def queue_gauge_loop() -> None:     # autoscaling signal: how much ingestion work is waiting
                from sqlalchemy import func, select

                from app.database.models import IngestionJob

                while app.state.metrics is not None:
                    try:
                        async with factory() as db:
                            rows = dict((await db.execute(select(IngestionJob.status, func.count()).group_by(IngestionJob.status))).all())
                        for st in ("queued", "running", "dead"):
                            app.state.metrics.set("nexus_ingestion_queue_depth", rows.get(st, 0), st)
                    except Exception:  # noqa: BLE001
                        log.exception("queue gauge refresh failed")
                    await asyncio.sleep(settings.queue_gauge_seconds)

            purge = asyncio.create_task(purge_loop())
            gauge = asyncio.create_task(queue_gauge_loop())
            try:
                yield
            finally:
                purge.cancel()
                gauge.cancel()
                await asyncio.gather(purge, gauge, return_exceptions=True)
                await app.state.reports.stop()
        await app.state.connectors.stop()
        await monitor.stop()
        await llmstack.stop()
        await bus.stop()
        await lease.close()
        await runner.stop()
        if hasattr(app.state.limiter, "close"):
            await app.state.limiter.close()
        await engine.dispose()

    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan,
                  docs_url="/docs" if settings.env != "prod" else None, redoc_url=None)
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
                           allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["authorization", "content-type"])
    limiter = build_limiter(settings.redis_url)
    app.state.limiter = limiter
    app.state.metrics = build_registry() if settings.metrics_enabled else None
    app.state.tracing = setup_tracing(app, settings)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        request_id_var.set(rid)
        t0 = time.perf_counter()
        ip = request.client.host if request.client else "unknown"
        path = request.url.path
        if path == "/auth/login" and not await limiter.allow(f"login:{ip}", settings.login_rate_limit_per_minute):
            resp = JSONResponse({"error": {"code": RateLimited.code, "message": "Too many login attempts"}}, 429)
        elif not path.startswith("/health") and not await limiter.allow(f"api:{ip}", settings.rate_limit_per_minute):
            resp = JSONResponse({"error": {"code": RateLimited.code, "message": "Rate limit exceeded"}}, 429)
        else:
            resp = await call_next(request)
        reg = app.state.metrics
        if reg is not None:
            route = getattr(request.scope.get("route"), "path", None) or "unmatched"   # template, not the raw path (cardinality)
            reg.inc("nexus_http_requests_total", request.method, route, str(resp.status_code))
            reg.observe("nexus_http_request_duration_seconds", time.perf_counter() - t0, request.method, route)
        resp.headers.update({
            "X-Request-ID": rid, "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
        })
        return resp

    @app.exception_handler(NexusError)
    async def nexus_error(_: Request, exc: NexusError):
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
                            exc.status_code, headers=headers)

    app.include_router(health.router)
    app.include_router(routes_auth.router)
    app.include_router(routes_kb.router)
    app.include_router(routes_connectors.router)
    app.include_router(routes_search.router)
    app.include_router(routes_multimodal.router)
    app.include_router(routes_graph.router)
    app.include_router(routes_chat.router)
    app.include_router(routes_agentic.router)
    app.include_router(routes_memory.router)
    app.include_router(routes_reports.router)
    app.include_router(routes_admin.router)
    app.include_router(routes_feedback.router)
    app.include_router(routes_llmops.router)
    app.include_router(routes_security.router)
    app.include_router(routes_learning.router)
    app.include_router(routes_evaluation.router)
    return app


app = None  # `uvicorn app.main:create_app --factory`
