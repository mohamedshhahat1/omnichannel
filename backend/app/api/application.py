"""Application factory.

`create_app` builds a fully configured application from an explicit settings
object. Building rather than importing a module-level singleton is what lets
the test suite construct several applications with different configurations in
one process, and it keeps startup order explicit.

Order matters:

1. Logging first, so everything after it is observable.
2. Tracing second, so instrumentation can attach to the application below.
3. Then the application object, its state, middleware, handlers and routers.

Middleware ordering is the part that is easy to get wrong. Starlette applies
middleware in reverse registration order, so the last registered is the
outermost. TrustedHost is registered last and is therefore outermost, which is
correct: a forged Host header should be rejected before anything else looks at
the request. Correlation sits inside CORS but outside the routers, so every log
line emitted while handling the request - including one emitted by an exception
handler - carries the identifiers.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api.exception_handlers import register_exception_handlers
from app.api.middleware import (
    CorrelationMiddleware,
    MaxBodySizeMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.routes import health
from app.api.routes.v1 import router as v1_router
from app.core.infrastructure import start_infrastructure, stop_infrastructure
from app.core.logging import configure_logging
from app.core.observability import (
    configure_tracing,
    instrument_fastapi,
    instrument_infrastructure,
    shutdown_tracing,
)
from app.core.settings import Settings, get_settings
from app.platform.health import HealthRegistry

logger = logging.getLogger(__name__)

_DESCRIPTION = (
    "Multi-tenant platform for AI-powered customer conversations across "
    "WhatsApp, Instagram, Facebook Messenger and social comments."
)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Run startup and shutdown work around the serving period.

    Phase 2 opens the database engine and the Redis pool here and registers
    their health checks. Anything acquired here must be released after the
    `yield`, including when startup itself fails part way through.
    """
    settings: Settings = app.state.settings

    # Preserve hermetic API/integration tests. Real PostgreSQL/Redis checks are
    # opt-in and use explicit OC_TEST_* URLs in the dedicated integration suite.
    if settings.is_test:
        logger.info(
            "application.startup",
            extra={
                "environment": settings.environment.value,
                "version": settings.service_version,
                "tracing_enabled": settings.observability.tracing_enabled,
            },
        )
        app.state.started = True
        try:
            yield
        finally:
            app.state.started = False
            shutdown_tracing(app.state.tracer_provider)
            logger.info("application.shutdown")
        return

    infrastructure = await start_infrastructure(settings, app.state.health_registry)
    app.state.infrastructure = infrastructure
    app.state.database_engine = infrastructure.database_engine
    app.state.session_factory = infrastructure.session_factory
    app.state.redis = infrastructure.redis
    instrument_infrastructure(infrastructure.database_engine, infrastructure.redis, settings)

    logger.info(
        "application.startup",
        extra={
            "environment": settings.environment.value,
            "version": settings.service_version,
            "tracing_enabled": settings.observability.tracing_enabled,
        },
    )
    app.state.started = True
    try:
        yield
    finally:
        app.state.started = False
        await stop_infrastructure(infrastructure)
        shutdown_tracing(app.state.tracer_provider)
        logger.info("application.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()

    configure_logging(settings)
    tracer_provider = configure_tracing(settings)

    app = FastAPI(
        title="Omnichannel API",
        description=_DESCRIPTION,
        version=settings.service_version,
        root_path=settings.server.root_path,
        lifespan=lifespan,
        # The schema describes every endpoint and every model. That is a map
        # for an attacker, so it is not served in production.
        docs_url=None if settings.is_production else "/docs",
        openapi_url=None if settings.is_production else "/openapi.json",
        redoc_url=None,
    )

    app.state.settings = settings
    app.state.health_registry = HealthRegistry()
    app.state.tracer_provider = tracer_provider
    app.state.started = False
    app.state.infrastructure = None

    _register_middleware(app, settings)
    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(v1_router, prefix=settings.api_prefix)

    instrument_fastapi(app, settings)
    return app


def _register_middleware(app: FastAPI, settings: Settings) -> None:
    """Register middleware. Registered first means innermost."""
    app.add_middleware(
        MaxBodySizeMiddleware,
        max_bytes=settings.server.max_request_body_bytes,
    )
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(CorrelationMiddleware, settings=settings)

    # Added only when configured. An empty origin list means no CORS layer at
    # all, which is the correct default for an API with no browser clients yet.
    if settings.security.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.security.cors_origins),
            allow_credentials=settings.security.cors_allow_credentials,
            allow_methods=list(settings.security.cors_allow_methods),
            allow_headers=list(settings.security.cors_allow_headers),
            expose_headers=[
                settings.server.request_id_header,
                settings.server.correlation_id_header,
            ],
        )

    if settings.security.trusted_hosts:
        app.add_middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(settings.security.trusted_hosts),
        )
