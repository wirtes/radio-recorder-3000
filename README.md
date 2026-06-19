# Radio Recorder 3000

A containerized web application that schedules radio stream recordings, converts
them to MP3, creates Mastodon-derived playlist files, writes ID3 and podcast
metadata, and safely delivers completed files to shared storage.

## Run with Docker

```bash
docker compose up --build
```

Open <http://localhost:8585>. Persistent application data is stored in `./data`
and completed recordings in `./recordings`.

The container's `TZ` environment variable controls schedule interpretation.
Change it in `docker-compose.yml` if necessary.

## Run locally

Python 3.12 and `ffmpeg` are required.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python app.py
```

## Recording lifecycle

1. The scheduler starts a configured stream at its daily or weekly local time.
2. `ffmpeg` records and encodes the stream as MP3 in local working storage.
3. If configured, the station's Mastodon account is queried for posts in the
   show's time window. The first text line of each post becomes the playlist.
4. ID3v2.3 title, artist, album artist, album, track, year, lyrics, description,
   genre, and cover-art tags are written.
5. The MP3 and matching TXT file are copied to:
   `Show Name/Show Name YYYY/YYYY-MM-DD Show Name.{mp3,txt}`.
6. Local work files are removed only after both final files are in place.
   Unavailable destinations retry with exponential backoff.

Use **Record now** to test a show without waiting for its scheduled time.
