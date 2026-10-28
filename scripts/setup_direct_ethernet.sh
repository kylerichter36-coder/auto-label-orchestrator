#!/usr/bin/env bash
# ==============================================================================
# Setup Direct Ethernet Link (Orange Pi PC <-> Lexmark MX511)
# ==============================================================================
# Configures eth0 on the Orange Pi with a static IP (192.168.50.1) and a dedicated
# dnsmasq DHCP server that assigns 192.168.50.2 to the Lexmark MX511 scanner.
# Keeps the printer completely off your workshop Wi-Fi.
# ==============================================================================

set -e

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (sudo ./setup_direct_ethernet.sh)"
  exit 1
fi

echo "==> Installing dnsmasq and network utilities..."
apt-get update -qq
apt-get install -y -qq dnsmasq net-tools iproute2

echo "==> Configuring static IP on eth0 (192.168.50.1)..."
cat << 'EOF' > /etc/network/interfaces.d/eth0-direct
# Direct connection to Lexmark MX511 scanner
auto eth0
iface eth0 inet static
    address 192.168.50.1
    netmask 255.255.255.0
EOF

ifdown eth0 2>/dev/null || true
ifup eth0 2>/dev/null || ip addr add 192.168.50.1/24 dev eth0 || true

echo "==> Configuring dnsmasq for eth0 DHCP leases..."
cat << 'EOF' > /etc/dnsmasq.d/lexmark-direct.conf
# Direct isolated DHCP lease for Lexmark MX511
interface=eth0
bind-interfaces
dhcp-range=192.168.50.2,192.168.50.10,255.255.255.0,24h
dhcp-option=3 # Suppress default gateway announcement over eth0 so Wi-Fi stays primary
dhcp-option=6 # Suppress DNS announcement over eth0
EOF

echo "==> Restarting dnsmasq..."
systemctl restart dnsmasq
systemctl enable dnsmasq

echo "=============================================================================="
echo " Direct Ethernet Setup Complete!"
echo " 1. Plug standard LAN cable between Orange Pi (eth0) and Lexmark MX511."
echo " 2. Lexmark will automatically receive IP: 192.168.50.2"
echo " 3. In Lexmark Web Settings -> E-mail Setup:"
echo "    - Primary SMTP Gateway: 192.168.50.1"
echo "    - SMTP Port: 2525"
echo "    - Authentication: None / Anonymous"
echo "=============================================================================="
