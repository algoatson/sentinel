# Running Sentinel 24/7 (Raspberry Pi / any Linux box)

Because both LLM tiers are routed to a serverless API (`LLM_API_*` in `.env`),
the host **does not run Ollama** — it only runs the Python orchestration
(scheduler + Discord client + dashboard + SQLite). A Raspberry Pi 4/5 handles
that comfortably.

## One-time setup

```bash
# 1. clone + sync deps (creates .venv)
cd ~/Work/tradingbot
uv sync

# 2. make sure .env is filled in (Discord token, channel IDs, LLM_API_KEY…)
#    and the bot starts by hand once:
uv run python -m sentinel.main            # Ctrl-C after you see it connect

# 3. install the service (edit User= / paths in the unit first if your Pi differs)
sudo cp deploy/sentinel.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sentinel
```

## Day-to-day

```bash
systemctl status sentinel          # is it up?
journalctl -u sentinel -f          # live logs
sudo systemctl restart sentinel    # after a config / code change
sudo systemctl stop sentinel       # before a --reset
```

A `--reset` (fresh run): `sudo systemctl stop sentinel`, then
`uv run python -m sentinel.main --reset`, then `start` again.

## Pi-specific tips

- **SD-card wear:** the price poller writes to SQLite (WAL) every few minutes.
  On a Pi, put `data/` on a USB SSD/stick rather than the boot SD card, or
  accept periodic re-flashing. Symlink `data/` to the external mount, or set
  `SENTINEL_DB_URL=sqlite:////mnt/ssd/radar.db`.
- **Dashboard on the LAN:** with `DASHBOARD_HOST=0.0.0.0` the cockpit is at
  `http://<pi-ip>:8730` from any device on your network. It has **no auth and
  a control surface** — keep it on the LAN; for remote access use Tailscale
  (`tailscale up`) and hit the Pi's tailnet IP, never a raw port-forward.
- **Memory:** steady-state is light (~hundreds of MB). The boot-time price
  backfill is the heaviest moment; a 2GB Pi is enough, 4GB comfortable.
- **Clock:** make sure `systemd-timesyncd` is active — the bot's 24h windows
  and market-hours gating depend on a correct clock.
