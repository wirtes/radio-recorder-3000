from __future__ import annotations

import sqlite3
import subprocess
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image

from radio_recorder import create_app
from radio_recorder.db import execute, now_iso, query
from radio_recorder.playlist import (
    clean_status_line,
    fetch_playlist,
    format_playlist,
    parse_account_url,
)
from radio_recorder.processing import (
    add_id3_tags,
    build_destination,
    capture_stream,
    track_number,
)
from radio_recorder.processing import deliver_recording
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
    assert b"Test Show <span>(KVCU)</span>" in page.data
    assert b"Every Thursday at 10:00am" in page.data
    assert client.get("/shows/1/artwork").status_code == 200


def test_show_activation_toggle(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/live",
    })
    client.post("/shows", data={
        "station_id": "1",
        "name": "Toggle Show",
        "duration_minutes": "62",
        "frequency": "daily",
        "start_time": "10:00",
    })
    assert b"Pause" in client.get("/").data
    response = client.post("/shows/1/toggle")
    assert response.headers["Location"].endswith(
        "?highlight_show=1"
    )
    assert "#show-" not in response.headers["Location"]
    highlighted = client.get(response.headers["Location"])
    assert b"show-highlight" in highlighted.data
    assert b"Toggle Show paused." in highlighted.data
    page = client.get("/")
    assert b"Toggle Show" in page.data
    assert b"Paused" in page.data
    assert b"Unpause" in page.data
    response = client.post("/shows/1/toggle")
    assert response.headers["Location"].endswith(
        "?highlight_show=1"
    )
    assert b"Pause" in client.get("/").data


def test_record_now_duration_override(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/live",
    })
    client.post("/shows", data={
        "station_id": "1",
        "name": "Manual Show",
        "duration_minutes": "62",
        "frequency": "daily",
        "start_time": "10:00",
    })
    monkeypatch.setattr("radio_recorder.routes.Thread.start", lambda self: None)

    page = client.get("/")
    assert b'class="quiet record-now-button"' in page.data
    assert b'data-duration="62"' in page.data

    response = client.post(
        "/shows/1/record", data={"duration_minutes": "17"}
    )
    assert response.status_code == 302
    with app.app_context():
        recording = query(
            "SELECT duration_minutes FROM recordings", one=True
        )
    assert recording["duration_minutes"] == 17

    invalid = client.post(
        "/shows/1/record", data={"duration_minutes": "0"}
    )
    assert invalid.status_code == 302
    with app.app_context():
        count = query(
            "SELECT COUNT(*) AS count FROM recordings", one=True
        )["count"]
    assert count == 1


def test_recording_status_lamp(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/live",
    })
    client.post("/shows", data={
        "station_id": "1",
        "name": "Live Show",
        "duration_minutes": "62",
        "frequency": "daily",
        "start_time": "10:00",
    })

    page = client.get("/")
    assert b'id="recording-lamp"' in page.data
    assert b"Scheduler online" not in page.data
    assert client.get("/recording-status").get_json() == {
        "recording": False,
        "show_ids": [],
        "shows": [],
    }

    with app.app_context():
        timestamp = now_iso()
        execute(
            """
            INSERT INTO recordings(
                show_id, scheduled_at, status, created_at, updated_at
            ) VALUES(1, ?, 'recording', ?, ?)
            """,
            (timestamp, timestamp, timestamp),
        )
    assert client.get("/recording-status").get_json() == {
        "recording": True,
        "show_ids": [1],
        "shows": ["Live Show"],
    }
    recording_page = client.get("/")
    assert b'class="show-recording-message"' in recording_page.data
    assert b"recording-now" in recording_page.data
    assert b"Recording now" in recording_page.data


def test_capture_stream_reconnects_and_concatenates_segments(tmp_path, monkeypatch):
    clock = {"now": 0.0}
    capture_commands = []

    def fake_run(command, **kwargs):
        output = Path(command[-1])
        if "-f" in command and "concat" in command:
            output.write_bytes(b"first-second")
            return subprocess.CompletedProcess(command, 0)
        capture_commands.append(command)
        output.write_bytes(b"first" if len(capture_commands) == 1 else b"second")
        clock["now"] += 4
        return subprocess.CompletedProcess(
            command, 1 if len(capture_commands) == 1 else 0
        )

    monkeypatch.setattr(
        "radio_recorder.processing.time.monotonic", lambda: clock["now"]
    )
    monkeypatch.setattr(
        "radio_recorder.processing.time.sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    monkeypatch.setattr("radio_recorder.processing.subprocess.run", fake_run)

    result = capture_stream("https://example.test/live", 8, tmp_path)

    assert result.read_bytes() == b"first-second"
    assert len(capture_commands) == 2
    assert "-reconnect" in capture_commands[0]
    assert capture_commands[0][capture_commands[0].index("-t") + 1] == "8"
    assert capture_commands[1][capture_commands[1].index("-t") + 1] == "2"


def test_delivery_without_playlist_sidecar(tmp_path):
    app = make_app(tmp_path)
    with app.app_context():
        execute(
            """
            INSERT INTO stations(
                station_id, call_letters, stream_url, created_at
            ) VALUES('KVCU Radio', 'KVCU', 'https://example.test/live', ?)
            """,
            (now_iso(),),
        )
        artwork_path = tmp_path / "data" / "artwork" / "show.jpg"
        Image.new("RGB", (8, 8), "purple").save(artwork_path, "JPEG")
        show_id = execute(
            """
            INSERT INTO shows(
                station_id, name, duration_minutes, artwork_path, frequency,
                start_time, weekday, enabled, created_at
            ) VALUES(1, 'No Playlist Show', 62, ?, 'daily', '10:00', NULL, 1, ?)
            """,
            (str(artwork_path), now_iso()),
        )
        work_dir = tmp_path / "data" / "work" / "1"
        work_dir.mkdir(parents=True)
        mp3_path = work_dir / "2026-06-19 No Playlist Show.mp3"
        mp3_path.write_bytes(b"mp3")
        recording_id = execute(
            """
            INSERT INTO recordings(
                show_id, scheduled_at, status, mp3_path, playlist_path,
                created_at, updated_at
            ) VALUES(?, '2026-06-19T10:00:00+00:00', 'ready', ?, NULL, ?, ?)
            """,
            (show_id, str(mp3_path), now_iso(), now_iso()),
        )
        assert deliver_recording(recording_id)
        destination = (
            tmp_path / "final" / "No Playlist Show" /
            "No Playlist Show 2026"
        )
        assert (destination / mp3_path.name).exists()
        assert list(destination.glob("*.txt")) == []
        show_dir = destination.parent
        assert (show_dir / "_info.yaml").read_text() == (
            "show: No Playlist Show\n"
            "station: KVCU Radio\n\n"
            "tags:\n"
            "  - KVCU\n\n"
            "notes: |\n"
            "  This is a longer human-editable note.\n"
            "  It can span multiple lines.\n"
        )
        assert (show_dir / "artist.jpg").exists()
        recording = query(
            "SELECT playlist_path FROM recordings WHERE id=?",
            (recording_id,),
            one=True,
        )
        assert recording["playlist_path"] is None

        (show_dir / "_info.yaml").write_text("user edited\n")
        (show_dir / "artist.jpg").write_bytes(b"user artwork")
        second_work_dir = tmp_path / "data" / "work" / "2"
        second_work_dir.mkdir(parents=True)
        second_mp3 = second_work_dir / "2026-06-20 No Playlist Show.mp3"
        second_mp3.write_bytes(b"mp3")
        second_id = execute(
            """
            INSERT INTO recordings(
                show_id, scheduled_at, status, mp3_path, created_at, updated_at
            ) VALUES(?, '2026-06-20T10:00:00+00:00', 'ready', ?, ?, ?)
            """,
            (show_id, str(second_mp3), now_iso(), now_iso()),
        )
        assert deliver_recording(second_id)
        assert (show_dir / "_info.yaml").read_text() == "user edited\n"
        assert (show_dir / "artist.jpg").read_bytes() == b"user artwork"


def test_station_logo_and_show_editing(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    logo = Image.new("RGB", (8, 8), "blue")
    logo_bytes = BytesIO()
    logo.save(logo_bytes, "PNG")
    logo_bytes.seek(0)
    response = client.post("/stations", data={
        "station_id": "WXYZ",
        "call_letters": "WXYZ-CALL-LETTERS",
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
    assert b'id="station-logo-preview"' in edit_station_page.data
    assert b'src="/stations/1/logo"' in edit_station_page.data
    assert b'name="call_letters"' in edit_station_page.data
    assert b'value="WXYZ-CALL-LETTERS"' in edit_station_page.data
    assert b'maxlength="8"' not in edit_station_page.data
    response = client.post("/stations/1/update", data={
        "station_id": "WXYZ-FM",
        "call_letters": "WXYZ",
        "stream_url": "https://example.test/updated",
        "mastodon_url": "https://mastodon.test/@wxyz",
    })
    assert response.status_code == 302
    station_page = client.get("/config/stations")
    assert b"WXYZ-FM" in station_page.data
    assert b"https://example.test/updated" in station_page.data
    assert b'class="logo-update"' not in station_page.data
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
        "0:20 Second Song",
        "0:00 Before Start",
    ]


def test_playlist_keeps_only_last_zero_timestamp(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    scheduled = datetime(2026, 6, 19, 10, 0, tzinfo=timezone.utc)
    assert format_playlist([
        "9:58am Before Start",
        "10:00am At Start",
        "10:05am First Real Track",
    ], scheduled) == [
        "0:00 At Start",
        "0:05 First Real Track",
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


def test_daily_mon_fri_frequency(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/live",
    })
    response = client.post("/shows", data={
        "station_id": "1",
        "name": "Weekday Selection Show",
        "duration_minutes": "62",
        "frequency": "weekdays",
        "start_time": "08:00",
    })
    assert response.status_code == 302
    page = client.get("/")
    assert b"Every Monday" in page.data
    with app.app_context():
        show = query(
            "SELECT frequency, weekday FROM shows WHERE name=?",
            ("Weekday Selection Show",),
            one=True,
        )
    assert show["frequency"] == "weekdays"
    assert show["weekday"] is None
    assert b"Monday-through-Friday" not in page.data


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


def test_recording_log_filters_by_valid_status(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    client.post("/stations", data={
        "station_id": "KVCU",
        "stream_url": "https://example.test/live",
    })
    client.post("/shows", data={
        "station_id": "1",
        "name": "Status Show",
        "duration_minutes": "62",
        "frequency": "daily",
        "start_time": "10:00",
    })
    statuses = [
        "queued", "recording", "ready", "delivery_pending",
        "complete", "failed", "unexpected",
    ]
    with app.app_context():
        for index, status in enumerate(statuses):
            timestamp = f"2026-06-{index + 1:02d}T16:00:00+00:00"
            execute(
                """
                INSERT INTO recordings(
                    show_id, scheduled_at, status, error, created_at, updated_at
                ) VALUES(1, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    status,
                    "ROGUE STATUS" if status == "unexpected" else None,
                    timestamp,
                    timestamp,
                ),
            )

    all_statuses = client.get("/?tab=recordings")
    for status in statuses[:-1]:
        assert f'<option value="{status}"'.encode() in all_statuses.data
    assert b'<option value="unexpected"' not in all_statuses.data
    assert b"ROGUE STATUS" not in all_statuses.data

    failed = client.get("/?tab=recordings&recording_status=failed")
    assert failed.data.count(b"<tr><td>Status Show") == 1
    assert b'<span class="pill failed">failed</span>' in failed.data
    assert b'<option value="failed" selected>' in failed.data


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
    assert b"Select a station" in dashboard.data
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
    stations_page = client.get("/config/stations")
    storage_page = client.get("/config/storage")
    assert stations_page.status_code == 200
    assert storage_page.status_code == 200
    assert b'<div class="panel-title"><h3>Stations</h3></div>' in stations_page.data
    assert b'<div class="panel-title"><h3>Archive location</h3></div>' in storage_page.data
    assert b'<div class="panel-title"><h3>Meta template</h3></div>' in storage_page.data
    assert b'name="meta_template"' in storage_page.data
    assert b"&lt;station-call-letters&gt;" in storage_page.data
    assert b"&lt;show_name&gt;" in storage_page.data
    assert b"Final storage" not in storage_page.data

    custom_template = "show: <show_name>\nstation: <station-call-letters>"
    response = client.post(
        "/settings/meta-template",
        data={"meta_template": custom_template},
    )
    assert response.status_code == 302
    with app.app_context():
        assert query(
            "SELECT value FROM settings WHERE key='meta_template'", one=True
        )["value"] == custom_template


def test_old_container_storage_default_migrates(tmp_path):
    app = make_app(tmp_path)
    with app.app_context():
        execute(
            "UPDATE settings SET value='/recordings' WHERE key='final_dir'"
        )
    app = create_app({
        "TESTING": True,
        "START_SCHEDULER": False,
        "DATA_DIR": str(tmp_path / "data"),
        "DATABASE": app.config["DATABASE"],
        "FINAL_DIR": "/server-share",
    })
    page = app.test_client().get("/config/storage")
    assert b'value="/server-share"' in page.data


def test_shows_order_by_schedule(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    fixed_now = datetime(2026, 6, 19, 8, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "radio_recorder.routes.current_local_time", lambda: fixed_now
    )
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
            ("Currently Recording", "weekly", "08:00", 4),
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
        b"Currently Recording", b"Friday Early", b"Daily Late",
        b"Friday Late", b"Sunday Show", b"Weekday Early", b"Tuesday Early",
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
        station_columns = [
            row[1] for row in db.execute("PRAGMA table_info(stations)")
        ]
        call_letters = db.execute(
            "SELECT call_letters FROM stations"
        ).fetchone()[0]
        show = db.execute("SELECT name, frequency FROM shows").fetchone()
        table_sql = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='shows'"
        ).fetchone()[0]
        foreign_key_errors = db.execute("PRAGMA foreign_key_check").fetchall()
        db.close()
    assert "slug" not in columns
    assert "call_letters" in station_columns
    assert call_letters == "KVCU"
    assert show == ("Legacy Show", "weekly")
    assert "'weekdays'" in table_sql
    assert foreign_key_errors == []
