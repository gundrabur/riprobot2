from fastapi import FastAPI, BackgroundTasks
import subprocess
import time
import os
from datetime import datetime
import threading

app = FastAPI(title="RipRobot2 API")

# Speichert, welche Laufwerke gerade beschäftigt sind
active_rips = set()
lock = threading.Lock()

def start_ripping(device_path: str):
    print(f"Audio-CD in Laufwerk {device_path} erkannt! Warte 5 Sekunden...")
    time.sleep(5)
    
    # Sicherheits-Check: Ist der USB-Stick wirklich eingehängt?
    if not os.path.exists("/media/usb"):
        print("FEHLER: Kein USB-Stick unter /media/usb gefunden! Breche ab.")
        subprocess.run(["eject", device_path])
        with lock:
            if device_path in active_rips:
                active_rips.remove(device_path)
        return
    
    # Zielordner auf dem USB-Stick
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    rip_dir = f"/media/usb/rip_{timestamp}"
    os.makedirs(rip_dir, exist_ok=True)
    
    print(f"Starte Ripping-Prozess von {device_path} in {rip_dir}...")
    
    try:
        # CD Rippen (WAV)
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
    return {"status": "RipRobot2 läuft mit intelligenter udev-Sperre und USB-Support!"}

@app.post("/trigger-rip")
def trigger_rip(background_tasks: BackgroundTasks, device: str = "sr0"):
    device_path = f"/dev/{device}"
    
    # Prüfen, ob das Laufwerk schon arbeitet
    with lock:
        if device_path in active_rips:
            print(f"Ignoriere Trigger: {device_path} wird bereits bearbeitet.")
            return {"message": "Rip läuft bereits"}
        
        # Laufwerk als "beschäftigt" markieren
        active_rips.add(device_path)
        
    # Wenn frei, starte den Prozess
    background_tasks.add_task(start_ripping, device_path)
    return {"message": f"Rip für {device_path} erfolgreich getriggert"}