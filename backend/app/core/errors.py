"""Ошибки API в формате T04: {"code": ..., "message": ...}."""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.contracts.t04 import ErrorCode

_CODE_BY_STATUS: dict[int, ErrorCode] = {
    401: "unauthenticated",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
    409: "key_conflict",
    422: "invalid_input",
    429: "rate_limited",
}


class ApiError(Exception):
    def __init__(self, status: int, code: ErrorCode, message: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def error_response(status: int, code: ErrorCode, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status, headers=headers)


def unauthenticated() -> ApiError:
    return ApiError(401, "unauthenticated", "Требуется вход.")


def forbidden_manager() -> ApiError:
    return ApiError(403, "forbidden", "Требуется роль оператора.")


def not_found(message: str = "Объект не найден.") -> ApiError:
    return ApiError(404, "not_found", message)


def invalid_input(message: str) -> ApiError:
    return ApiError(422, "invalid_input", message)


def _validation_message(exc: RequestValidationError) -> str:
    parts = []
    for err in exc.errors()[:3]:
        where = ".".join(str(p) for p in err.get("loc", ()) if p not in ("body",))
        parts.append(f"{where}: {err.get('msg')}" if where else str(err.get("msg")))
    return "; ".join(parts) or "Некорректные входные данные."


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return error_response(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        return error_response(422, "invalid_input", _validation_message(exc)[:8000])

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _CODE_BY_STATUS.get(exc.status_code, "invalid_input")
        message = exc.detail if isinstance(exc.detail, str) and exc.detail else "Ошибка запроса."
        if exc.status_code == 404 and message == "Not Found":
            message = "Объект не найден."
        return error_response(exc.status_code, code, message, getattr(exc, "headers", None))
