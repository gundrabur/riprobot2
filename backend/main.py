# FastAPI framework for creating REST API endpoints
# BackgroundTasks allows running long-running tasks without blocking the HTTP response
from fastapi import FastAPI, BackgroundTasks

# subprocess: Execute system commands like cdparanoia and eject
import subprocess

# time: Used for introducing delays (e.g., wait for CD to settle)
import time

# os: File system operations (checking paths, creating directories, renaming files)
import os

# datetime: Generate timestamps for organizing ripped files
from datetime import datetime

# threading: Enable thread-safe operations for managing concurrent rip requests
import threading

# discid: Read the unique identifier of an audio CD from a drive
import discid

# musicbrainzngs: Query MusicBrainz database to retrieve album and artist metadata
import musicbrainzngs

# re: Regular expressions for validating and cleaning filenames
import re

# Initialize FastAPI application
app = FastAPI(title="RipRobot2 API")

# Set to track all CD drives currently being ripped to prevent simultaneous operations on the same device
active_rips = set()

# Thread-safe lock to ensure atomic operations on active_rips set (prevent race conditions)
lock = threading.Lock()

# MusicBrainz API requires a User-Agent header to identify the client application
# This must be set before making any requests to the service
musicbrainzngs.set_useragent("RipRobot", "2.0", "AUDIO-RipRobot2@local.host")

def clean_filename(name):
    """
    Sanitize a string to be safe for use as a filename on all filesystems.
    Removes characters that are invalid or reserved in most operating systems:
    backslash, forward slash, asterisk, question mark, colon, quotes, angle brackets, pipe.
    Also strips leading/trailing whitespace.
    """
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def start_ripping(device_path: str):
    """
    Main ripping function. Handles the complete workflow of:
    1. Waiting for the CD to settle in the drive
    2. Verifying USB storage is available
    3. Retrieving album metadata and tracklist from MusicBrainz
    4. Creating organized output directory
    5. Executing cdparanoia to extract audio tracks
    6. Renaming extracted WAV files to actual track titles
    7. Ejecting the CD after completion
    """
    # Initial delay to allow CD to stabilize and be fully recognized by the system
    print(f"Audio-CD in Laufwerk {device_path} erkannt! Warte 5 Sekunden...")
    time.sleep(5)
    
    # Verify that USB storage destination is available and mounted
    # This ensures the ripped audio will have a valid output location
    if not os.path.exists("/media/usb"):
        print("FEHLER: Kein USB-Stick unter /media/usb gefunden! Breche ab.")
        # Eject the CD if USB storage is not available
        subprocess.run(["eject", device_path])
        # Clean up: remove device from active rips list in a thread-safe manner
        with lock:
            if device_path in active_rips:
                active_rips.remove(device_path)
        return
        
    # Retrieve album metadata and tracklist from MusicBrainz using the CD's unique disc ID
    # Requesting "recordings" includes individual track titles for renaming WAV files
    print("Lese Disc-ID und suche auf MusicBrainz...")
    artist, album = None, None
    track_titles = []
    
    try:
        # Read the unique identifier of the CD from the physical disc
        disc = discid.read(device_path)
        # Query MusicBrainz database for this disc ID, requesting artist and tracklist information
        result = musicbrainzngs.get_releases_by_discid(disc.id, includes=["artists", "recordings"])
        # Extract artist, album, and track titles from the first matching release
        if "disc" in result and result["disc"].get("release-list"):
            release = result["disc"]["release-list"][0]
            artist = release.get("artist-credit-phrase", "Unknown Artist")
            album = release.get("title", "Unknown Album")
            
            # Extract individual track titles from the release's tracklist
            if "medium-list" in release and len(release["medium-list"]) > 0:
                medium = release["medium-list"][0]
                if "track-list" in medium:
                    for track in medium["track-list"]:
                        title = track.get("title") or track.get("recording", {}).get("title", "Unbekannter Track")
                        track_titles.append(title)
                        
            print(f"Erkannt: {artist} - {album} ({len(track_titles)} Tracks gefunden)")
    # Gracefully handle any errors during metadata retrieval (network issues, unrecognized disc, etc.)
    except Exception as e:
        print(f"Keine Metadaten gefunden ({e}). Nutze Fallback-Namen.")

    # Generate output directory name based on metadata availability
    # Prefer descriptive names (Artist - Album) but fall back to timestamp-based names if metadata is unavailable
    if artist and album:
        folder_name = f"{clean_filename(artist)} - {clean_filename(album)}"
    else:
        # Use timestamp as fallback name for unidentified CDs
        folder_name = f"rip_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        
    # Construct the full output directory path on the USB storage
    rip_dir = f"/media/usb/{folder_name}"
    
    # Handle case where directory already exists (e.g., same CD ripped multiple times)
    # Append timestamp to ensure unique directory names
    if os.path.exists(rip_dir):
        rip_dir += f"_{datetime.now().strftime('%H%M%S')}"
        
    # Create the output directory (and any parent directories if needed)
    os.makedirs(rip_dir, exist_ok=True)
    
    print(f"Starte Ripping-Prozess in {rip_dir}...")
    
    # Execute the actual CD ripping operation with error handling
    try:
        # cdparanoia parameters:
        # -d: specify device path to read from
        # -B: batch mode (automatically create numbered track files)
        result = subprocess.run(
            ["cdparanoia", "-d", device_path, "-B"],
            cwd=rip_dir,  # Set output directory for extracted audio files
            capture_output=True,  # Capture stdout and stderr for error checking
            text=True  # Return output as strings instead of bytes
        )
        
        # Check if cdparanoia completed successfully (return code 0 indicates success)
        if result.returncode == 0:
            print("Ripping erfolgreich abgeschlossen! Benenne Tracks um...")
            
            # If track titles were retrieved from MusicBrainz, rename generic WAV files
            if track_titles:
                for filename in sorted(os.listdir(rip_dir)):
                    if filename.endswith(".wav"):
                        # Extract track number from default cdparanoia filenames (e.g., track01.cdda.wav)
                        match = re.search(r'track(\d+)', filename, re.IGNORECASE)
                        if match:
                            track_num = int(match.group(1))
                            # Ensure the track number matches an entry in our retrieved track list
                            if 1 <= track_num <= len(track_titles):
                                clean_title = clean_filename(track_titles[track_num - 1])
                                new_filename = f"{track_num:02d} - {clean_title}.wav"
                                
                                old_path = os.path.join(rip_dir, filename)
                                new_path = os.path.join(rip_dir, new_filename)
                                
                                # Rename file to include track number and sanitized title
                                os.rename(old_path, new_path)
                                print(f"Umbenannt: {filename} -> {new_filename}")
            print("Alle Dateien wurden erfolgreich verarbeitet!")
        else:
            # Log error details from cdparanoia if the operation failed
            print("Fehler beim Rippen:", result.stderr)
            
        # Always eject the CD after ripping attempt (successful or not)
        print(f"Werfe CD aus {device_path} aus...")
        subprocess.run(["eject", device_path])
        
    finally:
        # Cleanup: Mark the device as no longer being ripped, allowing future rip requests
        # Use lock to ensure thread-safe access to the active_rips set
        with lock:
            if device_path in active_rips:
                active_rips.remove(device_path)
        print(f"Laufwerk {device_path} ist wieder bereit.")

# Health check endpoint to verify the API is running
@app.get("/")
def read_root():
    """Return API status indicating the service is operational with MusicBrainz support."""
    return {"status": "RipRobot2 mit MusicBrainz-Integration!"}

# Endpoint to trigger a new CD rip operation
@app.post("/trigger-rip")
def trigger_rip(background_tasks: BackgroundTasks, device: str = "sr0"):
    """
    Initiate an asynchronous CD ripping operation.
    
    Parameters:
    - device: The CD drive device name (default: sr0 for /dev/sr0)
    
    Returns:
    - Status message indicating whether the rip was queued or if one is already in progress
    """
    # Construct full device path from device name
    device_path = f"/dev/{device}"
    
    # Check if this device is already being ripped (concurrency control)
    # Use lock to ensure thread-safe access to active_rips set
    with lock:
        if device_path in active_rips:
            # Reject new rip request if device is already in use
            print(f"Ignoriere Trigger: {device_path} wird bereits bearbeitet.")
            return {"message": "Rip läuft bereits"}
        # Mark this device as active to prevent concurrent operations
        active_rips.add(device_path)
    
    # Queue the ripping operation to run in the background without blocking the HTTP response
    background_tasks.add_task(start_ripping, device_path)
    return {"message": f"Rip für {device_path} erfolgreich getriggert"}