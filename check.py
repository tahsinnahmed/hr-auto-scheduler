"""
check.py — Diagnostic for HR Auto Scheduler.

Runs 8 independent tests. Each prints PASS or FAIL with details.
Run:  python check.py
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ENV_PATH   = SCRIPT_DIR / ".env"

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"


def line(name: str, passed: bool, detail: str = "") -> None:
    status = PASS if passed else FAIL
    print(f"  [{status}] {name}")
    if detail:
        for l in detail.split("\n"):
            print(f"         {l}")


def test_1_env_exists() -> bool:
    print("\n[1] .env file")
    if not ENV_PATH.exists():
        line(".env exists", False, f"Not found at {ENV_PATH}")
        return False
    size = ENV_PATH.stat().st_size
    line(".env exists", True, f"Size: {size} bytes")

    raw = ENV_PATH.read_bytes()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    has_utf16 = raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    line("No BOM", not (has_bom or has_utf16),
         f"BOM detected!" if has_bom or has_utf16 else "Clean encoding")
    return True


def test_2_env_loads() -> bool:
    print("\n[2] .env loading")
    from dotenv import dotenv_values
    values = dotenv_values(ENV_PATH)

    required = ["HRM_BASE_URL", "HRM_CLIENT_ID", "HRM_CLIENT_SECRET",
                "MYSQL_HOST", "MYSQL_USER", "MYSQL_DATABASE"]
    ok = True
    for key in required:
        val = values.get(key, "")
        if val:
            shown = val[:8] + "..." if len(val) > 12 else val
            line(key, True, shown)
        else:
            line(key, False, "EMPTY or MISSING")
            ok = False
    return ok


def test_3_mysql() -> bool:
    print("\n[3] MySQL connection")
    try:
        import pymysql
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
        conn = pymysql.connect(
            host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            port=int(os.getenv("MYSQL_PORT", "3306")),
            user=os.getenv("MYSQL_USER", "root"),
            password=os.getenv("MYSQL_PASSWORD", ""),
            connect_timeout=5,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            v = cur.fetchone()[0]
        conn.close()
        line("MySQL reachable", True, f"Version: {v}")
        return True
    except Exception as exc:
        line("MySQL reachable", False, str(exc))
        line("Hint", False, "Start MySQL in XAMPP Control Panel")
        return False


def test_4_orangehrm() -> bool:
    print("\n[4] OrangeHRM reachable")
    try:
        import httpx
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
        base = os.getenv("HRM_BASE_URL", "").rstrip("/")
        resp = httpx.get(f"{base}/web/index.php/auth/login", timeout=5.0,
                         follow_redirects=True)
        line("OrangeHRM reachable", resp.status_code < 500,
             f"HTTP {resp.status_code} from {base}")
        return resp.status_code < 500
    except Exception as exc:
        line("OrangeHRM reachable", False, str(exc))
        line("Hint", False, "Start Apache in XAMPP Control Panel")
        return False


def test_5_refresh_token() -> bool:
    print("\n[5] Refresh token")
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    token = os.getenv("HRM_REFRESH_TOKEN", "").strip()
    if not token:
        line("Token present", False, "Empty — OAuth flow will run on start")
        return True  # not fatal — start.py will handle it
    line("Token present", True, f"Length: {len(token)} chars")

    try:
        import httpx
        base = os.getenv("HRM_BASE_URL", "").rstrip("/")
        resp = httpx.post(
            f"{base}/web/index.php/oauth2/token",
            data={
                "grant_type":    "refresh_token",
                "client_id":     os.getenv("HRM_CLIENT_ID"),
                "client_secret": os.getenv("HRM_CLIENT_SECRET"),
                "refresh_token": token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            line("Token valid", True, "OrangeHRM accepted it")
            return True
        else:
            line("Token valid", False,
                 f"HTTP {resp.status_code}. Token expired — clear it in .env.")
            return False
    except Exception as exc:
        line("Token check", False, str(exc))
        return False


def test_6_port_8000() -> bool:
    print("\n[6] Port 8000 available")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.bind(("0.0.0.0", 8000))
        s.close()
        line("Port 8000 free", True, "Nothing is using it")
        return True
    except OSError as exc:
        s.close()
        line("Port 8000 free", False, f"In use — {exc}")
        line("Hint", False, "Run: taskkill /F /IM python.exe")
        return False


def test_7_imports() -> bool:
    print("\n[7] Python modules import")
    modules = ["fastapi", "uvicorn", "httpx", "pymysql", "apscheduler",
               "tenacity", "pydantic", "dotenv", "main", "hr_emailer",
               "urgency", "shifts", "shift_replacement", "seed_data"]
    all_ok = True
    for name in modules:
        try:
            __import__(name)
            line(name, True)
        except Exception as exc:
            line(name, False, str(exc)[:80])
            all_ok = False
    return all_ok


def test_8_webhook_secret() -> bool:
    print("\n[8] Webhook secret")
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH, override=True)
    secret = os.getenv("WEBHOOK_SECRET", "")
    if not secret:
        line("WEBHOOK_SECRET", False, "Empty — webhooks will reject")
        return False
    line("WEBHOOK_SECRET", True, f"Length: {len(secret)}")
    return True


def main() -> None:
    print("=" * 60)
    print("HR Auto Scheduler — Diagnostics")
    print("=" * 60)

    results = [
        test_1_env_exists(),
        test_2_env_loads(),
        test_3_mysql(),
        test_4_orangehrm(),
        test_5_refresh_token(),
        test_6_port_8000(),
        test_7_imports(),
        test_8_webhook_secret(),
    ]

    print()
    print("=" * 60)
    if all(results):
        print(f"  {PASS} — All checks passed. Run start.bat")
    else:
        failed = [i + 1 for i, r in enumerate(results) if not r]
        print(f"  {FAIL} — Failed tests: {failed}")
        print("  Fix those, then re-run this script.")
    print("=" * 60)


if __name__ == "__main__":
    main()