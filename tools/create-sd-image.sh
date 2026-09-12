#!/usr/bin/env bash
# create-sd-image.sh — Erstellt auf macOS interaktiv ein Roh-Image einer SD-Karte.
#
# Das erzeugte .img (bzw. .img.gz) kann anschließend mit dem offiziellen
# "Raspberry Pi Imager" wieder auf eine andere SD-Karte geschrieben werden
# ("Choose OS" → "Use custom" → Image-Datei auswählen).
#
# Verwendung: ./create-sd-image.sh   (fragt alles Nötige interaktiv ab)
#
# Läuft NICHT als root — sudo wird gezielt nur für dd abgefragt.

set -euo pipefail

echo "==> Externe Laufwerke und SD-Karten werden gesucht ..."
echo ""

# Kandidaten sammeln: externe Laufwerke sowie Wechselmedien in internen Kartenlesern.
# Interne, nicht wechselbare Laufwerke (insbesondere die System-Disk) bleiben ausgeschlossen.
CANDIDATES=()
while IFS= read -r line; do
  DISK_ID="$(echo "$line" | awk '{print $1}' | sed 's#^/dev/##')"
  [[ -z "$DISK_ID" ]] && continue
  INFO="$(diskutil info "/dev/${DISK_ID}" 2>/dev/null || true)"
  [[ -z "$INFO" ]] && continue
  if echo "$INFO" | grep -qE "^[[:space:]]*(Internal:.*Yes|Device Location:.*Internal)" \
    && ! echo "$INFO" | grep -qE "Removable Media:.*Removable|Ejectable:.*Yes"; then
    continue
  fi
  CANDIDATES+=("$DISK_ID")
done < <(diskutil list physical | awk '/^\/dev\/disk/ {print $1}')

if [[ ${#CANDIDATES[@]} -eq 0 ]]; then
  echo "Keine externen Laufwerke oder SD-Karten gefunden. Ist die SD-Karte eingesteckt?" >&2
  echo "" >&2
  diskutil list >&2
  exit 1
fi

echo "Gefundene externe Laufwerke und SD-Karten:"
echo ""
for i in "${!CANDIDATES[@]}"; do
  DISK_ID="${CANDIDATES[$i]}"
  INFO="$(diskutil info "/dev/${DISK_ID}")"
  NAME="$(echo "$INFO" | awk -F': *' '/Device \/ Media Name/ {print $2; exit}')"
  SIZE="$(echo "$INFO" | awk -F': *' '/Disk Size/ {print $2; exit}' | awk -F' *\\(' '{print $1}')"
  printf "  [%d] /dev/%s — %s (%s)\n" "$((i + 1))" "$DISK_ID" "$NAME" "$SIZE"
done
echo ""

read -r -p "Welches Laufwerk sichern? Nummer eingeben: " CHOICE
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

echo "Wohin soll das Image gespeichert werden?"
echo "  [1] Schreibtisch (${HOME}/Desktop)"
echo "  [2] Dokumente ($HOME/Documents)"
echo "  [3] Anderer Ordner"
read -r -p "Nummer eingeben [1]: " DIR_CHOICE
case "${DIR_CHOICE:-1}" in
  1) TARGET_DIR="${HOME}/Desktop" ;;
  2) TARGET_DIR="${HOME}/Documents" ;;
  3) read -r -p "Zielordner: " TARGET_DIR ;;
  *) echo "Ungültige Auswahl." >&2; exit 1 ;;
esac
TARGET_DIR="${TARGET_DIR%/}"
if [[ ! -d "$TARGET_DIR" ]]; then
  echo "Fehler: Ordner '$TARGET_DIR' existiert nicht." >&2
  exit 1
fi

read -r -p "Name der Ausgabedatei [${DISK_ID}.img]: " OUTPUT_NAME
OUTPUT_NAME="${OUTPUT_NAME:-${DISK_ID}.img}"
[[ "$OUTPUT_NAME" != *.img ]] && OUTPUT_NAME="${OUTPUT_NAME}.img"
OUTPUT="${TARGET_DIR}/${OUTPUT_NAME}"

read -r -p "Zusätzlich gzip-komprimieren (${OUTPUT}.gz)? [j/N]: " COMPRESS_ANSWER
COMPRESS=0
[[ "$COMPRESS_ANSWER" =~ ^[jJyY] ]] && COMPRESS=1

echo ""
read -r -p "Laufwerk $DISK_DEV komplett nach '${OUTPUT}' sichern? Tippe 'ja' zum Bestätigen: " CONFIRM
if [[ "$CONFIRM" != "ja" ]]; then
  echo "Abgebrochen."
  exit 1
fi

echo "==> Laufwerk aushängen (bleibt physisch verbunden)"
diskutil unmountDisk "$DISK_DEV"

echo "==> Image schreiben nach ${OUTPUT}"
echo "    Das kann je nach Kartengröße und -geschwindigkeit lange dauern."

DISK_BYTES=$(echo "$DISK_INFO" | tr '[:upper:]' '[:lower:]' | grep -oE '[0-9]+ bytes' | head -1 | awk '{print $1}' || true)
if [[ -z "$DISK_BYTES" ]]; then
  echo "Hinweis: Gerätegröße konnte nicht sauber in Bytes ermittelt werden; es wird nur die gelesene Menge angezeigt." >&2
  DISK_BYTES=""
fi

DD_LOG="$(mktemp)"
sudo -v
set +e
sudo sh -c '
  env LC_ALL=C dd if="$1" of="$2" bs=4m &
  dd_pid=$!
  while kill -0 "$dd_pid" 2>/dev/null; do
    sleep 1
    kill -INFO "$dd_pid" 2>/dev/null || true
  done &
  reporter_pid=$!
  wait "$dd_pid"
  dd_status=$?
  kill "$reporter_pid" 2>/dev/null || true
  wait "$reporter_pid" 2>/dev/null || true
  exit "$dd_status"
' sh "$DISK_RAW" "$OUTPUT" 2>"$DD_LOG" >/dev/null &
DD_PID=$!
START_TIME=$(date +%s)
echo "Fortschritt:"
while kill -0 "$DD_PID" 2>/dev/null; do
  sleep 1
  WRITTEN=$(awk '/bytes transferred/ {bytes=$1} END {print bytes}' "$DD_LOG")
  [[ "$WRITTEN" =~ ^[0-9]+$ ]] || continue
  ELAPSED=$(( $(date +%s) - START_TIME ))
  (( ELAPSED < 1 )) && ELAPSED=1
  awk -v written="$WRITTEN" -v total="$DISK_BYTES" -v elapsed="$ELAPSED" '
    function human(bytes, unit) {
      split("B KiB MiB GiB TiB", units, " ")
      unit = 1
      while (bytes >= 1024 && unit < 5) { bytes /= 1024; unit++ }
      return sprintf(bytes >= 10 || unit == 1 ? "%.0f %s" : "%.1f %s", bytes, units[unit])
    }
    BEGIN {
      rate = written / elapsed
      if (total > 0) {
        percent = written * 100 / total
        remaining = rate > 0 ? (total - written) / rate : 0
        if (remaining < 0) remaining = 0
        printf "\r%5.1f%% | %s / %s | %s/s | ETA %02d:%02d:%02d", \
          percent, human(written), human(total), human(rate), \
          remaining / 3600, (remaining % 3600) / 60, remaining % 60
      } else {
        printf "\r%s gelesen | %s/s", human(written), human(rate)
      }
    }
  '
done
wait "$DD_PID"
DD_STATUS=$?
printf '\n'
set -e

if [[ $DD_STATUS -ne 0 ]]; then
  cat "$DD_LOG" >&2
  echo "Fehler beim Erstellen des Images von $DISK_RAW." >&2
  rm -f "$DD_LOG"
  exit 1
fi

rm -f "$DD_LOG"

echo "==> Laufwerk auswerfen"
diskutil eject "$DISK_DEV" || true

sudo chown "$(id -u):$(id -g)" "$OUTPUT"

if [[ $COMPRESS -eq 1 ]]; then
  echo "==> Komprimiere ${OUTPUT} zu ${OUTPUT}.gz (Raspberry Pi Imager unterstützt .img.gz direkt)"
  gzip -f -k "$OUTPUT"
fi

echo ""
echo "Fertig: ${OUTPUT}$( [[ $COMPRESS -eq 1 ]] && echo " und ${OUTPUT}.gz" )"
echo "Im Raspberry Pi Imager: 'Choose OS' → 'Use custom' → Datei auswählen."
