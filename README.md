# Auto-Label Orchestrator 🏷️⚡

> **Edge AI Multi-Workflow Thermal Label Printing Station**  
> *Transforming Scanned Paper Invoices & Pick-and-Pack Sheets into Instant Continuous Argox PPLB Print Runs via Orange Pi & Gemini Flash Vision.*

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-brightgreen.svg)]()
[![Target SBC](https://img.shields.io/badge/Platform-Orange%20Pi%20PC%20(1GB%20RAM)-orange.svg)]()
[![Printer Protocol](https://img.shields.io/badge/Printer-Argox%20PPLB%20(Raw%20USB)-purple.svg)]()
[![Free Tier AI](https://img.shields.io/badge/AI%20Vision-Google%20Gemini%20Flash%20(Free%20Tier)-teal.svg)]()

---

## 📖 Executive Summary

In high-volume printer cartridge refilling, manufacturing, and warehouse logistics, warehouse operators ("okies") often spend hours manually deciphering handwritten quotes and retyping order details into desktop BarTender software to print thermal transfer labels.

**Auto-Label Orchestrator** automates this entire pipeline on a \$25 **Orange Pi PC (1GB RAM)**:
1. An operator drops a quote or packing sheet onto a **Lexmark MX511** scanner and taps a 1-touch touchscreen shortcut.
2. The scanner transmits the document over a direct, isolated Ethernet cable directly to the Orange Pi via a local SMTP listener.
3. The Orange Pi buffers the PDF **purely in RAM** (zero disk footprint for strict confidentiality), calls a resilient **Google Gemini Flash Free-Tier Key Pool**, accurately parses messy handwriting and circled SKUs, and immediately zeroes the memory buffer.
4. Extracted print jobs are routed through a browser-based **"BarTender Web"** dashboard and dispatched natively to an **Argox OS-214 thermal transfer printer** over raw USB (`/dev/usb/lp0`).

---

## 🚀 Key Features

* **Zero-Driver Direct Ethernet Link:** Direct LAN patch cable connects Lexmark MX511 to Orange Pi `eth0` with an isolated internal DHCP server (`192.168.50.1/24`). No router, network switch, or proprietary Linux scanner drivers needed.
* **Dual Automatic Workflow Classification:**
  * **Workflow A (Quotation / Tax Invoice):** Detects hand-drawn circles, ticks, or 'X's next to printed cartridge rows $\rightarrow$ extracts row quantity (e.g. 10, 30, 100) $\rightarrow$ matches pre-made `.prn` label on SD card $\rightarrow$ dynamically injects continuous batch command `P<Qty>` into Argox PPLB stream.
  * **Workflow B (Pick & Pack Outer Box Labels):** Reads Quote #, Weight, and `Box X of Y` $\rightarrow$ itemizes contents, categorizing as **Toner Unit** vs. **Drum / Imaging Unit** (e.g. `DR3115`, Lexmark imaging drums) $\rightarrow$ calculates dynamic PPLB line offsets $\rightarrow$ automatically prints **2 copies (`P2`)** for front and back carton labeling.
* **Toner Cross-Referencing & Handwriting OCR:** Understands industry annotations (e.g., cross-referencing an HP `CB540A` to `Canon 716 Black` or `HCF` high-capacity notes).
* **Confidential In-Memory Shredding:** Scanned PDFs are held strictly in volatile RAM (`io.BytesIO`), zero-overwritten with `0x00`, and garbage-collected immediately after AI analysis. No scans are ever saved to the MicroSD card.
* **Free-Tier Gemini Key Pool:** Round-robin pool of 5+ free Gemini API keys with automatic HTTP 429 (`RESOURCE_EXHAUSTED`) failover and a hard `max_output_tokens=1024` cap. Operates 100% free under Google's 1,500 requests/day quota.
* **Smart Color Queue & BarTender Web GUI:** Groups pending labels by roll color (`Black`, `Cyan`, `Magenta`, `Yellow`). Operators swap the physical roll only once per color batch.
* **ARM-Native Driverless Printing:** Streams native PPLB binary commands directly to `/dev/usb/lp0`. Proves no x86/x64 PC is required for industrial thermal printing.

---

## 🏗️ System Architecture

```mermaid
graph TD
    subgraph Isolated Workshop Hardware Cell
        Lexmark[Lexmark MX511 Scanner] -- "Direct LAN Cable (192.168.50.2)" --> Pi[Orange Pi PC]
        Pi -- "Raw USB (/dev/usb/lp0)" --> Argox[Argox OS-214 Thermal Printer]
    end

    subgraph Orange Pi Software Architecture
        DHCP[dnsmasq DHCP / 192.168.50.1]
        SMTP[aiosmtpd Local Receiver :2525]
        RAM[RAM Buffer / Instant 0x00 Shred]
        Pool[Gemini Key Pool Manager (5+ Keys)]
        Engine[PPLB Printer Engine]
        WebUI[BarTender Web Dashboard :8080]
    end

    subgraph External & Uplink
        Pi -- "USB Wi-Fi Dongle (wlan0)" --> Cloud[Google Gemini Flash API]
        Laptop[Operator Laptop / Phone Browser] -- "Local Wi-Fi" --> WebUI
    end

    Lexmark --> SMTP
    SMTP --> RAM
    RAM --> Pool
    Pool --> Cloud
    Cloud --> Pool
    Pool --> RAM
    RAM -.->|Zero Overwrite + GC| RAM
    Pool --> Engine
    Engine -->|Automatic Batch| Argox
    Engine --> WebUI
    WebUI -->|Operator Dispatches Color Batch| Argox
```

---

## 🛠️ Bill of Materials (BOM)

| Item | Model / Specification | Purpose |
| :--- | :--- | :--- |
| **Single Board Computer** | Orange Pi PC (Allwinner H3, 1GB DDR3 RAM) | Central orchestrator, web server, and printer interface |
| **Storage** | 8GB or 16GB MicroSD Card (Class 10) | Armbian Minimal OS + Label templates |
| **Scanner** | Lexmark MX511 (Multifunction ADF / Flatbed) | Scans quotes and pick & pack sheets |
| **Thermal Printer** | Argox OS-214 (Thermal Transfer, 203 DPI) | Prints continuous label batches via ink ribbon |
| **Network Cable** | Standard Cat5e / Cat6 Patch Cable (RJ45) | Direct connection between Lexmark and Orange Pi |
| **Wi-Fi Adapter** | 2.4GHz USB Wi-Fi Dongle | Connects Orange Pi to workshop Wi-Fi for Gemini API calls |
| **Power Supply** | 5V 3A DC Barrel Jack | Dedicated clean power for Orange Pi |

---

## 💻 Software Installation & Setup

### 1. Flash Armbian onto the 8GB MicroSD Card
Download and flash **Armbian Minimal (Headless CLI)** using BalenaEtcher or Raspberry Pi Imager. An 8GB SD card leaves >5GB of free space.

### 2. Clone Repository & Install
```bash
git clone https://github.com/your-username/auto-label-orchestrator.git
cd auto-label-orchestrator

# Setup direct isolated Ethernet link for Lexmark
sudo ./scripts/setup_direct_ethernet.sh

# Install systemd background service
sudo ./scripts/install_service.sh
```

### 3. Configure API Keys
Edit `/opt/label_system/.env`:
```env
GEMINI_API_KEYS=AIzaSyD-KEY1,AIzaSyD-KEY2,AIzaSyD-KEY3
PRINTER_DEVICE=/dev/usb/lp0
SIMULATION_MODE=false
```

---

## 🖨️ Lexmark MX511 Configuration Guide

1. Connect the Ethernet cable directly from the Lexmark MX511 to the Orange Pi.
2. The Orange Pi automatically assigns IP `192.168.50.2` to the scanner.
3. On a PC or phone, open `http://192.168.50.2` in a browser to view the Lexmark Embedded Web Server:
   * **Settings $\rightarrow$ Network/Ports $\rightarrow$ TCP/IP**: Set to DHCP (Automatic).
   * **Settings $\rightarrow$ E-mail / FTP $\rightarrow$ E-mail Setup**:
     * Primary SMTP Gateway: `192.168.50.1`
     * SMTP Port: `2525`
     * Authentication: `None / Anonymous`
     * SSL/TLS: `Disabled`
     * Device E-mail Address: `scanner@lexmark.local`
4. **Create a 1-Touch Home Screen Shortcut**:
   * Name: `Label Scanner`
   * Recipient: `quotes@orangepi.local`
   * Format: `PDF`
   * Color: `Color` (300 DPI)

---

## 🧪 Testing with the Built-In Hardware Simulator

No physical Lexmark scanner or Argox printer at home? Run the virtual test bench on any laptop:

```bash
# 1. Start the main orchestrator in simulation mode
python -m daemon.main

# 2. Open the BarTender Web Dashboard in your browser:
# http://localhost:8080

# 3. In another terminal, fire a simulated Lexmark scan:
python simulator/mock_lexmark.py

# 4. View the virtual Argox printer output and real-time PPLB commands:
python -m unittest tests/test_e2e.py
```

---

## 📄 License
Released under the [MIT License](LICENSE).
