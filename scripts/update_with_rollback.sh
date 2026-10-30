#!/usr/bin/env bash
# ==============================================================================
# Resilient GitHub Auto-Update with Rollback Safeguards
# Target Platform: Orange Pi PC Plus (Armbian Linux trixie)
# Service Target : autolabel.service
# ==============================================================================
# Implements an 8-state resilient update engine:
#   1. IDLE
#   2. PRE_FLIGHT_CHECK  (Disk > 500MB, Network, Git repo validity)
#   3. SNAPSHOT_CREATION (Stage tarball to SD card, prune keeping 5 newest)
#   4. FETCH_AND_RESET   (Stash local edits, atomic git fetch/reset, protect .env)
#   5. DEPENDENCY_VALID  (Check requirements.txt diff, run pip if changed)
#   6. SERVICE_RESTART   (Non-blocking restart of autolabel.service)
#   7. HEALTH_VALIDATION (30-second probe on port 8080 /api/status)
#   8. COMMITTED / ROLLBACK_EXECUTE (Commit on success; automatic rollback on failure)
# ==============================================================================

set -euo pipefail
export GIT_TERMINAL_PROMPT=0

# Default configuration
APP_DIR=""
STORAGE_DIR=""
MIN_DISK_MB=500
HEALTH_URL="http://127.0.0.1:8080/api/status"
HEALTH_TIMEOUT=30
GIT_REMOTE="origin"
GIT_BRANCH="main"
STATE_FILE_OVERRIDE=""

# Flags
CHECK_ONLY=0
DRY_RUN=0
SIMULATE_SUCCESS=0
SIMULATE_FAILURE=0
SIMULATE_FETCH_FAILURE=0
SIMULATE_DEP_FAILURE=0

# Runtime variables
OLD_COMMIT="v1.0.0"
NEW_COMMIT=""
SNAPSHOT_FILE=""
TEMP_FILES=()

# ------------------------------------------------------------------------------
# Logging & Helper Functions
# ------------------------------------------------------------------------------

log_info() {
    echo "[INFO] $(date -u '+%Y-%m-%d %H:%M:%SZ') $1"
}

log_warn() {
    echo "[WARN] $(date -u '+%Y-%m-%d %H:%M:%SZ') $1" >&2
}

log_error() {
    echo "[ERROR] $(date -u '+%Y-%m-%d %H:%M:%SZ') $1" >&2
}

log_state() {
    echo "[STATE] $(date -u '+%Y-%m-%d %H:%M:%SZ') Step $1/7: State -> $2: $3"
}

cleanup() {
    for f in "${TEMP_FILES[@]}"; do
        if [ -n "$f" ] && [ -e "$f" ]; then
            rm -rf "$f" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT

# ------------------------------------------------------------------------------
# Usage Information
# ------------------------------------------------------------------------------

print_usage() {
    cat << 'EOF'
Usage: update_with_rollback.sh [OPTIONS]

Options:
  --check                   Run pre-flight checks only and exit (non-destructive)
  --dry-run, -n             Simulate full update cycle without modifying repo/services
  --simulate-success        Simulate successful update without live network/systemd
  --simulate-failure        Simulate health check failure to trigger automatic rollback
  --simulate-fetch-failure  Simulate network drop during git fetch
  --simulate-dep-failure    Simulate dependency installation failure
  --app-dir <PATH>          Path to application root directory
  --storage-dir <PATH>      Path to SD card backup/snapshot storage directory
  --state-file <PATH>       Override path for JSON state file
  --health-url <URL>        Health check endpoint (default: http://127.0.0.1:8080/api/status)
  --health-timeout <SEC>    Health probe timeout in seconds (default: 30)
  --min-disk-mb <MB>        Minimum free disk space threshold (default: 500)
  --remote <NAME>           Git remote name (default: origin)
  --branch <NAME>           Git branch name (default: main)
  --help, -h                Display this help message and exit

Description:
  Executes an 8-state atomic auto-update with automatic rollback safeguards.
  Creates a pre-update snapshot archive on the SD card, applies git updates,
  validates dependencies, restarts the service, and verifies health on port 8080.
  Automatically rolls back to previous commit and snapshot if health check fails.
EOF
}

# ------------------------------------------------------------------------------
# Argument Parsing
# ------------------------------------------------------------------------------

parse_arguments() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --check)
                CHECK_ONLY=1
                shift
                ;;
            --dry-run|-n)
                DRY_RUN=1
                shift
                ;;
            --simulate-success)
                SIMULATE_SUCCESS=1
                shift
                ;;
            --simulate-failure)
                SIMULATE_FAILURE=1
                shift
                ;;
            --simulate-fetch-failure)
                SIMULATE_FETCH_FAILURE=1
                shift
                ;;
            --simulate-dep-failure)
                SIMULATE_DEP_FAILURE=1
                shift
                ;;
            --app-dir)
                APP_DIR="$2"
                shift 2
                ;;
            --storage-dir)
                STORAGE_DIR="$2"
                shift 2
                ;;
            --state-file)
                STATE_FILE_OVERRIDE="$2"
                shift 2
                ;;
            --health-url)
                HEALTH_URL="$2"
                shift 2
                ;;
            --health-timeout)
                HEALTH_TIMEOUT="$2"
                shift 2
                ;;
            --min-disk-mb)
                MIN_DISK_MB="$2"
                shift 2
                ;;
            --remote)
                GIT_REMOTE="$2"
                shift 2
                ;;
            --branch)
                GIT_BRANCH="$2"
                shift 2
                ;;
            --help|-h)
                print_usage
                exit 0
                ;;
            *)
                log_error "Unknown option: $1"
                print_usage
                exit 1
                ;;
        esac
    done
}

# ------------------------------------------------------------------------------
# Environment & Directory Setup
# ------------------------------------------------------------------------------

init_environment() {
    # Resolve APP_DIR if not provided
    if [ -z "$APP_DIR" ]; then
        if [ -d "/opt/label_system" ]; then
            APP_DIR="/opt/label_system"
        else
            APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
        fi
    fi

    # Resolve STORAGE_DIR if not provided
    if [ -z "$STORAGE_DIR" ]; then
        if [ -d "/mnt/sdcard_backup" ]; then
            STORAGE_DIR="/mnt/sdcard_backup"
        else
            STORAGE_DIR="${APP_DIR}/backup_storage"
        fi
    fi

    # Normalize paths under MSYS/Cygwin/Git-Bash if present
    if command -v cygpath >/dev/null 2>&1; then
        [ -n "$APP_DIR" ] && APP_DIR=$(cygpath -u "$APP_DIR")
        [ -n "$STORAGE_DIR" ] && STORAGE_DIR=$(cygpath -u "$STORAGE_DIR")
        [ -n "$STATE_FILE_OVERRIDE" ] && STATE_FILE_OVERRIDE=$(cygpath -u "$STATE_FILE_OVERRIDE")
    fi

    SNAPSHOT_DIR="${STORAGE_DIR}/snapshots"
    RECOVERY_DIR="${STORAGE_DIR}/rollback_recovery"
    AUDIT_DIR="${STORAGE_DIR}/audit_logs"

    # In check/dry-run mode without existing storage dir, use local fallback
    if [ "$DRY_RUN" -eq 1 ] || [ "$CHECK_ONLY" -eq 1 ]; then
        mkdir -p "${SNAPSHOT_DIR}" 2>/dev/null || true
        mkdir -p "${RECOVERY_DIR}" 2>/dev/null || true
        mkdir -p "${AUDIT_DIR}" 2>/dev/null || true
    else
        mkdir -p "${SNAPSHOT_DIR}" "${RECOVERY_DIR}" "${AUDIT_DIR}"
    fi

    UPDATE_STATE_FILE="${RECOVERY_DIR}/update_state.json"
    ROLLBACK_STATE_FILE="${RECOVERY_DIR}/rollback_state.json"
    AUDIT_FAILURE_LOG="${AUDIT_DIR}/update_failures.log"

    if [ -n "$STATE_FILE_OVERRIDE" ]; then
        UPDATE_STATE_FILE="$STATE_FILE_OVERRIDE"
        mkdir -p "$(dirname "$UPDATE_STATE_FILE")" 2>/dev/null || true
    fi
}

# ------------------------------------------------------------------------------
# State File Persistence
# ------------------------------------------------------------------------------

set_state() {
    local state_code="$1"
    local state_alias="$2"
    local step_num="$3"
    local status_code="$4"
    local message="$5"
    local now_epoch
    now_epoch=$(date +%s)
    local now_iso
    now_iso=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    log_state "$step_num" "$state_code" "$message"

    local tmp_file
    tmp_file="${UPDATE_STATE_FILE}.tmp.$$"
    TEMP_FILES+=("$tmp_file")

    cat > "$tmp_file" << EOF
{
  "state": "${state_code}",
  "state_alias": "${state_alias}",
  "step": ${step_num},
  "status": "${status_code}",
  "timestamp": ${now_epoch},
  "iso_timestamp": "${now_iso}",
  "previous_commit": "${OLD_COMMIT}",
  "current_commit": "${NEW_COMMIT:-$OLD_COMMIT}",
  "snapshot_path": "${SNAPSHOT_FILE}",
  "message": "${message}"
}
EOF

    mv -f "$tmp_file" "$UPDATE_STATE_FILE" 2>/dev/null || cp -f "$tmp_file" "$UPDATE_STATE_FILE"
}

write_rollback_state() {
    local prev_commit="$1"
    local status_str="$2"
    local reason_str="$3"
    local now_epoch
    now_epoch=$(date +%s)
    local now_iso
    now_iso=$(date -u '+%Y-%m-%dT%H:%M:%SZ')

    local tmp_rb
    tmp_rb="${ROLLBACK_STATE_FILE}.tmp.$$"
    TEMP_FILES+=("$tmp_rb")

    cat > "$tmp_rb" << EOF
{
  "previous_commit": "${prev_commit}",
  "timestamp": ${now_epoch},
  "iso_timestamp": "${now_iso}",
  "status": "${status_str}",
  "reason": "${reason_str}",
  "recovered_commit": "${prev_commit}"
}
EOF

    mv -f "$tmp_rb" "$ROLLBACK_STATE_FILE" 2>/dev/null || cp -f "$tmp_rb" "$ROLLBACK_STATE_FILE"
}

log_audit_failure() {
    local reason="$1"
    local now_iso
    now_iso=$(date -u '+%Y-%m-%d %H:%M:%SZ')
    mkdir -p "$(dirname "$AUDIT_FAILURE_LOG")" 2>/dev/null || true
    echo "[ROLLBACK_ALERT] ${now_iso} Failed at state ${CURRENT_STATE:-UNKNOWN}. Rolling back to ${OLD_COMMIT}. Reason: ${reason}" >> "$AUDIT_FAILURE_LOG" 2>/dev/null || true
}

# ------------------------------------------------------------------------------
# State 1: PRE_FLIGHT_CHECK
# ------------------------------------------------------------------------------

run_preflight_checks() {
    CURRENT_STATE="PRE_FLIGHT_CHECK"
    set_state "PRE_FLIGHT_CHECK" "PRE_FLIGHT" 1 "IN_PROGRESS" "Executing pre-flight checks (disk space, network, git state)"

    # Auto-restore .env from persistent storage if missing (power-loss recovery)
    if [ ! -f "${APP_DIR}/.env" ] && [ -f "${STORAGE_DIR}/rollback_recovery/.env.persist" ]; then
        log_warn "Detected missing .env in application directory. Restoring from persistent SD backup..."
        cp -f "${STORAGE_DIR}/rollback_recovery/.env.persist" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
    fi

    # 1. Disk space verification (>500MB free)
    local free_kb=0
    if command -v df >/dev/null 2>&1; then
        free_kb=$(df -k "$APP_DIR" 2>/dev/null | tail -n 1 | awk '{print $(NF-2)}')
        # Fallback if field index varies across OS versions
        if [ -z "$free_kb" ] || ! [[ "$free_kb" =~ ^[0-9]+$ ]]; then
            free_kb=$(df -P -k "$APP_DIR" 2>/dev/null | tail -n 1 | awk '{print $4}')
        fi
    fi

    local min_kb=$(( MIN_DISK_MB * 1024 ))
    if [ -n "$free_kb" ] && [[ "$free_kb" =~ ^[0-9]+$ ]]; then
        local free_mb=$(( free_kb / 1024 ))
        if [ "$free_kb" -lt "$min_kb" ]; then
            log_error "Pre-flight failed: Insufficient disk space (${free_mb}MB free, minimum ${MIN_DISK_MB}MB required)."
            return 1
        fi
        log_info "Pre-flight disk check passed: ${free_mb}MB free (minimum: ${MIN_DISK_MB}MB)."
    else
        log_warn "Could not determine free disk space with df. Proceeding with caution."
    fi

    # 2. Network connectivity check
    if [ "$DRY_RUN" -eq 0 ] && [ "$SIMULATE_SUCCESS" -eq 0 ] && [ "$SIMULATE_FAILURE" -eq 0 ]; then
        local net_ok=0
        if command -v curl >/dev/null 2>&1; then
            if curl -fsS --max-time 4 -I "https://github.com" >/dev/null 2>&1; then
                net_ok=1
            fi
        elif command -v ping >/dev/null 2>&1; then
            if ping -c 1 -W 2 1.1.1.1 >/dev/null 2>&1; then
                net_ok=1
            fi
        else
            net_ok=1
        fi

        if [ "$net_ok" -eq 0 ]; then
            log_error "Pre-flight failed: Network connectivity to GitHub/Internet is unavailable."
            return 1
        fi
        log_info "Pre-flight network check passed: Remote connectivity verified."
    else
        log_info "Pre-flight network check bypassed in simulation mode."
    fi

    # 3. Git repository validation
    if [ "$DRY_RUN" -eq 0 ] && [ "$SIMULATE_SUCCESS" -eq 0 ] && [ "$SIMULATE_FAILURE" -eq 0 ]; then
        if ! git -C "$APP_DIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
            log_error "Pre-flight failed: ${APP_DIR} is not a valid git repository."
            return 1
        fi
        log_info "Pre-flight git check passed: Valid repository work tree."
    else
        log_info "Pre-flight git check passed in simulation mode."
    fi

    return 0
}

# ------------------------------------------------------------------------------
# State 2: SNAPSHOT_CREATION
# ------------------------------------------------------------------------------

run_snapshot_creation() {
    CURRENT_STATE="SNAPSHOT_CREATION"
    set_state "SNAPSHOT_CREATION" "SNAPSHOT" 2 "IN_PROGRESS" "Creating pre-update SD card snapshot archive"

    # Query current git commit hash
    if [ "$DRY_RUN" -eq 1 ] || [ "$SIMULATE_SUCCESS" -eq 1 ] || [ "$SIMULATE_FAILURE" -eq 1 ]; then
        OLD_COMMIT=$(git -C "$APP_DIR" rev-parse --short HEAD 2>/dev/null || echo "c0ffee1")
    else
        OLD_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo "initial")
    fi

    local ts
    ts=$(date +%Y%m%d_%H%M%S)
    SNAPSHOT_FILE="${SNAPSHOT_DIR}/snapshot_${OLD_COMMIT}_${ts}.tar.gz"

    # Collect existing target subdirectories to archive
    local dirs_to_archive=()
    for dir_name in daemon web templates scripts; do
        if [ -d "${APP_DIR}/${dir_name}" ]; then
            dirs_to_archive+=("${dir_name}")
        fi
    done

    if [ ${#dirs_to_archive[@]} -eq 0 ]; then
        log_warn "No standard subdirectories found to archive in ${APP_DIR}."
        # Create minimal snapshot marker
        mkdir -p "${SNAPSHOT_DIR}"
        echo "snapshot_marker_${OLD_COMMIT}" > "${SNAPSHOT_DIR}/snapshot_marker.tmp"
        tar --force-local -czf "${SNAPSHOT_FILE}" -C "${SNAPSHOT_DIR}" snapshot_marker.tmp 2>/dev/null || true
        rm -f "${SNAPSHOT_DIR}/snapshot_marker.tmp"
    else
        log_info "Creating snapshot archive: ${SNAPSHOT_FILE}"
        tar --force-local -czf "${SNAPSHOT_FILE}" -C "${APP_DIR}" "${dirs_to_archive[@]}"
    fi

    if [ ! -f "${SNAPSHOT_FILE}" ]; then
        log_error "Snapshot creation failed: Target file ${SNAPSHOT_FILE} does not exist."
        return 1
    fi

    local snap_size
    snap_size=$(stat -c%s "${SNAPSHOT_FILE}" 2>/dev/null || wc -c < "${SNAPSHOT_FILE}" 2>/dev/null || echo 0)
    log_info "Snapshot archive verified: ${snap_size} bytes."

    # Snapshot retention pruning (Keep 5 newest snapshots)
    prune_snapshots "$SNAPSHOT_DIR" 5

    return 0
}

prune_snapshots() {
    local target_dir="$1"
    local keep_count="$2"

    [ -d "$target_dir" ] || return 0

    # List snapshots sorted by modification time (newest first)
    local snapshots=()
    while IFS= read -r f; do
        [ -n "$f" ] && snapshots+=("$f")
    done < <(find "$target_dir" -maxdepth 1 -name "snapshot_*.tar.gz" -type f -printf "%T@ %p\n" 2>/dev/null | sort -nr | awk '{print $2}')

    # If printf not available (e.g. busybox / BSD find), fallback to ls -t
    if [ ${#snapshots[@]} -eq 0 ]; then
        while IFS= read -r f; do
            [ -n "$f" ] && snapshots+=("$f")
        done < <(ls -t "${target_dir}"/snapshot_*.tar.gz 2>/dev/null || true)
    fi

    local total=${#snapshots[@]}
    if [ "$total" -gt "$keep_count" ]; then
        local to_remove=$(( total - keep_count ))
        log_info "Pruning snapshots: ${total} present, keeping ${keep_count} newest. Removing ${to_remove} older archives."
        for ((i=keep_count; i<total; i++)); do
            local old_snap="${snapshots[i]}"
            log_info "Pruning old snapshot: $(basename "$old_snap")"
            rm -f "$old_snap" 2>/dev/null || true
        done
    fi
}

# ------------------------------------------------------------------------------
# State 3: FETCH_AND_RESET
# ------------------------------------------------------------------------------

run_fetch_and_reset() {
    CURRENT_STATE="FETCH_AND_RESET"
    set_state "FETCH_AND_RESET" "FETCH_AND_RESET" 3 "IN_PROGRESS" "Fetching remote updates and staging reset"

    if [ "$SIMULATE_FETCH_FAILURE" -eq 1 ]; then
        log_error "Simulated network drop during git fetch (exit code 128)."
        return 128
    fi

    if [ "$DRY_RUN" -eq 1 ] || [ "$SIMULATE_SUCCESS" -eq 1 ] || [ "$SIMULATE_FAILURE" -eq 1 ] || [ "$SIMULATE_DEP_FAILURE" -eq 1 ]; then
        log_info "[DRY-RUN] Stashing dirty working tree: git stash push -u -m 'autolabel_update'"
        log_info "[DRY-RUN] Fetching remote: git fetch ${GIT_REMOTE} ${GIT_BRANCH}"
        log_info "[DRY-RUN] Hard reset to origin: git reset --hard ${GIT_REMOTE}/${GIT_BRANCH}"
        NEW_COMMIT="beef999"
        return 0
    fi

    # Protect .env configuration file with persistent SD card and memory fallback
    local env_backup=""
    local persistent_env="${STORAGE_DIR}/rollback_recovery/.env.persist"
    mkdir -p "${STORAGE_DIR}/rollback_recovery" 2>/dev/null || true
    if [ -f "${APP_DIR}/.env" ]; then
        env_backup=$(mktemp)
        TEMP_FILES+=("$env_backup")
        cp -f "${APP_DIR}/.env" "$env_backup"
        cp -f "${APP_DIR}/.env" "$persistent_env" 2>/dev/null || true
        chmod 600 "$persistent_env" 2>/dev/null || true
    elif [ -f "$persistent_env" ]; then
        # Recover missing .env from persistent storage before proceeding
        cp -f "$persistent_env" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
        env_backup="$persistent_env"
    fi

    # Stash any uncommitted modifications
    log_info "Stashing local changes..."
    git -C "$APP_DIR" stash push -u -m "autolabel_pre_update_$(date +%s)" 2>/dev/null || true

    # Immediately restore .env so it is NOT missing during the network fetch window
    if [ -f "$persistent_env" ] && [ ! -f "${APP_DIR}/.env" ]; then
        cp -f "$persistent_env" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
    elif [ -n "$env_backup" ] && [ -f "$env_backup" ] && [ ! -f "${APP_DIR}/.env" ]; then
        cp -f "$env_backup" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
    fi

    # Fetch latest upstream commits
    log_info "Fetching ${GIT_REMOTE} ${GIT_BRANCH}..."
    if ! git -C "$APP_DIR" fetch "$GIT_REMOTE" "$GIT_BRANCH"; then
        log_error "Failed to fetch from remote ${GIT_REMOTE}/${GIT_BRANCH}."
        return 1
    fi

    # Hard reset to upstream
    log_info "Resetting to ${GIT_REMOTE}/${GIT_BRANCH}..."
    if ! git -C "$APP_DIR" reset --hard "${GIT_REMOTE}/${GIT_BRANCH}"; then
        log_error "Failed to reset to ${GIT_REMOTE}/${GIT_BRANCH}."
        return 1
    fi

    NEW_COMMIT=$(git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || echo "latest")

    # Restore .env if modified or removed
    if [ -n "$env_backup" ] && [ -f "$env_backup" ]; then
        cp -f "$env_backup" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
        log_info "Verified .env isolation: Environment secrets protected and restored."
    elif [ -f "$persistent_env" ]; then
        cp -f "$persistent_env" "${APP_DIR}/.env"
        chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
        log_info "Verified .env isolation: Environment secrets protected and restored."
    fi

    return 0
}

# ------------------------------------------------------------------------------
# State 4: DEPENDENCY_VALID
# ------------------------------------------------------------------------------

run_dependency_check() {
    CURRENT_STATE="DEPENDENCY_VALID"
    set_state "DEPENDENCY_VALID" "DEPENDENCY_CHECK" 4 "IN_PROGRESS" "Validating dependencies and requirements.txt"

    if [ "$SIMULATE_DEP_FAILURE" -eq 1 ]; then
        log_error "Simulated dependency installation conflict (pip exit code 1)."
        return 1
    fi

    if [ "$DRY_RUN" -eq 1 ] || [ "$SIMULATE_SUCCESS" -eq 1 ] || [ "$SIMULATE_FAILURE" -eq 1 ]; then
        log_info "[DRY-RUN] Checking requirements.txt diff between ${OLD_COMMIT} and HEAD."
        log_info "[DRY-RUN] Dependencies verified successfully."
        return 0
    fi

    # Check if requirements.txt exists and changed between OLD_COMMIT and HEAD
    local req_file="${APP_DIR}/requirements.txt"
    if [ -f "$req_file" ]; then
        local changed=0
        if [ "$OLD_COMMIT" != "initial" ] && git -C "$APP_DIR" rev-parse "$OLD_COMMIT" >/dev/null 2>&1; then
            if git -C "$APP_DIR" diff --name-only "$OLD_COMMIT" HEAD 2>/dev/null | grep -q "^requirements.txt$"; then
                changed=1
            fi
        else
            changed=1
        fi

        if [ "$changed" -eq 1 ]; then
            log_info "requirements.txt modified: installing updated Python dependencies..."
            if command -v pip >/dev/null 2>&1; then
                if ! pip install -r "$req_file" --no-cache-dir; then
                    log_error "Dependency installation failed: pip install returned error."
                    return 1
                fi
            elif command -v pip3 >/dev/null 2>&1; then
                if ! pip3 install -r "$req_file" --no-cache-dir; then
                    log_error "Dependency installation failed: pip3 install returned error."
                    return 1
                fi
            else
                log_warn "pip command not found. Skipping dependency installation."
            fi
        else
            log_info "requirements.txt unchanged. Skipping pip install."
        fi
    fi

    return 0
}

# ------------------------------------------------------------------------------
# State 5: SERVICE_RESTART
# ------------------------------------------------------------------------------

run_service_restart() {
    CURRENT_STATE="SERVICE_RESTART"
    set_state "SERVICE_RESTART" "SERVICE_RESTART" 5 "IN_PROGRESS" "Triggering systemd autolabel.service restart"

    if [ "$DRY_RUN" -eq 1 ] || [ "$SIMULATE_SUCCESS" -eq 1 ] || [ "$SIMULATE_FAILURE" -eq 1 ]; then
        log_info "[DRY-RUN] Would execute: systemctl restart autolabel.service"
        return 0
    fi

    if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/autolabel.service" ]; then
        log_info "Restarting systemd service: autolabel.service..."
        systemctl restart autolabel.service
    else
        log_info "Systemd service not present or not running on Linux SBC. Service restart simulated."
    fi

    return 0
}

# ------------------------------------------------------------------------------
# State 6: HEALTH_VALIDATION
# ------------------------------------------------------------------------------

run_health_validation() {
    CURRENT_STATE="HEALTH_VALIDATION"
    set_state "HEALTH_VALIDATION" "HEALTH_CHECK" 6 "IN_PROGRESS" "Probing port 8080 health verification endpoint"

    # Simulated failure flag triggers immediate rollback
    if [ "$SIMULATE_FAILURE" -eq 1 ]; then
        log_error "Simulated health check failure triggered by --simulate-failure flag."
        return 1
    fi

    if [ "$SIMULATE_SUCCESS" -eq 1 ] || [ "$DRY_RUN" -eq 1 ]; then
        log_info "[DRY-RUN] Health probe validated: HTTP 200, valid schema payload."
        return 0
    fi

    log_info "Initiating health check probe against ${HEALTH_URL} (Max timeout: ${HEALTH_TIMEOUT}s)..."

    local max_attempts=$(( HEALTH_TIMEOUT / 2 ))
    [ "$max_attempts" -lt 1 ] && max_attempts=1
    local attempt=1
    local is_healthy=0

    while [ "$attempt" -le "$max_attempts" ]; do
        local tmp_body
        tmp_body=$(mktemp)
        TEMP_FILES+=("$tmp_body")

        local http_code=0
        if command -v curl >/dev/null 2>&1; then
            http_code=$(curl -s -o "$tmp_body" -w "%{http_code}" --max-time 2 "$HEALTH_URL" 2>/dev/null || echo "000")
        fi

        if [ "$http_code" = "200" ]; then
            # Verify JSON body structure contains queues, hardware, gemini_pool
            local body_content
            body_content=$(cat "$tmp_body" 2>/dev/null || echo "")
            if echo "$body_content" | grep -q "queues" && \
               echo "$body_content" | grep -q "hardware" && \
               echo "$body_content" | grep -q "gemini_pool"; then
                log_info "Health verification successful on attempt ${attempt}: HTTP 200 with complete schema."
                is_healthy=1
                break
            else
                log_warn "Attempt ${attempt}: HTTP 200 received but schema missing required keys."
            fi
        elif [ "$http_code" = "500" ]; then
            log_info "Attempt ${attempt}/${max_attempts}: Health check returned HTTP 500. Aborting immediately to trigger rollback."
            log_error "Critical: Health check returned HTTP 500 Internal Server Error. Immediate rollback triggered."
            return 1
        else
            log_info "Attempt ${attempt}/${max_attempts}: Health check returned HTTP ${http_code}. Retrying in 2s..."
        fi

        sleep 2
        attempt=$(( attempt + 1 ))
    done

    if [ "$is_healthy" -eq 1 ]; then
        return 0
    fi

    log_error "Health verification timed out after ${HEALTH_TIMEOUT} seconds: Service did not reach healthy state."
    return 1
}

# ------------------------------------------------------------------------------
# State 7: COMMITTED (Success Path)
# ------------------------------------------------------------------------------

run_commit() {
    CURRENT_STATE="COMMITTED"
    set_state "COMMITTED" "COMMIT" 7 "SUCCESS" "Update successfully committed and verified healthy"

    log_info "=============================================================================="
    log_info " Auto-Update Completed Successfully."
    log_info " Previous Commit : ${OLD_COMMIT}"
    log_info " New Commit      : ${NEW_COMMIT:-$OLD_COMMIT}"
    log_info " Snapshot Archive: ${SNAPSHOT_FILE}"
    log_info " State File      : ${UPDATE_STATE_FILE}"
    log_info "=============================================================================="
    return 0
}

# ------------------------------------------------------------------------------
# State 8: ROLLBACK_EXECUTE (Failure & Recovery Path)
# ------------------------------------------------------------------------------

run_rollback() {
    local failure_reason="$1"

    CURRENT_STATE="ROLLBACK_EXECUTE"
    log_error "CRITICAL: Triggering automatic rollback to previous state (${OLD_COMMIT})."
    log_error "Reason: ${failure_reason}"

    # 1. Log alert to persistent audit log
    log_audit_failure "$failure_reason"

    # 2. Update state to ROLLBACK
    set_state "ROLLBACK_EXECUTE" "ROLLBACK" 7 "ROLLBACK" "Update failed (${failure_reason}). Rollback in progress."

    # 3. Perform atomic restoration
    if [ "$DRY_RUN" -eq 1 ] || [ "$SIMULATE_SUCCESS" -eq 1 ] || [ "$SIMULATE_FAILURE" -eq 1 ]; then
        log_info "[DRY-RUN] Restoring git commit: git reset --hard ${OLD_COMMIT}"
        log_info "[DRY-RUN] Restoring stashed edits: git stash pop"
    else
        local git_restored=0
        if [ "$OLD_COMMIT" != "initial" ] && git -C "$APP_DIR" rev-parse "$OLD_COMMIT" >/dev/null 2>&1; then
            log_info "Resetting git tree to previous commit: ${OLD_COMMIT}..."
            if git -C "$APP_DIR" reset --hard "$OLD_COMMIT"; then
                git -C "$APP_DIR" stash pop 2>/dev/null || true
                git_restored=1
            fi
        fi

        # Fallback to tarball unpack if git reset was not possible
        if [ "$git_restored" -eq 0 ] && [ -n "$SNAPSHOT_FILE" ] && [ -f "$SNAPSHOT_FILE" ]; then
            log_info "Unpacking snapshot archive to restore system files: ${SNAPSHOT_FILE}..."
            tar --force-local -xzf "$SNAPSHOT_FILE" -C "$APP_DIR"
        fi

        # Ensure .env is restored upon rollback
        local persistent_env="${STORAGE_DIR}/rollback_recovery/.env.persist"
        if [ ! -f "${APP_DIR}/.env" ] && [ -f "$persistent_env" ]; then
            log_info "Restoring .env from persistent SD backup during rollback..."
            cp -f "$persistent_env" "${APP_DIR}/.env"
            chmod 600 "${APP_DIR}/.env" 2>/dev/null || true
        fi

        # 4. Restart service to recover previous operational state
        if command -v systemctl >/dev/null 2>&1 && [ -f "/etc/systemd/system/autolabel.service" ]; then
            log_info "Restarting autolabel.service after rollback..."
            systemctl restart autolabel.service 2>/dev/null || true
        fi
    fi

    # 5. Write rollback recovery manifest
    write_rollback_state "${OLD_COMMIT}" "ROLLBACK_READY" "${failure_reason}"

    # 6. Final state update
    set_state "ROLLBACK_EXECUTE" "ROLLBACK" 7 "ROLLBACK" "Rollback executed. Service recovered to commit ${OLD_COMMIT}."

    log_warn "Rollback completed. System restored to commit ${OLD_COMMIT}."
    exit 1
}

# ------------------------------------------------------------------------------
# Main 8-State Orchestration Workflow
# ------------------------------------------------------------------------------

main() {
    parse_arguments "$@"
    init_environment

    echo "=============================================================================="
    echo " Autonomous Label Orchestrator - Resilient Auto-Update Engine"
    echo " Target Application Directory : ${APP_DIR}"
    echo " Backup & Snapshot Storage    : ${STORAGE_DIR}"
    echo " Mode                         : $([ "$CHECK_ONLY" -eq 1 ] && echo "CHECK ONLY" || ([ "$DRY_RUN" -eq 1 ] && echo "DRY RUN" || echo "LIVE UPDATE"))"
    echo "=============================================================================="

    # State 0: IDLE
    CURRENT_STATE="IDLE"
    set_state "IDLE" "IDLE" 0 "IDLE" "Auto-update engine initialized and ready"

    # State 1: PRE_FLIGHT_CHECK
    if ! run_preflight_checks; then
        if [ "$CHECK_ONLY" -eq 1 ]; then
            log_error "Pre-flight checks failed."
            exit 2
        fi
        run_rollback "Pre-flight verification failed"
    fi

    if [ "$CHECK_ONLY" -eq 1 ]; then
        log_info "Pre-flight checks passed successfully. System is update-ready."
        set_state "IDLE" "IDLE" 0 "IDLE" "Pre-flight checks verified system readiness"
        exit 0
    fi

    # State 2: SNAPSHOT_CREATION
    if ! run_snapshot_creation; then
        run_rollback "Pre-update snapshot staging failed"
    fi

    # State 3: FETCH_AND_RESET
    if ! run_fetch_and_reset; then
        run_rollback "Remote git fetch and reset failed"
    fi

    # State 4: DEPENDENCY_VALID
    if ! run_dependency_check; then
        run_rollback "Dependency validation and installation failed"
    fi

    # State 5: SERVICE_RESTART
    if ! run_service_restart; then
        run_rollback "Service restart invocation failed"
    fi

    # State 6: HEALTH_VALIDATION
    if ! run_health_validation; then
        run_rollback "Health check validation failed or timed out"
    fi

    # State 7: COMMITTED
    run_commit
}

main "$@"
