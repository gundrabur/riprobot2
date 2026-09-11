#!/usr/bin/env bash
# wipe-sd-card.sh — Füllt eine angeschlossene SD-Karte/USB-Karte auf macOS
# interaktiv komplett mit Nullbytes.
#
# Zweck: Vor dem Erstellen eines Images (siehe create-sd-image.sh) sorgt das
# Nullen des kompletten Datenbereichs dafür, dass sich das Image danach mit
# gzip/xz deutlich effizienter komprimieren lässt (Nullbytes komprimieren
# nahezu verlustfrei auf sehr wenig Platz).
#
# ACHTUNG: Danach ist die Karte leer und muss mit dem Raspberry Pi Imager
# neu beschrieben werden — Reihenfolge:
#   1. wipe-sd-card.sh   (diese Karte für später vorbereiten/nullen)
#   2. Raspberry Pi OS mit dem Imager auf die Karte schreiben
#   3. ... Karte benutzen ...
#   4. create-sd-image.sh (Image der genutzten Karte ziehen)
#
# Verwendung: ./wipe-sd-card.sh   (fragt alles Nötige interaktiv ab)
#
# Läuft NICHT als root — sudo wird gezielt nur für dd abgefragt.

set -euo pipefail

echo "==> SD-Karten und externe Laufwerke werden gesucht ..."
echo ""

# Kandidaten sammeln: externe physische Laufwerke sowie entfernbare SD-Karten
# im eingebauten Kartenleser (diese meldet macOS als "Internal").
CANDIDATES=()
while IFS= read -r line; do
  DISK_ID="$(echo "$line" | awk '{print $1}' | sed 's#^/dev/##')"
  [[ -z "$DISK_ID" ]] && continue
  INFO="$(diskutil info "/dev/${DISK_ID}" 2>/dev/null || true)"
  [[ -z "$INFO" ]] && continue
  if echo "$INFO" | grep -Eq "Device Location:.*External|Internal:.*No"; then
    CANDIDATES+=("$DISK_ID")
  elif echo "$INFO" | grep -q "Protocol:.*Secure Digital" \
    && echo "$INFO" | grep -q "Removable Media:.*Removable"; then
    CANDIDATES+=("$DISK_ID")
  fi
done < <(diskutil list physical | awk '/^\/dev\/disk/ {print $1}')

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
  echo "Keine SD-Karten oder externen Laufwerke gefunden. Ist die Karte eingesteckt?" >&2
  echo "" >&2
  diskutil list >&2
  exit 1
fi

echo "Gefundene SD-Karten und externe Laufwerke:"
echo ""
for i in "${!CANDIDATES[@]}"; do
  DISK_ID="${CANDIDATES[$i]}"
  INFO="$(diskutil info "/dev/${DISK_ID}")"
  NAME="$(echo "$INFO" | awk -F': *' '/Device \/ Media Name/ {print $2; exit}')"
  SIZE="$(echo "$INFO" | awk -F': *' '/Disk Size/ {print $2; exit}' | awk -F' *\\(' '{print $1}')"
  printf "  [%d] /dev/%s — %s (%s)\n" "$((i + 1))" "$DISK_ID" "$NAME" "$SIZE"
done
echo ""

read -r -p "Welches Laufwerk komplett nullen? Nummer eingeben: " CHOICE
if ! [[ "$CHOICE" =~ ^[0-9]+$ ]] || (( CHOICE < 1 || CHOICE > ${#CANDIDATES[@]} )); then
  echo "Ungültige Auswahl." >&2
  exit 1
fi

DISK_ID="${CANDIDATES[$((CHOICE - 1))]}"
DISK_DEV="/dev/${DISK_ID}"
DISK_RAW="/dev/r${DISK_ID}"
DISK_INFO="$(diskutil info "$DISK_DEV")"
DISK_SIZE="$(echo "$DISK_INFO" | awk -F': *' '/Disk Size/ {print $2; exit}' | awk -F' *\\(' '{print $1}')"

echo ""
echo "==> Ausgewählt: $DISK_DEV ($DISK_SIZE)"
diskutil list "$DISK_DEV"
echo ""
echo "WARNUNG: Alle Daten auf $DISK_DEV werden unwiderruflich mit Nullbytes überschrieben!"
read -r -p "Wirklich fortfahren? Tippe 'ja' zum Bestätigen: " CONFIRM
if [[ "$CONFIRM" != "ja" ]]; then
  echo "Abgebrochen."
  exit 1
fi

echo "==> Laufwerk aushängen (bleibt physisch verbunden)"
diskutil unmountDisk "$DISK_DEV"

echo "==> Karte wird mit Nullbytes gefüllt ..."
echo "    Das kann je nach Kartengröße und -geschwindigkeit lange dauern."
echo "    'No space left on device' am Ende ist normal — die Karte ist dann voll beschrieben."

DISK_BYTES=$(echo "$DISK_INFO" | tr '[:upper:]' '[:lower:]' | grep -oE '[0-9]+ bytes' | head -1 | awk '{print $1}')
if [[ -z "$DISK_BYTES" ]]; then
  echo "Hinweis: Gerätegröße konnte nicht sauber in Bytes ermittelt werden; die Fortschrittsleiste wird übersprungen." >&2
  DISK_BYTES=""
fi

if [[ -n "$DISK_BYTES" ]] && ! command -v pv >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "pv wird installiert, damit eine echte Fortschrittsleiste angezeigt werden kann..."
    brew install pv >/dev/null 2>&1 || true
  fi
fi

DD_LOG="$(mktemp)"
set +e
if [[ -n "$DISK_BYTES" ]] && command -v pv >/dev/null 2>&1; then
  echo "Fortschritt:"
  sudo dd if=/dev/zero of="$DISK_RAW" bs=4m 2>"$DD_LOG" | pv -s "$DISK_BYTES" >/dev/null
  DD_STATUS=${PIPESTATUS[0]}
else
  echo "Fortschritt: Spinner wird angezeigt, bis der Schreibvorgang abgeschlossen ist ..."
  sudo dd if=/dev/zero of="$DISK_RAW" bs=4m 2>"$DD_LOG" >/dev/null &
  DD_PID=$!
  SPINNER='|/-\\'
  i=0
  while kill -0 "$DD_PID" 2>/dev/null; do
    printf '\r[%c] Schreiben ...' "${SPINNER:i++%${#SPINNER}:1}"
    sleep 1
  done
  wait "$DD_PID"
  DD_STATUS=$?
  printf '\r%s\n' "                                   "
fi
set -e

if [[ $DD_STATUS -ne 0 ]]; then
  if grep -qi "No space left on device" "$DD_LOG"; then
    echo "Schreiben abgeschlossen: Der Zielträger ist voll beschrieben."
  else
    cat "$DD_LOG" >&2
    echo "Fehler beim Nullen von $DISK_RAW." >&2
    rm -f "$DD_LOG"
    exit 1
  fi
fi

rm -f "$DD_LOG"

echo "==> Laufwerk auswerfen"
diskutil eject "$DISK_DEV" || true

echo ""
echo "Fertig: $DISK_DEV ist genullt und bereit für den Raspberry Pi Imager."
