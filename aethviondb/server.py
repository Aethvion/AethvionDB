"""
aethviondb/server.py
Standalone HTTP server for AethvionDB — the web explorer + versioned /api/v1 API.

Run:
    aethviondb-server                       # starts server, opens the explorer
    uvicorn aethviondb.server:app --reload   # dev, against a repo checkout

Trust model (local-first)
-------------------------
The server binds to 127.0.0.1 by default and is open by default — configure
per-database API keys via /api/v1/{db}/keys to require auth for writes. The
import endpoints intentionally read files from a server-side path and can open a
native folder picker on the host; this is safe for local single-user use but
means you should NOT expose this server to an untrusted network without the
(future) commercial security layer in front of it. Responses use a consistent
envelope: success is {ok:true, data, meta}; errors are {ok:false, error, meta}.
Request bodies are capped (AETHVIONDB_MAX_BODY_BYTES, default 64 MB).
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from aethviondb import __version__
from aethviondb._utils import get_logger
from aethviondb.api_v1.router import router as v1_router
from aethviondb.importers.api import router as import_router
from aethviondb.config import DATA_DIR

logger = get_logger(__name__)
_WEB = Path(__file__).parent / "web"

# Default max request body. Override with AETHVIONDB_MAX_BODY_BYTES. Imports read
# files from a server path (not request bodies), so this only bounds JSON payloads.
_DEFAULT_MAX_BODY = 64 * 1024 * 1024  # 64 MB

_STATUS_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden",
                 404: "not_found", 409: "conflict", 413: "payload_too_large",
                 422: "validation_error", 429: "rate_limited", 500: "internal_error"}


def _error_body(status: int, message: str, code: str | None = None, extra: dict | None = None) -> dict:
    """The error counterpart of the success envelope: {ok:false, error{...}, ...}.

    ``detail`` is kept alongside for backward compatibility with clients that
    read FastAPI's default error shape.
    """
    err = {"code": code or _STATUS_CODES.get(status, "error"), "message": message}
    if extra:
        err.update(extra)
    return {"ok": False, "error": err, "detail": extra if extra else message,
            "meta": {"version": "v1"}}


def _max_body_bytes() -> int:
    try:
        return int(os.environ.get("AETHVIONDB_MAX_BODY_BYTES", _DEFAULT_MAX_BODY))
    except ValueError:
        return _DEFAULT_MAX_BODY


def create_app() -> FastAPI:
    app = FastAPI(
        title="AethvionDB",
        version=__version__,
        description="Agent-first knowledge database — typed entity graph over /api/v1.",
    )

    # ── Consistent error envelope (4xx/5xx mirror the success {ok,data,meta}) ──
    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict):
            # Structured detail (e.g. the 409 version-conflict payload): preserve it.
            body = _error_body(exc.status_code, detail.get("message", "Request failed"),
                               code=detail.get("error"), extra=detail)
        else:
            body = _error_body(exc.status_code, str(detail))
        return JSONResponse(status_code=exc.status_code, content=body,
                            headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content=_error_body(422, "Request validation failed",
                                extra={"errors": jsonable_encoder(exc.errors())}),
        )

    @app.exception_handler(Exception)
    async def _unhandled_exc(request: Request, exc: Exception):
        logger.exception("[API] Unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(status_code=500, content=_error_body(500, "Internal server error"))

    # ── Request-size guard ──
    @app.middleware("http")
    async def _limit_body(request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > _max_body_bytes():
                    return JSONResponse(status_code=413, content=_error_body(
                        413, f"Request body exceeds the {_max_body_bytes()}-byte limit."))
            except ValueError:
                pass
        return await call_next(request)

    app.include_router(v1_router)
    app.include_router(import_router)

    @app.get("/health", tags=["meta"])
    async def health():
        return {"status": "ok", "service": "aethviondb",
                "version": __version__, "data_dir": str(DATA_DIR)}

    @app.get("/", include_in_schema=False)
    async def index():
        """Serve the web explorer (falls back to JSON info if it's missing)."""
        html = _WEB / "index.html"
        if html.exists():
            return FileResponse(html)
        return {"service": "AethvionDB", "version": __version__,
                "docs": "/docs", "api": "/api/v1"}

    return app


app = create_app()


def main() -> None:
    import uvicorn
    host = os.environ.get("AETHVIONDB_HOST", "127.0.0.1")
    port = int(os.environ.get("AETHVIONDB_PORT", "7475"))

    if os.environ.get("AETHVIONDB_OPEN_BROWSER", "1") == "1":
        import threading
        import time
        import webbrowser

        def _open():
            time.sleep(1.5)
            webbrowser.open(f"http://{host}:{port}/")

        threading.Thread(target=_open, daemon=True).start()

    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
