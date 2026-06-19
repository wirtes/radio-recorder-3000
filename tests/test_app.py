from __future__ import annotations

from datetime import datetime
from io import BytesIO

from PIL import Image

from radio_recorder import create_app
from radio_recorder.playlist import clean_status_line, parse_account_url
from radio_recorder.processing import add_id3_tags, build_destination, track_number
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
        "slug": "test-show",
        "name": "Test Show",
        "duration_minutes": "60",
        "frequency": "weekly",
        "weekday": "3",
        "start_time": "10:00",
        "artwork": (image_bytes, "cover.jpg"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302
    page = client.get("/")
    assert b"Test Show" in page.data
    assert b"KVCU" in page.data


def test_playlist_cleanup():
    content = "🎶 10:00am Out In A Boat by The Von Trapps<br>1190 Mixtape<br>#Radio"
    assert clean_status_line(content) == "10:00am Out In A Boat by The Von Trapps"
    assert parse_account_url("https://example.social/@radio") == (
        "https://example.social", "radio"
    )


def test_track_number_and_destination(tmp_path):
    when = datetime(2026, 6, 18, 10, 0)
    assert track_number(when, "daily") == 169
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
