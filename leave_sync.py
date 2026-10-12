"""
leave_sync.py — Syncs emergency leaves from OrangeHRM's Leave API.

This poller runs every 5 minutes (via the `leave_sync` job registered in
main.py). It fetches leave requests from OrangeHRM, filters for ones that
look like emergencies, classifies their urgency, and inserts them into the
local `leave_events` table.

The shift replacement poller (shift_poll) picks them up from there.
"""
from __future__ import annotations

import logging
from contextlib import closing
from datetime import date
from typing import Any, Dict, List, Optional

log = logging.getLogger("leave.sync")


# Keywords that indicate an emergency leave — anything matching
# triggers the automated coverage workflow.
EMERGENCY_KEYWORDS = [
    "emergency", "urgent", "accident", "hospital", "icu",
    "surgery", "died", "death", "critical",
]


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _looks_like_emergency(leave_type: str, reason: str) -> bool:
    """Return True if this leave qualifies for automated coverage."""
    text = f"{leave_type or ''} {reason or ''}".lower()
    return any(kw in text for kw in EMERGENCY_KEYWORDS)


def _extract_date(raw: Any) -> Optional[date]:
    """OrangeHRM returns dates as 'YYYY-MM-DD' or full ISO strings."""
    if not raw:
        return None
    if isinstance(raw, date):
        return raw
    try:
        return date.fromisoformat(str(raw)[:10])
    except (ValueError, TypeError):
        return None


def _extract_leave_fields(leave: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Normalize a raw leave record from OrangeHRM into our internal shape.
    Returns None if required fields are missing.
    """
    emp_id = str(
        leave.get("empNumber")
        or (leave.get("employee") or {}).get("empNumber")
        or (leave.get("employee") or {}).get("employeeId")
        or ""
    ).strip()
    if not emp_id:
        return None

    # Leave type — could be a string or a nested object
    lt = leave.get("leaveType") or leave.get("type") or ""
    if isinstance(lt, dict):
        leave_type = lt.get("name") or lt.get("label") or ""
    else:
        leave_type = str(lt)

    # Reason/comment — try several likely keys
    reason = (
        leave.get("comment")
        or leave.get("reason")
        or leave.get("remarks")
        or ""
    )

    start = _extract_date(leave.get("startDate") or leave.get("fromDate") or leave.get("date"))
    end   = _extract_date(leave.get("endDate")   or leave.get("toDate")   or start)

    if not start or not end:
        return None

    return {
        "employee_id": emp_id,
        "leave_type":  leave_type,
        "reason":      reason,
        "start_date":  start,
        "end_date":    end,
    }


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def sync_leave_events() -> int:
    """
    Fetch leave requests from OrangeHRM's Leave API and record any
    emergency ones in the local `leave_events` table.

    Returns the number of NEW leave events created (0 if none or API missing).
    """
    from main import OrangeHRMClient, _db, _utc_now
    from urgency import classify_urgency

    client = OrangeHRMClient()
    try:
        try:
            leaves = client.list_leave_requests(limit=100)
        except Exception:
            log.debug("Leave API unavailable on this OrangeHRM version")
            return 0
    finally:
        client.close()

    if not leaves:
        log.debug("No leave requests returned from OrangeHRM")
        return 0

    log.info("Fetched %d leave request(s) from OrangeHRM", len(leaves))

    created = 0
    for leave in leaves:
        fields = _extract_leave_fields(leave)
        if not fields:
            continue

        # Only process emergency-like leaves
        if not _looks_like_emergency(fields["leave_type"], fields["reason"]):
            continue

        # Classify urgency (rules first, Groq LLM fallback)
        urgency, source, rationale = classify_urgency(
            fields["leave_type"], fields["reason"]
        )

        # Insert (idempotent thanks to the UNIQUE(employee_id, start_date) key)
        try:
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
                            fields["employee_id"],
                            fields["leave_type"],
                            fields["reason"],
                            fields["start_date"],
                            fields["end_date"],
                            urgency,
                            rationale,
                            _utc_now(),
                        ),
                    )
                    # rowcount == 1 on INSERT, 0 on no-op update, 2 on changed update
                    if cur.rowcount == 1:
                        created += 1
                        log.info(
                            "Recorded emergency leave: emp=%s type=%s urgency=%s source=%s",
                            fields["employee_id"], fields["leave_type"],
                            urgency, source,
                        )
        except Exception:
            log.exception("Failed to record leave for emp %s", fields["employee_id"])

    if created == 0:
        log.info("Leave sync completed — no new emergency leaves")
    return created