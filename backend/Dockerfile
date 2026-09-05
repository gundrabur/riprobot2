FROM python:3.11-slim

# System-Abhängigkeiten für das Rippen von CDs installieren
RUN apt-get update && apt-get install -y \
    cdparanoia \
    lame \
    flac \
    eject \
    libdiscid0 \
    ffmpeg \
    curl \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

# Raspberry Pi APT-Repo hinzufügen, um rpi-keyboard-config für die Pi 500+ Tastatur-LEDs zu installieren
# (Blink-Codes über die Pfeiltasten, siehe KeyboardLedController in main.py). Rein optional: schlägt
# Netzwerk/Repo/Paket fehl, bricht der Image-Build NICHT ab - die App läuft dann einfach ohne dieses
# LED-Feature weiter (KeyboardLedController.available = False).
# Codename wird dynamisch aus dem Basis-Image ermittelt. [trusted=yes] umgeht Raspberry Pis kaputte
# GPG-Signatur (SHA1-Selbstsignatur, von Debian seit 2026-02-01 abgelehnt) - nur für dieses eine,
# nicht sicherheitskritische Vendor-Repo (Keyboard-Tool), keine globale Sicherheitsabsenkung.
RUN ( . /etc/os-release \
    && echo "deb [trusted=yes] https://archive.raspberrypi.com/debian/ ${VERSION_CODENAME} main" > /etc/apt/sources.list.d/raspi.list \
    && apt-get update \
    && apt-get install -y rpi-keyboard-config \
    ) || echo "rpi-keyboard-config nicht installierbar - LED-Feature bleibt inaktiv"; \
    rm -f /etc/apt/sources.list.d/raspi.list; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python-Pakete installieren
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Startet den FastAPI Server
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]