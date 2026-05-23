"""In-process operations cockpit (NiceGUI).

A localhost-only web dashboard mounted into the *same* event loop as the
Discord bot, APScheduler, and the price/LLM workers — one process, one loop,
no second SQLite writer (it reads through the same WAL engine the bot uses).
Discord stays the remote/notification surface; this is the at-the-machine
cockpit: live health, the scheduler, wallets, the call log, resource usage,
a streaming log tail, and a chatbox wired to the *same* Q&A path Discord
uses (`chat.answer_question`), plus a small control surface (pause/resume/
run-a-job-now, manual CALL entry).

`mount(scheduler)` is the only public entrypoint. It is fully isolated: any
import/bind/serve failure is swallowed and logged so the dashboard can never
take the bot down, and it is a no-op when `settings.DASHBOARD_ENABLED` is
false.
"""

from __future__ import annotations

from .app import mount

__all__ = ["mount"]
