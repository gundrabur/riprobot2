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

echo "==> Externe Laufwerke werden gesucht ..."
echo ""

# Kandidaten sammeln: alle externen, physischen Laufwerke (keine internen/System-Disks)
CANDIDATES=()
while IFS= read -r line; do
  DISK_ID="$(echo "$line" | awk '{print $1}' | sed 's#^/dev/##')"
  [[ -z "$DISK_ID" ]] && continue
  INFO="$(diskutil info "/dev/${DISK_ID}" 2>/dev/null || true)"
  [[ -z "$INFO" ]] && continue
  echo "$INFO" | grep -q "Internal:.*Yes" && continue
  CANDIDATES+=("$DISK_ID")
done < <(diskutil list external physical | awk '/^\/dev\/disk/ {print $1}')

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
  echo "Keine externen Laufwerke gefunden. Ist die Karte eingesteckt?" >&2
  echo "" >&2
  diskutil list >&2
  exit 1
fi

echo "Gefundene externe Laufwerke:"
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

echo "==> Karte wird mit Nullbytes gefüllt (Fortschritt: im Terminal Strg+T drücken)"
echo "    Das kann je nach Kartengröße und -geschwindigkeit lange dauern."
echo "    'No space left on device' am Ende ist normal — die Karte ist dann voll beschrieben."
set +e
DD_OUTPUT="$(sudo dd if=/dev/zero of="$DISK_RAW" bs=4m 2>&1)"
DD_STATUS=$?
set -e
echo "$DD_OUTPUT"
if [[ $DD_STATUS -ne 0 ]] && ! echo "$DD_OUTPUT" | grep -qi "no space left"; then
  echo "Fehler beim Nullen von $DISK_RAW (siehe Ausgabe oben)." >&2
  exit 1
fi

echo "==> Laufwerk auswerfen"
diskutil eject "$DISK_DEV" || true

echo ""
echo "Fertig: $DISK_DEV ist genullt und bereit für den Raspberry Pi Imager."
