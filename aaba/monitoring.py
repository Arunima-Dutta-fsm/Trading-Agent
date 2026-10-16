"""Dead-man's-switch alerts through Healthchecks.io (free plan), optional.

Set the ping URLs in the server's environment file; leave them empty to switch alerts off:
  HEALTHCHECK_DAILY_URL      the 09:45 New York workflow run: pinged at start, on success
                             (also when the day is skipped as a market holiday) and on failure.
                             Healthchecks emails you when a ping is missing or a failure arrives.
  HEALTHCHECK_HEARTBEAT_URL  pinged by the 15-minute job, so you also hear if the scheduler
                             process itself stops.
The URLs act like passwords for the check (anyone with one can send pings), so keep them out
of Git. A ping never raises: an alerting outage must not stop trading.
"""
from __future__ import annotations

import logging
import os
import urllib.request

log = logging.getLogger("aaba.monitoring")


def ping(env_name: str, kind: str = "", message: str = "") -> bool:
    """kind: '' (success), 'start' or 'fail'. Returns True if the ping was delivered."""
    base = os.getenv(env_name, "").strip().rstrip("/")
    if not base.startswith("https://"):
        return False
    url = f"{base}/{kind}" if kind else base
    try:
        req = urllib.request.Request(url, data=message[:9000].encode("utf-8"), method="POST",
                                     headers={"User-Agent": "aaba-trading-agent"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        log.warning("healthcheck ping %s/%s failed: %s", env_name, kind, type(e).__name__)
        return False
