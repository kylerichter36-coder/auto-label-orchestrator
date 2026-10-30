import os
from pathlib import Path

# Gracefully load .env with or without python-dotenv
BASE_DIR = Path(__file__).resolve().parent.parent
env_file = BASE_DIR / ".env"

try:
    from dotenv import load_dotenv
    load_dotenv(env_file)
except ImportError:
    # Minimal fallback parser if dotenv is not installed
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

class Config:
    BASE_DIR = BASE_DIR

    # Gemini API Key Pool (split by comma)
    _raw_keys = os.getenv("GEMINI_API_KEYS", "")
    GEMINI_API_KEYS = [k.strip() for k in _raw_keys.split(",") if k.strip()]
    if not GEMINI_API_KEYS and os.getenv("GEMINI_API_KEY"):
        GEMINI_API_KEYS = [os.getenv("GEMINI_API_KEY").strip()]

    # Model parameters
    GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
    MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1024"))
    TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))

    # Direct Ethernet & SMTP Configuration
    SMTP_HOST = os.getenv("SMTP_HOST", "0.0.0.0")
    SMTP_PORT = int(os.getenv("SMTP_PORT", "2525"))
    DIRECT_ETH_IP = os.getenv("DIRECT_ETH_IP", "192.168.50.1")
    SCANNER_IP = os.getenv("SCANNER_IP", "192.168.50.2")

    # Web Dashboard
    WEB_HOST = os.getenv("WEB_HOST", "0.0.0.0")
    WEB_PORT = int(os.getenv("WEB_PORT", "8080"))

    # Printing & Device
    PRINTER_DEVICE = os.getenv("PRINTER_DEVICE", "/dev/usb/lp0")
    SIMULATION_MODE = os.getenv("SIMULATION_MODE", "true").lower() in ("true", "1", "yes")

    # Directories
    LABEL_DIR = Path(os.getenv("LABEL_DIR", str(BASE_DIR / "templates" / "sample_labels")))
    TEMPLATE_DIR = Path(os.getenv("TEMPLATE_DIR", str(BASE_DIR / "templates")))

    # SD Card & Non-Volatile Persistence Storage
    SDCARD_MOUNT = Path(os.getenv("SDCARD_MOUNT", "/mnt/sdcard_backup"))
    FALLBACK_STORAGE_DIR = Path(os.getenv("FALLBACK_STORAGE_DIR", str(BASE_DIR / "backup_storage")))
    AUDIT_LOG_DIR = Path(os.getenv("AUDIT_LOG_DIR", str(SDCARD_MOUNT / "audit_logs" if SDCARD_MOUNT.exists() else BASE_DIR / "audit_logs")))

    # Ensure local dev directories exist
    LABEL_DIR.mkdir(parents=True, exist_ok=True)
    TEMPLATE_DIR.mkdir(parents=True, exist_ok=True)
    FALLBACK_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_LOG_DIR.mkdir(parents=True, exist_ok=True)

config = Config()
