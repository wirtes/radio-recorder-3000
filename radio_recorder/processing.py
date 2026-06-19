from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import current_app
from mutagen.id3 import (
    APIC,
    COMM,
    PCST,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TRCK,
    USLT,
    WFED,
    ID3,
    ID3NoHeaderError,
)

from .db import execute, now_iso, query
from .playlist import fetch_playlist


def track_number(when: datetime, frequency: str) -> int:
    if frequency == "daily":
        return when.timetuple().tm_yday
    return when.isocalendar().week


def build_destination(final_root: Path, show_name: str, when: datetime) -> Path:
    safe_name = show_name.replace("/", "-").replace("\\", "-").strip(". ")
    album = f"{safe_name} {when.year}"
    return final_root / safe_name / album


def add_id3_tags(
    mp3_path: Path,
    show_name: str,
    when: datetime,
    frequency: str,
    playlist: str,
    artwork_path: str | None,
) -> None:
    try:
        tags = ID3(mp3_path)
    except ID3NoHeaderError:
        tags = ID3()
    title = f"{when:%Y-%m-%d} {show_name}"
    tags.delall("TIT2")
    tags.delall("TPE1")
    tags.delall("TPE2")
    tags.delall("TALB")
    tags.delall("TRCK")
    tags.delall("TDRC")
    tags.delall("USLT")
    tags.delall("COMM")
    tags.add(TIT2(encoding=3, text=title))
    tags.add(TPE1(encoding=3, text=show_name))
    tags.add(TPE2(encoding=3, text=show_name))
    tags.add(TALB(encoding=3, text=f"{show_name} {when.year}"))
    tags.add(TRCK(encoding=3, text=str(track_number(when, frequency))))
    tags.add(TDRC(encoding=3, text=str(when.year)))
    tags.add(TCON(encoding=3, text="Podcast"))
    tags.add(USLT(encoding=3, lang="eng", desc="Playlist", text=playlist))
    tags.add(COMM(encoding=3, lang="eng", desc="Description", text=playlist))
    tags.add(PCST())
    tags.add(WFED(encoding=3, url="Radio Recorder 3000"))
    if artwork_path and Path(artwork_path).exists():
        tags.delall("APIC")
        mime = mimetypes.guess_type(artwork_path)[0] or "image/jpeg"
        tags.add(APIC(encoding=3, mime=mime, type=3, desc="Front cover", data=Path(artwork_path).read_bytes()))
    tags.save(mp3_path, v2_version=3)


def record_show(app, recording_id: int) -> None:
    with app.app_context():
        row = query(
            """
            SELECT r.*, s.name, s.slug, s.duration_minutes, s.frequency, s.artwork_path,
                   st.stream_url, st.mastodon_url
            FROM recordings r
            JOIN shows s ON s.id = r.show_id
            JOIN stations st ON st.id = s.station_id
            WHERE r.id = ?
            """,
            (recording_id,),
            one=True,
        )
        if not row:
            return
        execute(
            "UPDATE recordings SET status='recording', updated_at=?, error=NULL WHERE id=?",
            (now_iso(), recording_id),
        )
        scheduled = datetime.fromisoformat(row["scheduled_at"])
        local_tz = ZoneInfo(os.environ.get("TZ", "UTC"))
        local_when = scheduled.astimezone(local_tz)
        work_dir = Path(current_app.config["DATA_DIR"]) / "work" / str(recording_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        safe_name = row["name"].replace("/", "-").replace("\\", "-").strip(". ")
        raw_path = work_dir / "capture.mp3"
        mp3_path = work_dir / f"{local_when:%Y-%m-%d} {safe_name}.mp3"
        playlist_path = mp3_path.with_suffix(".txt")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-i", row["stream_url"],
                    "-t", str(row["duration_minutes"] * 60),
                    "-vn", "-c:a", "libmp3lame", "-q:a", "2",
                    str(raw_path),
                ],
                check=True,
                timeout=(row["duration_minutes"] + 5) * 60,
            )
            raw_path.replace(mp3_path)

            lines: list[str] = []
            if row["mastodon_url"]:
                try:
                    lines = fetch_playlist(
                        row["mastodon_url"], scheduled, row["duration_minutes"]
                    )
                except Exception as exc:
                    lines = [f"Playlist retrieval failed: {exc}"]
            playlist = "\n".join(lines)
            playlist_path.write_text(playlist + ("\n" if playlist else ""), encoding="utf-8")
            add_id3_tags(
                mp3_path, row["name"], local_when, row["frequency"],
                playlist, row["artwork_path"],
            )
            execute(
                """
                UPDATE recordings
                SET status='ready', mp3_path=?, playlist_path=?, updated_at=?, next_retry_at=?
                WHERE id=?
                """,
                (str(mp3_path), str(playlist_path), now_iso(), now_iso(), recording_id),
            )
            deliver_recording(recording_id)
        except Exception as exc:
            execute(
                "UPDATE recordings SET status='failed', error=?, updated_at=? WHERE id=?",
                (str(exc), now_iso(), recording_id),
            )


def deliver_recording(recording_id: int) -> bool:
    row = query(
        """
        SELECT r.*, s.name, s.frequency
        FROM recordings r JOIN shows s ON s.id = r.show_id
        WHERE r.id=?
        """,
        (recording_id,),
        one=True,
    )
    if not row or not row["mp3_path"] or not Path(row["mp3_path"]).exists():
        return False
    final_dir_row = query("SELECT value FROM settings WHERE key='final_dir'", one=True)
    final_root = Path(final_dir_row["value"])
    local_tz = ZoneInfo(os.environ.get("TZ", "UTC"))
    when = datetime.fromisoformat(row["scheduled_at"]).astimezone(local_tz)
    destination = build_destination(final_root, row["name"], when)
    try:
        destination.mkdir(parents=True, exist_ok=True)
        for source_string in (row["mp3_path"], row["playlist_path"]):
            source = Path(source_string)
            temporary = destination / f".{source.name}.partial"
            shutil.copy2(source, temporary)
            temporary.replace(destination / source.name)
        work_dir = Path(row["mp3_path"]).parent
        shutil.rmtree(work_dir)
        execute(
            """
            UPDATE recordings SET status='complete', attempts=attempts+1,
                mp3_path=?, playlist_path=?, error=NULL, next_retry_at=NULL, updated_at=?
            WHERE id=?
            """,
            (
                str(destination / Path(row["mp3_path"]).name),
                str(destination / Path(row["playlist_path"]).name),
                now_iso(),
                recording_id,
            ),
        )
        return True
    except Exception as exc:
        attempts = row["attempts"] + 1
        delay_minutes = min(60, 2 ** min(attempts, 6))
        from datetime import timedelta, timezone
        retry = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)
        execute(
            """
            UPDATE recordings SET status='delivery_pending', attempts=?, error=?,
                next_retry_at=?, updated_at=? WHERE id=?
            """,
            (attempts, str(exc), retry.isoformat(), now_iso(), recording_id),
        )
        return False
