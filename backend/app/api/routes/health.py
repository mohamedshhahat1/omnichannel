"""Operational health endpoints.

Liveness answers "is this process broken?" - if it fails, restarting helps.
Readiness answers "should this process receive traffic?" - if it fails,
restarting does not help, because the process is fine and a dependency is not.
Conflating the two is how a database incident turns into a restart loop.

Liveness therefore checks nothing. It returns 200 for as long as the event loop
can serve a request. Readiness consults the health registry, which is empty in
Phase 1 and gains PostgreSQL and Redis checks in Phase 2 without this module
changing.

Both are excluded from tracing by default: probes fire constantly and would
otherwise dominate trace volume.
"""

from fastapi import APIRouter, Request, Response

from app.api.dependencies import HealthRegistryDep, SettingsDep
from app.api.schemas import HealthCheckResult, LivenessResponse, ReadinessResponse
from app.platform.health import HealthStatus

router = APIRouter(prefix="/health", tags=["health"])


@router.get(
    "/live",
    response_model=LivenessResponse,
    summary="Liveness probe",
)
async def liveness(settings: SettingsDep) -> LivenessResponse:
    """Report that the process is running and able to serve requests."""
    return LivenessResponse(
        service=settings.service_name,
        version=settings.service_version,
        environment=settings.environment.value,
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    summary="Readiness probe",
    responses={
        503: {"model": ReadinessResponse, "description": "Not ready to serve traffic."},
    },
)
async def readiness(
    request: Request,
    response: Response,
    settings: SettingsDep,
    registry: HealthRegistryDep,
) -> ReadinessResponse:
    """Report whether the process should receive traffic.

    Returns 503 with the same body shape when not ready, so a probe can rely on
    the status code and an operator can still read which check failed.
    """
    started = bool(getattr(request.app.state, "started", False))
    report = await registry.run()
    ready = started and report.status is HealthStatus.PASS

    if not ready:
        response.status_code = 503

    return ReadinessResponse(
        status="ready" if ready else "not_ready",
        service=settings.service_name,
        version=settings.service_version,
        environment=settings.environment.value,
        checks=[
            HealthCheckResult(
                name=outcome.name,
                status="pass" if outcome.status is HealthStatus.PASS else "fail",
                critical=outcome.critical,
                duration_ms=outcome.duration_ms,
                detail=outcome.detail,
            )
            for outcome in report.outcomes
        ],
    )
