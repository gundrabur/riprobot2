from fastapi import FastAPI, BackgroundTasks
import subprocess
import time
import os
from datetime import datetime
import threading
import discid
import musicbrainzngs
import re

app = FastAPI(title="RipRobot2 API")

active_rips = set()
lock = threading.Lock()

# MusicBrainz verlangt zwingend einen "User-Agent"
musicbrainzngs.set_useragent("RipRobot", "2.0", "dein-name@local.host")

def clean_filename(name):
    """Entfernt ungültige Zeichen für Dateisysteme"""
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def start_ripping(device_path: str):
    print(f"Audio-CD in Laufwerk {device_path} erkannt! Warte 5 Sekunden...")
    time.sleep(5)
    
    if not os.path.exists("/media/usb"):
        print("FEHLER: Kein USB-Stick unter /media/usb gefunden! Breche ab.")
        subprocess.run(["eject", device_path])
        with lock:
            if device_path in active_rips:
                active_rips.remove(device_path)
        return
        
    # --- NEU: Metadaten von MusicBrainz abrufen ---
    print("Lese Disc-ID und suche auf MusicBrainz...")
    artist, album = None, None
    try:
        disc = discid.read(device_path)
        result = musicbrainzngs.get_releases_by_discid(disc.id, includes=["artists"])
        if "disc" in result and result["disc"].get("release-list"):
            release = result["disc"]["release-list"][0]
            artist = release.get("artist-credit-phrase", "Unknown Artist")
            album = release.get("title", "Unknown Album")
            print(f"Erkannt: {artist} - {album}")
    except Exception as e:
        print(f"Keine Metadaten gefunden ({e}). Nutze Fallback-Namen.")

    # Ordnernamen generieren (Band - Album oder Datum)
    if artist and album:
        folder_name = f"{clean_filename(artist)} - {clean_filename(album)}"
    else:
        folder_name = f"rip_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
        
    rip_dir = f"/media/usb/{folder_name}"
    
    # Falls der Ordner schon existiert (z.B. CD doppelt gerippt), hänge Zeitstempel an
    if os.path.exists(rip_dir):
        rip_dir += f"_{datetime.now().strftime('%H%M%S')}"
        
    os.makedirs(rip_dir, exist_ok=True)
    
    print(f"Starte Ripping-Prozess in {rip_dir}...")
    
    try:
        result = subprocess.run(
            ["cdparanoia", "-d", device_path, "-B"],
            cwd=rip_dir,
            capture_output=True,
            text=True
        )
        
        if result.returncode == 0:
            print("Ripping erfolgreich abgeschlossen!")
        else:
            print("Fehler beim Rippen:", result.stderr)
            
        print(f"Werfe CD aus {device_path} aus...")
        subprocess.run(["eject", device_path])
        
    finally:
        with lock:
            if device_path in active_rips:
                active_rips.remove(device_path)
        print(f"Laufwerk {device_path} ist wieder bereit.")

@app.get("/")
def read_root():
    return {"status": "RipRobot2 mit MusicBrainz-Integration!"}

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