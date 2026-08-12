"""Version 1 of the public API.

The router exists so that versioning is a decision already made rather than a
refactor waiting to happen. Business routers are included here as the phases
that own them land; Phase 3 adds identity.

The application factory mounts this router under `settings.api_prefix`, so the
effective base path is `/api/v1`.
"""

from fastapi import APIRouter

from app.modules.identity.api import router as identity_router

router = APIRouter(prefix="/v1")

router.include_router(identity_router)

__all__ = ["router"]
