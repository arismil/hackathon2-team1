"""Small ASGI boundary; product routes can be added after the final brief."""

from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from hackathon2_team1.api_errors import PublicApiError
from hackathon2_team1.settings import Settings


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


def create_app(settings: Settings | None = None) -> FastAPI:
    app = FastAPI(title="hackathon2-team1")
    app.state.settings = settings if settings is not None else Settings.from_environment()

    def public_error(request: Request, status_code: int, code: str, message: str) -> JSONResponse:
        error = PublicApiError(code=code, message=message, request_id=request.state.request_id)
        return JSONResponse(status_code=status_code, content=error.model_dump())

    @app.middleware("http")
    async def add_request_id(request: Request, call_next):
        # Incoming headers are untrusted; each request gets a server-generated ID.
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            response = public_error(request, 500, "internal_error", "Internal server error")
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, _exc: RequestValidationError) -> JSONResponse:
        return public_error(request, 422, "invalid_request", "Invalid request")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if exc.status_code == 404:
            return public_error(request, 404, "not_found", "Not found")
        if exc.status_code == 405:
            return public_error(request, 405, "method_not_allowed", "Method not allowed")
        return public_error(request, exc.status_code, "http_error", "Request could not be completed")

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    return app


app = create_app()
