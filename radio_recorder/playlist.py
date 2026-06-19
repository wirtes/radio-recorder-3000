from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests


def parse_account_url(account_url: str) -> tuple[str, str]:
    parsed = urlparse(account_url)
    username = parsed.path.strip("/").split("/")[0].lstrip("@")
    if not parsed.scheme or not parsed.netloc or not username:
        raise ValueError("Mastodon account URL must look like https://server/@account")
    return f"{parsed.scheme}://{parsed.netloc}", username


def clean_status_line(content: str) -> str | None:
    text = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = re.sub(r"^🎶\s*", "", first_line).strip()
    return first_line or None


def fetch_playlist(account_url: str, scheduled_at: datetime, duration_minutes: int) -> list[str]:
    base_url, username = parse_account_url(account_url)
    session = requests.Session()
    session.headers["User-Agent"] = "RadioRecorder3000/1.0"

    account_response = session.get(
        f"{base_url}/api/v1/accounts/lookup",
        params={"acct": username},
        timeout=20,
    )
    account_response.raise_for_status()
    account = account_response.json()

    status_response = session.get(
        f"{base_url}/api/v1/accounts/{account['id']}/statuses",
        params={"exclude_replies": "true", "exclude_reblogs": "true", "limit": 40},
        timeout=20,
    )
    status_response.raise_for_status()

    start = scheduled_at.astimezone(timezone.utc) - timedelta(minutes=10)
    end = scheduled_at.astimezone(timezone.utc) + timedelta(minutes=duration_minutes + 30)
    lines: list[tuple[datetime, str]] = []
    for status in status_response.json():
        if str(status.get("account", {}).get("id")) != str(account["id"]):
            continue
        created = datetime.fromisoformat(status["created_at"].replace("Z", "+00:00"))
        if start <= created <= end:
            line = clean_status_line(status.get("content", ""))
            if line:
                lines.append((created, line))
    return [line for _, line in sorted(lines)]

