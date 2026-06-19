"""
aethviondb/server.py
Standalone HTTP server for AethvionDB — the web explorer + versioned /api/v1 API.

Run:
    aethviondb-server                       # starts server, opens the explorer
    uvicorn aethviondb.server:app --reload   # dev, against a repo checkout

The API is open by default; configure per-database API keys via the
/api/v1/{db}/keys endpoints to require authentication.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from aethviondb import __version__
from aethviondb.api_v1.router import router as v1_router
from aethviondb.importers.api import router as import_router
from aethviondb.config import DATA_DIR

_WEB = Path(__file__).parent / "web"


def create_app() -> FastAPI:
    app = FastAPI(
        title="AethvionDB",
        version=__version__,
        description="Agent-first knowledge database — typed entity graph over /api/v1.",
    )
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
