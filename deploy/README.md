# Running the trading engine on an always-on server

Target: one Ubuntu 24.04 VM (Oracle Cloud Always Free), reached only through Tailscale.

| What | Where on the server |
|---|---|
| Code (this repository) | `/opt/aaba` (Python 3.11 venv in `/opt/aaba/.venv`) |
| Secrets (keys, PINs, LLM keys, alert URLs) | `/etc/aaba/engine.env`, `root:aaba 640`, never in Git |
| Database, reports | `/var/lib/aaba/data/aaba.db`, `/var/lib/aaba/model-run`, `/var/lib/aaba/reports` |
| Daily database backups (14 kept) | `/var/lib/aaba/backups` |
| Logs | `journalctl -u aaba-scheduler`, `/var/log/aaba/scheduler.log` |

Services:

| Unit | User | What it does |
|---|---|---|
| `aaba-scheduler` | `aaba` | 09:45 New York workflow (Alpaca holiday calendar, daylight saving handled), 15-minute approved-SELL check and reconcile, catch-up of a missed run after a restart. Restarted automatically. |
| `aaba-dashboard` | `aabaweb` | The real dashboard, **read-only** (`AABA_READ_ONLY=1`), on `127.0.0.1:8501` only. It has no broker keys and cannot read `engine.env`. Published privately with `tailscale serve`. |
| `aaba-backup.timer` | `aaba` | Daily consistent SQLite backup with an integrity check. |

## Install or update

```bash
sudo bash /opt/aaba/deploy/bootstrap.sh      # safe to re-run: pulls main, updates packages and units
sudo systemctl restart aaba-scheduler aaba-dashboard
```

## Daily operations

```bash
systemctl status aaba-scheduler aaba-dashboard --no-pager
journalctl -u aaba-scheduler -n 100 --no-pager        # recent scheduler output
sudo aaba check                                       # config, accounts, LLM, MCP self-test
cat /var/lib/aaba/data/run.lock 2>/dev/null || echo "no run in progress"
sudo aaba approvals                                   # list SELLs waiting for a student
sudo aaba approve apv-xxxxxxxx --student S2           # the STUDENT types their own PIN (hidden)
sudo aaba approve apv-xxxxxxxx --student S2 --reject
```

Stop or start:

```bash
sudo systemctl stop aaba-scheduler          # no new runs; a run in progress is allowed 120 s to stop
sudo systemctl start aaba-scheduler         # a missed 09:45 run is caught up (trading days, >30 min before close)
```

## Recovery

| Situation | What happens / what to do |
|---|---|
| Process crash | systemd restarts it after 30 s. The unfinished run is marked ABANDONED (its signals can no longer be acted on) and, if it was today's run, a catch-up run starts. Orders already sent are protected by their client order IDs. |
| VM reboot | Both services start at boot; same catch-up rule. |
| Missed or failed run | Healthchecks.io emails you (if `HEALTHCHECK_DAILY_URL` is set). Check `journalctl -u aaba-scheduler`. |
| Scheduler down | Healthchecks heartbeat (every 15 min) goes quiet and you get an email. |
| Database damaged | `sudo systemctl stop aaba-scheduler aaba-dashboard`, copy the newest `/var/lib/aaba/backups/aaba-YYYYMMDD.db` to `/var/lib/aaba/data/aaba.db` (as user `aaba`), start again, then `sudo aaba reconcile` to refresh order statuses from Alpaca. |

## Safety switches (in `/etc/aaba/engine.env` and `configs/strategy.json`)

Orders need both `ENABLE_PAPER_ORDERS=true` and `schedule.mode` = `paper`. The trading endpoint
is always Alpaca **paper**. To stop all trading at once: set `ENABLE_PAPER_ORDERS=false` and
`sudo systemctl restart aaba-scheduler`.
