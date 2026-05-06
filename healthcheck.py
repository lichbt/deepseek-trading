#!/usr/bin/env python3
"""
Healthcheck: Validates that the research pipeline is healthy.
Intended to run as a daily cron job or manual check.

Checks:
  1. OANDA reachable (recent candle fetch)
  2. pipeline.db accessible and below size limit
  3. Research progress (new candidate within last 24h)
  4. Research loop process running
"""

import sys
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from pipeline_utils import get_db_connection, init_db

OANDA_ACCOUNT_ID = os.getenv('OANDA_ACCOUNT_ID', '')
OANDA_API_TOKEN = os.getenv('OANDA_API_TOKEN', '')
OANDA_BASE_URL = 'https://api-fxpractice.oanda.com'

DB_PATH = Path(__file__).parent / 'pipeline.db'
MAX_DB_SIZE_MB = 500  # alert if DB exceeds this
MAX_DB_SIZE_BYTES = MAX_DB_SIZE_MB * 1024 * 1024


def check_oanda_reachable() -> tuple:
    """Fetch a small recent candle to verify OANDA API is up."""
    try:
        if not OANDA_ACCOUNT_ID or not OANDA_API_TOKEN:
            return False, "OANDA credentials not set"

        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=2)

        headers = {
            'Authorization': f'Bearer {OANDA_API_TOKEN}',
            'Content-Type': 'application/json',
        }
        params = {
            'granularity': 'H1',
            'from': start.isoformat(),
            'count': 5,
        }
        url = f'{OANDA_BASE_URL}/v3/instruments/EUR_USD/candles'
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        candles = data.get('candles', [])
        if candles:
            return True, f"OK ({len(candles)} candles fetched)"
        return False, "Empty response from OANDA"
    except Exception as e:
        return False, f"OANDA unreachable: {e}"


def check_database_healthy() -> tuple:
    """Check DB exists, is readable, and below size limit."""
    if not DB_PATH.exists():
        return False, f"DB not found at {DB_PATH}"

    size_bytes = DB_PATH.stat().st_size
    size_mb = size_bytes / (1024 * 1024)

    if size_bytes > MAX_DB_SIZE_BYTES:
        return False, f"DB too large: {size_mb:.1f} MB (limit {MAX_DB_SIZE_MB} MB)"

    # Verify it's readable and tables exist
    try:
        init_db()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM strategies")
            count = cursor.fetchone()[0]
        return True, f"OK ({(count)} strategies, {size_mb:.1f} MB)"
    except Exception as e:
        return False, f"DB read error: {e}"


def check_research_progress() -> tuple:
    """At least one new candidate within the last 24 hours."""
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT COUNT(*) FROM strategies WHERE created_at >= ?",
                (cutoff,)
            )
            count = cursor.fetchone()[0]
        if count > 0:
            return True, f"OK ({count} new candidate(s) in last 24h)"
        return False, "No new candidates in last 24 hours"
    except Exception as e:
        return False, f"Progress check failed: {e}"


def check_loop_running() -> tuple:
    """Check if auto_research process is alive."""
    try:
        result = subprocess.run(
            ['pgrep', '-f', 'auto_research.py'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split('\n')
            return True, f"OK (processes: {len(pids)})"
        return False, "auto_research.py not running"
    except Exception as e:
        return False, f"pgrep failed: {e}"


def send_alert(message: str):
    """Send Telegram alert if bot is available."""
    try:
        from telegram_bot import send_message
        send_message(f"[HEALTHCHECK] {message}")
    except Exception:
        pass  # Silently skip Telegram if not available


def main():
    checks = [
        ("OANDA API", check_oanda_reachable),
        ("Database", check_database_healthy),
        ("Research Progress", check_research_progress),
        ("Loop Process", check_loop_running),
    ]

    all_passed = True
    print(f"[healthcheck] {datetime.now().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("-" * 50)

    for name, check_fn in checks:
        passed, detail = check_fn()
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {name}: {detail}")
        if not passed:
            all_passed = False
            send_alert(f"FAIL: {name} — {detail}")

    print("-" * 50)
    if all_passed:
        print("All checks passed.")
        return 0
    else:
        print("Some checks FAILED — see above.")
        return 1


if __name__ == '__main__':
    sys.exit(main())
