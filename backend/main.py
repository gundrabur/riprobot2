from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import tempfile
import time
import os
import json
from datetime import datetime
import threading
import discid
import musicbrainzngs
import re
import socket

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=wait_for_startup_hardware, daemon=True).start()
    yield

app = FastAPI(title="RipRobot2 API", lifespan=lifespan)

# CORS für das Frontend erlauben
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SETTINGS_FILE = "settings.json"
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
STARTUP_EJECT_MARKER = "/tmp/riprobot-startup-eject-boot-id"
lock = threading.Lock()
musicbrainzngs.set_useragent("RipRobot", "2.0", "AUDIO-RipRobot2@local.host")

# Standard-Einstellungen
default_settings = {
    "format": "FLAC",           # FLAC, MP3, WAV
    "output_path": "/media/usb",
    "paranoia_mode": "safe",    # safe (-B), fast (-B -Y), disable (-B -Z)
    "network_timeout": 60,
    "rip_timeout": 3600
}

# Live-Status für das Web-Interface
current_status = {
    "state": "idle", # idle, ripping, metadata, converting, success, error
    "message": "Bereit. Lege eine CD ein.",
    "message_key": "status.ready",
    "message_params": {},
    "artist": "",
    "album": "",
    "progress": 0
}

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return {**default_settings, **json.load(f)}
        except:
            pass
    return default_settings.copy()

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

class SettingsModel(BaseModel):
    format: str
    output_path: str
    paranoia_mode: str
    network_timeout: int
    rip_timeout: int

def update_status(state, message, artist=None, album=None, progress=0, message_key=None, message_params=None):
    global current_status
    current_status["state"] = state
    current_status["message"] = message
    current_status["message_key"] = message_key
    current_status["message_params"] = message_params or {}
    if artist is not None: current_status["artist"] = artist
    if album is not None: current_status["album"] = album
    current_status["progress"] = max(0, min(100, progress))
    print(f"[{state.upper()}] {message}")

def clean_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def get_boot_id():
    try:
        with open(BOOT_ID_FILE, "r") as file:
            return file.read().strip()
    except OSError:
        return "process"

def output_storage_ready(output_path):
    if not os.path.isdir(output_path) or not os.path.ismount(output_path):
        return False

    try:
        with tempfile.NamedTemporaryFile(prefix=".riprobot-check-", dir=output_path):
            pass
        return True
    except OSError:
        return False

def wait_for_startup_hardware(device_path="/dev/sr0", retry_interval=2):
    boot_id = get_boot_id()
    try:
        with open(STARTUP_EJECT_MARKER, "r") as file:
            if file.read().strip() == boot_id:
                return
    except OSError:
        pass

    print("[STARTUP] Warte auf CD-Laufwerk und beschreibbaren USB-Stick...")
    while True:
        output_path = load_settings()["output_path"]
        if os.path.exists(device_path) and output_storage_ready(output_path):
            with lock:
                if current_status["state"] == "idle":
                    try:
                        subprocess.run(["eject", device_path], check=True, capture_output=True)
                        with open(STARTUP_EJECT_MARKER, "w") as file:
                            file.write(boot_id)
                        update_status("idle", "System bereit. Bitte CD einlegen.", "", "", 0, "status.systemReady")
                        return
                    except (OSError, subprocess.CalledProcessError) as error:
                        print(f"[STARTUP] Laufwerk noch nicht bereit: {error}")
        time.sleep(retry_interval)

def start_ripping(device_path: str):
    settings = load_settings()
    
    update_status("metadata", "Lese Disc und warte 5 Sekunden...", message_key="status.readingDisc")
    time.sleep(5)
    
    if not os.path.exists(settings["output_path"]):
        update_status("error", f"Zielverzeichnis {settings['output_path']} nicht gefunden!", message_key="status.outputMissing", message_params={"path": settings["output_path"]})
        subprocess.run(["eject", device_path])
        return
        
    artist, album = "Unknown Artist", "Unknown Album"
    track_titles = []
    track_sectors = []
    
    try:
        update_status("metadata", "Suche Metadaten auf MusicBrainz...", message_key="status.searchingMetadata")
        socket.setdefaulttimeout(settings["network_timeout"])
        disc = discid.read(device_path)
        track_sectors = [track.sectors for track in disc.tracks]
        result = musicbrainzngs.get_releases_by_discid(disc.id, includes=["artists", "recordings"])
        
        if "disc" in result and result["disc"].get("release-list"):
            release = result["disc"]["release-list"][0]
            artist = release.get("artist-credit-phrase", "Unknown Artist")
            album = release.get("title", "Unknown Album")
            
            if "medium-list" in release and len(release["medium-list"]) > 0:
                medium = release["medium-list"][0]
                if "track-list" in medium:
                    for track in medium["track-list"]:
                        title = track.get("title") or track.get("recording", {}).get("title", "Track")
                        track_titles.append(title)
    except Exception as e:
        print(f"MusicBrainz Fehler: {e}")

    update_status("ripping", "Starte Rip-Vorgang...", artist, album, message_key="status.startingRip")

    folder_name = f"{clean_filename(artist)} - {clean_filename(album)}" if artist != "Unknown Artist" else f"rip_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    rip_dir = os.path.join(settings["output_path"], folder_name)
    
    if os.path.exists(rip_dir):
        rip_dir += f"_{datetime.now().strftime('%H%M%S')}"
        
    os.makedirs(rip_dir, exist_ok=True)
    
    try:
        # Ripping Parameter basierend auf Settings
        rip_args = ["cdparanoia", "-d", device_path, "-B"]
        if settings["paranoia_mode"] == "fast": rip_args.append("-Y")
        elif settings["paranoia_mode"] == "disable": rip_args.append("-Z")

        total_tracks_expected = len(track_sectors) or len(track_titles) or 1
        total_sectors = sum(track_sectors)
        
        # Popen führt den Befehl im Hintergrund aus, blockiert Python aber nicht
        process = subprocess.Popen(rip_args, cwd=rip_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        start_time = time.time()
        # Schleife läuft, solange cdparanoia noch arbeitet (poll() ist None)
        while process.poll() is None:
            # Timeout-Schutz manuell überwachen
            if time.time() - start_time > settings["rip_timeout"]:
                process.kill()
                raise subprocess.TimeoutExpired(process.args, settings["rip_timeout"])
            
            wav_files = sorted(f for f in os.listdir(rip_dir) if f.endswith(".wav"))
            current_track = min(max(len(wav_files), 1), total_tracks_expected)
            track_title = track_titles[current_track - 1] if current_track <= len(track_titles) else f"Track {current_track:02d}"
            
            if total_sectors > 0:
                written_sectors = sum(max(0, os.path.getsize(os.path.join(rip_dir, filename)) - 44) // 2352 for filename in wav_files)
                prog = min(99, int((written_sectors / total_sectors) * 100))
            else:
                completed_tracks = max(0, len(wav_files) - 1)
                prog = min(99, int((completed_tracks / total_tracks_expected) * 100))

            update_status("ripping", f"Rippe Track {current_track} von {total_tracks_expected}: {track_title}", artist, album, prog, "status.rippingTrack", {"current": current_track, "total": total_tracks_expected, "title": track_title})
            
            time.sleep(2) # Alle 2 Sekunden aktualisieren
        
        if process.returncode == 0:
            wav_files = sorted([f for f in os.listdir(rip_dir) if f.endswith(".wav")])
            total_files = len(wav_files)
            
            if settings["format"] in ["FLAC", "MP3"]:
                update_status("converting", f"Konvertiere {total_files} Dateien in {settings['format']}...", message_key="status.convertingFiles", message_params={"count": total_files, "format": settings["format"]})
                
                for idx, filename in enumerate(wav_files):
                    track_num = idx + 1
                    tag_title = track_titles[idx] if idx < len(track_titles) else f"Track {track_num:02d}"
                    clean_title = clean_filename(tag_title)
                    
                    old_path = os.path.join(rip_dir, filename)
                    
                    if settings["format"] == "FLAC":
                        new_path = os.path.join(rip_dir, f"{track_num:02d} - {clean_title}.flac")
                        cmd = ["flac", "-8", "-T", f"ARTIST={artist}", "-T", f"ALBUM={album}", "-T", f"TITLE={tag_title}", "-T", f"TRACKNUMBER={track_num}", old_path, "-o", new_path]
                    
                    elif settings["format"] == "MP3":
                        new_path = os.path.join(rip_dir, f"{track_num:02d} - {clean_title}.mp3")
                        # MP3 mit höchster VBR Qualität (V0)
                        cmd = ["ffmpeg", "-i", old_path, "-codec:a", "libmp3lame", "-qscale:a", "0", "-metadata", f"artist={artist}", "-metadata", f"album={album}", "-metadata", f"title={tag_title}", "-metadata", f"track={track_num}", new_path]
                    
                    subprocess.run(cmd, capture_output=True)
                    os.remove(old_path) # WAV löschen
                    update_status("converting", f"Konvertiere in {settings['format']}...", artist, album, int(((idx+1)/total_files)*100), "status.converting", {"format": settings["format"]})
            else:
                # Nur umbenennen, wenn WAV gewünscht
                for idx, filename in enumerate(wav_files):
                    track_num = idx + 1
                    tag_title = track_titles[idx] if idx < len(track_titles) else f"Track {track_num:02d}"
                    new_path = os.path.join(rip_dir, f"{track_num:02d} - {clean_filename(tag_title)}.wav")
                    os.rename(os.path.join(rip_dir, filename), new_path)

            # --- NEU: Warten bis alles physisch auf dem USB-Stick ist ---
            update_status("converting", "Speichere Daten final auf USB (Bitte warten)...", artist, album, 99, "status.syncingUsb")
            os.sync() # Zwingt Linux, den Cache komplett auf den Stick zu leeren
            # -------------------------------------------------------------

            update_status("success", "Vorgang erfolgreich abgeschlossen!", artist, album, 100, "status.success")
        else:
            update_status("error", "Fehler beim Rippen der CD.", message_key="status.ripError")
            
    except subprocess.TimeoutExpired:
        update_status("error", "Timeout! CD konnte nicht gelesen werden.", message_key="status.timeout")
    except Exception as e:
        update_status("error", f"Unerwarteter Fehler: {e}", message_key="status.unexpectedError", message_params={"error": str(e)})
        
    finally:
        subprocess.run(["eject", device_path])
        # Reset status after 10 seconds
        threading.Timer(10.0, lambda: update_status("idle", "Bereit. Lege eine CD ein.", "", "", 0, "status.ready")).start()

# --- API ENDPUNKTE ---

@app.get("/api/status")
def get_status():
    return current_status

@app.get("/api/settings")
def get_settings():
    return load_settings()

@app.post("/api/settings")
def update_settings(new_settings: SettingsModel):
    save_settings(new_settings.model_dump())
    return {"message": "Settings gespeichert"}

@app.post("/trigger-rip")
def trigger_rip(background_tasks: BackgroundTasks, device: str = "sr0"):
    with lock:
        if current_status["state"] != "idle":
            return {"message": "System ist beschäftigt"}
        update_status("metadata", "Rip-Vorgang wird gestartet...", "", "", 0, "status.queueingRip")
        background_tasks.add_task(start_ripping, f"/dev/{device}")
    return {"message": "Rip getriggert"}

@app.post("/eject")
def eject_drive(device: str = "sr0"):
    with lock:
        if current_status["state"] != "idle":
            raise HTTPException(status_code=409, detail="Auswerfen während eines laufenden Vorgangs nicht möglich")

        try:
            subprocess.run(["eject", f"/dev/{device}"], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise HTTPException(status_code=500, detail="Laufwerk konnte nicht ausgeworfen werden") from error

    return {"message": "Laufwerk ausgeworfen"}

# Frontend Mount (muss am Ende stehen!)
os.makedirs("static", exist_ok=True)
app.mount("/", StaticFiles(directory="static", html=True), name="static")