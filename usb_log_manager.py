#!/usr/bin/env python3

import os
import subprocess
import time
import requests
import hashlib
import sys
import json
import pyudev
import logging
import fnmatch
import shutil
from datetime import datetime

# ============================================================================
# Configuration
# ============================================================================
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

CONFIG_FILE_PATH = "/opt/usblogmon/config.json"
GITHUB_SCRIPT_URL = "https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"

# Intervals
USB_SCAN_INTERVAL = 10            # main loop sleep interval
SCRIPT_UPDATE_INTERVAL = 86400    # once a day
FLUSH_INTERVAL = 12 * 3600        # 12 hours, for "twice-a-day" flush
LOG_UPDATE_INTERVAL = 86400       # (if you still want a 24hr-based Nx log cleanup timing)

# OverlayFS & Overflow Settings
TMP_RAM_SIZE = "100M"  # 100MB upper layer for /tmp
LOG_RAM_SIZE = "500M"  # 500MB upper layer for /var/log (example)
FILE_SIZE_LIMIT = 100 * 1024 * 1024  # 100MB threshold for immediate large file move

# Where to look for external drives (lower layer)
EXTERNAL_MOUNT_CHECK = ["/mnt", "/media"]
MOUNT_BASE = "/mnt"

# Nx Witness and journald
SERVICE_NAME = "networkoptix-mediaserver"
NX_LOG_DIR = "/opt/networkoptix/mediaserver/var/log"
JOURNALD_CONF = "/etc/systemd/journald.conf"

# One-time Nx tmpfs migration fix runs at 2:00AM
MIGRATION_HOUR = 2

# Large Drive Management
SIZE_THRESHOLD = 512 * 10**9  # 512 GB
FS_TYPE = "ext4"

# Combined limit for pure tmpfs fallback if needed
MAX_TMPFS_TOTAL = 530  # MB total for purely tmpfs if fallback

# ============================================================================
# Utility Functions
# ============================================================================
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

# ============================================================================
# Environment Detection
# ============================================================================
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

# ============================================================================
# Self-update Mechanism
# ============================================================================
def update_script():
    logging.info("Checking for script updates...")
    try:
        response = requests.get(GITHUB_SCRIPT_URL, timeout=10)
        response.raise_for_status()
        new_script_content = response.content
        new_script_hash = hashlib.sha256(new_script_content).hexdigest()

        if new_script_hash != get_script_hash():
            logging.info("New version found. Updating script...")
            with open(__file__, 'wb') as file:
                file.write(new_script_content)
            logging.info("Script updated. Restarting...")
            os.execv(__file__, sys.argv)
        else:
            logging.info("Already running the latest version.")
    except requests.RequestException as e:
        logging.error(f"Error checking for updates: {e}")

# ============================================================================
# Journald Configuration (Volatile)
# ============================================================================
def configure_journald_volatile():
    """
    Forces journald to store logs only in memory (volatile),
    disabling compression and sealing for minimal disk usage.
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

# ============================================================================
# OverlayFS Setup & Management
# ============================================================================
def find_external_drive():
    """
    Search for an already-mounted external drive in EXTERNAL_MOUNT_CHECK.
    Returns the first valid path or None if not found.
    """
    for base in EXTERNAL_MOUNT_CHECK:
        if os.path.isdir(base):
            for item in os.listdir(base):
                path = os.path.join(base, item)
                if os.path.ismount(path):
                    return path
    return None

def setup_overlayfs(directory, ram_size, fallback_tmpfs_size_mb=None, mode="755"):
    """
    Sets up an OverlayFS that uses a tmpfs 'upper' layer + an external drive
    'lower' layer. If no external drive is found, optionally fallback to direct
    tmpfs (from the original script logic).
    """
    external_dir = find_external_drive()
    # The 'ram_disk' is our upper layer
    ram_disk = f"/mnt/ramdisk_{os.path.basename(directory)}"
    # We'll store the overlay "work" dir here
    work_dir = f"{ram_disk}_work"

    # If external drive is found, we create an 'overflow_dir' on that drive
    overflow_dir = None
    if external_dir:
        overflow_dir = os.path.join(external_dir, f"overlay_{os.path.basename(directory)}")
        os.makedirs(overflow_dir, exist_ok=True)

    try:
        # 1. Ensure the final directory (the mountpoint) exists
        os.makedirs(directory, exist_ok=True)
        os.makedirs(ram_disk, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)

        # 2. Mount a tmpfs on ram_disk to hold the upper layer
        run_command(["mount", "-t", "tmpfs", "-o", f"size={ram_size}", "tmpfs", ram_disk])
        logging.info(f"Mounted tmpfs at {ram_disk} with size={ram_size} for {directory}")

        # 3. If we have an external drive, set up OverlayFS
        if overflow_dir:
            run_command([
                "mount", "-t", "overlay",
                "overlay",
                "-o", f"lowerdir={overflow_dir},upperdir={ram_disk},workdir={work_dir}",
                directory
            ])
            logging.info(f"Mounted OverlayFS for {directory}: upper={ram_disk}, lower={overflow_dir}")
        else:
            # If no external drive found, fallback to *pure tmpfs*.
            run_command(["umount", ram_disk], check=False)
            try:
                os.rmdir(ram_disk)
                os.rmdir(work_dir)
            except:
                pass
            if fallback_tmpfs_size_mb:
                fstab_line = f"tmpfs   {directory}    tmpfs   defaults,noatime,size={fallback_tmpfs_size_mb}M 0 0"
                with open("/etc/fstab", 'r') as f:
                    fstab_contents = f.read()
                if f" {directory} " not in fstab_contents:
                    with open("/etc/fstab", 'a') as f:
                        f.write(fstab_line + "\n")
                run_command(["mount", directory], check=False)
                logging.info(f"No external drive found. Using direct tmpfs {fallback_tmpfs_size_mb} MB for {directory}.")
            else:
                logging.info(f"No external drive found. Using direct tmpfs approach for {directory}, no fallback size given.")

        # 4. Set permissions
        if mode:
            os.chmod(directory, int(mode, 8))

    except Exception as e:
        logging.error(f"Failed to set up OverlayFS for {directory}: {e}")

def manage_file_overflow(directory):
    """
    Periodically checks files in `directory` that exceed FILE_SIZE_LIMIT
    and moves them to the external drive's lowerdir so they don't
    consume RAM. This is for immediate "big file" overflow.
    """
    external_dir = find_external_drive()
    if not external_dir:
        return

    overflow_dir = os.path.join(external_dir, f"overlay_{os.path.basename(directory)}")
    if not os.path.exists(overflow_dir):
        return

    for root, dirs, files in os.walk(directory):
        for file in files:
            file_path = os.path.join(root, file)
            try:
                size = os.path.getsize(file_path)
                if size > FILE_SIZE_LIMIT:
                    relative_path = os.path.relpath(file_path, directory)
                    dest_path = os.path.join(overflow_dir, relative_path)
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    shutil.move(file_path, dest_path)
                    logging.info(f"Moved large file {file_path} ({size} bytes) to {dest_path}")
            except Exception as e:
                logging.error(f"Error moving {file_path}: {e}")

def flush_overlay(directory):
    """
    Twice-a-day job: Moves *all* files from the upper RAM layer to the external
    (lower) directory, effectively syncing everything to disk. This helps ensure
    we’re not storing too many logs in RAM, even if they're below the size limit.
    """
    external_dir = find_external_drive()
    if not external_dir:
        logging.warning(f"No external drive found; cannot flush overlay for {directory}.")
        return
    overflow_dir = os.path.join(external_dir, f"overlay_{os.path.basename(directory)}")
    if not os.path.exists(overflow_dir):
        logging.warning(f"Overlay lower dir {overflow_dir} does not exist; cannot flush.")
        return

    # Check free space first
    if not check_free_space(overflow_dir):
        logging.warning(f"External drive might be low on space; continuing flush with caution...")

    # Upper layer path
    upper_dir = f"/mnt/ramdisk_{os.path.basename(directory)}"
    if not os.path.exists(upper_dir):
        logging.info(f"No upper directory found for {directory}. Maybe pure tmpfs fallback or not mounted.")
        return

    # Move everything from upper to lower
    for root, dirs, files in os.walk(upper_dir):
        for file in files:
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, upper_dir)
            dest = os.path.join(overflow_dir, rel_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            try:
                shutil.move(file_path, dest)
                logging.info(f"Flushed {file_path} to {dest}")
            except Exception as e:
                logging.error(f"Failed to flush {file_path}: {e}")

def check_free_space(path, min_percent=10):
    """
    Check if `path` has at least `min_percent` free space.
    Returns True if enough space, False if below threshold.
    """
    try:
        st = os.statvfs(path)
        # total available to non-superuser is f_bavail
        free_bytes = st.f_bavail * st.f_frsize
        total_bytes = st.f_blocks * st.f_frsize
        percent_free = (free_bytes / total_bytes) * 100 if total_bytes > 0 else 100
        logging.info(f"Free space at {path}: {percent_free:.2f}%")
        return percent_free >= min_percent
    except Exception as e:
        logging.error(f"Failed to check free space on {path}: {e}")
        # If we can’t check, assume we can proceed
        return True

# ============================================================================
# Nx Witness Log Management
# ============================================================================
def clean_nx_logs():
    """
    Removes old Nx Witness log archives (like *.zip) in NX_LOG_DIR.
    """
    if os.path.isdir(NX_LOG_DIR):
        for root, dirs, files in os.walk(NX_LOG_DIR):
            for f in files:
                if fnmatch.fnmatch(f, '*.zip'):
                    zip_path = os.path.join(root, f)
                    try:
                        os.remove(zip_path)
                        logging.info(f"Deleted Nx log archive {zip_path} to reduce storage.")
                    except Exception as e:
                        logging.error(f"Failed to delete {zip_path}: {e}")

# ============================================================================
# Drive Management (from original script)
# ============================================================================
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
        logging.info(f"Filesystem check/repair completed for {partition}")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"Filesystem check failed for {partition}: {e}")
        return False

def attempt_mount(partition, mount_point):
    try:
        run_command(["mount", mount_point], check=True)
        logging.info(f"Mounted {partition} at {mount_point}")
        return True
    except Exception as e:
        logging.error(f"Failed to mount {partition}: {e}")
        return False

def format_partition(partition):
    try:
        run_command(["mkfs.ext4", "-F", partition], check=True)
        logging.info(f"Formatted {partition} as ext4.")
    except Exception as e:
        logging.error(f"Failed to format {partition}: {e}")

def attempt_repair_and_remount(partition, mount_point):
    fs_type = get_partition_fs_type(partition)
    if fs_type == FS_TYPE:
        logging.info(f"Attempting fs repair on {partition}...")
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
        logging.error(f"Could not determine UUID for {partition}. Skipping.")
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
        # Update /etc/fstab if needed
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

# ============================================================================
# Service Management
# ============================================================================
def ensure_service_running(service_name):
    try:
        run_command(["systemctl", "enable", service_name], check=False)
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status != "active":
            logging.info(f"{service_name} not running. Starting...")
            run_command(["systemctl", "start", service_name], check=False)
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status == "active":
            logging.info(f"{service_name} is running.")
        else:
            logging.error(f"Failed to start {service_name}. Will retry next iteration.")
    except Exception as e:
        logging.error(f"Failed to ensure {service_name} is running: {e}")

# ============================================================================
# Main Execution Loop
# ============================================================================
def main():
    env_type = detect_environment()
    skip_disks = should_skip_disk_management(env_type)
    if skip_disks:
        logging.info(f"Detected env {env_type}. Skipping disk management.")
    else:
        logging.info(f"Env type: {env_type}. Will manage disks normally.")

    # Journald ephemeral config
    configure_journald_volatile()

    # Overlay for /var/log and /tmp
    setup_overlayfs("/var/log", LOG_RAM_SIZE, fallback_tmpfs_size_mb=500, mode="755")
    setup_overlayfs("/tmp", TMP_RAM_SIZE, fallback_tmpfs_size_mb=100, mode="1777")

    # Nx Witness
    ensure_service_running(SERVICE_NAME)

    last_script_update = time.time() - SCRIPT_UPDATE_INTERVAL
    last_flush = time.time() - FLUSH_INTERVAL  # so it flushes ASAP if needed

    while True:
        current_time = time.time()

        # Manage large drives (only if not in container)
        if not skip_disks:
            manage_drives()

        ensure_service_running(SERVICE_NAME)
        clean_nx_logs()

        # Manage immediate large-file overflow
        manage_file_overflow("/var/log")
        manage_file_overflow("/tmp")

        # Twice-a-day flush to external disk
        if (current_time - last_flush) >= FLUSH_INTERVAL:
            logging.info("Performing scheduled overlay flush...")
            flush_overlay("/var/log")
            flush_overlay("/tmp")
            last_flush = current_time

        # Self-update
        if current_time - last_script_update >= SCRIPT_UPDATE_INTERVAL:
            update_script()
            last_script_update = current_time

        time.sleep(USB_SCAN_INTERVAL)

if __name__ == "__main__":
    main()
