from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from io import BytesIO

from PIL import Image

from radio_recorder import create_app
from radio_recorder.db import execute, now_iso
from radio_recorder.playlist import (
    clean_status_line,
    fetch_playlist,
    format_playlist,
    parse_account_url,
)
from radio_recorder.processing import add_id3_tags, build_destination, track_number
from radio_recorder.scheduler import runs_on_weekday
from mutagen.id3 import ID3


def make_app(tmp_path):
    return create_app({
        "TESTING": True,
        "START_SCHEDULER": False,
        "SECRET_KEY": "test",
        "DATA_DIR": str(tmp_path / "data"),
        "FINAL_DIR": str(tmp_path / "final"),
    })


def test_station_and_show_configuration(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    response = client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/stream",
        "mastodon_url": "https://mastodon.test/@kvcu",
    })
    assert response.status_code == 302

    image = Image.new("RGB", (4, 4), "red")
    image_bytes = BytesIO()
    image.save(image_bytes, "JPEG")
    image_bytes.seek(0)
    response = client.post("/shows", data={
        "station_id": "1",
        "name": "Test Show",
        "duration_minutes": "60",
        "frequency": "weekly",
        "weekday": "3",
        "start_time": "10:00",
        "artwork": (image_bytes, "cover.jpg"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    assert response.headers["Location"] == "/"
    page = client.get("/")
    assert b"Test Show" in page.data
    assert b"KVCU" in page.data
    assert b"Every Thursday at 10:00am" in page.data
    assert client.get("/shows/1/artwork").status_code == 200


def test_station_logo_and_show_editing(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    logo = Image.new("RGB", (8, 8), "blue")
    logo_bytes = BytesIO()
    logo.save(logo_bytes, "PNG")
    logo_bytes.seek(0)
    response = client.post("/stations", data={
        "station_id": "WXYZ",
        "stream_url": "https://example.test/live",
        "mastodon_url": "",
        "logo": (logo_bytes, "station.png"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    assert client.get("/stations/1/logo").status_code == 200
    replacement = Image.new("RGB", (8, 8), "green")
    replacement_bytes = BytesIO()
    replacement.save(replacement_bytes, "JPEG")
    replacement_bytes.seek(0)
    response = client.post("/stations/1/logo", data={
        "logo": (replacement_bytes, "replacement.jpg"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    assert client.get("/stations/1/logo").mimetype == "image/jpeg"

    edit_station_page = client.get("/config/stations?edit_station=1")
    assert b"Editing WXYZ" in edit_station_page.data
    assert b'value="https://example.test/live"' in edit_station_page.data
    response = client.post("/stations/1/update", data={
        "station_id": "WXYZ-FM",
        "stream_url": "https://example.test/updated",
        "mastodon_url": "https://mastodon.test/@wxyz",
    })
    assert response.status_code == 302
    station_page = client.get("/config/stations")
    assert b"WXYZ-FM" in station_page.data
    assert b"https://example.test/updated" in station_page.data
    assert b'data-delete-type="station"' in station_page.data

    client.post("/shows", data={
        "station_id": "1",
        "name": "Old Name",
        "duration_minutes": "30",
        "frequency": "daily",
        "start_time": "07:40",
    })
    edit_page = client.get("/?edit_show=1")
    assert b"Editing Old Name" in edit_page.data
    assert b"Slug" not in edit_page.data

    response = client.post("/shows/1/update", data={
        "station_id": "1",
        "name": "New Name",
        "duration_minutes": "45",
        "frequency": "weekly",
        "weekday": "4",
        "start_time": "08:15",
    })
    assert response.status_code == 302
    page = client.get("/")
    assert b"New Name" in page.data
    assert b"Every Friday at 8:15am" in page.data
    assert b"/stations/1/logo" in page.data

    response = client.post("/shows", data={
        "station_id": "1",
        "name": "Weekday Show",
        "duration_minutes": "60",
        "frequency": "weekdays",
        "start_time": "07:30",
    })
    assert response.status_code == 302
    assert b"Every Monday" in client.get("/").data
    assert b'data-delete-type="show"' in client.get("/").data


def test_playlist_cleanup():
    content = (
        "🎶 10:00am Let&#39;s Dance by Belle &amp; Sebastian"
        "<br>1190 Mixtape<br>#Radio"
    )
    assert clean_status_line(content) == (
        "10:00am Let's Dance by Belle & Sebastian"
    )
    assert parse_account_url("https://example.social/@radio") == (
        "https://example.social", "radio"
    )


def test_playlist_elapsed_times(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    scheduled = datetime(2026, 6, 19, 7, 40, tzinfo=timezone.utc)
    assert format_playlist([
        "7:45am First Song",
        "8:00am Second Song",
        "7:30am Before Start",
    ], scheduled) == [
        "0:00 First Song",
        "0:20 Second Song",
        "0:00 Before Start",
    ]


def test_mastodon_window_and_older_post_paging(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    scheduled = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.status_calls = []

        def get(self, url, params, timeout):
            if url.endswith("/accounts/lookup"):
                return FakeResponse({"id": "station-account"})
            self.status_calls.append(params.copy())
            if len(self.status_calls) == 1:
                return FakeResponse([
                    {
                        "id": "500",
                        "created_at": "2026-06-19T11:06:00Z",
                        "content": "11:06am Too Late",
                        "account": {"id": "station-account"},
                    },
                    {
                        "id": "400",
                        "created_at": "2026-06-19T10:30:00Z",
                        "content": "10:30am Middle",
                        "account": {"id": "station-account"},
                    },
                ])
            if len(self.status_calls) == 2:
                return FakeResponse([
                    {
                        "id": "300",
                        "created_at": "2026-06-19T10:10:00Z",
                        "content": "10:10am Early",
                        "account": {"id": "station-account"},
                    },
                ])
            return FakeResponse([
                {
                    "id": "200",
                    "created_at": "2026-06-19T10:00:00Z",
                    "content": "10:00am Start",
                    "account": {"id": "station-account"},
                },
                {
                    "id": "190",
                    "created_at": "2026-06-19T09:58:00Z",
                    "content": "9:58am Boundary",
                    "account": {"id": "station-account"},
                },
                {
                    "id": "180",
                    "created_at": "2026-06-19T09:57:00Z",
                    "content": "9:57am Too Early",
                    "account": {"id": "station-account"},
                },
                {
                    "id": "170",
                    "created_at": "2026-06-19T11:05:00Z",
                    "content": "11:05am End Boundary",
                    "account": {"id": "station-account"},
                },
            ])

    fake_session = FakeSession()
    monkeypatch.setattr(
        "radio_recorder.playlist.requests.Session", lambda: fake_session
    )

    playlist = fetch_playlist(
        "https://mastodon.test/@radio", scheduled, duration_minutes=60
    )

    assert fake_session.status_calls == [
        {
            "exclude_replies": "true",
            "exclude_reblogs": "true",
            "limit": 40,
        },
        {
            "exclude_replies": "true",
            "exclude_reblogs": "true",
            "limit": 20,
            "max_id": "400",
        },
        {
            "exclude_replies": "true",
            "exclude_reblogs": "true",
            "limit": 20,
            "max_id": "300",
        },
    ]
    assert playlist == [
        "0:00 Start",
        "0:10 Early",
        "0:30 Middle",
        "1:05 End Boundary",
    ]


def test_track_number_and_destination(tmp_path):
    when = datetime(2026, 6, 18, 10, 0)
    assert track_number(when, "daily") == 169
    assert track_number(when, "weekdays") == 169
    assert track_number(when, "weekly") == 25
    assert build_destination(tmp_path, "Show Name", when) == (
        tmp_path / "Show Name" / "Show Name 2026"
    )


def test_id3_metadata(tmp_path):
    path = tmp_path / "recording.mp3"
    path.touch()
    when = datetime(2026, 6, 18, 10, 0)
    add_id3_tags(path, "Show Name", when, "weekly", "10:00am A Song", None)
    tags = ID3(path)
    assert str(tags["TIT2"]) == "2026-06-18 Show Name"
    assert str(tags["TALB"]) == "Show Name 2026"
    assert str(tags["TRCK"]) == "25"
    assert str(tags["TDRC"]) == "2026"
    assert tags.getall("PCST")


def test_weekday_frequency():
    assert runs_on_weekday("weekdays", None, 0)
    assert runs_on_weekday("weekdays", None, 4)
    assert not runs_on_weekday("weekdays", None, 5)
    assert runs_on_weekday("weekly", 4, 4)
    assert not runs_on_weekday("weekly", 4, 3)
    assert runs_on_weekday("daily", None, 6)


def test_paginated_lists_and_recording_time_format(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "America/Denver")
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "LONG-STATION-NAME",
        "stream_url": "https://example.test/live",
    })
    with app.app_context():
        for index in range(30):
            show_id = execute(
                """
                INSERT INTO shows(
                    station_id, name, duration_minutes, frequency,
                    start_time, weekday, enabled, created_at
                ) VALUES(1, ?, 60, 'daily', '10:00', NULL, 1, ?)
                """,
                (f"Show {index:02d}", now_iso()),
            )
            execute(
                """
                INSERT INTO recordings(
                    show_id, scheduled_at, status, attempts, created_at, updated_at
                ) VALUES(?, '2026-06-19T19:00:00+00:00', 'complete', 1, ?, ?)
                """,
                (show_id, now_iso(), now_iso()),
            )

    first_page = client.get("/")
    assert first_page.data.count(b'class="show-main"') == 30
    assert b"Show schedule" in first_page.data
    assert b'id="recording-log"' not in first_page.data
    assert b"Page 1 of" not in first_page.data
    assert b"LONG-STATION-NAME" in first_page.data

    recording_page = client.get(
        "/?tab=recordings&recordings_page=1"
    )
    assert b'id="show-schedule-list"' not in recording_page.data
    assert recording_page.data.count(b"<tr><td>Show") == 25
    assert b"2026-06-19 1:00pm" in recording_page.data
    assert b'<option value="25" selected>' in recording_page.data

    second_page = client.get(
        "/?tab=recordings&recordings_page=2&recordings_per_page=25"
    )
    assert second_page.data.count(b"<tr><td>Show") == 5

    expanded = client.get("/?tab=recordings&recordings_per_page=100")
    assert expanded.data.count(b"<tr><td>Show") == 30
    assert b'<option value="100" selected>' in expanded.data


def test_navigation_and_new_defaults(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/live",
    })
    dashboard = client.get("/")
    assert b'value="62"' in dashboard.data
    assert b'name="start_time" type="time" required value=""' in dashboard.data
    assert b"Select a weekday" in dashboard.data
    assert b"CONTROL ROOM" not in dashboard.data
    assert b"Catch the signal" not in dashboard.data
    assert b'class="meters"' not in dashboard.data
    assert b'href="/config/stations"' in dashboard.data
    assert b'href="/config/storage"' in dashboard.data
    assert b">01<" not in dashboard.data
    assert b">02<" not in dashboard.data
    assert b">03<" not in dashboard.data
    assert b">04<" not in dashboard.data
    assert client.get("/config/stations").status_code == 200
    assert client.get("/config/storage").status_code == 200


def test_shows_order_by_schedule(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/live",
    })
    with app.app_context():
        created = now_iso()
        rows = [
            ("Friday Late", "weekly", "14:00", 4),
            ("Tuesday Early", "weekly", "08:00", 1),
            ("Friday Early", "weekly", "09:00", 4),
            ("Sunday Show", "weekly", "12:00", 6),
            ("Daily Late", "daily", "11:00", None),
            ("Weekday Early", "weekdays", "07:00", None),
        ]
        for name, frequency, start_time, weekday in rows:
            execute(
                """
                INSERT INTO shows(
                    station_id, name, duration_minutes, frequency,
                    start_time, weekday, enabled, created_at
                ) VALUES(1, ?, 62, ?, ?, ?, 1, ?)
                """,
                (name, frequency, start_time, weekday, created),
            )

    page = client.get("/").data
    names = [
        b"Weekday Early", b"Daily Late", b"Sunday Show", b"Tuesday Early",
        b"Friday Early", b"Friday Late",
    ]
    positions = [page.index(name) for name in names]
    assert positions == sorted(positions)


def test_show_list_filters_by_station(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/kvcu",
    })
    client.post("/stations", data={
        "station_id": "WXYZ",
        "stream_url": "https://example.test/wxyz",
    })
    with app.app_context():
        created = now_iso()
        execute(
            """
            INSERT INTO shows(
                station_id, name, duration_minutes, frequency,
                start_time, weekday, enabled, created_at
            ) VALUES(1, 'KVCU Show', 62, 'daily', '09:00', NULL, 1, ?)
            """,
            (created,),
        )
        execute(
            """
            INSERT INTO shows(
                station_id, name, duration_minutes, frequency,
                start_time, weekday, enabled, created_at
            ) VALUES(2, 'WXYZ Show', 62, 'daily', '10:00', NULL, 1, ?)
            """,
            (created,),
        )

    all_stations = client.get("/")
    assert b"KVCU Show" in all_stations.data
    assert b"WXYZ Show" in all_stations.data

    filtered = client.get("/?station=1")
    assert b"KVCU Show" in filtered.data
    assert b"WXYZ Show" not in filtered.data
    assert b'<option value="1" selected>' in filtered.data


def test_legacy_show_schema_migrates_without_slug(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    database = data_dir / "legacy.sqlite3"
    db = sqlite3.connect(database)
    db.executescript(
        """
        CREATE TABLE stations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id TEXT NOT NULL UNIQUE,
            stream_url TEXT NOT NULL,
            mastodon_url TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE shows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            station_id INTEGER NOT NULL REFERENCES stations(id),
            slug TEXT NOT NULL UNIQUE,
            name TEXT NOT NULL,
            duration_minutes INTEGER NOT NULL,
            artwork_path TEXT,
            frequency TEXT NOT NULL CHECK(frequency IN ('daily', 'weekly')),
            start_time TEXT NOT NULL,
            weekday INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
        INSERT INTO stations(station_id, stream_url, created_at)
        VALUES('KVCU', 'https://example.test/live', 'now');
        INSERT INTO shows(
            station_id, slug, name, duration_minutes, frequency,
            start_time, weekday, enabled, created_at
        ) VALUES(1, 'legacy-show', 'Legacy Show', 60, 'weekly', '13:00', 4, 1, 'now');
        """
    )
    db.close()

    app = create_app({
        "TESTING": True,
        "START_SCHEDULER": False,
        "DATA_DIR": str(data_dir),
        "DATABASE": str(database),
        "FINAL_DIR": str(tmp_path / "final"),
    })
    with app.app_context():
        db = sqlite3.connect(database)
        columns = [row[1] for row in db.execute("PRAGMA table_info(shows)")]
        show = db.execute("SELECT name, frequency FROM shows").fetchone()
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='shows'"
        ).fetchone()[0]
        foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        db.close()
    assert "slug" not in columns
    assert show == ("Legacy Show", "weekly")
    assert "'weekdays'" in table_sql
    assert foreign_key_errors == []
