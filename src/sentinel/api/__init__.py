"""HTTP/JSON API for the v2 dashboard.

Exposes every accessor the bot already has — funds, portfolio, theses,
research desk, dossier, scorecard, etc. — as REST endpoints. The
SvelteKit frontend in `frontend/` consumes these via TanStack Query.

Backend stays Python; UI stays JS. Clean separation: no Python in the
browser, no JS-templating in Python. The hand-rolled HTML strings the
old NiceGUI surfaces emitted are now structured JSON; the new
frontend handles all presentation.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    calls,
    catalysts,
    copilot,
    events as _events,
    filings,
    health as _health,
    lookup,
    markets,
    news,
    overview,
    research,
    social,
    symbol,
    theses,
    wallets,
    watches,
)


router = APIRouter(prefix="/api", tags=["dashboard"])

# Mount sub-routers. Order doesn't matter beyond grouping in the auto-
# docs; keeping alphabetical so a new endpoint is easy to find later.
router.include_router(calls.router)
router.include_router(catalysts.router)
router.include_router(copilot.router)
router.include_router(_events.router)
router.include_router(filings.router)
router.include_router(_health.router)
router.include_router(lookup.router)
router.include_router(markets.router)
router.include_router(news.router)
router.include_router(overview.router)
router.include_router(research.router)
router.include_router(social.router)
router.include_router(symbol.router)
router.include_router(theses.router)
router.include_router(wallets.router)
router.include_router(watches.router)
