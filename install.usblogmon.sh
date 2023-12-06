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

# Check for Python3 and install if not exists
if ! command -v python3 &> /dev/null; then
    echo "Python3 is not installed. Installing Python3..."
    apt-get update
    apt-get install python3 -y
fi

# Check for pip and install if not exists
if ! command -v pip3 &> /dev/null; then
    echo "pip3 is not installed. Installing pip3..."
    apt-get install python3-pip -y
fi

# Install pyudev if not exists
if ! python3 -c "import pyudev" &> /dev/null; then
    echo "pyudev is not installed. Installing pyudev..."
    pip3 install pyudev
fi

# Install requests if not exists
if ! python3 -c "import requests" &> /dev/null; then
    echo "requests is not installed. Installing requests..."
    pip3 install requests
fi

# Create installation directory
echo "Creating installation directory at $INSTALL_DIR"
mkdir -p $INSTALL_DIR
chown $USER:$USER $INSTALL_DIR

# Download the script from GitHub
echo "Downloading the script from $REPO_URL"
curl -o $INSTALL_DIR/$SCRIPT_NAME "$REPO_URL/$SCRIPT_NAME"

# Make the script executable
chmod +x $INSTALL_DIR/$SCRIPT_NAME

# Create systemd service file
echo "Creating systemd service file at $SERVICE_FILE"
cat << EOF | tee $SERVICE_FILE
[Unit]
Description=USB Log Monitoring Service

[Service]
ExecStart=/usr/bin/python3 $INSTALL_DIR/$SCRIPT_NAME
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# Reload systemd to recognize new service
systemctl daemon-reload

# Enable and start the new service
systemctl enable usblogmon
systemctl start usblogmon

echo "Installation completed. The service is now running."
