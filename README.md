# Autonomous Label Orchestrator

> Universal Edge Multi-Workflow Thermal Label Printing Station  
> Automated ingestion, classification, conversion, preview, and continuous Argox PPLB thermal printing for industrial workshop workcells.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)]()
[![Architecture](https://img.shields.io/badge/Architecture-ARM32%20%7C%20ARM64%20%7C%20x86%20%7C%20x64-blueviolet.svg)]()
[![Milestones](https://img.shields.io/badge/Milestones%20M1--M5-100%25%20Complete-brightgreen.svg)]()
[![E2E Tests](https://img.shields.io/badge/E2E%20Tests-60%2F60%20Pass-brightgreen.svg)](TEST_READY.md)
[![Verification](https://img.shields.io/badge/Multi--Cycle-6%2F6%20Pass-brightgreen.svg)](TEST_READY.md)

---

## Documentation Links

- Production Deployment Manual: [DEPLOYMENT.md](DEPLOYMENT.md)
- Test Readiness Signal & Verification Scorecard: [TEST_READY.md](TEST_READY.md)
- Test Suite Infrastructure & Architecture: [TEST_INFRA.md](TEST_INFRA.md)
- Environment Configuration Schema: [.env.template](.env.template)

---

## Project Status: Production Ready (Milestones M1-M5 Complete)

The Autonomous Label Orchestrator has completed all engineering milestones
(M1 through M5 and M-TEST). The system has undergone full multi-cycle pipeline
verification, opaque-box end-to-end testing (60/60 passing tests), and secret
isolation auditing.

Milestone Summary:
- M-TEST: Opaque-Box E2E Testing Suite (Tiers 1-4, 60/60 passing tests).
- M1: BarTender Ingestion & Native 203.2 DPI PPLB/PPLA Conversion Engine.
- M2: Industrial Enterprise Web UI, In-Memory PDF Rasterizer & RAM Previews.
- M3: Hardware Integration, 64GB MicroSD Dedicated Persistence & Sandboxing.
- M4: Resilient 8-State GitHub Auto-Update Engine with Rollback Safeguards.
- M5: Multi-Cycle Pipeline Verification, Out-of-Band Sync & Secret Isolation.

---

## Executive Summary & System Capabilities

In high-volume manufacturing, refilling, and distribution facilities, workshop
operators receive paper orders (quotation sheets and pick-and-pack slips).
Manual re-entry of customer handwriting and order details into desktop BarTender
software creates operational bottlenecks, delays, and label waste.

The Autonomous Label Orchestrator completely automates this workflow directly
at the edge on an embedded host (Orange Pi PC Plus or any Linux machine):

1. Single-Touch Intake: Operator loads a batch of quotation or packing sheets
   into the document feeder of a network scanner and presses a single shortcut.
2. Air-Gapped Transmission: The scanner transmits the document over a direct,
   isolated Ethernet link to a local SMTP receiver on port 2525.
3. In-Memory Processing & Confidential Shredding: The raw PDF is held strictly
   in volatile RAM, rasterized into ephemeral page previews for web review,
   analyzed by vision models via a 10-key failover pool, and immediately
   shredded with zero-overwrite buffer wiping (0x00) and garbage collection.
4. BarTender & PPLB Conversion: Extracted data is mapped to BarTender template
   ASTs, scaled to 203.2 DPI (8 dots/mm), and compiled into native PPLB commands.
5. Continuous Direct Printing: Formatted PPLB print streams with dynamic batch
   quantity injection (e.g. P10, P30) are dispatched directly to the character
   device node /dev/usb/lp0 on the Argox OS-214 thermal printer without spoolers.
6. Non-Volatile Persistence: All scan transactions, active queue states, and
   audit logs persist across power cuts on a dedicated 64GB MicroSD partition.

---

## System Architecture & Hardware Topology

```
+--------------------------------------------------------------------------+
|                     ISOLATED INDUSTRIAL WORKCELL                         |
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
       | Local Workshop Network     |
       | (Port 8080 Enterprise UI)  |
       +-------------+--------------+
                     |
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
```

Component Specifications:
- Target SBC: Orange Pi PC Plus (Allwinner H3 Quad-Core, 1GB DDR3 RAM, 8GB eMMC).
- OS: Armbian Linux (Debian 13 trixie, headless minimal CLI).
- Dedicated Storage: 64GB MicroSD Class 10 mounted at /mnt/sdcard_backup.
- Document Feeder: Lexmark MX511 ADF network scanner on 192.168.50.2.
- Thermal Printer: Argox OS-214 (203.2 DPI, 8 dots/mm) on /dev/usb/lp0.
- Web Interface: Port 8080 (Industrial BarTender-style dark theme).

---

## Milestone Deliverables & Verification Inventory

| Milestone | Feature Scope | Verification Status | Artifacts |
|---|---|---|---|
| M-TEST | Opaque-Box E2E Testing Suite (Tiers 1-4) | 60/60 PASS (100.0%) | tests/test_opaque_e2e.py, tests/e2e_runner.py, TEST_READY.md |
| M1 | BarTender / PPLB / PPLA Conversion Engine | 100% Verified | daemon/bartender_converter.py, tests/test_bartender_converter.py |
| M2 | Enterprise Web Dashboard & RAM Previews | 100% Verified | daemon/pdf_preview.py, daemon/web_ui.py, web/index.html, web/setup.html |
| M3 | Hardware Integration & 64GB SD Persistence | 100% Verified | scripts/setup_sdcard_storage.sh, daemon/persistence.py, systemd/autolabel.service |
| M4 | Resilient GitHub Auto-Update with Rollback | 100% Verified | scripts/update_with_rollback.sh, tests/test_rollback.py |
| M5 | Multi-Cycle Pipeline Verification & Out-of-Band Sync | 6/6 Multi-Cycle PASS | tests/test_multicycle_pipeline.py, scripts/sync_to_orangepi.sh, .env.template |

---

## Detailed Milestone Engineering

### M1. BarTender & PPLB Conversion Engine
- Universal Ingestion: Parses BarTender export formats (.btw print-to-file,
  .prd, .prn, BTXML, CSV definitions) into a normalized Intermediate Label AST.
- 203.2 DPI Coordinate Scaler: Maps millimeter and inch dimensions to exact
  dot coordinates (8 dots/mm) with print direction and margin offsets.
- Native Emitters: Generates compliant PPLB (Eltron EPL2 emulation: N, q, Q,
  D, S, A, B, b, LO, X, P) and PPLA (Datamax DPL: STX L, 1211, 1e, E, Q) byte streams.
- Barcode Generators: Produces linear barcodes (Code 128, Code 39, EAN-13)
  and 2D matrices (QR Code, DataMatrix).
- Dynamic Quantity Injection: Modifies trailing print quantity commands
  (P<N> / Q<N>) for runtime batch resizing.

### M2. Industrial Web Dashboard & In-Memory Previews
- In-Memory PDF Rasterizer: Converts multi-page PDF quote sheets directly
  to ephemeral base64 JPEG/PNG images in volatile RAM.
- Ephemeral Cache & Shredding: Previews expire after a configurable TTL
  (default 600s). Raw document buffers undergo zero-overwrite memory wiping
  (buffer[i] = 0) followed by garbage collection, ensuring zero customer data
  remains on non-volatile storage.
- High-Density Visual Queue: Replaces card grids with a row-based visual table
  featuring inline quantity steppers, color tags, and print triggers.
- Industrial Styling Standard: Clean dark theme (#18191c), strictly pure ASCII,
  zero emojis, zero AI-themed badges, and zero simulated/fake readings.
- Physical Hardware Telemetry: Inspects physical sysfs carrier states
  (eth0/end0), character device nodes (/dev/usb/lp0), and live Gemini pool counts.

### M3. Dedicated 64GB SD Persistence & Systemd Sandboxing
- Dedicated Partitioning: scripts/setup_sdcard_storage.sh detects the attached
  64GB MicroSD card, creates an ext4 partition labeled LABEL_BACKUP, and mounts
  to /mnt/sdcard_backup with fstab options:
    defaults,noatime,commit=60,errors=remount-ro,nofail 0 2
- Non-Volatile Persistence: Transaction audit logs (audit_logs/scans,
  audit_logs/prints) and queue states (active_queues.json, transactions.sqlite3)
  persist on SD card, surviving hard power loss.
- Systemd Sandboxing: autolabel.service enforces MemoryMax=350M, PrivateTmp=true,
  ProtectSystem=full, and restricts filesystem write access to /opt/label_system
  and /mnt/sdcard_backup.

### M4. Resilient 8-State GitHub Auto-Update with Rollback
- State Machine: Executes an 8-state update sequence:
  1. IDLE -> 2. PRE_FLIGHT_CHECK -> 3. SNAPSHOT_CREATION ->
  4. FETCH_AND_RESET -> 5. DEPENDENCY_VALID -> 6. SERVICE_RESTART ->
  7. HEALTH_VALIDATION -> 8. COMMITTED / ROLLBACK_EXECUTE.
- SD Card Snapshots: Takes a pre-update tarball archive of application directories
  to /mnt/sdcard_backup/snapshots/, automatically pruning to retain the 5 newest.
- Health Validation: Probes http://127.0.0.1:8080/api/status for up to 30 seconds
  to verify complete JSON schema response (queues, hardware, gemini_pool).
- Automatic Rollback: If pre-flight, fetch, dependency install, or health probes
  fail, the engine immediately executes a git reset --hard to the previous commit,
  unpacks the snapshot tarball if necessary, restores .env, and restarts the service.

### M5. Multi-Cycle Verification & Out-of-Band Sync
- Multi-Cycle Verification: tests/test_multicycle_pipeline.py validates
  uninterrupted execution across consecutive scan intake cycles, Pick & Pack
  outer box label generation, batch reprints, preview dismissals, and database
  reconstruction after reload.
- Out-of-Band Deployment Sync: scripts/sync_to_orangepi.sh enables rsync/SSH
  synchronization from development workstations to the Orange Pi without
  committing secrets to Git.
- Secret Isolation: Strict .gitignore enforcement for .env, chmod 600 permissions,
  and deployment/env.template schema definition.

---

## Installation & Deployment

Refer to [DEPLOYMENT.md](DEPLOYMENT.md) for the complete, step-by-step production
deployment guide.

Quick Deployment Workflow:
```bash
# 1. On Orange Pi: Configure direct Ethernet for scanner
sudo ./scripts/setup_direct_ethernet.sh

# 2. On Orange Pi: Initialize dedicated 64GB MicroSD card
sudo ./scripts/setup_sdcard_storage.sh

# 3. On Development Workstation: Sync codebase out-of-band to Orange Pi
./scripts/sync_to_orangepi.sh --host 192.168.3.50 --restart-service

# 4. On Orange Pi: Configure environment secrets
cp /opt/label_system/.env.template /opt/label_system/.env
nano /opt/label_system/.env
chmod 600 /opt/label_system/.env

# 5. On Orange Pi: Restart and verify service
sudo systemctl restart autolabel.service
sudo systemctl status autolabel.service
```

---

## Test Suite Execution

The repository contains an opaque-box test suite and multi-cycle pipeline tests
that run without external hardware dependencies.

### 1. Primary Opaque-Box E2E Runner (60 Tests)
```bash
python tests/e2e_runner.py
```
Expected output:
```
================================================================================
  AUTONOMOUS LABEL ORCHESTRATOR - E2E TEST SCORECARD
  Hardware Target : Orange Pi PC Plus (Armbian trixie, Python 3.11+)
  Printer Target  : Argox OS-214 (203.2 DPI / 8 dots/mm, /dev/usb/lp0)
  Scanner Target  : Lexmark MX511 (Direct Ethernet SMTP:2525)
  Integrity Check : Strict Industrial Enterprise Standard (Zero Emojis)
================================================================================
...
================================================================================
  FINAL SUMMARY : [PASS]
  Total Tests   : 60
  Passed        : 60 (100.0%)
  Failed        : 0
  Errors        : 0
================================================================================
```

### 2. Multi-Cycle Pipeline Verification (6 Tests)
```bash
python -m unittest tests/test_multicycle_pipeline.py
```
Expected output:
```
......
----------------------------------------------------------------------
Ran 6 tests in ~0.5s

OK
```

### 3. Full Repository Test Discovery
```bash
python -m unittest discover -s tests
```

---

## Secret Protection & Git Integrity

- Active secrets (.env) are excluded from version control via .gitignore.
- Git history verification confirms 0 historical commits of .env:
  `git log --all --full-history -- ".env"` returns 0 commits.
- File permissions on production hosts must be locked down:
  `chmod 600 /opt/label_system/.env`
- Masked schema template is maintained at `.env.template` and `deployment/env.template`.

---

## License

Distributed under the [MIT License](LICENSE).
