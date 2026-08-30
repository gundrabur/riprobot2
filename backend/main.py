from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import subprocess
import time
import os
import json
from datetime import datetime
import threading
import discid
import musicbrainzngs
import re
import socket

app = FastAPI(title="RipRobot2 API")

# CORS für das Frontend erlauben
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SETTINGS_FILE = "settings.json"
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

def update_status(state, message, artist="", album="", progress=0):
    global current_status
    current_status["state"] = state
    current_status["message"] = message
    if artist: current_status["artist"] = artist
    if album: current_status["album"] = album
    if progress > 0: current_status["progress"] = progress
    print(f"[{state.upper()}] {message}")

def clean_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def start_ripping(device_path: str):
    settings = load_settings()
    
    update_status("metadata", "Lese Disc und warte 5 Sekunden...")
    time.sleep(5)
    
    if not os.path.exists(settings["output_path"]):
        update_status("error", f"Zielverzeichnis {settings['output_path']} nicht gefunden!")
        subprocess.run(["eject", device_path])
        return
        
    artist, album = "Unknown Artist", "Unknown Album"
    track_titles = []
    
    try:
        update_status("metadata", "Suche Metadaten auf MusicBrainz...")
        socket.setdefaulttimeout(settings["network_timeout"])
        disc = discid.read(device_path)
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

    update_status("ripping", f"Starte Rip-Vorgang...", artist, album)

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

        # Erwartete Tracks für die Prozentrechnung (Fallback 1, falls keine Metadaten gefunden)
        total_tracks_expected = len(track_titles) if track_titles else 1
        
        # Popen führt den Befehl im Hintergrund aus, blockiert Python aber nicht
        process = subprocess.Popen(rip_args, cwd=rip_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        start_time = time.time()
        # Schleife läuft, solange cdparanoia noch arbeitet (poll() ist None)
        while process.poll() is None:
            # Timeout-Schutz manuell überwachen
            if time.time() - start_time > settings["rip_timeout"]:
                process.kill()
                raise subprocess.TimeoutExpired(process.args, settings["rip_timeout"])
            
            # Zähle die bisher generierten WAV-Dateien im Ordner
            current_wavs = len([f for f in os.listdir(rip_dir) if f.endswith(".wav")])
            
            if total_tracks_expected > 0:
                # Maximal auf 99% deckeln, die echten 100% kommen erst beim Konvertieren
                prog = min(99, int((current_wavs / total_tracks_expected) * 100))
                update_status("ripping", f"Rippe Track {current_wavs + 1} von {total_tracks_expected}...", artist, album, prog)
            
            time.sleep(2) # Alle 2 Sekunden aktualisieren
        
        if process.returncode == 0:
            wav_files = sorted([f for f in os.listdir(rip_dir) if f.endswith(".wav")])
            total_files = len(wav_files)
            
            if settings["format"] in ["FLAC", "MP3"]:
                update_status("converting", f"Konvertiere {total_files} Dateien in {settings['format']}...")
                
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
                    update_status("converting", f"Konvertiere in {settings['format']}...", artist, album, int(((idx+1)/total_files)*100))
            else:
                # Nur umbenennen, wenn WAV gewünscht
                for idx, filename in enumerate(wav_files):
                    track_num = idx + 1
                    tag_title = track_titles[idx] if idx < len(track_titles) else f"Track {track_num:02d}"
                    new_path = os.path.join(rip_dir, f"{track_num:02d} - {clean_filename(tag_title)}.wav")
                    os.rename(os.path.join(rip_dir, filename), new_path)

            update_status("success", "Vorgang erfolgreich abgeschlossen!", artist, album)
        else:
            update_status("error", "Fehler beim Rippen der CD.")
            
    except subprocess.TimeoutExpired:
        update_status("error", "Timeout! CD konnte nicht gelesen werden.")
    except Exception as e:
        update_status("error", f"Unerwarteter Fehler: {e}")
        
    finally:
        subprocess.run(["eject", device_path])
        # Reset status after 10 seconds
        threading.Timer(10.0, lambda: update_status("idle", "Bereit. Lege eine CD ein.", "", "", 0)).start()

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
    if current_status["state"] != "idle":
        return {"message": "System ist beschäftigt"}
    background_tasks.add_task(start_ripping, f"/dev/{device}")
    return {"message": "Rip getriggert"}

# Frontend Mount (muss am Ende stehen!)
os.makedirs("static", exist_ok=True)
app.mount("/", StaticFiles(directory="static", html=True), name="static")