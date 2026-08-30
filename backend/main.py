# FastAPI framework for creating REST API endpoints
# BackgroundTasks allows running long-running tasks without blocking the HTTP response
from fastapi import FastAPI, BackgroundTasks

# subprocess: Execute system commands like cdparanoia, flac, and eject
import subprocess

# time: Used for introducing delays (e.g., wait for CD to settle)
import time

# os: File system operations (checking paths, creating directories, renaming/deleting files)
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

# socket: Set global network timeouts to prevent API calls from hanging indefinitely
import socket

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
    5. Executing cdparanoia to extract audio tracks (with global timeout)
    6. Converting WAV to FLAC and embedding metadata tags
    7. Ejecting the CD after completion or failure
    """
    # Initial delay to allow CD to stabilize and be fully recognized by the system
    print(f"Audio-CD in Laufwerk {device_path} erkannt! Warte 5 Sekunden...")
    time.sleep(5)
    
    # Verify that USB storage destination is available and mounted
    if not os.path.exists("/media/usb"):
        print("FEHLER: Kein USB-Stick unter /media/usb gefunden! Breche ab.")
        subprocess.run(["eject", device_path])
        with lock:
            if device_path in active_rips:
                active_rips.remove(device_path)
        return
        
    # Retrieve album metadata and tracklist from MusicBrainz using the CD's unique disc ID
    print("Lese Disc-ID und suche auf MusicBrainz...")
    artist, album = None, None
    track_titles = []
    
    try:
        # Set a 60-second timeout for network requests to allow slow or overloaded APIs time to respond
        socket.setdefaulttimeout(60)

        # Read the unique identifier of the CD from the physical disc
        disc = discid.read(device_path)
        # Query MusicBrainz database for this disc ID, requesting artist and tracklist information
        result = musicbrainzngs.get_releases_by_discid(disc.id, includes=["artists", "recordings"])
        
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
    except Exception as e:
        print(f"Keine Metadaten gefunden oder Zeitüberschreitung ({e}). Nutze Fallback-Namen.")

    # Set fallback values for metadata if MusicBrainz failed (used for tags)
    tag_artist = artist if artist else "Unknown Artist"
    tag_album = album if album else "Unknown Album"

    # Generate output directory name
    if artist and album:
        folder_name = f"{clean_filename(artist)} - {clean_filename(album)}"
    else:
        folder_name = f"rip_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        
    rip_dir = f"/media/usb/{folder_name}"
    
    # Append timestamp to ensure unique directory names if it already exists
    if os.path.exists(rip_dir):
        rip_dir += f"_{datetime.now().strftime('%H%M%S')}"
        
    os.makedirs(rip_dir, exist_ok=True)
    
    print(f"Starte Ripping-Prozess in {rip_dir} (Timeout: 1 Stunde)...")
    
    try:
        # Execute the actual CD ripping operation with cdparanoia
        result = subprocess.run(
            ["cdparanoia", "-d", device_path, "-B"],
            cwd=rip_dir,
            capture_output=True,
            text=True,
            timeout=3600
        )
        
        if result.returncode == 0:
            print("Ripping erfolgreich abgeschlossen! Starte FLAC-Konvertierung und Tagging...")
            
            # Iterate through all ripped WAV files for conversion
            for filename in sorted(os.listdir(rip_dir)):
                if filename.endswith(".wav"):
                    # Extract track number from generic cdparanoia filename
                    match = re.search(r'track(\d+)', filename, re.IGNORECASE)
                    if match:
                        track_num = int(match.group(1))
                        
                        # Determine track title for filename and tags
                        if track_titles and 1 <= track_num <= len(track_titles):
                            tag_title = track_titles[track_num - 1]
                        else:
                            tag_title = f"Track {track_num:02d}"
                            
                        clean_title = clean_filename(tag_title)
                        new_filename = f"{track_num:02d} - {clean_title}.flac"
                        
                        old_path = os.path.join(rip_dir, filename)
                        new_path = os.path.join(rip_dir, new_filename)
                        
                        print(f"Konvertiere {filename} zu FLAC...")
                        
                        # Convert to FLAC and embed metadata tags
                        # -8 specifies highest compression level (smallest file size, no quality loss)
                        flac_cmd = [
                            "flac", "-8",
                            "-T", f"ARTIST={tag_artist}",
                            "-T", f"ALBUM={tag_album}",
                            "-T", f"TITLE={tag_title}",
                            "-T", f"TRACKNUMBER={track_num}",
                            old_path,
                            "-o", new_path
                        ]
                        
                        flac_result = subprocess.run(flac_cmd, capture_output=True, text=True)
                        
                        if flac_result.returncode == 0:
                            # Delete original WAV file to save space on USB drive
                            os.remove(old_path)
                            print(f"Erfolgreich getaggt: {new_filename}")
                        else:
                            print(f"Fehler bei FLAC-Konvertierung von {filename}: {flac_result.stderr}")
                            
            print("Alle Dateien wurden erfolgreich verarbeitet!")
        else:
            print("Fehler beim Rippen (möglicherweise ungültiges Medium):", result.stderr)
            
    except subprocess.TimeoutExpired:
        print(f"KRITISCHER FEHLER: Ripping-Timeout (1 Stunde) überschritten! Breche Prozess hart ab.")
    
    except Exception as e:
        print(f"Unerwarteter Fehler während des Ripping-Vorgangs: {e}")
        
    finally:
        print(f"Werfe CD aus {device_path} aus...")
        subprocess.run(["eject", device_path])
        
        with lock:
            if device_path in active_rips:
                active_rips.remove(device_path)
        print(f"Laufwerk {device_path} ist wieder bereit.")

@app.get("/")
def read_root():
    """Return API status indicating the service is operational."""
    return {"status": "RipRobot2 bereit (Mit FLAC-Tagging)!"}

@app.post("/trigger-rip")
def trigger_rip(background_tasks: BackgroundTasks, device: str = "sr0"):
    device_path = f"/dev/{device}"
    with lock:
        if device_path in active_rips:
            print(f"Ignoriere Trigger: {device_path} wird bereits bearbeitet.")
            return {"message": "Rip läuft bereits"}
        active_rips.add(device_path)
        
    background_tasks.add_task(start_ripping, device_path)
    return {"message": f"Rip für {device_path} erfolgreich getriggert"}