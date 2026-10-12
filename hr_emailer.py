"""
hr_emailer.py — Welcome emails + shift coverage emails.
"""
from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("hr.emailer")


SMTP_HOST      = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT      = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER      = os.getenv("SMTP_USER", "").strip()
SMTP_PASS      = os.getenv("SMTP_PASS", "").strip()
SMTP_FROM      = os.getenv("SMTP_FROM", SMTP_USER).strip()
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "HR Team").strip()

COMPANY_NAME     = os.getenv("COMPANY_NAME", "Our Company").strip()
HR_CONTACT_EMAIL = os.getenv("HR_CONTACT_EMAIL", SMTP_FROM).strip()


# --------------------------------------------------------------------------- #
# Shared sender
# --------------------------------------------------------------------------- #
def _send(to_email: str, subject: str, plain: str, html: str) -> bool:
    if not SMTP_HOST or not SMTP_USER:
        log.info("─" * 60)
        log.info("📧  EMAIL (dry-run — no SMTP configured)")
        log.info("    To:      %s", to_email)
        log.info("    Subject: %s", subject)
        log.info("    Preview:")
        for line in plain.splitlines()[:8]:
            log.info("      %s", line)
        log.info("─" * 60)
        return True

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{SMTP_FROM_NAME} <{SMTP_FROM}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(plain, "plain", "utf-8"))
        msg.attach(MIMEText(html,  "html",  "utf-8"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as s:
            s.ehlo(); s.starttls(); s.ehlo()
            s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        log.info("📧  Email SENT → %s", to_email)
        return True
    except Exception:
        log.exception("Failed to send email to %s", to_email)
        return False


# --------------------------------------------------------------------------- #
# 1. Welcome email (new hires)
# --------------------------------------------------------------------------- #
def _build_welcome_email(
    first_name, job_title, department, start_date, hr_contact_email,
) -> tuple[str, str, str]:
    subject = f"🎉 Welcome to {COMPANY_NAME}, {first_name}!"

    plain = f"""Hi {first_name},

Welcome to {COMPANY_NAME}! We're thrilled to have you joining us as
{job_title} in the {department} team.

Start date: {start_date}

WHAT TO EXPECT ON YOUR FIRST DAY
  • Arrive at 9:00 AM (or your confirmed start time)
  • Bring a valid photo ID for building access
  • Your manager will meet you at reception
  • Your onboarding buddy will guide you through week one

BEFORE YOU START
  • Complete the attached new-hire paperwork
  • Reply to this email if you have any questions
  • Save our HR contact for emergencies

HR CONTACT
  Email: {hr_contact_email}

We can't wait to see you on {start_date}. Welcome aboard!

— The {COMPANY_NAME} Team
"""

    html = f"""\
<!DOCTYPE html>
<html><body style="font-family: -apple-system, sans-serif; color: #222; max-width: 600px; margin: 0 auto; padding: 24px;">
  <h1 style="color: #2c3e50; font-size: 24px;">🎉 Welcome to {COMPANY_NAME}, {first_name}!</h1>
  <p style="font-size: 16px; line-height: 1.6;">
    We're thrilled to have you joining us as <strong>{job_title}</strong>
    in the <strong>{department}</strong> team.
  </p>
  <p style="font-size: 16px; padding: 12px 16px; background: #f0f7ff; border-left: 4px solid #3498db; border-radius: 4px;">
    <strong>Your start date:</strong> {start_date}
  </p>
  <h2 style="color: #2c3e50; font-size: 18px; margin-top: 32px;">What to expect on your first day</h2>
  <ul style="font-size: 15px; line-height: 1.8;">
    <li>Arrive at <strong>9:00 AM</strong></li>
    <li>Bring a valid photo ID</li>
    <li>Your manager will meet you at reception</li>
    <li>Your onboarding buddy will guide you</li>
  </ul>
  <h2 style="color: #2c3e50; font-size: 18px; margin-top: 32px;">HR contact</h2>
  <p>📧 <a href="mailto:{hr_contact_email}">{hr_contact_email}</a></p>
  <p style="color: #888; font-size: 13px; margin-top: 40px; border-top: 1px solid #eee; padding-top: 16px;">
    — The {COMPANY_NAME} Team
  </p>
</body></html>
"""
    return subject, html, plain


def send_welcome_email(
    to_email: str,
    first_name: str,
    full_name: str,
    job_title: str,
    department: str,
    start_date: str,
) -> bool:
    if not to_email or "@" not in to_email:
        log.warning("No valid personal email — skipping welcome email for %s", full_name)
        return False
    subject, html, plain = _build_welcome_email(
        first_name, job_title, department, start_date, HR_CONTACT_EMAIL,
    )
    return _send(to_email, subject, plain, html)


# --------------------------------------------------------------------------- #
# 2. Coverage offer email (shift replacement)
# --------------------------------------------------------------------------- #
def send_coverage_email(
    to_email: str,
    candidate_first_name: str,
    absent_employee_name: str,
    shift_date: str,
    shift_start: str,
    shift_end: str,
    role: str,
    location: str,
    urgency: str,
    accept_url: str,
    decline_url: str,
) -> bool:
    if not to_email or "@" not in to_email:
        return False

    urgency_prefix = {"HIGH": "[URGENT] ", "MEDIUM": "", "LOW": "[Optional] "}.get(urgency, "")
    subject = f"{urgency_prefix}Shift coverage needed: {shift_date} {shift_start}-{shift_end}"

    location_line = f"\nLocation: {location}" if location else ""
    urgency_note = {
        "HIGH":   "This is a high-priority request. Please respond as soon as possible.",
        "MEDIUM": "Please respond when you have a moment.",
        "LOW":    "No pressure — respond only if you're available.",
    }.get(urgency, "")

    plain = f"""Hi {candidate_first_name},

A colleague ({absent_employee_name}) had an emergency and cannot work their shift:

    Date:  {shift_date}
    Time:  {shift_start} - {shift_end}
    Role:  {role}{location_line}

{urgency_note}

Can you cover it?

  ✓ ACCEPT:  {accept_url}
  ✗ DECLINE: {decline_url}

Thanks,
— The {COMPANY_NAME} Scheduling Team
"""

    html = f"""\
<!DOCTYPE html>
<html><body style="font-family: -apple-system, sans-serif; color: #222; max-width: 600px; margin: 0 auto; padding: 24px;">
  <h1 style="color: #2c3e50; font-size: 22px;">Shift coverage needed</h1>
  <p>Hi {candidate_first_name}, a colleague had an emergency and cannot work their shift:</p>
  <table style="font-size: 15px; margin: 20px 0;">
    <tr><td style="color: #888; padding-right: 16px;">Date</td><td><strong>{shift_date}</strong></td></tr>
    <tr><td style="color: #888; padding-right: 16px;">Time</td><td><strong>{shift_start} – {shift_end}</strong></td></tr>
    <tr><td style="color: #888; padding-right: 16px;">Role</td><td>{role}</td></tr>
    {f'<tr><td style="color: #888; padding-right: 16px;">Location</td><td>{location}</td></tr>' if location else ''}
  </table>
  <p style="color: #555;">{urgency_note}</p>
  <div style="margin: 32px 0;">
    <a href="{accept_url}" style="display:inline-block;padding:14px 32px;background:#27ae60;color:white;text-decoration:none;border-radius:6px;font-weight:bold;margin-right:12px;">✓ Yes, I can cover it</a>
    <a href="{decline_url}" style="display:inline-block;padding:14px 32px;background:#e74c3c;color:white;text-decoration:none;border-radius:6px;">✗ No, I can't</a>
  </div>
  <p style="color: #888; font-size: 13px; border-top: 1px solid #eee; padding-top: 16px;">
    — The {COMPANY_NAME} Scheduling Team
  </p>
</body></html>
"""
    return _send(to_email, subject, plain, html)


# --------------------------------------------------------------------------- #
# 3. Escalation email (HR)
# --------------------------------------------------------------------------- #
def send_escalation_email(
    to_email: str,
    request_id: int,
    shift_date: str,
    shift_start: str,
    shift_end: str,
    role: str,
    absent_employee_id: str,
    reason: str,
) -> bool:
    subject = f"[HR ACTION] Uncovered shift needs manual attention (request #{request_id})"
    plain = f"""Hi HR,

Shift coverage request #{request_id} needs manual attention.

  Reason:          {reason}
  Absent employee: {absent_employee_id}
  Shift:           {shift_date} {shift_start} - {shift_end}
  Role:            {role}

Please reach out to available staff directly.

— Smart Shift Replacement System
"""
    html = f"<pre style='font-family: sans-serif; white-space: pre-wrap;'>{plain}</pre>"
    return _send(to_email, subject, plain, html)