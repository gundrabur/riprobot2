# RipRobot 2 - Automated CD Ripping Solution

## Overview

RipRobot 2 is a Dockerized CD ripping appliance for Raspberry Pi and similar Linux hosts. The backend uses FastAPI to monitor the optical drive, extract audio tracks with cdparanoia, enrich them with MusicBrainz metadata, and serve a browser-based dashboard for status, history, settings, and diagnostics.

The current implementation already covers the core workflow for unattended ripping, live read-speed tracking, persistent history, USB-drive selection, and optional status LED feedback.

## Features

### Ripping and conversion
- Automated CD ripping with configurable paranoia modes: safe, fast, or disabled
- MusicBrainz metadata lookup for artist, album, and track titles
- Selectable output formats: FLAC, MP3, or WAV
- RAM-first ripping into /dev/shm when enough memory is available, followed by move to the final destination
- Automatic timeout protection for metadata lookup and extraction
- Automatic disc ejection after success, failure, or timeout
- Startup readiness eject once per boot when both drive and output storage are available

### Web dashboard
- Live status view with artist, album, progress, and current track title
- Optional real-time read-speed chart in MB/s while ripping
- Persistent rip history with per-entry details and delete/clear-all actions
- Settings panel for format, output path, paranoia mode, timeouts, and speed-chart toggle
- Info modal with version, developer, hardware details, LED detection, and storage information
- No-cache static delivery so frontend updates become visible after a normal refresh

### Output drive selection
- Removable drives are detected continuously and mounted automatically under /media/<label-or-device>; if several are plugged in, all of them are mounted
- If the configured output path is not a writable mount, the first detected drive is selected as the target automatically
- The settings page lists all detected drives and lets you switch the target to any of them
- The default output path is /media/usb, but another mounted or mountable target can be chosen

### Optional status LEDs
- Onboard LEDs under /sys/class/leds are detected automatically and used for status blinking when available
- Raspberry Pi 500+ cursor keys show persistent colors via rpi-keyboard-config: green = ready, violet = metadata query, yellow = ripping, blue = converting, white = copying to USB, red = rip error
- In the ready state, the cursor-up key is red when no USB storage is available and the cursor-down key is red when no optical drive is available
- The four cursor keys do not blink; their colors alone represent the current state
- Both features are best-effort and fail safely if the hardware or tooling is unavailable

### Platform
- JSON API for status, settings, history, USB-drive discovery, mount actions, system info, and LED diagnostics
- Docker-based deployment with an unattended installer for fresh Raspberry Pi OS installs

## Requirements

### System requirements
- Linux-based system with Docker and the Docker Compose plugin
- Optical drive available at /dev/sr0 (or another device passed via the trigger endpoint)
- Writable USB storage mounted under /media or another reachable destination
- Internet connection for MusicBrainz metadata lookups
- Optional: Raspberry Pi 500+ with compatible keyboard firmware and rpi-keyboard-config for keyboard LED feedback

### Software dependencies
- Docker Engine + Compose plugin
- Python 3.11+ inside the container
- CD extraction and encoding tools: cdparanoia, FLAC, LAME/ffmpeg, eject
- Python packages: fastapi, uvicorn, discid, musicbrainzngs (see backend/requirements.txt)

## Installation

### Option A: Unattended installer (recommended)

```bash
git clone <repository-url> riprobot2
cd riprobot2
sudo ./install.sh
```

The installer is non-interactive and idempotent. It will:
1. Install Docker Engine and the Compose plugin
2. Add the invoking user to the docker group
3. Install optional Raspberry Pi 500+ keyboard tools when applicable
4. Create /media/usb as a default mount point
5. Build and start the container
6. Print the dashboard URL as http://<pi-ip>:8000

### Option B: Docker Compose

```bash
git clone <repository-url> riprobot2
cd riprobot2
docker compose build --no-cache
docker compose up -d
```

Open http://localhost:8000 in a browser.

### Option C: Manual installation without Docker

```bash
sudo apt-get update
sudo apt-get install -y cdparanoia lame flac eject libdiscid0 ffmpeg
python3 -m venv venv
source venv/bin/activate
pip install -r backend/requirements.txt
cd backend
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

## Usage

Open http://<host>:8000 in a browser. The interface contains four sections:
- Dashboard: status, progress, optional live speed chart, manual rip trigger, eject button
- Verlauf: rip history with per-entry speed charts and delete/clear actions
- Einstellungen: output format, output path, paranoia mode, timeouts, and speed-chart toggle
- Info: version, developer, copyright, and live system/LED diagnostics

### REST API

| Method and path | Description |
|---|---|
| GET /api/status | Current rip state, progress, speed, and chart data |
| GET /api/settings | Current settings |
| POST /api/settings | Update settings |
| GET /api/history | List of past rip results |
| DELETE /api/history/{id} | Delete one history entry |
| DELETE /api/history | Clear the full history |
| GET /api/usb-drives | Discover removable drives, including unmounted ones |
| POST /api/usb-drives/mount | Mount a selected removable drive under /media/<label-or-device> |
| GET /api/system-info | OS, hardware, RAM, drive, storage, and LED information |
| GET /api/led-debug | Raw LED detection data for debugging |
| POST /trigger-rip?device=sr0 | Start a rip for a specific device |
| POST /eject?device=sr0 | Eject the optical drive |

Example:

```bash
curl -X POST "http://localhost:8000/trigger-rip?device=sr0"
```

### Rip workflow
1. Wait briefly for the disc to stabilize
2. Verify the chosen output path is available and writable
3. Request metadata from MusicBrainz when possible
4. Extract tracks with cdparanoia into a RAM staging directory when available
5. Convert to FLAC/MP3/WAV, then move the finished files to the final destination
6. Record the result in history and eject the disc

## Project structure

```text
riprobot2/
├── backend/
│   ├── Dockerfile
│   ├── main.py
│   ├── requirements.txt
│   ├── settings.json
│   ├── history.json
│   └── static/index.html
├── install.sh
├── docker-compose.yml
├── LICENSE
└── README.md
```

## Configuration

Most settings are handled in the web UI and stored in backend/settings.json.

| Setting | Default | Description |
|---|---|---|
| Output format | FLAC | FLAC, MP3, or WAV |
| Output path | /media/usb | Target folder; can be selected from the drive picker |
| Read accuracy | safe | safe, fast, or disabled paranoia mode |
| MusicBrainz timeout | 60 s | Network timeout for metadata lookup |
| Rip timeout | 3600 s | Maximum amount of time the rip process is allowed to run |
| Live speed chart | off | Enables live and historical speed charting |

## Technical details

### Read-speed measurement
The read-speed graph is based on the growth of the temporary rip files. Ripping into /dev/shm first avoids USB write speed affecting the measured optical drive read rate. If not enough RAM is available, the application falls back to writing directly to the destination and the chart can be influenced by the target device.

### LED support
The backend auto-detects onboard LEDs in /sys/class/leds and can also drive Pi 500+ cursor keys via rpi-keyboard-config when installed. The cursor keys use steady colors rather than blinking: green (ready), violet (metadata query), yellow (ripping), blue (conversion), white (USB copy), and red (error). In the ready state, cursor up indicates missing USB storage and cursor down indicates a missing optical drive. These features are optional and do not block the ripping workflow if unavailable.

## Troubleshooting
- Check /dev/sr0 and the output path if the app cannot see the drive or USB storage
- Use GET /api/usb-drives to verify that removable drives are discovered correctly
- Inspect GET /api/system-info and GET /api/led-debug for LED and hardware diagnostics
- Review logs with docker compose logs -f riprobot-backend

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for details.

## Changelog

### Current implementation
- Web dashboard with dashboard, history, settings, and info views
- Live read-speed chart and persisted rip history
- USB-drive discovery and mount-on-select support
- Optional onboard and keyboard LED feedback
- Unattended Docker installer for Raspberry Pi OS

### Version 2.0.0
- Initial release of the current FastAPI-based implementation
- MusicBrainz metadata lookup and format conversion
- Docker-based deployment and REST API

---

Last updated: September 6, 2026
Maintainer: Christian Möller
