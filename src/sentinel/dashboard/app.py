"""The cockpit page + its in-process server.

Design constraints that shape this file:
- One loop, one process. `mount()` is called from inside the bot's running
  asyncio loop; it starts uvicorn as a *task* on that same loop (with
  signal-handler installation suppressed so it doesn't fight main.py's
  SIGTERM/SIGINT handlers). discord.py, APScheduler and uvicorn then
  cooperate on one event loop.
- Read-mostly. Every panel pulls from the same WAL SQLite the bot uses;
  blocking DB/diagnostic calls are pushed to `asyncio.to_thread` so a refresh
  tick never stalls the loop (and never holds a write lock — these are reads).
- Reuse, don't fork. Q&A goes through `chat.answer_question` (the shared
  Discord path); the control surface goes through the existing chokepoints
  (`scorecard.record_call`, the live APScheduler instance) — the dashboard
  adds no parallel logic, only a window and a few buttons.
- Cannot harm the bot. Mount is wrapped+isolated; every timer callback and
  handler catches its own exceptions and degrades a panel instead of raising.

Presentation: there is exactly one stylesheet (`_THEME_CSS`, injected once)
and a small set of primitives (`_panel`, `_kpi`, `_tile`, `_bar`). Panels feed
those primitives from the *structured* accessors (`funds.fund_standings`,
`scorecard.track_record_summary`, `portfolio.open_positions`,
`health.health_report`, `sysinfo.snapshot`) — never by dumping a Discord
markdown string into the page.
"""

from __future__ import annotations

import asyncio
import html
from datetime import datetime, timedelta, timezone

from loguru import logger

from ..config import settings

# storage_secret is required only for per-client storage (we don't use it),
# but passing a constant silences NiceGUI's warning. Not a security boundary:
# the server binds localhost-only and this is a single-user personal tool.
_STORAGE_SECRET = "sentinel-cockpit-local"

_MOUNTED = False
_scheduler = None  # the live AsyncIOScheduler, set by mount()


# ── design system ───────────────────────────────────────────────────────────
# One stylesheet, injected once. Every visual decision lives here (tokens,
# card chrome, tables, alerts, the log viewer, the chat) so panels stay
# declarative and consistent instead of carrying ad-hoc inline hex.

_THEME_CSS = """
<style>
/* ── design tokens (OpenRouter-inspired neutral-dark palette) ────────────
   The previous warm-blue tokens (#0a0e14 etc.) are replaced with a near-
   black neutral; borders/hover/active are expressed as low-opacity white
   so that one tweak to alpha shifts every chrome line in proportion. */
:root{
  --bg:#090a0b; --surface:#131416; --surface2:#1b1d20;
  --border:rgba(255,255,255,.085); --border-soft:rgba(255,255,255,.05);
  --border-strong:rgba(255,255,255,.16);
  --text:#f0f0f1; --muted:rgba(255,255,255,.62); --faint:rgba(255,255,255,.42);
  --primary:#6699ff; --good:#3ddc97; --bad:#ff6b6b; --warn:#fbbf24;
  --accent:rgba(255,255,255,.07); --accent-strong:rgba(255,255,255,.11);
}
/* Auto-hide scrollbars — transparent at rest so they don't crowd right-
   aligned table cells; fade in only when the scrollable container (or
   any ancestor of the cursor) is :hovered. The thin-9px width still
   reserves layout space, so showing the slider doesn't reflow. */
*{scrollbar-width:thin;scrollbar-color:transparent transparent;
  transition:scrollbar-color .15s ease;}
*:hover{scrollbar-color:rgba(255,255,255,.18) transparent;}
body{background:var(--bg);color:var(--text);
  font-feature-settings:"tnum";}
::-webkit-scrollbar{width:9px;height:9px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:transparent;border-radius:6px;
  border:2px solid var(--bg);transition:background .15s ease;}
:hover::-webkit-scrollbar-thumb{background:rgba(255,255,255,.12);}
:hover::-webkit-scrollbar-thumb:hover{background:rgba(255,255,255,.22);}

/* `scroll-margin-top` ensures #anchor jumps from the sidebar align the
   target panel below the sticky header rather than under it. */
.fr-card{background:var(--surface);border:1px solid var(--border);
  border-radius:12px;display:flex;flex-direction:column;overflow:hidden;
  scroll-margin-top:4.5rem;}
.fr-hd{display:flex;align-items:center;gap:.55rem;padding:.62rem .95rem;
  border-bottom:1px solid var(--border);background:rgba(255,255,255,.012);}
.fr-hd .ic{font-size:14px;line-height:1;opacity:.85;}
.fr-hd .ti{font-size:11px;font-weight:600;letter-spacing:.15em;
  text-transform:uppercase;color:var(--muted);}
.fr-hd .rt{margin-left:auto;font-size:11px;color:var(--faint);
  font-variant-numeric:tabular-nums;}
.fr-bd{padding:.85rem .95rem;flex:1;min-width:0;}

.fr-kpi{background:var(--surface);border:1px solid var(--border);
  border-radius:12px;padding:.7rem .9rem;display:flex;flex-direction:column;
  gap:.15rem;}
.fr-kpi .l{font-size:10px;letter-spacing:.13em;text-transform:uppercase;
  color:var(--faint);}
.fr-kpi .v{font-size:23px;font-weight:600;line-height:1.1;
  font-variant-numeric:tabular-nums;}
.fr-kpi .s{font-size:11px;color:var(--muted);
  font-variant-numeric:tabular-nums;}

.fr-tile{background:var(--surface2);border:1px solid var(--border);
  border-radius:9px;padding:.55rem .7rem;display:flex;flex-direction:column;
  gap:.1rem;}
.fr-tile .l{font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);}
.fr-tile .v{font-size:15px;font-weight:600;
  font-variant-numeric:tabular-nums;}

.pos{color:var(--good);} .neg{color:var(--bad);}
.mut{color:var(--muted);} .fnt{color:var(--faint);}
.num{font-variant-numeric:tabular-nums;}

table.fr-tb{width:100%;border-collapse:collapse;font-size:12.5px;}
table.fr-tb th{font-size:10px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--faint);text-align:right;font-weight:600;
  padding:.32rem .5rem .45rem;border-bottom:1px solid var(--border);}
table.fr-tb th:first-child,table.fr-tb td:first-child{text-align:left;}
table.fr-tb td{padding:.42rem .5rem;border-bottom:1px solid
  var(--border-soft);text-align:right;font-variant-numeric:tabular-nums;
  white-space:nowrap;}
table.fr-tb tr:last-child td{border-bottom:0;}
table.fr-tb tbody tr:hover td{background:rgba(255,255,255,.022);}
.tk{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:600;}
/* Fixed-width side label — keeps the ticker aligned across LONG (4 chars)
   and SHORT (5 chars) rows; without it a single space gap shifts the ticker
   column by one character every time the side changes. */
.sd{display:inline-block;width:3.2rem;font-weight:700;letter-spacing:.04em;}
.sd-l{color:var(--good);} .sd-s{color:var(--bad);}

.fr-bar{height:5px;border-radius:3px;background:var(--surface2);
  overflow:hidden;min-width:42px;}
.fr-bar>i{display:block;height:100%;border-radius:3px;
  background:var(--primary);transition:width .4s ease;}

.fr-pill{font-size:12px;font-weight:600;padding:.22rem .7rem;
  border-radius:9999px;border:1px solid transparent;white-space:nowrap;}
.fr-pill.ok{background:rgba(61,220,151,.13);color:var(--good);
  border-color:rgba(61,220,151,.32);}
.fr-pill.warn{background:rgba(251,191,36,.13);color:var(--warn);
  border-color:rgba(251,191,36,.30);}
.fr-pill.crit{background:rgba(255,107,107,.14);color:var(--bad);
  border-color:rgba(255,107,107,.35);}
.fr-pill.idle{background:var(--surface2);color:var(--muted);
  border-color:var(--border);}

.fr-alert{display:flex;gap:.5rem;padding:.5rem .65rem;border-radius:9px;
  font-size:12.5px;line-height:1.45;align-items:flex-start;}
.fr-alert.crit{background:rgba(255,107,107,.10);
  border:1px solid rgba(255,107,107,.26);color:#fecaca;}
.fr-alert.warn{background:rgba(251,191,36,.09);
  border:1px solid rgba(251,191,36,.24);color:#fde68a;}

.dot{width:8px;height:8px;border-radius:50%;display:inline-block;
  flex:0 0 auto;}
.dot.ok{background:var(--good);box-shadow:0 0 6px rgba(61,220,151,.55);}
.dot.bad{background:var(--bad);box-shadow:0 0 6px rgba(255,107,107,.55);}
.dot.idle{background:rgba(255,255,255,.20);}

.fr-log{background:#0a0a0c;border:1px solid var(--border);
  border-radius:10px;height:21rem;overflow:auto;padding:.55rem .8rem;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11.5px;
  line-height:1.6;}
.fr-log .ln{white-space:pre-wrap;word-break:break-word;}
.fr-log .tm{color:rgba(255,255,255,.30);}
.fr-log .lv{font-weight:600;}
.fr-log .l-ERROR,.fr-log .l-CRITICAL{color:#ff8585;}
.fr-log .l-WARNING{color:#fbbf24;}
.fr-log .l-SUCCESS{color:#3ddc97;}
.fr-log .l-DEBUG{color:var(--faint);}
.fr-log .l-INFO{color:rgba(255,255,255,.70);}

.chat-wrap{display:flex;flex-direction:column;height:30rem;}
.chat-feed{flex:1;overflow-y:auto;padding:.3rem .2rem .6rem;display:flex;
  flex-direction:column;gap:.8rem;}
.chat-empty{margin:auto;text-align:center;color:var(--faint);
  font-size:13px;display:flex;flex-direction:column;gap:.8rem;
  align-items:center;max-width:30rem;}
.bub{max-width:84%;padding:.55rem .8rem;border-radius:14px;font-size:13px;
  line-height:1.5;}
.bub.a{align-self:flex-start;background:var(--surface2);
  border:1px solid var(--border);border-top-left-radius:4px;}
.bub.u{align-self:flex-end;background:rgba(102,153,255,.13);
  border:1px solid rgba(102,153,255,.30);border-top-right-radius:4px;
  white-space:pre-wrap;}
.bub .rl{font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--faint);margin-bottom:.22rem;}
.bub .ts{font-size:9.5px;color:var(--faint);margin-top:.35rem;
  text-align:right;}
.bub .nicegui-markdown,.bub .nicegui-markdown *{color:var(--text);}
.bub .nicegui-markdown p{margin:.28rem 0;}
.bub .nicegui-markdown p:first-child{margin-top:0;}
.bub .nicegui-markdown p:last-child{margin-bottom:0;}
.bub .nicegui-markdown ul,.bub .nicegui-markdown ol{margin:.3rem 0;
  padding-left:1.15rem;}
.bub .nicegui-markdown code{background:rgba(255,255,255,.06);
  padding:.04rem .32rem;border-radius:4px;font-size:11.5px;}
.bub .nicegui-markdown a{color:#8fb6ff;}
.typing{display:inline-flex;gap:5px;align-items:center;padding:.15rem 0;}
.typing i{width:6px;height:6px;border-radius:50%;background:var(--muted);
  display:inline-block;animation:frb 1.2s infinite both;}
.typing i:nth-child(2){animation-delay:.16s;}
.typing i:nth-child(3){animation-delay:.32s;}
@keyframes frb{0%,80%,100%{opacity:.25;transform:translateY(0);}
  40%{opacity:1;transform:translateY(-3px);}}
.chip{font-size:12px;padding:.32rem .7rem;border-radius:9999px;
  background:var(--surface2);border:1px solid var(--border);
  color:var(--muted);cursor:pointer;transition:all .15s;
  user-select:none;}
.chip:hover{border-color:var(--primary);color:var(--text);}

/* Lookup-panel result box — scrollable, monospaced-prose feel matching the
   rest of the dashboard. Markdown selectors are scoped here so they don't
   bleed back into the chat bubbles. */
.lookup-out{font-size:13px;line-height:1.55;max-height:26rem;
  overflow:auto;padding:.6rem .8rem;background:var(--surface2);
  border:1px solid var(--border);border-radius:10px;color:var(--text);}
.lookup-out p{margin:.3rem 0;}
.lookup-out p:first-child{margin-top:0;}
.lookup-out p:last-child{margin-bottom:0;}
.lookup-out code{background:rgba(255,255,255,.06);padding:.04rem .32rem;
  border-radius:4px;font-size:11.5px;}
.lookup-out a{color:#8fb6ff;}
.lookup-out ul,.lookup-out ol{margin:.3rem 0;padding-left:1.15rem;}
.lookup-out h1,.lookup-out h2,.lookup-out h3{
  font-size:14px;font-weight:600;margin:.5rem 0 .3rem;}

.fr-jobrow{display:flex;align-items:center;gap:.5rem;padding:.34rem .15rem;
  border-bottom:1px solid var(--border-soft);font-size:12px;}
.fr-jobrow:last-child{border-bottom:0;}
.fr-jobrow .jid{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;
  white-space:nowrap;}
.fr-jobrow .jt{color:var(--faint);font-size:11px;
  font-variant-numeric:tabular-nums;width:4.7rem;text-align:right;}
.q-field--outlined .q-field__control{border-radius:10px;}
.q-field--outlined .q-field__control:before{border-color:var(--border);}

/* ── shell + sidebar (OpenRouter-inspired left-categorized nav) ──────────
   The shell is a flex row holding the sticky sidebar and the scrolling
   main content. Sidebar items are plain `<a href="#id">` anchors — the
   browser does the scroll, scrollspy JS only toggles `.active`. */
.nicegui-content{padding:0!important;gap:0!important;}
.fr-shell{display:flex;flex-direction:row;align-items:flex-start;width:100%;
  box-sizing:border-box;}
/* `height` (not `max-height`) so the border + background extend the full
   viewport even when the nav has fewer items than fit — otherwise the rail
   stops where the items end and the page-edge feels chopped. */
.fr-sidebar{position:sticky;top:0;flex:0 0 14rem;width:14rem;
  height:calc(100vh - 3.5rem);overflow-y:auto;
  border-right:1px solid var(--border);
  padding:1rem .75rem 1.5rem;display:flex;flex-direction:column;
  background:rgba(255,255,255,.014);box-sizing:border-box;}
.fr-sb-section{font-size:10.5px;font-weight:600;letter-spacing:.13em;
  text-transform:uppercase;color:var(--faint);
  padding:.65rem .55rem .35rem;margin-top:.5rem;}
.fr-sb-section:first-child{margin-top:0;}
.fr-sb-item{display:flex;align-items:center;gap:.6rem;
  padding:.45rem .55rem;border-radius:7px;font-size:13px;
  color:var(--muted);text-decoration:none;cursor:pointer;
  transition:background .12s ease,color .12s ease;}
.fr-sb-item:hover{background:var(--accent);color:var(--text);}
.fr-sb-item.active{background:var(--accent-strong);color:var(--text);
  font-weight:500;}
.fr-sb-item .ic{font-size:14px;flex:0 0 1.1rem;text-align:center;
  opacity:.85;}
.fr-main{flex:1;min-width:0;}

/* ── layout grid (now lives inside .fr-main) ── */
.fr-grid{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));
  gap:1rem;width:100%;padding:1rem 1rem 4rem;align-items:stretch;
  grid-auto-flow:row dense;box-sizing:border-box;}
/* `ui.timer` from a panel's _tick_now lands in the grid's slot as a
   logic-only element. Left visible it is a 1-col grid item that scatters
   auto-placement and leaves huge empty tracks. display:none removes it
   from layout without unmounting it, so the timer keeps firing. */
.fr-grid > nicegui-timer{display:none;}
.fr-ribbon{grid-column:1/-1;display:grid;
  grid-template-columns:repeat(6,minmax(0,1fr));gap:1rem;
  scroll-margin-top:4.5rem;}
.c12{grid-column:span 12;} .c7{grid-column:span 7;}
.c5{grid-column:span 5;} .c4{grid-column:span 4;}
@media (max-width:1279px){
  .c7,.c5{grid-column:span 12;}
  .fr-ribbon{grid-template-columns:repeat(3,minmax(0,1fr));}}
@media (max-width:899px){
  .fr-grid{padding:.7rem .7rem 3rem;gap:.7rem;}
  .c4{grid-column:span 12;}
  .fr-ribbon{grid-template-columns:repeat(2,minmax(0,1fr));}
  .fr-sidebar{display:none;}}
.fr-w{width:100%;} .fr-grow{flex:1;min-width:0;}
.fr-row{display:flex;flex-direction:row;align-items:center;gap:.5rem;
  width:100%;}
.fr-rowi{display:flex;flex-direction:row;align-items:center;gap:.5rem;}
.fr-end{display:flex;flex-direction:row;align-items:flex-end;gap:.75rem;
  width:100%;}
.fr-between{display:flex;flex-direction:row;align-items:center;
  justify-content:space-between;gap:.6rem;width:100%;}
.fr-tiles{display:flex;flex-direction:row;gap:.5rem;width:100%;}
.fr-tiles>*{flex:1;min-width:0;}
.fr-3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
  gap:.5rem;width:100%;}
.fr-chips{display:flex;flex-wrap:wrap;gap:.5rem;justify-content:center;}
.fr-jobgrid{display:grid;grid-template-columns:1fr 1fr;gap:.1rem .9rem;
  max-height:9rem;overflow-y:auto;}

/* clickable wallets list — rows are real grid elements (NOT display:contents)
   so the hover background sits on the row and fills it. Negative horizontal
   margins push the rows past the card-body padding so the gray stripe runs
   edge-to-edge of the card; the rows re-add the same .95rem inside, so
   text stays aligned with everything else in the card. */
.fr-wgrid{display:flex;flex-direction:column;
  width:calc(100% + 1.9rem);margin:0 -.95rem;}
.fr-wh,.fr-wrow{display:grid;width:100%;
  grid-template-columns:minmax(0,1fr) 5.6rem 4.6rem 3rem 5.6rem 4rem;
  align-items:center;padding:0 .95rem;box-sizing:border-box;}
.fr-wh{border-bottom:1px solid var(--border);}
.fr-wh > *{font-size:10px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--faint);text-align:right;font-weight:600;
  padding:.32rem .55rem .45rem;}
.fr-wh > *:first-child{text-align:left;}
.fr-wrow{cursor:pointer;border-bottom:1px solid var(--border-soft);
  transition:background .12s;}
.fr-wrow:last-child{border-bottom:0;}
.fr-wrow:hover{background:rgba(255,255,255,.04);}
.fr-wrow > *{padding:.5rem .55rem;text-align:right;
  font-variant-numeric:tabular-nums;font-size:12.5px;white-space:nowrap;}
.fr-wrow > *:first-child{text-align:left;white-space:normal;}
.fr-wmandate{display:block;font-size:10.5px;color:var(--faint);
  line-height:1.35;margin-top:.15rem;}

/* watches list — plain-English wraps within each row, so it isn't a grid */
.fr-watch{display:flex;align-items:flex-start;gap:.5rem;
  padding:.5rem .15rem;border-bottom:1px solid var(--border-soft);
  font-size:12px;line-height:1.4;}
.fr-watch:last-child{border-bottom:0;}
.fr-watch .wmain{flex:1;min-width:0;}
.fr-watch .wid{font-family:ui-monospace,Menlo,monospace;
  color:var(--faint);font-size:10.5px;margin-right:.4rem;}
.fr-watch .wmeta{font-size:10.5px;color:var(--faint);margin-top:.2rem;}
.fr-watch .wpaused{color:var(--warn);}

/* paper-book rows (NiceGUI per-row grid so each row can host a real
   Close button on the same chokepoint as !close TICKER). */
.fr-bgrid{display:flex;flex-direction:column;
  width:calc(100% + 1.9rem);margin:0 -.95rem;}
.fr-bh,.fr-brow{display:grid;width:100%;
  grid-template-columns:minmax(0,1fr) 3.5rem 4rem 4rem 5rem 4rem 4.5rem;
  align-items:center;padding:0 .95rem;box-sizing:border-box;}
.fr-bh{border-bottom:1px solid var(--border);}
.fr-bh > *{font-size:10px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--faint);text-align:right;font-weight:600;
  padding:.32rem .55rem .45rem;}
.fr-bh > *:first-child{text-align:left;}
.fr-brow{border-bottom:1px solid var(--border-soft);}
.fr-brow:last-child{border-bottom:0;}
.fr-brow > *{padding:.42rem .55rem;text-align:right;
  font-variant-numeric:tabular-nums;font-size:12.5px;white-space:nowrap;}
.fr-brow > *:first-child{text-align:left;}
.fr-brow > *:last-child{padding:.25rem .55rem;text-align:center;}

.fr-dlg{min-width:min(46rem,92vw);max-width:92vw;}
.fr-dlg .fr-bd{max-height:70vh;overflow-y:auto;}

/* ── Quasar q-table dark theme override — aligns built-in sort/pagi/search
   with the rest of the dashboard so the watchlist doesn't look like a
   visitor from the default Material world. */
.q-table__container.q-table--dark{background:transparent!important;
  color:var(--text);box-shadow:none!important;}
.q-table--dark .q-table thead tr{background:transparent;}
.q-table--dark .q-table th{font-size:10px!important;letter-spacing:.09em;
  text-transform:uppercase;color:var(--faint)!important;font-weight:600;
  padding:.45rem .55rem!important;
  border-bottom:1px solid var(--border)!important;}
.q-table--dark .q-table td{padding:.42rem .55rem!important;font-size:12.5px;
  font-variant-numeric:tabular-nums;
  border-bottom:1px solid var(--border-soft)!important;}
.q-table--dark .q-table tbody tr{cursor:pointer;
  transition:background .12s ease;}
.q-table--dark .q-table tbody tr:hover{background:rgba(255,255,255,.035);}
.q-table__bottom{border-top:1px solid var(--border)!important;
  color:var(--muted)!important;font-size:11px;min-height:2.6rem;}
.q-table__top{padding:.3rem 0!important;}
.q-table__sort-icon{color:var(--faint)!important;font-size:1rem!important;}

/* ── tabs (click-to-show-tab, driven by `data-tab` on sidebar items and
   `.fr-tab.active` on the content). Hidden tabs stay in DOM so their
   timers keep refreshing in the background and switching feels instant. */
.fr-tab{display:none;}
.fr-tab.active{display:block;}
.fr-tab-header{display:flex;align-items:flex-end;
  justify-content:space-between;gap:1rem;
  padding:1.2rem 1rem .65rem;border-bottom:1px solid var(--border);
  margin:0 1rem;flex-wrap:wrap;}
.fr-tab-header h2{font-size:18px;font-weight:600;letter-spacing:.01em;
  margin:0;color:var(--text);line-height:1.2;display:flex;
  align-items:center;gap:.55rem;}
.fr-tab-header .sub{font-size:11.5px;color:var(--faint);
  margin-top:.32rem;letter-spacing:.01em;}
.fr-tab-header .right{display:flex;align-items:center;gap:.6rem;
  font-size:11.5px;color:var(--muted);}

/* a chart-bearing card — the body has no inner padding so the chart fills
   edge-to-edge; the title bar is `.fr-hd` as usual. */
.fr-chart{height:22rem;width:100%;}
.fr-chart.tall{height:30rem;}
.fr-chart.short{height:14rem;}
.fr-card.fr-chartcard .fr-bd{padding:.55rem .55rem .35rem;}

/* unified feed row used by news/filings/calls/activity lists */
.fr-feed{display:flex;flex-direction:column;}
.fr-feed-row{display:grid;grid-template-columns:auto 1fr auto;
  align-items:flex-start;gap:.7rem;padding:.55rem .2rem;
  border-bottom:1px solid var(--border-soft);font-size:12.5px;
  line-height:1.45;}
.fr-feed-row:last-child{border-bottom:0;}
.fr-feed-row .kind{font-size:9px;letter-spacing:.13em;
  text-transform:uppercase;font-weight:700;padding:.18rem .42rem;
  border-radius:4px;border:1px solid var(--border);
  color:var(--muted);background:var(--surface2);min-width:3.4rem;
  text-align:center;}
.fr-feed-row .kind.news{color:#7fb6ff;border-color:rgba(127,182,255,.32);
  background:rgba(127,182,255,.08);}
/* Sentiment-coloured NEWS chips — green for bullish, red for bearish,
   blue (default) for neutral/untagged. The sentiment pipeline scores
   -1/0/+1; only the non-zero sides get a recolour so neutral noise
   doesn't compete visually with directional reads. */
.fr-feed-row .kind.news.bull{color:var(--good);
  border-color:rgba(61,220,151,.32);background:rgba(61,220,151,.08);}
.fr-feed-row .kind.news.bear{color:var(--bad);
  border-color:rgba(255,107,107,.32);background:rgba(255,107,107,.08);}
/* Social pulse chip — its own colour family so it's distinguishable
   from a NEWS row at a glance even when both share the same ticker. */
.fr-feed-row .kind.pulse{color:#fbbf24;
  border-color:rgba(251,191,36,.32);background:rgba(251,191,36,.08);}
.fr-feed-row .kind.filing{color:#a78bfa;
  border-color:rgba(167,139,250,.32);
  background:rgba(167,139,250,.08);}
.fr-feed-row .kind.call{color:var(--good);
  border-color:rgba(61,220,151,.32);
  background:rgba(61,220,151,.08);}
.fr-feed-row .kind.call.short{color:var(--bad);
  border-color:rgba(255,107,107,.32);
  background:rgba(255,107,107,.08);}
.fr-feed-row .body{min-width:0;}
.fr-feed-row .body a{color:var(--text);text-decoration:none;}
.fr-feed-row .body a:hover{color:#8fb6ff;}
.fr-feed-row .body .meta{font-size:10.5px;color:var(--faint);
  margin-top:.18rem;display:flex;gap:.45rem;flex-wrap:wrap;
  align-items:center;}
.fr-feed-row .body .meta .tk{font-size:10.5px;color:var(--muted);}
.fr-feed-row .ts{font-size:10px;color:var(--faint);white-space:nowrap;
  font-variant-numeric:tabular-nums;align-self:flex-start;}

/* catalyst calendar — day-grouped list */
.fr-cal-day{display:flex;align-items:baseline;gap:.7rem;
  padding:.6rem .2rem .25rem;border-top:1px solid var(--border-soft);
  font-size:11px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--faint);font-weight:600;}
.fr-cal-day:first-child{border-top:0;padding-top:.2rem;}
.fr-cal-day .day-out{font-size:9.5px;color:var(--muted);
  background:var(--surface2);padding:.1rem .4rem;border-radius:4px;
  border:1px solid var(--border);}
.fr-cal-row{display:grid;grid-template-columns:5rem 1fr;
  padding:.32rem .35rem;font-size:12.5px;align-items:center;
  border-bottom:1px solid var(--border-soft);}
.fr-cal-row:last-child{border-bottom:0;}
.fr-cal-row .tk{color:var(--text);}

/* watchlist mini-grid (ticker · price · change · sparkline · volume) */
.fr-watchlist{display:flex;flex-direction:column;
  width:calc(100% + 1.9rem);margin:0 -.95rem;}
.fr-wlh,.fr-wlrow{display:grid;width:100%;
  grid-template-columns:1fr 5rem 4.4rem 4.4rem 5rem 5rem;
  align-items:center;padding:0 .95rem;box-sizing:border-box;}
.fr-wlh{border-bottom:1px solid var(--border);}
.fr-wlh > *{font-size:10px;letter-spacing:.09em;text-transform:uppercase;
  color:var(--faint);text-align:right;font-weight:600;
  padding:.32rem .55rem .45rem;}
.fr-wlh > *:first-child{text-align:left;}
.fr-wlrow{cursor:pointer;border-bottom:1px solid var(--border-soft);
  transition:background .12s;}
.fr-wlrow:last-child{border-bottom:0;}
.fr-wlrow:hover{background:rgba(255,255,255,.04);}
.fr-wlrow > *{padding:.42rem .55rem;text-align:right;
  font-variant-numeric:tabular-nums;font-size:12.5px;white-space:nowrap;}
.fr-wlrow > *:first-child{text-align:left;}

/* ticker picker input — slightly bigger than the form inputs since it's
   the primary control of the Markets tab */
.fr-pick{display:flex;gap:.5rem;align-items:center;margin-bottom:.7rem;}
.fr-pick .label{font-size:11px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--faint);min-width:4.5rem;}
.fr-pick .chips{display:flex;flex-wrap:wrap;gap:.35rem;}
.fr-pick .chips .chip.active{border-color:var(--primary);
  color:var(--text);background:rgba(102,153,255,.10);}

/* position summary card sits BESIDE the candlestick on wide screens */
.fr-pos-summary{display:flex;flex-direction:column;gap:.55rem;}
.fr-pos-summary .row{display:flex;justify-content:space-between;
  gap:1rem;font-size:12.5px;align-items:baseline;}
.fr-pos-summary .row .k{color:var(--faint);font-size:10.5px;
  letter-spacing:.09em;text-transform:uppercase;}
.fr-pos-summary .row .v{font-variant-numeric:tabular-nums;
  font-weight:500;}
.fr-pos-summary .big{font-size:22px;font-weight:600;line-height:1.1;
  font-variant-numeric:tabular-nums;}

/* ── news + call cards (Wave 2 redesign) ───────────────────────────────
   The list-of-rows feed was packing 6 fields onto one line and reading
   "built with twigs" per the user. Cards spread the same data across a
   small grid with proper visual hierarchy: meta strip, headline, ticker
   tags, summary excerpt, action row. Card click opens the AI dossier
   (the dominant intent on a news/call hit); secondary actions are
   buttons that stop propagation so they don't double-fire. */
.fr-card-grid{display:grid;
  grid-template-columns:repeat(auto-fill,minmax(22rem,1fr));
  gap:.7rem;width:100%;align-items:stretch;}
.fr-news-card,.fr-call-card{background:var(--surface);
  border:1px solid var(--border);border-radius:10px;
  padding:.7rem .85rem;display:flex;flex-direction:column;gap:.4rem;
  cursor:pointer;transition:border-color .12s ease,
  background .12s ease,transform .12s ease;}
.fr-news-card:hover,.fr-call-card:hover{
  border-color:var(--border-strong);
  background:rgba(255,255,255,.018);}
.fr-news-card .card-meta,
.fr-call-card .card-meta{display:flex;align-items:center;
  gap:.45rem;font-size:10px;color:var(--faint);}
.fr-news-card .card-meta .ts,
.fr-call-card .card-meta .ts{margin-left:auto;
  font-variant-numeric:tabular-nums;}
.fr-news-card .card-title,
.fr-call-card .card-title{font-size:13.5px;font-weight:500;
  color:var(--text);line-height:1.42;
  overflow:hidden;text-overflow:ellipsis;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;}
.fr-news-card .card-tags,
.fr-call-card .card-tags{display:flex;flex-wrap:wrap;
  gap:.32rem .5rem;font-size:11px;align-items:center;}
.fr-news-card .card-tags .tk,
.fr-call-card .card-tags .tk{font-family:ui-monospace,Menlo,monospace;
  background:var(--surface2);color:var(--text);
  padding:.12rem .42rem;border-radius:4px;
  border:1px solid var(--border);font-size:10.5px;font-weight:600;}
.fr-news-card .card-tags .src,
.fr-call-card .card-tags .src{color:var(--muted);font-size:10.5px;
  letter-spacing:.02em;}
.fr-news-card .card-tags .delta,
.fr-call-card .card-tags .delta{
  font-variant-numeric:tabular-nums;font-size:10.5px;}
.fr-news-card .card-excerpt,
.fr-call-card .card-excerpt{font-size:12px;color:var(--muted);
  line-height:1.45;
  overflow:hidden;text-overflow:ellipsis;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;}
.fr-news-card .card-actions,
.fr-call-card .card-actions{display:flex;gap:.45rem;margin-top:auto;
  padding-top:.5rem;border-top:1px solid var(--border-soft);
  align-items:center;}
.fr-news-card .card-actions .btn,
.fr-call-card .card-actions .btn{font-size:10.5px;
  padding:.28rem .6rem;border-radius:5px;background:transparent;
  border:1px solid var(--border);color:var(--muted);cursor:pointer;
  text-decoration:none;transition:all .12s;
  font-family:inherit;letter-spacing:.02em;
  display:inline-flex;align-items:center;gap:.3rem;}
.fr-news-card .card-actions .btn:hover,
.fr-call-card .card-actions .btn:hover{color:var(--text);
  border-color:var(--border-strong);
  background:rgba(255,255,255,.025);}
.fr-news-card .card-actions .btn.primary,
.fr-call-card .card-actions .btn.primary{color:#8fb6ff;
  border-color:rgba(102,153,255,.25);
  background:rgba(102,153,255,.06);}
.fr-news-card .card-actions .btn.primary:hover,
.fr-call-card .card-actions .btn.primary:hover{
  color:#a8c5ff;border-color:rgba(102,153,255,.5);
  background:rgba(102,153,255,.12);}
</style>
"""


# ── formatting helpers ──────────────────────────────────────────────────────


def _usd(v) -> str:
    try:
        return f"${v:,.0f}"
    except (TypeError, ValueError):
        return "—"


def _susd(v) -> str:
    """Signed dollars (+1,234 / -567), for P&L."""
    try:
        return f"{v:+,.0f}"
    except (TypeError, ValueError):
        return "—"


def _pct(v, digits: int = 1) -> str:
    try:
        return f"{v:+.{digits}f}%"
    except (TypeError, ValueError):
        return "—"


def _tone(v) -> str:
    """green/red/neutral class for a signed number (0 and None are neutral)."""
    try:
        if v > 0:
            return "pos"
        if v < 0:
            return "neg"
    except (TypeError, ValueError):
        pass
    return ""


def _verdict_marker(health_md: str) -> str:
    """The leading ✅/⚠️/🔴 from a health *string*, for the header chip.

    Retained for the Discord-text path / its pinned test; the cockpit now
    reads `health.health_report()["marker"]` directly.
    """
    for mark in ("🔴", "⚠️", "✅"):
        if mark in health_md:
            return mark
    return "•"


# loguru line format from logbuf: "HH:MM:SS | LEVEL | name:line - message".
_LEVELS = ("CRITICAL", "ERROR", "WARNING", "SUCCESS", "INFO", "DEBUG", "TRACE")


def _log_html(lines: list[str]) -> str:
    """Render tail lines as level-coloured monospace rows (pure, testable).

    Unparseable lines (tracebacks, continuations) still render — uncoloured
    — rather than being dropped, so nothing the bot logs goes invisible.
    """
    out: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n")
        lvl = "INFO"
        body_html = html.escape(line)
        parts = line.split(" | ", 2)
        if len(parts) == 3:
            ts, level_tok, rest = parts
            level = level_tok.strip()
            if level in _LEVELS:
                lvl = level
                body_html = (
                    f'<span class="tm">{html.escape(ts)}</span> '
                    f'<span class="lv l-{lvl}">{html.escape(level)}</span> '
                    f'<span>{html.escape(rest)}</span>'
                )
        out.append(f'<div class="ln l-{lvl}">{body_html}</div>')
    return "".join(out) or '<div class="ln l-DEBUG">— no log lines yet —</div>'


# ── mount ───────────────────────────────────────────────────────────────────


def mount(scheduler) -> "asyncio.Task | None":
    """Attach the cockpit and start serving it on the current event loop.

    Returns the uvicorn server task (so the caller can cancel it on
    shutdown), or None when disabled / unavailable. Never raises.
    """
    global _MOUNTED, _scheduler
    if not settings.DASHBOARD_ENABLED:
        logger.info("dashboard disabled (DASHBOARD_ENABLED=false)")
        return None
    if _MOUNTED:
        return None

    try:
        import uvicorn
        from fastapi import FastAPI
        from nicegui import ui

        from . import logbuf

        logbuf.install()
        _scheduler = scheduler

        fastapi_app = FastAPI(title="Sentinel Cockpit")

        # ── v2 dashboard (SvelteKit) — side-by-side with NiceGUI ─────
        # The new frontend lives under /app/* and reads from /api/*.
        # Existing NiceGUI keeps serving / for backward compat until
        # v2 covers every tab; then we'll swap the root route.
        try:
            from .. import api as _api
            from .v2_serve import attach_v2
            fastapi_app.include_router(_api.router)
            attach_v2(fastapi_app)
            logger.info("v2 dashboard mounted at /app + /api")
        except Exception as e:
            logger.warning("v2 dashboard NOT mounted ({}); NiceGUI only", e)

        _build_page(ui)
        ui.run_with(
            fastapi_app,
            title="Sentinel — Cockpit",
            favicon="🛰",
            dark=True,
            show_welcome_message=False,
            storage_secret=_STORAGE_SECRET,
        )

        config = uvicorn.Config(
            fastapi_app,
            host=settings.DASHBOARD_HOST,
            port=settings.DASHBOARD_PORT,
            log_level="warning",
            access_log=False,
        )
        server = uvicorn.Server(config)
        # Don't let uvicorn replace main.py's signal handlers (it would, and
        # then a single Ctrl-C would only stop the web server, not the bot).
        server.install_signal_handlers = lambda: None  # type: ignore[assignment]

        task = asyncio.get_running_loop().create_task(server.serve())
        _MOUNTED = True
        logger.info(
            "dashboard up → http://{}:{}",
            settings.DASHBOARD_HOST,
            settings.DASHBOARD_PORT,
        )
        return task
    except Exception as e:  # never take the bot down for the dashboard
        logger.exception("dashboard mount failed (continuing without it): {}", e)
        return None


# ── layout primitives ───────────────────────────────────────────────────────


class _Panel:
    """A card with a header bar; `with _Panel(...) as body:` adds content
    to the padded body. `span` is the owned grid-span class (`c7`/`c5`/…).

    Header AND body are built inside the card's slot so the card actually
    contains both — otherwise the body lands as a sibling grid cell and the
    header floats above unrelated content.

    `anchor` sets an HTML `id` on the card so the sidebar's `#wallets`-style
    links scroll here. Combined with `.fr-card { scroll-margin-top: 4.5rem }`
    in the stylesheet, the target lands below the sticky header instead of
    under it. Optional — panels without anchors stay un-IDed.
    """

    def __init__(self, ui, title: str, icon: str = "", span: str = "",
                 right_text: str = "", anchor: str = ""):
        self._ui = ui
        self.card = ui.element("div").classes(f"fr-card {span}")
        if anchor:
            # NiceGUI passes plain props through as HTML attributes for
            # non-Quasar elements, which makes `id` settable from Python
            # without dropping into raw `ui.html`.
            self.card.props(f"id={anchor}")
        with self.card:
            ic = f'<span class="ic">{icon}</span>' if icon else ""
            rt = (
                f'<span class="rt">{html.escape(right_text)}</span>'
                if right_text else ""
            )
            ui.html(
                f'<div class="fr-hd">{ic}'
                f'<span class="ti">{html.escape(title)}</span>{rt}</div>'
            ).classes("fr-w")
            self.body = ui.element("div").classes("fr-bd")

    def __enter__(self):
        self._cm = self.body
        self._cm.__enter__()
        return self.body

    def __exit__(self, *exc):
        return self._cm.__exit__(*exc)


def _kpi(ui, label: str):
    """A big headline metric tile. Returns (value_label, sub_label)."""
    with ui.element("div").classes("fr-kpi"):
        ui.html(f'<div class="l">{html.escape(label)}</div>')
        val = ui.label("—").classes("v")
        sub = ui.label("").classes("s mut")
    return val, sub


def _tile(ui, label: str):
    """A small stat tile. Returns the value label."""
    with ui.element("div").classes("fr-tile"):
        ui.html(f'<div class="l">{html.escape(label)}</div>')
        val = ui.label("—").classes("v")
    return val


def _bar_html(pct: float, color: str = "var(--primary)") -> str:
    p = max(0.0, min(100.0, float(pct)))
    return (
        f'<div class="fr-bar"><i style="width:{p:.0f}%;'
        f'background:{color}"></i></div>'
    )


# ── shared helpers for the new chart/feed panels ──────────────────────────


def _aware(t: datetime) -> datetime:
    """SQLAlchemy returns naive datetimes from SQLite; the bot stores UTC.
    Promote on the way out so timestamp arithmetic doesn't compare tz-naive
    and tz-aware values."""
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _ago_short(delta) -> str:
    """Compact 's', 'm', 'h', 'd' duration — fits the feed-row ts column."""
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# Cross-panel state: the Watchlist row click reaches into the ticker chart
# via this callback. Set by `_ticker_chart_panel` on construction; called
# by `_watchlist_panel` on row click. Both panels live in the same page
# render, so the assignment is race-free.
_TICKER_LOAD_CB: "callable | None" = None


# Stable mount for popups (dialog overlays). `ui.dialog()` parents itself
# to the *active slot at construction time* — when a click handler is
# invoked from inside a panel row and creates the dialog there, the
# dialog ends up parented to that row. The next time the panel's refresh
# timer clears its host (e.g. activity feed every 30s), the row goes
# away and the dialog goes with it. That's the "modal closes after a
# random number of seconds" bug. Fix: page-level container that never
# gets cleared; dialog openers `with _MODAL_PARENT: ui.dialog()`.
_MODAL_PARENT = None  # set by `cockpit()` once per page render


def _swap_chart(chart, spec: dict) -> None:
    """Replace an EChart's options in place.

    NiceGUI 3.12's `ui.echart` exposes `options` as a *read-only* property
    (`@property def options(self): return self._props['options']`) — direct
    assignment raises `property has no setter`. The supported pattern is to
    mutate the underlying dict and then call `.update()` so the diff ships
    to the client. We keep the dict identity (clear + update) rather than
    poking `_props` so we don't depend on private state.
    """
    opts = chart.options
    opts.clear()
    opts.update(spec)
    chart.update()


# ── equity curve (Overview tab) ────────────────────────────────────────────


def _equity_curve_panel(ui, span: str = "c7") -> None:
    """Multi-line equity curve, one line per active fund. Empty `points`
    → "no equity history yet" — fresh DBs render cleanly."""
    from .. import funds
    from . import charts

    with _Panel(ui, "Wallet equity (30d)", "📈", span,
                anchor="equity-curve"):
        chart = ui.echart({}).classes("fr-chart")

    async def refresh() -> None:
        try:
            data = await asyncio.to_thread(funds.equity_curve, None, 30)
        except Exception as e:
            logger.debug("equity_curve_panel: {}", e)
            return
        try:
            _swap_chart(chart, charts.equity_curve_spec(data))
        except Exception as e:
            logger.debug("equity chart render: {}", e)

    _tick_now(ui, refresh, _i("equity_curve"), tab="overview")


# ── realised P&L curve (Overview tab) ──────────────────────────────────────


def _realized_curve_panel(ui, span: str = "c5") -> None:
    """Cumulative realised P&L line — one point per closed trade. Empty
    list → placeholder."""
    from .. import portfolio
    from . import charts

    with _Panel(ui, "Realized P&L cumulative", "💰", span,
                anchor="realized-curve"):
        chart = ui.echart({}).classes("fr-chart")

    async def refresh() -> None:
        try:
            pts = await asyncio.to_thread(portfolio.realized_curve)
        except Exception as e:
            logger.debug("realized_curve_panel: {}", e)
            return
        try:
            _swap_chart(chart, charts.realized_curve_spec(pts))
        except Exception as e:
            logger.debug("realized chart render: {}", e)

    _tick_now(ui, refresh, _i("realized_curve"), tab="overview")


# ── ticker chart (Markets tab) — the star feature ─────────────────────────


def _ticker_chart_panel(ui, span: str = "c12") -> None:
    """Candlestick + volume + entry/exit markers for any ticker. The ticker
    is picked via the input or by clicking a chip (current open positions);
    other panels (Watchlist) can call `_TICKER_LOAD_CB` to push a ticker in.

    Range picker (1w / 1m / 3m / 6m / 1y / all) lives next to the ticker
    input and just re-calls `load()` with a different `days=` value —
    `all` passes `days=None` to fetch the full PriceBar history. State is
    held in `state["current"]` (ticker) and `state["days"]` (range).

    Layout: chart (flex) | stats card (17rem). Stacks below the chart on
    narrow viewports — the CSS grid auto-collapses past 900px."""
    global _TICKER_LOAD_CB
    from .. import portfolio
    from . import charts

    # Range presets — value is the `days` arg to position_chart; None = all.
    _RANGES: tuple[tuple[str, int | None], ...] = (
        ("1w", 7),
        ("1m", 30),
        ("3m", 90),
        ("6m", 180),
        ("1y", 365),
        ("All", None),
    )
    state: dict = {"current": "", "days": 30}  # default 1m

    with _Panel(ui, "Ticker chart", "📈", span, anchor="ticker-chart"):
        with ui.element("div").classes("fr-pick"):
            ui.label("Ticker").classes("label")
            box = ui.input(placeholder="e.g. NVDA").props(
                "dense outlined dark"
            ).style("width:11rem")
            chips_box = ui.element("div").classes("chips")
        with ui.element("div").classes("fr-pick"):
            ui.label("Range").classes("label")
            range_box = ui.element("div").classes("chips")

        wrap = ui.element("div").style(
            "display:grid;grid-template-columns:1fr 17rem;"
            "gap:1rem;align-items:start"
        )
        with wrap:
            chart = ui.echart({}).classes("fr-chart tall")
            stats_host = ui.element("div").classes("fr-pos-summary")

    async def load(ticker: str | None = None,
                   days: int | None | object = ...) -> None:
        """Re-render the chart. Either arg can be omitted (`...`) to keep
        the current value — so a range chip click only changes the days,
        and a ticker input only changes the ticker."""
        if ticker is not None:
            t = (ticker or "").strip().upper().lstrip("$")
            if not t:
                return
            state["current"] = t
        if days is not ...:
            state["days"] = days   # may be None for "All"

        tk = state["current"]
        if not tk:
            return
        try:
            d = await asyncio.to_thread(
                portfolio.position_chart, tk, state["days"]
            )
        except Exception as e:
            ui.notify(f"chart load failed: {e}", type="negative")
            return
        try:
            _swap_chart(chart, charts.candlestick_spec(d))
        except Exception as e:
            logger.debug("candle render: {}", e)
        _render_stats(stats_host, tk, d)
        # repaint active states on chips since `state` just changed
        _paint_chips()

    async def _on_enter(_e=None) -> None:
        await load(ticker=(box.value or "").strip())

    box.on("keydown.enter", _on_enter)

    # Watchlist rows call `_TICKER_LOAD_CB(ticker)` (single positional arg);
    # forward into `load(ticker=…)` without touching the days state.
    async def _load_from_external(t: str) -> None:
        await load(ticker=t)

    _TICKER_LOAD_CB = _load_from_external

    def _paint_chips() -> None:
        # Render the ticker chips + range chips with `active` reflecting
        # current state. Cheap, no LLM/DB work.
        chips_box.clear()
        opens = state.get("_opens") or []
        with chips_box:
            for tk in opens[:8]:
                chip_cls = (
                    "chip active" if tk == state["current"] else "chip"
                )
                ui.html(
                    f'<span class="{chip_cls}">${html.escape(tk)}</span>'
                ).on("click", lambda _e, t=tk: load(ticker=t))
            if not opens:
                for tk in ("SPY", "QQQ", "BTC", "ETH"):
                    ui.html(
                        f'<span class="chip">${html.escape(tk)}</span>'
                    ).on("click", lambda _e, t=tk: load(ticker=t))
        range_box.clear()
        with range_box:
            for label, d in _RANGES:
                chip_cls = (
                    "chip active" if d == state["days"] else "chip"
                )
                ui.html(
                    f'<span class="{chip_cls}">{label}</span>'
                ).on("click", lambda _e, dd=d: load(days=dd))

    async def _refresh_chips() -> None:
        # Pull open positions for the ticker quick-pick chips. Cached in
        # state so `_paint_chips` doesn't need its own DB call.
        try:
            opens = await asyncio.to_thread(portfolio.open_positions)
            state["_opens"] = [p["ticker"] for p in opens]
        except Exception:
            state["_opens"] = []
        _paint_chips()

    _tick_now(ui, _refresh_chips, _i("ticker_chips"), tab="markets")


def _render_stats(host, ticker: str, d: dict) -> None:
    """TradingView-style right-rail stat card. Shows (in order):

    - Big ticker symbol + asset class chip
    - Last price + 1d % change (colour-coded)
    - Day's range bar (low → last → high), 1d / 5d % cluster
    - 52-week range bar
    - Volume vs 20d avg
    - Open paper position summary, if any
    - Closed-trade tally in the window

    All numbers derive from `d` (which is `position_chart` output) plus a
    `portfolio.ticker_stats(ticker)` overlay for the 52w + day-range info.
    The card is rebuilt fully each load so refs to stale labels can't leak."""
    from nicegui import ui
    from .. import portfolio
    host.clear()
    ctx = d.get("context") or {}
    last = ctx.get("last_price")
    change_1d = ctx.get("change_1d_pct")
    change_5d = ctx.get("change_5d_pct")
    vva = ctx.get("volume_vs_20d_avg")

    # Pull richer day-range / 52w from PriceBar; tolerate missing rows.
    try:
        stats = portfolio.ticker_stats(ticker) or {}
    except Exception:
        stats = {}

    with host:
        # ── Title row: $TICKER + asset class chip ────────────────────────
        asset_class = (stats.get("asset_class") or "").lower()
        # We don't have asset_class on ticker_stats yet — pull from chart
        # context if possible; otherwise blank. Best-effort.
        with ui.element("div").style(
            "display:flex;align-items:baseline;gap:.45rem;"
            "margin-bottom:.35rem"
        ):
            ui.html(
                f'<span style="font-family:ui-monospace,Menlo,monospace;'
                f'font-weight:700;font-size:18px;letter-spacing:.01em">'
                f'${html.escape(ticker)}</span>'
            )
            if asset_class:
                ui.html(
                    f'<span class="fr-pill idle" '
                    f'style="font-size:9.5px;padding:.12rem .45rem">'
                    f'{html.escape(asset_class)}</span>'
                )

        # ── Big price + 1d % ─────────────────────────────────────────────
        if last is not None:
            ui.html(
                f'<div class="big {_tone(change_1d)}">{last:.4g}</div>'
            )
            sub_bits = []
            if change_1d is not None:
                sub_bits.append(
                    f'<span class="{_tone(change_1d)}">'
                    f'{_pct(change_1d)} 1d</span>'
                )
            if change_5d is not None:
                sub_bits.append(
                    f'<span class="{_tone(change_5d)}">'
                    f'{_pct(change_5d)} 5d</span>'
                )
            if sub_bits:
                ui.html(
                    '<div style="font-size:11.5px;color:var(--muted);'
                    'font-variant-numeric:tabular-nums">'
                    + " · ".join(sub_bits)
                    + "</div>"
                )
        else:
            ui.label(
                f"No price context for ${ticker}"
            ).classes("fnt").style("font-size:12px")

        # ── Range bars: day + 52w ────────────────────────────────────────
        day_low = stats.get("day_low")
        day_high = stats.get("day_high")
        if day_low is not None and day_high is not None and last is not None \
                and day_high > day_low:
            ui.element("div").style(
                "height:1px;background:var(--border);margin:.55rem 0 .35rem"
            )
            _range_row(ui, "Day range", day_low, last, day_high)
        hi52 = stats.get("high_52w")
        lo52 = stats.get("low_52w")
        if hi52 is not None and lo52 is not None and last is not None \
                and hi52 > lo52:
            _range_row(ui, "52w range", lo52, last, hi52)

        # ── Volume row ───────────────────────────────────────────────────
        vol = stats.get("volume")
        avg_vol = stats.get("avg_volume_20d")
        if vol is not None or vva is not None:
            ui.element("div").style(
                "height:1px;background:var(--border);margin:.55rem 0 .35rem"
            )
            if vol is not None:
                ui.html(
                    f'<div class="row" style="border:0;padding:0;'
                    f'font-size:12px">'
                    f'<span class="k">Volume</span>'
                    f'<span class="v">{_humansize(vol)}</span></div>'
                )
            if avg_vol is not None:
                ui.html(
                    f'<div class="row" style="border:0;padding:0;'
                    f'font-size:12px">'
                    f'<span class="k">Avg 20d</span>'
                    f'<span class="v">{_humansize(avg_vol)}</span></div>'
                )
            if vva is not None:
                tone = ("pos" if vva >= 1.8
                        else "neg" if vva < 0.5 else "mut")
                ui.html(
                    f'<div class="row" style="border:0;padding:0;'
                    f'font-size:12px">'
                    f'<span class="k">Today vs avg</span>'
                    f'<span class="v {tone}">×{vva:.2f}</span></div>'
                )

        # ── Open position ────────────────────────────────────────────────
        op = d.get("open_position")
        ui.element("div").style(
            "height:1px;background:var(--border);margin:.55rem 0 .35rem"
        )
        if op:
            side_lbl = op["side"].upper()
            side_cls = "pos" if op["side"] == "long" else "neg"
            ui.html(
                f'<div class="row" style="font-size:12.5px">'
                f'<span class="k">Position</span>'
                f'<span class="v {side_cls}">{side_lbl} {op["qty"]:g}</span>'
                "</div>"
            )
            ui.html(
                f'<div class="row" style="font-size:12.5px">'
                f'<span class="k">Entry</span>'
                f'<span class="v">{op["entry"]:.4g}</span></div>'
            )
            if op.get("pnl") is not None:
                pnl_label = _susd(op["pnl"])
                if op.get("pnl_pct") is not None:
                    pnl_label += f' ({_pct(op["pnl_pct"])})'
                ui.html(
                    f'<div class="row" style="font-size:12.5px">'
                    f'<span class="k">uPnL</span>'
                    f'<span class="v {_tone(op["pnl"])}">'
                    f'{pnl_label}</span></div>'
                )
        else:
            ui.html(
                '<div class="row" style="font-size:12.5px">'
                '<span class="k">Position</span>'
                '<span class="v fnt">none open</span></div>'
            )

        closed = d.get("closed") or []
        if closed:
            wins = sum(1 for c in closed if (c.get("pnl") or 0) > 0)
            total = sum(c.get("pnl") or 0 for c in closed)
            ui.html(
                f'<div class="row" style="font-size:12.5px">'
                f'<span class="k">Closed (window)</span>'
                f'<span class="v">{wins}/{len(closed)} won · '
                f'{_susd(total)}</span></div>'
            )

        # ── Data scope footer (tells the user how deep history goes) ─────
        bars_n = stats.get("bars_count")
        earliest = stats.get("earliest_bar")
        if bars_n:
            ui.element("div").style(
                "height:1px;background:var(--border);margin:.55rem 0 .35rem"
            )
            scope_label = f"{bars_n} bars"
            if earliest:
                scope_label += f" · from {earliest[:10]}"
            ui.html(
                f'<div class="fnt" style="font-size:10.5px;'
                f'letter-spacing:.05em">{html.escape(scope_label)}</div>'
            )


def _range_row(ui, label: str, lo: float, here: float, hi: float) -> None:
    """A min──●──max range visualisation (TV-style). `here` is the current
    price; we render it as a marker between `lo` (left) and `hi` (right).
    Used for both the day range and the 52-week range."""
    span = hi - lo
    if span <= 0:
        return
    pct = max(0.0, min(100.0, (here - lo) / span * 100))
    ui.html(
        '<div class="row" style="border:0;padding:0;font-size:11.5px;'
        'margin-top:.25rem">'
        f'<span class="k">{html.escape(label)}</span>'
        '<span class="v" style="display:flex;align-items:center;gap:.4rem">'
        f'<span class="fnt num">{lo:.4g}</span>'
        '<span style="position:relative;height:4px;width:6rem;'
        'background:var(--surface2);border-radius:2px;'
        'border:1px solid var(--border)">'
        f'<i style="position:absolute;left:{pct:.0f}%;top:50%;'
        'transform:translate(-50%,-50%);width:7px;height:7px;'
        'border-radius:50%;background:var(--primary);'
        'box-shadow:0 0 6px rgba(102,153,255,.55)"></i>'
        '</span>'
        f'<span class="num">{hi:.4g}</span>'
        '</span></div>'
    )


def _humansize(n: int | float | None) -> str:
    """K/M/B short formatting for volume cells. Not a measurement of
    bytes — same shape, but for share counts."""
    if n is None:
        return "—"
    n = float(n)
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,.0f}"


# ── watchlist (Markets tab) ────────────────────────────────────────────────


def _watchlist_panel(ui, span: str = "c12") -> None:
    """Watchlist with multi-period returns (1d/1w/1m/1y %) and built-in
    sort/paginate/search via Quasar's q-table. Click a row → load that
    ticker into the chart above via `_TICKER_LOAD_CB`.

    Returns are computed once per refresh by `portfolio.watchlist_returns`
    (single batch query over PriceBar + PriceContext). 1d % comes from
    PriceContext (already normalised by the price ingester); 1w/1m/1y are
    derived from PriceBar with a small day-window tolerance to dodge
    weekends/holidays."""
    from .. import portfolio

    # Per-% column renderer. `props.value` is the numeric pct; we color by
    # sign and clamp display to 2 decimals. Null → em-dash without colour.
    _PCT_SLOT = """
        <q-td :props="props" style="text-align:right;
            font-variant-numeric:tabular-nums">
            <span :style="`color: ${
                props.value === null || props.value === undefined ? 'inherit'
                : (props.value > 0 ? '#3ddc97'
                : (props.value < 0 ? '#ff6b6b' : 'inherit'))}`">
                {{ props.value === null || props.value === undefined ? '—'
                : ((props.value > 0 ? '+' : '') +
                   props.value.toFixed(2) + '%') }}
            </span>
        </q-td>
    """
    _VOL_SLOT = """
        <q-td :props="props" style="text-align:right;
            font-variant-numeric:tabular-nums">
            <span :style="`color: ${
                props.value === null || props.value === undefined ? 'inherit'
                : (props.value >= 1.8 ? '#3ddc97'
                : (props.value < 0.5 ? 'rgba(255,255,255,.42)'
                : 'rgba(255,255,255,.62)'))}`">
                {{ props.value === null || props.value === undefined ? '—'
                : ('×' + props.value.toFixed(2)) }}
            </span>
        </q-td>
    """
    _TICKER_SLOT = """
        <q-td :props="props" style="text-align:left">
            <span style="font-family:ui-monospace,Menlo,monospace;
                font-weight:600;color:#f0f0f1">
                ${{ props.value }}
            </span>
        </q-td>
    """
    _LAST_SLOT = """
        <q-td :props="props" style="text-align:right;
            font-variant-numeric:tabular-nums">
            {{ props.value === null || props.value === undefined ? '—'
                : props.value.toFixed(props.value < 10 ? 4 : 2) }}
        </q-td>
    """

    columns = [
        {"name": "ticker", "label": "Ticker", "field": "ticker",
         "sortable": True, "align": "left"},
        {"name": "asset_class", "label": "Class", "field": "asset_class",
         "sortable": True, "align": "left"},
        {"name": "last_price", "label": "Last", "field": "last_price",
         "sortable": True, "align": "right"},
        {"name": "change_1d_pct", "label": "1d %",
         "field": "change_1d_pct", "sortable": True, "align": "right"},
        {"name": "change_1w_pct", "label": "1w %",
         "field": "change_1w_pct", "sortable": True, "align": "right"},
        {"name": "change_1m_pct", "label": "1m %",
         "field": "change_1m_pct", "sortable": True, "align": "right"},
        {"name": "change_1y_pct", "label": "1y %",
         "field": "change_1y_pct", "sortable": True, "align": "right"},
        {"name": "volume_vs_avg", "label": "Vol×",
         "field": "volume_vs_avg", "sortable": True, "align": "right"},
    ]

    with _Panel(ui, "Watchlist", "📋", span, anchor="watchlist"):
        # Search input above the table — bound to the table's `filter`
        # property so Quasar filters client-side without a round-trip.
        with ui.element("div").classes("fr-row").style(
            "gap:.5rem;margin-bottom:.55rem;align-items:center"
        ):
            search = ui.input(
                placeholder="Filter…  (ticker / class)"
            ).props("dense outlined dark clearable").style("width:18rem")
            empty_label = ui.label("").classes("fnt").style(
                "font-size:11px;margin-left:auto"
            )

        table = ui.table(
            columns=columns, rows=[], row_key="ticker", pagination=20,
        ).props("dark dense flat").classes("fr-w")

        table.add_slot("body-cell-ticker", _TICKER_SLOT)
        table.add_slot("body-cell-last_price", _LAST_SLOT)
        table.add_slot("body-cell-change_1d_pct", _PCT_SLOT)
        table.add_slot("body-cell-change_1w_pct", _PCT_SLOT)
        table.add_slot("body-cell-change_1m_pct", _PCT_SLOT)
        table.add_slot("body-cell-change_1y_pct", _PCT_SLOT)
        table.add_slot("body-cell-volume_vs_avg", _VOL_SLOT)

        # Bind search → table filter (Quasar's text filter is whole-row,
        # case-insensitive substring across all visible columns).
        table.bind_filter_from(search, "value")

        # Row click → load the ticker into the chart panel.
        # The Quasar `row-click` event signature on the JS side is
        # `(evt, row, index)`; NiceGUI surfaces `e.args` as `[evt, row, idx]`.
        def _on_row_click(e) -> None:
            row = (e.args[1] if isinstance(e.args, list) and len(e.args) >= 2
                   else e.args.get("row") if isinstance(e.args, dict) else None)
            if row and "ticker" in row:
                _click_load(row["ticker"])
        table.on("row-click", _on_row_click)

    async def refresh() -> None:
        try:
            rows = await asyncio.to_thread(portfolio.watchlist_returns)
        except Exception as e:
            empty_label.set_text(f"unavailable: {e}")
            return
        table.rows = rows
        table.update()
        if rows:
            empty_label.set_text(f"{len(rows)} symbol(s)")
        else:
            empty_label.set_text(
                "Watchlist is empty — !add a ticker to start tracking."
            )

    _tick_now(ui, refresh, _i("watchlist"), tab="markets")


def _click_load(ticker: str) -> None:
    """Watchlist row click → load the ticker into the chart panel (and
    switch to the Markets tab in case the user is elsewhere)."""
    cb = _TICKER_LOAD_CB
    if cb is None:
        return
    try:
        asyncio.create_task(cb(ticker))
    except Exception:
        pass
    # ensure the user lands on the Markets tab if they clicked from a
    # cross-tab surface (the watchlist currently lives in Markets, but this
    # keeps the wiring robust if it later moves to Overview)
    from nicegui import ui as _ui
    try:
        _ui.run_javascript(
            'if (location.hash !== "#markets") location.hash = "#markets";'
        )
    except Exception:
        pass


# ── filings feed (Intel tab) ───────────────────────────────────────────────


def _filings_feed_panel(ui, span: str = "c6") -> None:
    """Last ~48h of filings — form type · summary · ticker · age. Click-
    through opens the SEC document. Materiality score shown when present."""
    from ..db import session_scope
    from ..models import Filing
    from sqlmodel import select

    with _Panel(ui, "Recent filings (48h)", "📑", span,
                anchor="filings-feed"):
        host = ui.element("div").classes("fr-feed")

    def _load() -> list[dict]:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48))
        cutoff_naive = cutoff.replace(tzinfo=None)
        with session_scope() as s:
            rows = s.exec(
                select(Filing)
                .where(Filing.filed_at >= cutoff_naive)
                .order_by(Filing.filed_at.desc())
                .limit(40)
            ).all()
        return [
            {
                "ticker": r.ticker, "form": r.form_type,
                "summary": r.summary, "url": r.primary_doc_url,
                "ts": _aware(r.filed_at).isoformat(),
                "score": r.materiality_score,
            } for r in rows
        ]

    async def refresh() -> None:
        try:
            rows = await asyncio.to_thread(_load)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"feed unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            if not rows:
                ui.label("No filings in last 48h.").classes("mut").style(
                    "font-size:13px"
                )
                return
            now = datetime.now(timezone.utc)
            for r in rows:
                ts = datetime.fromisoformat(r["ts"])
                ago = _ago_short(now - ts)
                tk = (r.get("ticker") or "—").upper()
                title = (r.get("summary") or r["form"]).strip() or r["form"]
                with ui.element("div").classes("fr-feed-row"):
                    ui.html(
                        f'<span class="kind filing">'
                        f'{html.escape(r["form"][:8])}</span>'
                    )
                    with ui.element("div").classes("body"):
                        ui.html(
                            f'<a href="{html.escape(r["url"])}" '
                            f'target="_blank" rel="noopener">'
                            f'{html.escape(title[:140])}</a>'
                        )
                        meta = [
                            f'<span class="tk">${html.escape(tk)}</span>'
                        ]
                        if r.get("score") is not None:
                            meta.append(f'score {r["score"]}/10')
                        ui.html(
                            '<div class="meta">' + " · ".join(meta)
                            + "</div>"
                        )
                    ui.html(f'<span class="ts">{ago}</span>')

    _tick_now(ui, refresh, _i("filings_feed"), tab="intel")


# ── news feed (Intel tab) ──────────────────────────────────────────────────


def _news_feed_panel(ui, span: str = "c6") -> None:
    """Last ~24h of news, rendered as a card grid (Wave 2 redesign).

    Each card carries: kind chip (sentiment-coloured), ticker tag,
    source name, impact-1d if measured, headline (clamped to 3 lines),
    summary excerpt (2 lines), and an action row with **AI summary**
    (opens cached dossier modal) and **Open article** (external link).

    Card click opens the AI dossier — the dominant user intent. The
    Open Article button stops propagation so clicking it doesn't ALSO
    open the modal."""
    from ..db import session_scope
    from ..models import NewsItem
    from sqlmodel import select

    with _Panel(ui, "Recent news (24h)", "📰", span,
                anchor="news-feed"):
        host = ui.element("div").classes("fr-w")

    def _load() -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        cutoff_naive = cutoff.replace(tzinfo=None)
        with session_scope() as s:
            rows = s.exec(
                select(NewsItem)
                .where(NewsItem.published_at >= cutoff_naive)
                .order_by(NewsItem.published_at.desc())
                .limit(50)
            ).all()
        return [
            {
                "id": r.id,
                "ticker": r.ticker, "title": r.title, "url": r.url,
                "source": r.source, "summary": r.summary,
                "ts": _aware(r.published_at).isoformat(),
                "impact_1d": r.impact_1d_pct,
                "sentiment": r.sentiment,
            } for r in rows
        ]

    async def refresh() -> None:
        try:
            rows = await asyncio.to_thread(_load)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"feed unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            if not rows:
                ui.label("No news in last 24h.").classes("mut").style(
                    "font-size:13px"
                )
                return
            now = datetime.now(timezone.utc)
            with ui.element("div").classes("fr-card-grid"):
                for r in rows:
                    _render_news_card(ui, r, now)

    _tick_now(ui, refresh, _i("news_feed"), tab="intel")


def _render_news_card(ui, r: dict, now: datetime) -> None:
    """One news card. Pulled out so future tweaks (badge for cached
    dossier, hot/cold tag, etc.) live in one place."""
    ts = datetime.fromisoformat(r["ts"])
    ago = _ago_short(now - ts)
    tk = (r.get("ticker") or "").upper()
    sent = r.get("sentiment") or 0
    chip_cls = (
        "news bull" if sent > 0
        else "news bear" if sent < 0
        else "news"
    )
    chip_label = "NEWS" if not tk else (
        "BULL" if sent > 0 else "BEAR" if sent < 0 else "NEWS"
    )

    with ui.element("div").classes("fr-news-card").on(
        "click", lambda _e, nid=r["id"]: _open_news_dialog(ui, nid),
    ):
        # ── meta row ──
        ui.html(
            f'<div class="card-meta">'
            f'<span class="kind {chip_cls}">{chip_label}</span>'
            f'<span>{html.escape((r.get("source") or "")[:30])}</span>'
            f'<span class="ts">{ago}</span></div>'
        )
        # ── title ──
        ui.html(
            f'<div class="card-title">'
            f'{html.escape((r.get("title") or "")[:200])}</div>'
        )
        # ── tags ──
        tag_bits = []
        if tk:
            tag_bits.append(f'<span class="tk">${html.escape(tk)}</span>')
        if r.get("impact_1d") is not None:
            tone = _tone(r["impact_1d"])
            tag_bits.append(
                f'<span class="delta {tone}">'
                f'{_pct(r["impact_1d"])} 1d</span>'
            )
        if tag_bits:
            ui.html(
                '<div class="card-tags">' + "".join(tag_bits) + '</div>'
            )
        # ── summary excerpt (if non-trivial) ──
        summary = (r.get("summary") or "").strip()
        if summary and len(summary) > 40:
            ui.html(
                f'<div class="card-excerpt">'
                f'{html.escape(summary[:260])}</div>'
            )
        # ── actions ──
        url = r.get("url") or ""
        url_safe = html.escape(url) if url else ""
        ui.html(
            '<div class="card-actions">'
            '<span class="btn primary">✨ AI summary</span>'
            + (f'<a class="btn" href="{url_safe}" '
               f'target="_blank" rel="noopener" '
               f'onclick="event.stopPropagation()">↗ Open article</a>'
               if url else "")
            + '</div>'
        )


# ── social pulse (Intel tab) ───────────────────────────────────────────────


def _social_pulse_panel(ui, span: str = "c12") -> None:
    """Tickers the crowd is suddenly louder about. Backed by `SocialPulse`
    (the social_pulse pipeline writes one row per "mention-count surge
    above baseline" event). Sorted by ratio descending so the biggest
    spikes sit on top; bot's summary is the LLM gloss of *what* they're
    talking about."""
    from ..db import session_scope
    from ..models import SocialPulse
    from sqlmodel import select

    with _Panel(ui, "Social pulse (48h)", "📣", span,
                anchor="social-pulse"):
        host = ui.element("div").classes("fr-feed")

    def _load() -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        cutoff_naive = cutoff.replace(tzinfo=None)
        with session_scope() as s:
            rows = s.exec(
                select(SocialPulse)
                .where(SocialPulse.created_at >= cutoff_naive)
                .order_by(SocialPulse.ratio.desc())
                .limit(30)
            ).all()
        return [{
            "ticker": r.ticker,
            "ratio": r.ratio,
            "mentions": r.mention_count,
            "baseline": r.baseline,
            "summary": r.summary,
            "ts": _aware(r.created_at).isoformat(),
        } for r in rows]

    async def refresh() -> None:
        try:
            rows = await asyncio.to_thread(_load)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"social pulse unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            if not rows:
                ui.label(
                    "No mention surges in last 48h — quiet crowd."
                ).classes("mut").style("font-size:13px")
                return
            now = datetime.now(timezone.utc)
            for r in rows:
                ts = datetime.fromisoformat(r["ts"])
                ago = _ago_short(now - ts)
                tk = (r.get("ticker") or "—").upper()
                ratio = r.get("ratio") or 0
                with ui.element("div").classes("fr-feed-row"):
                    ui.html('<span class="kind pulse">PULSE</span>')
                    with ui.element("div").classes("body"):
                        ui.html(html.escape(r.get("summary") or "")[:200])
                        meta = [
                            f'<span class="tk">${html.escape(tk)}</span>',
                            f'×{ratio:.1f} vs baseline',
                            f'{r["mentions"]} mentions',
                        ]
                        ui.html(
                            '<div class="meta">' + " · ".join(meta)
                            + "</div>"
                        )
                    ui.html(f'<span class="ts">{ago}</span>')

    _tick_now(ui, refresh, _i("social_pulse"), tab="intel")


# ── catalyst calendar (Intel tab) ──────────────────────────────────────────


def _catalysts_panel(ui, span: str = "c12") -> None:
    """Upcoming earnings (next 14 days), grouped by date. Stale cache
    entries are excluded by the accessor — better to omit a date than
    show one the upstream source has since shifted."""
    from .. import earnings

    with _Panel(ui, "Catalyst calendar (14d)", "🗓", span,
                anchor="catalysts"):
        host = ui.element("div").classes("fr-w")

    async def refresh() -> None:
        try:
            rows = await asyncio.to_thread(earnings.upcoming, 14)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"calendar unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            if not rows:
                ui.label(
                    "No upcoming earnings cached. The catalyst pipeline "
                    "refreshes daily — check back tomorrow."
                ).classes("mut").style("font-size:13px")
                return
            # group by date
            from collections import OrderedDict
            from datetime import date
            grouped: OrderedDict[str, list[dict]] = OrderedDict()
            for r in rows:
                grouped.setdefault(r["report_date"], []).append(r)
            today = date.today()
            for d_str, items in grouped.items():
                dt = date.fromisoformat(d_str)
                days_out = (dt - today).days
                pretty = dt.strftime("%a %b %d")
                out_lbl = ("today" if days_out == 0
                           else "tomorrow" if days_out == 1
                           else f"in {days_out}d")
                with ui.element("div").classes("fr-cal-day"):
                    ui.label(pretty)
                    ui.html(f'<span class="day-out">{out_lbl}</span>')
                for r in items:
                    with ui.element("div").classes("fr-cal-row"):
                        ui.html(
                            f'<span class="tk">${html.escape(r["ticker"])}'
                            "</span>"
                        )
                        ui.html('<span class="mut">earnings report</span>')

    _tick_now(ui, refresh, _i("catalysts"), tab="intel")


# ── recent activity feed (Overview tab) ────────────────────────────────────


def _activity_panel(ui, span: str = "c12") -> None:
    """Most-recent activity across the bot: TradingCalls + Filings + News
    in one chronological stream, capped at 40. The bot is always *doing
    something* — this surface makes that visible."""
    from ..db import session_scope
    from ..models import Filing, NewsItem, TradingCall
    from sqlmodel import select

    with _Panel(ui, "Recent activity (48h)", "⚡", span,
                anchor="activity"):
        host = ui.element("div").classes("fr-feed")

    def _load() -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
        cutoff_naive = cutoff.replace(tzinfo=None)
        items: list[dict] = []
        with session_scope() as s:
            for c in s.exec(
                select(TradingCall)
                .where(TradingCall.created_at >= cutoff_naive)
                .order_by(TradingCall.created_at.desc())
                .limit(25)
            ).all():
                items.append({
                    "kind": "call", "id": c.id, "ticker": c.ticker,
                    "ts": _aware(c.created_at).isoformat(),
                    "title": (c.thesis or "")[:160],
                    "side": c.direction, "src": c.source,
                    "conv": c.conviction, "url": None,
                })
            for f in s.exec(
                select(Filing)
                .where(Filing.filed_at >= cutoff_naive)
                .order_by(Filing.filed_at.desc())
                .limit(20)
            ).all():
                items.append({
                    "kind": "filing", "ticker": f.ticker,
                    "ts": _aware(f.filed_at).isoformat(),
                    "title": ((f.summary or f.form_type) or "")[:160],
                    "form": f.form_type, "url": f.primary_doc_url,
                })
            for n in s.exec(
                select(NewsItem)
                .where(NewsItem.published_at >= cutoff_naive)
                .order_by(NewsItem.published_at.desc())
                .limit(20)
            ).all():
                items.append({
                    "kind": "news", "id": n.id, "ticker": n.ticker,
                    "ts": _aware(n.published_at).isoformat(),
                    "title": (n.title or "")[:160],
                    "url": n.url, "src": n.source,
                })
        items.sort(key=lambda x: x["ts"], reverse=True)
        return items[:40]

    async def refresh() -> None:
        try:
            items = await asyncio.to_thread(_load)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"activity unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            if not items:
                ui.label(
                    "No activity in the last 48h."
                ).classes("mut").style("font-size:13px")
                return
            now = datetime.now(timezone.utc)
            for it in items:
                ts = datetime.fromisoformat(it["ts"])
                ago = _ago_short(now - ts)
                tk = (it.get("ticker") or "—").upper()
                kind = it["kind"]
                if kind == "call":
                    label = (it.get("side") or "").upper() or "CALL"
                    kind_cls = ("call short" if it.get("side") == "short"
                                else "call")
                elif kind == "filing":
                    label = (it.get("form") or "FILING")[:8]
                    kind_cls = "filing"
                else:
                    label = "NEWS"
                    kind_cls = "news"
                # Calls + news open their AI dossier; filings open the SEC
                # document directly (no LLM dossier for raw filings yet —
                # the filings pipeline already writes its own summary).
                if kind == "call" and it.get("id"):
                    row = ui.element("div").classes(
                        "fr-feed-row"
                    ).style("cursor:pointer").on(
                        "click",
                        lambda _e, cid=it["id"]: _open_call_dialog(ui, cid),
                    )
                elif kind == "news" and it.get("id"):
                    row = ui.element("div").classes(
                        "fr-feed-row"
                    ).style("cursor:pointer").on(
                        "click",
                        lambda _e, nid=it["id"]: _open_news_dialog(ui, nid),
                    )
                else:
                    row = ui.element("div").classes("fr-feed-row")

                with row:
                    ui.html(
                        f'<span class="kind {kind_cls}">'
                        f'{html.escape(label)}</span>'
                    )
                    with ui.element("div").classes("body"):
                        url = it.get("url")
                        title_html = html.escape(it.get("title") or "")
                        if kind == "filing" and url:
                            # Filings still link directly — no dossier yet
                            ui.html(
                                f'<a href="{html.escape(url)}" '
                                f'target="_blank" rel="noopener">'
                                f'{title_html}</a>'
                            )
                        else:
                            # Call / news → the row click opens the dialog,
                            # title is plain text to make that single intent
                            ui.html(
                                f'<span style="color:var(--text)">'
                                f'{title_html or "(no title)"}</span>'
                            )
                        meta = [f'<span class="tk">${html.escape(tk)}</span>']
                        if kind == "call":
                            meta.append(html.escape(it.get("src") or ""))
                            meta.append(f'conv {it.get("conv") or 0}/5')
                        elif kind == "news":
                            src = html.escape((it.get("src") or "")[:30])
                            if src:
                                meta.append(src)
                        ui.html(
                            '<div class="meta">' + " · ".join(meta)
                            + "</div>"
                        )
                    ui.html(f'<span class="ts">{ago}</span>')

    _tick_now(ui, refresh, _i("activity"), tab="overview")


# ── LLM grounding card (System tab) ────────────────────────────────────────


def _grounding_panel(ui, span: str = "c12") -> None:
    """Show the current LLM grounding preamble — the date-stamped trust
    rules + world anchor that's prepended to every reasoning call.

    Refreshes every 5 minutes which lines up with the preamble's own
    cache TTL, so what's rendered here is what calls are actually using.
    A "Reload" button bypasses the cache for the next call (used after
    editing `config/world_anchor.yaml` on the Pi without restarting)."""

    with _Panel(ui, "LLM grounding", "🌐", span, anchor="grounding"):
        host = ui.element("div").classes("fr-w")
        with ui.element("div").classes("fr-row").style(
            "margin-top:.6rem;gap:.5rem"
        ):
            ui.button(
                "Reload anchor",
                on_click=lambda: asyncio.create_task(_force_refresh()),
            ).props("flat dense size=sm color=primary").style(
                "font-size:10px"
            )
            ui.html(
                '<span class="fnt" style="font-size:10.5px">'
                'Hot-edit <code>config/world_anchor.yaml</code> on the Pi '
                '— the bot picks it up on the next call (cache TTL 5min) '
                'or sooner if you hit reload.</span>'
            )

    async def _force_refresh() -> None:
        from .. import grounding as _g
        _g.reset_cache()
        await refresh()
        ui.notify("anchor reloaded", type="positive")

    async def refresh() -> None:
        from .. import grounding as _g
        try:
            body = await asyncio.to_thread(_g.block)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"grounding unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            ui.html(
                '<pre style="background:#0a0a0c;border:1px solid var(--border);'
                'border-radius:8px;padding:.7rem .9rem;font-size:11.5px;'
                'line-height:1.55;color:var(--text);max-height:24rem;'
                f'overflow:auto;white-space:pre-wrap">{html.escape(body)}</pre>'
            )

    _tick_now(ui, refresh, _i("grounding"), tab="system")


# ── recent-calls list (Calls tab) ──────────────────────────────────────────


def _calls_history_panel(ui, span: str = "c12") -> None:
    """All TradingCalls from the last 7 days, newest-first. Shows the
    direction · ticker · thesis · 1d/5d realised return if marked."""
    from ..db import session_scope
    from ..models import TradingCall
    from sqlmodel import select

    with _Panel(ui, "Recent calls (7d)", "🎯", span,
                anchor="calls-history"):
        host = ui.element("div").classes("fr-feed")

    def _load() -> list[dict]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        cutoff_naive = cutoff.replace(tzinfo=None)
        with session_scope() as s:
            rows = s.exec(
                select(TradingCall)
                .where(TradingCall.created_at >= cutoff_naive)
                .order_by(TradingCall.created_at.desc())
                .limit(60)
            ).all()
        return [{
            "id": r.id, "ticker": r.ticker, "direction": r.direction,
            "conv": r.conviction, "source": r.source,
            "thesis": r.thesis, "ts": _aware(r.created_at).isoformat(),
            "ret_1d": r.ret_1d_pct, "ret_5d": r.ret_5d_pct,
            "px": r.price_at_call,
        } for r in rows]

    async def refresh() -> None:
        try:
            rows = await asyncio.to_thread(_load)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"calls unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            if not rows:
                ui.label("No calls in the last 7 days.").classes("mut").style(
                    "font-size:13px"
                )
                return
            now = datetime.now(timezone.utc)
            with ui.element("div").classes("fr-card-grid"):
                for r in rows:
                    _render_call_card(ui, r, now)

    _tick_now(ui, refresh, _i("calls_history"), tab="calls")


def _render_call_card(ui, r: dict, now: datetime) -> None:
    """One call card. Mirrors the news-card shape so both feeds read as
    one cohesive UI — direction chip (LONG green / SHORT red), source +
    conviction in the meta strip, thesis as the headline, returns
    surfaced as colour-coded delta tags, primary action is "AI dossier"
    (opens the cached dossier modal with full context)."""
    ts = datetime.fromisoformat(r["ts"])
    ago = _ago_short(now - ts)
    direction = (r["direction"] or "").upper()
    kind_cls = "call short" if r["direction"] == "short" else "call"
    conv = r.get("conv") or 0
    px = r.get("px")

    with ui.element("div").classes("fr-call-card").on(
        "click", lambda _e, cid=r["id"]: _open_call_dialog(ui, cid),
    ):
        # ── meta row ──
        ui.html(
            f'<div class="card-meta">'
            f'<span class="kind {kind_cls}">{direction or "CALL"}</span>'
            f'<span>{html.escape(r.get("source") or "")[:24]}</span>'
            f'<span>conv {conv}/5</span>'
            f'<span class="ts">{ago}</span></div>'
        )
        # ── title (ticker + first line of thesis) ──
        ui.html(
            f'<div class="card-title">'
            f'<span style="color:#8fb6ff;font-weight:600;'
            f'font-family:ui-monospace,Menlo,monospace">'
            f'${html.escape(r["ticker"])}</span>'
            f'  &nbsp; {html.escape((r.get("thesis") or "")[:180])}</div>'
        )
        # ── tags: price + returns ──
        tag_bits: list[str] = []
        if px is not None:
            tag_bits.append(
                f'<span class="src">@ {px:.4g}</span>'
            )
        if r.get("ret_1d") is not None:
            tone = _tone(r["ret_1d"])
            tag_bits.append(
                f'<span class="delta {tone}">'
                f'{_pct(r["ret_1d"])} 1d</span>'
            )
        if r.get("ret_5d") is not None:
            tone = _tone(r["ret_5d"])
            tag_bits.append(
                f'<span class="delta {tone}">'
                f'{_pct(r["ret_5d"])} 5d</span>'
            )
        if tag_bits:
            ui.html(
                '<div class="card-tags">' + "".join(tag_bits) + '</div>'
            )
        # ── actions: dossier (primary) + jump-to-chart ──
        # The Chart button just navigates to #markets; from there the
        # user can type the ticker. A future JS bridge could auto-fill
        # the picker but the simpler thing is cleaner today.
        ui.html(
            '<div class="card-actions">'
            '<span class="btn primary">✨ AI dossier</span>'
            '<a class="btn" href="#markets" '
            'onclick="event.stopPropagation()">📈 Markets</a>'
            '</div>'
        )


# ── left categorized navigation + tab routing ──────────────────────────────
# Each sidebar item carries a `data-tab=` id; the matching content lives in
# a `<div class="fr-tab" data-tab="..."` block. Clicks update `location.hash`
# (so URLs are shareable / browser-backable) and a `hashchange` listener
# flips the `.active` class on both sides. Hidden tabs stay in the DOM so
# their refresh timers keep running — switching is instant with fresh data.

_NAV: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    ("Workspace", (
        ("overview",  "📊", "Overview"),
        ("portfolio", "💼", "Portfolio"),
        ("markets",   "📈", "Markets"),
        ("research",  "🔬", "Research"),
        ("theses",    "🧠", "Theses"),
        ("intel",     "🛰", "Intel"),
        ("calls",     "🎯", "Calls"),
    )),
    ("Tools", (
        ("watches",   "🔔", "Watches"),
        ("lookup",    "🔎", "Lookup"),
        ("copilot",   "✨", "Copilot"),
    )),
    ("Operations", (
        ("system",    "🖥", "System"),
    )),
)

# Tab metadata: header title + subtitle. Keys must match `_NAV` ids.
_TAB_META: dict[str, tuple[str, str, str]] = {
    "overview":  ("📊", "Overview",
                  "At-a-glance — KPIs, equity curve, realised P&L, "
                  "recent activity."),
    "portfolio": ("💼", "Portfolio",
                  "Autonomous wallets, paper book, holdings, manual entry."),
    "markets":   ("📈", "Markets",
                  "Ticker chart with entry markers; watchlist gauges."),
    "research":  ("🔬", "Research desk",
                  "Ask the bot to research a topic and recommend a trade. "
                  "You confirm the execution — nothing fires autonomously."),
    "theses":    ("🧠", "Running theses",
                  "Hypotheses the bot is tracking across days. New news + "
                  "filings link in automatically and tag as supporting or "
                  "challenging — the cross-pollination loop."),
    "intel":     ("🛰", "Intel",
                  "Filings + news feed and the forward catalyst calendar."),
    "calls":     ("🎯", "Calls",
                  "Track record, recent calls, manual call entry."),
    "watches":   ("🔔", "Watches",
                  "Plain-English conditional alerts."),
    "lookup":    ("🔎", "Lookup",
                  "Read-only `!cmd` surface — ticker / news / filing / "
                  "timeline / catalysts / status."),
    "copilot":   ("✨", "Copilot",
                  "Ask the bot anything about the book, wallets, filings, "
                  "sentiment or a ticker. Same context as Discord."),
    "system":    ("🖥", "System",
                  "Health, scheduler, live log, host metrics."),
}


def _render_sidebar(ui) -> None:
    """Render the left categorised nav. Items are plain `<a href="#tab">`
    so URLs stay shareable; the tab-nav JS reads `data-tab` to flip class."""
    with ui.element("nav").classes("fr-sidebar"):
        for section, items in _NAV:
            ui.html(
                f'<div class="fr-sb-section">{html.escape(section)}</div>'
            )
            for tab, icon, label in items:
                ui.html(
                    f'<a class="fr-sb-item" href="#{tab}" '
                    f'data-tab="{tab}">'
                    f'<span class="ic">{icon}</span>'
                    f'<span>{html.escape(label)}</span></a>'
                )


def _tab_header(ui, tab: str) -> None:
    """The big title row at the top of each tab body — keeps the visual
    hierarchy consistent across tabs without duplicating Python."""
    icon, title, sub = _TAB_META.get(tab, ("", tab.title(), ""))
    ui.html(
        '<div class="fr-tab-header"><div>'
        f'<h2><span>{icon}</span>{html.escape(title)}</h2>'
        f'<div class="sub">{html.escape(sub)}</div>'
        '</div></div>'
    )


# Tab-nav JS: drives both the URL hash and the .active class on the
# sidebar item AND the content div. `wait()` self-retries until NiceGUI's
# WebSocket hydrate has materialised the tabs in the DOM. Hash-default
# routes ` ` / `#` / unknown to `overview`. `scrollTo` resets the page
# scroll on each tab switch — a tab-relative top is what the user expects
# coming from a "click a category" mental model.
_TAB_NAV_JS = """
(()=>{
  const KNOWN = new Set(["overview","portfolio","markets","research",
                          "theses","intel","calls","watches","lookup",
                          "copilot","system"]);
  const activate = (raw) => {
    let id = (raw || location.hash || '#overview').replace(/^#/, '');
    if (!KNOWN.has(id)) id = 'overview';
    document.querySelectorAll('.fr-tab').forEach(el =>
      el.classList.toggle('active', el.dataset.tab === id));
    document.querySelectorAll('.fr-sb-item').forEach(a =>
      a.classList.toggle('active', a.dataset.tab === id));
    try { window.scrollTo({top: 0, behavior: 'instant'}); } catch(_) {}
  };
  window.addEventListener('hashchange', () => activate());
  const wait = () => {
    if (!document.querySelector('.fr-tab')) {
      setTimeout(wait, 120); return;
    }
    activate();
  };
  wait();
})();
"""


# ── page ────────────────────────────────────────────────────────────────────


def _build_page(ui) -> None:
    """Register the single cockpit page. The stylesheet is injected once
    here (not per connection); `@ui.page` rebuilds widgets per browser and
    `ui.timer`s drive the live refresh."""

    # shared=True → part of every page's <head> at first paint (one global
    # design system, no flash of unstyled content), not per-client post-connect.
    ui.add_head_html(_THEME_CSS, shared=True)

    @ui.page("/")
    def cockpit() -> None:
        ui.colors(primary="#6699ff")

        # ── header: brand · clock · uptime · health verdict ──────────────
        # Tokenised colours so a single :root tweak rolls through the whole
        # chrome (header included). min-height matches `.fr-sidebar`'s
        # `max-height: 100vh - 3.5rem` so the sidebar fills exactly under it.
        with ui.header().style(
            "background:var(--bg);border-bottom:1px solid var(--border);"
            "padding:.6rem 1.15rem;justify-content:space-between;"
            "align-items:center;min-height:3.5rem"
        ):
            with ui.element("div").classes("fr-rowi"):
                ui.label("🛰").style("font-size:18px")
                ui.label("Sentinel").style(
                    "font-size:16px;font-weight:600;letter-spacing:.02em"
                )
                ui.label("COCKPIT").style(
                    "font-size:9.5px;letter-spacing:.22em;color:var(--faint);"
                    "border:1px solid var(--border);padding:.1rem .4rem;"
                    "border-radius:5px"
                )
            with ui.element("div").classes("fr-rowi"):
                up = ui.label("up —").classes("fnt").style(
                    "font-size:11px;font-variant-numeric:tabular-nums"
                )
                clock = ui.label("").classes("mut").style(
                    "font-size:12px;font-variant-numeric:tabular-nums;"
                    "font-family:ui-monospace,Menlo,monospace"
                )
                verdict = ui.html(
                    '<span class="fr-pill idle">• booting</span>'
                )

        def _tick() -> None:
            clock.set_text(
                datetime.now(timezone.utc).strftime("%Y-%m-%d  %H:%M:%SZ")
            )

        _tick()
        ui.timer(1.0, _tick)

        # Stable mount for popups — parented at the page root so panel
        # refresh timers can't unmount whatever dialog is currently open.
        # See `_MODAL_PARENT` doc above for the failure mode this prevents.
        global _MODAL_PARENT
        _MODAL_PARENT = ui.element("div").style(
            "position:absolute;width:0;height:0;overflow:visible"
        )

        # ── shell: sticky sidebar (categorised tab nav) + main content ───
        # Each `.fr-tab` is one logical page worth of content. Hidden tabs
        # stay in the DOM so their refresh timers keep running in the
        # background — switching feels instant with fresh data, at the
        # cost of ~9 inactive timers ticking. Memory cost is negligible.
        with ui.element("div").classes("fr-shell"):
            _render_sidebar(ui)
            with ui.element("div").classes("fr-main"):
                # ── OVERVIEW ─────────────────────────────────────────────
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=overview"
                ):
                    _tab_header(ui, "overview")
                    with ui.element("div").classes("fr-grid"):
                        _kpi_ribbon(ui, up)
                        _equity_curve_panel(ui, span="c7")
                        _realized_curve_panel(ui, span="c5")
                        _activity_panel(ui, span="c12")

                # ── PORTFOLIO ────────────────────────────────────────────
                # Wallets take a full row so the 6-column wallets table
                # has room to breathe; paper book + holds each get their
                # own full-width rows for the same reason (their grid
                # column templates are wide and squished at <c12).
                # Open-position form lives narrow alongside the holds.
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=portfolio"
                ):
                    _tab_header(ui, "portfolio")
                    with ui.element("div").classes("fr-grid"):
                        _funds_panel(ui, span="c12")
                        _book_panel(ui, span="c12")
                        _holds_panel(ui, span="c7")
                        _open_form_panel(ui, span="c5")

                # ── MARKETS ──────────────────────────────────────────────
                # Star feature: the candlestick + position-summary card
                # sits up top; the watchlist below it is clickable and
                # pumps the ticker into the chart via _TICKER_LOAD_CB.
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=markets"
                ):
                    _tab_header(ui, "markets")
                    with ui.element("div").classes("fr-grid"):
                        _ticker_chart_panel(ui, span="c12")
                        _watchlist_panel(ui, span="c12")

                # ── THESES ───────────────────────────────────────────────
                # Running hypotheses the bot tracks across days. New
                # news + filings on a thesis's ticker auto-link as
                # `supports` / `challenges` / `neutral`; periodic
                # review closes on target hit / decisive challenges /
                # horizon-elapsed.
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=theses"
                ):
                    _tab_header(ui, "theses")
                    with ui.element("div").classes("fr-grid"):
                        _theses_panel(ui, span="c12")

                # ── RESEARCH ─────────────────────────────────────────────
                # User-prompted research with confirm-before-trade. Goes
                # to the dedicated `research` wallet so its P&L curve sits
                # alongside (but separate from) the seven autonomous funds.
                # The wallet panel ABOVE the research desk gives a
                # persistent audit of executed trades (open + closed with
                # close_reason) — the user's "where did my trade go?".
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=research"
                ):
                    _tab_header(ui, "research")
                    with ui.element("div").classes("fr-grid"):
                        _research_wallet_panel(ui, span="c12")
                        _research_panel(ui, span="c12")

                # ── INTEL ────────────────────────────────────────────────
                # News + filings get full-width rows — they carry the most
                # text per row and their 3-column meta line wraps awkwardly
                # at half-width. Social pulse + catalysts are denser so
                # they share the bottom row at c6/c6.
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=intel"
                ):
                    _tab_header(ui, "intel")
                    with ui.element("div").classes("fr-grid"):
                        _news_feed_panel(ui, span="c12")
                        _filings_feed_panel(ui, span="c12")
                        _social_pulse_panel(ui, span="c6")
                        _catalysts_panel(ui, span="c6")

                # ── CALLS ────────────────────────────────────────────────
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=calls"
                ):
                    _tab_header(ui, "calls")
                    with ui.element("div").classes("fr-grid"):
                        _scorecard_panel(ui, span="c7")
                        _calls_panel(ui, span="c5")
                        _calls_history_panel(ui, span="c12")

                # ── WATCHES ──────────────────────────────────────────────
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=watches"
                ):
                    _tab_header(ui, "watches")
                    with ui.element("div").classes("fr-grid"):
                        _watches_panel(ui, span="c12")

                # ── LOOKUP ───────────────────────────────────────────────
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=lookup"
                ):
                    _tab_header(ui, "lookup")
                    with ui.element("div").classes("fr-grid"):
                        _lookup_panel(ui, span="c12")

                # ── COPILOT ──────────────────────────────────────────────
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=copilot"
                ):
                    _tab_header(ui, "copilot")
                    with ui.element("div").classes("fr-grid"):
                        _chat_panel(ui, span="c12")

                # ── SYSTEM ───────────────────────────────────────────────
                with ui.element("div").classes("fr-tab").props(
                    "data-tab=system"
                ):
                    _tab_header(ui, "system")
                    with ui.element("div").classes("fr-grid"):
                        _health_panel(ui, verdict, span="c7")
                        _system_panel(ui, span="c5")
                        _grounding_panel(ui, span="c12")
                        _scheduler_panel(ui, span="c12")
                        _log_panel(ui, span="c12")

        # Kick the tab-nav JS after first paint. The script self-retries
        # until the `.fr-tab` nodes exist (NiceGUI hydrates async over WS).
        async def _init_nav() -> None:
            try:
                await ui.run_javascript(_TAB_NAV_JS, timeout=2.0)
            except Exception:
                pass

        ui.timer(0.3, _init_nav, once=True)

        # Poll the client's location.hash to keep `_ACTIVE_TAB` synced —
        # that's what `_tick_now(tab=…)` reads when deciding whether to
        # run a refresh body. 2s is brisk enough that switching tabs
        # feels responsive (the new tab's interval timer wakes up on its
        # next firing rather than instantly, which is acceptable) while
        # keeping the JS round-trip rate low.
        async def _sync_active_tab() -> None:
            global _ACTIVE_TAB
            try:
                value = await ui.run_javascript(
                    "location.hash ? location.hash.replace('#','') : 'overview'",
                    timeout=1.0,
                )
                if isinstance(value, str) and value:
                    _ACTIVE_TAB = value
            except Exception:
                # WS slow, browser tab backgrounded, etc. — last-known
                # active tab keeps running, which is the safe default.
                pass

        ui.timer(2.0, _sync_active_tab)


# ── KPI ribbon ──────────────────────────────────────────────────────────────


def _kpi_snapshot() -> dict:
    """One blocking gather of headline numbers (run off-loop). Each source
    is isolated so one empty/failed accessor doesn't blank the whole ribbon."""
    from .. import funds, portfolio, scorecard
    from ..llm import llm_stats

    o: dict = {}
    try:
        st = funds.fund_standings()
        eq = sum(r["equity"] for r in st)
        start = sum(r["start"] for r in st)
        o["equity"] = eq
        o["ret"] = ((eq - start) / start * 100) if start else None
        o["nfunds"] = len(st)
    except Exception:
        o["equity"] = o["ret"] = o["nfunds"] = None
    try:
        pos = portfolio.open_positions()
        o["open"] = len(pos)
        o["upnl"] = sum(
            p["pnl"] for p in pos if p.get("pnl") is not None
        )
    except Exception:
        o["open"] = o["upnl"] = None
    try:
        r = portfolio.realized_summary()
        o["realized"] = r["realized_pnl"]
        o["wins"], o["closed"] = r["wins"], r["closed"]
    except Exception:
        o["realized"] = o["wins"] = o["closed"] = None
    try:
        ov = scorecard.track_record_summary()["overall"]
        o["hit_n"] = ov["n"]
        o["hit"] = (ov["hits"] / ov["n"] * 100) if ov["n"] else None
        o["hits"] = ov["hits"]
    except Exception:
        o["hit"] = o["hit_n"] = o["hits"] = None
    try:
        ls = llm_stats()
        o["llm_calls"] = ls["calls"]
        o["llm_err"] = ls["errors"]
    except Exception:
        o["llm_calls"] = o["llm_err"] = None
    return o


def _kpi_ribbon(ui, uptime_lbl) -> None:
    from . import sysinfo

    # `id=snapshot` is the deep-link target for the sidebar's Overview item.
    wrap = ui.element("div").classes("fr-ribbon").props("id=snapshot")
    with wrap:
        equity_v, equity_s = _kpi(ui, "Wallet equity")
        ret_v, ret_s = _kpi(ui, "Aggregate return")
        open_v, open_s = _kpi(ui, "Open positions")
        real_v, real_s = _kpi(ui, "Realized P&L")
        hit_v, hit_s = _kpi(ui, "Call hit rate")
        llm_v, llm_s = _kpi(ui, "LLM reliability")

    async def refresh() -> None:
        try:
            k = await asyncio.to_thread(_kpi_snapshot)
            s = await asyncio.to_thread(sysinfo.snapshot)
        except Exception:
            return

        equity_v.set_text(_usd(k["equity"]))
        equity_s.set_text(
            f"{k['nfunds']} wallets" if k.get("nfunds") else "—"
        )

        ret_v.set_text(_pct(k["ret"]) if k["ret"] is not None else "—")
        ret_v.classes(replace=f"v {_tone(k['ret'])}")
        ret_s.set_text("since inception" if k["ret"] is not None else "—")

        open_v.set_text(str(k["open"]) if k["open"] is not None else "—")
        if k.get("upnl") is not None and k["open"]:
            open_s.set_text(f"uPnL {_susd(k['upnl'])}")
            open_s.classes(replace=f"s {_tone(k['upnl'])}")
        else:
            open_s.set_text("flat")
            open_s.classes(replace="s mut")

        real_v.set_text(
            _susd(k["realized"]) if k["realized"] is not None else "—"
        )
        real_v.classes(replace=f"v {_tone(k['realized'])}")
        if k.get("closed"):
            real_s.set_text(f"{k['wins']}/{k['closed']} closed won")
        else:
            real_s.set_text("no closed trades")

        if k.get("hit") is not None:
            hit_v.set_text(f"{k['hit']:.0f}%")
            hit_s.set_text(f"{k['hits']}/{k['hit_n']} scored")
        else:
            hit_v.set_text("—")
            hit_s.set_text("none scored yet")

        calls, err = k.get("llm_calls"), k.get("llm_err")
        if calls:
            okp = (1 - err / calls) * 100
            llm_v.set_text(f"{okp:.1f}%")
            llm_v.classes(
                replace="v " + ("neg" if okp < 90 else "")
            )
            llm_s.set_text(f"{calls:,} calls · {err} failed")
        else:
            llm_v.set_text("—")
            llm_s.set_text("no calls yet")

        try:
            uptime_lbl.set_text(f"up {sysinfo.fmt_uptime(s['uptime_s'])}")
        except Exception:
            pass

    _tick_now(ui, refresh, _i("kpi"), tab="overview")


# Refresh intervals per panel, in seconds. All in one table so the
# dashboard's overall load (DB queries + WS deltas + client DOM churn)
# is tunable from one place. Picked to keep the page responsive on a
# Raspberry Pi while still feeling "live" on the surfaces that matter
# (system metrics, paper book, log tail tick a bit faster than the
# slow data — wallets, watchlist, feeds — which only change minute-to-
# minute). Adjust here, not per call site.
_INTERVALS: dict[str, float] = {
    "kpi":             45.0,    # was 20s; ribbon doesn't change that fast
    "equity_curve":   120.0,    # FundEquity rows write every wallet cycle
    "realized_curve":  60.0,    # closed trades are bursty, not constant
    "watchlist":       45.0,    # 586-ticker batch query is the heavy one
    "ticker_chips":    90.0,    # open-positions chips for the picker
    "filings_feed":    90.0,    # filings drip in; 60s was wasteful
    "news_feed":       75.0,    # news polls every 5min; UI ≤ 75s = fresh
    "social_pulse":   120.0,    # social_pulse pipeline runs hourly
    "catalysts":      600.0,    # 10min — earnings calendar is daily
    "activity":        60.0,    # mixed feed; 30s was very tight
    "grounding":      300.0,    # the preamble itself is 5min-cached
    "calls_history":   90.0,    # calls don't fire constantly
    "funds":          120.0,    # wallet cycle runs every 60min anyway
    "research_wallet": 90.0,    # research trades are user-paced
    "research_desk":   60.0,    # task list churn is human-rate
    "scorecard":       90.0,    # marks update on a slow cron
    "book":            40.0,    # open positions; faster matters here
    "health":         120.0,    # health verdict changes slowly
    "system":          15.0,    # 5s was overkill for CPU/RAM
    "holds":          120.0,    # holds are user-pace; calm
    "watches":        120.0,    # watch defs are user-pace
    "live_log":         8.0,    # was 3s — half the DOM churn, still live
    "theses":         120.0,    # theses change slowly; daily generator
}


def _i(name: str) -> float:
    return _INTERVALS.get(name, 60.0)


# Active-tab tracker. Set by a 2-second JS poll registered in cockpit()
# that reads `location.hash`. Panels that pass `tab=` to `_tick_now`
# only run their coroutine body when this matches their tab — so 8 of
# the 9 tabs sit silent at any moment, cutting baseline DB + WS load
# by ~8x relative to the always-running mode. Module-level (not
# per-client) since this is a single-user bot; if multiple browser
# tabs are open with different active tabs, last-write-wins is fine.
_ACTIVE_TAB = "overview"


def _tick_now(ui, coro, interval: float, *, tab: str | None = None) -> None:
    """Run an async refresh immediately, then on an interval.

    Wraps the caller's coroutine in a try/except so a single bad refresh
    doesn't propagate out and (under some NiceGUI / uvicorn / Quasar
    interactions) cascade into the WebSocket closing with "connection
    lost". A dropped panel is a much better failure mode than a dropped
    page. Errors are logged with the panel callable's name so they're
    findable in the live journal.

    When `tab=` is provided, the body is SKIPPED whenever the user is
    on a different tab — silent panels for ~8 of 9 tabs at any moment.
    The first-paint tick (`once=True` at 0.1s) is NOT gated, so every
    panel renders its initial state regardless of the starting tab —
    users see fresh data the first time they switch to any tab.
    """
    async def _safe(*, force: bool = False) -> None:
        if not force and tab is not None and _ACTIVE_TAB != tab:
            return
        try:
            await coro()
        except Exception as e:
            logger.warning(
                "dashboard refresh failed in {}: {}",
                getattr(coro, "__qualname__", "?"), e,
            )

    async def _first() -> None:
        # First paint: always run so the panel isn't blank when the
        # user lands on its tab later. After that, the interval timer
        # honours the active-tab gate.
        await _safe(force=True)

    ui.timer(0.1, _first, once=True)
    ui.timer(interval, _safe)


# ── wallets / funds ─────────────────────────────────────────────────────────


def _funds_panel(ui, span: str = "c7") -> None:
    from .. import funds

    with _Panel(ui, "Autonomous wallets", "🏦",
                span, anchor="wallets"):
        host = ui.element("div").classes("fr-w")

    def _load() -> dict:
        out: dict = {"rows": funds.fund_standings(), "exp": None}
        try:
            m = funds.wallet_meta()
            if m.get("funds"):
                out["exp"] = m["experiments"]
        except Exception:
            pass
        return out

    async def refresh() -> None:
        try:
            data = await asyncio.to_thread(_load)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"wallets unavailable: {e}").classes("fnt").style(
                    "font-size:12px"
                )
            return
        rows = data["rows"]
        host.clear()
        with host:
            if not rows:
                ui.label(
                    "No wallets seeded yet — they start on the next "
                    "funds cycle."
                ).classes("mut").style("font-size:13px")
                return
            medals = {0: "🥇", 1: "🥈", 2: "🥉"}
            with ui.element("div").classes("fr-wgrid"):
                with ui.element("div").classes("fr-wh"):
                    for h in ("#  Wallet", "Equity", "Return",
                              "Open", "uPnL", "W/L"):
                        ui.html(html.escape(h))
                for i, r in enumerate(rows):
                    rank = medals.get(i, f'<span class="fnt">{i + 1}</span>')
                    wl = (
                        f"{r['wins']}/{r['closed']}" if r["closed"] else "—"
                    )
                    with ui.element("div").classes("fr-wrow").on(
                        "click",
                        lambda _e, n=r["name"]: _open_wallet_dialog(ui, n),
                    ):
                        ui.html(
                            f'{rank}&nbsp;&nbsp;'
                            f'<span class="tk">'
                            f'{html.escape(r["name"])}</span>'
                            f'<span class="fr-wmandate">'
                            f'{html.escape(r.get("mandate") or "")}</span>'
                        )
                        ui.html(_usd(r["equity"]))
                        ui.html(_pct(r["ret_pct"])).classes(
                            _tone(r["ret_pct"])
                        )
                        ui.html(str(r["open"]))
                        if r["open"]:
                            ui.html(_susd(r["upnl"])).classes(
                                _tone(r["upnl"])
                            )
                        else:
                            ui.html("—").classes("fnt")
                        ui.html(wl).classes("mut")

            exp = data.get("exp")
            if exp:
                ui.element("div").style(
                    "height:1px;background:var(--border);margin:.85rem 0"
                )
                ui.label("EDGE EXPERIMENTS").classes("fnt").style(
                    "font-size:10px;letter-spacing:.13em;margin-bottom:.4rem"
                )
                for key, title in (
                    ("trend", "Trend filter · leaders vs degen"),
                    ("crowd", "Crowd · hype vs degen"),
                ):
                    e = exp.get(key, {})
                    with ui.element("div").classes("fr-between").style(
                        "padding:.22rem 0;font-size:12px"):
                        ui.html(
                            f'<span class="mut">{html.escape(title)}</span>'
                            f'&nbsp;&nbsp;<span class="num">'
                            f'{e.get("a_ret", 0):+.1f}% / '
                            f'{e.get("b_ret", 0):+.1f}%</span>'
                        )
                        v = str(e.get("verdict", "—"))
                        ui.html(
                            f'<span class="fr-pill idle" '
                            f'style="font-size:10.5px">'
                            f"{html.escape(v[:34])}</span>"
                        )

    _tick_now(ui, refresh, _i("funds"), tab="portfolio")


# ── wallet drill-in (click a row → open positions dialog) ───────────────────


def _render_wallet_detail(ui, d: dict) -> None:
    """Build the wallet-positions panel from `funds.fund_positions(name)`."""
    if d.get("mandate"):
        ui.label(d["mandate"]).classes("fnt").style(
            "font-size:11.5px;line-height:1.4;margin-bottom:.7rem"
        )
    with ui.element("div").classes("fr-tiles").style("margin-bottom:.7rem"):
        for label, val, tone in (
            ("Equity",   _usd(d["equity"]),       ""),
            ("Return",   _pct(d["ret_pct"]),      _tone(d["ret_pct"])),
            ("Open uPnL", _susd(d["open_upnl"]),  _tone(d["open_upnl"])),
            ("Cash",     _usd(d["cash"]),         ""),
        ):
            with ui.element("div").classes("fr-tile"):
                ui.html(f'<div class="l">{html.escape(label)}</div>')
                ui.html(f'<div class="v {tone}">{html.escape(val)}</div>')

    if d.get("marks_asof") is not None:
        if d.get("marks_stale"):
            with ui.element("div").classes("fr-alert warn").style(
                "margin-bottom:.7rem"
            ):
                ui.html(
                    "⚠️&nbsp;&nbsp;marks last updated "
                    f"{html.escape(d['marks_ago'] or '')} ago — "
                    "P&amp;L frozen at entry until the market reopens."
                )
        else:
            ui.html(
                f'<div class="fnt" style="font-size:11px;'
                f'margin-bottom:.5rem">marks live · updated '
                f"{html.escape(d['marks_ago'] or '')} ago</div>"
            )

    if not d["positions"]:
        ui.label("No open positions.").classes("mut").style(
            "font-size:13px"
        )
        return
    head = (
        '<tr><th><span style="display:inline-block;width:3.2rem">'
        'Side</span>Ticker</th><th>Qty</th>'
        "<th>Entry</th><th>Mark</th><th>uPnL</th><th>%</th></tr>"
    )
    rows = []
    for p in d["positions"]:
        long = p["side"] == "long"
        side_cls = "sd-l" if long else "sd-s"
        side_lbl = "LONG" if long else "SHORT"
        mark_cls = "" if p["mark_live"] else "fnt"
        rows.append(
            f'<tr><td><span class="sd {side_cls}">{side_lbl}</span>'
            f'<span class="tk">${html.escape(p["ticker"])}</span></td>'
            f'<td>{p["qty"]:g}</td>'
            f'<td>{p["entry"]:.4g}</td>'
            f'<td class="{mark_cls}">{p["mark"]:.4g}</td>'
            f'<td class="{_tone(p["upnl"])}">{_susd(p["upnl"])}</td>'
            f'<td class="{_tone(p["upnl_pct"])}">'
            f'{_pct(p["upnl_pct"])}</td></tr>'
        )
    ui.html(
        f'<table class="fr-tb"><thead>{head}</thead>'
        f'<tbody>{"".join(rows)}</tbody></table>'
    ).classes("fr-w")


async def _open_wallet_dialog(ui, name: str) -> None:
    """Click handler on a wallets-list row — opens a NiceGUI dialog with
    the wallet's open positions and per-position stats. Snapshot-on-open
    (close & re-open for a fresh read); cheap and avoids timer leaks.

    Dialog is parented to the page-level `_MODAL_PARENT` (set in
    `cockpit()`) — NOT the click handler's slot. Without that anchor,
    the panel's refresh timer would clear its host and take the dialog
    with it (≈60s on the wallets panel, faster on others)."""
    from .. import funds

    with (_MODAL_PARENT or ui.element("div")):
        dlg = ui.dialog()
    with dlg, ui.element("div").classes("fr-card fr-dlg"):
        with ui.element("div").classes("fr-hd"):
            ui.html(
                f'<span class="ic">🏦</span>'
                f'<span class="ti">{html.escape(name)}</span>'
            )
            ui.button(icon="close", on_click=dlg.close).props(
                "round dense flat size=sm"
            ).style("margin-left:auto")
        body = ui.element("div").classes("fr-bd")
        with body:
            ui.spinner(size="md").classes("mut")
    dlg.open()

    try:
        d = await asyncio.to_thread(funds.fund_positions, name)
    except Exception as e:
        body.clear()
        with body:
            ui.label(f"could not load: {e}").classes("fnt")
        return
    body.clear()
    with body:
        if d is None:
            ui.label("This wallet isn't seeded yet.").classes("mut")
            return
        _render_wallet_detail(ui, d)


# ── call / news dossier dialogs ────────────────────────────────────────────


def _dossier_chat(ui, ask_fn, item_id: int) -> None:
    """Render the follow-up chat section inside a dossier dialog. Builds
    a tiny chat feed + input row using the existing `.bub.a/.bub.u` styles
    so the look matches the Copilot tab. State is local — closing the
    modal drops history, which is intentional (the cached dossier sticks,
    but iterative Qs are scratch). `ask_fn(item_id, question)` is the
    backend (e.g. `dossier.ask_about_call`)."""
    ui.html(
        '<div class="fnt" style="font-size:10.5px;'
        'letter-spacing:.13em;text-transform:uppercase;margin:.85rem 0 .35rem">'
        'Follow-up</div>'
    )
    feed = ui.element("div").classes("chat-feed").style(
        "height:auto;max-height:18rem;flex:0 0 auto;gap:.55rem"
    )
    with ui.element("div").classes("fr-row").style(
        "margin-top:.5rem;gap:.4rem"
    ):
        box = ui.input(
            placeholder="Ask a follow-up…  (Enter to send)"
        ).props("outlined dense dark").classes("fr-grow")
        send_btn = ui.button(icon="send").props(
            "round dense unelevated color=primary"
        )

    async def _send() -> None:
        q = (box.value or "").strip()
        if not q:
            return
        box.value = ""
        box.disable()
        send_btn.disable()
        now = datetime.now().strftime("%H:%M")
        with feed:
            with ui.element("div").classes("bub u"):
                ui.html(
                    f'<div class="rl">you</div>'
                    f'<div>{html.escape(q)}</div>'
                    f'<div class="ts">{now}</div>'
                )
            pending = ui.element("div").classes("bub a")
            with pending:
                ui.html(
                    '<div class="rl">copilot</div>'
                    '<div class="typing"><i></i><i></i><i></i></div>'
                )
        try:
            reply = await asyncio.to_thread(ask_fn, item_id, q)
        except Exception as e:
            reply = f"_LLM error: {e}_"
        pending.clear()
        with pending:
            ui.html('<div class="rl">copilot</div>')
            ui.markdown(reply or "_no reply_")
            ui.html(
                f'<div class="ts">{datetime.now().strftime("%H:%M")}</div>'
            )
        box.enable()
        send_btn.enable()
        box.run_method("focus")

    send_btn.on_click(_send)
    box.on("keydown.enter", _send)


def _dossier_header(
    ui, dlg, icon: str, title: str, subtitle: str = "",
    href: str | None = None,
) -> None:
    """Shared dialog header — icon + title + (optional clickable subtitle)
    + close button. Keeps the call and news modals visually consistent."""
    with ui.element("div").classes("fr-hd"):
        ui.html(
            f'<span class="ic">{icon}</span>'
            f'<span class="ti">{html.escape(title)[:90]}</span>'
        )
        if subtitle:
            sub_html = (
                f'<a href="{html.escape(href)}" target="_blank" '
                f'rel="noopener" style="color:#8fb6ff;text-decoration:none">'
                f'{html.escape(subtitle)[:60]}</a>'
                if href else
                f'<span>{html.escape(subtitle)[:60]}</span>'
            )
            ui.html(
                f'<span class="rt" style="margin-left:.6rem">{sub_html}</span>'
            )
        ui.button(icon="close", on_click=dlg.close).props(
            "round dense flat size=sm"
        ).style("margin-left:auto")


async def _load_dossier_into(
    ui, summary_host, get_dossier_fn, item_id: int, *, refresh: bool = False,
    get_meta_fn=None,
) -> None:
    """Fetch (or refresh) a dossier off the loop and render it.

    When `get_meta_fn(item_id)` is provided and returns a hit, the body
    is loaded from cache without showing the spinner — instant repaint
    — and a "cached on X" badge is rendered above the markdown. This is
    what tells the user the cache is actually working; previously a
    cache hit looked identical to a regen (same spinner, same delay).

    `refresh=True` always shows the spinner and bypasses the cache."""
    summary_host.clear()

    # Cache-hit fast path: render immediately + badge, no spinner.
    cached_meta = None
    if get_meta_fn is not None and not refresh:
        try:
            cached_meta = get_meta_fn(item_id)
        except Exception as e:
            logger.debug("dossier cache_meta failed: {}", e)

    if cached_meta is None:
        with summary_host:
            ui.spinner(size="md").classes("mut").style("margin:1rem auto")

    try:
        body = await asyncio.to_thread(
            get_dossier_fn, item_id, refresh=refresh
        )
    except Exception as e:
        summary_host.clear()
        with summary_host:
            ui.label(f"dossier failed: {e}").classes("fnt")
        return

    summary_host.clear()
    with summary_host:
        if cached_meta:
            ts = cached_meta.get("created_at", "")[:16].replace("T", " ")
            model = cached_meta.get("model") or "—"
            ui.html(
                '<div class="fr-pill ok" style="display:inline-flex;'
                'align-items:center;gap:.35rem;font-size:10px;padding:.18rem .55rem;'
                f'margin-bottom:.5rem">✓ cached · {html.escape(ts)} UTC · '
                f'{html.escape(model[:40])}</div>'
            )
        ui.markdown(body or "_no dossier_").classes("lookup-out").style(
            "max-height:32rem"
        )


async def _open_call_dialog(ui, call_id: int) -> None:
    """Click a call row → modal with cached LLM dossier + follow-up chat.
    The dossier is read from cache on every open; the *first* open ever
    generates it (one LLM round-trip), subsequent opens are instant."""
    from .. import dossier
    from ..db import session_scope
    from ..models import TradingCall

    try:
        with session_scope() as s:
            c = s.get(TradingCall, call_id)
            if c is None:
                ui.notify(f"call #{call_id} not found", type="warning")
                return
            tk = c.ticker
            side = c.direction.upper()
            conv = c.conviction
            source = c.source
            thesis = c.thesis
            px = c.price_at_call
            r1d = c.ret_1d_pct
            r5d = c.ret_5d_pct
    except Exception as e:
        ui.notify(f"call load failed: {e}", type="negative")
        return

    icon = "🟢" if side == "LONG" else "🔴"
    # Anchor the dialog to the page-level mount so the call-history /
    # activity panel's refresh tick can't yank it out from under us.
    with (_MODAL_PARENT or ui.element("div")):
        dlg = ui.dialog()
    with dlg, ui.element("div").classes("fr-card fr-dlg"):
        _dossier_header(
            ui, dlg, icon, f"{side} ${tk}",
            subtitle=f"{source} · conv {conv}/5",
        )
        body = ui.element("div").classes("fr-bd")
        with body:
            # Header strip: the original thesis + realised returns so far,
            # so the dossier is in CONTEXT (this is what the model wrote
            # against, this is what's happened since).
            with ui.element("div").style(
                "background:var(--surface2);border:1px solid var(--border);"
                "border-radius:8px;padding:.55rem .75rem;margin-bottom:.7rem"
            ):
                bits = []
                if px is not None:
                    bits.append(f"@ {px:.4g}")
                if r1d is not None:
                    bits.append(
                        f'<span class="{_tone(r1d)}">{_pct(r1d)} 1d</span>'
                    )
                if r5d is not None:
                    bits.append(
                        f'<span class="{_tone(r5d)}">{_pct(r5d)} 5d</span>'
                    )
                if bits:
                    ui.html(
                        f'<div class="fnt" style="font-size:10.5px;'
                        f'letter-spacing:.1em;text-transform:uppercase;'
                        f'margin-bottom:.25rem">Thesis</div>'
                        f'<div style="font-size:13px;line-height:1.5">'
                        f'{html.escape(thesis or "")}</div>'
                        f'<div style="font-size:11px;color:var(--muted);'
                        f'margin-top:.4rem">{" · ".join(bits)}</div>'
                    )
                else:
                    ui.html(
                        f'<div style="font-size:13px;line-height:1.5">'
                        f'{html.escape(thesis or "")}</div>'
                    )

            with ui.element("div").classes("fr-row").style(
                "margin-bottom:.4rem;gap:.5rem"
            ):
                ui.html(
                    '<div class="fnt" style="font-size:10.5px;'
                    'letter-spacing:.13em;text-transform:uppercase;flex:1">'
                    'AI dossier</div>'
                )
                ui.button(
                    "Regenerate",
                    on_click=lambda: asyncio.create_task(
                        _load_dossier_into(
                            ui, summary_host, dossier.call_dossier,
                            call_id, refresh=True,
                        )
                    ),
                ).props("flat dense size=sm").style("font-size:10px")

            summary_host = ui.element("div").classes("fr-w")
            _dossier_chat(ui, dossier.ask_about_call, call_id)
    dlg.open()
    await _load_dossier_into(
        ui, summary_host, dossier.call_dossier, call_id,
        get_meta_fn=dossier.call_summary_meta,
    )


async def _open_news_dialog(ui, news_id: int) -> None:
    """Click a news row → modal with cached LLM dossier + follow-up chat.
    Same caching philosophy as `_open_call_dialog`."""
    from .. import dossier
    from ..db import session_scope
    from ..models import NewsItem

    try:
        with session_scope() as s:
            n = s.get(NewsItem, news_id)
            if n is None:
                ui.notify(f"news #{news_id} not found", type="warning")
                return
            title = n.title
            url = n.url
            tk = n.ticker
            source = n.source
            summary = n.summary or ""
            sentiment = n.sentiment
    except Exception as e:
        ui.notify(f"news load failed: {e}", type="negative")
        return

    sent_icon = ("🟢" if (sentiment or 0) > 0
                 else "🔴" if (sentiment or 0) < 0 else "📰")
    with (_MODAL_PARENT or ui.element("div")):
        dlg = ui.dialog()
    with dlg, ui.element("div").classes("fr-card fr-dlg"):
        _dossier_header(
            ui, dlg, sent_icon, title,
            subtitle=f"${tk or 'macro'} · {source}",
            href=url,
        )
        body = ui.element("div").classes("fr-bd")
        with body:
            if summary:
                with ui.element("div").style(
                    "background:var(--surface2);border:1px solid var(--border);"
                    "border-radius:8px;padding:.55rem .75rem;margin-bottom:.7rem;"
                    "font-size:12.5px;line-height:1.5"
                ):
                    ui.html(html.escape(summary)[:1000])

            with ui.element("div").classes("fr-row").style(
                "margin-bottom:.4rem;gap:.5rem"
            ):
                ui.html(
                    '<div class="fnt" style="font-size:10.5px;'
                    'letter-spacing:.13em;text-transform:uppercase;flex:1">'
                    'AI dossier</div>'
                )
                ui.button(
                    "Regenerate",
                    on_click=lambda: asyncio.create_task(
                        _load_dossier_into(
                            ui, summary_host, dossier.news_dossier,
                            news_id, refresh=True,
                        )
                    ),
                ).props("flat dense size=sm").style("font-size:10px")

            summary_host = ui.element("div").classes("fr-w")
            _dossier_chat(ui, dossier.ask_about_news, news_id)
    dlg.open()
    await _load_dossier_into(
        ui, summary_host, dossier.news_dossier, news_id,
        get_meta_fn=dossier.news_analysis_meta,
    )


# ── research desk dialog ──────────────────────────────────────────────────


async def _open_research_dialog(ui, task_id: int) -> None:
    """Click handler on a research task row → modal with dossier + execute.

    Anchored to `_MODAL_PARENT` so the panel's refresh tick can't close it.
    Re-pulls the task on open so a recently-executed row reflects that
    state (status badge, link to trade). The dossier itself is cached on
    the row — opening is instant after generation, no re-LLM."""
    from .. import research_desk

    try:
        t = await asyncio.to_thread(research_desk.get_task, task_id)
    except Exception as e:
        ui.notify(f"research load failed: {e}", type="negative")
        return
    if t is None:
        ui.notify(f"task #{task_id} not found", type="warning")
        return

    verdict = (t.get("verdict") or "—").upper()
    icon = (
        "🟢" if verdict == "TRADE"
        else "🟡" if verdict == "WATCHLIST"
        else "⚪"
    )
    title = (t.get("prompt") or "")[:80]
    subtitle = f"task #{task_id} · {verdict}"
    if t.get("rec_ticker"):
        subtitle += f" · ${t['rec_ticker']}"

    with (_MODAL_PARENT or ui.element("div")):
        dlg = ui.dialog()
    with dlg, ui.element("div").classes("fr-card fr-dlg"):
        _dossier_header(ui, dlg, icon, title, subtitle=subtitle)
        body = ui.element("div").classes("fr-bd")
        with body:
            # ── original prompt strip ────────────────────────────────────
            with ui.element("div").style(
                "background:var(--surface2);border:1px solid var(--border);"
                "border-radius:8px;padding:.55rem .75rem;margin-bottom:.7rem"
            ):
                ui.html(
                    f'<div class="fnt" style="font-size:10.5px;'
                    f'letter-spacing:.1em;text-transform:uppercase;'
                    f'margin-bottom:.25rem">Original prompt</div>'
                    f'<div style="font-size:13px;line-height:1.5">'
                    f'{html.escape(t["prompt"])}</div>'
                )

            # ── recommendation summary + action ──────────────────────────
            _render_research_action(ui, dlg, t)

            # ── dossier (markdown) ───────────────────────────────────────
            ui.html(
                '<div class="fnt" style="font-size:10.5px;'
                'letter-spacing:.13em;text-transform:uppercase;'
                'margin:.85rem 0 .35rem">Dossier</div>'
            )
            ui.markdown(t.get("dossier") or "_no dossier produced_").classes(
                "lookup-out"
            ).style("max-height:32rem")

            # ── audit footer ─────────────────────────────────────────────
            ui.html(
                f'<div class="fnt" style="font-size:10.5px;margin-top:.75rem">'
                f'created {html.escape(t["created_at"][:19])} UTC · '
                f'model: {html.escape(t.get("model") or "—")}</div>'
            )
    dlg.open()


def _render_research_action(ui, dlg, t: dict) -> None:
    """The actionable block under the prompt — shows the recommendation
    + an Execute button when applicable, OR explains why nothing's
    actionable (verdict, conviction floor, already executed, cap hit)."""
    from .. import research_desk

    verdict = (t.get("verdict") or "").upper()
    conv = t.get("rec_conviction") or 0
    executed_at = t.get("executed_at")
    ticker = t.get("rec_ticker")
    direction = (t.get("rec_direction") or "").upper()
    size_pct = t.get("rec_size_pct")
    thesis = t.get("rec_thesis") or ""
    risks = t.get("rec_risks") or ""

    # Already executed → show what landed.
    if executed_at:
        note = t.get("execution_note") or ""
        with ui.element("div").classes("fr-alert").style(
            "background:rgba(61,220,151,.08);"
            "border:1px solid rgba(61,220,151,.32);color:var(--good);"
            "margin-bottom:.7rem"
        ):
            ui.html(
                f"✅&nbsp;&nbsp;<b>Executed</b> · {html.escape(note)}<br>"
                f'<span class="fnt" style="font-size:11px">at '
                f"{html.escape(executed_at[:19])} UTC · trade "
                f"#{t.get('executed_trade_id') or '—'} on the research wallet"
                "</span>"
            )
        return

    # Non-TRADE verdicts → no execution path. Show why.
    if verdict != "TRADE":
        msg = {
            "WATCHLIST": (
                "🟡 Bot says <b>WATCHLIST</b> — interesting but not "
                "actionable. Re-run the prompt later when there's a "
                "specific catalyst."
            ),
            "PASS": (
                "⚪ Bot says <b>PASS</b> — noise or no clear connection "
                "to a tradable name."
            ),
        }.get(verdict, "—")
        with ui.element("div").classes("fr-alert").style(
            "background:var(--surface2);border:1px solid var(--border);"
            "color:var(--muted);margin-bottom:.7rem"
        ):
            ui.html(msg)
        return

    # TRADE but below conviction floor → show + refuse.
    if conv < research_desk._CONVICTION_FLOOR:
        with ui.element("div").classes("fr-alert warn").style(
            "margin-bottom:.7rem"
        ):
            ui.html(
                f'🟡 <b>{direction} ${html.escape(ticker or "—")}</b> · '
                f'conviction {conv}/5 — below the floor of '
                f"{research_desk._CONVICTION_FLOOR}/5. Won't execute."
            )
        return

    # Actionable: Execute button + summary.
    side_cls = "pos" if (t.get("rec_direction") == "long") else "neg"
    remaining = research_desk.executions_remaining_today()
    with ui.element("div").style(
        "background:rgba(102,153,255,.08);border:1px solid rgba(102,153,255,.32);"
        "border-radius:8px;padding:.65rem .85rem;margin-bottom:.7rem"
    ):
        ui.html(
            f'<div style="display:flex;align-items:baseline;gap:.7rem;'
            f'flex-wrap:wrap">'
            f'<span style="font-size:18px;font-weight:700" class="{side_cls}">'
            f'{html.escape(direction)} ${html.escape(ticker or "")}</span>'
            f'<span class="fnt" style="font-size:12px">'
            f'conviction {conv}/5 · size {size_pct:.1f}% of wallet'
            f'</span></div>'
            f'<div style="font-size:12.5px;margin-top:.4rem;line-height:1.45">'
            f'<b>Thesis:</b> {html.escape(thesis)}</div>'
            f'<div style="font-size:12px;margin-top:.3rem;line-height:1.4;'
            f'color:var(--muted)"><b>Risk:</b> {html.escape(risks)}</div>'
        )
        with ui.element("div").classes("fr-row").style(
            "margin-top:.6rem;gap:.5rem;align-items:center"
        ):
            exec_btn = ui.button(
                f"Execute as recommended  ({remaining} left today)"
            ).props("color=primary unelevated")
            ui.html(
                '<span class="fnt" style="font-size:11px">'
                'Lands on the <code>research</code> wallet. One-tap; can\'t '
                'be undone except by manually closing the position later.'
                '</span>'
            )

    async def _do_execute() -> None:
        exec_btn.disable()
        exec_btn.props("loading")
        try:
            res = await asyncio.to_thread(research_desk.execute, t["id"])
        except Exception as e:
            ui.notify(f"execution failed: {e}", type="negative")
            exec_btn.enable()
            exec_btn.props(remove="loading")
            return
        if res["ok"]:
            ui.notify(f"🟢 {res['message']}", type="positive")
            dlg.close()
        else:
            ui.notify(res["message"], type="warning")
            exec_btn.enable()
            exec_btn.props(remove="loading")

    exec_btn.on_click(_do_execute)


# ── theses (Theses tab) ────────────────────────────────────────────────────


def _theses_panel(ui, span: str = "c12") -> None:
    """Active running theses + recently-closed graveyard.

    Two sections: active cards on top (clickable → modal with the
    full body, invalidation criteria, and the timeline of linked
    events) and a `30d closed` section below for the won/lost/
    matured trail. The whole thing is read-only here — closes
    happen via the modal's action buttons or the daily review
    pipeline."""
    from .. import thesis

    with _Panel(ui, "Running theses", "🧠", span, anchor="theses"):
        host = ui.element("div").classes("fr-w")

    async def refresh() -> None:
        try:
            active = await asyncio.to_thread(thesis.list_active)
            closed = await asyncio.to_thread(thesis.list_recent_closed, 30)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"theses unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            # Headline tiles — quick at-a-glance.
            validated = sum(1 for t in closed if t["state"] == "validated")
            invalidated = sum(1 for t in closed if t["state"] == "invalidated")
            matured = sum(1 for t in closed if t["state"] == "matured")
            with ui.element("div").classes("fr-tiles").style(
                "margin-bottom:.85rem"
            ):
                for label, val, tone in (
                    ("Active",        str(len(active)),     ""),
                    ("Validated 30d", str(validated),       "pos"),
                    ("Invalidated 30d", str(invalidated),   "neg"),
                    ("Matured 30d",   str(matured),         "mut"),
                ):
                    with ui.element("div").classes("fr-tile"):
                        ui.html(f'<div class="l">{html.escape(label)}</div>')
                        ui.html(
                            f'<div class="v {tone}">{html.escape(val)}</div>'
                        )

            # Active cards
            ui.html(
                '<div class="fnt" style="font-size:10.5px;letter-spacing:.13em;'
                'text-transform:uppercase;margin:.6rem 0 .4rem">Active</div>'
            )
            if not active:
                ui.label(
                    "No active theses yet — the generator runs daily at "
                    "08:15 ET, or trigger one now via `!run-once "
                    "thesis_generate`."
                ).classes("mut").style("font-size:13px")
            else:
                with ui.element("div").classes("fr-card-grid"):
                    for t in active:
                        _render_thesis_card(ui, t)

            # Closed graveyard (compact)
            if closed:
                ui.html(
                    '<div class="fnt" style="font-size:10.5px;'
                    'letter-spacing:.13em;text-transform:uppercase;'
                    'margin:1rem 0 .4rem">Closed (30d)</div>'
                )
                with ui.element("div").classes("fr-card-grid"):
                    for t in closed:
                        _render_thesis_card(ui, t, compact=True)

    _tick_now(ui, refresh, _i("theses"), tab="theses")


def _render_thesis_card(ui, t: dict, *, compact: bool = False) -> None:
    """One thesis card — direction chip, ticker, title, invalidation
    criteria, support/challenge tally, click → modal."""
    direction = (t.get("direction") or "neutral").upper()
    state = t.get("state") or "active"
    kind_cls = (
        "call" if direction == "LONG"
        else "call short" if direction == "SHORT"
        else "news"
    )
    if state == "validated":
        state_chip = '<span class="kind call">VALIDATED</span>'
    elif state == "invalidated":
        state_chip = '<span class="kind call short">INVALIDATED</span>'
    elif state == "matured":
        state_chip = '<span class="kind">MATURED</span>'
    elif state == "closed":
        state_chip = '<span class="kind">CLOSED</span>'
    else:
        state_chip = ""

    target = t.get("target_price")
    horizon = t.get("horizon_days")
    supports = t.get("supporting_events") or 0
    challenges = t.get("challenging_events") or 0

    with ui.element("div").classes("fr-call-card").on(
        "click", lambda _e, tid=t["id"]: _open_thesis_dialog(ui, tid),
    ):
        # ── meta row ──
        meta_bits = [
            f'<span class="kind {kind_cls}">{direction}</span>',
            f'<span>conv {t.get("conviction") or 0}/5</span>',
        ]
        if state_chip:
            meta_bits.append(state_chip)
        meta_bits.append(
            f'<span class="ts">'
            f'{html.escape((t.get("created_at") or "")[:10])}</span>'
        )
        ui.html('<div class="card-meta">' + "".join(meta_bits) + '</div>')

        # ── title with ticker prefix ──
        ui.html(
            f'<div class="card-title">'
            f'<span style="color:#8fb6ff;font-weight:600;'
            f'font-family:ui-monospace,Menlo,monospace">'
            f'${html.escape(t.get("ticker") or "")}</span>'
            f'  &nbsp; {html.escape((t.get("title") or "")[:200])}</div>'
        )

        # ── tags: target/horizon/event counts ──
        tag_bits: list[str] = []
        if target is not None:
            tag_bits.append(f'<span class="src">target {target:.4g}</span>')
        if horizon:
            tag_bits.append(f'<span class="src">{horizon}d horizon</span>')
        if supports or challenges:
            ratio_tone = (
                "pos" if supports >= challenges * 2 and supports > 0
                else "neg" if challenges >= supports * 2 and challenges > 0
                else "mut"
            )
            tag_bits.append(
                f'<span class="delta {ratio_tone}">'
                f'+{supports} / -{challenges} events</span>'
            )
        if tag_bits:
            ui.html(
                '<div class="card-tags">' + "".join(tag_bits) + '</div>'
            )

        # ── invalidation criteria (excerpt) — compact rows skip this ──
        if not compact and t.get("invalidation_criteria"):
            ui.html(
                f'<div class="card-excerpt">'
                f'<b>Kills it:</b> '
                f'{html.escape(t["invalidation_criteria"][:200])}</div>'
            )

        # ── actions ──
        if state == "active":
            ui.html(
                '<div class="card-actions">'
                '<span class="btn primary">🧠 Open thesis</span>'
                '<a class="btn" href="#markets" '
                'onclick="event.stopPropagation()">📈 Markets</a>'
                '</div>'
            )
        else:
            close_reason = t.get("close_reason") or ""
            ui.html(
                '<div class="card-actions">'
                '<span class="btn primary">🧠 Read trail</span>'
                + (f'<span class="btn">{html.escape(close_reason[:80])}</span>'
                   if close_reason else "")
                + '</div>'
            )


async def _open_thesis_dialog(ui, thesis_id: int) -> None:
    """Click handler on a thesis card — modal with the full body,
    invalidation criteria, the chronological event timeline, and
    action buttons to validate / invalidate / mark matured / close."""
    from .. import thesis

    try:
        t = await asyncio.to_thread(thesis.get_thesis, thesis_id)
    except Exception as e:
        ui.notify(f"thesis load failed: {e}", type="negative")
        return
    if t is None:
        ui.notify(f"thesis #{thesis_id} not found", type="warning")
        return

    direction = (t.get("direction") or "neutral").upper()
    icon = (
        "🟢" if direction == "LONG"
        else "🔴" if direction == "SHORT"
        else "⚪"
    )
    state = t.get("state") or "active"
    subtitle = f"#{thesis_id} · {state.upper()}"
    if t.get("target_price"):
        subtitle += f" · target {t['target_price']:.4g}"

    with (_MODAL_PARENT or ui.element("div")):
        dlg = ui.dialog()
    with dlg, ui.element("div").classes("fr-card fr-dlg"):
        _dossier_header(
            ui, dlg, icon, f"{direction} ${t.get('ticker') or ''}",
            subtitle=subtitle,
        )
        body = ui.element("div").classes("fr-bd")
        with body:
            # Full thesis body
            ui.html(
                '<div class="fnt" style="font-size:10.5px;'
                'letter-spacing:.13em;text-transform:uppercase;'
                'margin-bottom:.35rem">Thesis</div>'
            )
            with ui.element("div").style(
                "background:var(--surface2);border:1px solid var(--border);"
                "border-radius:8px;padding:.7rem .9rem;margin-bottom:.7rem;"
                "font-size:13px;line-height:1.5"
            ):
                ui.html(html.escape(t.get("title") or "")).classes(
                    "card-title"
                )
                ui.html(
                    f'<div style="margin-top:.45rem">'
                    f'{html.escape(t.get("body") or "")}</div>'
                )
                if t.get("invalidation_criteria"):
                    ui.html(
                        f'<div style="margin-top:.55rem;color:var(--warn)">'
                        f'<b>Kills it:</b> '
                        f'{html.escape(t["invalidation_criteria"])}</div>'
                    )

            # Action buttons (active theses only)
            if state == "active":
                with ui.element("div").classes("fr-row").style(
                    "margin-bottom:.7rem;gap:.4rem"
                ):
                    for label, st, tone in (
                        ("✅ Validated",   "validated",   "color=positive"),
                        ("❌ Invalidated", "invalidated", "color=negative"),
                        ("⏳ Matured",     "matured",     "color=primary"),
                        ("✖ Close",        "closed",      "flat dense"),
                    ):
                        ui.button(
                            label,
                            on_click=lambda _e, s=st, lbl=label:
                                _close_thesis(ui, dlg, t["id"], s, lbl),
                        ).props(f"size=sm {tone} unelevated").style(
                            "font-size:10px"
                        )
            elif t.get("close_reason"):
                ui.html(
                    f'<div class="fr-alert" style="background:var(--surface2);'
                    f'border:1px solid var(--border);color:var(--muted);'
                    f'margin-bottom:.7rem">'
                    f'Closed as <b>{html.escape(state)}</b>: '
                    f'{html.escape(t["close_reason"])}</div>'
                )

            # Event timeline
            events = t.get("events") or []
            ui.html(
                '<div class="fnt" style="font-size:10.5px;'
                'letter-spacing:.13em;text-transform:uppercase;'
                'margin-bottom:.35rem">Linked events (timeline)</div>'
            )
            if not events:
                ui.label(
                    "No events linked yet. New news/filings on this "
                    "ticker will appear here automatically."
                ).classes("mut").style("font-size:12px")
            else:
                with ui.element("div").classes("fr-feed"):
                    for e in events:
                        _render_event_row(ui, e)


def _close_thesis(ui, dlg, thesis_id: int, state: str, label: str) -> None:
    """Action button handler — close the thesis with the chosen state."""
    from .. import thesis
    try:
        ok = thesis.close_thesis(thesis_id, state=state, reason=label)
    except Exception as e:
        ui.notify(f"close failed: {e}", type="negative")
        return
    if ok:
        ui.notify(f"thesis #{thesis_id} → {state}", type="positive")
        dlg.close()
    else:
        ui.notify(
            f"thesis #{thesis_id} couldn't be closed (already non-active?)",
            type="warning",
        )


def _render_event_row(ui, e: dict) -> None:
    """One row in the thesis timeline."""
    impact = (e.get("impact") or "neutral").lower()
    impact_cls = (
        "call" if impact == "supports"
        else "call short" if impact == "challenges"
        else "news"
    )
    impact_label = (
        "SUPPORTS" if impact == "supports"
        else "CHALLENGES" if impact == "challenges"
        else "NEUTRAL"
    )
    ts = (e.get("created_at") or "")[:16].replace("T", " ")
    kind = (e.get("kind") or "").upper()[:8]
    with ui.element("div").classes("fr-feed-row"):
        ui.html(
            f'<span class="kind {impact_cls}">{impact_label}</span>'
        )
        with ui.element("div").classes("body"):
            ui.html(
                f'<span style="color:var(--text)">'
                f'{html.escape((e.get("description") or "")[:200])}</span>'
            )
            meta_bits = [
                f'<span class="src">{html.escape(kind)}</span>',
            ]
            if e.get("rationale"):
                meta_bits.append(
                    f'<span class="mut">'
                    f'{html.escape(e["rationale"][:160])}</span>'
                )
            ui.html('<div class="meta">' + " · ".join(meta_bits) + "</div>")
        ui.html(f'<span class="ts">{html.escape(ts)}</span>')


# ── research wallet trade history (Research tab) ──────────────────────────


def _research_wallet_panel(ui, span: str = "c12") -> None:
    """Open + recently-closed trades on the `research` wallet — the
    audit surface for "where did my trade go?". Closed rows include
    the reason they closed (read from `FundTrade.close_reason`),
    realized P&L, and the open_reason (which embeds the Research Desk
    task id and the original thesis)."""
    from .. import funds

    with _Panel(ui, "Research wallet — book", "💼", span,
                anchor="research-wallet"):
        host = ui.element("div").classes("fr-w")

    async def refresh() -> None:
        try:
            d = await asyncio.to_thread(
                funds.trade_history, funds.RESEARCH_WALLET_NAME, 90,
            )
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"wallet unavailable: {e}").classes("fnt")
            return
        if d is None:
            host.clear()
            with host:
                ui.label(
                    "Research wallet not seeded yet — restart the bot."
                ).classes("mut")
            return

        host.clear()
        with host:
            # ── headline tiles ───────────────────────────────────────────
            with ui.element("div").classes("fr-tiles").style(
                "margin-bottom:.7rem"
            ):
                for label, val, tone in (
                    ("Equity",   _usd(d["equity"]),       ""),
                    ("Return",   _pct(d["ret_pct"]),      _tone(d["ret_pct"])),
                    ("Cash",     _usd(d["cash"]),         ""),
                    ("Open",     str(len(d["open"])),     ""),
                    ("Closed (90d)", str(len(d["closed"])), ""),
                ):
                    with ui.element("div").classes("fr-tile"):
                        ui.html(f'<div class="l">{html.escape(label)}</div>')
                        ui.html(
                            f'<div class="v {tone}">{html.escape(val)}</div>'
                        )

            # ── open positions ───────────────────────────────────────────
            ui.html(
                '<div class="fnt" style="font-size:10.5px;'
                'letter-spacing:.13em;text-transform:uppercase;'
                'margin:.85rem 0 .35rem">Open</div>'
            )
            if not d["open"]:
                ui.label("No open positions on the research wallet.").classes(
                    "mut"
                ).style("font-size:13px")
            else:
                for t in d["open"]:
                    side_cls = "sd-l" if t["side"] == "long" else "sd-s"
                    mark_cls = "" if t["mark_live"] else "fnt"
                    with ui.element("div").classes("fr-feed-row").style(
                        "align-items:center"
                    ):
                        ui.html(
                            f'<span class="kind {"call" if t["side"]=="long" else "call short"}">'
                            f'{t["side"].upper()}</span>'
                        )
                        with ui.element("div").classes("body"):
                            ui.html(
                                f'<span class="sd {side_cls}">'
                                f'{t["side"].upper()}</span>'
                                f'<span class="tk">${html.escape(t["ticker"])}</span>'
                                f' &nbsp; <span class="num">{t["qty"]:g}</span>'
                                f' @ <span class="num">{t["entry"]:.4g}</span>'
                                f' → <span class="num {mark_cls}">{t["mark"]:.4g}</span>'
                                f' &nbsp; <span class="{_tone(t["upnl"])}">'
                                f'{_susd(t["upnl"])} '
                                f'({_pct(t["upnl_pct"])})</span>'
                            )
                            if t.get("open_reason"):
                                ui.html(
                                    f'<div class="meta">'
                                    f'{html.escape(t["open_reason"][:280])}'
                                    f'</div>'
                                )
                        ui.html(
                            f'<span class="ts">'
                            f'{html.escape(t["entry_at"][:10])}</span>'
                        )

            # ── closed positions ─────────────────────────────────────────
            ui.html(
                '<div class="fnt" style="font-size:10.5px;'
                'letter-spacing:.13em;text-transform:uppercase;'
                'margin:.85rem 0 .35rem">Closed (90d)</div>'
            )
            if not d["closed"]:
                ui.label(
                    "No closed trades yet on the research wallet."
                ).classes("mut").style("font-size:13px")
            else:
                for t in d["closed"]:
                    side_cls = "sd-l" if t["side"] == "long" else "sd-s"
                    realized_tone = _tone(t.get("realized_pnl") or 0)
                    with ui.element("div").classes("fr-feed-row").style(
                        "align-items:center"
                    ):
                        # green/red kind chip mirrors realized P&L sign so
                        # the user can see at a glance whether the close
                        # was a winner or a loser without reading numbers.
                        chip_kind = (
                            "call" if (t.get("realized_pnl") or 0) > 0
                            else "call short"
                        )
                        ui.html(
                            f'<span class="kind {chip_kind}">'
                            f'{"WIN" if (t.get("realized_pnl") or 0) > 0 else "LOSS"}'
                            f'</span>'
                        )
                        with ui.element("div").classes("body"):
                            ui.html(
                                f'<span class="sd {side_cls}">'
                                f'{t["side"].upper()}</span>'
                                f'<span class="tk">${html.escape(t["ticker"])}</span>'
                                f' &nbsp; <span class="num">{t["qty"]:g}</span>'
                                f' @ <span class="num">{t["entry"]:.4g}</span>'
                                f' → <span class="num">'
                                f'{t["exit"]:.4g if t.get("exit") else "—"}</span>'
                                f' &nbsp; <span class="{realized_tone}">'
                                f'{_susd(t.get("realized_pnl") or 0)} '
                                f'({_pct(t.get("realized_pct") or 0)})'
                                f'</span>'
                            )
                            details: list[str] = []
                            if t.get("close_reason"):
                                details.append(
                                    f'closed: {html.escape(t["close_reason"])[:140]}'
                                )
                            if t.get("open_reason"):
                                details.append(
                                    html.escape(t["open_reason"][:200])
                                )
                            if details:
                                ui.html(
                                    '<div class="meta">'
                                    + ' · '.join(details)
                                    + '</div>'
                                )
                        ui.html(
                            f'<span class="ts">'
                            f'{html.escape(t["exit_at"][:10] if t.get("exit_at") else "—")}'
                            f'</span>'
                        )

    _tick_now(ui, refresh, _i("research_wallet"), tab="research")


# ── research desk panel (Research tab) ────────────────────────────────────


def _research_panel(ui, span: str = "c12") -> None:
    """The Research Desk surface: prompt → recommendation → confirm trade.

    Top row: prompt textarea + Run button + remaining-executions badge.
    Below: chronological task list. Click any row → modal with the
    dossier + (if applicable) the Execute button. The bot never auto-
    fires; nothing trades without the user clicking Execute."""
    from .. import research_desk

    state: dict = {"in_flight": False}

    with _Panel(ui, "Research desk", "🔬", span, anchor="research-desk"):
        # Input row.
        prompt = ui.textarea(
            placeholder=(
                "Ask the bot to look into something specific. e.g.:\n"
                "'There's a Reddit thread on Trump's $2B quantum investment "
                "— is there a tradable name here?'"
            ),
        ).props("dense outlined dark rows=3").classes("fr-w")

        with ui.element("div").classes("fr-row").style(
            "margin-top:.55rem;gap:.6rem;align-items:center"
        ):
            run_btn = ui.button("Run research").props(
                "color=primary unelevated"
            )
            remaining_label = ui.label("").classes("fnt").style(
                "font-size:11.5px"
            )
            ui.html(
                '<span class="fnt" style="font-size:11px;margin-left:auto">'
                f'Limits: ≤{research_desk._RATE_LIMIT_PER_DAY} executions/day · '
                f'conviction floor {research_desk._CONVICTION_FLOOR}/5 · '
                f'size {research_desk._MIN_SIZE_PCT:g}–{research_desk._MAX_SIZE_PCT:g}% '
                'of the research wallet'
                '</span>'
            )

        # History.
        ui.html(
            '<div class="fnt" style="font-size:10.5px;letter-spacing:.13em;'
            'text-transform:uppercase;margin:1rem 0 .35rem">History</div>'
        )
        host = ui.element("div").classes("fr-feed")

    def _update_remaining() -> None:
        n = research_desk.executions_remaining_today()
        remaining_label.set_text(f"{n} execution(s) left today")
        if n <= 0:
            remaining_label.classes(replace="fnt neg")
        elif n == 1:
            remaining_label.classes(replace="fnt warn")
        else:
            remaining_label.classes(replace="fnt")

    async def _run() -> None:
        if state["in_flight"]:
            return
        text = (prompt.value or "").strip()
        if not text:
            ui.notify("type a prompt first", type="warning")
            return
        state["in_flight"] = True
        run_btn.disable()
        run_btn.props("loading")
        ui.notify("researching…", type="info")
        task_id = None
        try:
            task_id = await research_desk.run_research(text)
        except Exception as e:
            ui.notify(f"research failed: {e}", type="negative")
        finally:
            state["in_flight"] = False
            run_btn.enable()
            run_btn.props(remove="loading")
        if task_id is None:
            return
        prompt.value = ""
        await refresh()
        # Auto-open the freshly-created task so the user doesn't have to
        # hunt for it in the list. The modal carries its own dossier
        # render path so this is safe even on a duplicate (cached) task.
        await _open_research_dialog(ui, task_id)

    run_btn.on_click(_run)

    async def refresh() -> None:
        _update_remaining()
        try:
            rows = await asyncio.to_thread(research_desk.list_recent, 30)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"research unavailable: {e}").classes("fnt")
            return
        host.clear()
        with host:
            if not rows:
                ui.label(
                    "No research tasks yet — type a prompt above to start."
                ).classes("mut").style("font-size:13px")
                return
            now = datetime.now(timezone.utc)
            for r in rows:
                ts = datetime.fromisoformat(r["created_at"])
                ago = _ago_short(now - ts)
                v = (r.get("verdict") or "").upper()
                kind_cls = (
                    "call" if v == "TRADE"
                    else "filing" if v == "WATCHLIST"
                    else "news"
                )
                label = v or "…"
                if not r.get("has_dossier"):
                    label = "…"
                    kind_cls = "news"
                with ui.element("div").classes("fr-feed-row").style(
                    "cursor:pointer"
                ).on(
                    "click",
                    lambda _e, tid=r["id"]:
                        _open_research_dialog(ui, tid),
                ):
                    ui.html(
                        f'<span class="kind {kind_cls}">'
                        f'{html.escape(label)}</span>'
                    )
                    with ui.element("div").classes("body"):
                        ui.html(
                            f'<span style="color:var(--text)">'
                            f'{html.escape(r["prompt"][:140])}</span>'
                        )
                        meta_bits = []
                        if r.get("rec_ticker"):
                            tk = r["rec_ticker"]
                            d = (r.get("rec_direction") or "").upper()
                            meta_bits.append(
                                f'<span class="tk">'
                                f'{d} ${html.escape(tk)}</span>'
                            )
                        if r.get("rec_conviction"):
                            meta_bits.append(f'conv {r["rec_conviction"]}/5')
                        if r.get("executed_at"):
                            meta_bits.append(
                                '<span class="pos">✓ executed</span>'
                            )
                        elif v == "TRADE":
                            meta_bits.append(
                                '<span class="warn">pending execute</span>'
                            )
                        if meta_bits:
                            ui.html(
                                '<div class="meta">' + " · ".join(meta_bits)
                                + "</div>"
                            )
                    ui.html(f'<span class="ts">{ago}</span>')

    _tick_now(ui, refresh, _i("research_desk"), tab="research")


# ── scorecard ───────────────────────────────────────────────────────────────


def _scorecard_panel(ui, span: str = "c5") -> None:
    from .. import scorecard

    with _Panel(ui, "Scorecard", "🎯",
                span, anchor="scorecard"):
        host = ui.element("div").classes("fr-w")

    def _load() -> dict:
        tr = scorecard.track_record_summary()
        note = scorecard._calibration_note(tr.get("by_conviction", {}))
        return {"tr": tr, "note": note}

    async def refresh() -> None:
        try:
            d = await asyncio.to_thread(_load)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"scorecard unavailable: {e}").classes(
                    "fnt"
                ).style("font-size:12px")
            return
        tr, note = d["tr"], d["note"]
        o = tr["overall"]
        host.clear()
        with host:
            if not o["n"]:
                ui.label(
                    "No calls scored yet — needs a few days of marked "
                    "1d/5d returns to populate."
                ).classes("mut").style("font-size:13px")
                return
            rate = o["hits"] / o["n"] * 100
            with ui.element("div").classes("fr-end").style(
                "margin-bottom:.7rem"
            ):
                ui.label(f"{rate:.0f}%").style(
                    "font-size:30px;font-weight:700;line-height:1;"
                    "font-variant-numeric:tabular-nums"
                )
                ui.label(f"{o['hits']}/{o['n']} calls hit").classes(
                    "mut"
                ).style("font-size:12px;padding-bottom:.25rem")
            ui.html(_bar_html(rate)).classes("fr-w").style(
                "margin-bottom:.9rem"
            )

            by_src = {k: v for k, v in tr["by_source"].items() if v["n"]}
            if by_src:
                ui.label("BY SOURCE").classes("fnt").style(
                    "font-size:10px;letter-spacing:.13em;"
                    "margin-bottom:.35rem"
                )
                for src in sorted(by_src):
                    v = by_src[src]
                    pr = v["hits"] / v["n"] * 100
                    with ui.element("div").classes("fr-row").style(
                        "padding:.18rem 0;font-size:12px"):
                        ui.label(src).classes("tk").style(
                            "width:9rem;overflow:hidden;"
                            "text-overflow:ellipsis;white-space:nowrap"
                        )
                        ui.html(_bar_html(pr)).style("flex:1")
                        ui.label(
                            f"{v['hits']}/{v['n']} · {pr:.0f}%"
                        ).classes("mut num").style(
                            "width:5.5rem;text-align:right"
                        )

            bc = tr.get("by_conviction", {})
            conv = [(k, bc[k]) for k in ("high", "med", "low")
                    if bc.get(k, {}).get("n")]
            if conv:
                ui.element("div").style("height:.7rem")
                with ui.element("div").classes("fr-tiles"):
                    for k, v in conv:
                        with ui.element("div").classes("fr-tile").style(
                            "flex:1"
                        ):
                            ui.html(
                                f'<div class="l">{k}</div>'
                                f'<div class="v">'
                                f'{v["hits"] / v["n"] * 100:.0f}%</div>'
                                f'<div class="fnt" style="font-size:10px">'
                                f'{v["hits"]}/{v["n"]}</div>'
                            )

            if note:
                ui.element("div").style("height:.7rem")
                with ui.element("div").classes("fr-alert warn"):
                    ui.html(f"⚠️&nbsp;&nbsp;{html.escape(note)}")

    _tick_now(ui, refresh, _i("scorecard"), tab="calls")


# ── paper book ──────────────────────────────────────────────────────────────


def _book_panel(ui, span: str = "c7") -> None:
    from .. import portfolio

    with _Panel(ui, "Paper book", "📈",
                span, anchor="paper-book"):
        host = ui.element("div").classes("fr-w")

    def _load() -> dict:
        return {
            "pos": portfolio.open_positions(),
            "real": portfolio.realized_summary(),
        }

    async def refresh() -> None:
        try:
            d = await asyncio.to_thread(_load)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"book unavailable: {e}").classes("fnt").style(
                    "font-size:12px"
                )
            return
        pos, r = d["pos"], d["real"]
        host.clear()
        with host:
            if not pos:
                ui.label("No open positions.").classes("mut").style(
                    "font-size:13px"
                )
            else:
                with ui.element("div").classes("fr-bgrid"):
                    with ui.element("div").classes("fr-bh"):
                        # First header cell mirrors the data row's fixed-
                        # width SIDE chip + ticker so the columns align.
                        ui.html(
                            '<span style="display:inline-block;'
                            'width:3.2rem">Side</span>Ticker'
                        )
                        for h in ("Qty", "Entry", "Mark",
                                  "P&L", "%", ""):
                            ui.html(html.escape(h))
                    for p in pos:
                        long = p["side"] == "long"
                        side_cls = "sd-l" if long else "sd-s"
                        side_lbl = "LONG" if long else "SHORT"
                        with ui.element("div").classes("fr-brow"):
                            ui.html(
                                f'<span class="sd {side_cls}">'
                                f'{side_lbl}</span>'
                                f'<span class="tk">'
                                f'${html.escape(p["ticker"])}</span>'
                            )
                            ui.html(f"{p['qty']:g}")
                            ui.html(f"{p['entry']:.4g}")
                            if p["mark"] is not None:
                                ui.html(f"{p['mark']:.4g}")
                            else:
                                ui.html("—").classes("fnt")
                            pnl, pct = p.get("pnl"), p.get("pnl_pct")
                            if pnl is not None:
                                ui.html(_susd(pnl)).classes(_tone(pnl))
                            else:
                                ui.html("—").classes("fnt")
                            if pct is not None:
                                ui.html(_pct(pct)).classes(_tone(pct))
                            else:
                                ui.html("—").classes("fnt")
                            ui.button(
                                "Close",
                                on_click=lambda _e, t=p["ticker"]:
                                    _close_position_action(
                                        ui, t, refresh
                                    ),
                            ).props(
                                "flat dense size=sm color=primary"
                            ).style("font-size:10px")

            ui.element("div").style(
                "height:1px;background:var(--border);margin:.8rem 0 .7rem"
            )
            wr = (
                f"{r['wins']}/{r['closed']} "
                f"({r['wins'] / r['closed'] * 100:.0f}%)"
                if r["closed"] else "—"
            )
            with ui.element("div").classes("fr-tiles"):
                with ui.element("div").classes("fr-tile").style("flex:1"):
                    ui.html(
                        '<div class="l">Realized P&L</div>'
                        f'<div class="v {_tone(r["realized_pnl"])}">'
                        f'{_susd(r["realized_pnl"])}</div>'
                    )
                with ui.element("div").classes("fr-tile").style("flex:1"):
                    ui.html(
                        '<div class="l">Closed win rate</div>'
                        f'<div class="v">{wr}</div>'
                    )

    _tick_now(ui, refresh, _i("book"), tab="portfolio")


async def _close_position_action(ui, ticker: str, refresh_fn) -> None:
    """Inline Close button → same chokepoint as `!close TICKER` (paper).
    Mark=None lets `portfolio.close_position` use the live mark from
    PriceContext (the chat handler does the same)."""
    from .. import portfolio

    try:
        p = await asyncio.to_thread(
            portfolio.close_position, ticker, None
        )
    except Exception as e:
        ui.notify(f"close failed: {e}", type="negative")
        return
    if p is None:
        ui.notify(
            f"no open position on ${ticker}", type="warning"
        )
    else:
        sign = "🟢" if (p.realized_pnl or 0) >= 0 else "🔴"
        ui.notify(
            f"{sign} closed {p.side} {p.qty:g} ${ticker} "
            f"@ {p.exit_price:.4g} · realized {p.realized_pnl:+.2f}",
            type=("positive" if (p.realized_pnl or 0) >= 0
                  else "warning"),
        )
    await refresh_fn()


# ── health ──────────────────────────────────────────────────────────────────


def _health_panel(ui, verdict_chip, span: str = "c5") -> None:
    from .. import health

    with _Panel(ui, "Health & diagnostics", "🩺",
                span, anchor="health"):
        host = ui.element("div").classes("fr-w")

    _cls = {"ok": "ok", "warn": "warn", "crit": "crit", "error": "idle"}

    async def refresh() -> None:
        try:
            rep = await asyncio.to_thread(health.health_report)
        except Exception as e:
            host.clear()
            with host:
                ui.label(f"health unavailable: {e}").classes("fnt").style(
                    "font-size:12px"
                )
            return

        cls = _cls.get(rep["verdict"], "idle")
        verdict_chip.set_content(
            f'<span class="fr-pill {cls}">{rep["marker"]} '
            f'{html.escape(rep["headline"])}</span>'
        )

        host.clear()
        with host:
            with ui.element("div").classes(f"fr-pill {cls}").style(
                "display:inline-block;margin-bottom:.7rem"
            ):
                ui.html(
                    f'{rep["marker"]} {html.escape(rep["headline"])}'
                )
            for c in rep["critical"]:
                with ui.element("div").classes("fr-alert crit").style(
                    "margin-bottom:.4rem"
                ):
                    ui.html(f"🔴&nbsp;&nbsp;{html.escape(c)}")
            for w in rep["warnings"]:
                with ui.element("div").classes("fr-alert warn").style(
                    "margin-bottom:.4rem"
                ):
                    ui.html(f"⚠️&nbsp;&nbsp;{html.escape(w)}")

            if rep["jobs"]:
                ui.label(
                    f"JOBS · 24H  ({rep['jobs_runs']} runs / "
                    f"{rep['jobs_n']} jobs · {rep['jobs_fail']} failed)"
                ).classes("fnt").style(
                    "font-size:10px;letter-spacing:.1em;"
                    "margin:.6rem 0 .4rem"
                )
                with ui.element("div").style(
                    "max-height:9rem;overflow-y:auto;display:grid;"
                    "grid-template-columns:1fr 1fr;gap:.1rem .9rem"
                ):
                    for j in rep["jobs"]:
                        d = "ok" if j["ok"] else "bad"
                        fail = (
                            f' <span class="neg">·{j["fail"]}✗</span>'
                            if j["fail"] else ""
                        )
                        ui.html(
                            f'<div style="display:flex;align-items:center;'
                            f'gap:.4rem;font-size:11.5px;padding:.12rem 0">'
                            f'<span class="dot {d}"></span>'
                            f'<span class="tk" style="flex:1;overflow:hidden;'
                            f'text-overflow:ellipsis;white-space:nowrap">'
                            f'{html.escape(j["id"])}</span>'
                            f'<span class="fnt num">×{j["runs"]}{fail}</span>'
                            f"</div>"
                        )

            s = rep.get("streams") or {}
            if s:
                ui.element("div").style("height:.7rem")
                with ui.element("div").classes("fr-tiles"):
                    for key in ("filings", "news", "reddit", "bars"):
                        n = s.get(key, 0)
                        bad = (key == "bars" and n == 0)
                        with ui.element("div").classes("fr-tile").style(
                            "flex:1;padding:.4rem .5rem"
                        ):
                            ui.html(
                                f'<div class="l">{key}</div>'
                                f'<div class="v {"neg" if bad else ""}" '
                                f'style="font-size:14px">{n}</div>'
                            )
                lm = rep.get("llm", {})
                ui.html(
                    f'<div class="fnt" style="font-size:11px;'
                    f'margin-top:.6rem">LLM {lm.get("calls", 0):,} calls · '
                    f'{lm.get("errors", 0)} failed ({lm.get("rate", 0)}%) '
                    f'&nbsp;·&nbsp; watchlist {rep["watchlist"]} '
                    f'&nbsp;·&nbsp; open calls {rep["open_calls"]}</div>'
                )
            if rep.get("faded"):
                ui.html(
                    f'<div class="fnt" style="font-size:11px;'
                    f'margin-top:.45rem">auto-fade active: '
                    f'{html.escape(" · ".join(rep["faded"]))}</div>'
                )

    _tick_now(ui, refresh, _i("health"), tab="system")


# ── scheduler control ───────────────────────────────────────────────────────


def _scheduler_panel(ui, span: str = "c4") -> None:
    with _Panel(ui, "Scheduler", "⏱", span, anchor="scheduler"):
        summary = ui.label("").classes("fnt").style(
            "font-size:11px;margin-bottom:.5rem"
        )

        @ui.refreshable
        def rows() -> None:
            if _scheduler is None:
                ui.label("scheduler not attached").classes("mut").style(
                    "font-size:12px"
                )
                return
            try:
                jobs = sorted(_scheduler.get_jobs(), key=lambda j: j.id)
            except Exception as e:
                ui.label(f"scheduler read failed: {e}").classes(
                    "fnt"
                ).style("font-size:12px")
                return
            paused_n = 0
            with ui.element("div").style(
                "max-height:19rem;overflow-y:auto"
            ):
                for job in jobs:
                    # APScheduler 3.x: `next_run_time` is absent until the
                    # scheduler starts, None when paused, else tz-aware.
                    # main.py starts it before mount, so None == paused in
                    # production; getattr keeps the panel safe regardless.
                    nrt = getattr(job, "next_run_time", None)
                    paused = nrt is None
                    paused_n += paused
                    nxt = (
                        "paused" if paused
                        else nrt.astimezone(timezone.utc).strftime(
                            "%H:%M:%SZ"
                        )
                    )
                    with ui.element("div").classes("fr-jobrow"):
                        ui.html(
                            f'<span class="dot '
                            f'{"idle" if paused else "ok"}"></span>'
                        )
                        ui.label(job.id).classes("jid")
                        ui.label(nxt).classes("jt")
                        ui.button(
                            "Resume" if paused else "Pause",
                            on_click=lambda _e, j=job.id, p=paused:
                                _toggle_job(ui, j, p, rows),
                        ).props("flat dense size=sm").style(
                            "font-size:10px"
                        )
                        ui.button(
                            "Run",
                            on_click=lambda _e, j=job.id:
                                _run_job_now(ui, j, rows),
                        ).props(
                            "flat dense size=sm color=primary"
                        ).style("font-size:10px")
            summary.set_text(
                f"{len(jobs)} jobs · {paused_n} paused · "
                f"{len(jobs) - paused_n} active"
            )

        rows()
        ui.timer(7.0, rows.refresh)


def _toggle_job(ui, job_id: str, was_paused: bool, panel) -> None:
    try:
        if was_paused:
            _scheduler.resume_job(job_id)
            ui.notify(f"▶ resumed {job_id}", type="positive")
        else:
            _scheduler.pause_job(job_id)
            ui.notify(f"⏸ paused {job_id}", type="warning")
    except Exception as e:
        ui.notify(f"job toggle failed: {e}", type="negative")
    panel.refresh()


def _run_job_now(ui, job_id: str, panel) -> None:
    try:
        # Re-arms the next fire to now. APScheduler dispatches it on this same
        # loop; max_instances=1/coalesce already guard against pile-ups. (This
        # also un-pauses a paused job — intended for an explicit "run now".)
        _scheduler.modify_job(
            job_id, next_run_time=datetime.now(timezone.utc)
        )
        ui.notify(f"⏵ {job_id} scheduled to run now", type="positive")
    except Exception as e:
        ui.notify(f"run-now failed: {e}", type="negative")
    panel.refresh()


# ── manual call ─────────────────────────────────────────────────────────────


def _calls_panel(ui, span: str = "c4") -> None:
    """The one write control: log a manual directional CALL through the same
    `scorecard.record_call` chokepoint the pipelines use (auto-fade, de-dup
    and mark-at-call all apply identically — source tagged `dashboard`)."""
    from .. import scorecard

    with _Panel(ui, "Log a call", "📝",
                span, anchor="log-call"):
        tk = ui.input("Ticker").props(
            "dense outlined dark"
        ).classes("fr-w")
        with ui.element("div").classes("fr-row").style(
            "margin-top:.5rem"
        ):
            direction = ui.select(
                {"long": "Long", "short": "Short"}, value="long"
            ).props("dense outlined dark options-dense").classes("fr-grow")
            ui.label("Conviction").classes("mut").style("font-size:11px")
            conv_lbl = ui.label("3").style(
                "font-size:13px;font-weight:600;width:1rem;text-align:center"
            )
        conv = ui.slider(min=1, max=5, step=1, value=3).props(
            "label-always color=primary"
        ).classes("fr-w")
        conv.on_value_change(
            lambda e: conv_lbl.set_text(str(int(e.args)))
        )
        thesis = ui.textarea("Thesis").props(
            "dense outlined dark rows=3"
        ).classes("fr-w").style("margin-top:.3rem")

        async def submit() -> None:
            ticker = (tk.value or "").strip().upper().lstrip("$")
            th = (thesis.value or "").strip()
            if not ticker or not th:
                ui.notify(
                    "ticker and thesis are required", type="warning"
                )
                return
            try:
                await asyncio.to_thread(
                    scorecard.record_call,
                    ticker, direction.value, "dashboard", th,
                    int(conv.value),
                )
                ui.notify(
                    f"📒 logged {direction.value} ${ticker} "
                    f"(conv {int(conv.value)})",
                    type="positive",
                )
                tk.value = ""
                thesis.value = ""
            except Exception as e:
                ui.notify(f"record_call failed: {e}", type="negative")

        ui.button("Log call", on_click=submit).props(
            "color=primary unelevated"
        ).classes("fr-w").style("margin-top:.6rem")


# ── system ──────────────────────────────────────────────────────────────────


def _system_panel(ui, span: str = "c4") -> None:
    from . import sysinfo

    with _Panel(ui, "System", "🖥", span, anchor="system"):
        grid = ui.element("div").classes("fr-3")
        with grid:
            cpu = _tile(ui, "CPU")
            ram = _tile(ui, "RAM")
            thr = _tile(ui, "Threads")
            fds = _tile(ui, "FDs")
            updays = _tile(ui, "Uptime")
            dbsz = _tile(ui, "DB size")
        with ui.element("div").classes("fr-tiles").style("margin-top:.5rem"):
            with ui.element("div").classes("fr-tile").style("flex:1"):
                ui.html('<div class="l">LLM calls</div>')
                llm_c = ui.label("—").classes("v")
            with ui.element("div").classes("fr-tile").style("flex:1"):
                ui.html('<div class="l">LLM errors</div>')
                llm_e = ui.label("—").classes("v")

    async def refresh() -> None:
        try:
            s = await asyncio.to_thread(sysinfo.snapshot)
        except Exception:
            return
        cpu.set_text(
            f"{s['cpu_pct']}%" if s["cpu_pct"] is not None else "—"
        )
        ram.set_text(
            f"{s['rss_mb']:.0f}" if s["rss_mb"] is not None else "—"
        )
        thr.set_text(
            str(s["threads"]) if s["threads"] is not None else "—"
        )
        fds.set_text(str(s["fds"]) if s["fds"] is not None else "—")
        updays.set_text(sysinfo.fmt_uptime(s["uptime_s"]))
        dbsz.set_text(s["db_human"])
        llm_c.set_text(f"{s['llm_calls']:,}")
        llm_e.set_text(str(s["llm_errors"]))
        llm_e.classes(
            replace="v " + ("neg" if s["llm_errors"] else "")
        )

    _tick_now(ui, refresh, _i("system"), tab="system")


# ── open paper position (!buy / !short on the dashboard) ────────────────────


def _open_form_panel(ui, span: str = "c4") -> None:
    """Open a paper position via the same chokepoint Discord !buy/!short
    uses (`portfolio.open_paper_position`). One position per ticker rule is
    enforced inside the chokepoint, not here."""
    from .. import portfolio

    with _Panel(ui, "Open paper position", "🛒", span,
                anchor="open-position"):
        tk = ui.input("Ticker").props(
            "dense outlined dark"
        ).classes("fr-w")
        with ui.element("div").classes("fr-row").style(
            "margin-top:.5rem"
        ):
            side = ui.select(
                {"long": "Long", "short": "Short"}, value="long"
            ).props(
                "dense outlined dark options-dense"
            ).classes("fr-grow")
            qty = ui.input("Qty").props(
                "dense outlined dark"
            ).classes("fr-grow")
        price = ui.input("Price (blank → last mark)").props(
            "dense outlined dark"
        ).classes("fr-w").style("margin-top:.5rem")
        note = ui.textarea("Note (optional)").props(
            "dense outlined dark rows=2"
        ).classes("fr-w").style("margin-top:.3rem")

        async def submit() -> None:
            ticker_v = (tk.value or "").strip().upper().lstrip("$")
            qty_raw = (qty.value or "").strip()
            try:
                qty_v = float(qty_raw)
            except ValueError:
                ui.notify(
                    f"couldn't parse qty `{qty_raw}`", type="warning"
                )
                return
            if not ticker_v or qty_v <= 0:
                ui.notify(
                    "ticker and positive qty are required",
                    type="warning",
                )
                return
            price_raw = (price.value or "").strip()
            price_v: float | None = None
            if price_raw:
                try:
                    price_v = float(price_raw)
                except ValueError:
                    ui.notify(
                        f"couldn't parse price `{price_raw}`",
                        type="warning",
                    )
                    return
            try:
                res = await asyncio.to_thread(
                    portfolio.open_paper_position,
                    ticker_v, side.value, qty_v,
                    price=price_v, note=(note.value or None),
                    opened_by="dashboard",
                )
            except Exception as e:
                ui.notify(f"open failed: {e}", type="negative")
                return
            if res["ok"]:
                emoji = "🟢" if side.value == "long" else "🔴"
                ui.notify(
                    f"{emoji} {res['message']}", type="positive"
                )
                tk.value = ""
                qty.value = ""
                price.value = ""
                note.value = ""
            else:
                ui.notify(res["message"], type="warning")

        ui.button("Open position", on_click=submit).props(
            "color=primary unelevated"
        ).classes("fr-w").style("margin-top:.6rem")


# ── holds (the manual tagging book — !hold / !unhold / !holdings) ────────────


_HOLDS_COLS = "grid-template-columns:minmax(0,1fr) 3rem 4rem 4rem 2.6rem"


def _holds_panel(ui, span: str = "c4") -> None:
    """Holds panel — every action goes through the same `portfolio.*`
    chokepoints Discord uses, so the two surfaces can't diverge."""
    from .. import portfolio

    with _Panel(ui, "Holds", "📌", span, anchor="holds"):
        with ui.element("div").classes("fr-row").style(
            "gap:.4rem;margin-bottom:.55rem"
        ):
            tk = ui.input(placeholder="ticker").props(
                "dense outlined dark"
            ).classes("fr-grow")
            q = ui.input(placeholder="qty").props(
                "dense outlined dark"
            ).style("max-width:5rem")
            add_btn = ui.button(icon="add").props(
                "round dense unelevated color=primary"
            )
        host = ui.element("div").classes("fr-w")

        async def refresh() -> None:
            try:
                rows = await asyncio.to_thread(portfolio.list_holds)
            except Exception as e:
                host.clear()
                with host:
                    ui.label(f"unavailable: {e}").classes("fnt").style(
                        "font-size:12px"
                    )
                return
            host.clear()
            with host:
                if not rows:
                    ui.label("No holds yet.").classes("mut").style(
                        "font-size:13px"
                    )
                    return
                with ui.element("div").classes("fr-bgrid"):
                    with ui.element("div").classes("fr-bh").style(
                        _HOLDS_COLS
                    ):
                        for h in ("Ticker", "Qty", "Price", "1d", ""):
                            ui.html(html.escape(h))
                    for r in rows:
                        with ui.element("div").classes("fr-brow").style(
                            _HOLDS_COLS
                        ):
                            ui.html(
                                f'<span class="tk">'
                                f'${html.escape(r["ticker"])}</span>'
                            )
                            if r["qty"] is not None:
                                ui.html(f"{r['qty']:g}")
                            else:
                                ui.html("—").classes("fnt")
                            if r["price"] is not None:
                                ui.html(f"{r['price']:.4g}")
                            else:
                                ui.html("—").classes("fnt")
                            d1 = r["change_1d_pct"]
                            if d1 is not None:
                                ui.html(_pct(d1)).classes(_tone(d1))
                            else:
                                ui.html("—").classes("fnt")
                            ui.button(
                                icon="close",
                                on_click=lambda _e, t=r["ticker"]:
                                    _remove_hold_action(ui, t, refresh),
                            ).props("flat dense size=sm").style(
                                "font-size:10px"
                            )

        async def _add() -> None:
            ticker_v = (tk.value or "").strip().upper().lstrip("$")
            if not ticker_v:
                ui.notify("ticker required", type="warning")
                return
            qty_raw = (q.value or "").strip()
            qty_v: float | None = None
            if qty_raw:
                try:
                    qty_v = float(qty_raw)
                except ValueError:
                    ui.notify(
                        f"couldn't parse qty `{qty_raw}`",
                        type="warning",
                    )
                    return
            try:
                res = await asyncio.to_thread(
                    portfolio.add_hold, ticker_v, qty_v
                )
            except Exception as e:
                ui.notify(f"add failed: {e}", type="negative")
                return
            if res["ok"]:
                ui.notify(f"📌 {res['message']}", type="positive")
                tk.value = ""
                q.value = ""
                await refresh()
            else:
                ui.notify(res["message"], type="warning")

        add_btn.on_click(_add)
        _tick_now(ui, refresh, _i("holds"), tab="portfolio")


async def _remove_hold_action(ui, ticker: str, refresh_fn) -> None:
    from .. import portfolio

    try:
        res = await asyncio.to_thread(portfolio.remove_hold, ticker)
    except Exception as e:
        ui.notify(f"remove failed: {e}", type="negative")
        return
    if res["ok"]:
        ui.notify(f"🗑️ {res['message']}", type="positive")
    else:
        ui.notify(res["message"], type="warning")
    await refresh_fn()


# ── watches (plain-English conditional alerts — !watch / !unwatch) ───────────


def _watches_panel(ui, span: str = "c4") -> None:
    """Watches panel. Add via plain English (the LLM compiles it to a
    spec — same `watches.add_watch` path Discord uses); remove by ID."""
    from ..pipelines import watches

    with _Panel(ui, "Watches", "🔔", span, anchor="watches"):
        box = ui.textarea(
            label="New watch (plain English)",
        ).props(
            "dense outlined dark rows=2"
        ).classes("fr-w")
        add_btn = ui.button("Add watch").props(
            "color=primary unelevated"
        ).classes("fr-w").style("margin-top:.4rem")
        host = ui.element("div").classes("fr-w").style(
            "margin-top:.6rem;max-height:18rem;overflow-y:auto"
        )

        async def refresh() -> None:
            try:
                rows = await asyncio.to_thread(watches.list_watches)
            except Exception as e:
                host.clear()
                with host:
                    ui.label(f"unavailable: {e}").classes("fnt").style(
                        "font-size:12px"
                    )
                return
            host.clear()
            with host:
                if not rows:
                    ui.label(
                        'No watches set. Try: "tell me if NVDA moves '
                        '>5% on >2x volume".'
                    ).classes("mut").style("font-size:12.5px")
                    return
                for w in rows:
                    with ui.element("div").classes("fr-watch"):
                        with ui.element("div").classes("wmain"):
                            ui.html(
                                f'<span class="wid">#{w["id"]}</span>'
                                f'{html.escape(w["raw_text"])[:240]}'
                            )
                            bits = []
                            if not w["active"]:
                                bits.append(
                                    '<span class="wpaused">paused</span>'
                                )
                            bits.append(f"×{w['trigger_count']}")
                            if w["last_triggered_at"]:
                                bits.append(
                                    f"last "
                                    f"{w['last_triggered_at']:%m-%d %H:%M}"
                                )
                            ui.html(
                                '<div class="wmeta">'
                                + " · ".join(bits)
                                + "</div>"
                            )
                        ui.button(
                            icon="close",
                            on_click=lambda _e, wid=w["id"]:
                                _remove_watch_action(ui, wid, refresh),
                        ).props("flat dense size=sm").style(
                            "font-size:10px"
                        )

        async def _add() -> None:
            text = (box.value or "").strip()
            if not text:
                ui.notify(
                    "describe what to watch first", type="warning"
                )
                return
            add_btn.disable()
            add_btn.props("loading")
            try:
                msg = await watches.add_watch(text)
            except Exception as e:
                ui.notify(f"add failed: {e}", type="negative")
                msg = None
            finally:
                add_btn.enable()
                add_btn.props(remove="loading")
            if msg:
                # add_watch returns markdown; surface as notification + reset
                # the input on what looks like a successful compile.
                positive = msg.startswith("🔔")
                ui.notify(
                    msg.split("\n", 1)[0],
                    type="positive" if positive else "warning",
                )
                if positive:
                    box.value = ""
                    await refresh()

        add_btn.on_click(_add)
        _tick_now(ui, refresh, _i("watches"), tab="watches")


async def _remove_watch_action(ui, wid: int, refresh_fn) -> None:
    from ..pipelines import watches

    try:
        res = await asyncio.to_thread(watches.remove_watch, wid)
    except Exception as e:
        ui.notify(f"remove failed: {e}", type="negative")
        return
    if res["ok"]:
        ui.notify(f"🗑️ {res['message']}", type="positive")
    else:
        ui.notify(res["message"], type="warning")
    await refresh_fn()


# ── lookup (read-only `!cmd` surface, unified) ──────────────────────────────

_LOOKUP_KINDS = (
    ("Ticker",    "ticker",    True),
    ("News",      "news",      False),
    ("Filing",    "filing",    True),
    ("Timeline",  "timeline",  True),
    ("Recent",    "recent",    False),
    ("Catalysts", "catalysts", False),
    ("Status",    "status",    False),
)


def _lookup_panel(ui, span: str = "c12") -> None:
    """One panel for every read-only `!cmd`. Each chip dispatches through
    `chat.lookup`, which is the same path Discord uses — so the dashboard
    can't drift from the bot by editing this file alone."""
    with _Panel(ui, "Lookup", "🔎", span, anchor="lookup"):
        with ui.element("div").classes("fr-row").style(
            "gap:.5rem;margin-bottom:.55rem"
        ):
            box = ui.input(
                placeholder="ticker · accession · count (leave empty when "
                            "not needed)"
            ).props("outlined dense dark").classes("fr-grow")
        result = ui.markdown(
            "_Pick a category to look up — Ticker / News / Filing / "
            "Timeline / Recent / Catalysts / Status._"
        ).classes("lookup-out")
        with ui.element("div").classes("fr-chips").style(
            "justify-content:flex-start;margin-top:.6rem"
        ):
            for label, kind, needs_arg in _LOOKUP_KINDS:
                ui.html(
                    f'<span class="chip">{html.escape(label)}</span>'
                ).on(
                    "click",
                    lambda _e, k=kind, n=needs_arg, lbl=label:
                        _run_lookup(ui, box, k, n, lbl, result),
                )


async def _run_lookup(ui, box, kind: str, needs_arg: bool,
                      label: str, out_md) -> None:
    from .. import chat

    arg = (box.value or "").strip()
    out_md.set_content(f"_{label} loading…_")
    try:
        text = await asyncio.to_thread(chat.lookup, kind, arg)
    except Exception as e:
        out_md.set_content(f"_lookup failed: {e}_")
        return
    out_md.set_content(text or f"_(no result for {label})_")


# ── live log ────────────────────────────────────────────────────────────────


def _log_panel(ui, span: str = "c12") -> None:
    from . import logbuf

    with _Panel(ui, "Live log", "📜", span, anchor="live-log"):
        view = ui.html("").classes("fr-log fr-w")

    async def refresh() -> None:
        try:
            view.set_content(_log_html(logbuf.tail(220)))
            # Stick to bottom only if the reader is already there (replacing
            # innerHTML keeps scrollTop, so a scrolled-up reader isn't yanked
            # down; someone at the tail stays pinned to new lines).
            await ui.run_javascript(
                "(()=>{const e=document.querySelector('.fr-log');"
                "if(e&&e.scrollHeight-e.scrollTop-e.clientHeight<140)"
                "e.scrollTop=e.scrollHeight;})()",
                timeout=2.0,
            )
        except Exception:
            pass

    _tick_now(ui, refresh, _i("live_log"), tab="system")


# ── chat ────────────────────────────────────────────────────────────────────

_CHAT_EXAMPLES = (
    "What's the read on $NVDA into earnings?",
    "Which wallet is leading and why?",
    "Anything notable in filings today?",
    "How calibrated are my high-conviction calls?",
)


def _chat_panel(ui, span: str = "c12") -> None:
    """Chatbox on the shared `chat.answer_question` path — same context,
    same voice as Discord !ask / @-mention, rendered as bubbles here."""
    from .. import chat

    with _Panel(ui, "Copilot", "✨", span, anchor="copilot") as body:
        body.classes("chat-wrap")
        feed = ui.element("div").classes("chat-feed")
        state = {"started": False}

        def _empty() -> None:
            state["started"] = False
            feed.clear()
            with feed:
                with ui.element("div").classes("chat-empty"):
                    ui.html(
                        '<div style="font-size:22px">✨</div>'
                        '<div>Ask the copilot anything about the book, '
                        'wallets, filings, sentiment or a ticker. Same '
                        'context and voice as Discord — just here.</div>'
                    )
                    with ui.element("div").classes("fr-chips"):
                        for ex in _CHAT_EXAMPLES:
                            ui.html(
                                f'<span class="chip">{html.escape(ex)}</span>'
                            ).on(
                                "click",
                                lambda _e, q=ex: _set_box(q),
                            )

        def _set_box(q: str) -> None:
            box.value = q
            box.run_method("focus")

        async def _scroll() -> None:
            try:
                await ui.run_javascript(
                    "(()=>{const e=document.querySelector('.chat-feed');"
                    "if(e)e.scrollTop=e.scrollHeight;})()",
                    timeout=2.0,
                )
            except Exception:
                pass

        with ui.element("div").classes("fr-row").style(
            "padding-top:.6rem;border-top:1px solid var(--border);"
            "margin-top:.4rem"
        ):
            box = ui.input(
                placeholder="Ask the copilot…  (Enter to send)"
            ).props("outlined dense dark autofocus").classes("fr-grow")
            send_btn = ui.button(icon="send").props(
                "round dense unelevated color=primary"
            )

        async def send() -> None:
            q = (box.value or "").strip()
            if not q:
                return
            box.value = ""
            box.disable()
            send_btn.disable()
            now = datetime.now().strftime("%H:%M")

            # first message clears the empty-state hint
            if not state["started"]:
                feed.clear()
                state["started"] = True

            with feed:
                with ui.element("div").classes("bub u"):
                    ui.html(
                        f'<div class="rl">you</div>'
                        f'<div>{html.escape(q)}</div>'
                        f'<div class="ts">{now}</div>'
                    )
                pending = ui.element("div").classes("bub a")
                with pending:
                    ui.html(
                        '<div class="rl">copilot</div>'
                        '<div class="typing"><i></i><i></i><i></i></div>'
                    )
            await _scroll()

            try:
                reply = await chat.answer_question(q)
            except Exception as e:
                reply = f"[LLM_ERROR] {e}"

            pending.clear()
            with pending:
                ui.html('<div class="rl">copilot</div>')
                if not reply or reply.startswith("[LLM_ERROR]"):
                    ui.html(
                        '<div class="neg">⚠️ LLM unreachable — try '
                        'again in a moment.</div>'
                    )
                else:
                    ui.markdown(reply)
                ui.html(
                    f'<div class="ts">{datetime.now().strftime("%H:%M")}'
                    f'</div>'
                )

            box.enable()
            send_btn.enable()
            box.run_method("focus")
            await _scroll()

        send_btn.on_click(send)
        box.on("keydown.enter", send)
        _empty()
