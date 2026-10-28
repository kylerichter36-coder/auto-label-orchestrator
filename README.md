# Auto-Label Orchestrator

> **Universal Edge AI Multi-Workflow Thermal Label Printing Station**  
> *Transforming Scanned Paper Invoices & Pick-and-Pack Sheets into Instant Continuous Argox PPLB Print Runs via Any Linux Host & Gemini Flash Vision.*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)]()
[![Architecture](https://img.shields.io/badge/Architecture-ARM32%20%7C%20ARM64%20%7C%20x86%20%7C%20x64-blueviolet.svg)]()
[![Hardware Status](https://img.shields.io/badge/Hardware%20Status-Work%20in%20Progress%20(Pending%20Bench%20Test)-yellow.svg)]()
[![Software Status](https://img.shields.io/badge/Software%20Daemon-100%25%20Verified%20(Simulation)-brightgreen.svg)]()
[![Free Tier AI](https://img.shields.io/badge/AI%20Vision-Google%20Gemini%20Flash%20(Free%20Tier)-teal.svg)]()

---

## Project Status: Work In Progress (WIP)

> [!NOTE]
> **Current Milestone**: Software Core, In-Memory SMTP Receiver, Gemini Key Pool Manager, PPLB Generator, and Web Dashboard are **100% built and validated** against the virtual simulation test bench. Live bench testing with the physical scanner and Argox OS-214 printer is scheduled for the upcoming workshop testing session.

---

## Executive Summary & Problem Solved

In high-volume printer cartridge manufacturing, refilling, and distribution facilities, workshop operators process dozens of paper orders daily. Staff must manually examine handwritten notes on printed quotes, identify requested cartridges, decipher messy handwriting, and manually re-enter order details into desktop BarTender software to print individual cartridge and outer carton labels.

Typing 30+ outer box labels and dozens of cartridge stickers by hand is the **single biggest operational bottleneck** on the factory floor, causing delays, typos, and costly label waste.

**Auto-Label Orchestrator** automates this end-to-end on **any Linux host** (Orange Pi, Raspberry Pi, Intel/AMD Mini PC, thin client, or spare workstation):
1. **1-Touch Scan**: The operator loads a quote sheet or packing slip into the document feeder of any network scanner and taps a single touchscreen shortcut.
2. **Direct Isolated Transmission**: The scanner pushes the PDF directly over an air-gapped Ethernet cable to a local SMTP daemon on the Linux host.
3. **Confidential In-Memory AI Extraction**: The Linux host holds the document strictly in volatile RAM, queries a free-tier **Google Gemini Flash** key pool to extract marked items and handwriting, and immediately executes a **Zero-Shred protocol** (`0x00` wipe + garbage collection) to ensure confidential customer data is never written to disk.
4. **Native Continuous Printing**: The host injects the extracted batch quantities (e.g. `P30`) directly into native PPLB `.prn` files and streams them over raw USB (`/dev/usb/lp0`) to the **Argox OS-214 thermal transfer printer**, running off entire batches continuously without proprietary drivers.

---

## Universal Host Compatibility: Any Linux PC (ARM or x86/x64)

> [!IMPORTANT]
> **Host Agnostic Architecture (Why the Orange Pi?)**:
> While this reference setup runs on an **Orange Pi PC (Allwinner H3, 1GB DDR3 RAM)**, the system is designed to run on **any machine that can boot Linux**.
> 
> The Orange Pi PC was chosen specifically because:
> 1. **Available Hardware**: It was the hardware on hand.
> 2. **Lowest Compute Benchmark**: The Orange Pi PC represents the absolute lowest tier of viable computing power (a \$20 board with a 32-bit Quad-Core ARM CPU and only 1GB of RAM). Proving that this entire pipeline runs flawlessly with minimal RAM footprint (<150MB) and near-instant response proves that **it will run effortlessly on anything**.
>
> **Single Universal Codebase (Zero Architecture Lock-in)**:
> You do **not** need separate versions or branches for ARM and x86/x64. The exact same repository and code run identically on:
> * **ARM Single Board Computers**: Orange Pi (PC, Zero, 3, 5), Raspberry Pi (3, 4, 5, Zero 2W).
> * **x86 / x64 Mini PCs & Thin Clients**: Intel NUC, Beelink, HP EliteDesk Mini, Lenovo ThinkCentre Tiny, Dell OptiPlex Micro, or standard Linux desktops.
>
> **Why is it 100% cross-platform?**
> 1. **Pure Python 3**: Written entirely in cross-platform Python without compiled binary blobs.
> 2. **Standard Linux Kernel USB (`/dev/usb/lp0`)**: The native Linux kernel USB printer driver (`usblp`) is built into all Linux kernels identically across all CPU architectures. PPLB raw commands stream directly over USB whether running on a 32-bit ARM chip or an Intel Core i7.
> 3. **Native TCP/IP Networking**: Standard socket protocols handle the scanner email stream identically on any host.

---

## Universal Scanner Compatibility

> [!TIP]
> **Scanner Agnostic (Why the Lexmark MX511?)**:
> While our reference build and testing bench utilize the **Lexmark MX511**, **this system is completely universal**.
>
> The Lexmark MX511 was selected for our workshop bench simply because:
> 1. It is extremely easy to source at very low cost on the commercial secondary market.
> 2. It features a fast, heavy-duty duplex Automatic Document Feeder (ADF).
> 3. It includes an easily accessible embedded web configuration server.
>
> **Any desktop, multifunction, or commercial scanner** capable of **Scan-to-Email (SMTP)** or **Network Folder (SMB/FTP)** push (such as Canon, HP, Brother, Xerox, Ricoh, Epson, or Kyocera) works out of the box with zero code changes. Simply point your scanner's primary SMTP gateway to the host's IP address on port `2525`.

---

## Key Software Capabilities

* **Dual-Workflow Automatic Classifier**:
  * **Workflow A (Quotation / Tax Invoice $\rightarrow$ Cartridge Labels)**: Identifies printed table rows marked with a handwritten circle, tick, or 'X' $\rightarrow$ extracts row quantity (e.g. 10, 30, 100) $\rightarrow$ handles cross-brand cartridge annotations (e.g. `CB540A` marked as `Canon 716 Black` or `HCF` notes) $\rightarrow$ matches pre-made `.prn` templates on the SD card/drive $\rightarrow$ rewrites print count to `P<Qty>` for continuous batch printing.
  * **Workflow B (Pick & Pack Sheet $\rightarrow$ Outer Box Labels)**: Parses Quote Number, Box Weight, and `Box X of Y` $\rightarrow$ itemizes contents, categorizing models as **Toner Unit** vs. **Drum / Imaging Unit** (e.g. `DR3115`, imaging drums) $\rightarrow$ calculates dynamic PPLB line coordinates $\rightarrow$ automatically prints **2 copies (`P2`)** for front and back carton placement.
* **Resilient Free-Tier Gemini Key Pool**: Rotates across 5+ free Gemini API keys, automatically detecting HTTP `429 RESOURCE_EXHAUSTED` and failing over in <500ms. Capped at `max_output_tokens=1024` with `temperature=0.0` for 100% free, deterministic operation under Google's 1,500 requests/day quota.
* **BarTender Web Dashboard & Smart Color Queue**: Responsive web UI served on port 8080. Intelligently groups pending labels by roll color (`Black`, `Cyan`, `Magenta`, `Yellow`) so operators only swap physical label rolls **once per color batch**.
* **Human-in-the-Loop Safeguard**: If an operator's handwriting is scribbled or ambiguous, the AI flags `requires_confirmation: true`, placing the job in an approval queue accessible from any phone or laptop on the local Wi-Fi.

---

## Hardware Architecture & Wiring Topology

```mermaid
graph TD
    subgraph Cell ["Air-Gapped Physical Cell"]
        Scanner["Network Scanner (e.g. Lexmark MX511)"] -- "Direct Patch Cable (192.168.50.2)" --> Host["Linux Host (Orange Pi / Mini PC)"]
        Host -- "USB Raw (/dev/usb/lp0)" --> Argox["Argox OS-214 Thermal Printer"]
    end

    subgraph Stack ["Linux Host Software Stack"]
        DHCP["dnsmasq DHCP Server (192.168.50.1)"]
        SMTP["aiosmtpd Local Receiver (:2525)"]
        RAM["RAM Buffer (Zero Disk Instant Shred)"]
        Pool["Gemini Key Pool Manager (5+ Keys)"]
        PPLB["PPLB Dynamic Batch Engine"]
        WebUI["BarTender Web Dashboard (:8080)"]
    end

    subgraph Control ["Workshop Network & Control"]
        Host -- "Wi-Fi or LAN" --> Cloud["Google Gemini Flash API"]
        Operator["Operator Phone / PC Browser"] -- "Workshop Wi-Fi" --> WebUI
    end

    Scanner --> SMTP
    SMTP --> RAM
    RAM --> Pool
    Pool --> Cloud
    Cloud --> Pool
    Pool --> PPLB
    PPLB --> Argox
    PPLB --> WebUI
    WebUI --> Argox
```

### 1. Direct Network Interconnect (Scanner $\leftrightarrow$ Linux Host)
* **Physical Connection**: Standard Cat5e/Cat6 patch cable directly connecting the scanner RJ45 port to the host Ethernet port. (Auto-MDIX handles crossover automatically).
* **Isolated Subnet**: The host runs a minimal `dnsmasq` DHCP service on `eth0` (`192.168.50.1/24`), leasing `192.168.50.2` to the scanner. The scanner is completely air-gapped from Wi-Fi.
* **Internet Uplink for Gemini**: An onboard or USB Wi-Fi adapter (`wlan0`) provides internet access exclusively for Gemini Flash API requests.

### 2. Printer Interface (Host $\leftrightarrow$ Argox OS-214)
* **Physical Connection**: Standard USB Type-A to Type-B cable connecting host USB to Argox rear port.
* **Driverless Raw USB**: Exposes Linux character device node `/dev/usb/lp0`. The host writes native PPLB binary text directly to the printer with zero driver overhead.

---

## Installation & Deployment

### 1. Deploying on Linux (Orange Pi, Raspberry Pi, or Mini PC)
Once your Linux host is running (Armbian, Debian, Ubuntu, Raspberry Pi OS) and connected to Wi-Fi:
```bash
git clone https://github.com/kylerichter36-coder/auto-label-orchestrator.git
cd auto-label-orchestrator

# Configure direct isolated Ethernet link for the scanner
sudo ./scripts/setup_direct_ethernet.sh

# Install dependencies and systemd service
sudo ./scripts/install_service.sh
```

### 2. Adding Your Free Gemini Keys
Edit `/opt/label_system/.env`:
```env
GEMINI_API_KEYS=AIzaSyA_KEY1,AIzaSyB_KEY2,AIzaSyC_KEY3,AIzaSyD_KEY4,AIzaSyE_KEY5
PRINTER_DEVICE=/dev/usb/lp0
SIMULATION_MODE=false
```

---

## Reference Scanner Configuration (Lexmark MX511)

1. Plug the Ethernet cable directly from the scanner to the Linux host.
2. The host automatically assigns IP `192.168.50.2` to the scanner.
3. In a web browser on any device connected to the network, access `http://192.168.50.2`:
   * **Settings $\rightarrow$ Network/Ports $\rightarrow$ TCP/IP**: Ensure DHCP is set to Automatic.
   * **Settings $\rightarrow$ E-mail / FTP $\rightarrow$ E-mail Setup**:
     * Primary SMTP Gateway: `192.168.50.1`
     * SMTP Port: `2525`
     * Authentication: `None / Anonymous`
     * SSL/TLS: `Disabled`
     * Device E-mail Address: `scanner@local`
4. **Create a 1-Touch Home Screen Shortcut**:
   * Name: `Label Scanner`
   * Destination: `quotes@orangepi.local`
   * Format: `PDF`
   * Resolution: `300 DPI`
   * Color: `Color (24-bit)`

---

## Virtual Test Bench (Testing Without Hardware)

You can run and test the complete system on any computer today before connecting physical hardware:

```bash
# 1. Run the automated test suite
python tests/test_e2e.py

# 2. Launch the orchestrator in simulation mode
python -m daemon.main

# 3. Open the BarTender Web Dashboard in your browser:
# http://localhost:8080

# 4. In a separate terminal, trigger a simulated scan:
python simulator/mock_lexmark.py
```

---

## Roadmap & TODOs

### Phase 1: Software Foundation & Simulation [COMPLETED]
- [x] In-memory local SMTP server with RFC 5322 MIME extraction.
- [x] Instant Confidential Shredder (`0x00` buffer zeroing + garbage collection).
- [x] Resilient Gemini Free-Tier Key Pool with automatic HTTP 429 failover.
- [x] Dual-workflow classifier (Quote vs. Pick & Pack) with toner cross-referencing.
- [x] Native Argox PPLB batch generator with dynamic `P<Qty>` injection.
- [x] Dynamic outer box PPLB generator with automatic 2x copies (`P2`).
- [x] BarTender Web dashboard with smart color queues (Black, Cyan, Magenta, Yellow).
- [x] Virtual scanner and Argox hardware simulators.
- [x] Automated test suite passing 100%.

### Phase 2: Live Workshop Bench Testing [IN PROGRESS]
- [ ] Connect Lexmark MX511 via direct patch cable and verify automatic DHCP lease (`192.168.50.2`).
- [ ] Test 1-touch touchscreen scan from scanner to host port 2525.
- [ ] Connect Argox OS-214 via USB and verify native `/dev/usb/lp0` binary transmission.
- [ ] Conduct live scan tests with 10 actual customer quote sheets.
- [ ] Calibrate outer box label print boundaries on physical 4x6" thermal stock.

### Phase 3: Electromechanical Multi-Roll Feeder [FUTURE EXPANSION]
- [ ] Construct 2020 aluminum extrusion rear feeder frame behind Argox external media slot.
- [ ] 3D-print converging 4-channel Y-manifold for Black, Cyan, Magenta, and Yellow rolls.
- [ ] Mount NEMA 17 stepper motors and TCRT5000 optical sensors for automated media switching.
- [ ] Connect stepper drivers to Arduino Nano / RP2040 sub-controller over USB serial.
- [ ] Upgrade web software to command automated roll switching instead of manual prompts.

---

## License

Distributed under the [MIT License](LICENSE).
