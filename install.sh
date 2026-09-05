#!/usr/bin/env bash
# Unbeaufsichtigte Installation von RipRobot2 auf einem frisch aufgesetzten Raspberry Pi OS (Trixie).
# Muss als root laufen (z.B. "sudo ./install.sh"), stellt keine Rückfragen.
# Idempotent: kann gefahrlos mehrfach ausgeführt werden.

set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

log() { echo -e "\n\033[1;32m==> $*\033[0m"; }
warn() { echo -e "\033[1;33m[WARNUNG] $*\033[0m"; }

if [ "$(id -u)" -ne 0 ]; then
    echo "Bitte als root ausführen, z.B.: sudo ./install.sh" >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -f "docker-compose.yml" ]; then
    echo "docker-compose.yml nicht gefunden in $SCRIPT_DIR - Skript muss im Projekt-Root liegen." >&2
    exit 1
fi

# Ursprünglichen (nicht-root) Benutzer ermitteln, z.B. für die docker-Gruppe
TARGET_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

log "Paketlisten aktualisieren"
apt-get update -y

log "Grundlegende Werkzeuge installieren"
apt-get install -y --no-install-recommends ca-certificates curl gnupg git

# --- Docker Engine + Compose-Plugin ---
if ! command -v docker >/dev/null 2>&1; then
    log "Docker Engine installieren (offizielles get.docker.com-Skript, unbeaufsichtigt)"
    curl -fsSL https://get.docker.com -o /tmp/get-docker.sh
    sh /tmp/get-docker.sh
    rm -f /tmp/get-docker.sh
else
    log "Docker ist bereits installiert - überspringe Installation"
fi

log "Docker-Dienst aktivieren und starten"
systemctl enable --now docker

if [ "$TARGET_USER" != "root" ]; then
    log "Benutzer '$TARGET_USER' zur docker-Gruppe hinzufügen"
    usermod -aG docker "$TARGET_USER" || true
fi

# --- Optional: Raspberry Pi 500+ Tastatur-Firmware/-Tool (für die RGB-Blink-Codes) ---
# Nur relevant, wenn ein Pi 500/500+ erkannt wird. Schlägt der Schritt fehl (z.B. Repo/Netzwerk),
# bricht die Installation NICHT ab - die App läuft dann einfach ohne dieses LED-Feature.
HARDWARE_MODEL="$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo "")"
if echo "$HARDWARE_MODEL" | grep -qi "Pi 500"; then
    log "Raspberry Pi 500/500+ erkannt ('$HARDWARE_MODEL') - installiere Tastatur-Tools"
    (
        . /etc/os-release
        echo "deb [trusted=yes] https://archive.raspberrypi.com/debian/ ${VERSION_CODENAME} main" > /etc/apt/sources.list.d/raspi.list
        apt-get update -y
        apt-get install -y rpi-keyboard-fw-update rpi-keyboard-config
        rpi-keyboard-fw-update || warn "Tastatur-Firmware-Update fehlgeschlagen oder bereits aktuell"
    ) || warn "rpi-keyboard-config/-fw-update konnte nicht installiert werden - LED-Feature bleibt inaktiv"
    rm -f /etc/apt/sources.list.d/raspi.list
else
    log "Kein Pi 500/500+ erkannt ('$HARDWARE_MODEL') - Tastatur-LED-Tools werden übersprungen"
fi

# --- Zielverzeichnis für den USB-Stick sicherstellen ---
mkdir -p /media/usb

# --- RipRobot2 Container bauen und starten ---
log "RipRobot2 Docker-Image bauen (kann einige Minuten dauern)"
docker compose build --no-cache

log "RipRobot2 starten"
docker compose up -d

log "Fertig! RipRobot2 läuft unter http://$(hostname -I | awk '{print $1}'):8000"
if [ "$TARGET_USER" != "root" ]; then
    warn "Für passwortlose 'docker'-Befehle als '$TARGET_USER' bitte einmal neu einloggen (Gruppenmitgliedschaft wirkt erst nach neuer Sitzung)."
fi
