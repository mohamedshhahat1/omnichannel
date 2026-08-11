"""Identity use cases.

Enforcement lives in this package rather than in route decorators. A Celery
task, a management command and an HTTP request all reach the same service, so
they all get the same checks - a permission test attached to a router would
only protect the one caller that happens to arrive over HTTP.
"""
