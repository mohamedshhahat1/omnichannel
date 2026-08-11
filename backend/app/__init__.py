"""Omnichannel backend application package.

Layout (see `docs/architecture.md`):

- `app.core`     framework wiring: settings, logging, observability, errors
- `app.platform` shared kernel: correlation context, health registry
- `app.modules`  business domain modules (added from Phase 3 onward)
- `app.api`      HTTP delivery layer: application factory, middleware, routes
"""

__all__: list[str] = []
