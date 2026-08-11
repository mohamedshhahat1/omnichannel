"""Request and correlation identifiers over HTTP."""

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.api.application import create_app
from app.api.dependencies import RequestContextDep
from app.core.settings import ServerSettings
from app.platform.correlation import is_valid_external_id
from tests.conftest import build_settings

REQUEST_HEADER = "X-Request-ID"
CORRELATION_HEADER = "X-Correlation-ID"


def test_every_response_carries_generated_identifiers(client: TestClient) -> None:
    response = client.get("/health/live")

    request_id = response.headers[REQUEST_HEADER]
    correlation_id = response.headers[CORRELATION_HEADER]

    assert is_valid_external_id(request_id)
    assert correlation_id == request_id


def test_identifiers_differ_between_requests(client: TestClient) -> None:
    first = client.get("/health/live").headers[REQUEST_HEADER]
    second = client.get("/health/live").headers[REQUEST_HEADER]

    assert first != second


def test_inbound_request_id_is_ignored_by_default(client: TestClient) -> None:
    response = client.get("/health/live", headers={REQUEST_HEADER: "client-chosen-value"})

    assert response.headers[REQUEST_HEADER] != "client-chosen-value"


def test_inbound_request_id_is_honoured_when_the_proxy_is_trusted() -> None:
    settings = build_settings(server=ServerSettings(trust_inbound_request_id=True))
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        response = test_client.get(
            "/health/live",
            headers={REQUEST_HEADER: "proxy-generated-value"},
        )

    assert response.headers[REQUEST_HEADER] == "proxy-generated-value"


def test_inbound_correlation_id_is_honoured(client: TestClient) -> None:
    response = client.get("/health/live", headers={CORRELATION_HEADER: "workflow-abc-123"})

    assert response.headers[CORRELATION_HEADER] == "workflow-abc-123"
    assert response.headers[REQUEST_HEADER] != "workflow-abc-123"


def test_malformed_inbound_correlation_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health/live", headers={CORRELATION_HEADER: "not a valid id"})

    correlation_id = response.headers[CORRELATION_HEADER]
    assert correlation_id != "not a valid id"
    assert is_valid_external_id(correlation_id)


def test_overlong_inbound_correlation_id_is_replaced(client: TestClient) -> None:
    response = client.get("/health/live", headers={CORRELATION_HEADER: "a" * 500})

    assert len(response.headers[CORRELATION_HEADER]) < 200


def test_identifiers_are_available_to_handlers(app: FastAPI) -> None:
    @app.get("/_test/context")
    async def context(
        request: Request, request_context: RequestContextDep
    ) -> dict[str, str | None]:
        return {
            "from_dependency": request_context.request_id,
            "from_state": request.state.request_id,
            "correlation_id": request_context.correlation_id,
        }

    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/_test/context")

    body = response.json()
    assert body["from_dependency"] == response.headers[REQUEST_HEADER]
    assert body["from_state"] == response.headers[REQUEST_HEADER]
    assert body["correlation_id"] == response.headers[CORRELATION_HEADER]
