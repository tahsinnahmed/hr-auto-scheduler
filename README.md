# HR Auto Scheduler

Automated shift replacement for emergency leaves — detects a callout, ranks available staff, emails them, and confirms the replacement. No phone calls. No spreadsheets. No manual follow-up.

## System Architecture

<img width="1536" height="1024" alt="architecture" src="https://github.com/user-attachments/assets/1c0fdbd2-0ec0-4ec2-93ec-5b705ec8eb19" />

---

## Table of Contents

- [The Problem](#the-problem)
- [What It Does](#what-it-does)
- [System Architecture](#system-architecture)
- [Screenshots](#screenshots)
- [Features](#features)
- [Tech Stack](#tech-stack)
- [Installation](#installation)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [API Reference](#api-reference)
- [Project Structure](#project-structure)
- [Testing](#testing)
- [Troubleshooting](#troubleshooting)
- [Roadmap](#roadmap)
- [License](#license)

---

## The Problem

When an employee calls in sick, especially on short notice, someone has to:

1. Find out which shift they were covering
2. Figure out who else is off-duty and qualified
3. Call or text each candidate one-by-one
4. Wait for someone to say yes
5. Update the schedule
6. Notify the manager

That's **45–90 minutes of manual work** per incident — and it happens at 5 AM when nobody wants to make phone calls.

## What It Does

**HR Auto Scheduler** reduces this to **2–10 minutes** with zero human coordination:

1. Emergency leave is logged (via webhook or OrangeHRM Leave API)
2. AI classifies the urgency (rules + Groq LLM fallback)
3. The system finds the affected shift and ranks every off-duty candidate
4. Top-ranked candidates receive a personalized email with **Accept / Decline** buttons
5. First to accept gets the shift — everyone else's offer auto-expires
6. HR watches the whole thing unfold live on the dashboard

---

## Screenshots

### Live Dashboard

Real-time stats and activity feed. Refreshes every 5 seconds.

<img width="1887" height="657" alt="HRAS 3" src="https://github.com/user-attachments/assets/7adae7cc-aa6d-4f3e-b591-e4662c771440" />

### Test Tools — Fire a Leave in One Click

<img width="1880" height="880" alt="HRAS 4" src="https://github.com/user-attachments/assets/beb970de-7202-46ff-b53b-8ce2670f7f3b" />

### Startup Logs — Everything Running

<img width="1919" height="1018" alt="HRAS 2" src="https://github.com/user-attachments/assets/c674a2bc-eb70-4477-ac29-7dad69bc8a59" />

### Proof It Works — Emails Sent

Every candidate receives a personalized offer. First to accept wins.

<img width="1913" height="489" alt="HRAS 5" src="https://github.com/user-attachments/assets/769111ef-1238-41a6-9a98-befb0a2f450a" />

### Employee Roster

<img width="1909" height="876" alt="HRAS 6" src="https://github.com/user-attachments/assets/6c4083bd-4e75-4a04-a5ad-f64c30d1a754" />

### Email Sent to Employee

<img width="1915" height="903" alt="HRAS 7" src="https://github.com/user-attachments/assets/43c557ec-476f-4084-903d-d3cbc53c875f" />


---

## Features

| Feature | Description |
|---|---|
| **Automated leave detection** | Webhook endpoint + polling OrangeHRM's Leave API |
| **AI urgency classifier** | Keyword rules first, Groq LLM for ambiguous cases |
| **Weighted candidate matching** | Role, hours, familiarity, availability — all scored |
| **Personalized offers** | Each candidate gets a unique accept/decline link |
| **Race-safe acceptance** | First accept wins; siblings auto-expire |
| **HR escalation** | Timeout after 60 min notifies the manager |
| **Live dashboard** | Real-time stats + activity feed |
| **Idempotent** | Duplicate webhook calls, poller restarts — all safe |
| **Zero-cost AI** | Groq's free tier (optional — rules work standalone) |
| **Single-command launcher** | Double-click `start.bat` — done |

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Source system** | OrangeHRM 5.x (Starter) |
| **Web server** | FastAPI + Uvicorn |
| **Database** | MySQL 10.4 (via XAMPP) |
| **Scheduler** | APScheduler (in-process) |
| **AI classifier** | Groq API — `llama-3.1-8b-instant` |
| **Auth** | OAuth 2.0 (Authorization Code + PKCE) |
| **Email** | SMTP/TLS via Gmail App Password |
| **Retry policy** | tenacity (exponential backoff) |
| **Frontend** | Vanilla JS + CSS (no build step) |

---

## Installation

### Prerequisites

- **Python 3.10+** — [python.org](https://www.python.org/downloads/)
- **XAMPP** — [apachefriends.org](https://www.apachefriends.org/)
- **OrangeHRM 5.x** — installed into XAMPP's `htdocs/orangehrm`
- **Gmail App Password** — for sending emails (optional, but recommended)

### Quick Start

```bat
git clone https://github.com/tahsinnahmed/hr-auto-scheduler.git
cd hr-auto-scheduler

REM Double-click start.bat — it does everything automatically
start.bat
