#!/usr/bin/env bash
# ==============================================================================
# Out-of-Band Deployment Sync to Orange Pi PC Plus SBC
# Target Hardware: Orange Pi PC Plus (Allwinner H3, 1GB DDR3 RAM, 8GB eMMC)
# Target OS      : Armbian Linux (Debian 13 trixie / headless minimal CLI)
# Target Path    : /opt/label_system/
# Target Service : autolabel.service
# ==============================================================================
# Strict Integrity & Security Rules:
# 1. Strictly excludes .env, credentials, git databases, caches, and test logs.
# 2. Uses rsync over SSH when available; falls back to tar pipe over SSH.
# 3. Ensures executable permissions on all remote shell scripts.
# 4. Outputs post-sync instructions and systemd service verification steps.
# 5. Pure ASCII output, strictly 0 emojis.
# ==============================================================================

set -euo pipefail

# Default deployment configuration
DEFAULT_HOST="192.168.3.50"
ORANGEPI_HOST="${ORANGEPI_HOST:-}"
ORANGEPI_USER="${ORANGEPI_USER:-root}"
ORANGEPI_PORT="${ORANGEPI_PORT:-22}"
ORANGEPI_TARGET="${ORANGEPI_TARGET:-/opt/label_system/}"
SSH_KEY=""
DRY_RUN=0
RESTART_SERVICE=0

# Determine script and project directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Usage function
print_usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  -h, --host <HOST>       Target Orange Pi IP or hostname (default: \$ORANGEPI_HOST or prompted)
  -u, --user <USER>       Remote SSH user (default: root)
  -p, --port <PORT>       Remote SSH port (default: 22)
  -t, --target <PATH>     Remote target directory (default: /opt/label_system/)
  -i, --identity <KEY>    Path to SSH private key file
  -n, --dry-run           Show files that would be transferred without modifying target
  -r, --restart-service   Automatically reload systemd and restart autolabel.service
  --help                  Show this help message and exit

Environment Variables:
  ORANGEPI_HOST           Default target IP address
  ORANGEPI_USER           Default SSH username
  ORANGEPI_PORT           Default SSH port
  ORANGEPI_TARGET         Default remote installation path

Description:
  Safely synchronizes the Autonomous Label Orchestrator codebase from
  the local development environment to the Orange Pi PC Plus SBC over
  SSH/rsync while strictly isolating secrets (.env) and runtime artifacts.
EOF
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--host)
            ORANGEPI_HOST="$2"
            shift 2
            ;;
        -u|--user)
            ORANGEPI_USER="$2"
            shift 2
            ;;
        -p|--port)
            ORANGEPI_PORT="$2"
            shift 2
            ;;
        -t|--target)
            ORANGEPI_TARGET="$2"
            shift 2
            ;;
        -i|--identity)
            SSH_KEY="$2"
            shift 2
            ;;
        -n|--dry-run)
            DRY_RUN=1
            shift
            ;;
        -r|--restart-service)
            RESTART_SERVICE=1
            shift
            ;;
        --help)
            print_usage
            exit 0
            ;;
        *)
            echo "[ERROR] Unknown option: $1" >&2
            print_usage
            exit 1
            ;;
    esac
done

# If host is not provided via flag or environment, prompt or use default
if [ -z "${ORANGEPI_HOST}" ]; then
    if [ -t 0 ]; then
        printf "Enter Orange Pi IP address [%s]: " "${DEFAULT_HOST}"
        read -r input_host
        ORANGEPI_HOST="${input_host:-${DEFAULT_HOST}}"
    else
        ORANGEPI_HOST="${DEFAULT_HOST}"
    fi
fi

# Ensure target path has trailing slash for directory sync
if [[ "${ORANGEPI_TARGET}" != */ ]]; then
    ORANGEPI_TARGET="${ORANGEPI_TARGET}/"
fi

# Assemble SSH command options
SSH_OPTS=("-p" "${ORANGEPI_PORT}" "-o" "StrictHostKeyChecking=accept-new" "-o" "ConnectTimeout=10")
if [ -n "${SSH_KEY}" ]; then
    SSH_OPTS+=("-i" "${SSH_KEY}")
fi

SSH_DEST="${ORANGEPI_USER}@${ORANGEPI_HOST}"

echo "=============================================================================="
echo " Autonomous Label Orchestrator - Out-of-Band Orange Pi Deployment Sync"
echo " Target Host  : ${SSH_DEST}:${ORANGEPI_PORT}"
echo " Target Path  : ${ORANGEPI_TARGET}"
echo " Source Path  : ${PROJECT_ROOT}"
echo " Dry Run Mode : $( [ "${DRY_RUN}" -eq 1 ] && echo "ENABLED" || echo "DISABLED" )"
echo "=============================================================================="

# Pre-flight check: Verify source directory exists
if [ ! -d "${PROJECT_ROOT}/daemon" ] || [ ! -d "${PROJECT_ROOT}/scripts" ]; then
    echo "[ERROR] Invalid project root: '${PROJECT_ROOT}' does not contain expected daemon/ or scripts/ directories." >&2
    exit 1
fi

# Strict exclusion list (secrets, git metadata, bytecode, logs, test artifacts)
EXCLUDE_LIST=(
    ".env"
    ".env.*"
    ".git"
    ".git/"
    ".gitignore"
    "__pycache__"
    "__pycache__/"
    "*/__pycache__/*"
    "*.pyc"
    "*.pyo"
    "*.pyd"
    ".pytest_cache"
    ".pytest_cache/"
    "tmp"
    "tmp/"
    "scratch"
    "scratch/"
    ".agents"
    ".agents/"
    "*.log"
    "*.sqlite3"
    ".sdcard_write_probe*"
    "backup_storage"
    "backup_storage/"
    "audit_logs"
    "audit_logs/"
    ".DS_Store"
    "Thumbs.db"
    "*.swp"
    "*.swo"
    "*~"
)

# Build rsync exclude arguments
RSYNC_EXCLUDES=()
for pattern in "${EXCLUDE_LIST[@]}"; do
    RSYNC_EXCLUDES+=("--exclude=${pattern}")
done

# Perform synchronization
if command -v rsync >/dev/null 2>&1; then
    echo "[INFO] Using rsync for incremental synchronization..."
    RSYNC_CMD=(
        rsync
        -avz
        --delete
        -e "ssh ${SSH_OPTS[*]}"
        "${RSYNC_EXCLUDES[@]}"
    )

    if [ "${DRY_RUN}" -eq 1 ]; then
        RSYNC_CMD+=("--dry-run")
    fi

    # Sync primary application directories and configuration files
    echo "[INFO] Executing rsync from '${PROJECT_ROOT}/' to '${SSH_DEST}:${ORANGEPI_TARGET}'..."
    "${RSYNC_CMD[@]}" \
        "${PROJECT_ROOT}/daemon" \
        "${PROJECT_ROOT}/web" \
        "${PROJECT_ROOT}/templates" \
        "${PROJECT_ROOT}/scripts" \
        "${PROJECT_ROOT}/systemd" \
        "${PROJECT_ROOT}/requirements.txt" \
        "${PROJECT_ROOT}/.env.example" \
        "${SSH_DEST}:${ORANGEPI_TARGET}"

else
    echo "[WARN] rsync not found on host. Falling back to tar over SSH..."

    # Create remote target directory
    if [ "${DRY_RUN}" -eq 0 ]; then
        ssh "${SSH_OPTS[@]}" "${SSH_DEST}" "mkdir -p '${ORANGEPI_TARGET}'"
    fi

    # Build tar exclude arguments
    TAR_EXCLUDES=()
    for pattern in "${EXCLUDE_LIST[@]}"; do
        TAR_EXCLUDES+=("--exclude=${pattern}")
    done

    echo "[INFO] Streaming archive to remote host via SSH pipeline..."
    if [ "${DRY_RUN}" -eq 1 ]; then
        echo "[DRY-RUN] Would execute: tar -czf - -C '${PROJECT_ROOT}' daemon web templates scripts systemd requirements.txt .env.example | ssh ${SSH_DEST} 'tar -xzf - -C ${ORANGEPI_TARGET}'"
    else
        tar -czf - \
            -C "${PROJECT_ROOT}" \
            "${TAR_EXCLUDES[@]}" \
            daemon web templates scripts systemd requirements.txt .env.example \
            | ssh "${SSH_OPTS[@]}" "${SSH_DEST}" "tar -xzf - -C '${ORANGEPI_TARGET}'"
    fi
fi

# Ensure remote permissions and post-sync steps
if [ "${DRY_RUN}" -eq 0 ]; then
    echo "[INFO] Setting executable permissions on remote scripts..."
    ssh "${SSH_OPTS[@]}" "${SSH_DEST}" "chmod +x '${ORANGEPI_TARGET}'scripts/*.sh 2>/dev/null || true"

    if [ "${RESTART_SERVICE}" -eq 1 ]; then
        echo "[INFO] Applying systemd service update and restarting autolabel.service..."
        ssh "${SSH_OPTS[@]}" "${SSH_DEST}" bash << 'REMOTE_COMMANDS'
            set -e
            if [ -f /opt/label_system/systemd/autolabel.service ]; then
                cp /opt/label_system/systemd/autolabel.service /etc/systemd/system/autolabel.service
                systemctl daemon-reload
                systemctl enable autolabel.service
                systemctl restart autolabel.service
                echo "[INFO] autolabel.service successfully restarted."
            else
                echo "[WARN] /opt/label_system/systemd/autolabel.service not found."
            fi
REMOTE_COMMANDS
    fi
fi

echo "=============================================================================="
echo " Synchronization Complete!"
echo "=============================================================================="
echo "Post-Sync Instructions for Orange Pi PC Plus:"
echo "1. Verify .env configuration on remote target:"
echo "   ssh ${SSH_DEST} 'cat ${ORANGEPI_TARGET}.env'"
echo "   (If .env does not exist, copy from .env.example and populate Gemini API keys)"
echo ""
echo "2. Install/update systemd service unit:"
echo "   ssh ${SSH_DEST} 'cp ${ORANGEPI_TARGET}systemd/autolabel.service /etc/systemd/system/'"
echo "   ssh ${SSH_DEST} 'systemctl daemon-reload'"
echo ""
echo "3. Restart and verify service status:"
echo "   ssh ${SSH_DEST} 'systemctl restart autolabel.service'"
echo "   ssh ${SSH_DEST} 'systemctl status autolabel.service'"
echo "   ssh ${SSH_DEST} 'journalctl -u autolabel.service -f -n 50'"
echo ""
echo "4. Verify web dashboard connectivity:"
echo "   http://${ORANGEPI_HOST}:8080"
echo "=============================================================================="
