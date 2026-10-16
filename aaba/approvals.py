"""SELL approvals: only the student who owns the portfolio can approve or reject.

How access control works (classroom version, documented in README):
- Each student has a personal approval PIN in .env (STUDENT1_APPROVAL_PIN, ...),
  at least 6 characters. The PIN is checked here, on the server side, every time.
  Choosing a name in the web page is not enough.
- A request carries the claimed student ID and the PIN. Both must match the
  approval's own student. A correct PIN for another student is refused.
- After 5 wrong tries in 15 minutes (configurable), that student ID is locked out
  for the rest of the window.
- Each approval is tied to one student, account, symbol, quantity and signal
  through a fingerprint, and expires after 24 hours (configurable). It cannot be
  reused for anything else.

This is not full user authentication (there are no individual logins or
passwords managed by an identity system). It is a shared-secret PIN suitable for a
local classroom demo; keep the dashboard on localhost or behind ngrok basic auth.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import timedelta

from . import clock, db, demo
from .settings import load_strategy, load_students

MIN_PIN_LEN = 6
OPEN = ("PENDING",)


class ApprovalError(Exception):
    pass


def fingerprint(student_id: str, account_number: str | None, symbol: str, qty: float,
                signal_id: str, signal_time: str) -> str:
    raw = f"{student_id}|{account_number}|{symbol}|{qty:g}|{signal_id}|{signal_time}"
    return hashlib.sha256(raw.encode()).hexdigest()


def create_request(run_id: str, signal: dict, student_id: str, account_number: str | None,
                   qty: float, reason: str) -> dict:
    """Create a PENDING approval. An older open request for the same stock is superseded,
    so only the newest signal can ever be approved."""
    demo.guard("creating SELL approvals")
    cfg = load_strategy()
    now = clock.now_utc()
    aid = db.new_id("apv")
    fp = fingerprint(student_id, account_number, signal["symbol"], qty, signal["signal_id"], signal["created_at"])
    with db.tx() as con:
        dup = con.execute("SELECT approval_id FROM approvals WHERE signal_id=?", (signal["signal_id"],)).fetchone()
        if dup:
            return dict(con.execute("SELECT * FROM approvals WHERE approval_id=?", (dup[0],)).fetchone())
        con.execute("UPDATE approvals SET status='SUPERSEDED', decision_note='replaced by a newer SELL signal' "
                    "WHERE student_id=? AND symbol=? AND status='PENDING'", (student_id, signal["symbol"]))
        con.execute(
            "INSERT INTO approvals(approval_id, student_id, account_number, symbol, qty, signal_id, run_id,"
            " signal_price, signal_time, reason, evidence, status, created_at, expires_at, fingerprint)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, student_id, account_number, signal["symbol"], qty, signal["signal_id"], run_id,
             signal.get("price"), signal["created_at"], reason[:2000], signal.get("indicators"), "PENDING",
             clock.iso(now), clock.iso(now + timedelta(hours=cfg.approvals.expiry_hours)), fp))
        row = con.execute("SELECT * FROM approvals WHERE approval_id=?", (aid,)).fetchone()
    db.log_event("INFO", "sell_approval_requested",
                 f"SELL {qty:g} {signal['symbol']} waiting for {student_id} approval", run_id, student_id,
                 {"approval_id": aid})
    return dict(row)


def expire_stale() -> int:
    if demo.is_read_only():
        return 0            # read-only processes never change the database
    n = db.ex("UPDATE approvals SET status='EXPIRED', decision_note='not decided before expiry' "
              "WHERE status='PENDING' AND expires_at < ?", (clock.iso(),))
    n += db.ex("UPDATE approvals SET status='EXPIRED', decision_note='approved but not executed before expiry' "
               "WHERE status='APPROVED' AND expires_at < ?", (clock.iso(),))
    return n


def is_expired(ap: dict) -> bool:
    return clock.parse_iso(ap["expires_at"]) < clock.now_utc()


def _locked_out(student_id: str) -> bool:
    cfg = load_strategy().approvals
    since = clock.iso(clock.now_utc() - timedelta(minutes=cfg.lockout_minutes))
    row = db.q1("SELECT COUNT(*) AS n FROM approval_attempts WHERE claimed_student_id=? AND success=0 AND at>=?",
                (student_id, since))
    return (row or {}).get("n", 0) >= cfg.lockout_attempts


def _attempt(approval_id, claimed, target, success, detail):
    db.ex("INSERT INTO approval_attempts(at, approval_id, claimed_student_id, target_student_id, success, detail)"
          " VALUES (?,?,?,?,?,?)", (clock.iso(), approval_id, claimed, target, int(success), detail))


_COMMON_PINS = {"123456", "654321", "123123", "112233", "121212", "123321", "102030", "147258", "159753",
                "password", "qwerty", "abc123", "admin123", "1234567", "12345678"}


def pin_problem(pin: str) -> str | None:
    """Why a configured PIN is unusable, or None. Roll numbers and student IDs are shown on the
    dashboard and in the repository, so they can never be PINs."""
    p = (pin or "").strip()
    if len(p) < MIN_PIN_LEN:
        return f"shorter than {MIN_PIN_LEN} characters"
    group = load_students()
    if p.lower() in {x.lower() for s in group.students for x in (s.roll_no, s.student_id, s.name)}:
        return "it is a roll number, student ID or name (shown on the dashboard)"
    if p.lower() in _COMMON_PINS or len(set(p)) == 1:
        return "it is a common or repeated PIN"
    if p.isdigit():
        steps = {int(b) - int(a) for a, b in zip(p, p[1:])}
        if steps in ({1}, {-1}):
            return "it is a sequential PIN"
    return None


def verify_admin_pin(pin: str) -> bool:
    """DASHBOARD_ADMIN_PIN check for protected dashboard actions (never valid in demo mode)."""
    import os
    expected = os.getenv("DASHBOARD_ADMIN_PIN", "").strip()
    if demo.is_read_only() or pin_problem(expected) or not pin:
        return False
    return hmac.compare_digest(str(pin).strip().encode(), expected.encode())


def verify_pin(student_id: str, pin: str) -> bool:
    st = load_students().get(student_id)
    expected = st.approval_pin()
    if pin_problem(expected) or not pin:
        return False
    return hmac.compare_digest(str(pin).strip().encode(), expected.encode())


def decide(approval_id: str, claimed_student_id: str, pin: str, decision: str, note: str = "") -> dict:
    """decision: 'approve' or 'reject'. Raises ApprovalError with a plain reason on refusal."""
    if demo.is_read_only():
        raise ApprovalError("read-only demo: SELL approvals are disabled" if demo.is_demo()
                            else "read-only dashboard: SELL approvals are disabled here")
    decision = decision.lower().strip()
    if decision not in ("approve", "reject"):
        raise ApprovalError("decision must be approve or reject")
    ap = db.q1("SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
    if not ap:
        raise ApprovalError("approval request not found")
    group = load_students()
    try:
        group.get(claimed_student_id)
    except KeyError:
        _attempt(approval_id, claimed_student_id, ap["student_id"], False, "unknown student id")
        raise ApprovalError("unknown student ID")
    if _locked_out(claimed_student_id):
        raise ApprovalError("too many wrong attempts; try again later")
    if claimed_student_id != ap["student_id"]:
        _attempt(approval_id, claimed_student_id, ap["student_id"], False, "wrong student")
        db.log_event("WARNING", "approval_refused",
                     f"{claimed_student_id} tried to decide {ap['student_id']}'s request {approval_id}",
                     ap["run_id"], ap["student_id"])
        raise ApprovalError("only the student who owns this portfolio can decide this request")
    problem = pin_problem(load_students().get(claimed_student_id).approval_pin())
    if problem:
        raise ApprovalError(f"the approval PIN for {claimed_student_id} in .env cannot be used: {problem}; "
                            f"set a new one")
    if not verify_pin(claimed_student_id, pin):
        _attempt(approval_id, claimed_student_id, ap["student_id"], False, "wrong PIN")
        raise ApprovalError("wrong PIN")
    if ap["status"] != "PENDING":
        _attempt(approval_id, claimed_student_id, ap["student_id"], False, f"status {ap['status']}")
        raise ApprovalError(f"request is {ap['status']}, not PENDING")
    if is_expired(ap):
        db.ex("UPDATE approvals SET status='EXPIRED' WHERE approval_id=? AND status='PENDING'", (approval_id,))
        _attempt(approval_id, claimed_student_id, ap["student_id"], False, "expired")
        raise ApprovalError("request has expired")
    fp = fingerprint(ap["student_id"], ap["account_number"], ap["symbol"], float(ap["qty"]), ap["signal_id"],
                     ap["signal_time"])
    if fp != ap["fingerprint"]:
        _attempt(approval_id, claimed_student_id, ap["student_id"], False, "fingerprint mismatch")
        raise ApprovalError("request details were changed after it was created; refused")

    st = group.get(claimed_student_id)
    new_status = "APPROVED" if decision == "approve" else "REJECTED"
    n = db.ex("UPDATE approvals SET status=?, decided_by=?, decided_at=?, decision_note=? "
              "WHERE approval_id=? AND status='PENDING'",
              (new_status, f"{st.name} ({st.roll_no}) [{st.student_id}]", clock.iso(), note[:500], approval_id))
    if n != 1:
        raise ApprovalError("request was changed by someone else; reload and try again")
    _attempt(approval_id, claimed_student_id, ap["student_id"], True, new_status)
    db.log_event("INFO", "approval_decided", f"{new_status} SELL {ap['qty']:g} {ap['symbol']} by {st.name}",
                 ap["run_id"], ap["student_id"], {"approval_id": approval_id})
    return db.q1("SELECT * FROM approvals WHERE approval_id=?", (approval_id,))
