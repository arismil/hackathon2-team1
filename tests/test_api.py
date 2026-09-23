from uuid import UUID

from fastapi.testclient import TestClient

from hackathon2_team1.api import create_app
from hackathon2_team1.settings import Settings


def test_health_reports_process_liveness() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_app_factory_accepts_settings_without_starting_services() -> None:
    settings = Settings(environment="test")

    app = create_app(settings)

    assert app.state.settings is settings


def test_request_id_is_generated_by_server_for_each_request() -> None:
    with TestClient(create_app()) as client:
        first = client.get("/health", headers={"X-Request-ID": "caller-value"})
        second = client.get("/health", headers={"X-Request-ID": "caller-value"})

    first_id = first.headers["X-Request-ID"]
    second_id = second.headers["X-Request-ID"]
    assert str(UUID(first_id)) == first_id
    assert first_id != "caller-value"
    assert second_id != first_id


def test_missing_route_has_safe_public_error() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/missing")

    assert response.status_code == 404
    assert response.json() == {
        "code": "not_found",
        "message": "Not found",
        "request_id": response.headers["X-Request-ID"],
    }


def test_unhandled_error_does_not_expose_exception_details() -> None:
    app = create_app()

    @app.get("/fails")
    def fails() -> None:
        raise RuntimeError("private implementation detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/fails")

    assert response.status_code == 500
    assert response.json() == {
        "code": "internal_error",
        "message": "Internal server error",
        "request_id": response.headers["X-Request-ID"],
    }
    assert "private implementation detail" not in response.text


def test_validation_error_does_not_reflect_input() -> None:
    app = create_app()

    @app.get("/number/{value}")
    def number(value: int) -> dict[str, int]:
        return {"value": value}

    with TestClient(app) as client:
        response = client.get("/number/sensitive-input")

    assert response.status_code == 422
    assert response.json() == {
        "code": "invalid_request",
        "message": "Invalid request",
        "request_id": response.headers["X-Request-ID"],
    }
    assert "sensitive-input" not in response.text
