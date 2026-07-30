#!/bin/bash
set -euo pipefail

# Copies the validated package only. It deliberately does not load/activate it.
ENGINE_ROOT="/Users/jeramiehicks/neuruh-repos/NeuruhAI/landos-engine"
CONTROLLED_PYTHON="${ENGINE_ROOT}/.venv/bin/python"
VALIDATOR="${ENGINE_ROOT}/deploy/validate_orchestrator_package.py"
SOURCE_PLIST="${ENGINE_ROOT}/deploy/com.neuruh.landos.orchestrator.plist"
TARGET_DIR="/Users/jeramiehicks/Library/LaunchAgents"
TARGET_PLIST="${TARGET_DIR}/com.neuruh.landos.orchestrator.plist"
LOG_DIR="/Users/jeramiehicks/Library/Logs/Neuruh/LandOS"
STATE_DIR="/Users/jeramiehicks/Library/Application Support/Neuruh/LandOS"

if [[ "${1:-}" != "--copy-only" ]]; then
  echo "Usage: $0 --copy-only" >&2
  echo "Copies a dry-run plist; does not call launchctl." >&2
  exit 64
fi

"${CONTROLLED_PYTHON}" "${VALIDATOR}" --package-root "${ENGINE_ROOT}"

/bin/mkdir -p "${TARGET_DIR}" "${LOG_DIR}" "${STATE_DIR}"
/bin/chmod 700 "${LOG_DIR}" "${STATE_DIR}"
if [[ -e "${TARGET_PLIST}" ]]; then
  echo "Refusing to overwrite existing ${TARGET_PLIST}; uninstall/archive it first." >&2
  exit 73
fi
/usr/bin/install -m 0644 "${SOURCE_PLIST}" "${TARGET_PLIST}"

echo "COPIED, NOT LOADED: ${TARGET_PLIST}"
echo "Mode: dry-run only"
