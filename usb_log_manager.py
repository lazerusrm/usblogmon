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
LOG_UPDATE_INTERVAL = 86400       # daily Nx log cleanup, if you want to time-limit it

# OverlayFS & Overflow Settings
TMP_RAM_SIZE = "100M"  # 100MB upper layer for /tmp
LOG_RAM_SIZE = "500M"  # 500MB upper layer for /var/log
FILE_SIZE_LIMIT = 100 * 1024 * 1024  # 100MB threshold for immediate big-file move

# External drive search
EXTERNAL_MOUNT_CHECK = ["/mnt", "/media"]
MOUNT_BASE = "/mnt"

# Nx Witness
SERVICE_NAME = "networkoptix-mediaserver"
NX_LOG_DIR = "/opt/networkoptix/mediaserver/var/log"

# Journald
JOURNALD_CONF = "/etc/systemd/journald.conf"

# Large Drive Management
SIZE_THRESHOLD = 512 * 10**9  # 512 GB
FS_TYPE = "ext4"

# Combined limit for pure tmpfs fallback if needed
MAX_TMPFS_TOTAL = 530  # MB total for purely tmpfs if fallback

# Additional log archive patterns we want to remove to reduce disk writes
ARCHIVE_PATTERNS = [
    "*.zip",
    "*.gz",
    "*.bz2",
    "*.xz",
    "*.1",
    "*.old",
    "*.[0-9]",  # e.g. .1, .2, ...
]

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
    """
    Returns a string like "none", "lxc", "docker", "kvm", etc.
    If systemd-detect-virt fails, returns 'none'.
    """
    try:
        result = run_command(["systemd-detect-virt", "--quiet"], check=False)
        env_type = result.stdout.strip()
        return env_type if env_type else "none"
    except:
        return "none"

def should_skip_disk_management(env_type):
    """
    Skips parted, mkfs, etc., for known container/VM types if desired.
    """
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

    Some unprivileged LXC containers may not allow editing /etc/systemd/journald.conf
    or restarting journald. We can catch that exception.
    """
    try:
        if os.path.exists(JOURNALD_CONF):
            with open(JOURNALD_CONF, 'r') as f:
                lines = f.readlines()
            new_lines = []
            for line in lines:
                lstrip = line.strip()
                if lstrip.startswith("Storage="):
                    new_lines.append("Storage=volatile\n")
                elif lstrip.startswith("Compress="):
                    new_lines.append("Compress=no\n")
                elif lstrip.startswith("Seal="):
                    new_lines.append("Seal=no\n")
                else:
                    new_lines.append(line)

            # Ensure these lines exist
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
        logging.error(f"Failed to configure journald (might be LXC restrictions?): {e}")

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
    tmpfs. In unprivileged LXC, mount operations might fail; we log errors.
    """
    external_dir = find_external_drive()
    ram_disk = f"/mnt/ramdisk_{os.path.basename(directory)}"
    work_dir = f"{ram_disk}_work"

    # If external drive is found, create overflow dir
    overflow_dir = None
    if external_dir:
        overflow_dir = os.path.join(external_dir, f"overlay_{os.path.basename(directory)}")
        os.makedirs(overflow_dir, exist_ok=True)

    try:
        # Ensure final mountpoint, ramdisk, and workdir exist
        os.makedirs(directory, exist_ok=True)
        os.makedirs(ram_disk, exist_ok=True)
        os.makedirs(work_dir, exist_ok=True)

        # Mount tmpfs for the upper layer
        run_command(["mount", "-t", "tmpfs", "-o", f"size={ram_size}", "tmpfs", ram_disk])
        logging.info(f"Mounted tmpfs at {ram_disk} with size={ram_size} for {directory}")

        if overflow_dir:
            # Setup OverlayFS
            run_command([
                "mount", "-t", "overlay",
                "overlay",
                "-o", f"lowerdir={overflow_dir},upperdir={ram_disk},workdir={work_dir}",
                directory
            ])
            logging.info(f"Mounted OverlayFS: {directory} (upper={ram_disk}, lower={overflow_dir})")
        else:
            # If no external drive found, fallback to pure tmpfs
            run_command(["umount", ram_disk], check=False)
            try:
                os.rmdir(ram_disk)
                os.rmdir(work_dir)
            except:
                pass
            if fallback_tmpfs_size_mb:
                # Add /etc/fstab entry
                fstab_line = f"tmpfs   {directory}    tmpfs   defaults,noatime,size={fallback_tmpfs_size_mb}M 0 0"
                with open("/etc/fstab", 'r') as f:
                    fstab_contents = f.read()
                if f" {directory} " not in fstab_contents:
                    with open("/etc/fstab", 'a') as f:
                        f.write(fstab_line + "\n")
                run_command(["mount", directory], check=False)
                logging.info(f"No external drive found. Using direct tmpfs {fallback_tmpfs_size_mb}M for {directory}.")
            else:
                logging.info(f"No external drive found. Using direct tmpfs for {directory}, no fallback size given.")

        # Set permissions
        if mode:
            os.chmod(directory, int(mode, 8))

    except Exception as e:
        logging.error(f"Failed to set up OverlayFS/tmpfs for {directory}: {e}")

def manage_file_overflow(directory):
    """
    Checks for files > FILE_SIZE_LIMIT in `directory` and moves them
    to the external lowerdir if OverlayFS is in use.
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
                    rel_path = os.path.relpath(file_path, directory)
                    dest_path = os.path.join(overflow_dir, rel_path)
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    shutil.move(file_path, dest_path)
                    logging.info(f"Moved large file {file_path} ({size}B) to {dest_path}")
            except Exception as e:
                logging.error(f"Error moving {file_path}: {e}")

def flush_overlay(directory):
    """
    Twice-a-day: Move *all* files from the RAM-based upper layer to the
    external lower directory. Helps prevent log accumulation in RAM.
    """
    external_dir = find_external_drive()
    if not external_dir:
        logging.warning(f"No external drive found; cannot flush {directory}.")
        return
    overflow_dir = os.path.join(external_dir, f"overlay_{os.path.basename(directory)}")
    if not os.path.exists(overflow_dir):
        logging.warning(f"Overlay lower dir {overflow_dir} not found; cannot flush.")
        return

    # Check free space first
    if not check_free_space(overflow_dir):
        logging.warning("External drive might be low on space; continuing flush with caution...")

    upper_dir = f"/mnt/ramdisk_{os.path.basename(directory)}"
    if not os.path.exists(upper_dir):
        logging.info(f"No upper dir found for {directory}; maybe pure tmpfs fallback.")
        return

    # Move everything from upper to lower
    for root, dirs, files in os.walk(upper_dir):
        for file in files:
            file_path = os.path.join(root, file)
            rel_path = os.path.relpath(file_path, upper_dir)
            dest_path = os.path.join(overflow_dir, rel_path)
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            try:
                shutil.move(file_path, dest_path)
                logging.info(f"Flushed {file_path} -> {dest_path}")
            except Exception as e:
                logging.error(f"Failed to flush {file_path}: {e}")

def check_free_space(path, min_percent=10):
    """
    Returns True if `path` has at least `min_percent`% free space, else False.
    """
    try:
        st = os.statvfs(path)
        free_bytes = st.f_bavail * st.f_frsize
        total_bytes = st.f_blocks * st.f_frsize
        percent_free = (free_bytes / total_bytes) * 100 if total_bytes > 0 else 100
        logging.info(f"Free space at {path}: {percent_free:.2f}%")
        return percent_free >= min_percent
    except Exception as e:
        logging.error(f"check_free_space: {e}")
        return True

# ============================================================================
# Nx Witness Log Management
# ============================================================================
def clean_nx_logs():
    """
    Removes old Nx Witness log archives in NX_LOG_DIR. 
    Now extended to remove various patterns: .zip, .gz, .bz2, .xz, .1, etc.
    """
    if os.path.isdir(NX_LOG_DIR):
        clean_archive_files(NX_LOG_DIR, ARCHIVE_PATTERNS)

def clean_archive_files(directory, patterns):
    """
    Removes files matching any of the given 'patterns' in `directory` (recursively).
    E.g., for .zip, .gz, .1, .bz2, .xz, etc.
    """
    for root, dirs, files in os.walk(directory):
        for f in files:
            file_path = os.path.join(root, f)
            for pattern in patterns:
                if fnmatch.fnmatch(f, pattern):
                    try:
                        os.remove(file_path)
                        logging.info(f"Deleted {file_path} (pattern: {pattern})")
                        break  # done checking patterns for this file
                    except Exception as e:
                        logging.error(f"Failed to delete {file_path}: {e}")

# Optionally, you could do the same for /var/log if you want to remove older archives.
def clean_var_log_archives():
    if os.path.isdir("/var/log"):
        clean_archive_files("/var/log", ARCHIVE_PATTERNS)

# ============================================================================
# Drive Management
# ============================================================================
def is_boot_drive(drive):
    try:
        mount_info = run_command(["findmnt", "-n", "-o", "SOURCE", "/"]).stdout.strip()
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
    # Requires pyudev
    context = pyudev.Context()
    disks = []
    for device in context.list_devices(subsystem="block", DEVTYPE="disk"):
        disks.append(device.device_node)
    return disks

def get_partitions(drive):
    try:
        result = run_command(["lsblk", "-l", "-n", "-o", "NAME", drive])
        output = result.stdout.strip().split("\n")
    except:
        return []
    partitions = []
    drive_name = os.path.basename(drive)
    for line in output:
        p = line.strip()
        if p and p != drive_name:
            partitions.append("/dev/" + p)
    return partitions

def get_partition_fs_type(partition):
    try:
        out = run_command(["blkid", "-o", "value", "-s", "TYPE", partition], check=False)
        fs_type = out.stdout.strip()
        return fs_type if fs_type else None
    except:
        return None

def get_device_uuid(partition):
    try:
        out = run_command(["blkid", "-o", "value", "-s", "UUID", partition], check=True)
        return out.stdout.strip()
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
        logging.info(f"Filesystem check/repair done for {partition}")
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
                logging.info("Mount still failing after repair, reformatting partition...")
                format_partition(partition)
                return attempt_mount(partition, mount_point)
        else:
            logging.info("Repair failed; reformatting partition...")
            format_partition(partition)
            return attempt_mount(partition, mount_point)
    else:
        logging.info(f"Partition {partition} not {FS_TYPE}, reformatting...")
        format_partition(partition)
        return attempt_mount(partition, mount_point)

def mount_partition(partition, config, size_bytes):
    uuid = get_device_uuid(partition)
    if not uuid:
        logging.error(f"Could not get UUID for {partition}. Skipping.")
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
        parts = get_partitions(drive)
        if parts:
            partition = parts[0]
            format_partition(partition)
            return partition
        else:
            logging.error(f"No partition found after parted on {drive}")
            return None
    except Exception as e:
        logging.error(f"Failed to create partition on {drive}: {e}")
        return None

def manage_drives():
    """
    Detects large drives >= 512GB, partitions/formats them if needed, mounts them,
    and stores mount info in config.
    Skips if environment is a container/VM (by default).
    """
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
            logging.info(f"{drive} has no partitions; creating partition...")
            partition = create_partition_and_format(drive)
            if partition:
                mount_partition(partition, config, size)
        else:
            for partition in partitions:
                fs_type = get_partition_fs_type(partition)
                if fs_type != FS_TYPE:
                    logging.info(f"Reformatting {partition} from {fs_type} to {FS_TYPE}...")
                    format_partition(partition)
                mount_partition(partition, config, size)

    save_config(config)

# ============================================================================
# Service Management
# ============================================================================
def ensure_service_running(service_name):
    """
    Ensures Nx Witness (or any service) is running. If not, tries to start it.
    """
    try:
        run_command(["systemctl", "enable", service_name], check=False)
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status != "active":
            logging.info(f"{service_name} not running; starting...")
            run_command(["systemctl", "start", service_name], check=False)
        status = run_command(["systemctl", "is-active", service_name], check=False).stdout.strip()
        if status == "active":
            logging.info(f"{service_name} is running.")
        else:
            logging.error(f"Failed  to start {service_name}. Will retry later.")
    except Exception as e:
        logging.error(f"ensure_service_running error: {e}")

# ============================================================================
# Main Execution Loop
# ============================================================================
def main():
    env_type = detect_environment()
    skip_disks = should_skip_disk_management(env_type)

    logging.info(f"Detected environment: {env_type}. skip_disks={skip_disks}")

    # Attempt to configure journald ephemeral
    # If unprivileged container, it may fail -> we log the error
    configure_journald_volatile()

    # Setup OverlayFS or fallback tmpfs for /var/log and /tmp
    # (In unprivileged LXC, mounting might fail, but we attempt anyway)
    setup_overlayfs("/var/log", LOG_RAM_SIZE, fallback_tmpfs_size_mb=500, mode="755")
    setup_overlayfs("/tmp", TMP_RAM_SIZE, fallback_tmpfs_size_mb=100, mode="1777")

    # Nx Witness service
    ensure_service_running(SERVICE_NAME)

    last_script_update = time.time() - SCRIPT_UPDATE_INTERVAL
    last_flush = time.time() - FLUSH_INTERVAL  # so it flushes ASAP

    while True:
        current_time = time.time()

        # Manage large drives only if not skipping (bare metal or privileged container)
        if not skip_disks:
            manage_drives()

        # Ensure Nx Witness is running
        ensure_service_running(SERVICE_NAME)

        # Clean Nx logs (removing .zip, .gz, etc.)
        clean_nx_logs()

        # Optionally also remove old archives from /var/log to reduce writes
        clean_var_log_archives()

        # Immediately move huge files out of the upper RAM layer
        manage_file_overflow("/var/log")
        manage_file_overflow("/tmp")

        # Twice-a-day flush of all files in RAM to disk
        if (current_time - last_flush) >= FLUSH_INTERVAL:
            logging.info("Performing scheduled overlay flush (2x/day)...")
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
