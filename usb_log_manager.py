#!/usr/bin/env python3

import os
import subprocess
import time
import requests
import hashlib
import sys
import re
import json
import pyudev
import logging

# ============================================================
# Configuration
# ============================================================

# Logging setup: minimal logging to stdout to minimize writes
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CONFIG_FILE_PATH = "/opt/usblogmon/config.json"   # Persistent config storage (adjust as needed)
GITHUB_SCRIPT_URL = "https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"
USB_SCAN_INTERVAL = 180           # Interval for rescanning USB drives (seconds)
LOG_UPDATE_INTERVAL = 86400       # Interval for checking updates (24 hours)
MOUNTS = {
    # Directory : (size_in_MB, mode if needed)
    "/var/log": (15, None),
    "/opt/networkoptix/mediaserver/var/log": (5, None),
    "/tmp": (30, "1777"),
}

JOURNALD_CONF = "/etc/systemd/journald.conf"
MAX_TMPFS_TOTAL = 50  # MB total allowed across tmpfs mounts

SERVICE_NAME = "networkoptix-mediaserver"

# ============================================================
# Utility Functions
# ============================================================

def run_command(cmd, check=True, capture_output=True):
    """
    Run a shell command.
    """
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True)

def load_config():
    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            content = f.read().strip()
            return json.loads(content) if content else {}
    except (FileNotFoundError, json.JSONDecodeError):
        # Create a new empty config if missing or invalid
        save_config({})
        return {}

def save_config(config):
    with open(CONFIG_FILE_PATH, 'w') as f:
        json.dump(config, f, indent=4)

def get_script_hash():
    with open(__file__, 'rb') as file:
        data = file.read()
    return hashlib.sha256(data).hexdigest()

# ============================================================
# Self-update Mechanism
# ============================================================

def update_script():
    """
    Check if a newer version of the script is available. If yes, update and restart.
    """
    logging.info("Checking for script updates...")
    try:
        response = requests.get(GITHUB_SCRIPT_URL, timeout=10)
        response.raise_for_status()
        new_script_content = response.content
        new_script_hash = hashlib.sha256(new_script_content).hexdigest()

        if new_script_hash != get_script_hash():
            logging.info("New version of the script found. Updating...")
            with open(__file__, 'wb') as file:
                file.write(new_script_content)
            logging.info("Script updated. Restarting...")
            os.execv(__file__, sys.argv)
        else:
            logging.info("Already running the latest version.")
    except requests.RequestException as e:
        logging.error(f"Error while checking for updates: {e}")

# ============================================================
# Journald Configuration for Volatile Storage
# ============================================================

def configure_journald_volatile():
    """
    Configure systemd-journald to use volatile storage and disable compression/sealing.
    """
    try:
        if os.path.exists(JOURNALD_CONF):
            with open(JOURNALD_CONF, 'r') as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                if line.strip().startswith("Storage="):
                    new_lines.append("Storage=volatile\n")
                elif line.strip().startswith("Compress="):
                    new_lines.append("Compress=no\n")
                elif line.strip().startswith("Seal="):
                    new_lines.append("Seal=no\n")
                else:
                    new_lines.append(line)
            # Ensure settings exist if not found
            if not any(l.strip().startswith("Storage=") for l in new_lines):
                new_lines.append("Storage=volatile\n")
            if not any(l.strip().startswith("Compress=") for l in new_lines):
                new_lines.append("Compress=no\n")
            if not any(l.strip().startswith("Seal=") for l in new_lines):
                new_lines.append("Seal=no\n")

            with open(JOURNALD_CONF, 'w') as f:
                f.writelines(new_lines)
        else:
            with open(JOURNALD_CONF, 'w') as f:
                f.write("[Journal]\nStorage=volatile\nCompress=no\nSeal=no\n")

        # Restart journald to apply changes
        run_command(["systemctl", "restart", "systemd-journald"])
        logging.info("systemd-journald configured for volatile storage and restarted.")
    except Exception as e:
        logging.error(f"Failed to configure journald: {e}")

# ============================================================
# Tmpfs Mount Management
# ============================================================

def is_tmpfs_mounted(directory):
    try:
        out = run_command(["mount"], check=False).stdout
        return f" on {directory} type tmpfs" in out
    except:
        return False

def configure_tmpfs(directory, size_mb, mode=None):
    """
    Configure a directory as tmpfs. Add to /etc/fstab if not present and mount it.
    """
    # Ensure directory exists
    if os.path.exists(directory):
        # Remove old contents if needed
        try:
            for root, dirs, files in os.walk(directory, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
        except Exception:
            pass
    else:
        os.makedirs(directory, exist_ok=True)

    if mode:
        os.chmod(directory, int(mode, 8))

    fstab_line = f"tmpfs   {directory}    tmpfs   defaults,noatime"
    if directory == "/tmp":
        fstab_line += f",mode={mode},size={size_mb}M    0 0"
    else:
        fstab_line += f",size={size_mb}M    0 0"

    # Add to /etc/fstab if not already present
    fstab_file = "/etc/fstab"
    with open(fstab_file, 'r') as f:
        fstab_contents = f.read()

    if f" {directory} " not in fstab_contents:
        with open(fstab_file, 'a') as f:
            f.write(fstab_line + "\n")
        logging.info(f"Added {directory} to /etc/fstab.")

    # Mount if not mounted
    if not is_tmpfs_mounted(directory):
        run_command(["mount", directory], check=True)
        logging.info(f"Mounted {directory} as tmpfs with size={size_mb}M.")

def check_total_tmpfs():
    total = sum(size for _, (size, _) in MOUNTS.items())
    if total > MAX_TMPFS_TOTAL:
        raise ValueError(f"Total tmpfs allocation {total}MB exceeds the limit {MAX_TMPFS_TOTAL}MB.")
    else:
        logging.info(f"Total tmpfs allocation {total}MB is within the {MAX_TMPFS_TOTAL}MB limit.")

def setup_tmpfs_mounts():
    check_total_tmpfs()
    for directory, (size_mb, mode) in MOUNTS.items():
        configure_tmpfs(directory, size_mb, mode)

# ============================================================
# Drive Management
# ============================================================

def is_boot_drive(drive):
    try:
        mount_info = run_command(['findmnt', '-n', '-o', 'SOURCE', '/']).stdout.strip()
        return drive in mount_info
    except:
        return False

def read_fstab():
    fstab_entries = {}
    with open('/etc/fstab', 'r') as file:
        for line in file:
            if line.startswith('#') or not line.strip():
                continue
            parts = re.split(r'\s+', line.strip())
            if len(parts) >= 2:
                fstab_entries[parts[0]] = parts[1]
    return fstab_entries

def check_and_mount_fstab_drives():
    fstab_entries = read_fstab()
    mounted = run_command(["mount"]).stdout

    for device, mount_point in fstab_entries.items():
        if device in ["/tmp", "/var/log", "/opt/networkoptix/mediaserver/var/log"]:
            # Skip tmpfs or already handled special mounts
            continue
        if device not in mounted:
            mount_drive(device, mount_point)  # Attempt mount

def detect_usb_drives():
    context = pyudev.Context()
    usb_drives = []
    for device in context.list_devices(subsystem='block', DEVTYPE='disk'):
        if device.get('ID_BUS') == 'usb':
            usb_drives.append(device.device_node)
    return usb_drives

def detect_other_drives():
    context = pyudev.Context()
    drives = []
    for d in context.list_devices(subsystem='block', DEVTYPE='disk'):
        if not is_boot_drive(d.device_node):
            drives.append(d.device_node)
    return drives

def get_partitions(drive):
    try:
        result = run_command(["lsblk", "-l", "-n", "-o", "NAME", drive])
        output = result.stdout.strip().split('\n')
    except subprocess.CalledProcessError:
        return []

    partitions = []
    drive_name = os.path.basename(drive)
    for line in output:
        partition = line.strip()
        if partition != drive_name:
            partition_path = "/dev/" + partition
            try:
                size = int(run_command(["blockdev", "--getsize64", partition_path]).stdout.strip())
                # For example, only mount if size >= 100GB (arbitrary logic retained from original)
                if size >= 100 * 10**9:
                    partitions.append(partition_path)
            except:
                continue
    return partitions

def is_mounted(partition):
    try:
        run_command(["findmnt", partition], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False

def get_device_uuid(partition):
    # Attempt to get a stable UUID to identify the device
    try:
        uuid = run_command(["blkid", "-s", "UUID", "-o", "value", partition], check=True).stdout.strip()
        return uuid
    except:
        # If UUID not found, fallback to using partition as unique ID
        return partition

def mount_drive(partition, mount_point):
    if is_mounted(partition):
        logging.info(f"{partition} is already mounted.")
        return
    os.makedirs(mount_point, exist_ok=True)
    try:
        run_command(["mount", partition, mount_point], check=True)
        logging.info(f"Mounted {partition} at {mount_point}")
    except Exception as e:
        logging.error(f"Failed to mount {partition}: {e}")

def manage_drives():
    config = load_config()

    # Detect drives
    usb_drives = detect_usb_drives()
    other_drives = detect_other_drives()

    for drive in usb_drives + other_drives:
        partitions = get_partitions(drive)
        for partition in partitions:
            dev_uuid = get_device_uuid(partition)
            if dev_uuid in config:
                # Use previously known mount point
                mount_point = config[dev_uuid]
            else:
                # Generate a friendly name
                friendly_name = f"drive_{partition.split('/')[-1]}"
                mount_point = f"/mnt/{friendly_name}"
                config[dev_uuid] = mount_point

            if not is_mounted(partition):
                mount_drive(partition, mount_point)

    save_config(config)

# ============================================================
# Service Management
# ============================================================

def ensure_service_running(service_name):
    try:
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status != "active":
            logging.info(f"{service_name} is not running. Starting...")
            run_command(["systemctl", "start", service_name], check=True)
            logging.info(f"{service_name} started successfully.")
        else:
            logging.info(f"{service_name} is running.")
    except Exception as e:
        logging.error(f"Failed to ensure {service_name} is running: {e}")

# ============================================================
# Main Execution Loop
# ============================================================

def main():
    last_log_update = time.time() - LOG_UPDATE_INTERVAL
    last_script_update = time.time()

    # Initial setup tasks
    configure_journald_volatile()
    setup_tmpfs_mounts()
    check_and_mount_fstab_drives()

    while True:
        current_time = time.time()

        # Manage USB and other drives
        manage_drives()

        # Ensure networkoptix-mediaserver is running
        ensure_service_running(SERVICE_NAME)

        # Check for script updates every 24 hours
        if current_time - last_script_update >= LOG_UPDATE_INTERVAL:
            update_script()
            last_script_update = current_time

        # Sleep until next scan
        time.sleep(USB_SCAN_INTERVAL)


if __name__ == "__main__":
    main()
