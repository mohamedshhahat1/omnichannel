"""Error responses: shape, HTTP semantics and information disclosure."""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from app.api.application import create_app
from app.core.errors import ConflictError
from app.core.settings import ServerSettings
from tests.conftest import build_settings

SECRET_VALUE = "sup3r-s3cret-password"


class Credentials(BaseModel):
    email: str
    password: str


def app_with_failing_routes(app: FastAPI) -> FastAPI:
    @app.get("/_test/conflict")
    async def conflict() -> None:
        raise ConflictError(
            "That channel is already connected.",
            code="channel_already_connected",
            details={"channel": "whatsapp"},
            internal_message="tenant_id=42 duplicate channel row",
        )

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("database password is hunter2")

    @app.post("/_test/credentials")
    async def credentials(payload: Credentials) -> dict[str, str]:
        return {"email": payload.email}

    return app


def test_unknown_route_returns_the_standard_envelope(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"]
    assert error["request_id"]
    assert error["correlation_id"]


def test_wrong_method_returns_405(client: TestClient) -> None:
    response = client.post("/health/live")

    assert response.status_code == 405
    assert response.json()["error"]["code"] == "method_not_allowed"


def test_application_errors_map_to_their_status_and_code(app: FastAPI) -> None:
    with TestClient(app_with_failing_routes(app), raise_server_exceptions=False) as test_client:
        response = test_client.get("/_test/conflict")

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "channel_already_connected"
    assert error["message"] == "That channel is already connected."
    assert error["details"] == {"channel": "whatsapp"}


def test_internal_message_is_never_serialised(app: FastAPI) -> None:
    with TestClient(app_with_failing_routes(app), raise_server_exceptions=False) as test_client:
        body = test_client.get("/_test/conflict").text

    assert "tenant_id=42" not in body


def test_unhandled_exceptions_become_an_opaque_500(app: FastAPI) -> None:
    with TestClient(app_with_failing_routes(app), raise_server_exceptions=False) as test_client:
        response = test_client.get("/_test/crash")

    assert response.status_code == 500
    error = response.json()["error"]
    assert error["code"] == "internal_error"
    assert error["request_id"]

    body = response.text
    assert "hunter2" not in body
    assert "RuntimeError" not in body
    assert "Traceback" not in body


def test_validation_errors_do_not_echo_submitted_values(app: FastAPI) -> None:
    with TestClient(app_with_failing_routes(app), raise_server_exceptions=False) as test_client:
        response = test_client.post(
            "/_test/credentials",
            json={"password": SECRET_VALUE},
        )

    assert response.status_code == 422
    body = response.text
    assert SECRET_VALUE not in body

    error = response.json()["error"]
    assert error["code"] == "unprocessable_entity"
    field = error["details"]["fields"][0]
    assert field["location"] == ["body", "email"]
    assert "input" not in field


def test_error_responses_carry_the_response_correlation_headers(client: TestClient) -> None:
    response = client.get("/does-not-exist")

    assert response.headers["X-Request-ID"] == response.json()["error"]["request_id"]


def test_oversized_bodies_are_rejected_with_413() -> None:
    settings = build_settings(server=ServerSettings(max_request_body_bytes=1024))
    with TestClient(create_app(settings), raise_server_exceptions=False) as test_client:
        response = test_client.post("/_test/credentials", content=b"x" * 2048)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
