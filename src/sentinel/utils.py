"""Shared utilities. Currently: ticker extraction per SPEC §7."""

import re
from typing import Iterable, Optional


# ── low-value headline filter ────────────────────────────────────────────
# Income-ETF / closed-end-fund payout notices ("X declares monthly
# distribution of $0.36", "PGIM … Fund declares $0.108 dividend") are pure
# PR-wire boilerplate — zero signal for a trading desk, but they syndicate in
# huge volume and were drowning the /intel feed (~25% of items on the live
# Pi). High-precision match: a declaration verb + a *payout* phrasing, gated
# so material company dividends (no fund/ETF marker) and editorial dividend
# articles (no "declares/announces") survive.
_PAYOUT_DECL_RE = re.compile(r"\b(declares?|announces?)\b", re.IGNORECASE)
_PAYOUT_DIST_RE = re.compile(
    r"\b(monthly|quarterly|semi-?annual|weekly|special|cash)\s+distribution\b"
    r"|\bdistribution of \$",
    re.IGNORECASE,
)
_PAYOUT_DIV_RE = re.compile(r"\bdividend\b", re.IGNORECASE)
_FUND_VEHICLE_RE = re.compile(
    r"\b(fund|etf|trust|portfolio|closed-end)\b", re.IGNORECASE
)


def is_routine_payout_headline(title: Optional[str]) -> bool:
    """True for routine fund/ETF distribution + closed-end-fund dividend
    declarations — the high-volume PR-wire boilerplate that adds no trading
    signal. Conservative: a real company dividend ("Apple declares a
    dividend…") has no fund/ETF marker and is kept; a dividend *article*
    ("2 ETFs Paying Reliable Dividends") has no declaration verb and is kept.
    """
    t = title or ""
    if not _PAYOUT_DECL_RE.search(t):
        return False
    if _PAYOUT_DIST_RE.search(t):
        return True
    if _PAYOUT_DIV_RE.search(t) and _FUND_VEHICLE_RE.search(t):
        return True
    return False


# Common English / finance-jargon words that look like tickers — reject these
# even when they appear as cashtags. Exact list from SPEC §7.
TICKER_BLOCKLIST: frozenset[str] = frozenset({
    "A", "ARE", "IT", "ALL", "ON", "BE", "OR", "AND", "FOR", "BY", "AT", "TO",
    "AS", "IS", "GO", "ANY", "CAN", "DO", "HAS", "HE", "I", "IF", "IN", "MY",
    "NO", "NOW", "OF", "OUT", "SO", "UP", "WE", "WHO", "YOU", "ONE", "TWO",
    "DD", "CEO", "USA", "USD", "EOD", "EPS", "PE", "PR", "IPO", "ATH", "ATL",
    "IV", "OTM", "ITM",
})

_CASHTAG_RE = re.compile(r"\$([A-Z]{1,5})\b")
_BARE_RE = re.compile(r"\b([A-Z]{1,5})\b")


# Company-name → canonical-ticker map for the names most likely to show
# up in financial news WITHOUT a cashtag ("Nvidia announced…" rather
# than "$NVDA announced…"). Used by `extract_tickers` as a third path
# alongside cashtags and bare-ticker matching, so a story that mentions
# Apple/Nvidia by name still gets the ticker tag.
#
# Each entry is a case-insensitive substring keyed on a non-overlapping
# distinctive phrase — we use word-boundary matching at runtime so
# "Microsoft" doesn't accidentally fire on "Microsoftie" (yes, real word).
# Aliases under the same canonical ticker (Google/Alphabet, J&J/Johnson
# & Johnson) are listed separately for predictable maintenance.
#
# Keep this curated and biased toward names that appear in headline-grade
# news. The long tail belongs in a future TickerAlias DB table seeded
# from yfinance.
COMPANY_NAME_ALIASES: dict[str, str] = {
    # ── mega-cap tech ──
    "nvidia": "NVDA",
    "apple": "AAPL",
    "microsoft": "MSFT",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "amazon": "AMZN",
    "meta": "META",
    "facebook": "META",
    "tesla": "TSLA",
    "netflix": "NFLX",
    "oracle": "ORCL",
    "salesforce": "CRM",
    "adobe": "ADBE",
    "broadcom": "AVGO",
    "intel": "INTC",
    "amd": "AMD",
    "qualcomm": "QCOM",
    "ibm": "IBM",
    "cisco": "CSCO",
    # ── semis / AI infra ──
    "tsmc": "TSM",
    "taiwan semiconductor": "TSM",
    "asml": "ASML",
    "arm holdings": "ARM",
    "palantir": "PLTR",
    "snowflake": "SNOW",
    "crowdstrike": "CRWD",
    "servicenow": "NOW",
    "service now": "NOW",
    "supermicro": "SMCI",
    "super micro": "SMCI",
    "micron": "MU",
    "lam research": "LRCX",
    "applied materials": "AMAT",
    # ── mega-cap non-tech ──
    "berkshire": "BRK.B",
    "berkshire hathaway": "BRK.B",
    "jpmorgan": "JPM",
    "jp morgan": "JPM",
    "exxonmobil": "XOM",
    "exxon": "XOM",
    "walmart": "WMT",
    "costco": "COST",
    "visa": "V",
    "mastercard": "MA",
    "unitedhealth": "UNH",
    "johnson & johnson": "JNJ",
    "j&j": "JNJ",
    "procter & gamble": "PG",
    "p&g": "PG",
    "chevron": "CVX",
    "pfizer": "PFE",
    "eli lilly": "LLY",
    "lilly": "LLY",
    "boeing": "BA",
    "ford": "F",
    "general motors": "GM",
    # ── fintech / crypto-equity (frequent in news, distinctive names) ──
    "coinbase": "COIN",
    "robinhood": "HOOD",
    "microstrategy": "MSTR",
    # ── quantum (the user's example) ──
    "ionq": "IONQ",
    "rigetti": "RGTI",
    "d-wave": "QBTS",
    "quantum computing inc": "QUBT",
    "honeywell quantum": "HON",
    # ── ETFs frequently named ──
    "s&p 500": "SPY",
    "nasdaq 100": "QQQ",
    "russell 2000": "IWM",
    "dow jones industrial": "DIA",
    # ── crypto ──
    "bitcoin": "BTC-USD",
    "ethereum": "ETH-USD",
    "ether": "ETH-USD",
    "solana": "SOL-USD",
    "ripple": "XRP-USD",
    "dogecoin": "DOGE-USD",
    "shiba inu": "SHIB-USD",
    "polkadot": "DOT-USD",
    "cardano": "ADA-USD",
    "chainlink": "LINK-USD",
    "litecoin": "LTC-USD",
    "polygon": "MATIC-USD",
    "avalanche": "AVAX-USD",
}

# Pre-built regex per alias for word-boundary case-insensitive match.
# Compiled once at import; per-call cost is ~negligible (a dict + ~80
# tiny regex passes over a few hundred chars of text).
_NAME_PATTERNS: tuple[tuple[re.Pattern, str], ...] = tuple(
    (re.compile(rf"\b{re.escape(name)}\b", re.IGNORECASE), ticker)
    for name, ticker in COMPANY_NAME_ALIASES.items()
)


# ── chat reply highlighting ─────────────────────────────────────────────────
# Deterministic post-formatter for conversational replies: makes the
# scannable bits pop (tickers, % moves, a tight set of unambiguous trading
# terms) regardless of which model/temperature produced the text. Keeps it
# tasteful — high-signal only, never a bold soup, never inside code/links/
# existing emphasis.

# Spans we must NOT touch: fenced code, inline code, existing bold/italic,
# and URLs. re.split with a capture group keeps these as pass-through chunks.
_PROTECTED_RE = re.compile(
    r"(```.*?```|`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|https?://\S+)",
    re.DOTALL,
)

# Unambiguous finance terms only — deliberately excludes common English like
# "long"/"short"/"support"/"beat" that would mis-highlight in prose.
_KEYWORDS = (
    "breakout", "breakdown", "short squeeze", "squeeze", "catalyst",
    "bullish", "bearish", "oversold", "overbought", "upgrade", "downgrade",
    "guidance", "merger", "acquisition", "buyback", "dilution", "bankruptcy",
    "halt", "all-time high", "52-week high", "52-week low",
)
_HIGHLIGHT_RE = re.compile(
    r"\$[A-Za-z][A-Za-z0-9]{0,5}(?:[-=.][A-Za-z0-9]{1,6})?"   # $AAPL $BTC-USD $ES=F
    r"|(?<![\w.])[+-]?\d+(?:\.\d+)?%"                          # +12%  -3.4%
    r"|\b(?:" + "|".join(k.replace(" ", r"\s") for k in _KEYWORDS) + r")\b",
    re.IGNORECASE,
)


def highlight_markdown(text: str) -> str:
    """Bold tickers, % moves and key trading terms in a chat reply, leaving
    code spans, links and existing markdown untouched."""
    if not text:
        return text

    def _emphasize(chunk: str) -> str:
        return _HIGHLIGHT_RE.sub(lambda m: f"**{m.group(0)}**", chunk)

    parts = _PROTECTED_RE.split(text)
    # Odd indices are the captured protected spans — pass them through as-is.
    return "".join(
        p if i % 2 else _emphasize(p) for i, p in enumerate(parts)
    )


def chunk_text(text: str, limit: int = 1950) -> list[str]:
    """Split `text` into Discord-sendable pieces (≤ `limit`, < the 2000-char
    hard cap) WITHOUT truncating. Breaks on paragraph, then line, then — only
    if a single line is itself too long — a hard slice. Order preserved; no
    chunk exceeds `limit`; empty input → []. This is what stops long answers
    from being chopped mid-sentence."""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        candidate = f"{buf}\n\n{para}" if buf else para
        if len(candidate) <= limit:
            buf = candidate
            continue
        if buf:
            chunks.append(buf)
            buf = ""
        if len(para) <= limit:
            buf = para
            continue
        # A single paragraph over the limit — fall back to lines, then hard.
        line_buf = ""
        for line in para.split("\n"):
            lb = f"{line_buf}\n{line}" if line_buf else line
            if len(lb) <= limit:
                line_buf = lb
                continue
            if line_buf:
                chunks.append(line_buf)
                line_buf = ""
            while len(line) > limit:
                chunks.append(line[:limit])
                line = line[limit:]
            line_buf = line
        buf = line_buf
    if buf:
        chunks.append(buf)
    return chunks


def extract_tickers_ranked(
    text: str,
    watchlist_tickers: Iterable[str],
    *,
    flair: Optional[str] = None,
    title: Optional[str] = None,
) -> list[str]:
    """Same matching rules as `extract_tickers` but returns an
    ORDERED list with the most-relevant ticker first.

    Ranking:
    1. Cashtagged in the title          → most signal-rich
    2. Cashtagged anywhere              → strong
    3. Highest plain-mention count      → "DELL beat, NVDA beat" both
                                          mentioned 3x → tied (insertion order)
    4. Name-only resolution             → weakest

    Used by ingesters to pick the "primary" ticker for the legacy
    single-`ticker` column while populating the full set on
    `tickers_csv`. Same gates: watchlist + blocklist apply.
    """
    accepted = extract_tickers(text, watchlist_tickers, flair=flair, title=title)
    if not accepted:
        return []
    combined = text if title is None else f"{title}\n{text}"
    title_text = title or ""
    flair_upper = (flair or "").strip().upper()

    def _score(t: str) -> tuple[int, int]:
        title_cashtag = bool(re.search(rf"\${re.escape(t)}\b", title_text))
        any_cashtag = bool(re.search(rf"\${re.escape(t)}\b", combined))
        # Count bare appearances (excluding $-prefixed) for tiebreaks.
        bare_count = len(re.findall(rf"(?<!\$)\b{re.escape(t)}\b", combined))
        primary_tier = (
            4 if title_cashtag
            else 3 if any_cashtag
            else 2 if flair_upper == t
            else 1
        )
        return (primary_tier, bare_count)

    return sorted(accepted, key=_score, reverse=True)


def format_tickers_csv(tickers: Iterable[str]) -> Optional[str]:
    """Pack tickers into the ``,A,B,C,`` substring-search format we
    store in NewsItem.tickers_csv. Leading + trailing comma so
    ``LIKE '%,X,%'`` matches end-of-list too. Returns None for empty."""
    seen: list[str] = []
    for t in tickers:
        u = t.upper().strip()
        if u and u not in seen:
            seen.append(u)
    if not seen:
        return None
    return "," + ",".join(seen) + ","


def parse_tickers_csv(csv: Optional[str]) -> list[str]:
    """Inverse of format_tickers_csv. Returns [] for None or empty."""
    if not csv:
        return []
    return [t for t in csv.strip(",").split(",") if t]


def extract_tickers(
    text: str,
    watchlist_tickers: Iterable[str],
    *,
    flair: Optional[str] = None,
    title: Optional[str] = None,
) -> set[str]:
    """Extract tickers from arbitrary text (Reddit, news articles, filings).

    Rules:
    1. Cashtag form ($AAPL): accept if in watchlist + not blocklisted.
    2. Bare form (AAPL): accept if in watchlist + not blocklisted AND
       any one of:
         - appears ≥2 times
         - post flair matches
         - title cashtags it
         - title mentions it as a bare word
         - body mentions it alongside a financial-context cue
           (earnings / beats / shares / surge / report / guidance /
           upgrade / target / analyst …) within the same sentence
    3. **Company name** (Nvidia, Apple, Microsoft, …): accept if the
       canonical ticker is in the watchlist + not blocklisted AND the
       name appears anywhere in the combined text. Name matching is
       word-boundary case-insensitive (`COMPANY_NAME_ALIASES`). This is
       why "Nvidia announced…" (no cashtag) now tags $NVDA on news
       articles where the old rules would miss it entirely.
    4. Blocklist always wins regardless of cashtag presence.
    """
    watchlist = {t.upper() for t in watchlist_tickers}
    combined = text if title is None else f"{title}\n{text}"

    cashtag_hits = set(_CASHTAG_RE.findall(combined))
    bare_hits = set(_BARE_RE.findall(combined))
    title_cashtags = set(_CASHTAG_RE.findall(title)) if title else set()
    title_bare = (
        set(_BARE_RE.findall(title)) if title else set()
    )
    flair_upper = flair.strip().upper() if flair else None

    accepted: set[str] = set()

    # Rule 1: cashtags — straight accept if watchlisted and not blocklisted.
    for ticker in cashtag_hits:
        if ticker in TICKER_BLOCKLIST:
            continue
        if ticker in watchlist:
            accepted.add(ticker)

    # Rule 2: bare tickers — need a second signal.
    for ticker in bare_hits - cashtag_hits:
        if ticker in TICKER_BLOCKLIST:
            continue
        if ticker not in watchlist:
            continue
        bare_count = len(re.findall(rf"(?<!\$)\b{re.escape(ticker)}\b", combined))
        flair_match = flair_upper == ticker
        title_cashtag_match = ticker in title_cashtags
        title_bare_match = ticker in title_bare
        sentence_cue_match = _has_financial_cue_near(combined, ticker)
        if (
            bare_count >= 2
            or flair_match
            or title_cashtag_match
            or title_bare_match
            or sentence_cue_match
        ):
            accepted.add(ticker)

    # Rule 3: company-name resolution. Watchlist + blocklist gates still
    # apply — a name match alone doesn't add a ticker we're not tracking.
    for pattern, ticker in _NAME_PATTERNS:
        if ticker in accepted:
            continue
        if ticker in TICKER_BLOCKLIST:
            continue
        if ticker not in watchlist:
            continue
        if pattern.search(combined):
            accepted.add(ticker)

    return accepted


# Financial-context cue words. If a bare ticker appears in the same
# sentence as one of these, count it as a real reference (a sentence
# in news/Reddit that says "DELL beats earnings" is unambiguously
# about the stock; the watchlist gate already filters to known symbols
# so the false-positive surface is small).
_FIN_CUE_RE = re.compile(
    r"\b("
    r"earn(?:ing|ed|s|)|beat(?:s|en)?|miss(?:ed|es)?|surg(?:e|ed|ing)|"
    r"plung(?:e|ed|ing)|drop(?:ped|s|ping)?|fall(?:ing|s)?|fell|"
    r"rall(?:y|ied|ies|ying)|jump(?:ed|s|ing)?|soar(?:ed|s|ing)?|"
    r"gain(?:ed|s|ing)?|los(?:e|t|es|ing)|stock|shares?|"
    r"report(?:ed|s|ing)?|guidance|forecast(?:ed|s)?|outlook|"
    r"upgrad(?:e|ed|es|ing)?|downgrad(?:e|ed|es|ing)?|"
    r"analyst|rating|target|raise(?:d)?|cut(?:s|ting)?|"
    r"price target|buy|sell|hold|overweight|underweight|"
    r"valuation|multiple|p/?e|EPS|revenue|profit|margin|"
    r"acquir(?:e|ed|es|ing)|merger|spinoff|IPO|SPAC|"
    r"crypto|coin|token|blockchain|halving"
    r")\b",
    re.IGNORECASE,
)


def _has_financial_cue_near(text: str, ticker: str) -> bool:
    """True iff `ticker` appears bare somewhere in `text` AND the same
    sentence contains a financial-context cue word. Tolerant of
    standard sentence boundaries (`.`, `!`, `?`, newlines)."""
    bare_re = re.compile(rf"(?<!\$)\b{re.escape(ticker)}\b")
    # Split on simple sentence boundaries; not perfect but good enough
    # for newspaper-style headline+lead-paragraph extraction.
    sentences = re.split(r"(?<=[.!?\n])\s+", text)
    for s in sentences:
        if bare_re.search(s) and _FIN_CUE_RE.search(s):
            return True
    return False
