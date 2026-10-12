"""
shift_replacement.py — Orchestration for the Smart Shift Replacement System.
"""
from __future__ import annotations

import logging
import os
import secrets
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from urgency import classify_urgency
from shifts import find_candidates, get_shift, mark_shift_covered

log = logging.getLogger("shift.replacement")

SERVICE_BASE_URL            = os.getenv("SERVICE_BASE_URL", "http://localhost:8000").rstrip("/")
COVERAGE_ESCALATION_MINUTES = int(os.getenv("COVERAGE_ESCALATION_MINUTES", "60"))
COVERAGE_MAX_CANDIDATES     = int(os.getenv("COVERAGE_MAX_CANDIDATES", "5"))
SHIFT_MANAGER_EMAIL         = os.getenv("SHIFT_MANAGER_EMAIL", "").strip()


def _db():
    from main import _db as main_db
    return main_db()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Leave event recording
# --------------------------------------------------------------------------- #
def record_leave_event(
    employee_id: str,
    leave_type: str,
    reason: str,
    start_date: date,
    end_date: date,
) -> Optional[int]:
    urgency, source, rationale = classify_urgency(leave_type, reason)
    log.info(
        "Leave event classified: employee=%s type=%s urgency=%s source=%s",
        employee_id, leave_type, urgency, source,
    )

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO leave_events
                    (employee_id, leave_type, reason, start_date, end_date,
                     urgency, rationale, detected_at, processed)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 0)
                ON DUPLICATE KEY UPDATE
                    reason    = VALUES(reason),
                    urgency   = VALUES(urgency),
                    rationale = VALUES(rationale)
                """,
                (
                    employee_id, leave_type, reason, start_date, end_date,
                    urgency, rationale, _utc_now(),
                ),
            )
            if cur.lastrowid:
                return cur.lastrowid
            cur.execute(
                "SELECT event_id FROM leave_events WHERE employee_id = %s AND start_date = %s",
                (employee_id, start_date),
            )
            row = cur.fetchone()
            return row["event_id"] if row else None


def _unprocessed_leave_events() -> List[Dict[str, Any]]:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM leave_events WHERE processed = 0 ORDER BY detected_at"
            )
            return cur.fetchall()


def _mark_event_processed(event_id: int) -> None:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE leave_events SET processed = 1 WHERE event_id = %s", (event_id,))


# --------------------------------------------------------------------------- #
# Coverage request creation
# --------------------------------------------------------------------------- #
def create_coverage_request(leave_event: Dict[str, Any]) -> List[int]:
    employee_id = leave_event["employee_id"]
    shift_date  = leave_event["start_date"]

    from shifts import get_shifts_for_employee_on_date
    shifts = get_shifts_for_employee_on_date(employee_id, shift_date)
    if not shifts:
        log.info("No shifts scheduled for employee %s on %s — no coverage needed",
                 employee_id, shift_date)
        return []

    request_ids: List[int] = []
    for shift in shifts:
        request_id = _create_request_for_shift(shift, leave_event)
        if request_id:
            request_ids.append(request_id)
            _dispatch_offers(request_id, shift, leave_event)
    return request_ids


def _create_request_for_shift(shift: Dict[str, Any], leave_event: Dict[str, Any]) -> Optional[int]:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO coverage_requests
                    (shift_id, absent_employee_id, urgency, reason_summary,
                     status, created_at)
                VALUES (%s, %s, %s, %s, 'open', %s)
                """,
                (
                    shift["shift_id"],
                    leave_event["employee_id"],
                    leave_event["urgency"],
                    leave_event.get("reason") or "",
                    _utc_now(),
                ),
            )
            return cur.lastrowid


# --------------------------------------------------------------------------- #
# Offer dispatch
# --------------------------------------------------------------------------- #
def _dispatch_offers(
    request_id: int,
    shift: Dict[str, Any],
    leave_event: Dict[str, Any],
) -> None:
    from hr_emailer import send_coverage_email

    candidates = find_candidates(
        shift=shift,
        absent_employee_id=leave_event["employee_id"],
        max_candidates=COVERAGE_MAX_CANDIDATES,
    )

    if not candidates:
        log.warning("No candidates found for shift %s — escalating to HR", shift["shift_id"])
        _escalate_to_hr(request_id, shift, leave_event, reason="no candidates")
        return

    absent_name = _employee_name(leave_event["employee_id"])

    for cand in candidates:
        token = secrets.token_urlsafe(32)
        candidate_email = _employee_email(cand["employee_id"])
        if not candidate_email:
            log.warning("Candidate %s has no email on file — skipping", cand["employee_id"])
            continue

        accept_url  = f"{SERVICE_BASE_URL}/coverage/accept/{token}"
        decline_url = f"{SERVICE_BASE_URL}/coverage/decline/{token}"

        sent_ok = send_coverage_email(
            to_email=candidate_email,
            candidate_first_name=_employee_first_name(cand["employee_id"]),
            absent_employee_name=absent_name,
            shift_date=str(shift["shift_date"]),
            shift_start=str(shift["start_time"]),
            shift_end=str(shift["end_time"]),
            role=shift.get("role") or "shift",
            location=shift.get("location") or "",
            urgency=leave_event["urgency"],
            accept_url=accept_url,
            decline_url=decline_url,
        )

        if not sent_ok:
            log.warning("Failed to email candidate %s", cand["employee_id"])
            continue

        with closing(_db()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO coverage_offers
                        (request_id, candidate_employee_id, candidate_email,
                         token, score, status, sent_at)
                    VALUES (%s, %s, %s, %s, %s, 'sent', %s)
                    """,
                    (
                        request_id, cand["employee_id"], candidate_email,
                        token, cand["score"], _utc_now(),
                    ),
                )
        log.info(
            "Coverage offer sent: request=%s candidate=%s score=%d",
            request_id, cand["employee_id"], cand["score"],
        )


def _employee_name(employee_id: str) -> str:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT full_name FROM provisioned WHERE employee_id = %s",
                (employee_id,),
            )
            row = cur.fetchone()
            if row and row.get("full_name"):
                return row["full_name"]
    return f"Employee {employee_id}"


def _employee_first_name(employee_id: str) -> str:
    full = _employee_name(employee_id)
    return full.split()[0] if full else f"Employee {employee_id}"


def _employee_email(employee_id: str) -> Optional[str]:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT corporate_email FROM provisioned WHERE employee_id = %s",
                (employee_id,),
            )
            row = cur.fetchone()
            if row and row.get("corporate_email"):
                return row["corporate_email"]
    domain = os.getenv("CORPORATE_DOMAIN", "company.com")
    return f"{employee_id}@{domain}"


# --------------------------------------------------------------------------- #
# Escalation
# --------------------------------------------------------------------------- #
def _escalate_to_hr(
    request_id: int,
    shift: Dict[str, Any],
    leave_event: Dict[str, Any],
    reason: str = "timeout",
) -> None:
    from hr_emailer import send_escalation_email

    if not SHIFT_MANAGER_EMAIL:
        log.warning("No SHIFT_MANAGER_EMAIL set — cannot escalate request %s", request_id)
        return

    send_escalation_email(
        to_email=SHIFT_MANAGER_EMAIL,
        request_id=request_id,
        shift_date=str(shift["shift_date"]),
        shift_start=str(shift["start_time"]),
        shift_end=str(shift["end_time"]),
        role=shift.get("role") or "shift",
        absent_employee_id=leave_event["employee_id"],
        reason=reason,
    )

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE coverage_requests SET status = 'escalated' WHERE request_id = %s",
                (request_id,),
            )


# --------------------------------------------------------------------------- #
# Poll cycle
# --------------------------------------------------------------------------- #
def run_shift_poll() -> None:
    log.info("Running shift replacement poll")
    events = _unprocessed_leave_events()
    log.info("Found %d unprocessed leave event(s)", len(events))

    for event in events:
        try:
            request_ids = create_coverage_request(event)
            _mark_event_processed(event["event_id"])
            log.info("Leave event %s produced %d coverage request(s)",
                     event["event_id"], len(request_ids))
        except Exception:
            log.exception("Failed to process leave event %s", event["event_id"])

    _check_timeouts()


def _check_timeouts() -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=COVERAGE_ESCALATION_MINUTES)

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cr.*, s.shift_date, s.start_time, s.end_time, s.role
                FROM coverage_requests cr
                JOIN shifts s ON s.shift_id = cr.shift_id
                WHERE cr.status = 'open'
                  AND cr.created_at < %s
                """,
                (cutoff.isoformat(timespec="seconds").replace("+00:00", "Z"),),
            )
            stale = cur.fetchall()

    for req in stale:
        log.warning("Coverage request %s has timed out — escalating", req["request_id"])
        _escalate_to_hr(
            request_id=req["request_id"],
            shift=req,
            leave_event={"employee_id": req["absent_employee_id"]},
            reason="timeout",
        )


# --------------------------------------------------------------------------- #
# FastAPI routes for accept / decline
# --------------------------------------------------------------------------- #
router = APIRouter(prefix="/coverage", tags=["coverage"])


@router.get("/accept/{token}", response_class=HTMLResponse)
async def accept_offer(token: str) -> HTMLResponse:
    return _handle_response(token, action="accepted")


@router.get("/decline/{token}", response_class=HTMLResponse)
async def decline_offer(token: str) -> HTMLResponse:
    return _handle_response(token, action="declined")


def _handle_response(token: str, action: str) -> HTMLResponse:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT co.*, cr.status AS request_status, cr.shift_id
                FROM coverage_offers co
                JOIN coverage_requests cr ON cr.request_id = co.request_id
                WHERE co.token = %s
                """,
                (token,),
            )
            offer = cur.fetchone()

    if not offer:
        return HTMLResponse(_page("Invalid link", "This offer link is not valid."), status_code=404)

    if offer["status"] != "sent":
        return HTMLResponse(_page("Already handled", "You have already responded to this offer."))

    if offer["request_status"] != "open":
        return HTMLResponse(_page("Already covered",
                                  "This shift has already been covered by another team member."))

    now = _utc_now()

    if action == "accepted":
        with closing(_db()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE coverage_offers SET status = 'accepted', responded_at = %s WHERE offer_id = %s",
                    (now, offer["offer_id"]),
                )
                cur.execute(
                    "UPDATE coverage_requests SET status = 'assigned', resolved_at = %s, assigned_to = %s WHERE request_id = %s",
                    (now, offer["candidate_employee_id"], offer["request_id"]),
                )
                cur.execute(
                    "UPDATE coverage_offers SET status = 'expired' WHERE request_id = %s AND offer_id != %s AND status = 'sent'",
                    (offer["request_id"], offer["offer_id"]),
                )
        mark_shift_covered(offer["shift_id"])
        log.info("Coverage accepted: request=%s candidate=%s",
                 offer["request_id"], offer["candidate_employee_id"])
        return HTMLResponse(_page(
            "Thanks for accepting!",
            "You've been assigned to this shift. HR has been notified."
        ))

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE coverage_offers SET status = 'declined', responded_at = %s WHERE offer_id = %s",
                (now, offer["offer_id"]),
            )
    log.info("Coverage declined: request=%s candidate=%s",
             offer["request_id"], offer["candidate_employee_id"])
    return HTMLResponse(_page(
        "Thanks for responding",
        "Your response has been recorded. HR will find other coverage."
    ))


def _page(title: str, message: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><title>{title}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; max-width: 500px;
         margin: 80px auto; padding: 24px; text-align: center; }}
  h1 {{ color: #2c3e50; }}
  p  {{ color: #555; font-size: 16px; }}
</style></head>
<body>
  <h1>{title}</h1>
  <p>{message}</p>
</body></html>"""