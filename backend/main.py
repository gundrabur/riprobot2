from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import tempfile
import shutil
import time
import os
import json
from datetime import datetime
import threading
import discid
import musicbrainzngs
import re
import socket
import uuid
import platform

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
HISTORY_FILE = "history.json"
MAX_HISTORY_ENTRIES = 200
BOOT_ID_FILE = "/proc/sys/kernel/random/boot_id"
STARTUP_EJECT_MARKER = "/tmp/riprobot-startup-eject-boot-id"
APP_VERSION = "2.0.0"
APP_DEVELOPER = "Christian Möller"
APP_COPYRIGHT_YEAR = 2026
lock = threading.Lock()
history_lock = threading.Lock()
musicbrainzngs.set_useragent("RipRobot", "2.0", "AUDIO-RipRobot2@local.host")

# Standard-Einstellungen
default_settings = {
    "format": "FLAC",           # FLAC, MP3, WAV
    "output_path": "/media/usb",
    "paranoia_mode": "safe",    # safe (-B), fast (-B -Y), disable (-B -Z)
    "network_timeout": 60,
    "rip_timeout": 3600,
    "enable_speed_chart": False # Live-Diagramm der Lesegeschwindigkeit
}

# Live-Status für das Web-Interface
current_status = {
    "state": "idle", # idle, ripping, metadata, converting, success, error
    "message": "Bereit. Lege eine CD ein.",
    "message_key": "status.ready",
    "message_params": {},
    "artist": "",
    "album": "",
    "progress": 0,
    "speed_mbps": 0,
    "speed_history": [] # Liste von {"t": Sekunden, "mbps": Wert} fuer das Live-Diagramm
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

def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []

def save_history(history):
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2)

def add_history_entry(entry):
    with history_lock:
        history = load_history()
        history.insert(0, entry) # neueste zuerst
        save_history(history[:MAX_HISTORY_ENTRIES])

class SettingsModel(BaseModel):
    format: str
    output_path: str
    paranoia_mode: str
    network_timeout: int
    rip_timeout: int
    enable_speed_chart: bool = False

def update_status(state, message, artist=None, album=None, progress=0, message_key=None, message_params=None):
    global current_status
    current_status["state"] = state
    current_status["message"] = message
    current_status["message_key"] = message_key
    current_status["message_params"] = message_params or {}
    if artist is not None: current_status["artist"] = artist
    if album is not None: current_status["album"] = album
    current_status["progress"] = max(0, min(100, progress))
    if state == "idle":
        current_status["speed_mbps"] = 0
        current_status["speed_history"] = []
    print(f"[{state.upper()}] {message}")

def clean_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

# cdparanoia liest per SCSI-Passthrough (ioctl), das umgeht die /sys/block-Iostat-Zähler.
# Daher wird zunächst nach RAM (tmpfs) gerippt: Schreiben dorthin ist quasi verzögerungsfrei,
# wodurch das Dateiwachstum dort ausschließlich die Laufwerks-Lesegeschwindigkeit widerspiegelt.
def get_ram_rip_dir():
    shm = "/dev/shm"
    try:
        if os.path.isdir(shm) and shutil.disk_usage(shm).free > 900 * 1024 * 1024:
            return tempfile.mkdtemp(prefix="riprobot_", dir=shm)
    except OSError:
        pass
    return None # Kein/zu wenig RAM verfügbar -> Fallback: direkt auf das Zielverzeichnis rippen

def get_hardware_model():
    try:
        with open("/proc/device-tree/model", "r") as f:
            return f.read().strip("\x00").strip()
    except OSError:
        pass
    try:
        with open("/proc/cpuinfo", "r") as f:
            for line in f:
                if line.startswith("Model"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.machine() or None

def get_ram_info():
    try:
        meminfo = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    meminfo[parts[0].strip()] = int(parts[1].strip().split()[0]) # kB
        return {
            "total_mb": round(meminfo.get("MemTotal", 0) / 1024, 1),
            "available_mb": round(meminfo.get("MemAvailable", 0) / 1024, 1),
        }
    except (OSError, ValueError, IndexError):
        return {"total_mb": None, "available_mb": None}

def get_optical_drive_info(device_path):
    device_name = os.path.basename(device_path)
    info = {"device": device_path, "present": os.path.exists(device_path), "model": None}
    try:
        with open(f"/sys/block/{device_name}/device/model", "r") as f:
            info["model"] = f.read().strip()
    except OSError:
        pass
    return info

def get_usb_stick_info(output_path):
    info = {"path": output_path, "mounted": False, "total_gb": None, "free_gb": None}
    try:
        info["mounted"] = os.path.ismount(output_path)
        usage = shutil.disk_usage(output_path)
        info["total_gb"] = round(usage.total / (1024 ** 3), 1)
        info["free_gb"] = round(usage.free / (1024 ** 3), 1)
    except OSError:
        pass
    return info

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
    final_dir = os.path.join(settings["output_path"], folder_name)
    
    if os.path.exists(final_dir):
        final_dir += f"_{datetime.now().strftime('%H%M%S')}"
        
    os.makedirs(final_dir, exist_ok=True)

    # Erst nach RAM rippen (siehe get_ram_rip_dir), sonst direkt auf den Stick als Fallback
    ram_dir = get_ram_rip_dir()
    rip_dir = ram_dir or final_dir
    session_start = time.time()
    total_files = 0

    def record_history(status, message_key):
        add_history_entry({
            "id": uuid.uuid4().hex,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "artist": artist,
            "album": album,
            "format": settings["format"],
            "status": status,
            "message_key": message_key,
            "track_count": total_files or total_tracks_expected,
            "duration_sec": round(time.time() - session_start, 1),
            "speed_history": current_status.get("speed_history", []),
        })

    try:
        # Ripping Parameter basierend auf Settings
        rip_args = ["cdparanoia", "-d", device_path, "-B"]
        if settings["paranoia_mode"] == "fast": rip_args.append("-Y")
        elif settings["paranoia_mode"] == "disable": rip_args.append("-Z")

        total_tracks_expected = len(track_sectors) or len(track_titles) or 1
        total_sectors = sum(track_sectors)
        
        # Popen führt den Befehl im Hintergrund aus, blockiert Python aber nicht
        process = subprocess.Popen(rip_args, cwd=rip_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        speed_chart_enabled = settings.get("enable_speed_chart", False)
        current_status["speed_mbps"] = 0
        current_status["speed_history"] = []
        last_bytes = 0
        last_sample_time = time.time()

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

            if speed_chart_enabled:
                total_bytes = sum(os.path.getsize(os.path.join(rip_dir, filename)) for filename in wav_files)
                now = time.time()
                elapsed = now - last_sample_time
                if elapsed > 0:
                    mbps = max(0, (total_bytes - last_bytes) / elapsed) / (1024 * 1024)
                    current_status["speed_mbps"] = round(mbps, 2)
                    current_status["speed_history"].append({"t": round(now - start_time, 1), "mbps": round(mbps, 2)})
                    current_status["speed_history"] = current_status["speed_history"][-150:] # Historie begrenzen
                last_bytes = total_bytes
                last_sample_time = now

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

            # --- Fertige Dateien vom RAM-Zwischenspeicher auf den USB-Stick verschieben ---
            update_status("converting", "Speichere Daten final auf USB (Bitte warten)...", artist, album, 99, "status.syncingUsb")
            if ram_dir:
                for filename in os.listdir(ram_dir):
                    shutil.move(os.path.join(ram_dir, filename), os.path.join(final_dir, filename))
            
            os.sync() # Zwingt Linux, den Cache komplett auf den Stick zu leeren
            # -------------------------------------------------------------

            update_status("success", "Vorgang erfolgreich abgeschlossen!", artist, album, 100, "status.success")
            record_history("success", "status.success")
        else:
            update_status("error", "Fehler beim Rippen der CD.", message_key="status.ripError")
            record_history("error", "status.ripError")
            
    except subprocess.TimeoutExpired:
        update_status("error", "Timeout! CD konnte nicht gelesen werden.", message_key="status.timeout")
        record_history("error", "status.timeout")
    except Exception as e:
        update_status("error", f"Unerwarteter Fehler: {e}", message_key="status.unexpectedError", message_params={"error": str(e)})
        record_history("error", "status.unexpectedError")
        
    finally:
        if ram_dir:
            shutil.rmtree(ram_dir, ignore_errors=True) # RAM immer freigeben, auch bei Fehlern/Timeout
        subprocess.run(["eject", device_path])
        # Reset status after 10 seconds
        threading.Timer(10.0, lambda: update_status("idle", "Bereit. Lege eine CD ein.", "", "", 0, "status.ready")).start()

# --- API ENDPUNKTE ---

@app.get("/api/status")
def get_status():
    return {**current_status, "speed_chart_enabled": load_settings().get("enable_speed_chart", False)}

@app.get("/api/system-info")
def get_system_info():
    settings = load_settings()
    return {
        "version": APP_VERSION,
        "developer": APP_DEVELOPER,
        "copyright_year": APP_COPYRIGHT_YEAR,
        "os": platform.platform(),
        "hardware_model": get_hardware_model(),
        "ram": get_ram_info(),
        "optical_drive": get_optical_drive_info("/dev/sr0"),
        "usb_stick": get_usb_stick_info(settings["output_path"]),
    }

@app.get("/api/settings")
def get_settings():
    return load_settings()

@app.post("/api/settings")
def update_settings(new_settings: SettingsModel):
    save_settings(new_settings.model_dump())
    return {"message": "Settings gespeichert"}

@app.get("/api/history")
def get_history():
    return load_history()

@app.delete("/api/history/{entry_id}")
def delete_history_entry(entry_id: str):
    with history_lock:
        history = load_history()
        filtered = [entry for entry in history if entry.get("id") != entry_id]
        if len(filtered) == len(history):
            raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
        save_history(filtered)
    return {"message": "Eintrag gelöscht"}

@app.delete("/api/history")
def clear_history():
    with history_lock:
        save_history([])
    return {"message": "Verlauf geleert"}

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

class NoCacheStaticFiles(StaticFiles):
    """Verhindert Browser-Caching, damit Frontend-Updates sofort ohne Hard-Refresh sichtbar sind."""
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

# Frontend Mount (muss am Ende stehen!)
os.makedirs("static", exist_ok=True)
app.mount("/", NoCacheStaticFiles(directory="static", html=True), name="static")