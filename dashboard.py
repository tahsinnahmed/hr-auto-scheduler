"""
dashboard.py — Web UI for HR Auto Scheduler.

Mounted at: http://localhost:8000/hr-auto-scheduler/dashboard
"""
from __future__ import annotations

import json
import os
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse

PREFIX = "/hr-auto-scheduler"

router = APIRouter(prefix=PREFIX, tags=["dashboard"])

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")


# --------------------------------------------------------------------------- #
# Safe JSON response — never crashes on unexpected types
# --------------------------------------------------------------------------- #
class SafeJSON(JSONResponse):
    """
    JSONResponse subclass that coerces any non-serializable value to a string
    instead of raising TypeError. This handles cases where MySQL returns
    timedelta, Decimal, datetime, or anything else Python's json module
    doesn't natively support.
    """
    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            default=str,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


# --------------------------------------------------------------------------- #
# DB helper
# --------------------------------------------------------------------------- #
def _db():
    from main import _db as main_db
    return main_db()


# --------------------------------------------------------------------------- #
# Value serialization — handles all MySQL column types
# --------------------------------------------------------------------------- #
def _fmt_value(v: Any) -> Any:
    """Convert a single DB value into a JSON-safe Python primitive."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat(sep=" ", timespec="seconds")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, timedelta):
        # MySQL TIME columns come back as timedelta
        total = int(v.total_seconds())
        h, rem = divmod(abs(total), 3600)
        m, _ = divmod(rem, 60)
        sign = "-" if total < 0 else ""
        return f"{sign}{h:02d}:{m:02d}"
    if isinstance(v, time):
        return f"{v.hour:02d}:{v.minute:02d}"
    if isinstance(v, (int, float, str, bool)):
        return v
    # Anything else (Decimal, bytes, etc.) — stringify
    return str(v)


def _serialize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert a list of DB rows into JSON-safe dicts."""
    return [{k: _fmt_value(v) for k, v in row.items()} for row in rows]


# --------------------------------------------------------------------------- #
# Shared CSS + shell
# --------------------------------------------------------------------------- #
CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  --bg: #f6f8fa; --panel: #ffffff; --border: #e1e6eb; --text: #1a202c;
  --muted: #6b7280; --accent: #2563eb; --accent-hover: #1d4ed8;
  --success: #16a34a; --warn: #ea580c; --danger: #dc2626;
  --sidebar-bg: #0f172a; --sidebar-text: #cbd5e1;
}
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: var(--bg); color: var(--text); display: flex; min-height: 100vh;
}
.sidebar {
  width: 240px; background: var(--sidebar-bg); color: var(--sidebar-text);
  padding: 24px 0; display: flex; flex-direction: column;
  position: sticky; top: 0; height: 100vh; flex-shrink: 0;
}
.brand { padding: 0 24px 24px; border-bottom: 1px solid rgba(255,255,255,0.1); margin-bottom: 16px; }
.brand-title { color: white; font-size: 15px; font-weight: 700; }
.brand-sub { color: #94a3b8; font-size: 12px; margin-top: 4px; }
.sidebar nav { display: flex; flex-direction: column; gap: 2px; padding: 0 12px; }
.sidebar nav a {
  color: var(--sidebar-text); text-decoration: none; padding: 10px 12px;
  border-radius: 6px; font-size: 14px;
}
.sidebar nav a:hover { background: rgba(255,255,255,0.06); color: white; }
.sidebar nav a.active { background: var(--accent); color: white; font-weight: 500; }
.sidebar-footer { margin-top: auto; padding: 16px 24px; font-size: 13px; }
.sidebar-footer a { color: #64748b; text-decoration: none; }
.sidebar-footer a:hover { color: white; }
.content { flex: 1; padding: 32px 40px; max-width: 1400px; }
.page-header {
  display: flex; justify-content: space-between; align-items: baseline;
  margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border);
}
.page-header h1 { font-size: 24px; font-weight: 700; }
.header-meta { font-size: 13px; color: var(--muted); }
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }
.stat-card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 20px; }
.stat-label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
.stat-value { font-size: 28px; font-weight: 700; margin-top: 8px; }
.stat-value.accent { color: var(--accent); }
.panel { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 24px; overflow: hidden; }
.panel-header { padding: 16px 20px; border-bottom: 1px solid var(--border); font-weight: 600; font-size: 14px; }
.panel-body { padding: 20px; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
table th {
  text-align: left; padding: 12px 20px; font-weight: 600; color: var(--muted);
  font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em;
  border-bottom: 1px solid var(--border);
}
table td { padding: 12px 20px; border-bottom: 1px solid var(--border); }
table tbody tr:last-child td { border-bottom: none; }
table tbody tr:hover { background: #f9fafb; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; font-weight: 500; }
.badge.HIGH, .badge.high { background: #fee2e2; color: #991b1b; }
.badge.MEDIUM, .badge.medium { background: #fed7aa; color: #9a3412; }
.badge.LOW, .badge.low { background: #dcfce7; color: #166534; }
.badge.open { background: #dbeafe; color: #1e40af; }
.badge.assigned { background: #dcfce7; color: #166534; }
.badge.escalated { background: #fee2e2; color: #991b1b; }
.badge.cancelled { background: #f3f4f6; color: #6b7280; }
.badge.sent { background: #e0e7ff; color: #3730a3; }
.badge.accepted { background: #dcfce7; color: #166534; }
.badge.declined { background: #fee2e2; color: #991b1b; }
.badge.expired { background: #f3f4f6; color: #6b7280; }
.badge.scheduled { background: #e0e7ff; color: #3730a3; }
.badge.covered { background: #dcfce7; color: #166534; }
.form-group { margin-bottom: 16px; }
.form-group label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; }
.form-group input, .form-group select, .form-group textarea {
  width: 100%; padding: 10px 12px; border: 1px solid var(--border);
  border-radius: 6px; font-size: 14px; font-family: inherit; background: white;
}
.form-group input:focus, .form-group select:focus, .form-group textarea:focus {
  outline: none; border-color: var(--accent); box-shadow: 0 0 0 3px rgba(37,99,235,0.1);
}
.form-group textarea { resize: vertical; min-height: 80px; }
.btn {
  display: inline-block; padding: 10px 20px; border: none; border-radius: 6px;
  font-size: 14px; font-weight: 500; cursor: pointer; font-family: inherit;
  text-decoration: none; text-align: center; transition: background 0.15s;
}
.btn-primary { background: var(--accent); color: white; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary { background: #f3f4f6; color: var(--text); }
.btn-secondary:hover { background: #e5e7eb; }
.form-row { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.result {
  margin-top: 16px; padding: 12px 16px; border-radius: 6px;
  font-family: ui-monospace, Menlo, Consolas, monospace; font-size: 12px;
  white-space: pre-wrap; display: none; max-height: 300px; overflow: auto;
}
.result.show { display: block; }
.result.success { background: #f0fdf4; border: 1px solid #86efac; color: #166534; }
.result.error { background: #fef2f2; border: 1px solid #fca5a5; color: #991b1b; }
.empty { padding: 40px; text-align: center; color: var(--muted); font-size: 14px; }
.token-link { color: var(--accent); text-decoration: none; font-family: ui-monospace, monospace; font-size: 12px; }
.token-link:hover { text-decoration: underline; }
.activity-item {
  padding: 10px 0; border-bottom: 1px solid var(--border);
  display: flex; gap: 12px; align-items: flex-start;
}
.activity-item:last-child { border-bottom: none; }
.activity-time {
  color: var(--muted); font-size: 12px; font-family: ui-monospace, monospace;
  min-width: 70px;
}
.activity-msg { flex: 1; font-size: 13px; }
"""


def _page(title: str, active: str, body: str, extra_js: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{title} — HR Auto Scheduler</title>
  <style>{CSS}</style>
</head>
<body>
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-title">HR Auto Scheduler</div>
      <div class="brand-sub">by Tahsin Ahmed</div>
    </div>
    <nav>
      <a href="{PREFIX}/dashboard" class="{'active' if active=='home' else ''}">Dashboard</a>
      <a href="{PREFIX}/dashboard/employees" class="{'active' if active=='employees' else ''}">Employees</a>
      <a href="{PREFIX}/dashboard/shifts" class="{'active' if active=='shifts' else ''}">Shifts</a>
      <a href="{PREFIX}/dashboard/coverage" class="{'active' if active=='coverage' else ''}">Coverage</a>
      <a href="{PREFIX}/dashboard/test" class="{'active' if active=='test' else ''}">Test Tools</a>
    </nav>
    <div class="sidebar-footer">
      <a href="/docs" target="_blank">API Docs →</a>
    </div>
  </aside>
  <main class="content">
    <header class="page-header">
      <h1>{title}</h1>
      <div class="header-meta">Updated: <span id="last-updated">—</span></div>
    </header>
    {body}
  </main>
  <script>const PREFIX = "{PREFIX}";</script>
  <script>{extra_js}</script>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# Home dashboard
# --------------------------------------------------------------------------- #
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_home() -> HTMLResponse:
    body = """
    <div class="stats">
      <div class="stat-card"><div class="stat-label">Employees</div><div class="stat-value accent" id="s-emp">—</div></div>
      <div class="stat-card"><div class="stat-label">Shifts This Week</div><div class="stat-value" id="s-shifts">—</div></div>
      <div class="stat-card"><div class="stat-label">Open Coverage</div><div class="stat-value" id="s-cov">—</div></div>
      <div class="stat-card"><div class="stat-label">Recent Leaves (24h)</div><div class="stat-value" id="s-leaves">—</div></div>
    </div>

    <div class="panel">
      <div class="panel-header">Recent Leave Events</div>
      <div id="leaves-panel"><div class="empty">Loading...</div></div>
    </div>

    <div class="panel">
      <div class="panel-header">Open Coverage Requests</div>
      <div id="cov-panel"><div class="empty">Loading...</div></div>
    </div>
    """
    js = """
    async function refresh() {
      try {
        const r = await fetch(PREFIX + '/api/dashboard/stats');
        const d = await r.json();
        document.getElementById('s-emp').textContent = d.employees;
        document.getElementById('s-shifts').textContent = d.shifts_week;
        document.getElementById('s-cov').textContent = d.open_coverage;
        document.getElementById('s-leaves').textContent = d.recent_leaves;
        document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();

        document.getElementById('leaves-panel').innerHTML = d.recent_leaves_rows.length
          ? '<table><thead><tr><th>Employee</th><th>Type</th><th>Urgency</th><th>Date</th><th>Detected</th></tr></thead><tbody>'
            + d.recent_leaves_rows.map(r =>
                '<tr><td>' + r.employee_id + '</td><td>' + r.leave_type + '</td>'
                + '<td><span class="badge ' + r.urgency + '">' + r.urgency + '</span></td>'
                + '<td>' + r.start_date + '</td><td>' + r.detected_at + '</td></tr>').join('')
            + '</tbody></table>'
          : '<div class="empty">No leave events yet. Use Test Tools to fire one.</div>';

        document.getElementById('cov-panel').innerHTML = d.open_coverage_rows.length
          ? '<table><thead><tr><th>Req#</th><th>Absent</th><th>Urgency</th><th>Status</th><th>Created</th></tr></thead><tbody>'
            + d.open_coverage_rows.map(r =>
                '<tr><td>#' + r.request_id + '</td><td>' + r.absent_employee_id + '</td>'
                + '<td><span class="badge ' + r.urgency + '">' + r.urgency + '</span></td>'
                + '<td><span class="badge ' + r.status + '">' + r.status + '</span></td>'
                + '<td>' + r.created_at + '</td></tr>').join('')
            + '</tbody></table>'
          : '<div class="empty">No open coverage requests.</div>';
      } catch (e) { console.error(e); }
    }
    refresh();
    setInterval(refresh, 5000);
    """
    return HTMLResponse(_page("Dashboard", "home", body, js))


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@router.get("/dashboard/employees", response_class=HTMLResponse)
async def dashboard_employees() -> HTMLResponse:
    body = """
    <div class="panel">
      <div class="panel-header">Provisioned Employees</div>
      <div id="emp-panel"><div class="empty">Loading...</div></div>
    </div>
    """
    js = """
    async function refresh() {
      try {
        const r = await fetch(PREFIX + '/api/dashboard/employees');
        const d = await r.json();
        document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
        document.getElementById('emp-panel').innerHTML = d.rows.length
          ? '<table><thead><tr><th>ID</th><th>Name</th><th>Email</th><th>Department</th><th>Title</th><th>Provisioned</th></tr></thead><tbody>'
            + d.rows.map(e =>
                '<tr><td>' + e.employee_id + '</td><td>' + e.full_name + '</td>'
                + '<td>' + e.corporate_email + '</td><td>' + e.department + '</td>'
                + '<td>' + e.job_title + '</td><td>' + e.provisioned_at + '</td></tr>').join('')
            + '</tbody></table>'
          : '<div class="empty">No employees yet.</div>';
      } catch (e) { console.error(e); }
    }
    refresh();
    setInterval(refresh, 5000);
    """
    return HTMLResponse(_page("Employees", "employees", body, js))


# --------------------------------------------------------------------------- #
# Shifts
# --------------------------------------------------------------------------- #
@router.get("/dashboard/shifts", response_class=HTMLResponse)
async def dashboard_shifts() -> HTMLResponse:
    body = """
    <div class="panel">
      <div class="panel-header">Upcoming Shifts (next 7 days)</div>
      <div id="shift-panel"><div class="empty">Loading...</div></div>
    </div>
    """
    js = """
    async function refresh() {
      try {
        const r = await fetch(PREFIX + '/api/dashboard/shifts');
        const d = await r.json();
        document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
        document.getElementById('shift-panel').innerHTML = d.rows.length
          ? '<table><thead><tr><th>Date</th><th>Employee</th><th>Time</th><th>Role</th><th>Location</th><th>Status</th></tr></thead><tbody>'
            + d.rows.map(s =>
                '<tr><td>' + s.shift_date + '</td><td>' + s.employee_id + '</td>'
                + '<td>' + s.start_time + ' – ' + s.end_time + '</td>'
                + '<td>' + s.role + '</td><td>' + (s.location || '—') + '</td>'
                + '<td><span class="badge ' + s.status + '">' + s.status + '</span></td></tr>').join('')
            + '</tbody></table>'
          : '<div class="empty">No shifts scheduled.</div>';
      } catch (e) { console.error(e); }
    }
    refresh();
    setInterval(refresh, 5000);
    """
    return HTMLResponse(_page("Shifts", "shifts", body, js))


# --------------------------------------------------------------------------- #
# Coverage
# --------------------------------------------------------------------------- #
@router.get("/dashboard/coverage", response_class=HTMLResponse)
async def dashboard_coverage() -> HTMLResponse:
    body = """
    <div class="panel">
      <div class="panel-header">Coverage Requests</div>
      <div id="req-panel"><div class="empty">Loading...</div></div>
    </div>
    <div class="panel">
      <div class="panel-header">Coverage Offers</div>
      <div id="offer-panel"><div class="empty">Loading...</div></div>
    </div>
    """
    js = """
    async function refresh() {
      try {
        const r = await fetch(PREFIX + '/api/dashboard/coverage');
        const d = await r.json();
        document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();

        document.getElementById('req-panel').innerHTML = d.requests.length
          ? '<table><thead><tr><th>Req#</th><th>Absent</th><th>Shift</th><th>Urgency</th><th>Status</th><th>Assigned To</th></tr></thead><tbody>'
            + d.requests.map(r =>
                '<tr><td>#' + r.request_id + '</td><td>' + r.absent_employee_id + '</td>'
                + '<td>' + r.shift_date + ' ' + r.start_time + '–' + r.end_time + ' (' + r.role + ')</td>'
                + '<td><span class="badge ' + r.urgency + '">' + r.urgency + '</span></td>'
                + '<td><span class="badge ' + r.status + '">' + r.status + '</span></td>'
                + '<td>' + (r.assigned_to || '—') + '</td></tr>').join('')
            + '</tbody></table>'
          : '<div class="empty">No coverage requests yet.</div>';

        document.getElementById('offer-panel').innerHTML = d.offers.length
          ? '<table><thead><tr><th>Req#</th><th>Candidate</th><th>Score</th><th>Status</th><th>Respond</th><th>Sent</th></tr></thead><tbody>'
            + d.offers.map(o =>
                '<tr><td>#' + o.request_id + '</td><td>' + o.candidate_employee_id + '</td>'
                + '<td>' + o.score + '</td>'
                + '<td><span class="badge ' + o.status + '">' + o.status + '</span></td>'
                + '<td><a class="token-link" href="/coverage/accept/' + o.token + '" target="_blank">Accept</a> · '
                + '<a class="token-link" href="/coverage/decline/' + o.token + '" target="_blank">Decline</a></td>'
                + '<td>' + o.sent_at + '</td></tr>').join('')
            + '</tbody></table>'
          : '<div class="empty">No offers sent yet.</div>';
      } catch (e) { console.error(e); }
    }
    refresh();
    setInterval(refresh, 5000);
    """
    return HTMLResponse(_page("Coverage", "coverage", body, js))


# --------------------------------------------------------------------------- #
# Test tools
# --------------------------------------------------------------------------- #
@router.get("/dashboard/test", response_class=HTMLResponse)
async def dashboard_test() -> HTMLResponse:
    today = date.today().isoformat()
    body = f"""
    <div class="panel">
      <div class="panel-header">Add a Shift to the Roster</div>
      <div class="panel-body">
        <div class="form-row">
          <div class="form-group"><label>Employee ID</label><input id="sh-emp" value="102"></div>
          <div class="form-group"><label>Date</label><input type="date" id="sh-date" value="{today}"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>Start Time</label><input type="time" id="sh-start" value="06:00"></div>
          <div class="form-group"><label>End Time</label><input type="time" id="sh-end" value="14:00"></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>Role</label><input id="sh-role" value="Machine Operator"></div>
          <div class="form-group"><label>Location</label><input id="sh-loc" value="Plant A"></div>
        </div>
        <button class="btn btn-primary" onclick="addShift(this)">Add Shift</button>
        <div class="result" id="sh-result"></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">Trigger Emergency Leave</div>
      <div class="panel-body">
        <div class="form-row">
          <div class="form-group"><label>Employee ID</label><input id="l-emp" value="101"></div>
          <div class="form-group"><label>Leave Type</label>
            <select id="l-type"><option>Emergency</option><option>Sick</option><option>Casual</option></select>
          </div>
        </div>
        <div class="form-group"><label>Reason</label>
          <textarea id="l-reason">Father is in the ICU.</textarea></div>
        <div class="form-row">
          <div class="form-group"><label>Start Date</label><input type="date" id="l-start" value="{today}"></div>
          <div class="form-group"><label>End Date</label><input type="date" id="l-end" value="{today}"></div>
        </div>
        <button class="btn btn-primary" onclick="fireLeave(this)">Fire Leave Webhook</button>
        <div class="result" id="l-result"></div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">⚡ Live Activity (auto-refresh every 3s)</div>
      <div class="panel-body" id="activity-body">
        <div class="empty">Waiting for activity...</div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-header">Force Poll Cycles</div>
      <div class="panel-body">
        <button class="btn btn-secondary" onclick="triggerPoll('onboarding', this)">Run Onboarding Poll</button>
        <button class="btn btn-secondary" onclick="triggerPoll('shift', this)">Run Shift Poll</button>
        <div class="result" id="p-result"></div>
      </div>
    </div>
    """

    js = f"""
    const SECRET = "{WEBHOOK_SECRET}";

    async function post(url, body, resultEl, btn) {{
      btn.disabled = true;
      const r = resultEl;
      r.className = 'result show';
      r.textContent = 'Working...';
      try {{
        const resp = await fetch(url, {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json', 'X-Webhook-Secret': SECRET }},
          body: body ? JSON.stringify(body) : null,
        }});
        const text = await resp.text();
        let data;
        try {{ data = JSON.parse(text); }} catch (e) {{ data = {{ raw: text }}; }}
        r.className = 'result show ' + (resp.ok && data.success !== false ? 'success' : 'error');
        r.textContent = JSON.stringify(data, null, 2);
      }} catch (e) {{
        r.className = 'result show error';
        r.textContent = 'Network error: ' + e.message;
      }}
      btn.disabled = false;
      refreshActivity();
    }}

    async function addShift(btn) {{
      btn.disabled = true;
      const r = document.getElementById('sh-result');
      try {{
        const resp = await fetch(PREFIX + '/api/dashboard/add-shift', {{
          method: 'POST',
          headers: {{ 'Content-Type': 'application/json' }},
          body: JSON.stringify({{
            employee_id: document.getElementById('sh-emp').value,
            shift_date:  document.getElementById('sh-date').value,
            start_time:  document.getElementById('sh-start').value + ':00',
            end_time:    document.getElementById('sh-end').value + ':00',
            role:        document.getElementById('sh-role').value,
            location:    document.getElementById('sh-loc').value,
          }}),
        }});
        const data = await resp.json();
        r.className = 'result show ' + (data.success ? 'success' : 'error');
        r.textContent = JSON.stringify(data, null, 2);
      }} catch (e) {{
        r.className = 'result show error';
        r.textContent = 'Error: ' + e.message;
      }}
      btn.disabled = false;
    }}

    function fireLeave(btn) {{
      post('/webhooks/emergency-leave', {{
        employee_id: document.getElementById('l-emp').value,
        leave_type: document.getElementById('l-type').value,
        reason: document.getElementById('l-reason').value,
        start_date: document.getElementById('l-start').value,
        end_date: document.getElementById('l-end').value,
      }}, document.getElementById('l-result'), btn);
    }}

    function triggerPoll(kind, btn) {{
      post(PREFIX + '/api/dashboard/trigger/' + kind, null, document.getElementById('p-result'), btn);
    }}

    async function refreshActivity() {{
      try {{
        const r = await fetch(PREFIX + '/api/dashboard/activity');
        const d = await r.json();
        document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();
        const el = document.getElementById('activity-body');
        if (!d.events.length) {{
          el.innerHTML = '<div class="empty">No activity yet. Fire a webhook above.</div>';
          return;
        }}
        el.innerHTML = d.events.map(e =>
          '<div class="activity-item">'
          + '<span class="activity-time">' + e.time + '</span>'
          + '<span class="activity-msg">' + e.message + '</span>'
          + '</div>'
        ).join('');
      }} catch (e) {{ console.error(e); }}
    }}
    refreshActivity();
    setInterval(refreshActivity, 3000);
    """
    return HTMLResponse(_page("Test Tools", "test", body, js))


# --------------------------------------------------------------------------- #
# JSON APIs
# --------------------------------------------------------------------------- #
@router.get("/api/dashboard/stats")
async def api_stats() -> SafeJSON:
    cutoff_24h = (datetime.now(timezone.utc) - timedelta(hours=24)) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM provisioned")
            employees = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(*) AS n FROM shifts WHERE status='scheduled' "
                "AND shift_date BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL 7 DAY)"
            )
            shifts_week = cur.fetchone()["n"]

            cur.execute("SELECT COUNT(*) AS n FROM coverage_requests WHERE status='open'")
            open_coverage = cur.fetchone()["n"]

            cur.execute(
                "SELECT COUNT(*) AS n FROM leave_events WHERE detected_at >= %s",
                (cutoff_24h,),
            )
            recent_leaves = cur.fetchone()["n"]

            cur.execute(
                "SELECT employee_id, leave_type, urgency, start_date, detected_at "
                "FROM leave_events ORDER BY detected_at DESC LIMIT 10"
            )
            recent_leaves_rows = _serialize(list(cur.fetchall()))

            cur.execute(
                "SELECT request_id, absent_employee_id, urgency, status, created_at "
                "FROM coverage_requests WHERE status IN ('open','assigned') "
                "ORDER BY created_at DESC LIMIT 10"
            )
            open_coverage_rows = _serialize(list(cur.fetchall()))

    return SafeJSON({
        "employees":          employees,
        "shifts_week":        shifts_week,
        "open_coverage":      open_coverage,
        "recent_leaves":      recent_leaves,
        "recent_leaves_rows": recent_leaves_rows,
        "open_coverage_rows": open_coverage_rows,
    })


@router.get("/api/dashboard/employees")
async def api_employees() -> SafeJSON:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT employee_id, full_name, corporate_email, department, job_title, provisioned_at "
                "FROM provisioned ORDER BY provisioned_at DESC LIMIT 200"
            )
            rows = _serialize(list(cur.fetchall()))
    return SafeJSON({"rows": rows})


@router.get("/api/dashboard/shifts")
async def api_shifts() -> SafeJSON:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT shift_id, employee_id, shift_date, start_time, end_time, "
                "role, location, status "
                "FROM shifts WHERE shift_date BETWEEN CURDATE() AND DATE_ADD(CURDATE(), INTERVAL 7 DAY) "
                "ORDER BY shift_date, start_time LIMIT 200"
            )
            rows = _serialize(list(cur.fetchall()))
    return SafeJSON({"rows": rows})


@router.get("/api/dashboard/coverage")
async def api_coverage() -> SafeJSON:
    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT cr.request_id, cr.absent_employee_id, cr.urgency, cr.status, "
                "cr.created_at, cr.assigned_to, s.shift_date, s.start_time, s.end_time, s.role "
                "FROM coverage_requests cr "
                "JOIN shifts s ON s.shift_id = cr.shift_id "
                "ORDER BY cr.created_at DESC LIMIT 50"
            )
            requests = _serialize(list(cur.fetchall()))

            cur.execute(
                "SELECT offer_id, request_id, candidate_employee_id, candidate_email, "
                "token, score, status, sent_at, responded_at "
                "FROM coverage_offers ORDER BY sent_at DESC LIMIT 100"
            )
            offers = _serialize(list(cur.fetchall()))

    return SafeJSON({"requests": requests, "offers": offers})


@router.get("/api/dashboard/activity")
async def api_activity() -> SafeJSON:
    events = []

    with closing(_db()) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT employee_id, leave_type, urgency, rationale, detected_at
                FROM leave_events ORDER BY event_id DESC LIMIT 5
            """)
            for row in cur.fetchall():
                events.append({
                    "ts": row["detected_at"],
                    "message": (
                        f"🚨 Leave: emp {row['employee_id']} "
                        f"({row['leave_type']}) → urgency "
                        f"<b>{row['urgency']}</b> — {row.get('rationale') or ''}"
                    ),
                })

            cur.execute("""
                SELECT request_id, absent_employee_id, urgency, status, created_at
                FROM coverage_requests ORDER BY request_id DESC LIMIT 5
            """)
            for row in cur.fetchall():
                events.append({
                    "ts": row["created_at"],
                    "message": (
                        f"📋 Coverage req #{row['request_id']} for "
                        f"emp {row['absent_employee_id']} → status "
                        f"<b>{row['status']}</b>"
                    ),
                })

            cur.execute("""
                SELECT request_id, candidate_employee_id, score, status, sent_at
                FROM coverage_offers ORDER BY offer_id DESC LIMIT 10
            """)
            for row in cur.fetchall():
                icon = "✅" if row["status"] == "accepted" else (
                       "❌" if row["status"] == "declined" else "📨")
                events.append({
                    "ts": row["sent_at"],
                    "message": (
                        f"{icon} Offer to emp {row['candidate_employee_id']} "
                        f"(score {row['score']}) → <b>{row['status']}</b>"
                    ),
                })

    events.sort(key=lambda e: e["ts"], reverse=True)
    events = events[:20]

    for e in events:
        e["time"] = e["ts"].split("T")[-1][:8] if "T" in e["ts"] else e["ts"]

    return SafeJSON({"events": events})


# --------------------------------------------------------------------------- #
# Trigger endpoints
# --------------------------------------------------------------------------- #
@router.post("/api/dashboard/trigger/onboarding")
async def trigger_onboarding() -> SafeJSON:
    try:
        from main import OnboardingPoller
        OnboardingPoller().run_once()
        return SafeJSON({"success": True, "message": "Onboarding poll completed"})
    except Exception as exc:
        return SafeJSON({"success": False, "error": str(exc)}, status_code=500)


@router.post("/api/dashboard/trigger/shift")
async def trigger_shift() -> SafeJSON:
    try:
        from shift_replacement import run_shift_poll
        run_shift_poll()
        return SafeJSON({"success": True, "message": "Shift poll completed"})
    except Exception as exc:
        return SafeJSON({"success": False, "error": str(exc)}, status_code=500)


@router.post("/api/dashboard/add-shift")
async def api_add_shift(request: Request) -> SafeJSON:
    """Add a shift to the roster. This is the REAL data source for shifts."""
    try:
        body = await request.json()
        from datetime import date as date_cls, time as time_cls
        from shifts import add_shift

        # Accept "HH:MM" or "HH:MM:SS"
        def _parse_time(s: str) -> time_cls:
            s = s.strip()
            if len(s) == 5:  # HH:MM
                s = s + ":00"
            return time_cls.fromisoformat(s)

        shift_id = add_shift(
            employee_id=body["employee_id"],
            shift_date=date_cls.fromisoformat(body["shift_date"]),
            start_time=_parse_time(body["start_time"]),
            end_time=_parse_time(body["end_time"]),
            role=body["role"],
            location=body.get("location", ""),
        )
        return SafeJSON({"success": True, "shift_id": shift_id})
    except KeyError as exc:
        return SafeJSON(
            {"success": False, "error": f"Missing field: {exc}"},
            status_code=400,
        )
    except Exception as exc:
        return SafeJSON({"success": False, "error": str(exc)}, status_code=500)