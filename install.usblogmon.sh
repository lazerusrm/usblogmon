#!/bin/bash

# Check if the script is running as root
if [ "$(id -u)" != "0" ]; then
    echo "This script must be run as root. Trying to run with sudo..."
    sudo "$0" "$@"
    exit $?
fi

# Define variables
REPO_URL="https://raw.githubusercontent.com/lazerusrm/usblogmon/main"
SCRIPT_NAME="usb_log_manager.py"
INSTALL_DIR="/opt/usblogmon"
SERVICE_FILE="/etc/systemd/system/usblogmon.service"

echo "Updating package list..."
apt-get update -y

# Ensure necessary system packages are installed
DEPS=("python3" "python3-pip" "parted" "e2fsprogs" "curl")
for pkg in "${DEPS[@]}"; do
    if ! dpkg -l | grep -q "^ii\s\+$pkg\s"; then
        echo "Installing $pkg..."
        apt-get install -y $pkg
    fi
done

# Install required Python packages if not present
REQ_PY_MODULES=("pyudev" "requests")
for mod in "${REQ_PY_MODULES[@]}"; do
    if ! python3 -c "import $mod" &> /dev/null; then
        echo "Installing Python module: $mod"
        pip3 install "$mod"
    fi
done

# Create installation directory
echo "Creating installation directory at $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
chown "$USER:$USER" "$INSTALL_DIR"

# Download the updated script from GitHub
echo "Downloading the script from $REPO_URL"
curl -sSf -o "$INSTALL_DIR/$SCRIPT_NAME" "$REPO_URL/$SCRIPT_NAME"

# Make the script executable
chmod +x "$INSTALL_DIR/$SCRIPT_NAME"

# Create systemd service file
echo "Creating systemd service file at $SERVICE_FILE"
cat << EOF | tee $SERVICE_FILE
[Unit]
Description=USB Log Monitoring and Drive Management Service
Wants=network-online.target
After=network.target network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $INSTALL_DIR/$SCRIPT_NAME
Restart=always
RestartSec=10
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd to recognize new service
systemctl daemon-reload

# Enable and start the new service
systemctl enable usblogmon
systemctl start usblogmon

echo "Installation completed. The 'usblogmon' service is now running."
echo "The usb_log_manager.py script will now handle drive management, journald configuration, service checks, and self-updates automatically."
