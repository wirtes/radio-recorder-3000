from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from threading import Thread

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from PIL import Image
from werkzeug.utils import secure_filename

from .db import execute, now_iso, query
from .processing import record_show

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    return render_template(
        "index.html",
        stations=query("SELECT * FROM stations ORDER BY station_id"),
        shows=query(
            """
            SELECT s.*, st.station_id AS station_code
            FROM shows s JOIN stations st ON st.id=s.station_id
            ORDER BY s.name
            """
        ),
        recordings=query(
            """
            SELECT r.*, s.name AS show_name FROM recordings r
            JOIN shows s ON s.id=r.show_id ORDER BY r.created_at DESC LIMIT 30
            """
        ),
        final_dir=query("SELECT value FROM settings WHERE key='final_dir'", one=True)["value"],
    )


@bp.post("/stations")
def create_station():
    try:
        execute(
            "INSERT INTO stations(station_id, stream_url, mastodon_url, created_at) VALUES(?,?,?,?)",
            (
                request.form["station_id"].strip(),
                request.form["stream_url"].strip(),
                request.form.get("mastodon_url", "").strip() or None,
                now_iso(),
            ),
        )
        flash("Station added.", "success")
    except Exception as exc:
        flash(f"Could not add station: {exc}", "error")
    return redirect(url_for("main.index"))


@bp.post("/stations/<int:station_id>/delete")
def delete_station(station_id: int):
    try:
        execute("DELETE FROM stations WHERE id=?", (station_id,))
        flash("Station deleted.", "success")
    except Exception as exc:
        flash(f"Could not delete station: {exc}", "error")
    return redirect(url_for("main.index"))


def save_artwork(file, slug: str) -> str | None:
    if not file or not file.filename:
        return None
    filename = secure_filename(file.filename)
    if Path(filename).suffix.lower() not in {".jpg", ".jpeg"}:
        raise ValueError("Artwork must be a JPG file.")
    destination = Path(current_app.config["DATA_DIR"]) / "artwork" / f"{slug}.jpg"
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


@bp.post("/shows")
def create_show():
    try:
        slug = request.form["slug"].strip()
        name = request.form["name"].strip()
        if "/" in name or "\\" in name or name in {".", ".."}:
            raise ValueError("Show name cannot contain path separators.")
        frequency = request.form.get("frequency", "weekly")
        weekday = int(request.form["weekday"]) if frequency == "weekly" else None
        artwork_path = save_artwork(request.files.get("artwork"), slug)
        execute(
            """
            INSERT INTO shows(
                station_id, slug, name, duration_minutes, artwork_path,
                frequency, start_time, weekday, enabled, created_at
            ) VALUES(?,?,?,?,?,?,?,?,1,?)
            """,
            (
                int(request.form["station_id"]),
                slug,
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


@bp.post("/shows/<int:show_id>/toggle")
def toggle_show(show_id: int):
    execute("UPDATE shows SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?", (show_id,))
    return redirect(url_for("main.index"))


@bp.post("/shows/<int:show_id>/delete")
def delete_show(show_id: int):
    execute("DELETE FROM shows WHERE id=?", (show_id,))
    flash("Show deleted.", "success")
    return redirect(url_for("main.index"))


@bp.post("/shows/<int:show_id>/record")
def record_now(show_id: int):
    recording_id = execute(
        """
        INSERT INTO recordings(show_id, scheduled_at, status, created_at, updated_at)
        VALUES(?, ?, 'queued', ?, ?)
        """,
        (show_id, datetime.now(timezone.utc).isoformat(), now_iso(), now_iso()),
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
        flash("Final storage location updated.", "success")
    return redirect(url_for("main.index"))
