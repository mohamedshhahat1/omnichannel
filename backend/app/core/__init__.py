"""Framework and process wiring.

Settings, logging, observability and the error taxonomy. This package knows how
to start and configure the process; it holds no business rules.

`app.core` may import `app.platform`. It must never import `app.modules`.
"""

__all__: list[str] = []
