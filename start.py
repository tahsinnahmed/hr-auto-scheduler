"""
start.py — Single-command launcher for HR Auto Scheduler.

What it does:
    1. Verifies XAMPP MySQL is reachable.
    2. If HRM_REFRESH_TOKEN is missing from .env, runs the OAuth flow
       and writes the token back into .env automatically.
    3. Bootstraps the database and tables.
    4. Starts the pollers (onboarding + leave sync + shift coverage)
       and the FastAPI server.

Run:
    python start.py
    (or double-click start.bat on Windows)
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import os
import secrets
import sys
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx
import pymysql
from dotenv import load_dotenv, set_key

# --------------------------------------------------------------------------- #
# Console helpers
# --------------------------------------------------------------------------- #
GREEN  = "\033[92m"
YELLOW = "\033[93m"
RED    = "\033[91m"
CYAN   = "\033[96m"
RESET  = "\033[0m"


def ok(msg: str) -> None:
    print(f"{GREEN}✓{RESET} {msg}")


def info(msg: str) -> None:
    print(f"{CYAN}→{RESET} {msg}")


def warn(msg: str) -> None:
    print(f"{YELLOW}!{RESET} {msg}")


def die(msg: str) -> None:
    print(f"{RED}✗{RESET} {msg}")
    sys.exit(1)


# --------------------------------------------------------------------------- #
# Load .env
# --------------------------------------------------------------------------- #
SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH   = SCRIPT_DIR / ".env"

if not ENV_PATH.exists():
    die(
        f".env not found at {ENV_PATH}\n"
        "Create one with HRM_BASE_URL, HRM_CLIENT_ID, HRM_CLIENT_SECRET,\n"
        "COMPANY_NAME, and MYSQL_* settings."
    )

load_dotenv(ENV_PATH)


# --------------------------------------------------------------------------- #
# 1. Verify MySQL
# --------------------------------------------------------------------------- #
def check_mysql() -> None:
    host = os.getenv("MYSQL_HOST", "127.0.0.1")
    port = int(os.getenv("MYSQL_PORT", "3306"))
    user = os.getenv("MYSQL_USER", "root")
    pwd  = os.getenv("MYSQL_PASSWORD", "")

    info(f"Checking MySQL at {host}:{port}...")
    try:
        conn = pymysql.connect(
            host=host, port=port, user=user, password=pwd,
            connect_timeout=5, autocommit=True,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        conn.close()
        ok(f"MySQL reachable (version {version})")
    except pymysql.err.OperationalError as exc:
        die(
            f"Could not connect to MySQL at {host}:{port} — {exc}\n"
            "   → Open the XAMPP Control Panel and start the MySQL service."
        )


# --------------------------------------------------------------------------- #
# 2. OAuth flow
# --------------------------------------------------------------------------- #
REDIRECT_URI = "http://localhost:8766/callback"


def _run_oauth_flow() -> str:
    base          = os.environ["HRM_BASE_URL"].rstrip("/")
    client_id     = os.environ["HRM_CLIENT_ID"]
    client_secret = os.environ["HRM_CLIENT_SECRET"]

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()

    # NOTE: OrangeHRM does NOT accept a "scope" parameter in the authorization
    # request. Sending one causes "invalid_scope". The client's permissions
    # are configured when the OAuth client is created in the admin panel.
    auth_url = (
        f"{base}/web/index.php/oauth2/authorize?"
        + urllib.parse.urlencode({
            "response_type":         "code",
            "client_id":             client_id,
            "redirect_uri":          REDIRECT_URI,
            "code_challenge":        challenge,
            "code_challenge_method": "S256",
        })
    )

    print()
    warn("No HRM_REFRESH_TOKEN in .env — running the one-time OAuth flow.")
    print(f"   Opening: {auth_url}")
    print("   Log in as OrangeHRM admin and approve access in the browser.")
    webbrowser.open(auth_url)

    code_holder: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.urlparse(self.path).query
            code_holder["code"] = urllib.parse.parse_qs(qs).get("code", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(
                b"<h2>Authorization received.</h2>"
                b"<p>You can close this tab and return to the terminal.</p>"
            )
        def log_message(self, *args, **kwargs):
            pass

    info(f"Waiting for browser redirect on {REDIRECT_URI} ...")
    server = HTTPServer(("localhost", 8766), Handler)
    server.handle_request()
    server.server_close()

    code = code_holder.get("code")
    if not code:
        die("No authorization code received. Aborting.")

    info("Exchanging authorization code for tokens...")
    resp = httpx.post(
        f"{base}/web/index.php/oauth2/token",
        data={
            "grant_type":    "authorization_code",
            "client_id":     client_id,
            "client_secret": client_secret,
            "code":          code,
            "redirect_uri":  REDIRECT_URI,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    resp.raise_for_status()
    payload = resp.json()
    return payload["refresh_token"]


def ensure_refresh_token() -> None:
    token = os.getenv("HRM_REFRESH_TOKEN", "").strip()
    if token:
        ok("HRM_REFRESH_TOKEN present in .env")
        return

    refresh = _run_oauth_flow()

    set_key(str(ENV_PATH), "HRM_REFRESH_TOKEN", refresh)
    os.environ["HRM_REFRESH_TOKEN"] = refresh
    ok("Refresh token saved to .env — you will not need to log in again.")


# --------------------------------------------------------------------------- #
# 3. Hand off to main.py
# --------------------------------------------------------------------------- #
def main() -> None:
    print()
    print(f"{CYAN}╭──────────────────────────────────────────────╮{RESET}")
    print(f"{CYAN}│ HR Auto Scheduler © Copyright : Tahsin Ahmed │{RESET}")
    print(f"{CYAN}╰──────────────────────────────────────────────╯{RESET}")
    print()

    check_mysql()
    ensure_refresh_token()

    info("Loading main.py...")
    _main = importlib.import_module("main")

    info("Bootstrapping database...")
    _main.init_db()
    ok("Database ready")

    info(f"Starting pollers (interval: {os.getenv('POLL_INTERVAL_SECONDS', '300')}s)...")
    scheduler = _main.start_poller_scheduler()

    info("Starting webhook server on http://0.0.0.0:8000 ...")
    print()
    ok("Service is live. Press Ctrl+C to stop.")
    print()

    import uvicorn

    try:
        uvicorn.run(_main.app, host="0.0.0.0", port=8000, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        info("Shutting down pollers...")
        scheduler.shutdown(wait=False)
        ok("Stopped cleanly.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
        ok("Interrupted by user.")