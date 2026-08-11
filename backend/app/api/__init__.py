"""HTTP delivery layer.

The application factory, ASGI middleware, exception handlers, dependency
declarations and routers live here. This layer translates between HTTP and the
rest of the application; it holds no business rules (ENGINEERING.md 8.4).

`app.api` may import `app.core`, `app.platform` and - from Phase 3 - the public
service interfaces of `app.modules`. Nothing may import `app.api`.
"""

__all__: list[str] = []
