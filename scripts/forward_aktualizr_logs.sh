#!/usr/bin/env bash
set -euo pipefail

# Simple journal forwarder: aktualizr-torizon -> plain text log file.
#
# Why this exists:
# - In containerized setups, reading journald directly from the app container can be inconvenient.
# - This script forwards the unit logs into a plain text file that can be bind-mounted.
#
# Behavior:
# - On start, it truncates the output log so the UI does not replay stale events.
# - Then it follows journalctl forever and appends to the log file.
# - Rotates the log if it grows too large.
#
# Comments intentionally in English.

BASE_DIR="${BASE_DIR:-/home/torizon/ota-progress}"
OUT_FILE="${OUT_FILE:-${BASE_DIR}/aktualizr.log}"

# Unit name can vary across systems. Override if needed:
#   OTA_JOURNAL_UNIT=aktualizr-torizon
OTA_JOURNAL_UNIT="${OTA_JOURNAL_UNIT:-aktualizr-torizon}"

# Rotate when file exceeds this size (MB) and keep last N rotated files.
MAX_SIZE_MB="${MAX_SIZE_MB:-20}"
KEEP_FILES="${KEEP_FILES:-5}"

mkdir -p "${BASE_DIR}"

truncate_on_start() {
  # Always start from a clean session log to prevent replaying old completion lines.
  : > "${OUT_FILE}"
  chmod 0644 "${OUT_FILE}" || true
}

rotate_if_needed() {
  if [[ -f "${OUT_FILE}" ]]; then
    local size_bytes
    size_bytes=$(stat -c%s "${OUT_FILE}" 2>/dev/null || echo 0)
    local max_bytes=$(( MAX_SIZE_MB * 1024 * 1024 ))
    if (( size_bytes >= max_bytes )); then
      local ts
      ts=$(date +"%Y%m%d-%H%M%S")
      mv "${OUT_FILE}" "${OUT_FILE}.${ts}"

      # Keep only last KEEP_FILES rotated logs.
      ls -1t "${OUT_FILE}."* 2>/dev/null | tail -n +$((KEEP_FILES + 1)) | xargs -r rm -f

      # Create a new empty file for continued appending.
      : > "${OUT_FILE}"
      chmod 0644 "${OUT_FILE}" || true
    fi
  else
    : > "${OUT_FILE}"
    chmod 0644 "${OUT_FILE}" || true
  fi
}

echo "[forwarder] Base dir: ${BASE_DIR}"
echo "[forwarder] Output file: ${OUT_FILE}"
echo "[forwarder] Unit: ${OTA_JOURNAL_UNIT}"
echo "[forwarder] Rotating at ~${MAX_SIZE_MB} MB, keeping ${KEEP_FILES} files"
echo "[forwarder] Press Ctrl+C to stop"

# Start with a clean log session.
truncate_on_start

# Follow logs forever.
# -o cat prints only the log message (stable output format).
# stdbuf forces line buffering so UI can react quickly.
while true; do
  rotate_if_needed

  stdbuf -oL -eL journalctl -u "${OTA_JOURNAL_UNIT}" -f -n 0 -o cat >> "${OUT_FILE}" 2>/dev/null || true

  # If journalctl exits (rare), retry after a short pause.
  sleep 1
done
