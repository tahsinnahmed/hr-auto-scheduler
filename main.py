"""
main.py — Core service for HR Auto Scheduler.
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import httpx
import pymysql
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, field_validator
from pymysql.cursors import DictCursor
from tenacity import retry, stop_after_attempt, wait_exponential

from hr_emailer import send_welcome_email
from shift_replacement import router as coverage_router, run_shift_poll

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
load_dotenv()

HRM_BASE_URL      = os.getenv("HRM_BASE_URL", "").rstrip("/")
HRM_CLIENT_ID     = os.getenv("HRM_CLIENT_ID", "")
HRM_CLIENT_SECRET = os.getenv("HRM_CLIENT_SECRET", "")
HRM_REFRESH_TOKEN = os.getenv("HRM_REFRESH_TOKEN", "")

POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "300"))
POLL_LOOKBACK = int(os.getenv("POLL_LOOKBACK_MINUTES", "10"))

CORPORATE_DOMAIN    = os.getenv("CORPORATE_DOMAIN", "company.com")
WEBHOOK_SECRET      = os.getenv("WEBHOOK_SECRET", "")
STRICT_HIRED_FILTER = os.getenv("STRICT_HIRED_FILTER", "false").lower() == "true"

MYSQL_HOST     = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT     = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER     = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "hr_onboarding")

if not re.match(r"^[A-Za-z0-9_]+$", MYSQL_DATABASE):
    raise RuntimeError("MYSQL_DATABASE must contain only letters, digits, underscores.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("hr.onboarding")


class RefreshTokenExpired(Exception):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# MySQL
# --------------------------------------------------------------------------- #
def _db(with_database: bool = True) -> pymysql.connections.Connection:
    kwargs: Dict[str, Any] = dict(
        host=MYSQL_HOST, port=MYSQL_PORT, user=MYSQL_USER, password=MYSQL_PASSWORD,
        charset="utf8mb4", cursorclass=DictCursor, autocommit=True,
        connect_timeout=10, read_timeout=30, write_timeout=30,
    )
    if with_database:
        kwargs["database"] = MYSQL_DATABASE
    return pymysql.connect(**kwargs)


def init_db() -> None:
    with closing(_db(with_database=False)) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{MYSQL_DATABASE}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
    log.info("Database '%s' ready", MYSQL_DATABASE)

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS provisioned (
                    employee_id      VARCHAR(50)  NOT NULL PRIMARY KEY,
                    corporate_email  VARCHAR(254) NOT NULL UNIQUE,
                    department       VARCHAR(100) NOT NULL,
                    job_title        VARCHAR(150) NOT NULL,
                    full_name        VARCHAR(200) NOT NULL,
                    checklist_json   JSON         NOT NULL,
                    provisioned_at   VARCHAR(32)  NOT NULL,
                    INDEX idx_provisioned_at (provisioned_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS kv (
                    `key`  VARCHAR(100) NOT NULL PRIMARY KEY,
                    value  TEXT         NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS shifts (
                    shift_id     INT PRIMARY KEY AUTO_INCREMENT,
                    employee_id  VARCHAR(50)  NOT NULL,
                    shift_date   DATE         NOT NULL,
                    start_time   TIME         NOT NULL,
                    end_time     TIME         NOT NULL,
                    role         VARCHAR(100) NOT NULL,
                    location     VARCHAR(100),
                    status       ENUM('scheduled','covered','cancelled') DEFAULT 'scheduled',
                    INDEX idx_date_employee (shift_date, employee_id),
                    INDEX idx_status_date (status, shift_date)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS leave_events (
                    event_id     INT PRIMARY KEY AUTO_INCREMENT,
                    employee_id  VARCHAR(50)  NOT NULL,
                    leave_type   VARCHAR(50),
                    reason       TEXT,
                    start_date   DATE,
                    end_date     DATE,
                    urgency      ENUM('LOW','MEDIUM','HIGH') NOT NULL,
                    rationale    VARCHAR(255),
                    detected_at  VARCHAR(32)  NOT NULL,
                    processed    TINYINT      DEFAULT 0,
                    UNIQUE KEY uniq_employee_start (employee_id, start_date),
                    INDEX idx_processed (processed)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS coverage_requests (
                    request_id          INT PRIMARY KEY AUTO_INCREMENT,
                    shift_id            INT NOT NULL,
                    absent_employee_id  VARCHAR(50) NOT NULL,
                    urgency             ENUM('LOW','MEDIUM','HIGH') NOT NULL,
                    reason_summary      TEXT,
                    status              ENUM('open','assigned','escalated','cancelled') DEFAULT 'open',
                    created_at          VARCHAR(32) NOT NULL,
                    resolved_at         VARCHAR(32),
                    assigned_to         VARCHAR(50),
                    INDEX idx_status (status),
                    INDEX idx_created (created_at),
                    FOREIGN KEY (shift_id) REFERENCES shifts(shift_id)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS coverage_offers (
                    offer_id                 INT PRIMARY KEY AUTO_INCREMENT,
                    request_id               INT NOT NULL,
                    candidate_employee_id    VARCHAR(50)  NOT NULL,
                    candidate_email          VARCHAR(254) NOT NULL,
                    token                    VARCHAR(64)  NOT NULL UNIQUE,
                    score                    INT          NOT NULL,
                    status                   ENUM('sent','accepted','declined','expired') DEFAULT 'sent',
                    sent_at                  VARCHAR(32)  NOT NULL,
                    responded_at             VARCHAR(32),
                    FOREIGN KEY (request_id) REFERENCES coverage_requests(request_id),
                    INDEX idx_token (token),
                    INDEX idx_request_status (request_id, status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
    log.info("Tables verified")


def kv_get(key: str) -> Optional[str]:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT value FROM kv WHERE `key` = %s", (key,))
            row = cur.fetchone()
            return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO kv (`key`, value) VALUES (%s, %s) "
                "ON DUPLICATE KEY UPDATE value = VALUES(value)",
                (key, value),
            )


def already_provisioned(employee_id: str) -> bool:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM provisioned WHERE employee_id = %s", (employee_id,))
            return cur.fetchone() is not None


def get_provisioned(employee_id: str) -> Optional[Dict[str, Any]]:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM provisioned WHERE employee_id = %s", (employee_id,))
            return cur.fetchone()


def save_provisioning(
    employee_id: str, corporate_email: str, department: str,
    job_title: str, full_name: str, checklist: List[str],
) -> None:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO provisioned
                    (employee_id, corporate_email, department, job_title,
                     full_name, checklist_json, provisioned_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    corporate_email = VALUES(corporate_email),
                    department      = VALUES(department),
                    job_title       = VALUES(job_title),
                    full_name       = VALUES(full_name),
                    checklist_json  = VALUES(checklist_json),
                    provisioned_at  = VALUES(provisioned_at)
                """,
                (employee_id, corporate_email, department, job_title,
                 full_name, json.dumps(checklist), _utc_now()),
            )


# --------------------------------------------------------------------------- #
# OrangeHRM client
# --------------------------------------------------------------------------- #
class OrangeHRMClient:
    def __init__(self) -> None:
        self._client = httpx.Client(base_url=HRM_BASE_URL, timeout=30.0)

    def close(self) -> None:
        self._client.close()

    def _load_cached_token(self) -> Optional[str]:
        token = kv_get("hrm_access_token")
        exp   = kv_get("hrm_access_token_expires_at")
        if not token or not exp:
            return None
        try:
            if datetime.fromisoformat(exp) > datetime.now(timezone.utc) + timedelta(seconds=30):
                return token
        except ValueError:
            pass
        return None

    def _refresh_access_token(self) -> str:
        log.info("Refreshing OrangeHRM access token...")
        resp = self._client.post(
            "/web/index.php/oauth2/token",
            data={
                "grant_type":    "refresh_token",
                "client_id":     HRM_CLIENT_ID,
                "client_secret": HRM_CLIENT_SECRET,
                "refresh_token": HRM_REFRESH_TOKEN,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code == 401:
            raise RefreshTokenExpired(
                "OrangeHRM rejected the refresh token (HTTP 401). "
                "Clear HRM_REFRESH_TOKEN in .env and restart."
            )
        resp.raise_for_status()
        payload    = resp.json()
        token      = payload["access_token"]
        expires_in = int(payload.get("expires_in", 3600))
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        kv_set("hrm_access_token", token)
        kv_set("hrm_access_token_expires_at", expires_at.isoformat())
        return token

    def _auth_header(self) -> Dict[str, str]:
        token = self._load_cached_token() or self._refresh_access_token()
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10),
           reraise=True)
    def _fetch_employee_page(self, offset: int, limit: int) -> List[Dict[str, Any]]:
        resp = self._client.get(
            "/web/index.php/api/v2/pim/employees",
            headers=self._auth_header(),
            params={"limit": limit, "offset": offset},
        )
        resp.raise_for_status()
        return resp.json().get("data", [])

    def _fetch_employee_detail(self, emp_number: int | str) -> Optional[Dict[str, Any]]:
        try:
            resp = self._client.get(
                f"/web/index.php/api/v2/pim/employees/{emp_number}",
                headers=self._auth_header(),
            )
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json().get("data")
        except Exception:
            return None

    def _fetch_contact_details(self, emp_number: int | str) -> dict:
        try:
            resp = self._client.get(
                f"/web/index.php/api/v2/pim/employee/{emp_number}/contact-details",
                headers=self._auth_header(),
            )
            if resp.status_code == 404:
                return {}
            resp.raise_for_status()
            return resp.json().get("data") or {}
        except Exception:
            return {}

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10))
    def list_leave_requests(self, limit: int = 50) -> List[Dict[str, Any]]:
        """
        Fetch leave requests from OrangeHRM's Leave module API.
        Returns an empty list on any 4xx (endpoint may not exist on all versions).
        """
        try:
            resp = self._client.get(
                "/web/index.php/api/v2/leave/leaves",
                headers=self._auth_header(),
                params={"limit": limit, "offset": 0},
            )
            if resp.status_code == 404:
                log.debug("Leave API not available on this OrangeHRM version")
                return []
            resp.raise_for_status()
            return resp.json().get("data", [])
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 403, 404, 422):
                log.debug("Leave API returned %d — skipping", exc.response.status_code)
                return []
            raise

    def list_hired_employees(self, since: datetime) -> List[Dict[str, Any]]:
        all_employees: List[Dict[str, Any]] = []
        offset = 0
        limit  = 50
        while True:
            page = self._fetch_employee_page(offset=offset, limit=limit)
            if not page:
                break
            all_employees.extend(page)
            if len(page) < limit:
                break
            offset += limit

        log.info("Fetched %d total employee(s) from OrangeHRM", len(all_employees))
        if not all_employees:
            return []

        enriched: List[Dict[str, Any]] = []
        for emp in all_employees:
            emp_number = emp.get("empNumber")
            if not emp_number:
                continue
            if already_provisioned(str(emp_number)):
                continue
            detail  = self._fetch_employee_detail(emp_number) or {}
            contact = self._fetch_contact_details(emp_number)
            merged  = {**emp, **detail, "_contact": contact}
            enriched.append(merged)

        log.info("Enriched %d unprocessed employee(s)", len(enriched))
        return enriched

    def update_provisioning(
        self, employee_id: str, corporate_email: str,
        status_text: str, task_count: int,
    ) -> bool:
        try:
            resp = self._client.put(
                f"/web/index.php/api/v2/pim/employees/{employee_id}",
                headers={**self._auth_header(), "Content-Type": "application/json"},
                json={
                    "provisioningStatus":    status_text,
                    "corporateEmail":        corporate_email,
                    "onboardingTasksCount":  task_count,
                },
            )
            resp.raise_for_status()
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (400, 403, 404, 422):
                return False
            log.exception("Unexpected HTTP error writing back for %s", employee_id)
            return False
        except Exception:
            log.exception("Unexpected error writing back for %s", employee_id)
            return False


# --------------------------------------------------------------------------- #
# Provisioning logic
# --------------------------------------------------------------------------- #
COMMON_TASKS = [
    "Create Active Directory / SSO account",
    "Issue corporate laptop and peripherals",
    "Enroll device in MDM and enable disk encryption",
    "Grant VPN access and enforce MFA",
    "Send welcome email with first-day instructions",
    "Schedule Day-1 orientation with People Ops",
]

DEPARTMENT_TASKS: Dict[str, List[str]] = {
    "tech":        ["Invite to GitHub org", "Provision AWS sandbox", "Assign IDE licences"],
    "engineering": ["Invite to GitHub org", "Provision GCP sandbox", "Add to #eng-oncall"],
    "sales":       ["Provision Salesforce seat", "Assign Outreach licence", "Enroll in sales bootcamp"],
    "marketing":   ["Provision HubSpot seat", "Assign Canva licence", "Grant brand asset access"],
    "finance":     ["Provision NetSuite access", "Request corporate card", "Grant Expensify access"],
    "hr":          ["Provision OrangeHRM admin", "Assign HR compliance training"],
}
DEFAULT_TASKS = ["Assign onboarding buddy", "Grant shared drive access", "Schedule 30/60/90 check-ins"]


def slugify(value: str) -> str:
    nfkd = unicodedata.normalize("NFKD", value)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]", "", ascii_only.lower())


def generate_email(first: str, last: str) -> str:
    base      = f"{slugify(first) or 'employee'}.{slugify(last) or 'user'}"
    candidate = f"{base}@{CORPORATE_DOMAIN}"
    suffix    = 1
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            while True:
                cur.execute("SELECT 1 FROM provisioned WHERE corporate_email = %s", (candidate,))
                if not cur.fetchone():
                    return candidate
                candidate = f"{base}{suffix}@{CORPORATE_DOMAIN}"
                suffix   += 1


def build_checklist(department: str) -> List[str]:
    key = department.strip().lower()
    return COMMON_TASKS + DEPARTMENT_TASKS.get(key, DEFAULT_TASKS)


def _extract_field(emp: Dict[str, Any], *paths: str, default: str = "") -> str:
    for path in paths:
        cur: Any = emp
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            if isinstance(cur, dict):
                for key in ("name", "title", "label"):
                    if key in cur:
                        return str(cur[key])
            return str(cur)
    return default


def _is_hired(emp: Dict[str, Any]) -> bool:
    candidates = [
        emp.get("employeeStatus"),
        (emp.get("employeeStatus") or {}).get("name") if isinstance(emp.get("employeeStatus"), dict) else None,
        emp.get("employmentStatus"),
        (emp.get("employmentStatus") or {}).get("name") if isinstance(emp.get("employmentStatus"), dict) else None,
        emp.get("status"),
    ]
    for value in candidates:
        if isinstance(value, str) and value.strip().lower() == "hired":
            return True
    return False


def _extract_personal_email(emp: Dict[str, Any]) -> str:
    contact = emp.get("_contact") or {}
    return (
        contact.get("otherEmail")
        or contact.get("personalEmail")
        or contact.get("workEmail")
        or emp.get("personalEmail")
        or ""
    ).strip()


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #
class OnboardingPoller:
    def __init__(self) -> None:
        self.hrm = OrangeHRMClient()

    def run_once(self) -> None:
        since = datetime.now(timezone.utc) - timedelta(minutes=POLL_LOOKBACK)
        log.info("Polling OrangeHRM (lookback %d minutes)", POLL_LOOKBACK)
        try:
            employees = self.hrm.list_hired_employees(since)
        except RefreshTokenExpired:
            log.error("━" * 60)
            log.error("✗  REFRESH TOKEN EXPIRED — SERVICE NEEDS ATTENTION")
            log.error("   OrangeHRM rejected the refresh token (HTTP 401).")
            log.error("   To fix:")
            log.error("     1. Stop this service (Ctrl+C)")
            log.error("     2. Open .env and clear HRM_REFRESH_TOKEN=")
            log.error("     3. Run: DELETE FROM hr_onboarding.kv;")
            log.error("     4. Restart start.bat to redo the OAuth flow")
            log.error("━" * 60)
            return
        except Exception:
            log.exception("Failed to fetch employees from OrangeHRM")
            return

        log.info("Found %d unprocessed employee(s) to consider", len(employees))
        for emp in employees:
            try:
                self._process(emp)
            except Exception:
                log.exception("Failed to process employee %s", emp.get("empNumber"))

    def _process(self, emp: Dict[str, Any]) -> None:
        emp_id = str(emp.get("empNumber") or emp.get("employeeId") or "").strip()
        if not emp_id:
            return
        if already_provisioned(emp_id):
            return
        if STRICT_HIRED_FILTER and not _is_hired(emp):
            return

        first = _extract_field(emp, "firstName", default="").strip()
        last  = _extract_field(emp, "lastName",  default="").strip()
        dept  = _extract_field(emp, "subunit.name", "department.name", "department", default="General").strip()
        title = _extract_field(emp, "jobTitle.title", "jobTitle.name", "jobTitle", default="Employee").strip()

        if not first or not last:
            return

        personal_email = _extract_personal_email(emp)
        if not personal_email or "@" not in personal_email:
            log.info(
                "Employee %s (%s) has no personal email on file — skipping",
                f"{first} {last}", emp_id,
            )
            return

        start_date = emp.get("joinedDate") or emp.get("startDate") or "the agreed start date"
        full_name  = f"{first} {last}"
        email      = generate_email(first, last)
        checklist  = build_checklist(dept)

        log.info("━" * 60)
        log.info("🚀  ONBOARDING: %s (%s)", full_name, emp_id)
        log.info("━" * 60)
        log.info("📧  Personal email : %s", personal_email)
        log.info("🏢  Department     : %s", dept)
        log.info("💼  Job title      : %s", title)
        log.info("📅  Start date     : %s", start_date)

        email_ok = send_welcome_email(
            to_email=personal_email,
            first_name=first,
            full_name=full_name,
            job_title=title,
            department=dept,
            start_date=str(start_date),
        )

        save_provisioning(emp_id, email, dept, title, full_name, checklist)
        log.info("💾  Saved to MySQL  (email_sent=%s)", email_ok)
        log.info("━" * 60)

        self.hrm.update_provisioning(
            employee_id=emp_id,
            corporate_email=email,
            status_text="provisioned",
            task_count=len(checklist),
        )


def start_poller_scheduler() -> BackgroundScheduler:
    """
    Register all background jobs and start the scheduler.
    Three jobs:
        1. hrm_poll      — onboarding poller (OrangeHRM employees)
        2. shift_poll    — processes leave events → coverage offers
        3. leave_sync    — pulls emergency leaves from OrangeHRM Leave API
    """
    poller    = OnboardingPoller()
    scheduler = BackgroundScheduler(timezone="UTC")

    # Job 1 — Onboarding poller
    scheduler.add_job(
        poller.run_once,
        "interval",
        seconds=POLL_INTERVAL,
        id="hrm_poll",
        next_run_time=datetime.now(timezone.utc),
        max_instances=1,
        coalesce=True,
    )

    # Job 2 — Shift replacement poller
    scheduler.add_job(
        run_shift_poll,
        "interval",
        seconds=POLL_INTERVAL,
        id="shift_poll",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
        max_instances=1,
        coalesce=True,
    )

    # Job 3 — Leave sync poller (pulls from OrangeHRM Leave API)
    def _leave_sync_job() -> None:
        try:
            from leave_sync import sync_leave_events
            count = sync_leave_events()
            if count:
                log.info("Leave sync created %d new event(s)", count)
        except Exception:
            log.exception("Leave sync failed")

    scheduler.add_job(
        _leave_sync_job,
        "interval",
        seconds=POLL_INTERVAL,
        id="leave_sync",
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=15),
        max_instances=1,
        coalesce=True,
    )

    # Start AFTER all jobs are registered
    scheduler.start()
    log.info("Poller scheduled every %d seconds (3 jobs)", POLL_INTERVAL)
    return scheduler


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
class EmployeeHiredPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    event:          str = "employee.hired"
    employee_id:    str
    first_name:     str
    last_name:      str
    department:     str
    job_title:      str
    personal_email: str
    status:         str = "Hired"

    @field_validator("employee_id", mode="before")
    @classmethod
    def _coerce_id(cls, v: Any) -> str:
        return str(v).strip()

    @field_validator("personal_email")
    @classmethod
    def _validate_email(cls, v: str) -> str:
        if not re.match(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$", v):
            raise ValueError("invalid email")
        return v.lower()


class EmergencyLeavePayload(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    employee_id: str
    leave_type:  str = "Emergency"
    reason:      str = ""
    start_date:  str
    end_date:    str


app = FastAPI(title="HR Auto Scheduler")

# Register routers
from dashboard import router as dashboard_router
app.include_router(dashboard_router)
app.include_router(coverage_router)


@app.exception_handler(RequestValidationError)
async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    missing = [
        ".".join(str(p) for p in e["loc"] if p != "body")
        for e in exc.errors()
        if e["type"] == "missing"
    ]
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "success": False, "status": "validation_error",
            "message": "Invalid payload.",
            "missing_fields": missing, "errors": exc.errors(),
        },
    )


@app.get("/health")
async def health() -> dict:
    try:
        with closing(_db()) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS n FROM provisioned")
                count = cur.fetchone()["n"]
        return {"status": "ok", "db": "mysql", "provisioned_count": count, "timestamp": _utc_now()}
    except Exception:
        raise HTTPException(status_code=503, detail="Database unavailable")


@app.post("/webhooks/employee-hired")
async def employee_hired(
    payload: EmployeeHiredPayload,
    x_webhook_secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    if already_provisioned(payload.employee_id):
        row = get_provisioned(payload.employee_id)
        return {
            "success": True, "status": "already_provisioned",
            "employee_id": payload.employee_id,
            "corporate_email": row["corporate_email"], "duplicate": True,
        }

    email     = generate_email(payload.first_name, payload.last_name)
    checklist = build_checklist(payload.department)
    full_name = f"{payload.first_name} {payload.last_name}"

    save_provisioning(payload.employee_id, email, payload.department,
                     payload.job_title, full_name, checklist)

    send_welcome_email(
        to_email=payload.personal_email,
        first_name=payload.first_name, full_name=full_name,
        job_title=payload.job_title, department=payload.department,
        start_date="the agreed start date",
    )

    return {
        "success": True, "status": "provisioned",
        "employee_id": payload.employee_id, "full_name": full_name,
        "corporate_email": email, "department": payload.department,
        "checklist": checklist, "total_tasks": len(checklist),
        "processed_at": _utc_now(),
        "correlation_id": str(uuid.uuid4()),
    }


@app.post("/webhooks/emergency-leave")
async def emergency_leave(
    payload: EmergencyLeavePayload,
    x_webhook_secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret"),
) -> dict:
    if WEBHOOK_SECRET and x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    from datetime import date as date_cls
    from shift_replacement import record_leave_event, create_coverage_request

    try:
        start = date_cls.fromisoformat(payload.start_date)
        end   = date_cls.fromisoformat(payload.end_date)
    except ValueError:
        return {"success": False, "error": "Dates must be YYYY-MM-DD"}

    # Record the leave event
    try:
        event_id = record_leave_event(
            employee_id=payload.employee_id,
            leave_type=payload.leave_type,
            reason=payload.reason,
            start_date=start,
            end_date=end,
        )
    except Exception as exc:
        log.exception("Failed to record leave event")
        return {"success": False, "stage": "record_leave", "error": str(exc)}

    if not event_id:
        return {"success": False, "stage": "record_leave", "error": "No event_id returned"}

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM leave_events WHERE event_id = %s", (event_id,))
            event = cur.fetchone()

    if not event:
        return {"success": False, "stage": "fetch_event", "error": "Event not found"}

    # Find coverage candidates
    try:
        request_ids = create_coverage_request(event)
    except Exception as exc:
        log.exception("Failed to create coverage request")
        return {
            "success": False,
            "stage": "create_coverage",
            "event_id": event_id,
            "error": str(exc),
        }

    return {
        "success": True,
        "event_id": event_id,
        "urgency": event.get("urgency"),
        "request_ids": request_ids,
        "processed_at": _utc_now(),
    }