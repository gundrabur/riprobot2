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
import atexit
import fcntl

@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=watch_for_usb_drives, daemon=True).start()
    threading.Thread(target=wait_for_startup_hardware, daemon=True).start()
    threading.Thread(target=watch_for_disc_insertion, daemon=True).start()
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
# Kontakt-URL statt Fantasie-Domain, da MusicBrainz Anfragen mit unglaubwürdigem User-Agent droht abzulehnen
musicbrainzngs.set_useragent("RipRobot", "2.0", "https://github.com/")

MUSICBRAINZ_MAX_ATTEMPTS = 3
MUSICBRAINZ_RETRY_BASE_DELAY = 2  # Sekunden, wird pro Versuch verdoppelt


def describe_musicbrainz_error(error: Exception) -> str:
    """musicbrainzngs-Exceptions (ResponseError/NetworkError) tragen die eigentliche
    Fehlermeldung im .cause-Attribut, str(error) allein ist meist leer."""
    cause = getattr(error, "cause", None)
    detail = str(cause) if cause else str(error)
    if "name resolution" in detail or "Name or service not known" in detail:
        detail += " (DNS-Auflösung im Container fehlgeschlagen, Netzwerk/DNS des Hosts pruefen)"
    return f"{type(error).__name__}: {detail}"


def query_musicbrainz_with_retry(disc_id: str):
    """Fragt MusicBrainz mit Retries/Backoff ab, um kurzzeitige Netzwerk- oder
    Rate-Limit-Fehler (Timeouts, 503) nicht sofort als endgültiges Scheitern zu werten."""
    last_error = None
    for attempt in range(1, MUSICBRAINZ_MAX_ATTEMPTS + 1):
        try:
            return musicbrainzngs.get_releases_by_discid(disc_id, includes=["artists", "recordings"])
        except musicbrainzngs.ResponseError as e:
            cause = getattr(e, "cause", None)
            status = getattr(cause, "code", None)
            if status == 404:
                # Disc ist MusicBrainz schlicht nicht bekannt, kein Retry sinnvoll
                return None
            last_error = e
        except (musicbrainzngs.NetworkError, OSError) as e:
            last_error = e

        if attempt < MUSICBRAINZ_MAX_ATTEMPTS:
            delay = MUSICBRAINZ_RETRY_BASE_DELAY * (2 ** (attempt - 1))
            print(f"MusicBrainz Versuch {attempt}/{MUSICBRAINZ_MAX_ATTEMPTS} fehlgeschlagen "
                  f"({describe_musicbrainz_error(last_error)}), erneuter Versuch in {delay}s...")
            time.sleep(delay)

    if last_error:
        raise last_error
    return None

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

# --- Onboard-LED für Blink-Codes ---
# Wird nur genutzt, wenn Linux eine steuerbare LED unter /sys/class/leds meldet. Erkennt automatisch:
#  1) "multicolor"-Klasse (ein Verzeichnis mit multi_intensity/multi_index) - z.B. RGB-Tastatur-LEDs
#     wie die Power-Taste des Pi 500+ (KTD202x-artiger Treiber). Nicht an echter Hardware verifiziert,
#     das Debug-Panel zeigt den erkannten Modus/Pfad zur Kontrolle an.
#  2) getrennte rot/grün/blau-LED-Geräte (Name enthält "red"/"green"/"blue")
#  3) einfache einfarbige LED (ACT/PWR/led0/led1 oder irgendein Gerät mit "brightness")
# Ist nichts davon vorhanden (z.B. Pi 400/500 ohne Sichtfenster, oder kein Zugriff), bleibt alles ein No-Op.
MONO_LED_NAME_CANDIDATES = ["ACT", "PWR", "led0", "led1"]
COLOR_CHANNEL_MAP = {"red": ("red",), "green": ("green",), "blue": ("blue",), "yellow": ("red", "green")}

# Werte je Status: mono = einfache Blink-Pause-Liste (None = aus); color/pattern = Farbe + Blink-Pause-Liste
# (pattern None = dauerhaft an) für RGB/Multicolor-LEDs.
LED_PATTERNS = {
    "idle":       {"mono": None,                                        "color": "green", "pattern": None},
    "metadata":   {"mono": [(0.1, 0.1)],                                 "color": "blue",  "pattern": [(0.1, 0.1)]},
    "ripping":    {"mono": [(0.5, 0.5)],                                 "color": "blue",  "pattern": [(0.5, 0.5)]},
    "converting": {"mono": [(0.2, 0.2)],                                 "color": "blue",  "pattern": [(0.2, 0.2)]},
    "success":    {"mono": [(1.0, 1.0)],                                 "color": "green", "pattern": [(1.0, 1.0)]},
    "error":      {"mono": [(0.1, 0.1), (0.1, 0.1), (0.1, 0.8)],         "color": "red",   "pattern": [(0.1, 0.1), (0.1, 0.1), (0.1, 0.8)]},
}

def find_multicolor_led(base_dir="/sys/class/leds"):
    try:
        entries = os.listdir(base_dir)
    except OSError:
        return None
    for name in entries:
        path = os.path.join(base_dir, name)
        if os.path.exists(os.path.join(path, "multi_intensity")) and os.path.exists(os.path.join(path, "multi_index")):
            try:
                with open(os.path.join(path, "multi_index"), "r") as f:
                    channels = f.read().split()
                with open(os.path.join(path, "max_brightness"), "r") as f:
                    max_brightness = int(f.read().strip())
                return {"path": path, "channels": channels, "max_brightness": max_brightness}
            except (OSError, ValueError):
                continue
    return None

def find_rgb_led_triplet(base_dir="/sys/class/leds"):
    try:
        entries = os.listdir(base_dir)
    except OSError:
        return None
    channels = {}
    for name in entries:
        path = os.path.join(base_dir, name)
        if not os.path.exists(os.path.join(path, "brightness")):
            continue
        lower = name.lower()
        for color in ("red", "green", "blue"):
            if color in lower:
                channels[color] = path
    return channels if "red" in channels and "green" in channels else None

def find_mono_led(base_dir="/sys/class/leds"):
    for name in MONO_LED_NAME_CANDIDATES:
        path = os.path.join(base_dir, name)
        if os.path.exists(os.path.join(path, "brightness")):
            return path
    try:
        for name in os.listdir(base_dir):
            path = os.path.join(base_dir, name)
            if os.path.exists(os.path.join(path, "brightness")):
                return path
    except OSError:
        pass
    return None

class LedController:
    def __init__(self):
        self.mode = None # "multicolor", "rgb" oder "mono"
        self.rgb_channels = None    # {farbe: pfad} bei Modus "rgb"
        self.multicolor = None      # {path, channels, max_brightness} bei Modus "multicolor"
        self.mono_path = None
        self.available_colors = set()
        self._original_triggers = {} # pfad -> ursprünglicher Trigger, für sauberes Wiederherstellen
        self._stop_event = threading.Event()
        self._thread = None

        multicolor = find_multicolor_led()
        rgb = None if multicolor else find_rgb_led_triplet()
        mono = None if (multicolor or rgb) else find_mono_led()

        if multicolor:
            self.mode = "multicolor"
            self.multicolor = multicolor
            self.available_colors = {c.lower() for c in multicolor["channels"] if c.lower() in ("red", "green", "blue")}
            self._disable_trigger(multicolor["path"])
        elif rgb:
            self.mode = "rgb"
            self.rgb_channels = rgb
            self.available_colors = set(rgb.keys())
            for path in rgb.values():
                self._disable_trigger(path)
        elif mono:
            self.mode = "mono"
            self.mono_path = mono
            self._disable_trigger(mono)

        self.available = self.mode is not None
        if self.available:
            atexit.register(self._restore_on_exit)

    def _disable_trigger(self, path):
        self._original_triggers[path] = self._read_current_trigger(path)
        self._write(path, "trigger", "none")

    def _read_current_trigger(self, path):
        try:
            with open(os.path.join(path, "trigger"), "r") as f:
                match = re.search(r"\[(.+?)\]", f.read())
                return match.group(1) if match else None
        except OSError:
            return None

    def _write(self, path, filename, value):
        try:
            with open(os.path.join(path, filename), "w") as f:
                f.write(str(value))
        except OSError:
            pass

    def _resolve_color(self, requested):
        if requested in self.available_colors:
            return requested
        if requested == "blue" and {"red", "green"} <= self.available_colors:
            return "yellow" # kein eigener Blau-Kanal -> Rot+Grün als Ersatzfarbe
        return next(iter(self.available_colors), None)

    def _apply_off(self):
        if self.mode == "mono":
            self._write(self.mono_path, "brightness", 0)
        elif self.mode == "rgb":
            for path in self.rgb_channels.values():
                self._write(path, "brightness", 0)
        elif self.mode == "multicolor":
            self._write(self.multicolor["path"], "brightness", 0)

    def _apply_color(self, color):
        if self.mode == "mono":
            self._write(self.mono_path, "brightness", 1)
        elif self.mode == "rgb":
            active = COLOR_CHANNEL_MAP.get(color, ())
            for name, path in self.rgb_channels.items():
                self._write(path, "brightness", 1 if name in active else 0)
        elif self.mode == "multicolor":
            active = COLOR_CHANNEL_MAP.get(color, ())
            max_brightness = self.multicolor["max_brightness"]
            intensities = [str(max_brightness if ch.lower() in active else 0) for ch in self.multicolor["channels"]]
            self._write(self.multicolor["path"], "multi_intensity", " ".join(intensities))
            self._write(self.multicolor["path"], "brightness", max_brightness)

    def _blink_loop(self, color, pattern):
        while not self._stop_event.is_set():
            for on_time, off_time in pattern:
                if self._stop_event.is_set():
                    break
                self._apply_color(color) # bei mono wird "color" ignoriert (einfach an)
                if self._stop_event.wait(on_time):
                    break
                self._apply_off()
                if self._stop_event.wait(off_time):
                    break

    def set_state_pattern(self, spec):
        if not self.available or spec is None:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._stop_event = threading.Event()

        if self.mode == "mono":
            pattern = spec["mono"]
            if pattern is None:
                self._apply_off()
            else:
                self._thread = threading.Thread(target=self._blink_loop, args=(None, pattern), daemon=True)
                self._thread.start()
        else:
            color = self._resolve_color(spec["color"])
            pattern = spec["pattern"]
            if pattern is None:
                self._apply_color(color) # dauerhaft an, z.B. idle = grün
            else:
                self._thread = threading.Thread(target=self._blink_loop, args=(color, pattern), daemon=True)
                self._thread.start()

    def _restore_on_exit(self):
        self._stop_event.set()
        for path, trigger in self._original_triggers.items():
            if trigger:
                self._write(path, "trigger", trigger)

led_controller = LedController()
_last_led_state = None
_last_keyboard_led_state = None

# --- Pi 500+ Tastatur-RGB-LEDs für Blink-Codes ---
# Die Power-Taste ist laut Raspberry-Pi-Doku von keinem Software-Effekt beeinflussbar, aber einzelne
# Tasten können per "rpi-keyboard-config" (Vial-QMK-Firmware) individuell eingefärbt werden.
# Vorausgesetzt: `sudo apt install rpi-keyboard-fw-update rpi-keyboard-config` + Firmware-Update.
# Genutzt werden die vier Pfeiltasten unten rechts (Hoch/Links/Runter/Rechts) - unauffällig beim Tippen.
KEYBOARD_STATUS_KEYS = [(4, 14), (5, 13), (5, 14), (5, 15)]
KEYBOARD_LED_COLOR_VALUES = {
    "red": "rgb(255,0,0)",
    "yellow": "rgb(255,180,0)",
    "green": "rgb(0,255,0)",
    "blue": "rgb(0,0,255)",
    "violet": "rgb(180,0,255)",
    "white": "rgb(255,255,255)",
}

class KeyboardLedController:
    def __init__(self):
        self.binary = shutil.which("rpi-keyboard-config")
        self.available = self.binary is not None
        self._stop_event = threading.Event()
        self._thread = None
        self._last_colors = None

    def _run(self, *args):
        try:
            subprocess.run([self.binary, *args], capture_output=True, timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _configure_colors(self, colors):
        self._run("leds", "clear")
        for (row, col), color in zip(KEYBOARD_STATUS_KEYS, colors):
            colour_value = KEYBOARD_LED_COLOR_VALUES.get(color, "rgb(0,0,0)")
            self._run("led", "set", f"{row},{col}", "--colour", colour_value)
        self._run("leds", "save")

    def set_state_pattern(self, state, message_key=None):
        if not self.available:
            return
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=1)
        self._stop_event = threading.Event()

        colors = ["green"] * len(KEYBOARD_STATUS_KEYS)
        if state == "metadata":
            colors = ["violet"] * len(KEYBOARD_STATUS_KEYS)
        elif state == "ripping":
            colors = ["yellow"] * len(KEYBOARD_STATUS_KEYS)
        elif state == "converting":
            colors = ["white"] * len(KEYBOARD_STATUS_KEYS) if message_key == "status.syncingUsb" else ["blue"] * len(KEYBOARD_STATUS_KEYS)
        elif state == "error":
            colors = ["red"] * len(KEYBOARD_STATUS_KEYS)

        if state == "idle":
            if not output_storage_ready(load_settings()["output_path"]):
                colors[0] = "red" # Cursor hoch: kein USB-Speicher
            if not os.path.exists("/dev/sr0"):
                colors[2] = "red" # Cursor runter: kein optisches Laufwerk

        if colors == self._last_colors:
            return
        self._configure_colors(colors)
        self._last_colors = colors

    def refresh_hardware_state(self):
        self.set_state_pattern(current_status["state"], current_status.get("message_key"))

keyboard_led_controller = KeyboardLedController()

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
    global current_status, _last_led_state, _last_keyboard_led_state
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
    if state != _last_led_state:
        _last_led_state = state
        led_controller.set_state_pattern(LED_PATTERNS.get(state))
    keyboard_state = (state, message_key)
    if keyboard_state != _last_keyboard_led_state:
        _last_keyboard_led_state = keyboard_state
        keyboard_led_controller.set_state_pattern(state, message_key)
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

def _describe_mount(path):
    try:
        usage = shutil.disk_usage(path)
        return {"total_gb": round(usage.total / (1024 ** 3), 1), "free_gb": round(usage.free / (1024 ** 3), 1)}
    except OSError:
        return {"total_gb": None, "free_gb": None}

def _unescape_mount_path(path):
    # /proc/mounts kodiert Sonderzeichen in Mountpunkten oktal, z.B. Leerzeichen als \040
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), path)

def _read_mounted_devices():
    """{Geraetepfad: Mountpunkt} fuer alles, was aktuell gemountet ist."""
    mounted = {}
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("/dev/"):
                    mounted[parts[0]] = _unescape_mount_path(parts[1])
    except OSError:
        pass
    return mounted

def _read_partition_label(name):
    try:
        for label in os.listdir("/dev/disk/by-label"):
            target = os.path.realpath(os.path.join("/dev/disk/by-label", label))
            if os.path.basename(target) == name:
                return label
    except OSError:
        pass
    return None

def list_usb_drives():
    """Findet alle Partitionen auf wechselbaren (USB-)Datentraegern - gemountet oder nicht - über
    /sys/class/block. So tauchen auch Sticks auf, die vom Betriebssystem (noch) nicht automatisch
    gemountet wurden (z.B. headless ohne Automount-Dienst), und koennen ueber die UI gemountet werden."""
    drives = []
    base = "/sys/class/block"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return drives
    mounted_devices = _read_mounted_devices()
    for name in names:
        if name.startswith(("loop", "zram", "sr", "ram")):
            continue
        if not os.path.exists(os.path.join(base, name, "partition")):
            continue # nur Partitionen (z.B. sda1), nicht die Disk selbst (sda)
        parent = re.sub(r"\d+$", "", name)
        try:
            with open(f"/sys/block/{parent}/removable", "r") as f:
                removable = f.read().strip() == "1"
        except OSError:
            removable = False
        if not removable:
            continue

        device_path = f"/dev/{name}"
        mount_point = mounted_devices.get(device_path)
        label = _read_partition_label(name) or name
        entry = {"device": device_path, "name": name, "label": label, "mounted": mount_point is not None, "path": mount_point}
        if mount_point:
            entry.update(_describe_mount(mount_point))
        else:
            entry["total_gb"] = None
            entry["free_gb"] = None
        drives.append(entry)
    return sorted(drives, key=lambda d: d["device"])

def mount_usb_drive(device_path):
    """Haengt eine (noch) nicht gemountete Partition unter /media/<Label|Geraetename> ein."""
    if not device_path.startswith("/dev/"):
        raise ValueError("Ungültiger Gerätepfad")
    name = os.path.basename(device_path)
    label = _read_partition_label(name) or name
    mount_point = os.path.join("/media", label)
    os.makedirs(mount_point, exist_ok=True)
    subprocess.run(["mount", device_path, mount_point], check=True, capture_output=True)
    return mount_point

_usb_mount_failures = set() # Geräte, deren Mount fehlschlug - erst nach Abziehen/Neuanstecken erneut versuchen

def sync_usb_drives():
    """Haengt alle erkannten, noch nicht gemounteten Wechseldatentraeger ein. Ist das konfigurierte Ziel
    nicht beschreibbar gemountet, wird der erste erkannte Stick automatisch als Ziel gesetzt."""
    drives = list_usb_drives()
    present_devices = {drive["device"] for drive in drives}
    _usb_mount_failures.intersection_update(present_devices)

    for drive in drives:
        if drive["mounted"] or drive["device"] in _usb_mount_failures:
            continue
        try:
            drive["path"] = mount_usb_drive(drive["device"])
            drive["mounted"] = True
            print(f"[USB] {drive['device']} eingehängt unter {drive['path']}")
        except (OSError, subprocess.CalledProcessError) as error:
            _usb_mount_failures.add(drive["device"])
            detail = error.stderr.decode(errors="ignore").strip() if getattr(error, "stderr", None) else str(error)
            print(f"[USB] {drive['device']} konnte nicht eingehängt werden: {detail}")

    mounted_paths = [drive["path"] for drive in drives if drive["mounted"] and drive["path"]]
    if not mounted_paths:
        keyboard_led_controller.refresh_hardware_state()
        return
    settings = load_settings()
    if not output_storage_ready(settings["output_path"]):
        settings["output_path"] = mounted_paths[0]
        save_settings(settings)
        print(f"[USB] Zielverzeichnis automatisch auf {mounted_paths[0]} gesetzt")
    keyboard_led_controller.refresh_hardware_state()

def watch_for_usb_drives(poll_interval=3):
    print("[USB] Beobachte Wechseldatenträger...")
    while True:
        try:
            sync_usb_drives()
        except Exception as error:
            print(f"[USB] Fehler bei der Laufwerksprüfung: {error}")
        time.sleep(poll_interval)

def list_all_leds():
    """Rohe Auflistung aller /sys/class/leds-Geräte samt Fähigkeiten - zur manuellen Identifikation der richtigen LED."""
    base_dir = "/sys/class/leds"
    entries = []
    try:
        names = sorted(os.listdir(base_dir))
    except OSError:
        return entries
    for name in names:
        path = os.path.join(base_dir, name)
        entry = {"name": name}
        for filename in ("brightness", "max_brightness", "trigger", "multi_intensity", "multi_index"):
            filepath = os.path.join(path, filename)
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r") as f:
                        entry[filename] = f.read().strip()
                except OSError:
                    entry[filename] = None
        entries.append(entry)
    return entries

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

# CDROM-ioctl (aus linux/cdrom.h), um zu erkennen, ob ein Medium eingelegt ist, ohne es zu lesen/mounten.
CDROM_DRIVE_STATUS = 0x5326
CDS_DISC_OK = 4

def disc_is_present(device_path):
    try:
        fd = os.open(device_path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError:
        return False
    try:
        return fcntl.ioctl(fd, CDROM_DRIVE_STATUS, 0) == CDS_DISC_OK
    except OSError:
        return False
    finally:
        os.close(fd)

def wait_for_startup_hardware(device_path="/dev/sr0", retry_interval=2):
    boot_id = get_boot_id()
    already_ejected = False
    try:
        with open(STARTUP_EJECT_MARKER, "r") as file:
            already_ejected = file.read().strip() == boot_id
    except OSError:
        pass

    if not already_ejected:
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
                            break
                        except (OSError, subprocess.CalledProcessError) as error:
                            print(f"[STARTUP] Laufwerk noch nicht bereit: {error}")
            time.sleep(retry_interval)

def watch_for_disc_insertion(device_path="/dev/sr0", poll_interval=2):
    print("[WATCH] Beobachte Laufwerk auf eingelegte CDs...")
    while True:
        time.sleep(poll_interval)
        with lock:
            if current_status["state"] != "idle":
                continue
            if not disc_is_present(device_path):
                continue
            update_status("metadata", "CD erkannt, Rip-Vorgang wird gestartet...", "", "", 0, "status.queueingRip")
            threading.Thread(target=start_ripping, args=(device_path,), daemon=True).start()

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
        result = query_musicbrainz_with_retry(disc.id)

        if result and "disc" in result and result["disc"].get("release-list"):
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
        print(f"MusicBrainz Fehler: {describe_musicbrainz_error(e)}")

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
    if led_controller.mode == "mono":
        led_path = led_controller.mono_path
    elif led_controller.mode == "multicolor":
        led_path = led_controller.multicolor["path"]
    elif led_controller.mode == "rgb":
        led_path = ", ".join(sorted(led_controller.rgb_channels.values()))
    else:
        led_path = None
    return {
        "version": APP_VERSION,
        "developer": APP_DEVELOPER,
        "copyright_year": APP_COPYRIGHT_YEAR,
        "os": platform.platform(),
        "hardware_model": get_hardware_model(),
        "ram": get_ram_info(),
        "optical_drive": get_optical_drive_info("/dev/sr0"),
        "usb_stick": get_usb_stick_info(settings["output_path"]),
        "led": {
            "available": led_controller.available,
            "mode": led_controller.mode,
            "path": led_path,
            "colors": sorted(led_controller.available_colors) or None,
        },
        "keyboard_led": {
            "available": keyboard_led_controller.available,
            "binary": keyboard_led_controller.binary,
            "keys": KEYBOARD_STATUS_KEYS,
        },
    }

@app.get("/api/led-debug")
def led_debug():
    return {
        "detected_mode": led_controller.mode,
        "detected_colors": sorted(led_controller.available_colors) or None,
        "all_leds": list_all_leds(),
    }

@app.get("/api/settings")
def get_settings():
    return load_settings()

@app.get("/api/usb-drives")
def get_usb_drives():
    return list_usb_drives()

@app.post("/api/usb-drives/mount")
def post_mount_usb_drive(payload: dict):
    device = payload.get("device", "")
    try:
        mount_point = mount_usb_drive(device)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode(errors="ignore").strip() if error.stderr else "Mount fehlgeschlagen"
        raise HTTPException(status_code=500, detail=detail) from error
    return {"path": mount_point}

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