#!/usr/bin/env bash
# ==============================================================================
# Install Auto-Label Orchestrator as Systemd Service on Armbian / Orange Pi PC
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./install_service.sh)"
  exit 1
fi

INSTALL_DIR="/opt/label_system"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> Creating installation directory at ${INSTALL_DIR}..."
mkdir -p "${INSTALL_DIR}"
mkdir -p "${INSTALL_DIR}/labels"
mkdir -p "${INSTALL_DIR}/templates"

echo "==> Copying application files..."
cp -r "${SOURCE_DIR}/daemon" "${INSTALL_DIR}/"
cp -r "${SOURCE_DIR}/web" "${INSTALL_DIR}/"
cp -r "${SOURCE_DIR}/templates/"* "${INSTALL_DIR}/labels/" 2>/dev/null || true

if [ ! -f "${INSTALL_DIR}/.env" ]; then
    echo "==> Creating default .env configuration..."
    cp "${SOURCE_DIR}/.env.example" "${INSTALL_DIR}/.env"
fi

echo "==> Installing Python dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv
pip3 install --break-system-packages -r "${SOURCE_DIR}/requirements.txt"

echo "==> Installing Systemd unit..."
cp "${SOURCE_DIR}/systemd/autolabel.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable autolabel.service
systemctl restart autolabel.service

echo "=============================================================================="
echo " Auto-Label Orchestrator Successfully Installed and Started!"
echo " Check Status: systemctl status autolabel.service"
echo " View Logs:    journalctl -u autolabel.service -f"
echo " Web UI:       http://$(hostname -I | awk '{print $1}'):8080"
echo "=============================================================================="
