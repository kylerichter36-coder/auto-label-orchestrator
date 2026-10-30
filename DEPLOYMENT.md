# Autonomous Label Orchestrator - Production Deployment Guide
Target Hardware: Orange Pi PC Plus (Allwinner H3, 1GB DDR3 RAM, 8GB eMMC + 64GB MicroSD)
Target Operating System: Armbian Linux (Debian 13 trixie / minimal headless CLI)
Target Path: /opt/label_system/
Service Target: autolabel.service

==============================================================================
1. HARDWARE ARCHITECTURE & PHYSICAL SPECIFICATIONS
==============================================================================

The Autonomous Label Orchestrator is engineered for continuous production
deployment on embedded Single Board Computers (SBCs) operating in industrial,
manufacturing, and warehouse environments.

Host Single Board Computer:
- Board: Orange Pi PC Plus
- SoC: Allwinner H3 Quad-Core ARM Cortex-A7 @ 1.2 GHz (32-bit ARMv7-A)
- System Memory: 1GB DDR3 SDRAM (shared with GPU)
- Onboard Storage: 8GB eMMC 5.0 (hosts Armbian OS and application binaries)
- External Storage: 64GB MicroSD Class 10 / UHS-I (dedicated /mnt/sdcard_backup)
- Network Interface 1: 10/100M Onboard RJ45 Ethernet (dedicated to scanner)
- Network Interface 2: Realtek RTL8189ETV Onboard 802.11 b/g/n Wi-Fi (WAN uplink)
- USB Host: 3x USB 2.0 Host Type-A ports, 1x USB 2.0 OTG port
- Power Supply: 5V / 2.5A DC via 4.0mm x 1.7mm barrel jack (dedicated power rail)

Peripheral Hardware:
1. Label Printer: Argox OS-214 Thermal Transfer Printer
   - Resolution: 203.2 DPI (8 dots/mm)
   - Emulation Mode: Native PPLB (Eltron EPL2 compatible) or PPLA (Datamax DPL)
   - Interface: Direct USB Type-B to Type-A cable (/dev/usb/lp0 character device)
   - Printable Width: Up to 104 mm (4.1 inches)
   - Media: Continuous thermal transfer rolls, black, cyan, magenta, yellow stock

2. Document Scanner: Lexmark MX511 Multifunction Network Scanner
   - Feeder: 50-sheet Automatic Document Feeder (ADF), Duplex scanning
   - Interface: Dedicated Cat5e/Cat6 patch cable directly to Orange Pi Ethernet port
   - Influx Protocol: SMTP (Scan-to-Email) without TLS or authentication on port 2525
   - Native Scan Resolution: 300 DPI Color PDF (24-bit RGB)

==============================================================================
2. PHYSICAL CABLING & NETWORK TOPOLOGY
==============================================================================

The production installation utilizes an isolated cell topology where scanner
data never touches the local workshop Wi-Fi network:

  +--------------------------------------------------------------------------+
  |                   AIR-GAPPED SCAN & PRINT WORKCELL                       |
  |                                                                          |
  |  +--------------------+               +-------------------------------+  |
  |  | Lexmark MX511      |  Direct Cat5e | Orange Pi PC Plus (Host)      |  |
  |  | Network Scanner    | ------------> | eth0: 192.168.50.1/24         |  |
  |  | IP: 192.168.50.2   |  Patch Cable  | dnsmasq DHCP server (:67)     |  |
  |  | SMTP Gateway: .1   |               | local aiosmtpd daemon (:2525) |  |
  |  +--------------------+               +---------------+---------------+  |
  |                                                       |                  |
  |                                        Direct USB     |                  |
  |                                        Cable (usblp)  v                  |
  |                                       +-------------------------------+  |
  |                                       | Argox OS-214 Thermal Printer  |  |
  |                                       | Device Node: /dev/usb/lp0     |  |
  |                                       | Emulation: PPLB (203.2 DPI)   |  |
  |                                       +-------------------------------+  |
  +-------------------------------------------------------|------------------+
                                                          |
                       +----------------------------------+
                       | wlan0 (Workshop Wi-Fi / DHCP)
                       v
         +----------------------------+
         | Internet Gateway / Router  |
         +-------------+--------------+
                       |
                       v
         +----------------------------+
         | Google Gemini Vision API   |
         | (10-Key Failover Pool)     |
         +----------------------------+

Wiring Instructions:
1. Connect Orange Pi 5V/2.5A DC power supply to a stable wall outlet (UPS recommended).
2. Connect standard Cat5e/Cat6 network patch cable between the Lexmark MX511
   RJ45 port and the Orange Pi onboard RJ45 port. (Orange Pi PHY supports Auto-MDIX).
3. Connect standard USB Type-A to Type-B cable from any Orange Pi USB 2.0 port
   to the Argox OS-214 rear USB interface.
4. Insert 64GB MicroSD card into the Orange Pi external TF card slot.
5. Connect Orange Pi Wi-Fi (wlan0) to the workshop wireless network via nmtui.

==============================================================================
3. DEDICATED 64GB MICROSD STORAGE SETUP
==============================================================================

The system uses an external 64GB MicroSD card mounted at /mnt/sdcard_backup
for non-volatile persistent storage of active print queues, SQLite audit
databases, raw transaction logs, and snapshot archives. This prevents flash
wear and exhaustion on the onboard 8GB eMMC.

Script: scripts/setup_sdcard_storage.sh

Features & Safety Guardrails:
- Automatically detects block devices sized between 50 GB and 70 GB.
- Explicitly detects and excludes the root block device hosting / (eMMC mmcblk0).
- Creates a single primary ext4 partition across 100% of the card.
- Formats the filesystem with volume label LABEL_BACKUP.
- Configures /etc/fstab using filesystem UUID with resilient mount options:
    defaults,noatime,commit=60,errors=remount-ro,nofail 0 2
  - noatime: Eliminates unnecessary flash write operations on file reads.
  - commit=60: Batches filesystem metadata flush cycles every 60 seconds.
  - errors=remount-ro: Prevents disk corruption during unexpected power cuts.
  - nofail: Allows the OS to boot normally even if the card is detached.
- Establishes directory hierarchy with permissions 0755:
    /mnt/sdcard_backup/snapshots
    /mnt/sdcard_backup/audit_logs/scans
    /mnt/sdcard_backup/audit_logs/prints
    /mnt/sdcard_backup/templates_archive/golden_master
    /mnt/sdcard_backup/templates_archive/custom_user_templates
    /mnt/sdcard_backup/rollback_recovery

Execution Steps on Orange Pi:
1. Verify attached storage devices:
   lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,UUID

2. Run non-destructive check:
   sudo ./scripts/setup_sdcard_storage.sh --check

3. Execute storage initialization:
   sudo ./scripts/setup_sdcard_storage.sh

4. Verify mount and write access:
   df -h /mnt/sdcard_backup
   touch /mnt/sdcard_backup/.probe && rm -f /mnt/sdcard_backup/.probe

==============================================================================
4. DIRECT ETHERNET & SCANNER INTERCONNECT
==============================================================================

The Lexmark MX511 scanner communicates directly with the Orange Pi over an
isolated subnet. The Orange Pi acts as a lightweight DHCP server on eth0
while Wi-Fi (wlan0) remains the default gateway for external cloud traffic.

Script: scripts/setup_direct_ethernet.sh

Configuration Details:
- Static IP on eth0: 192.168.50.1 / 255.255.255.0
  Created at: /etc/network/interfaces.d/eth0-direct
- dnsmasq DHCP daemon configuration:
  Created at: /etc/dnsmasq.d/lexmark-direct.conf
  - interface=eth0
  - bind-interfaces
  - dhcp-range=192.168.50.2,192.168.50.10,255.255.255.0,24h
  - dhcp-option=3 (Suppresses default gateway announcement so wlan0 stays primary)
  - dhcp-option=6 (Suppresses DNS server announcement over eth0)

Execution Steps on Orange Pi:
1. Run direct Ethernet setup script:
   sudo ./scripts/setup_direct_ethernet.sh

2. Verify dnsmasq service status:
   systemctl status dnsmasq

3. Configure Lexmark MX511 via embedded web interface:
   - Connect workstation to workshop Wi-Fi or directly to scanner web UI.
   - Open browser to http://192.168.50.2 (assigned by dnsmasq).
   - Navigate to: Settings -> Network/Ports -> TCP/IP
     * Set DHCP: Enabled (Automatic)
   - Navigate to: Settings -> E-mail / FTP -> E-mail Setup
     * Primary SMTP Gateway: 192.168.50.1
     * SMTP Port: 2525
     * Authentication: None / Anonymous
     * SSL/TLS: Disabled
     * Device E-mail Address: scanner@local
   - Navigate to: Shortcuts -> Add Shortcut
     * Shortcut Number: 1
     * Shortcut Name: Label Scanner
     * Destination: quotes@orangepi.local
     * Format: PDF
     * Resolution: 300 DPI
     * Color: Color (24-bit)

==============================================================================
5. ENVIRONMENT SECRET CONFIGURATION & ISOLATION
==============================================================================

All runtime configurations, port bindings, hardware device nodes, and Google
Gemini API keys are loaded via environment variables from /opt/label_system/.env.

Security Rules:
- The active .env file is strictly ignored by Git and MUST NEVER be committed.
- Production permissions must be restricted to root-only read/write (chmod 600).
- The web UI masks all API keys, exposing only prefix/suffix for telemetry.

Configuration Template (.env.template):
------------------------------------------------------------------------------
# Gemini Vision API Key Pool (10 keys for automated 429 failover & rotation)
GEMINI_API_KEYS=AIzaSyA_KEY01,AIzaSyB_KEY02,AIzaSyC_KEY03,AIzaSyD_KEY04,AIzaSyE_KEY05,AIzaSyF_KEY06,AIzaSyG_KEY07,AIzaSyH_KEY08,AIzaSyI_KEY09,AIzaSyJ_KEY10

# Gemini Vision Model Configuration
GEMINI_MODEL=gemini-3.8-flash
MAX_OUTPUT_TOKENS=1024
TEMPERATURE=0.0

# Network Scanner Influx (Lexmark MX511 Direct Ethernet SMTP Tether)
SMTP_HOST=0.0.0.0
SMTP_PORT=2525
DIRECT_ETH_IP=192.168.50.1
SCANNER_IP=192.168.50.2

# Enterprise Industrial Web Dashboard
WEB_HOST=0.0.0.0
WEB_PORT=8080

# Physical Thermal Transfer Label Printer (Argox OS-214)
PRINTER_DEVICE=/dev/usb/lp0
SIMULATION_MODE=false

# Template and Label Asset Directories
LABEL_DIR=/opt/label_system/labels
TEMPLATE_DIR=/opt/label_system/templates

# Dedicated 64GB MicroSD Non-Volatile Backup & Audit Storage
SDCARD_MOUNT=/mnt/sdcard_backup
FALLBACK_STORAGE_DIR=/opt/label_system/backup_storage
AUDIT_LOG_DIR=/mnt/sdcard_backup/audit_logs
------------------------------------------------------------------------------

Setup Steps on Orange Pi:
1. Copy template to active configuration:
   cp /opt/label_system/.env.template /opt/label_system/.env

2. Edit .env to populate the 10 real Google Gemini API keys:
   nano /opt/label_system/.env

3. Restrict file permissions:
   chmod 600 /opt/label_system/.env
   chown root:root /opt/label_system/.env

==============================================================================
6. OUT-OF-BAND DEPLOYMENT SYNC WORKFLOW
==============================================================================

To deploy code updates from a development machine to the Orange Pi without
committing secrets or modifying production configuration, use the out-of-band
synchronization script.

Script: scripts/sync_to_orangepi.sh

Features:
- Connects via SSH/rsync or fallback tar-stream pipe over SSH.
- Strictly excludes .env, git metadata (.git/), Python cache (__pycache__),
  temporary files, logs, and local SQLite databases.
- Ensures executable permissions (chmod +x) on all remote shell scripts.
- Optional flag to trigger automatic systemctl reload and restart.

CLI Parameters:
  -h, --host <IP>         Target Orange Pi IP address (default: 192.168.3.50)
  -u, --user <USER>       Remote SSH user (default: root)
  -p, --port <PORT>       Remote SSH port (default: 22)
  -t, --target <PATH>     Target directory on Orange Pi (default: /opt/label_system/)
  -i, --identity <KEY>    Path to SSH private key file
  -n, --dry-run           Preview sync actions without transferring files
  -r, --restart-service   Automatically restart autolabel.service post-sync

Usage Examples:
1. Dry-run verification from workstation:
   ./scripts/sync_to_orangepi.sh --host 192.168.3.50 --dry-run

2. Live synchronization with service restart:
   ./scripts/sync_to_orangepi.sh --host 192.168.3.50 --restart-service

3. Sync using specific SSH identity key:
   ./scripts/sync_to_orangepi.sh --host 192.168.3.50 -i ~/.ssh/id_ed25519 -r

==============================================================================
7. SYSTEMD SERVICE INSTALLATION & SANDBOXING
==============================================================================

The application runs as a managed background systemd daemon (autolabel.service),
starting automatically on boot with resource constraints and sandboxing.

Unit Configuration: systemd/autolabel.service
------------------------------------------------------------------------------
[Unit]
Description=Auto-Label Orchestrator (Lexmark Scanner to Argox Thermal Printer)
After=network.target network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/label_system
EnvironmentFile=/opt/label_system/.env
ExecStart=/usr/bin/python3 -m daemon.main
Restart=always
RestartSec=5s
KillMode=process

# Security and resource sandboxing for 1GB RAM Orange Pi
MemoryMax=350M
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/opt/label_system /mnt/sdcard_backup

[Install]
WantedBy=multi-user.target
------------------------------------------------------------------------------

Hardening & Sandboxing Properties:
- MemoryMax=350M: Enforces a strict memory ceiling, preventing out-of-memory (OOM)
  starvation of the Armbian OS on the 1GB RAM SBC.
- PrivateTmp=true: Isolates /tmp from other system processes.
- ProtectSystem=full: Mounts /usr, /boot, and /etc read-only for the service.
- ReadWritePaths: Whitelists only /opt/label_system and /mnt/sdcard_backup for writes.
- Restart=always, RestartSec=5s: Ensures self-healing daemon recovery on unhandled exceptions.

Installation via Script:
   sudo ./scripts/install_service.sh

Manual Service Management Commands:
- Enable service on boot:
  sudo systemctl enable autolabel.service
- Start service:
  sudo systemctl start autolabel.service
- Restart service:
  sudo systemctl restart autolabel.service
- Inspect service status:
  sudo systemctl status autolabel.service
- Follow real-time operational logs:
  journalctl -u autolabel.service -f -n 100

==============================================================================
8. RESILIENT AUTO-UPDATE WITH ROLLBACK SAFEGUARDS
==============================================================================

The system includes an automated 8-state update engine designed to safely pull
Git updates on the Orange Pi, verify health, and automatically roll back if
any step or service health probe fails.

Script: scripts/update_with_rollback.sh

8-State Update Lifecycle:
1. IDLE: Update engine initialized; sets up state tracking files.
2. PRE_FLIGHT_CHECK: Validates >=500MB free disk space, internet connectivity
   to GitHub, and git work tree integrity. Verifies/restores .env persistence.
3. SNAPSHOT_CREATION: Compresses current application state (daemon, web,
   templates, scripts) into /mnt/sdcard_backup/snapshots/snapshot_<commit>_<ts>.tar.gz.
   Prunes older snapshots, retaining the 5 newest archives.
4. FETCH_AND_RESET: Stashes local modifications, fetches remote origin/main,
   and executes atomic git reset --hard origin/main. Restores .env file.
5. DEPENDENCY_VALID: Inspects requirements.txt diff; executes pip install
   only if dependencies were added or changed.
6. SERVICE_RESTART: Triggers systemctl restart autolabel.service.
7. HEALTH_VALIDATION: Probes http://127.0.0.1:8080/api/status every 2s for up
   to 30 seconds. Confirms HTTP 200 response with valid JSON payload containing
   queues, hardware, and gemini_pool.
8. COMMITTED / ROLLBACK_EXECUTE:
   - On Success: Marks state as COMMITTED in update_state.json.
   - On Failure: Immediately triggers ROLLBACK_EXECUTE:
     * Hard resets git work tree to pre-update commit hash.
     * Unpacks previous snapshot tarball if git reset fails.
     * Restores .env configuration and permissions (chmod 600).
     * Restarts autolabel.service to restore operational state.
     * Writes recovery manifest to /mnt/sdcard_backup/rollback_recovery/rollback_state.json.
     * Records failure details to /mnt/sdcard_backup/audit_logs/update_failures.log.

CLI Options:
  --check                   Run pre-flight checks only and exit (non-destructive)
  --dry-run, -n             Simulate full update cycle without modifying repo/services
  --simulate-success        Simulate successful update without live network/systemd
  --simulate-failure        Simulate health check failure to trigger automatic rollback
  --simulate-fetch-failure  Simulate network drop during git fetch
  --simulate-dep-failure    Simulate dependency installation failure

Manual Update Execution on Orange Pi:
- Check pre-flight prerequisites:
  sudo /opt/label_system/scripts/update_with_rollback.sh --check

- Execute live update:
  sudo /opt/label_system/scripts/update_with_rollback.sh

- Test rollback mechanism (validation drill):
  sudo /opt/label_system/scripts/update_with_rollback.sh --simulate-failure

==============================================================================
9. RECOVERY PROCEDURES & DISASTER RECOVERY
==============================================================================

Scenario A: Power Cut During Operation
1. The filesystem mount options (commit=60, errors=remount-ro) safeguard ext4
   integrity on the SD card.
2. Systemd automatically restarts autolabel.service upon power restoration.
3. ColorQueueManager reconstructs active queues and audit logs from:
   /mnt/sdcard_backup/audit_logs/scans/
   /mnt/sdcard_backup/active_queues.json

Scenario B: Accidental .env Loss or Deletion
1. Persistent backup is maintained at:
   /mnt/sdcard_backup/rollback_recovery/.env.persist
2. Run pre-flight check or restore manually:
   cp /mnt/sdcard_backup/rollback_recovery/.env.persist /opt/label_system/.env
   chmod 600 /opt/label_system/.env
   systemctl restart autolabel.service

Scenario C: Manual Rollback to Earlier Snapshot
1. List available snapshots on SD card:
   ls -la /mnt/sdcard_backup/snapshots/

2. Unpack selected snapshot archive over application root:
   tar -xzf /mnt/sdcard_backup/snapshots/snapshot_<commit>_<ts>.tar.gz -C /opt/label_system/

3. Restart daemon:
   systemctl restart autolabel.service

Scenario D: Complete SD Card Replacement
1. Insert fresh 64GB MicroSD card.
2. Run setup script:
   sudo /opt/label_system/scripts/setup_sdcard_storage.sh
3. Restart daemon to initialize databases and queues:
   sudo systemctl restart autolabel.service

==============================================================================
10. PRODUCTION VERIFICATION CHECKLIST
==============================================================================

Execute this sequential verification before releasing the station to warehouse staff:

[ ] 1. Carrier Status:
       cat /sys/class/net/eth0/carrier (or end0) returns "1" when scanner is plugged in.
[ ] 2. DHCP Lease:
       dnsmasq lease file shows 192.168.50.2 assigned to Lexmark MAC address.
[ ] 3. Printer Device Node:
       ls -l /dev/usb/lp0 exists and is accessible.
[ ] 4. SD Card Mount:
       mountpoint -q /mnt/sdcard_backup returns 0.
[ ] 5. Gemini Pool:
       Web UI setup tab shows 10/10 keys active with 0 exhausted.
[ ] 6. End-to-End Test Suite:
       python tests/e2e_runner.py returns 60/60 PASS (100.0%).
[ ] 7. Multi-Cycle Pipeline Suite:
       python -m unittest tests/test_multicycle_pipeline.py returns 6/6 PASS.
[ ] 8. Confidential Shredding:
       In-memory buffer shredding verified; 0 scanned PDFs retained on filesystem.
[ ] 9. BarTender Web Dashboard:
       Browser to http://<orangepi-ip>:8080 loads row-based print queue and PDF viewer.
[ ] 10. Cleanliness & Security:
       git status shows .env untracked; 0 emojis in logs or UI.
