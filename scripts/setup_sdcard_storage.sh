#!/usr/bin/env bash
# ==============================================================================
# Setup 64GB MicroSD Storage on Orange Pi PC Plus (Armbian Linux)
# ==============================================================================
# - Automatically detects attached 64GB MicroSD card (~58-64GB vs 8GB eMMC)
# - Explicitly excludes onboard 8GB eMMC hosting root filesystem
# - Creates primary partition (ext4) if not partitioned
# - Formats partition with label LABEL_BACKUP
# - Configures /etc/fstab UUID mount at /mnt/sdcard_backup with:
#     defaults,noatime,commit=60,errors=remount-ro,nofail 0 2
# - Creates required directory topology:
#     /mnt/sdcard_backup/snapshots
#     /mnt/sdcard_backup/audit_logs/scans
#     /mnt/sdcard_backup/audit_logs/prints
#     /mnt/sdcard_backup/templates_archive/golden_master
#     /mnt/sdcard_backup/templates_archive/custom_user_templates
#     /mnt/sdcard_backup/rollback_recovery
# - Supports --check / --dry-run for non-destructive verification
# ==============================================================================

set -euo pipefail

MOUNT_POINT="/mnt/sdcard_backup"
TARGET_LABEL="LABEL_BACKUP"
MIN_BYTES=50000000000   # ~50 GB minimum for 64GB MicroSD card
MAX_BYTES=70000000000   # ~70 GB maximum for 64GB MicroSD card

DRY_RUN=0

print_usage() {
    cat << EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --check, --dry-run, -n   Non-destructive check and validation mode (no disk changes)
  --help, -h               Show this help message and exit

Description:
  Identifies the 64GB MicroSD card attached to the Orange Pi PC Plus,
  creates an ext4 partition with label LABEL_BACKUP, registers the mount
  in /etc/fstab with resilient mount options, mounts to /mnt/sdcard_backup,
  and establishes the required directory hierarchy.
EOF
}

# Parse command line flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --check|--dry-run|-n)
            DRY_RUN=1
            shift
            ;;
        --help|-h)
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

echo "=============================================================================="
echo " Auto-Label Orchestrator - 64GB MicroSD Storage Setup"
echo " Target Mount: ${MOUNT_POINT}"
echo " Mode: $([ "$DRY_RUN" -eq 1 ] && echo "DRY-RUN / CHECK MODE" || echo "EXECUTION MODE")"
echo "=============================================================================="

# Root privilege validation (only required for real execution)
if [ "$DRY_RUN" -eq 0 ] && [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "[ERROR] This script must be executed with root privileges (sudo)." >&2
    exit 1
fi

# Detect root disk to ensure eMMC/root is never touched
ROOT_DEV=""
if command -v findmnt >/dev/null 2>&1; then
    ROOT_DEV=$(findmnt -n -o SOURCE / 2>/dev/null || true)
fi
if [ -z "$ROOT_DEV" ] && command -v df >/dev/null 2>&1; then
    ROOT_DEV=$(df / 2>/dev/null | tail -n 1 | awk '{print $1}')
fi

ROOT_DISK=""
if [ -n "$ROOT_DEV" ]; then
    # Strip /dev/ and partition suffix
    DEV_NAME=$(basename "$ROOT_DEV")
    if [[ "$DEV_NAME" =~ ^mmcblk[0-9]+ ]]; then
        ROOT_DISK="${BASH_REMATCH[0]}"
    elif [[ "$DEV_NAME" =~ ^(sd[a-z]|nvme[0-9]+n[0-9]+|vd[a-z]) ]]; then
        ROOT_DISK="${BASH_REMATCH[0]}"
    fi
fi

if [ -n "$ROOT_DISK" ]; then
    echo "[INFO] Root device is on: ${ROOT_DISK} (Strictly excluded from targeting)"
else
    echo "[INFO] Could not determine root block device. Proceeding with size checks."
fi

# Detect candidate 64GB MicroSD card
CANDIDATE_DISK=""
CANDIDATE_PART=""
CANDIDATE_SIZE=0

if command -v lsblk >/dev/null 2>&1; then
    # Read block devices in raw format
    while read -r name size type mount; do
        [ "$type" != "disk" ] && continue
        [ "$name" = "$ROOT_DISK" ] && continue

        # Check size range for ~64GB card (50GB to 70GB)
        if [ "$size" -ge "$MIN_BYTES" ] && [ "$size" -le "$MAX_BYTES" ]; then
            CANDIDATE_DISK="$name"
            CANDIDATE_SIZE="$size"
            break
        fi
    done < <(lsblk -b -n -o NAME,SIZE,TYPE,MOUNTPOINT 2>/dev/null || true)
fi

# Fallback: check /sys/block if lsblk was not conclusive
if [ -z "$CANDIDATE_DISK" ] && [ -d /sys/block ]; then
    for b in /sys/block/*; do
        [ -e "$b" ] || continue
        name=$(basename "$b")
        [ "$name" = "$ROOT_DISK" ] && continue
        [[ "$name" =~ ^(loop|ram|zram) ]] && continue

        if [ -f "$b/size" ]; then
            sectors=$(cat "$b/size" 2>/dev/null || echo 0)
            bytes=$(( sectors * 512 ))
            if [ "$bytes" -ge "$MIN_BYTES" ] && [ "$bytes" -le "$MAX_BYTES" ]; then
                CANDIDATE_DISK="$name"
                CANDIDATE_SIZE="$bytes"
                break
            fi
        fi
    done
fi

if [ -z "$CANDIDATE_DISK" ]; then
    # Check if /mnt/sdcard_backup is already mounted or configured in fstab
    ALREADY_MOUNTED=0
    if command -v mountpoint >/dev/null 2>&1 && mountpoint -q "$MOUNT_POINT" 2>/dev/null; then
        ALREADY_MOUNTED=1
    elif grep -q "$MOUNT_POINT" /proc/mounts 2>/dev/null; then
        ALREADY_MOUNTED=1
    fi

    if [ "$ALREADY_MOUNTED" -eq 1 ]; then
        echo "[INFO] Target ${MOUNT_POINT} is already mounted."
    else
        echo "[NOTICE] No physical 64GB MicroSD card detected in range ${MIN_BYTES}-${MAX_BYTES} bytes."
        if [ "$DRY_RUN" -eq 0 ]; then
            echo "[ERROR] Cannot proceed without a valid 64GB MicroSD card attached." >&2
            exit 1
        else
            echo "[INFO] Host dry-run: skipping partition/format checks since no target device was found."
        fi
    fi
else
    SIZE_GB=$(( CANDIDATE_SIZE / 1000000000 ))
    echo "[INFO] Detected MicroSD card candidate: /dev/${CANDIDATE_DISK} (${SIZE_GB} GB)"
fi

# Determine partition name
if [ -n "$CANDIDATE_DISK" ]; then
    if [[ "$CANDIDATE_DISK" =~ [0-9]$ ]]; then
        CANDIDATE_PART="${CANDIDATE_DISK}p1"
    else
        CANDIDATE_PART="${CANDIDATE_DISK}1"
    fi
    PART_PATH="/dev/${CANDIDATE_PART}"
    DISK_PATH="/dev/${CANDIDATE_DISK}"

    # Partitioning step
    PART_EXISTS=0
    if [ -b "$PART_PATH" ]; then
        PART_EXISTS=1
    elif command -v lsblk >/dev/null 2>&1 && lsblk -n -o NAME "$DISK_PATH" 2>/dev/null | grep -q "${CANDIDATE_PART}"; then
        PART_EXISTS=1
    fi

    if [ "$PART_EXISTS" -eq 0 ]; then
        echo "[ACTION] Partitioning ${DISK_PATH} with single primary ext4 partition..."
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[DRY-RUN] Would run: parted -s ${DISK_PATH} mklabel msdos mkpart primary ext4 1MiB 100%"
        else
            parted -s "${DISK_PATH}" mklabel msdos mkpart primary ext4 1MiB 100%
            command -v partprobe >/dev/null 2>&1 && partprobe "${DISK_PATH}" || true
            command -v udevadm >/dev/null 2>&1 && udevadm settle || sleep 1
        fi
    else
        echo "[INFO] Partition ${PART_PATH} already exists."
    fi

    # Formatting step
    FORMAT_NEEDED=1
    EXISTING_LABEL=""
    EXISTING_FSTYPE=""
    if command -v blkid >/dev/null 2>&1 && [ -b "$PART_PATH" ]; then
        EXISTING_LABEL=$(blkid -s LABEL -o value "$PART_PATH" 2>/dev/null || true)
        EXISTING_FSTYPE=$(blkid -s TYPE -o value "$PART_PATH" 2>/dev/null || true)
    fi

    if [ "$EXISTING_LABEL" = "$TARGET_LABEL" ] && [ "$EXISTING_FSTYPE" = "ext4" ]; then
        echo "[INFO] ${PART_PATH} is already formatted with ext4 and labeled ${TARGET_LABEL}."
        FORMAT_NEEDED=0
    fi

    if [ "$FORMAT_NEEDED" -eq 1 ]; then
        echo "[ACTION] Formatting ${PART_PATH} as ext4 (Label: ${TARGET_LABEL})..."
        if [ "$DRY_RUN" -eq 1 ]; then
            echo "[DRY-RUN] Would run: mkfs.ext4 -F -L ${TARGET_LABEL} ${PART_PATH}"
        else
            mkfs.ext4 -F -L "${TARGET_LABEL}" "${PART_PATH}"
        fi
    fi

    # Retrieve UUID
    PART_UUID=""
    if command -v blkid >/dev/null 2>&1 && [ -b "$PART_PATH" ]; then
        PART_UUID=$(blkid -s UUID -o value "$PART_PATH" 2>/dev/null || true)
    fi

    # /etc/fstab entry
    FSTAB_LINE=""
    if [ -n "$PART_UUID" ]; then
        FSTAB_LINE="UUID=${PART_UUID} ${MOUNT_POINT} ext4 defaults,noatime,commit=60,errors=remount-ro,nofail 0 2"
    else
        FSTAB_LINE="LABEL=${TARGET_LABEL} ${MOUNT_POINT} ext4 defaults,noatime,commit=60,errors=remount-ro,nofail 0 2"
    fi

    echo "[INFO] Target /etc/fstab configuration:"
    echo "       ${FSTAB_LINE}"

    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[DRY-RUN] Would verify/add mount entry to /etc/fstab"
    else
        if [ -f /etc/fstab ]; then
            if grep -q "${MOUNT_POINT}" /etc/fstab; then
                echo "[INFO] ${MOUNT_POINT} entry already present in /etc/fstab."
            else
                echo "[ACTION] Appending persistent mount entry to /etc/fstab..."
                echo -e "\n# 64GB MicroSD backup and persistence storage\n${FSTAB_LINE}" >> /etc/fstab
            fi
        fi
    fi

    # Mount execution
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[DRY-RUN] Would ensure ${MOUNT_POINT} exists and mount partition"
    else
        mkdir -p "${MOUNT_POINT}"
        if ! mountpoint -q "${MOUNT_POINT}" 2>/dev/null; then
            echo "[ACTION] Mounting ${MOUNT_POINT}..."
            mount "${MOUNT_POINT}" || mount "${PART_PATH}" "${MOUNT_POINT}"
        else
            echo "[INFO] ${MOUNT_POINT} is currently mounted."
        fi
    fi
fi

# Directory hierarchy creation
REQUIRED_SUBDIRS=(
    "snapshots"
    "audit_logs"
    "audit_logs/scans"
    "audit_logs/prints"
    "templates_archive"
    "templates_archive/golden_master"
    "templates_archive/custom_user_templates"
    "rollback_recovery"
)

echo "[INFO] Ensuring backup directory hierarchy..."
for sub in "${REQUIRED_SUBDIRS[@]}"; do
    target_dir="${MOUNT_POINT}/${sub}"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "[DRY-RUN] Would create directory: ${target_dir} (mode 0755)"
    else
        mkdir -p "${target_dir}"
        chmod 755 "${target_dir}"
    fi
done

if [ "$DRY_RUN" -eq 0 ]; then
    chmod 755 "${MOUNT_POINT}" 2>/dev/null || true
fi

echo "=============================================================================="
echo " 64GB MicroSD Storage Setup Completed Successfully."
echo " Mount Location : ${MOUNT_POINT}"
echo " File System    : ext4 (LABEL_BACKUP)"
echo " Options        : defaults,noatime,commit=60,errors=remount-ro,nofail"
echo " Topology Ready : snapshots, audit_logs, templates_archive, rollback_recovery"
echo "=============================================================================="
exit 0
