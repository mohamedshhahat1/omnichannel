"""ASGI entrypoint.

Local development::

    uvicorn app.main:app --reload

The application object is built at import time so that a misconfigured
environment fails immediately and loudly rather than on the first request.
"""

from app.api.application import create_app

app = create_app()
