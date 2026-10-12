"""
shifts.py — Roster management and candidate ranking for shift coverage.
"""
from __future__ import annotations

import logging
from contextlib import closing
from datetime import date, time, timedelta
from typing import Any, Dict, List, Optional

log = logging.getLogger("shift.roster")


def _db():
    from main import _db as main_db
    return main_db()


# --------------------------------------------------------------------------- #
# MySQL time helper — PyMySQL returns TIME columns as either datetime.time
# or datetime.timedelta depending on version. This normalizes both.
# --------------------------------------------------------------------------- #
def _time_to_minutes(value) -> int:
    """Convert a MySQL TIME value to total minutes since midnight."""
    if value is None:
        return 0
    if isinstance(value, timedelta):
        total_seconds = int(value.total_seconds())
        if total_seconds < 0:
            total_seconds += 24 * 3600
        return total_seconds // 60
    # datetime.time
    return value.hour * 60 + value.minute


# --------------------------------------------------------------------------- #
# Roster CRUD
# --------------------------------------------------------------------------- #
def add_shift(
    employee_id: str,
    shift_date: date,
    start_time: time,
    end_time: time,
    role: str,
    location: str = "",
) -> int:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO shifts (employee_id, shift_date, start_time, end_time, role, location)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (employee_id, shift_date, start_time, end_time, role, location),
            )
            return cur.lastrowid


def get_shift(shift_id: int) -> Optional[Dict[str, Any]]:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM shifts WHERE shift_id = %s", (shift_id,))
            return cur.fetchone()


def get_shifts_for_employee_on_date(
    employee_id: str, on_date: date
) -> List[Dict[str, Any]]:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM shifts
                WHERE employee_id = %s AND shift_date = %s AND status = 'scheduled'
                ORDER BY start_time
                """,
                (employee_id, on_date),
            )
            return cur.fetchall()


def mark_shift_covered(shift_id: int) -> None:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE shifts SET status = 'covered' WHERE shift_id = %s",
                (shift_id,),
            )


# --------------------------------------------------------------------------- #
# Candidate scoring helpers
# --------------------------------------------------------------------------- #
def _worked_with_before(candidate_id: str, absent_id: str) -> bool:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM shifts s1
                JOIN shifts s2 ON s1.shift_date = s2.shift_date
                WHERE s1.employee_id = %s
                  AND s2.employee_id = %s
                  AND s1.shift_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
                LIMIT 1
                """,
                (candidate_id, absent_id),
            )
            return cur.fetchone() is not None


def _hours_this_week(employee_id: str) -> float:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT start_time, end_time FROM shifts
                WHERE employee_id = %s
                  AND shift_date BETWEEN %s AND %s
                  AND status = 'scheduled'
                """,
                (employee_id, monday, sunday),
            )
            total = 0.0
            for row in cur.fetchall():
                sm = _time_to_minutes(row["start_time"])
                em = _time_to_minutes(row["end_time"])
                if em <= sm:
                    em += 24 * 60
                total += (em - sm) / 60.0
            return total


def _on_leave(candidate_id: str, on_date: date) -> bool:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM leave_events
                WHERE employee_id = %s AND %s BETWEEN start_date AND end_date
                LIMIT 1
                """,
                (candidate_id, on_date),
            )
            return cur.fetchone() is not None


def _already_working(candidate_id: str, on_date: date) -> bool:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1 FROM shifts
                WHERE employee_id = %s AND shift_date = %s AND status = 'scheduled'
                LIMIT 1
                """,
                (candidate_id, on_date),
            )
            return cur.fetchone() is not None


def find_candidates(
    shift: Dict[str, Any],
    absent_employee_id: str,
    max_candidates: int = 5,
) -> List[Dict[str, Any]]:
    """Rank candidates who could cover the shift."""
    shift_date = shift["shift_date"]
    role       = (shift["role"] or "").lower()

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT employee_id FROM shifts
                WHERE shift_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
                  AND employee_id != %s
                """,
                (absent_employee_id,),
            )
            employee_ids = [row["employee_id"] for row in cur.fetchall()]

    absent_role = None
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM shifts WHERE employee_id = %s ORDER BY shift_date DESC LIMIT 1",
                (absent_employee_id,),
            )
            row = cur.fetchone()
            if row:
                absent_role = (row["role"] or "").lower()

    scored: List[Dict[str, Any]] = []

    for emp_id in employee_ids:
        if _on_leave(emp_id, shift_date):
            continue
        if _already_working(emp_id, shift_date):
            continue

        score = 0
        reasons: List[str] = []

        with closing(_db()) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role FROM shifts WHERE employee_id = %s ORDER BY shift_date DESC LIMIT 1",
                    (emp_id,),
                )
                row = cur.fetchone()
                candidate_role = (row["role"] or "").lower() if row else ""

        if candidate_role and absent_role and candidate_role == absent_role:
            score += 40
            reasons.append("exact role match")
        elif role and candidate_role and candidate_role in role:
            score += 30
            reasons.append("role overlap")
        elif candidate_role:
            score += 10
            reasons.append("adjacent role")

        hours = _hours_this_week(emp_id)
        if hours < 20:
            score += 15
            reasons.append(f"low hours this week ({hours:.1f}h)")
        elif hours < 35:
            score += 5
            reasons.append(f"moderate hours ({hours:.1f}h)")

        if _worked_with_before(emp_id, absent_employee_id):
            score += 10
            reasons.append("has worked with absent employee")

        score += 10
        reasons.append("off-duty and available")

        scored.append({
            "employee_id": emp_id,
            "score":       score,
            "reasons":     reasons,
        })

    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:max_candidates]