# Test Infrastructure & Methodology Specification (TEST_INFRA)

**Document ID:** INFRA-TEST-ORCHESTRATOR-2026-10-29  
**System Target:** Autonomous Label Orchestrator  
**SBC Target:** Orange Pi PC Plus (Allwinner H3, 1GB DDR3 RAM, 8GB eMMC + 64GB MicroSD)  
**Printer Target:** Argox OS-214 Thermal Transfer Printer (203.2 DPI, /dev/usb/lp0)  
**Scanner Target:** Lexmark MX511 Multifunction Scanner (Direct Ethernet SMTP:2525)  
**Integrity Standard:** Zero Emojis, Zero Fake Telemetry, Strict Industrial Enterprise BarTender Architecture  

---

## 1. Architecture & Test Design Methodology

The Autonomous Label Orchestrator test suite adheres strictly to **Opaque-Box Requirement-Driven Testing**. The test suite treats the system as an opaque industrial appliance accessed via standard entry points:
- **Binary/Text Print Streams:** Native Argox PPLB (Eltron EPL2) and PPLA (Datamax DPL) byte streams.
- **Network Ports:** Local SMTP intake on port `2525` and Enterprise Web UI on HTTP port `8080`.
- **Hardware Device Nodes:** Direct character device checks on `/dev/usb/lp0` and sysfs carrier `/sys/class/net/end0/carrier`.
- **Non-Volatile Storage Topologies:** Ext4 partition mount `/mnt/sdcard_backup/` with atomic write protocols.
- **Lifecycle Automations:** 8-state GitHub auto-update manager with health check validation and rollback.

### 4-Tier Test Matrix

| Tier | Purpose | Target Coverage | Test Count |
|---|---|---|---|
| **Tier 1** | **Feature Coverage** | Primary nominal workflows for all architectural modules | 27 tests |
| **Tier 2** | **Boundary & Corner Cases** | Buffer extremes, malformed syntax, network drops, storage errors, 429 backoff | 25 tests |
| **Tier 3** | **Cross-Feature Pairwise** | Multi-system interactions (SMTP -> OCR -> Queue -> Print -> Web UI) | 5 tests |
| **Tier 4** | **Warehouse Workloads** | Real-world pick & pack carton runs, multi-color quotation sheets, binary streams | 3 tests |
| **Total** | **Comprehensive E2E Suite** | End-to-end full operational pipeline verification | **60 tests** |

---

## 2. Test Execution & CLI Commands

### Master Test Runner: `e2e_runner.py`
The master executable test runner is located at `tests/e2e_runner.py`. It runs the test tiers and renders an industrial enterprise scorecard.

```powershell
# 1. Run Complete 4-Tier Suite (Default)
python tests/e2e_runner.py

# 2. Run Specific Tiers (1 to 4)
python tests/e2e_runner.py --tier 1
python tests/e2e_runner.py --tier 2
python tests/e2e_runner.py --tier 3
python tests/e2e_runner.py --tier 4

# 3. Run Verbose Mode (Individual Test Timings and Summaries)
python tests/e2e_runner.py --verbose

# 4. Fail-Fast Execution (Halt on first error)
python tests/e2e_runner.py --failfast
```

### Standard Python Discovery Runner
```powershell
# Run with Python standard unittest runner
python -m unittest tests/test_opaque_e2e.py

# Discover all test files across tests/
python -m unittest discover -s tests
```

---

## 3. Feature Inventory Mapping (PROJECT.md Mapping)

Every feature defined in `PROJECT.md` is covered by corresponding test methods in `test_opaque_e2e.py`:

| # | Feature Name | Source Requirement | Primary Test Methods | Tier |
|---|---|---|---|---|
| 1 | BarTender Ingestion & AST Parser | ORIGINAL_REQUEST §R1 | `test_t1_01_pplb_syntax_validation_sample_templates`, `test_t1_04_ppla_grammar_and_control_codes` | Tier 1 |
| 2 | Argox 203 DPI Coordinate Engine | ORIGINAL_REQUEST §R1 | `test_t1_05_203dpi_coordinate_scaling`, `test_t2_01_coordinate_boundary_limits` | Tier 1 & 2 |
| 3 | Native PPLB Emitter Engine | ORIGINAL_REQUEST §R1 | `test_t1_01_pplb_syntax_validation_sample_templates`, `test_t4_03_end_to_end_argox_binary_stream_verification` | Tier 1 & 4 |
| 4 | Native PPLA Emitter Engine | ORIGINAL_REQUEST §R1 | `test_t1_04_ppla_grammar_and_control_codes` | Tier 1 |
| 5 | Barcode Symbology Generation | ORIGINAL_REQUEST §R1 | `test_t1_06_barcode_symbology_code128_qr_datamatrix`, `test_t2_05_unencodable_barcode_characters` | Tier 1 & 2 |
| 6 | Dynamic Template Quantity Injection | Codebase Survey | `test_t1_02_dynamic_quantity_injection_pplb`, `test_t2_03_batch_quantity_boundary_p0_p1_p9999` | Tier 1 & 2 |
| 7 | In-Memory PDF Page Rasterizer | ORIGINAL_REQUEST §R2 | `test_t1_09_smtp_influx_mime_pdf_extraction_in_memory`, `test_t2_09_multipage_pdf_page_limit_guard` | Tier 1 & 2 |
| 8 | Ephemeral Preview Store & Shredding | ORIGINAL_REQUEST §R2 | `test_t1_07_buffer_shredding_zero_overwrite`, `test_t1_08_buffer_shredding_memory_cleanup`, `test_t1_10_ephemeral_preview_store_contract` | Tier 1 |
| 9 | Web UI Scanned PDF Viewer | ORIGINAL_REQUEST §R2 | `test_t1_13_web_index_and_setup_pages_served`, `test_t3_02_web_preview_retrieval_during_active_scan` | Tier 1 & 3 |
| 10 | Enterprise Row-Based Queue Table | ORIGINAL_REQUEST §R2 | `test_t1_14_web_print_color_queue_dispatch`, `test_t1_15_web_print_single_job_and_outer_box` | Tier 1 |
| 11 | Zero-Emoji & Industrial Design Audit | ORIGINAL_REQUEST §R2, §R5 | `test_t1_16_zero_emoji_audit_web_responses` | Tier 1 |
| 12 | Honest Physical Hardware Telemetry | ORIGINAL_REQUEST §R2 | `test_t1_17_honest_hardware_telemetry_reporting`, `test_t3_05_printer_offline_detection_during_queue_dispatch` | Tier 1 & 3 |
| 13 | 64GB SD Card Storage Partitioning | ORIGINAL_REQUEST §R3 | `test_t1_18_sd_card_directory_topology`, `test_t1_22_sdcard_mount_options_fstab_validation` | Tier 1 |
| 14 | Non-Volatile Queue & Audit Persistence | ORIGINAL_REQUEST §R3 | `test_t1_19_atomic_write_fsync_replace_pattern`, `test_t1_20_persistent_queue_state_serialization`, `test_t1_21_transaction_audit_log_jsonl_integrity` | Tier 1 |
| 15 | Systemd Service Hardening & Sandboxing | Hardware Survey | `test_t1_26_autoupdate_health_validation_protocol` | Tier 1 |
| 16 | Dynamic Ethernet Carrier Resolver | Hardware Survey | `test_t1_12_web_status_endpoint_contract`, `test_t1_17_honest_hardware_telemetry_reporting` | Tier 1 |
| 17 | Resilient 8-State GitHub Auto-Update | ORIGINAL_REQUEST §R4 | `test_t1_23_autoupdate_8state_machine_transition_sequence`, `test_t1_24_autoupdate_preflight_checks`, `test_t1_25_autoupdate_snapshot_tarball_creation` | Tier 1 |
| 18 | Automatic Rollback Safeguards | ORIGINAL_REQUEST §R4 | `test_t1_27_autoupdate_rollback_on_failure`, `test_t2_22_autoupdate_health_check_timeout`, `test_t2_23_autoupdate_health_check_http500_response` | Tier 1 & 2 |
| 19 | Opaque-Box E2E Test Suite | Project Pattern | Entire test suite (`tests/test_opaque_e2e.py` & `tests/e2e_runner.py`) | All Tiers |
| 20 | Out-of-Band Orange Pi Deployment Sync | ORIGINAL_REQUEST §R5 | Excluded from local git index; verified via `test_t1_11_zero_disk_write_during_pdf_lifecycle` | Tier 1 |
| 21 | Environment Secret & Git Isolation | ORIGINAL_REQUEST §R5 | `.env` exclusion, key masking tested in `test_t1_12_web_status_endpoint_contract` | Tier 1 |
| 22 | Multi-Cycle Pipeline Verification | ORIGINAL_REQUEST §R5 | `test_t3_01_full_intake_pipeline_smtp_to_print_stream`, `test_t4_01`, `test_t4_02` | Tier 3 & 4 |

---

## 4. Test Suite Architecture & File Structure

```
auto_label_orchestrator/
├── tests/
│   ├── __init__.py                # Package initialization
│   ├── e2e_runner.py              # Master CLI test runner with scorecard
│   ├── test_opaque_e2e.py         # 60 opaque-box test cases across Tiers 1-4
│   ├── test_e2e.py                # Legacy baseline integration tests
│   └── test_bartender_converter.py# Unit tests for M1 BarTender parser
├── TEST_INFRA.md                  # Test architecture and feature inventory mapping
└── TEST_READY.md                  # Published test completion signal & scorecard
```

---

## 5. Verification Integrity Checklist

- [x] **Zero-Emoji Compliance:** RegEx scanner asserts zero Unicode emojis across all HTML, JSON, and log outputs.
- [x] **Zero Fake Readings:** Hardware indicators honestly report offline states when `/dev/usb/lp0` is disconnected.
- [x] **Zero Disk Leaks:** PDF ingestion, preview, and shredding validated to write zero temporary files to eMMC.
- [x] **Power-Loss Safeguards:** Atomic write protocols verified to prevent truncated or corrupted configuration files.
- [x] **Rate-Limit Resilience:** 10-key Gemini pool rotation and automatic 429 failover validated under simulated load.
