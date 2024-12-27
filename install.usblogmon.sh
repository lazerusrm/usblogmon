#!/usr/bin/env bash

# ============================================================================
# USB Log Manager Installer
# For Debian/Ubuntu, including LXC containers where systemctl is available
# ----------------------------------------------------------------------------
# 1. Installs needed packages (python3, requests, pyudev, parted, e2fsprogs, curl).
# 2. Downloads usb_log_manager.py to /opt/usblogmon (creates dir if needed).
# 3. Creates a systemd service in /etc/systemd/system/usblogmon.service
# 4. Enables and starts the usblogmon.service with systemd
# ============================================================================
set -euo pipefail

SCRIPT_URL="https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"
INSTALL_DIR="/opt/usblogmon"
INSTALL_SCRIPT="${INSTALL_DIR}/usb_log_manager.py"
SERVICE_FILE="/etc/systemd/system/usblogmon.service"

echo "==============================================================="
echo "         USB Log Manager Installer"
echo "==============================================================="

# 1. Check for root privileges
if [[ "$(id -u)" -ne 0 ]]; then
    echo "Error: Please run as root (sudo)!"
    exit 1
fi

# 2. Update package list and install dependencies
echo "Updating package list with apt-get update..."
apt-get update -y

echo "Installing required packages via apt-get ..."
apt-get install -y \
  python3 \
  python3-requests \
  python3-pyudev \
  parted \
  e2fsprogs \
  curl

# 3. Create installation directory, download the python script
echo "Creating installation directory at ${INSTALL_DIR} ..."
mkdir -p "${INSTALL_DIR}"

echo "Downloading usb_log_manager.py from ${SCRIPT_URL} ..."
curl -fsSL "${SCRIPT_URL}" -o "${INSTALL_SCRIPT}"
chmod 755 "${INSTALL_SCRIPT}"

# 4. Always create a systemd service
echo "Creating systemd service file at ${SERVICE_FILE} ..."
cat > "${SERVICE_FILE}" << EOF
[Unit]
Description=USB Log Monitoring and Drive Management Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${INSTALL_SCRIPT}
Restart=always

[Install]
WantedBy=multi-user.target
EOF

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling and starting usblogmon.service ..."
systemctl enable usblogmon.service
systemctl start usblogmon.service

echo "Service status (systemctl status usblogmon):"
systemctl status usblogmon.service --no-pager

echo "Done."
