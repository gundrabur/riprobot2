# RipRobot 2 - Automated CD Ripping Solution

## Overview

RipRobot 2 is an automated CD ripping appliance with intelligent music metadata integration and a full web dashboard. A FastAPI backend monitors the optical drive, extracts audio tracks, enriches them with MusicBrainz metadata, and serves a bilingual (German/English) single-page web interface for triggering rips, watching live progress (including a real-time read-speed chart), browsing rip history, and configuring the system. The entire application runs in Docker for easy, reproducible deployment - typically on a Raspberry Pi with an attached optical drive and USB storage.

## Features

### Ripping & Conversion
- **Automated CD Ripping**: Detects and rips audio CDs using `cdparanoia`, with a configurable accuracy mode (safe / fast / error-correction disabled)
- **MusicBrainz Integration**: Retrieves album metadata (artist, title, full tracklist) for intelligent file/folder naming and tagging
- **Selectable Output Format**: FLAC (max compression, tagged), MP3 (V0 quality, tagged), or WAV (untagged raw audio) - configurable per settings
- **RAM-First Ripping**: Rips into a `tmpfs` (`/dev/shm`) staging area first (falls back to the destination directly if not enough RAM is free), then moves finished files to the USB stick. This keeps the write-speed of the USB stick from skewing the read-speed measurement and reduces flash wear
- **Intelligent Timeouts**: Configurable network timeout (MusicBrainz) and extraction timeout (cdparanoia) to prevent hanging operations
- **Organized Output**: Creates folders based on artist/album metadata with properly numbered/named tracks (e.g. `01 - Track Title.flac`)
- **Automatic Device Ejection**: Safely ejects the disc after completion or failure
- **Startup Readiness Signal**: Ejects the tray once per boot as soon as the drive and a writable USB target are both available

### Web Dashboard
- **Live Status View**: Current state, artist/album, progress bar, and the currently ripping track name (styled as its own subtitle line)
- **Live Read-Speed Chart**: Optional (opt-in, default off) real-time line chart of the drive's actual read throughput in MB/s while ripping, powered by Chart.js; stays visible through conversion/success/error until the next disc starts
- **Rip History**: Every rip attempt (success or failure) is recorded with artist/album, format, duration, track count, and - if enabled - its full speed chart. Entries can be inspected, deleted individually, or cleared entirely from the "Verlauf" tab
- **USB Drive Picker**: The output-path field in Settings can auto-discover mounted drives under `/media`; picks the only one automatically, or shows a picker popup when multiple are found
- **Info Modal**: Shows app version, developer, copyright, and live debug info (OS, hardware model, RAM, optical drive model, USB stick capacity, status-LED detection)
- **Bilingual Interface**: Complete German and English frontend with a persistent language selection
- **No-Cache Static Serving**: Frontend files are served with `Cache-Control: no-cache` so UI updates apply on a normal refresh without a hard-reload

### Status LEDs (optional, best-effort)
- **Onboard LED Blink Codes**: Auto-detects a controllable status LED under `/sys/class/leds` (multicolor class, separate RGB channels, or a simple mono LED like `ACT`/`PWR`) and blinks it according to the current state (searching, ripping, converting, success, error). No-ops safely if no LED is available or accessible
- **Raspberry Pi 500+ Keyboard LEDs**: On a Pi 500+ with an updated keyboard firmware and `rpi-keyboard-config` installed, the four arrow keys mirror the same status colors/blink patterns
- **Diagnostics**: `/api/system-info` and `/api/led-debug` expose exactly what was detected, for troubleshooting on real hardware

### Platform
- **REST API**: Full JSON API backing the dashboard (status, settings, history, USB drives, system info, rip trigger, eject)
- **Docker Containerized**: Complete environment encapsulation for consistent, repeatable deployments
- **Unattended Installer**: `install.sh` sets up Docker, optional Pi 500+ keyboard tooling, and builds/starts the app on a fresh Raspberry Pi OS (Trixie) install with no prompts

## Requirements

### System Requirements
- Linux-based system (tested on Raspberry Pi OS / Debian) with Docker and the Docker Compose plugin
- Optical drive accessible at `/dev/sr0` (configurable)
- USB storage mountable under `/media` for output files
- Internet connection for MusicBrainz metadata lookups
- Optional: Raspberry Pi 500+ with updated keyboard firmware, for the keyboard-LED blink codes

### Software Dependencies
- Docker Engine + Compose plugin
- Python 3.11+ (runs inside Docker)
- CD extraction/encoding tools: cdparanoia, FLAC, LAME/ffmpeg, eject
- Python libraries: fastapi, uvicorn, discid, musicbrainzngs (see `backend/requirements.txt`)
- Frontend: Tailwind CSS and Chart.js, both loaded from CDN (no build step)

## Installation

### Option A: Unattended installer (recommended for a fresh Raspberry Pi)

```bash
git clone <repository-url> riprobot2
cd riprobot2
sudo ./install.sh
```

`install.sh` is fully non-interactive and idempotent (safe to re-run). It:
1. Installs Docker Engine + the Compose plugin (via the official `get.docker.com` script)
2. Adds the invoking user to the `docker` group
3. Detects a Raspberry Pi 500/500+ and, if found, best-effort installs `rpi-keyboard-fw-update`/`rpi-keyboard-config` for the keyboard-LED feature (skipped harmlessly on other models or if unavailable)
4. Creates `/media/usb` as a default mount point
5. Builds the Docker image and starts the container
6. Prints the dashboard URL (`http://<pi-ip>:8000`)

### Option B: Docker Compose (manual)

1. **Clone the repository**
   ```bash
   git clone <repository-url> riprobot2
   cd riprobot2
   ```

2. **Build and start**
   ```bash
   docker compose build --no-cache
   docker compose up -d
   ```

3. **Open the dashboard**
   ```
   http://localhost:8000
   ```

### Option C: Manual installation (without Docker)

1. **Install system dependencies**
   ```bash
   sudo apt-get update
   sudo apt-get install -y cdparanoia lame flac eject libdiscid0 ffmpeg
   ```

2. **Create a virtual environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Python dependencies**
   ```bash
   pip install -r backend/requirements.txt
   ```

4. **Run the application**
   ```bash
   cd backend
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

## Usage

### Web Dashboard

Open `http://<host>:8000` in a browser. The interface has four sections, reachable from the top navigation bar:

- **Dashboard**: current status, progress, optional live speed chart, manual rip trigger, eject button
- **Verlauf (History)**: list of past rip attempts, expandable per-entry speed chart, delete individually or clear all
- **Einstellungen (Settings)**: output format, output path (with USB drive picker), read accuracy, timeouts, speed-chart toggle
- **Info (i button)**: version/developer/copyright plus live debug info about the running system

### REST API

All endpoints are also used internally by the dashboard and can be scripted directly.

| Method & Path | Description |
|---|---|
| `GET /api/status` | Current rip status, progress, live speed data |
| `GET /api/settings` | Current settings |
| `POST /api/settings` | Update settings (format, output path, paranoia mode, timeouts, speed-chart toggle) |
| `GET /api/history` | List of past rip results |
| `DELETE /api/history/{id}` | Delete a single history entry |
| `DELETE /api/history` | Clear the entire history |
| `GET /api/usb-drives` | Auto-discovered mounted drives under `/media` |
| `GET /api/system-info` | Version, OS/hardware info, RAM, optical drive, USB stick, LED detection |
| `GET /api/led-debug` | Raw listing of all `/sys/class/leds` devices, for LED troubleshooting |
| `POST /trigger-rip?device=sr0` | Start a rip on the given device |
| `POST /eject?device=sr0` | Eject the given device (blocked while a rip is in progress) |

**Example:**
```bash
curl -X POST "http://localhost:8000/trigger-rip?device=sr0"
```

**Processing steps for a triggered rip:**
1. Waits briefly for the disc to stabilize
2. Verifies the USB storage is available and writable
3. Queries MusicBrainz for album metadata and tracklist
4. Extracts tracks with `cdparanoia` into RAM (falls back to the destination if not enough RAM), tracking live read speed
5. Converts WAV to the configured format (FLAC/MP3) with tags, or renames WAV files
6. Moves finished files from RAM to the USB stick and syncs
7. Records the result (success/error, duration, track count, speed chart) in the history
8. Ejects the disc

### Output Directory Structure

```
/media/usb/
├── The Beatles - Abbey Road/
│   ├── 01 - Come Together.flac
│   ├── 02 - Something.flac
│   └── ...
├── Pink Floyd - The Wall/
│   ├── 01 - In the Flesh?.flac
│   └── ...
└── rip_2026-08-30_15-45-32/  # Fallback for unidentified CDs (timestamp-based)
    ├── 01 - Track 01.flac
    └── ...
```

Tagged formats (FLAC/MP3) embed artist, album, track number, and title.

## Project Structure

```
riprobot2/
├── .github/                          # Internal development guidelines
├── backend/
│   ├── Dockerfile                    # Docker image: system deps + optional rpi-keyboard-config
│   ├── main.py                       # FastAPI app, ripping logic, settings/history/LED controllers
│   ├── requirements.txt              # Python package dependencies
│   ├── settings.json                 # Persisted settings (created at runtime)
│   ├── history.json                  # Persisted rip history (created at runtime)
│   └── static/
│       └── index.html                # Full web dashboard (HTML/JS/Tailwind/Chart.js, no build step)
├── install.sh                        # Unattended installer for a fresh Raspberry Pi
├── docker-compose.yml                # Docker Compose configuration
├── .gitignore
├── .vscode/
├── LICENSE                            # MIT License
└── README.md                          # This file
```

## Configuration

Most configuration is done via the **Settings** tab in the web dashboard (persisted to `backend/settings.json`):

| Setting | Default | Description |
|---|---|---|
| Output format | `FLAC` | `FLAC`, `MP3`, or `WAV` |
| Output path | `/media/usb` | Destination folder; use the "Select" button to pick a detected drive |
| Read accuracy | `safe` | `safe` (`-B`), `fast` (`-B -Y`), or `disable` (`-B -Z`) paranoia mode |
| MusicBrainz timeout | `60` s | Network timeout for metadata lookups |
| Rip timeout | `3600` s | Abort threshold for a stuck `cdparanoia` process |
| Live speed chart | off | Enables read-speed measurement and the live/history charts |

### Environment Variables (`docker-compose.yml`)

| Variable | Default | Description |
|---|---|---|
| `TZ` | `Europe/Berlin` | System timezone |
| `PYTHONUNBUFFERED` | `1` | Unbuffered Python output for real-time logging |

### Device Configuration

To use a different optical drive, trigger with a different device query parameter:
```bash
curl -X POST "http://localhost:8000/trigger-rip?device=sr1"
```

## Technical Details

### Core Technologies

- **FastAPI + Uvicorn**: Backend API and static file serving
- **cdparanoia**: High-quality CD audio extraction with timeout protection
- **discid** / **musicbrainzngs**: Disc ID lookup and MusicBrainz metadata client
- **flac** / **ffmpeg (lame)**: Lossless/MP3 encoding and tagging
- **Chart.js**: Live and historical read-speed line charts (loaded from CDN)
- **Tailwind CSS**: Utility-first styling (loaded from CDN)
- **threading**: Concurrency control (rip lock, LED blink loops) and background operations

### Read-Speed Measurement

The speed chart reflects genuine drive read throughput, not USB write speed: `cdparanoia` rips into a `tmpfs` staging directory (`/dev/shm`) first, where writes are effectively instantaneous, so file-growth sampling there is bottlenecked only by the optical drive. Files are moved to the real USB destination only after ripping/conversion completes. If insufficient RAM is free, ripping falls back to writing directly to the destination (in which case the chart may be influenced by USB write speed).

### LED Status Indicators

`LedController` auto-detects, in order: a `multicolor` LED class device (e.g. RGB status LEDs), separate red/green/blue LED devices, or a simple single-color LED (`ACT`, `PWR`, or any other device under `/sys/class/leds`). `KeyboardLedController` additionally drives the four arrow keys on a Raspberry Pi 500+ via the `rpi-keyboard-config` CLI (installed on the host and, best-effort, inside the Docker image). Both controllers are no-ops if nothing is detected or accessible - the app runs identically with or without working status LEDs.

### Key Implementation Details

- **Thread-Safe Operations**: `threading.Lock()` guards concurrent access to the shared rip/eject state
- **Asynchronous Ripping**: FastAPI `BackgroundTasks` run rips without blocking HTTP responses
- **Timeout Protection**: Configurable network and extraction timeouts prevent indefinite hangs
- **History Persistence**: Every rip attempt (success or failure) is appended to `backend/history.json` (capped at 200 entries), including its speed-chart data if the feature was enabled
- **Graceful Degradation**: Speed chart, status LEDs, and keyboard LEDs are all optional, best-effort features that fail safely without impacting core ripping functionality

## Troubleshooting

### CD Drive Not Detected
- Verify the drive is accessible: `ls -la /dev/sr0`
- Check the device mapping in `docker-compose.yml`
- Ensure the disc is properly inserted and recognized by the system

### USB Storage Not Available
- Verify the mount: `mount | grep media`
- Check permissions: `ls -la /media/usb`
- Ensure the device has sufficient free space
- Use the "Select" button in Settings to confirm the drive is auto-discovered

### MusicBrainz Lookup Fails
- Verify internet connectivity
- The system falls back to timestamp-based naming if metadata is unavailable
- Review logs: `docker compose logs -f riprobot-backend`

### Container Won't Start / Build Fails
- Check logs: `docker compose logs riprobot-backend`
- Verify Compose syntax: `docker compose config`
- Ensure all required volumes and devices are accessible
- Run `docker compose build --no-cache` from the project root (where `docker-compose.yml` lives), not from `backend/`

### Status LEDs Not Blinking
- Check `GET /api/system-info` (`led` and `keyboard_led` fields) and `GET /api/led-debug` for what was detected
- On a Pi 500+, ensure the keyboard firmware is updated (`sudo rpi-keyboard-fw-update`) and `rpi-keyboard-config` is installed both on the host and inside the container image

## Performance Considerations

- **Ripping Speed**: Depends on disc condition and drive speed (typically a few minutes per CD)
- **Memory Usage**: A full CD ripped into RAM uses up to ~850 MB of `tmpfs`; the app falls back to direct-to-disk if this isn't available
- **CPU Usage**: Low during metadata retrieval, moderate during extraction/encoding
- **Network**: Required only for MusicBrainz lookups

## Security Notes

- The application runs with access to the host's optical drive and file system, and (if enabled) keyboard firmware tooling
- Ensure proper file permissions on USB storage
- Do not expose the API/dashboard to untrusted networks without authentication or a reverse proxy
- Static files are served with `Cache-Control: no-cache` for fast iteration; add caching at a reverse proxy layer for production if desired

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Support & Contributing

For issues, feature requests, or contributions:

1. Check existing issues on GitHub
2. Follow the contribution guidelines in `.github/github-instructions.md`
3. Submit pull requests with clear descriptions and updated documentation

## Changelog

### Version 2.2.0 (Current)
- **Web Dashboard**: Full bilingual single-page frontend (Dashboard, Verlauf, Einstellungen, Info) replacing pure API-only usage
- **Live Read-Speed Chart**: Optional real-time and historical MB/s charting, measured via RAM-first ripping for accuracy
- **Rip History**: Persisted per-run results with delete (single/all) support
- **USB Drive Picker**: Auto-discovery of mounted drives under `/media` with a selection popup
- **Status LEDs**: Auto-detected onboard LED blink codes (mono/RGB/multicolor) plus Raspberry Pi 500+ keyboard-arrow-key blink codes
- **Info Modal & Diagnostics**: Version/developer/copyright plus live system/hardware/LED debug info
- **Selectable Output Format**: FLAC, MP3 (V0), or WAV, configurable via Settings
- **Unattended Installer**: `install.sh` for fully non-interactive setup on a fresh Raspberry Pi OS (Trixie)
- **No-Cache Static Serving**: Frontend updates apply without a hard-refresh

### Version 2.1.0
- FLAC conversion with maximum compression and embedded metadata tags
- Tracklist integration from MusicBrainz
- Timeout protection for network and extraction operations
- Improved error handling and file naming

### Version 2.0.0
- Initial release with FastAPI backend
- MusicBrainz metadata integration (artist/album only)
- Docker containerization
- Asynchronous ripping operations with cdparanoia
- REST API for triggering rips
- WAV output format

---

**Last Updated**: September 6, 2026 (v2.2.0)
**Maintainer**: Christian Möller
**Status**: Active Development
