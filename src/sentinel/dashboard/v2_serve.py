"""Serve the SvelteKit build at `/app/*`.

The frontend in `frontend/` builds to `frontend/build/` via
`pnpm build` on the dev box. We commit the build artifacts to git so
the Pi gets them automatically with `git pull` (no node install needed
on the Pi). FastAPI's `StaticFiles` serves the bundle.

SPA fallback: every URL under `/app/*` returns `index.html` when no
file matches, so client-side routing (`/app/markets`, `/app/theses`)
works on refresh. Static assets in `_app/` still serve as files.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

# `<repo>/frontend/build` is two levels up from this file
# (src/sentinel/dashboard/v2_serve.py).
_FRONTEND_BUILD = Path(__file__).resolve().parents[3] / "frontend" / "build"


def attach_v2(app: FastAPI) -> None:
    """Wire the SvelteKit SPA into the existing FastAPI app.

    No-op (with a warning) when the build artifacts aren't present —
    e.g. dev box hasn't run `pnpm build` yet. Bot keeps booting; v2
    just isn't served. NiceGUI continues to handle `/`.
    """
    if not _FRONTEND_BUILD.exists():
        logger.warning(
            "frontend/build/ not found ({}); v2 dashboard not served. "
            "Run `cd frontend && pnpm install && pnpm build`.",
            _FRONTEND_BUILD,
        )
        return

    # The SvelteKit static adapter emits `index.html` + `_app/` + any
    # routes/*.html. We mount the *whole* build dir as static at /app
    # so a request like /app/_app/immutable/foo.js resolves directly
    # to frontend/build/_app/immutable/foo.js.
    app.mount(
        "/app/_app",
        StaticFiles(directory=_FRONTEND_BUILD / "_app"),
        name="v2_app_static",
    )
    # Top-level static assets (favicon, fonts, images shipped via
    # `frontend/static/`)
    favicon = _FRONTEND_BUILD / "favicon.png"
    if favicon.exists():
        @app.get("/app/favicon.png", include_in_schema=False)
        def _favicon():
            return FileResponse(favicon)

    # Index + SPA fallback. Any other /app/* path serves index.html
    # so the SPA's client-side router takes over.
    index_html = _FRONTEND_BUILD / "index.html"
    if not index_html.exists():
        logger.warning(
            "frontend/build/index.html missing — build is incomplete; "
            "expected an adapter-static output. Skipping SPA mount."
        )
        return

    @app.get("/app", include_in_schema=False)
    def _app_root():
        return FileResponse(index_html)

    @app.get("/app/{path:path}", include_in_schema=False)
    def _app_spa(path: str):
        # If a real file matches under build/ (e.g. a generated route
        # like /app/markets/index.html), serve it. Otherwise fall back
        # to index.html and let the SPA route.
        candidate = _FRONTEND_BUILD / path
        if candidate.is_file():
            return FileResponse(candidate)
        # Try `path/index.html` for prerendered routes
        candidate_index = _FRONTEND_BUILD / path / "index.html"
        if candidate_index.is_file():
            return FileResponse(candidate_index)
        return FileResponse(index_html)

    # Convenience: `/` could later redirect to `/app` once we're ready
    # to swap. For now NiceGUI keeps `/` — we'll flip when v2 is
    # complete. Leaving this comment as a marker.
    _ = RedirectResponse  # silence "unused import" until the swap
    _ = HTTPException
