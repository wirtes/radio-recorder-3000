from __future__ import annotations

import html
import os
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

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
    text = html.unescape(text)
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    first_line = re.sub(r"^🎶\s*", "", first_line).strip()
    return first_line or None


TIME_PREFIX = re.compile(
    r"^(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<meridiem>[ap]\.?m\.?)(?=\s|$)\s*",
    flags=re.IGNORECASE,
)


def elapsed_playlist_line(
    line: str,
    scheduled_at: datetime,
    *,
    first: bool = False,
) -> str:
    match = TIME_PREFIX.match(line)
    if not match:
        return line

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    meridiem = re.sub(r"[^apm]", "", match.group("meridiem").lower())
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0

    local_tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    show_start = scheduled_at.astimezone(local_tz)
    entry_time = show_start.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if entry_time < show_start - timedelta(hours=12):
        entry_time += timedelta(days=1)

    elapsed_minutes = 0 if first else max(
        0, int((entry_time - show_start).total_seconds() // 60)
    )
    elapsed = f"{elapsed_minutes // 60}:{elapsed_minutes % 60:02d}"
    return f"{elapsed} {line[match.end():].strip()}".rstrip()


def format_playlist(lines: list[str], scheduled_at: datetime) -> list[str]:
    return [
        elapsed_playlist_line(line, scheduled_at, first=index == 0)
        for index, line in enumerate(lines)
    ]


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

    statuses_url = f"{base_url}/api/v1/accounts/{account['id']}/statuses"
    base_params = {"exclude_replies": "true", "exclude_reblogs": "true"}
    statuses: list[dict] = []
    seen_ids: set[str] = set()
    max_id: str | None = None
    limit = 40
    show_start = scheduled_at.astimezone(timezone.utc)

    while True:
        params = {**base_params, "limit": limit}
        if max_id is not None:
            params["max_id"] = max_id
        status_response = session.get(statuses_url, params=params, timeout=20)
        status_response.raise_for_status()
        batch = status_response.json()
        if not batch:
            break

        new_statuses = [
            status for status in batch
            if str(status.get("id")) not in seen_ids
        ]
        if not new_statuses:
            break
        for status in new_statuses:
            seen_ids.add(str(status.get("id")))
        statuses.extend(new_statuses)

        dated_statuses = [
            status for status in new_statuses if status.get("created_at")
        ]
        if not dated_statuses:
            break
        oldest = min(
            dated_statuses,
            key=lambda status: datetime.fromisoformat(
                status["created_at"].replace("Z", "+00:00")
            ),
        )
        oldest_created = datetime.fromisoformat(
            oldest["created_at"].replace("Z", "+00:00")
        )
        if oldest_created <= show_start:
            break

        max_id = str(oldest["id"])
        limit = 20

    start = show_start - timedelta(minutes=2)
    end = show_start + timedelta(minutes=duration_minutes + 5)
    lines: list[tuple[datetime, str]] = []
    for status in statuses:
        if str(status.get("account", {}).get("id")) != str(account["id"]):
            continue
        created = datetime.fromisoformat(status["created_at"].replace("Z", "+00:00"))
        if start <= created <= end:
            line = clean_status_line(status.get("content", ""))
            if line:
                lines.append((created, line))
    return format_playlist([line for _, line in sorted(lines)], scheduled_at)
