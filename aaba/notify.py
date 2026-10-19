"""Email notifications for SELL approvals, through Gmail (an app password), using an outbox.

Any process (the trading engine, the dashboard, the command line) only QUEUES a message in the
`notifications` table. The scheduler, the only process that holds the Gmail app password, sends
what is queued every minute and retries failures (up to MAX_ATTEMPTS). So the dashboard never holds
the email password, and an email outage loses nothing.

Settings (server: /etc/aaba/engine.env; never in Git):
  SMTP_USER            the Gmail address that sends (e.g. you@gmail.com)
  SMTP_APP_PASSWORD    a Google "app password" for that account (needs 2-Step Verification)
  NOTIFY_ADMIN_EMAIL   a copy of every message goes here (optional)
  STUDENT1_EMAIL ...   the owning student's address, by the student's env_prefix
Without SMTP_USER / SMTP_APP_PASSWORD messages stay queued (and are listed on the dashboard log).
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import clock, db, demo

log = logging.getLogger("aaba.notify")
MAX_ATTEMPTS = 5
SMTP_HOST, SMTP_PORT = "smtp.gmail.com", 465

KINDS = {
    "created": "SELL approval needed",
    "approved": "SELL approved",
    "rejected": "SELL rejected",
    "executed": "SELL executed",
    "failed": "SELL could not be executed",
    "invalidated": "SELL cancelled by the safety re-check",
    "expired": "SELL request expired",
}


def _fmt(ts: str | None) -> str:
    if not ts:
        return "-"
    t = clock.parse_iso(ts)
    return (f"{t.astimezone(ZoneInfo('Asia/Kolkata')):%d %b %Y %H:%M} IST / "
            f"{t.astimezone(clock.NY):%d %b %H:%M} New York")


def queue(kind: str, ap: dict, detail: str = "") -> None:
    """Queue one message about approval `ap` (a row of the approvals table). Never raises."""
    if demo.is_demo() or kind not in KINDS:
        return
    try:
        from .settings import load_students
        st = load_students().get(ap["student_id"])
        subject = f"[AABA] {KINDS[kind]}: SELL {float(ap['qty']):g} {ap['symbol']} ({st.name})"
        lines = [f"Student: {st.name} ({ap['student_id']})",
                 f"Stock: {ap['symbol']}",
                 f"Quantity: {float(ap['qty']):g} shares",
                 f"Signal price: ${float(ap['signal_price']):,.2f}" if ap.get("signal_price") else "Signal price: -",
                 f"Status: {KINDS[kind]}",
                 f"Reason: {(ap.get('reason') or '-')[:600]}",
                 f"Request created: {_fmt(ap.get('created_at'))}",
                 f"Expires: {_fmt(ap.get('expires_at'))}",
                 f"Request ID: {ap['approval_id']}"]
        if ap.get("decided_by"):
            lines.append(f"Decided by: {ap['decided_by']} at {_fmt(ap.get('decided_at'))}")
        if detail:
            lines.append(f"Details: {detail[:600]}")
        if kind == "created":
            lines += ["", "To decide: open the private dashboard (Tailscale) -> Approval Centre, choose your student "
                          "ID, enter YOUR approval PIN and press Approve or Reject. Only you can decide this request; "
                          "nothing is sold without your approval. Never send your PIN by email or chat."]
        elif kind == "approved":
            lines += ["", "The trading engine re-checks the order (same account, holding, fresh price, market open) "
                          "and sends it during market hours, usually within 10 minutes. You will get another email."]
        lines += ["", "This is a paper-trading account (no real money). Automated message from the AABA trading agent."]
        db.ex("INSERT INTO notifications(created_at, kind, student_id, approval_id, subject, body) VALUES (?,?,?,?,?,?)",
              (clock.iso(), kind, ap["student_id"], ap["approval_id"], subject, "\n".join(lines)))
    except Exception as e:  # noqa: BLE001
        log.warning("could not queue notification: %s", type(e).__name__)


def recipients(student_id: str) -> list[str]:
    from .settings import load_students
    st = load_students().get(student_id)
    out = [os.getenv(f"{st.env_prefix}_EMAIL", "").strip(), os.getenv("NOTIFY_ADMIN_EMAIL", "").strip()]
    return sorted({a for a in out if "@" in a})


def configured() -> bool:
    return bool(os.getenv("SMTP_USER", "").strip() and os.getenv("SMTP_APP_PASSWORD", "").strip())


def _send(to: list[str], subject: str, body: str) -> None:
    user, pw = os.getenv("SMTP_USER", "").strip(), os.getenv("SMTP_APP_PASSWORD", "").replace(" ", "").strip()
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, ", ".join(to), subject
    msg.set_content(body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) as s:
        s.login(user, pw)
        s.send_message(msg)


def flush(limit: int = 50) -> dict:
    """Send queued messages (scheduler, every minute). Returns counts; never raises."""
    demo.guard("sending email")
    out = {"sent": 0, "failed": 0, "skipped": 0}
    if not configured():
        out["skipped"] = (db.q1("SELECT COUNT(*) AS n FROM notifications WHERE sent_at IS NULL AND attempts<?",
                                (MAX_ATTEMPTS,)) or {}).get("n", 0)
        return out
    for n in db.q("SELECT * FROM notifications WHERE sent_at IS NULL AND attempts<? ORDER BY id LIMIT ?",
                  (MAX_ATTEMPTS, limit)):
        to = recipients(n["student_id"])
        if not to:
            db.ex("UPDATE notifications SET attempts=?, error=? WHERE id=?", (MAX_ATTEMPTS, "no recipient address", n["id"]))
            out["failed"] += 1
            continue
        try:
            _send(to, n["subject"], n["body"])
            db.ex("UPDATE notifications SET sent_at=?, attempts=attempts+1, error=NULL WHERE id=?", (clock.iso(), n["id"]))
            out["sent"] += 1
        except Exception as e:  # noqa: BLE001
            # the error text never contains the password; keep only the exception type and a short message
            db.ex("UPDATE notifications SET attempts=attempts+1, error=? WHERE id=?",
                  (f"{type(e).__name__}: {str(e)[:150]}", n["id"]))
            out["failed"] += 1
            log.warning("email %s failed: %s", n["id"], type(e).__name__)
    if out["sent"] or out["failed"]:
        db.log_event("INFO" if not out["failed"] else "WARNING", "notify",
                     f"emails sent {out['sent']}, failed {out['failed']}")
    return out
