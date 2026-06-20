# Radio Recorder 3000

A containerized web application that schedules radio stream recordings, converts
them to MP3, creates Mastodon-derived playlist files, writes ID3 and podcast
metadata, and safely delivers completed files to shared storage.

## Run with Docker

```bash
docker compose up --build
```

Open <http://localhost:8585>. Persistent application data is stored in `./data`.
Completed recordings are delivered to `./Music/server-share`, mounted inside
the container as `/server-share`.

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
   Shows can also run every Monday through Friday.
2. `ffmpeg` records and encodes the stream as MP3 in local working storage. If
   the stream drops, recording reconnects until the scheduled recording window
   ends and concatenates the available audio segments into the final MP3.
3. If configured, the station's Mastodon account is queried for posts in the
   show's time window. The first text line of each post becomes the playlist.
4. ID3v2.3 title, artist, album artist, album, track, year, lyrics, description,
   genre, and cover-art tags are written.
5. The MP3 and matching TXT file are copied to:
   `Show Name/Show Name YYYY/YYYY-MM-DD Show Name.{mp3,txt}`.
6. Local work files are removed only after both final files are in place.
   Unavailable destinations retry with exponential backoff.

Use **Record now** to test a show without waiting for its scheduled time.

## Mount the Mac SMB share on Debian

This mounts the Mac server share:

- Server: `plex-server.lan`
- Share: `Radio Rips`
- Debian mount point: `/home/alw/code/radio-recorder-3000/Music/server-share`

### 1. Install CIFS support

```bash
sudo apt update
sudo apt install cifs-utils
```

### 2. Create the local mount directory

```bash
mkdir -p /home/alw/code/radio-recorder-3000/Music/server-share
```

### 3. Create a credentials file

Create a private credentials directory:

```bash
mkdir -p /home/alw/.smbcredentials
```

Create the credentials file:

```bash
nano /home/alw/.smbcredentials/plex-server
```

Add the Mac username and password:

```ini
username=YOUR_MAC_USERNAME
password=YOUR_MAC_PASSWORD
```

Secure the credentials file:

```bash
chmod 600 /home/alw/.smbcredentials/plex-server
```

### 4. Test the mount manually

```bash
sudo mount -t cifs "//plex-server.lan/Radio Rips" \
  "/home/alw/code/radio-recorder-3000/Music/server-share" \
  -o credentials=/home/alw/.smbcredentials/plex-server,uid=alw,gid=alw,vers=3.0,sec=ntlmssp,noserverino
```

Verify that the files are visible:

```bash
ls -la /home/alw/code/radio-recorder-3000/Music/server-share
```

To unmount it manually:

```bash
sudo umount /home/alw/code/radio-recorder-3000/Music/server-share
```

## Make the mount survive restarts with `/etc/fstab`

### 1. Edit `/etc/fstab`

```bash
sudo nano /etc/fstab
```

Add this line:

```fstab
//plex-server.lan/Radio\040Rips /home/alw/code/radio-recorder-3000/Music/server-share cifs credentials=/home/alw/.smbcredentials/plex-server,uid=alw,gid=alw,vers=3.0,sec=ntlmssp,noserverino,_netdev,nofail,x-systemd.automount 0 0
```

Notes:

- `Radio\040Rips` is the escaped form of `Radio Rips` for `/etc/fstab`.
- `_netdev` marks it as a network mount.
- `nofail` prevents Debian from hanging at boot if the Mac is offline.
- `x-systemd.automount` mounts the share on first access instead of forcing it during boot.

### 2. Test the `/etc/fstab` entry

Unmount the manual mount if it is currently mounted:

```bash
sudo umount /home/alw/code/radio-recorder-3000/Music/server-share
```

Reload systemd and test the fstab mount:

```bash
sudo systemctl daemon-reload
sudo mount -a
```

Access the directory to trigger the automount:

```bash
ls -la /home/alw/code/radio-recorder-3000/Music/server-share
```

### 3. Check mount status

```bash
mount | grep server-share
```

or:

```bash
findmnt /home/alw/code/radio-recorder-3000/Music/server-share
```

### 4. Reboot test

```bash
sudo reboot
```

After reboot, check the mount:

```bash
ls -la /home/alw/code/radio-recorder-3000/Music/server-share
findmnt /home/alw/code/radio-recorder-3000/Music/server-share
```
