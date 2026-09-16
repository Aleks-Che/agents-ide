import asyncio
import contextlib
import json
import logging
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException

from agents_ide import __version__
from agents_ide.api.domain import router as domain_router
from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.persistence.database import check_database, create_database, migrate
from agents_ide.security.auth import COOKIE_NAME, AuthService, Session
from agents_ide.security.filesystem import prepare_data_dir
from agents_ide.security.secrets import SecretStore
from agents_ide.worker.main import worker_status


class PairRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)


def error_response(error: AppError, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=error.status,
        content={
            "code": error.code,
            "message": error.message,
            "details": error.details,
            "request_id": request_id,
            "retryable": error.retryable,
        },
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        prepare_data_dir(settings.data_dir)
        await asyncio.to_thread(migrate, settings)
        engine = create_database(settings)
        auth = AuthService(settings, engine)
        app.state.engine = engine
        app.state.auth = auth
        app.state.settings = settings
        app.state.session_factory = sessionmaker(
            bind=engine, expire_on_commit=False, autoflush=False
        )
        app.state.secrets = SecretStore(settings.data_dir / "secrets")
        from agents_ide.services.presets import install_all

        def install_presets() -> None:
            with app.state.session_factory() as session:
                install_all(session)
                session.commit()

        await asyncio.to_thread(install_presets)
        from agents_ide.engine.events_stream import StreamHub

        app.state.run_streams = StreamHub(app.state.session_factory)
        await asyncio.to_thread(auth.issue_code)

        async def maintenance() -> None:
            while True:
                await asyncio.sleep(1)
                try:
                    await asyncio.to_thread(auth.cleanup)
                except Exception:
                    logging.error("auth.maintenance_failed")

        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            await app.state.run_streams.close()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            engine.dispose()

    app = FastAPI(
        title="Agents IDE",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.middleware("http")
    async def perimeter(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = uuid.uuid4().hex
        request.state.request_id = request_id
        started = time.monotonic()
        try:
            hosts = request.headers.getlist("host")
            if hosts != [f"{settings.host}:{settings.port}"]:
                raise AppError("host_invalid", "Недопустимый Host", 400)
            origins = request.headers.getlist("origin")
            if len(origins) > 1 or (origins and origins[0] not in settings.allowed_origins):
                raise AppError("origin_invalid", "Недопустимый Origin", 403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise AppError("origin_invalid", "Межсайтовый запрос запрещён", 403)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and not origins:
                raise AppError("origin_invalid", "Для изменения данных требуется Origin", 403)
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                from agents_ide.operations.maintenance import ensure_available

                ensure_available(settings)
            response = await call_next(request)
        except AppError as error:
            response = error_response(error, request_id)
        except Exception:
            logging.exception("request.failed", extra={"request_id": request_id})
            response = error_response(
                AppError("internal_error", "Внутренняя ошибка", 500), request_id
            )
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        logging.info(
            "request.finished",
            extra={
                "request_id": request_id,
                "status": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, error: AppError) -> JSONResponse:
        return error_response(error, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        # ValidationError.input can contain the pairing code or provider credentials.
        return error_response(
            AppError(
                "validation_error",
                "Неверный формат запроса",
                422,
                {"fields": [list(item["loc"]) for item in error.errors()]},
            ),
            request.state.request_id,
        )

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, error: HTTPException) -> JSONResponse:
        return error_response(
            AppError("http_error", "Запрос не может быть выполнен", error.status_code),
            request.state.request_id,
        )

    def current_session(request: Request) -> Session:
        session: Session = app.state.auth.authenticate(request.cookies.get(COOKIE_NAME))
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            csrf = request.headers.get("x-csrf-token", "")
            if not secrets.compare_digest(csrf, session.csrf_token):
                raise AppError("csrf_invalid", "Недопустимый CSRF-токен", 403)
        return session

    app.include_router(domain_router, dependencies=[Depends(current_session)])

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/auth/pair")
    def pair(body: PairRequest) -> Response:
        token, session = app.state.auth.pair(body.code)
        response = JSONResponse(
            {"csrf_token": session.csrf_token, "expires_at": session.expires_at}
        )
        response.set_cookie(
            COOKIE_NAME,
            token,
            max_age=settings.session_seconds,
            httponly=True,
            samesite="strict",
            secure=False,
            path="/api",
        )
        return response

    @app.get("/api/auth/session")
    def session_info(session: Annotated[Session, Depends(current_session)]) -> dict[str, Any]:
        return {"csrf_token": session.csrf_token, "expires_at": session.expires_at}

    @app.post("/api/auth/logout")
    def logout(session: Annotated[Session, Depends(current_session)]) -> Response:
        app.state.auth.revoke(session)
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/api", httponly=True, samesite="strict")
        return response

    def readiness_data() -> dict[str, Any]:
        try:
            database_ok = check_database(app.state.engine)
            worker = worker_status(app.state.engine, settings)
        except SQLAlchemyError:
            database_ok = False
            worker = {"status": "unknown", "last_seen_at": None}
        return {
            "api": "ready",
            "database": "ready" if database_ok else "unavailable",
            "worker": worker,
            "version": __version__,
            "ready": database_ok and worker["status"] == "running",
        }

    @app.get("/api/readiness", dependencies=[Depends(current_session)])
    def readiness() -> Response:
        state = readiness_data()
        if not state["ready"]:
            raise AppError("service_unavailable", "Исполнитель ещё не готов", 503, state, True)
        return JSONResponse(state)

    @app.get("/api/system/status", dependencies=[Depends(current_session)])
    def status() -> dict[str, Any]:
        return readiness_data()

    @app.get("/api/system/events")
    async def events(
        request: Request, session: Annotated[Session, Depends(current_session)]
    ) -> StreamingResponse:
        token = request.cookies.get(COOKIE_NAME)

        async def stream() -> AsyncIterator[str]:
            while not await request.is_disconnected():
                try:
                    await asyncio.to_thread(app.state.auth.authenticate, token)
                except AppError:
                    yield "event: auth.expired\ndata: {}\n\n"
                    return
                state = await asyncio.to_thread(readiness_data)
                yield f"event: system.status\ndata: {json.dumps(state)}\n\n"
                await asyncio.sleep(1)

        # Snapshots only; durable Run event replay belongs to stage 4.
        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.get("/api/openapi.json", dependencies=[Depends(current_session)])
    def schema() -> dict[str, Any]:
        return app.openapi()

    @app.get("/{path:path}")
    def frontend(path: str) -> Response:
        if path == "api" or path.startswith("api/"):
            raise AppError("not_found", "API route not found", 404)
        root = settings.frontend_dir.resolve()
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root):
            raise AppError("not_found", "Not found", 404)
        if candidate.is_file():
            return FileResponse(candidate)
        if Path(path).suffix:
            raise AppError("not_found", "Not found", 404)
        if (root / "index.html").is_file():
            return FileResponse(root / "index.html")
        raise AppError("frontend_unavailable", "Соберите frontend: npm run build", 503)

    return app
