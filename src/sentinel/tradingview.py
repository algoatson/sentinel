"""TradingView bridge.

There is *no* supported way to push our paper positions or P&L into a
personal TradingView account — TradingView's broker/trading API is a
partner-only program for integrated brokers, and webhooks only flow *out*
of TradingView (alerts → us), never in. So the feasible, genuinely useful
hook is read-side: turn a position's ticker into the correct
exchange-qualified TradingView symbol, a one-click chart deep-link, and a
copy-pasteable importable watchlist of the live book.

Symbol mapping mirrors the asset classes the watchlist already tracks:
  equity   AAPL / BRK.B   → AAPL / BRK.B          (TV auto-resolves exchange)
  crypto   BTC-USD        → CRYPTO:BTCUSD
  futures  ES=F           → ES1!                  (continuous front month)
  index    ^TNX / ^VIX    → TVC:TNX / TVC:VIX
"""

from __future__ import annotations

from urllib.parse import quote

from .routing import asset_class_of

_CHART = "https://www.tradingview.com/chart/?symbol="


def tv_symbol(ticker: str, asset_class: str | None = None) -> str:
    """Best-effort exchange-qualified TradingView symbol for a ticker.

    Falls back to the bare symbol (TradingView resolves most equities
    unprefixed) rather than guessing a wrong exchange.
    """
    if not ticker:
        return ""
    t = ticker.strip().upper()
    cls = asset_class or asset_class_of(t)

    if t.startswith("^"):
        return f"TVC:{t[1:]}"
    if t.endswith("=F"):
        return f"{t[:-2]}1!"
    if cls == "crypto" or t.endswith(("-USD", "-USDT", "-USDC")):
        base = t.split("-")[0]
        return f"CRYPTO:{base}USD"
    return t  # equity / unknown — let TradingView resolve it


def chart_url(ticker: str, asset_class: str | None = None) -> str:
    return _CHART + quote(tv_symbol(ticker, asset_class))


def watchlist_export(tickers: list[str]) -> str:
    """Newline-joined symbols in TradingView's watchlist-import format
    (paste into TV → Watchlist → ⋯ → Import). De-duped, order preserved."""
    seen: list[str] = []
    for t in tickers:
        sym = tv_symbol(t)
        if sym and sym not in seen:
            seen.append(sym)
    return "\n".join(seen)
