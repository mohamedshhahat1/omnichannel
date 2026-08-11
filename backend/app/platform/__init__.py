"""Shared kernel.

Small, dependency-free building blocks that every module may use: correlation
context and the health check registry today; tenant context, pagination, ID
generation, audit helpers and rate limiting in later phases.

Nothing here may import from `app.modules` or `app.api`. Nothing here may
depend on FastAPI. That keeps these primitives usable from Celery workers and
CLI entrypoints, and makes them testable without an HTTP layer.

Note: this package shadows the standard library `platform` module by name only.
Python 3 uses absolute imports, and relative imports are banned by the Ruff
configuration, so `import platform` anywhere in the codebase still resolves to
the standard library.
"""

__all__: list[str] = []
