from __future__ import annotations

import os
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread
from uuid import uuid4
from zoneinfo import ZoneInfo

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from PIL import Image
from werkzeug.utils import secure_filename

from .db import execute, now_iso, query
from .processing import record_show

bp = Blueprint("main", __name__)
PAGE_SIZES = (10, 25, 100)


def current_local_time() -> datetime:
    return datetime.now(ZoneInfo(os.environ.get("TZ", "UTC")))


def next_show_start(show, now_local: datetime) -> tuple[datetime, bool]:
    hour, minute = map(int, show["start_time"].split(":"))
    duration = timedelta(minutes=show["duration_minutes"])
    for offset in range(8):
        candidate = (now_local + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        )
        weekday = candidate.weekday()
        runs_today = (
            show["frequency"] == "daily"
            or (show["frequency"] == "weekdays" and weekday < 5)
            or (show["frequency"] == "weekly" and show["weekday"] == weekday)
        )
        if not runs_today:
            continue
        if candidate <= now_local < candidate + duration:
            return candidate, True
        if candidate > now_local:
            return candidate, False
    raise ValueError(f"Could not calculate next run for show {show['id']}")


def pagination_params(prefix: str, total: int, default_per_page: int = 10) -> dict:
    per_page = request.args.get(
        f"{prefix}_per_page", default=default_per_page, type=int
    )
    if per_page not in PAGE_SIZES:
        per_page = default_per_page
    pages = max(1, math.ceil(total / per_page))
    page = request.args.get(f"{prefix}_page", default=1, type=int)
    page = min(max(page, 1), pages)
    return {
        "page": page,
        "per_page": per_page,
        "pages": pages,
        "total": total,
        "offset": (page - 1) * per_page,
    }


@bp.get("/recording-status")
def recording_status():
    rows = query(
        """
        SELECT s.name
        FROM recordings r
        JOIN shows s ON s.id = r.show_id
        WHERE r.status = 'recording'
        ORDER BY r.created_at
        """
    )
    return jsonify(
        recording=bool(rows),
        shows=[row["name"] for row in rows],
    )


@bp.get("/")
def index():
    active_tab = request.args.get("tab", "shows")
    if active_tab not in {"shows", "recordings"}:
        active_tab = "shows"
    edit_show = None
    edit_show_id = request.args.get("edit_show", type=int)
    if edit_show_id:
        edit_show = query("SELECT * FROM shows WHERE id=?", (edit_show_id,), one=True)
    station_filter = request.args.get("station", type=int)
    highlight_show = request.args.get("highlight_show", type=int)
    show_params: tuple = ()
    show_where = ""
    if station_filter:
        show_where = "WHERE s.station_id = ?"
        show_params = (station_filter,)
    shows = [
        dict(row) for row in query(
            f"""
            SELECT s.*, st.station_id AS station_code, st.logo_path AS station_logo_path
            FROM shows s JOIN stations st ON st.id=s.station_id
            {show_where}
            """,
            show_params,
        )
    ]
    active_recording_ids = {
        row["show_id"]
        for row in query(
            "SELECT DISTINCT show_id FROM recordings WHERE status IN ('queued', 'recording')"
        )
    }
    now_local = current_local_time()
    for show in shows:
        show["schedule_description"] = schedule_description(show)
        show["next_start"], show["is_current"] = next_show_start(show, now_local)
    shows.sort(
        key=lambda show: (
            0
            if show["id"] in active_recording_ids
            or (show["enabled"] and show["is_current"])
            else 1
            if show["enabled"]
            else 2,
            show["next_start"],
            show["name"].casefold(),
        )
    )

    recording_total = query(
        "SELECT COUNT(*) AS count FROM recordings", one=True
    )["count"]
    recording_pagination = pagination_params(
        "recordings", recording_total, default_per_page=25
    )
    recordings = [
        dict(row) for row in query(
            """
            SELECT r.*, s.name AS show_name FROM recordings r
            JOIN shows s ON s.id=r.show_id
            ORDER BY r.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (recording_pagination["per_page"], recording_pagination["offset"]),
        )
    ]
    for recording in recordings:
        recording["scheduled_display"] = display_scheduled_at(
            recording["scheduled_at"]
        )

    return render_template(
        "index.html",
        stations=query("SELECT * FROM stations ORDER BY station_id"),
        shows=shows,
        recordings=recordings,
        recording_pagination=recording_pagination,
        page_sizes=PAGE_SIZES,
        active_tab=active_tab,
        station_filter=station_filter,
        highlight_show=highlight_show,
        edit_show=edit_show,
    )


@bp.get("/config/stations")
def station_config():
    edit_station = None
    edit_station_id = request.args.get("edit_station", type=int)
    if edit_station_id:
        edit_station = query(
            "SELECT * FROM stations WHERE id=?", (edit_station_id,), one=True
        )
    return render_template(
        "stations.html",
        stations=query("SELECT * FROM stations ORDER BY station_id"),
        edit_station=edit_station,
    )


@bp.get("/config/storage")
def storage_config():
    return render_template(
        "storage.html",
        final_dir=query(
            "SELECT value FROM settings WHERE key='final_dir'", one=True
        )["value"],
    )


def display_time(value: str) -> str:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.strftime("%I:%M%p").lstrip("0").lower()


def display_scheduled_at(value: str) -> str:
    scheduled = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if scheduled.tzinfo is None:
        scheduled = scheduled.replace(tzinfo=timezone.utc)
    local = scheduled.astimezone(ZoneInfo(os.environ.get("TZ", "UTC")))
    return f"{local:%Y-%m-%d} {local.strftime('%I:%M%p').lstrip('0').lower()}"


def schedule_description(show) -> str:
    time_text = display_time(show["start_time"])
    minutes = show["duration_minutes"]
    if show["frequency"] == "weekly":
        day = [
            "Monday", "Tuesday", "Wednesday", "Thursday",
            "Friday", "Saturday", "Sunday",
        ][show["weekday"]]
        cadence = f"Every {day}"
    elif show["frequency"] == "weekdays":
        cadence = "Every Monday–Friday"
    else:
        cadence = "Every day"
    return f"{cadence} at {time_text} • {minutes} minutes"


def schedule_fields(form) -> tuple[str, int | None]:
    frequency = form.get("frequency", "weekly")
    weekday_value = form.get("weekday")
    if frequency == "weekly" and weekday_value == "weekdays":
        return "weekdays", None
    weekday = int(weekday_value) if frequency == "weekly" else None
    return frequency, weekday


@bp.post("/stations")
def create_station():
    try:
        station_code = request.form["station_id"].strip()
        logo_path = save_station_logo(request.files.get("logo"), station_code)
        execute(
            """
            INSERT INTO stations(
                station_id, stream_url, mastodon_url, logo_path, created_at
            ) VALUES(?,?,?,?,?)
            """,
            (
                station_code,
                request.form["stream_url"].strip(),
                request.form.get("mastodon_url", "").strip() or None,
                logo_path,
                now_iso(),
            ),
        )
        flash("Station added.", "success")
    except Exception as exc:
        flash(f"Could not add station: {exc}", "error")
    return redirect(url_for("main.station_config"))


@bp.post("/stations/<int:station_id>/update")
def update_station(station_id: int):
    station = query("SELECT * FROM stations WHERE id=?", (station_id,), one=True)
    if not station:
        flash("Station not found.", "error")
        return redirect(url_for("main.station_config"))
    try:
        station_code = request.form["station_id"].strip()
        logo_path = save_station_logo(request.files.get("logo"), station_code)
        if logo_path is None:
            logo_path = station["logo_path"]
        execute(
            """
            UPDATE stations
            SET station_id=?, stream_url=?, mastodon_url=?, logo_path=?
            WHERE id=?
            """,
            (
                station_code,
                request.form["stream_url"].strip(),
                request.form.get("mastodon_url", "").strip() or None,
                logo_path,
                station_id,
            ),
        )
        old_logo = station["logo_path"]
        if old_logo and logo_path != old_logo:
            Path(old_logo).unlink(missing_ok=True)
        flash("Station updated.", "success")
        return redirect(url_for("main.station_config"))
    except Exception as exc:
        flash(f"Could not update station: {exc}", "error")
        return redirect(url_for("main.station_config", edit_station=station_id))


def save_station_logo(file, station_code: str) -> str | None:
    if not file or not file.filename:
        return None
    filename = secure_filename(file.filename)
    suffix = Path(filename).suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        raise ValueError("Station logo must be a JPG, PNG, or WebP image.")
    safe_code = secure_filename(station_code) or "station"
    destination = (
        Path(current_app.config["DATA_DIR"]) / "station-logos" / f"{safe_code}{suffix}"
    )
    file.save(destination)
    try:
        with Image.open(destination) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError("Station logo is not a valid JPG, PNG, or WebP image.")
            image.verify()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return str(destination)


@bp.get("/stations/<int:station_id>/logo")
def station_logo(station_id: int):
    station = query("SELECT logo_path FROM stations WHERE id=?", (station_id,), one=True)
    if not station or not station["logo_path"] or not Path(station["logo_path"]).is_file():
        return "", 404
    return send_file(station["logo_path"], conditional=True)


@bp.post("/stations/<int:station_id>/logo")
def update_station_logo(station_id: int):
    station = query("SELECT * FROM stations WHERE id=?", (station_id,), one=True)
    if not station:
        flash("Station not found.", "error")
        return redirect(url_for("main.station_config"))
    try:
        logo_path = save_station_logo(request.files.get("logo"), station["station_id"])
        if not logo_path:
            raise ValueError("Choose a logo file to upload.")
        old_logo = station["logo_path"]
        execute("UPDATE stations SET logo_path=? WHERE id=?", (logo_path, station_id))
        if old_logo and old_logo != logo_path:
            Path(old_logo).unlink(missing_ok=True)
        flash(f"{station['station_id']} logo updated.", "success")
    except Exception as exc:
        flash(f"Could not update station logo: {exc}", "error")
    return redirect(url_for("main.station_config"))


@bp.post("/stations/<int:station_id>/delete")
def delete_station(station_id: int):
    try:
        execute("DELETE FROM stations WHERE id=?", (station_id,))
        flash("Station deleted.", "success")
    except Exception as exc:
        flash(f"Could not delete station: {exc}", "error")
    return redirect(url_for("main.station_config"))


def save_artwork(file) -> str | None:
    if not file or not file.filename:
        return None
    filename = secure_filename(file.filename)
    if Path(filename).suffix.lower() not in {".jpg", ".jpeg"}:
        raise ValueError("Artwork must be a JPG file.")
    destination = (
        Path(current_app.config["DATA_DIR"]) / "artwork" / f"{uuid4().hex}.jpg"
    )
    file.save(destination)
    try:
        with Image.open(destination) as image:
            if image.format != "JPEG":
                raise ValueError("Artwork content is not a valid JPG.")
            image.verify()
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return str(destination)


@bp.get("/shows/<int:show_id>/artwork")
def show_artwork(show_id: int):
    show = query("SELECT artwork_path FROM shows WHERE id=?", (show_id,), one=True)
    if not show or not show["artwork_path"] or not Path(show["artwork_path"]).is_file():
        return "", 404
    return send_file(show["artwork_path"], conditional=True)


@bp.post("/shows")
def create_show():
    try:
        name = request.form["name"].strip()
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("Show name cannot contain path separators.")
        frequency, weekday = schedule_fields(request.form)
        artwork_path = save_artwork(request.files.get("artwork"))
        execute(
            """
            INSERT INTO shows(
                station_id, name, duration_minutes, artwork_path,
                frequency, start_time, weekday, enabled, created_at
            ) VALUES(?,?,?,?,?,?,?,1,?)
            """,
            (
                int(request.form["station_id"]),
                name,
                int(request.form["duration_minutes"]),
                artwork_path,
                frequency,
                request.form["start_time"],
                weekday,
                now_iso(),
            ),
        )
        flash("Show scheduled.", "success")
    except Exception as exc:
        flash(f"Could not add show: {exc}", "error")
    return redirect(url_for("main.index"))


@bp.post("/shows/<int:show_id>/update")
def update_show(show_id: int):
    show = query("SELECT * FROM shows WHERE id=?", (show_id,), one=True)
    if not show:
        flash("Show not found.", "error")
        return redirect(url_for("main.index"))
    try:
        name = request.form["name"].strip()
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("Show name cannot contain path separators.")
        frequency, weekday = schedule_fields(request.form)
        artwork_path = save_artwork(request.files.get("artwork"))
        if artwork_path is None:
            artwork_path = show["artwork_path"]
        execute(
            """
            UPDATE shows SET station_id=?, name=?, duration_minutes=?,
                artwork_path=?, frequency=?, start_time=?, weekday=?
            WHERE id=?
            """,
            (
                int(request.form["station_id"]),
                name,
                int(request.form["duration_minutes"]),
                artwork_path,
                frequency,
                request.form["start_time"],
                weekday,
                show_id,
            ),
        )
        flash("Show updated.", "success")
        return redirect(url_for("main.index"))
    except Exception as exc:
        flash(f"Could not update show: {exc}", "error")
        return redirect(url_for("main.index", edit_show=show_id))


@bp.post("/shows/<int:show_id>/toggle")
def toggle_show(show_id: int):
    show = query("SELECT name, enabled FROM shows WHERE id=?", (show_id,), one=True)
    if not show:
        flash("Show not found.", "error")
        return redirect(url_for("main.index"))
    enabled = 0 if show["enabled"] else 1
    execute("UPDATE shows SET enabled=? WHERE id=?", (enabled, show_id))
    flash(
        f"{show['name']} {'unpaused' if enabled else 'paused'}.",
        "success",
    )
    station_filter = request.args.get("station", type=int)
    return redirect(
        url_for(
            "main.index",
            station=station_filter,
            highlight_show=show_id,
        )
    )


@bp.post("/shows/<int:show_id>/delete")
def delete_show(show_id: int):
    execute("DELETE FROM shows WHERE id=?", (show_id,))
    flash("Show deleted.", "success")
    return redirect(url_for("main.index"))


@bp.post("/shows/<int:show_id>/record")
def record_now(show_id: int):
    try:
        duration_minutes = int(request.form["duration_minutes"])
        if duration_minutes < 1:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        flash("Recording duration must be at least one minute.", "error")
        return redirect(url_for("main.index"))
    recording_id = execute(
        """
        INSERT INTO recordings(
            show_id, scheduled_at, status, duration_minutes, created_at, updated_at
        ) VALUES(?, ?, 'queued', ?, ?, ?)
        """,
        (
            show_id,
            datetime.now(timezone.utc).isoformat(),
            duration_minutes,
            now_iso(),
            now_iso(),
        ),
    )
    Thread(target=record_show, args=(current_app._get_current_object(), recording_id), daemon=True).start()
    flash("Recording started.", "success")
    return redirect(url_for("main.index"))


@bp.post("/settings")
def update_settings():
    final_dir = request.form["final_dir"].strip()
    if not final_dir:
        flash("Final directory cannot be empty.", "error")
    else:
        execute("UPDATE settings SET value=? WHERE key='final_dir'", (final_dir,))
        flash("Archive location updated.", "success")
    return redirect(url_for("main.storage_config"))
