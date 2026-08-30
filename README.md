# RipRobot 2 - Automated CD Ripping Solution

## Overview

RipRobot 2 is an automated CD ripping application with intelligent music metadata integration. It features a FastAPI-based backend that monitors CD drives, automatically extracts audio tracks, and enriches ripped files with metadata from the MusicBrainz database. The entire system runs in Docker for easy deployment and portability.

The application provides a modern REST API for triggering rip operations and managing CD extraction tasks asynchronously without blocking user interactions.

## Features

- **Automated CD Ripping**: Automatically detects and rips audio CDs using cdparanoia
- **MusicBrainz Integration**: Retrieves album metadata (artist, title) to organize files intelligently
- **Asynchronous Processing**: Handles rip operations in the background via FastAPI BackgroundTasks
- **Concurrency Control**: Prevents simultaneous operations on the same device using thread-safe locks
- **Multiple Audio Format Support**: Extracts audio using cdparanoia with built-in FLAC and MP3 encoding capabilities
- **Organized Output**: Creates directory structures based on artist and album metadata for easy navigation
- **Automatic Device Ejection**: Safely ejects CDs after successful ripping
- **REST API**: Web-based interface for triggering rips and monitoring status
- **Docker Containerized**: Complete environment encapsulation for consistent deployments

## Requirements

### System Requirements
- Linux-based system with Docker and Docker Compose installed
- CD drive accessible at `/dev/sr0` (configurable)
- USB storage mounted at `/media/usb` for output files
- Internet connection for MusicBrainz metadata lookups

### Software Dependencies
- Docker and Docker Compose
- Python 3.11+ (runs inside Docker)
- CD extraction tools: cdparanoia, FLAC, LAME encoder
- Libraries: fastapi, uvicorn, discid, musicbrainzngs

## Installation

### Using Docker Compose (Recommended)

1. **Clone the Repository**
   ```bash
   git clone https://github.com/yourusername/riprobot2.git
   cd riprobot2
   ```

2. **Build and Start the Application**
   ```bash
   docker-compose up -d
   ```

   This will:
   - Build the Docker image from the Dockerfile
   - Start the RipRobot backend service
   - Expose the API on `http://localhost:8000`

3. **Verify Installation**
   ```bash
   curl http://localhost:8000/
   ```
   
   Expected response:
   ```json
   {"status": "RipRobot2 mit MusicBrainz-Integration!"}
   ```

### Manual Installation (Without Docker)

1. **Install System Dependencies**
   ```bash
   sudo apt-get update
   sudo apt-get install -y cdparanoia lame flac eject libdiscid0
   ```

2. **Create Virtual Environment**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Python Dependencies**
   ```bash
   pip install -r backend/requirements.txt
   ```

4. **Run the Application**
   ```bash
   cd backend
   uvicorn main:app --host 0.0.0.0 --port 8000 --reload
   ```

## Usage

### API Endpoints

#### 1. Health Check Endpoint
**GET** `/`

Returns the API status.

**Response:**
```json
{"status": "RipRobot2 mit MusicBrainz-Integration!"}
```

---

#### 2. Trigger CD Rip
**POST** `/trigger-rip`

Initiates an asynchronous CD ripping operation.

**Parameters:**
- `device` (query string, optional): CD drive device name (default: `sr0` for `/dev/sr0`)

**Request Example:**
```bash
curl -X POST http://localhost:8000/trigger-rip?device=sr0
```

**Response:**
```json
{"message": "Rip läuft bereits"}  # If already ripping
```

or

```json
{"message": "Rip queued"}  # If new rip is queued
```

---

### Workflow

1. **CD Placement**: Insert an audio CD into the drive
2. **Automatic Detection**: The system detects the CD presence (optional: trigger via API)
3. **Metadata Retrieval**: RipRobot queries MusicBrainz for album information
4. **Directory Creation**: Creates organized folders using artist and album names
5. **Audio Extraction**: Runs cdparanoia to extract all tracks
6. **Automatic Ejection**: Safely ejects the CD after completion
7. **File Organization**: Ripped tracks are stored in `/media/usb/[Artist] - [Album]/`

### Output Directory Structure

```
/media/usb/
├── The Beatles - Abbey Road/
│   ├── track01.wav
│   ├── track02.wav
│   └── ...
├── Pink Floyd - The Wall/
│   ├── track01.wav
│   ├── track02.wav
│   └── ...
└── rip_2026-08-30_15-45-32/  # Fallback for unidentified CDs
    ├── track01.wav
    └── ...
```

## Project Structure

```
riprobot2/
├── .github/
│   └── github-instructions.md      # Internal development guidelines
├── backend/
│   ├── Dockerfile                   # Docker configuration for the service
│   ├── main.py                      # FastAPI application and ripping logic
│   ├── requirements.txt             # Python package dependencies
│   └── venv/                        # Python virtual environment (if local)
├── .gitignore                       # Git ignore rules
├── .vscode/                         # VS Code workspace settings
├── docker-compose.yml               # Docker Compose configuration
├── LICENSE                          # MIT License
└── README.md                        # This file
```

## Configuration

### Environment Variables

Edit `docker-compose.yml` to customize:

| Variable | Default | Description |
|----------|---------|-------------|
| `TZ` | `Europe/Berlin` | System timezone |
| `PYTHONUNBUFFERED` | `1` | Unbuffered Python output for real-time logging |

### Device Configuration

To use a different CD drive:

1. Edit `docker-compose.yml`:
   ```yaml
   devices:
     - "/dev/sr1:/dev/sr1"  # For second CD drive
   ```

2. Trigger with the new device:
   ```bash
   curl -X POST http://localhost:8000/trigger-rip?device=sr1
   ```

### USB Storage Path

The default output path is `/media/usb`. To change it:

1. Edit the mount path in `docker-compose.yml`
2. Update the path in `backend/main.py` (search for `/media/usb`)

## Technical Details

### Core Technologies

- **FastAPI**: Modern, fast Python web framework for building APIs
- **Uvicorn**: ASGI server for running the FastAPI application
- **cdparanoia**: High-quality CD audio extraction tool
- **discid**: Python library for reading CD disc IDs
- **musicbrainzngs**: Python client for MusicBrainz metadata database
- **threading**: Python threading for concurrency control and async operations

### Key Implementation Features

1. **Thread-Safe Operations**: Uses `threading.Lock()` to prevent race conditions when managing concurrent rip requests

2. **Asynchronous Ripping**: BackgroundTasks run ripping operations without blocking the HTTP response

3. **Error Handling**: Gracefully handles unrecognized CDs, missing metadata, and unavailable storage

4. **Metadata Fallback**: Uses timestamp-based naming when MusicBrainz metadata is unavailable

5. **Device Management**: Automatically tracks and releases devices to prevent conflicts

## Development

### Adding New Features

When implementing changes, ensure:

1. **Update README.md** with new features, API endpoints, or configuration options
2. **Add English comments** to all new or modified Python code explaining logic and purpose
3. **Test thoroughly** before committing changes
4. **Follow the code style** established in existing files

See `.github/github-instructions.md` for detailed contribution guidelines (local development only).

### Running Tests

```bash
# (Test suite to be implemented)
```

### Debugging

Enable verbose logging by modifying the Dockerfile:

```dockerfile
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--log-level", "debug"]
```

## Troubleshooting

### CD Drive Not Detected
- Verify CD drive is mounted and accessible: `ls -la /dev/sr0`
- Check device mapping in `docker-compose.yml`
- Ensure CD is properly inserted and recognized by the system

### USB Storage Not Available
- Verify USB mount at `/media/usb`: `mount | grep media`
- Check file permissions: `ls -la /media/usb`
- Ensure USB device has sufficient free space

### MusicBrainz Lookup Fails
- Verify internet connection
- Check MusicBrainz service availability
- The system will fall back to timestamp-based naming if metadata is unavailable
- Review application logs: `docker-compose logs -f riprobot-backend`

### Container Won't Start
- Check logs: `docker-compose logs riprobot-backend`
- Verify Docker Compose syntax: `docker-compose config`
- Ensure all required volumes and devices are accessible

## Performance Considerations

- **Ripping Speed**: Depends on CD quality and drive speed (typically 2-10 minutes per CD)
- **Memory Usage**: Minimal, typically <100MB per operation
- **CPU Usage**: Low during metadata retrieval, moderate during extraction
- **Network**: Required only for MusicBrainz lookups

## Security Notes

- The application runs with access to the host's CD drive and file system
- Ensure proper file permissions on USB storage
- Do not expose the API to untrusted networks without authentication
- Consider using a reverse proxy (nginx) for production deployments

## License

This project is licensed under the MIT License. See the [LICENSE](LICENSE) file for details.

## Support & Contributing

For issues, feature requests, or contributions:

1. Check existing issues on GitHub
2. Follow the contribution guidelines in `.github/github-instructions.md`
3. Submit pull requests with clear descriptions and updated documentation

## Changelog

### Version 2.0.0 (Current)
- Initial release with FastAPI backend
- MusicBrainz metadata integration
- Docker containerization
- Asynchronous ripping operations
- REST API for triggering rips

---

**Last Updated**: August 30, 2026  
**Maintainer**: RipRobot Contributors
