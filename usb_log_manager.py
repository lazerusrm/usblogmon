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
import fnmatch
from datetime import datetime

# ============================================================
# Configuration
# ============================================================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CONFIG_FILE_PATH = "/opt/usblogmon/config.json"
GITHUB_SCRIPT_URL = "https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"

USB_SCAN_INTERVAL = 10
LOG_UPDATE_INTERVAL = 86400       # 24 hours
SCRIPT_UPDATE_INTERVAL = 86400    # once a day
MOUNTS = {
    "/var/log": (500, None),
    "/tmp": (30, "1777"),
}
JOURNALD_CONF = "/etc/systemd/journald.conf"
MAX_TMPFS_TOTAL = 530  # MB total allowed for tmpfs
SERVICE_NAME = "networkoptix-mediaserver"
SIZE_THRESHOLD = 512 * 10**9  # 512 GB
FS_TYPE = "ext4"
MOUNT_BASE = "/mnt"
NX_LOG_DIR = "/opt/networkoptix/mediaserver/var/log"

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
# Environment Detection
# ============================================================

def detect_environment():
    try:
        result = run_command(["systemd-detect-virt", "--quiet"], check=False)
        env_type = result.stdout.strip()
        return env_type if env_type else "none"
    except:
        return "none"

def should_skip_disk_management(env_type):
    container_types = {"lxc", "docker", "openvz"}
    vm_types = {"qemu", "kvm"}
    if env_type in container_types or env_type in vm_types:
        return True
    return False

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

    fstab_line = f"tmpfs   {directory}    tmpfs   defaults,noatime,size={size_mb}M"
    if directory == "/tmp" and mode:
        fstab_line += f",mode={mode}"
    fstab_line += "    0 0"

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
# Nx Witness Log Management
# ============================================================

def clean_nx_logs():
    if os.path.isdir(NX_LOG_DIR):
        for root, dirs, files in os.walk(NX_LOG_DIR):
            for f in files:
                if fnmatch.fnmatch(f, '*.zip'):
                    zip_path = os.path.join(root, f)
                    try:
                        os.remove(zip_path)
                        logging.info(f"Deleted {zip_path} to reduce log storage.")
                    except Exception as e:
                        logging.error(f"Failed to delete {zip_path}: {e}")

# ============================================================
# Deprecated NX Tmpfs Migration (One-Time Fix)
# ============================================================

def migrate_deprecated_nx_tmpfs():
    config = load_config()
    if config.get("deprecated_tmpfs_fixed"):
        # Already fixed, do nothing
        return

    # Check current time
    now = datetime.now()
    if now.hour != 2:  # Only run between 2:00AM and 3:00AM
        # Not in the allowed time window, skip for now
        return

    target_dir = "/opt/networkoptix/mediaserver/var/log"
    is_mounted_tmpfs = False
    try:
        mount_out = run_command(["mount"], check=False).stdout
        if "tmpfs on " + target_dir in mount_out:
            is_mounted_tmpfs = True
    except:
        pass

    fstab_file = "/etc/fstab"
    with open(fstab_file, 'r') as f:
        fstab_lines = f.readlines()

    new_lines = []
    fstab_modified = False
    for line in fstab_lines:
        if target_dir in line and "tmpfs" in line:
            fstab_modified = True
            continue
        new_lines.append(line)

    # Stop Nx Witness service before unmounting
    run_command(["systemctl", "stop", SERVICE_NAME], check=False)

    if is_mounted_tmpfs:
        try:
            run_command(["umount", target_dir], check=True)
            logging.info(f"Unmounted tmpfs from {target_dir}.")
        except Exception as e:
            logging.error(f"Failed to unmount {target_dir}: {e}")
            # If we fail to unmount, let's try lazy unmount as a last resort
            try:
                run_command(["umount", "-l", target_dir], check=True)
                logging.info(f"Lazy unmounted tmpfs from {target_dir}.")
            except Exception as ee:
                logging.error(f"Lazy unmount also failed for {target_dir}: {ee}")
                # If we still fail, we will retry tomorrow at 2am
                return

    if fstab_modified:
        with open(fstab_file, 'w') as f:
            f.writelines(new_lines)
        logging.info("Removed deprecated tmpfs entry for NX logs from /etc/fstab.")

    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)

    # Adjust ownership/permissions as needed
    run_command(["chown", "networkoptix:networkoptix", target_dir], check=False)
    run_command(["chmod", "755", target_dir], check=False)

    # Restart Nx Witness service after fix
    try:
        run_command(["systemctl", "start", SERVICE_NAME], check=False)
        logging.info(f"Restarted {SERVICE_NAME} after nx tmpfs migration.")
    except Exception as e:
        logging.error(f"Failed to restart {SERVICE_NAME} after nx tmpfs migration: {e}")

    config["deprecated_tmpfs_fixed"] = True
    save_config(config)

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
    try:
        run_command(["findmnt", partition], check=True)
        return True
    except subprocess.CalledProcessError:
        return False

def approximate_size(size_bytes):
    gb = size_bytes / (10**9)
    if gb >= 1000:
        tb = int(gb / 1000)
        return f"{tb}T"
    else:
        return f"{int(gb)}G"

def generate_mount_name(size_bytes, uuid):
    size_str = approximate_size(size_bytes)
    last4 = uuid[-4:] if len(uuid) >= 4 else uuid
    return f"d{size_str}-{last4}"

def run_fsck(partition):
    try:
        run_command(["e2fsck", "-fy", partition], check=True)
        logging.info(f"Filesystem check and repair completed for {partition}.")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Filesystem check (e2fsck) failed for {partition}: {e}")
        return False

def attempt_mount(partition, mount_point):
    try:
        run_command(["mount", mount_point], check=True)
        logging.info(f"Mounted {partition} at {mount_point}")
        return True
    except Exception as e:
        logging.error(f"Failed to mount {partition}: {e}")
        return False

def attempt_repair_and_remount(partition, mount_point):
    fs_type = get_partition_fs_type(partition)
    if fs_type == FS_TYPE:
        logging.info(f"Attempting to repair filesystem on {partition}...")
        if run_fsck(partition):
            if attempt_mount(partition, mount_point):
                return True
            else:
                logging.info(f"Mount still failing after repair, reformatting {partition}...")
                format_partition(partition)
                return attempt_mount(partition, mount_point)
        else:
            logging.info(f"Filesystem repair failed for {partition}, reformatting...")
            format_partition(partition)
            return attempt_mount(partition, mount_point)
    else:
        logging.info(f"Partition {partition} not {FS_TYPE}, reformatting...")
        format_partition(partition)
        return attempt_mount(partition, mount_point)

def mount_partition(partition, config, size_bytes):
    uuid = get_device_uuid(partition)
    if not uuid:
        logging.error(f"Could not determine UUID for {partition}. Skipping mount.")
        return

    if uuid in config:
        mount_point = config[uuid]
    else:
        mount_point_name = generate_mount_name(size_bytes, uuid)
        mount_point = f"{MOUNT_BASE}/{mount_point_name}"
        config[uuid] = mount_point
        save_config(config)

    os.makedirs(mount_point, exist_ok=True)

    if not is_mounted(partition):
        fstab_file = "/etc/fstab"
        with open(fstab_file, 'r') as f:
            fstab_contents = f.read()
        if f"UUID={uuid}" not in fstab_contents:
            with open(fstab_file, 'a') as f:
                f.write(f"UUID={uuid} {mount_point} {FS_TYPE} defaults,noatime 0 2\n")

        if not attempt_mount(partition, mount_point):
            attempt_repair_and_remount(partition, mount_point)

def create_partition_and_format(drive):
    try:
        run_command(["parted", "-s", drive, "mklabel", "gpt"])
        run_command(["parted", "-s", drive, "mkpart", "primary", FS_TYPE, "0%", "100%"])
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

        size = get_device_size(drive)
        if size < SIZE_THRESHOLD:
            continue

        partitions = get_partitions(drive)
        if not partitions:
            logging.info(f"{drive} has no partitions. Creating partition...")
            partition = create_partition_and_format(drive)
            if partition:
                mount_partition(partition, config, size)
        else:
            for partition in partitions:
                fs_type = get_partition_fs_type(partition)
                if fs_type != FS_TYPE:
                    logging.info(f"Reformatting {partition} from {fs_type or 'unknown'} to {FS_TYPE}...")
                    format_partition(partition)
                mount_partition(partition, config, size)

    save_config(config)

# ============================================================
# Service Management
# ============================================================

def ensure_service_running(service_name):
    try:
        run_command(["systemctl", "enable", service_name], check=False)
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status != "active":
            logging.info(f"{service_name} not running. Starting...")
            run_command(["systemctl", "start", service_name], check=False)
        # Double check
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status == "active":
            logging.info(f"{service_name} is running.")
        else:
            logging.error(f"Failed to start {service_name}. Will retry in next iteration.")
    except Exception as e:
        logging.error(f"Failed to ensure {service_name} is running: {e}")

# ============================================================
# Main Execution Loop
# ============================================================

def main():
    last_log_update = time.time() - LOG_UPDATE_INTERVAL
    last_script_update = time.time() - SCRIPT_UPDATE_INTERVAL

    env_type = detect_environment()
    skip_disks = should_skip_disk_management(env_type)
    if skip_disks:
        logging.info(f"Detected environment type: {env_type}. Skipping disk management.")
    else:
        logging.info(f"Environment type: {env_type}. Proceeding with disk management.")

    # One-time migration for deprecated nx tmpfs setup (only at 2:00AM - 3:00AM)
    migrate_deprecated_nx_tmpfs()

    # Initial setup tasks
    configure_journald_volatile()
    setup_tmpfs_mounts()
    ensure_service_running(SERVICE_NAME)

    while True:
        current_time = time.time()

        if not skip_disks:
            manage_drives()

        ensure_service_running(SERVICE_NAME)
        clean_nx_logs()

        if current_time - last_script_update >= SCRIPT_UPDATE_INTERVAL:
            update_script()
            last_script_update = current_time

        time.sleep(USB_SCAN_INTERVAL)

if __name__ == "__main__":
    main()
