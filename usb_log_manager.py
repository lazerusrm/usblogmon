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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CONFIG_FILE_PATH = "/opt/usblogmon/config.json"   
GITHUB_SCRIPT_URL = "https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"
USB_SCAN_INTERVAL = 180           # Interval for rescanning USB drives (seconds)
LOG_UPDATE_INTERVAL = 86400       # 24 hours
MOUNTS = {
    "/var/log": (15, None),
    "/opt/networkoptix/mediaserver/var/log": (5, None),
    "/tmp": (30, "1777"),
}
JOURNALD_CONF = "/etc/systemd/journald.conf"
MAX_TMPFS_TOTAL = 50  # MB total allowed for tmpfs
SERVICE_NAME = "networkoptix-mediaserver"
SIZE_THRESHOLD = 512 * 10**9  # 512 GB in bytes
FS_TYPE = "ext4"
MOUNT_BASE = "/mnt"

# ============================================================
# Utility Functions
# ============================================================

def run_command(cmd, check=True, capture_output=True):
    return subprocess.run(cmd, check=check, capture_output=capture_output, text=True)

def load_config():
    try:
        with open(CONFIG_FILE_PATH, 'r') as f:
            content = f.read().strip()
            return json.loads(content) if content else {}
    except (FileNotFoundError, json.JSONDecodeError):
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
# Journald Configuration (Volatile)
# ============================================================

def configure_journald_volatile():
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
            # Ensure settings exist
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

        run_command(["systemctl", "restart", "systemd-journald"])
        logging.info("systemd-journald configured for volatile storage and restarted.")
    except Exception as e:
        logging.error(f"Failed to configure journald: {e}")

# ============================================================
# Tmpfs Mounts
# ============================================================

def is_tmpfs_mounted(directory):
    try:
        out = run_command(["mount"], check=False).stdout
        return f" on {directory} type tmpfs" in out
    except:
        return False

def configure_tmpfs(directory, size_mb, mode=None):
    # Clean directory
    if os.path.exists(directory):
        for root, dirs, files in os.walk(directory, topdown=False):
            for name in files:
                os.remove(os.path.join(root, name))
            for name in dirs:
                os.rmdir(os.path.join(root, name))
    else:
        os.makedirs(directory, exist_ok=True)

    if mode:
        os.chmod(directory, int(mode, 8))

    fstab_line = f"tmpfs   {directory}    tmpfs   defaults,noatime"
    if directory == "/tmp":
        fstab_line += f",mode={mode},size={size_mb}M    0 0"
    else:
        fstab_line += f",size={size_mb}M    0 0"

    fstab_file = "/etc/fstab"
    with open(fstab_file, 'r') as f:
        fstab_contents = f.read()

    if f" {directory} " not in fstab_contents:
        with open(fstab_file, 'a') as f:
            f.write(fstab_line + "\n")
        logging.info(f"Added {directory} to /etc/fstab.")

    if not is_tmpfs_mounted(directory):
        run_command(["mount", directory], check=True)
        logging.info(f"Mounted {directory} as tmpfs with size={size_mb}M.")

def check_total_tmpfs():
    total = sum(size for _, (size, _) in MOUNTS.items())
    if total > MAX_TMPFS_TOTAL:
        raise ValueError(f"Total tmpfs allocation {total}MB exceeds {MAX_TMPFS_TOTAL}MB.")
    else:
        logging.info(f"Total tmpfs allocation {total}MB is within limit.")

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

def get_device_size(drive):
    try:
        size_str = run_command(["blockdev", "--getsize64", drive], check=True).stdout.strip()
        return int(size_str)
    except:
        return 0

def list_block_devices():
    context = pyudev.Context()
    disks = []
    for device in context.list_devices(subsystem='block', DEVTYPE='disk'):
        disks.append(device.device_node)
    return disks

def get_partitions(drive):
    try:
        result = run_command(["lsblk", "-l", "-n", "-o", "NAME", drive])
        output = result.stdout.strip().split('\n')
    except:
        return []

    partitions = []
    drive_name = os.path.basename(drive)
    for line in output:
        partition = line.strip()
        if partition and partition != drive_name:
            partitions.append("/dev/" + partition)
    return partitions

def get_partition_fs_type(partition):
    try:
        blkid_out = run_command(["blkid", "-o", "value", "-s", "TYPE", partition], check=False)
        fs_type = blkid_out.stdout.strip()
        return fs_type if fs_type else None
    except:
        return None

def get_device_uuid(partition):
    try:
        uuid = run_command(["blkid", "-o", "value", "-s", "UUID", partition], check=True).stdout.strip()
        return uuid
    except:
        return None

def is_mounted(partition):
    # Just call run_command without redirecting stdout/stderr since we capture anyway
    try:
        run_command(["findmnt", partition], check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def mount_partition(partition, config):
    uuid = get_device_uuid(partition)
    if not uuid:
        logging.error(f"Could not determine UUID for {partition}. Skipping mount.")
        return

    if uuid in config:
        mount_point = config[uuid]
    else:
        mount_point = f"{MOUNT_BASE}/drive_{uuid}"
        config[uuid] = mount_point
        save_config(config)

    os.makedirs(mount_point, exist_ok=True)

    if not is_mounted(partition):
        # Ensure /etc/fstab has entry for this uuid
        fstab_file = "/etc/fstab"
        with open(fstab_file, 'r') as f:
            fstab_contents = f.read()
        if f"UUID={uuid}" not in fstab_contents:
            # Add a persistent fstab entry
            with open(fstab_file, 'a') as f:
                f.write(f"UUID={uuid} {mount_point} {FS_TYPE} defaults,noatime 0 2\n")

        try:
            run_command(["mount", mount_point], check=True)
            logging.info(f"Mounted {partition} ({uuid}) at {mount_point}")
        except Exception as e:
            logging.error(f"Failed to mount {partition}: {e}")

def create_partition_and_format(drive):
    try:
        run_command(["parted", "-s", drive, "mklabel", "gpt"])
        run_command(["parted", "-s", drive, "mkpart", "primary", FS_TYPE, "0%", "100%"])
        # After creation, wait for partition to appear
        time.sleep(2)
        partitions = get_partitions(drive)
        if partitions:
            partition = partitions[0]
            format_partition(partition)
            return partition
        else:
            logging.error(f"No partition found after creating partition on {drive}")
            return None
    except Exception as e:
        logging.error(f"Failed to create partition on {drive}: {e}")
        return None

def format_partition(partition):
    try:
        run_command(["mkfs.ext4", "-F", partition], check=True)
        logging.info(f"Formatted {partition} as ext4.")
    except Exception as e:
        logging.error(f"Failed to format {partition}: {e}")

def manage_drives():
    config = load_config()
    all_drives = list_block_devices()

    for drive in all_drives:
        if is_boot_drive(drive):
            continue

        # Check drive size
        size = get_device_size(drive)
        if size < SIZE_THRESHOLD:
            # Not large enough for video storage, skip
            continue

        partitions = get_partitions(drive)
        if not partitions:
            # No partitions, create one
            logging.info(f"{drive} has no partitions. Creating partition...")
            partition = create_partition_and_format(drive)
            if partition:
                mount_partition(partition, config)
        else:
            # For simplicity, handle all partitions on the drive.
            for partition in partitions:
                fs_type = get_partition_fs_type(partition)
                if fs_type != FS_TYPE:
                    # Reformat partition to ext4
                    logging.info(f"Reformatting {partition} from {fs_type or 'unknown'} to {FS_TYPE}...")
                    format_partition(partition)
                mount_partition(partition, config)

    save_config(config)

# ============================================================
# Service Management
# ============================================================

def ensure_service_running(service_name):
    try:
        # Enable service if not enabled
        run_command(["systemctl", "enable", service_name], check=False)
        # Start service if not running
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status != "active":
            logging.info(f"{service_name} not running. Starting...")
            run_command(["systemctl", "start", service_name], check=False)
        # Double check
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status == "active":
            logging.info(f"{service_name} is running.")
        else:
            logging.error(f"Failed to start {service_name}.")
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

    # Ensure media server is enabled and running
    ensure_service_running(SERVICE_NAME)

    while True:
        current_time = time.time()

        # Manage large drives
        manage_drives()

        # Ensure Nx Witness service still running
        ensure_service_running(SERVICE_NAME)

        # Check for script updates every 24 hours
        if current_time - last_script_update >= LOG_UPDATE_INTERVAL:
            update_script()
            last_script_update = current_time

        time.sleep(USB_SCAN_INTERVAL)

if __name__ == "__main__":
    main()
