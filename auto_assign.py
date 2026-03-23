#!/home/fguirama/Desktop/eval/.venv/bin/python
"""
PeerSphere — 42Berlin Evaluation Auto-Assigner

Automatically polls POST /backend/intra/teams/assign/ every 30 seconds
so you can keep coding instead of manually refreshing for eval slots.

Usage:
    1. Copy .env.example to .env and fill in your tokens
    2. python3 auto_assign.py

    Or pass tokens directly:
    python3 auto_assign.py --team-id 7198790 --project kfs-2
"""

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

try:
    import requests
except ImportError:
    print("Missing dependency: requests")
    print("Install it with: pip3 install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://evaluations.42berlin.de"
ASSIGN_ENDPOINT = f"{BASE_URL}/backend/intra/teams/assign/"
USER_INFO_ENDPOINT = f"{BASE_URL}/backend/intra/teams/"
TOKEN_REFRESH_ENDPOINT = f"{BASE_URL}/backend/api/accounts/token/refresh/"
POLL_INTERVAL = 30  # seconds

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("peersphere")

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

running = True


def _handle_signal(signum, _frame):
    global running
    log.info("Received signal %s — shutting down gracefully.", signum)
    running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


def refresh_access_token(session: requests.Session, refresh_token: str) -> bool:
    """Use the refresh token to obtain a new access token via Set-Cookie."""
    log.info("Attempting to refresh access token...")
    try:
        resp = session.post(
            TOKEN_REFRESH_ENDPOINT,
            json={"refresh": refresh_token, "refresh_token": refresh_token},
            timeout=15,
        )
        if resp.status_code == 200:
            # Server sets new tokens via Set-Cookie headers,
            # requests.Session picks them up automatically.
            log.info("Access token refreshed successfully.")
            return True
        log.warning("Token refresh failed (HTTP %s): %s", resp.status_code, resp.text[:200])
    except requests.RequestException as exc:
        log.error("Token refresh request error: %s", exc)
    return False


class MissingUserData(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__("Missing project name and team id")


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def build_session(access_token: str, refresh_token: str, csrf_token: str, session_id: str) -> requests.Session:
    """Build a requests.Session with the necessary headers and cookies."""
    s = requests.Session()

    # Cookies
    s.cookies.set("access_token", access_token, domain="evaluations.42berlin.de")
    s.cookies.set("refresh_token", refresh_token, domain="evaluations.42berlin.de")
    s.cookies.set("csrftoken", csrf_token, domain="evaluations.42berlin.de")
    s.cookies.set("sessionid", session_id, domain="evaluations.42berlin.de")

    # Headers that stay constant
    s.headers.update(
        {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": BASE_URL,
            "Referer": f"{BASE_URL}/profile",
            "X-CSRFToken": csrf_token,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
                "Gecko/20100101 Firefox/147.0"
            ),
        }
    )
    return s


def get_user_info(
        session: requests.Session,
        refresh_token: str,
):

    try:
        resp = session.get(USER_INFO_ENDPOINT, timeout=15)
    except requests.RequestException as exc:
        log.error("Request failed: %s", exc)
        raise MissingUserData()


    if resp.status_code == 401:
        log.warning("Unauthorized (401) — token may have expired.")
        refreshed = refresh_access_token(session, refresh_token)
        if refreshed:
            try:
                resp = session.get(USER_INFO_ENDPOINT, timeout=15)
            except requests.RequestException as exc:
                log.error("Request failed: %s", exc)
                raise Exception()
            if resp.status_code >= 400:
                log.error(
                    "Could not refresh token. You may need to log in again and update .env"
                )
                raise MissingUserData()

    data = resp.json()
    project = data['teams'][0]
    return project['id'], project['project_name']


def try_assign(
    session: requests.Session,
    team_id: int,
    project_name: str,
    refresh_token: str,
    csrf_token: str,
    session_id: str,
) -> bool:
    """
    Send a single assign request.

    Returns True if assignment succeeded (script should stop).
    Handles token refresh on 401.
    """
    payload = {"team_id": team_id, "project_name": project_name}

    try:
        resp = session.post(ASSIGN_ENDPOINT, json=payload, timeout=15)
    except requests.RequestException as exc:
        log.error("Request failed: %s", exc)
        return False

    status = resp.status_code
    body = resp.text[:300]

    if status == 200:
        log.info("Assignment SUCCESSFUL! Response: %s", body)
        return True

    if status == 201:
        log.info("Assignment CREATED! Response: %s", body)
        return True

    if status == 401:
        log.warning("Unauthorized (401) — token may have expired.")
        refreshed = refresh_access_token(session, refresh_token)
        if refreshed:
            # Session cookies are updated automatically by the server's Set-Cookie
            log.info("Retrying assign with refreshed token...")
            try:
                resp = session.post(ASSIGN_ENDPOINT, json=payload, timeout=15)
                if resp.status_code in (200, 201):
                    log.info("Assignment SUCCESSFUL after token refresh! Response: %s", resp.text[:300])
                    return True
                log.warning("Retry after refresh got HTTP %s: %s", resp.status_code, resp.text[:300])
            except requests.RequestException as exc:
                log.error("Retry request failed: %s", exc)
        else:
            log.error(
                "Could not refresh token. You may need to log in again and update .env"
            )
        return False

    if status == 429:
        log.warning("Rate limited (429). Will wait and retry next cycle.")
        return False

    # Any other status
    log.info("HTTP %s — %s", status, body)
    return False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def load_env_file():
    """Load .env file from the script's directory if it exists."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                os.environ.setdefault(key, value)


def parse_args():
    parser = argparse.ArgumentParser(
        description="PeerSphere — 42Berlin eval auto-assigner"
    )
    parser.add_argument("--team-id", type=int, help="Team ID to assign")
    parser.add_argument("--project", type=str, help="Project name (e.g. kfs-2)")
    parser.add_argument(
        "--interval",
        type=int,
        default=POLL_INTERVAL,
        help=f"Seconds between attempts (default: {POLL_INTERVAL})",
    )
    return parser.parse_args()


def main():
    load_env_file()
    args = parse_args()

    # Resolve configuration: CLI args > env vars
    team_id = args.team_id or int(os.environ.get("TEAM_ID", 0))
    project_name = args.project or os.environ.get("PROJECT_NAME", "")
    access_token = os.environ.get("ACCESS_TOKEN", "")
    refresh_token = os.environ.get("REFRESH_TOKEN", "")
    csrf_token = os.environ.get("CSRF_TOKEN", "")
    session_id = os.environ.get("SESSION_ID", "")
    interval = args.interval

    # Validate required values
    missing = []
    # if not team_id:
    #     missing.append("TEAM_ID (or --team-id)")
    # if not project_name:
    #     missing.append("PROJECT_NAME (or --project)")
    if not access_token:
        missing.append("ACCESS_TOKEN")
    if not csrf_token:
        missing.append("CSRF_TOKEN")
    if not session_id:
        missing.append("SESSION_ID")

    if missing:
        log.error("Missing required configuration: %s", ", ".join(missing))
        log.error("Set them in .env or pass via CLI / environment variables.")
        sys.exit(1)

    if not refresh_token:
        log.warning(
            "REFRESH_TOKEN not set — token auto-refresh will not work. "
            "Script will stop if the access token expires."
        )

    # Build session
    session = build_session(access_token, refresh_token, csrf_token, session_id)

    if not project_name or not team_id:
        try:
            team_id, project_name = get_user_info(session, refresh_token)
        except MissingUserData:
            log.error(
                "Can get project name and team id, you must add in .env"
            )
            exit(1)

    log.info("=" * 55)
    log.info("PeerSphere — 42Berlin Eval Auto-Assigner")
    log.info("=" * 55)
    log.info("Team ID     : %s", team_id)
    log.info("Project     : %s", project_name)
    log.info("Interval    : %ds", interval)
    log.info("Press Ctrl+C to stop.")
    log.info("-" * 55)

    attempt = 0
    while running:
        attempt += 1
        log.info("Attempt #%d", attempt)

        if try_assign(session, team_id, project_name, refresh_token, csrf_token, session_id):
            log.info("Done! You got your eval slot. Go crush it.")
            sys.exit(0)

        # Sleep in small increments so Ctrl+C is responsive
        for _ in range(interval):
            if not running:
                break
            time.sleep(1)

    log.info("Stopped after %d attempts.", attempt)


if __name__ == "__main__":
    main()
