#!/usr/bin/env bash
# prepare-image.sh — RipRobot-System für die Image-Erstellung vorbereiten
#
# Ausführen AUF DEM PI, kurz bevor die SD-Karte gezogen und geklont wird:
#   sudo bash prepare-image.sh
#
# Bewusst NICHT verändert: Hostname und SSH-Dienst/Passwort-Login bleiben
# aktiv, damit Nutzer ohne Monitor/Tastatur sofort per
# "ssh rr2@riprobot2.local" auf das Gerät kommen.

set -euo pipefail

TARGET_USER="rr2"                 # ggf. anpassen
TARGET_HOME="/home/${TARGET_USER}"
SERVICE_NAME="riprobot.service"   # ggf. anpassen

if [[ $EUID -ne 0 ]]; then
  echo "Bitte mit sudo ausführen: sudo bash $0" >&2
  exit 1
fi

echo "==> RipRobot-Dienst aktivieren (idempotent, falls schon aktiv)"
systemctl enable "${SERVICE_NAME}" \
  || echo "    Hinweis: ${SERVICE_NAME} nicht gefunden — Namen oben prüfen."

echo "==> APT-Cache leeren"
apt clean

echo "==> Logs kürzen"
journalctl --vacuum-time=1s || true
rm -f /var/log/wtmp /var/log/btmp

echo "==> Bash-History löschen"
rm -f "${TARGET_HOME}/.bash_history"
rm -f /root/.bash_history

echo "==> Sicherstellen, dass ssh.service Host-Keys bei jedem Start selbst nachgeneriert"
echo "    (verlässt sich NICHT auf den OS-eigenen Automatismus, der auf diesem System"
echo "    offenbar nicht zuverlässig greift — ssh-keygen -A ist idempotent, also für"
echo "    jeden Boot unbedenklich, egal ob Keys schon existieren oder fehlen)"
mkdir -p /etc/systemd/system/ssh.service.d
cat > /etc/systemd/system/ssh.service.d/override.conf << 'OVERRIDE_EOF'
[Service]
ExecStartPre=/usr/bin/ssh-keygen -A
OVERRIDE_EOF
systemctl daemon-reload

echo "==> SSH-Host-Keys entfernen (werden dank Override oben beim nächsten Start von ssh.service neu erzeugt)"
rm -f /etc/ssh/ssh_host_*

echo "==> machine-id zurücksetzen (wird beim nächsten Boot neu vergeben)"
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id

echo "==> Gespeicherte WLAN-Zugangsdaten entfernen"
rm -f /etc/NetworkManager/system-connections/*.nmconnection

echo "==> Eigenen Dev-SSH-Key aus authorized_keys entfernen"
rm -f "${TARGET_HOME}/.ssh/authorized_keys"

echo ""
echo "Fertig. Hostname (riprobot2) und SSH (Dienst + Passwort-Login) bleiben unverändert."
echo "Nächster Schritt: Ausgabe oben prüfen, dann manuell:  sudo shutdown now"