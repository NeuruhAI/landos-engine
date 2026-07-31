#!/bin/bash
set -euo pipefail

# Recoverable file removal only. This script never invokes launchctl.
TARGET_PLIST="/Users/jeramiehicks/Library/LaunchAgents/com.neuruh.landos.orchestrator.plist"
ROLLBACK_DIR="/Users/jeramiehicks/Library/Application Support/Neuruh/LandOS/rollback"

usage() {
  echo "Usage:" >&2
  echo "  $0 uninstall --confirm-not-loaded" >&2
  echo "  $0 rollback /absolute/path/to/archived.plist --confirm-not-loaded" >&2
  exit 64
}

case "${1:-}" in
  uninstall)
    [[ "${2:-}" == "--confirm-not-loaded" ]] || usage
    if [[ ! -e "${TARGET_PLIST}" ]]; then
      echo "NOT INSTALLED: ${TARGET_PLIST}"
      exit 0
    fi
    /bin/mkdir -p "${ROLLBACK_DIR}"
    /bin/chmod 700 "${ROLLBACK_DIR}"
    stamp="$(/bin/date -u +%Y%m%dT%H%M%SZ)"
    archived="${ROLLBACK_DIR}/com.neuruh.landos.orchestrator.${stamp}.$$.plist"
    /bin/mv "${TARGET_PLIST}" "${archived}"
    echo "ARCHIVED, NOT DELETED: ${archived}"
    ;;
  rollback)
    archived="${2:-}"
    [[ "${3:-}" == "--confirm-not-loaded" ]] || usage
    [[ "${archived}" == /* && -f "${archived}" ]] || {
      echo "Rollback source must be an existing absolute file." >&2
      exit 66
    }
    [[ ! -e "${TARGET_PLIST}" ]] || {
      echo "Refusing to overwrite existing ${TARGET_PLIST}." >&2
      exit 73
    }
    /usr/bin/install -m 0644 "${archived}" "${TARGET_PLIST}"
    echo "RESTORED, NOT LOADED: ${TARGET_PLIST}"
    ;;
  *)
    usage
    ;;
esac
