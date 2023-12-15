#!/usr/bin/env python3

import os
import subprocess
import pyudev
import logging
import time
import fnmatch
import requests
import hashlib
import sys
import re
import json

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Constants
LOG_DIRS = ["/var/log"]
MAX_LOG_SIZE = 20 * 10**6  # 20 MB
GITHUB_SCRIPT_URL = "https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"
USB_SCAN_INTERVAL = 180  # 3 minutes in seconds
LOG_UPDATE_INTERVAL = 86400  # 24 hours in seconds
CONFIG_FILE_PATH = "/opt/usblogmon/config.json"  # Update this path as needed

def load_config():
    try:
        with open(CONFIG_FILE_PATH, 'r') as file:
            content = file.read().strip()
            return json.loads(content) if content else {}
    except FileNotFoundError:
        logging.info(f"{CONFIG_FILE_PATH} not found. Creating a new config file.")
        with open(CONFIG_FILE_PATH, 'w') as file:
            json.dump({}, file, indent=4)
        return {}
    except json.JSONDecodeError:
        logging.error(f"Error reading JSON from {CONFIG_FILE_PATH}. Creating a new configuration.")
        with open(CONFIG_FILE_PATH, 'w') as file:
            json.dump({}, file, indent=4)
        return {}


def save_config(config):
    with open(CONFIG_FILE_PATH, 'w') as file:
        json.dump(config, file, indent=4)

def is_boot_drive(drive):
    try:
        mount_info = subprocess.run(['findmnt', '-n', '-o', 'SOURCE', '/'], capture_output=True, text=True).stdout.strip()
        return drive in mount_info
    except subprocess.CalledProcessError:
        logging.error("Error determining the boot drive.")
        return False

def read_fstab():
    fstab_entries = {}
    with open('/etc/fstab', 'r') as file:
        for line in file:
            if line.startswith('#') or not line.strip():
                continue
            parts = re.split(r'\s+', line.strip())
            if len(parts) >= 2:
                fstab_entries[parts[0]] = parts[1]  # device: mount_point
    return fstab_entries

def check_and_mount_fstab_drives():
    fstab_entries = read_fstab()
    mounted = subprocess.run(["mount"], capture_output=True, text=True).stdout

    for device, mount_point in fstab_entries.items():
        if device not in mounted:
            try:
                mount_drive(device, mount_point)  # Utilize existing mount_drive function
            except Exception as e:
                logging.error(f"Failed to mount {device} from fstab: {e}")

def detect_usb_drives():
    context = pyudev.Context()
    usb_drives = []

    for device in context.list_devices(subsystem='block', DEVTYPE='disk'):
        if device.get('ID_BUS') == 'usb':
            usb_drives.append(device.device_node)

    return usb_drives

def get_partitions(drive):
    try:
        result = subprocess.run(["lsblk", "-l", "-n", "-o", "NAME", drive], capture_output=True, text=True, check=True)
        output = result.stdout.strip().split('\n')
    except subprocess.CalledProcessError as e:
        logging.error(f"Error getting partitions for {drive}: {e}")
        return []

    partitions = []
    drive_name = os.path.basename(drive)
    for line in output:
        partition = line.strip()
        if partition != drive_name:  # Skip the main drive
            partition_path = "/dev/" + partition
            try:
                size = int(subprocess.run(["blockdev", "--getsize64", partition_path], capture_output=True, text=True, check=True).stdout.strip())
                if size >= 100 * 10**9:
                    partitions.append(partition_path)
            except subprocess.CalledProcessError:
                continue  # Skip if blockdev fails

    return partitions

def is_mounted(partition):
    try:
        subprocess.run(["findmnt", partition], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False

def mount_drive(partition, mount_point):
    if is_mounted(partition):
        logging.info(f"{partition} is already mounted.")
        return

    try:
        os.makedirs(mount_point, exist_ok=True)
        subprocess.run(["mount", partition, mount_point], check=True)
        logging.info(f"Mounted {partition} at {mount_point}")
    except Exception as e:
        logging.error(f"Failed to mount {partition}: {e}")

def manage_drives():
    config = load_config()
    drives = detect_usb_drives()  # Detect USB drives
    other_drives = [d.device_node for d in pyudev.Context().list_devices(subsystem='block', DEVTYPE='disk') if not is_boot_drive(d.device_node)]

    for drive in drives + other_drives:
        partitions = get_partitions(drive)
        for partition in partitions:
            if not is_mounted(partition):
                friendly_name = "drive_" + partition.split('/')[-1]
                mount_point = f"/mnt/{friendly_name}"
                mount_drive(partition, mount_point)
                config[partition] = mount_point

    save_config(config)

def find_log_files():
    log_files = []
    patterns = ['*.gz', '*.1', '*syslog*', '*.log']  # Patterns to match
    for log_dir in LOG_DIRS:
        for root, dirs, files in os.walk(log_dir):
            for pattern in patterns:
                for file in fnmatch.filter(files, pattern):
                    log_files.append(os.path.join(root, file))
    return log_files

def delete_large_logs():
    log_files = find_log_files()
    for log_file in log_files:
        try:
            if os.path.getsize(log_file) > MAX_LOG_SIZE:
                os.remove(log_file)
                logging.info(f"Deleted large log file: {log_file}")
        except Exception as e:
            logging.error(f"Error checking/deleting log file {log_file}: {e}")

def monitor_logs():
    delete_large_logs()

def get_script_hash():
    with open(__file__, 'rb') as file:
        data = file.read()
        return hashlib.sha256(data).hexdigest()

def update_script():
    logging.info("Checking for script updates...")
    try:
        response = requests.get(GITHUB_SCRIPT_URL)
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

def main():
    last_log_update = time.time() - LOG_UPDATE_INTERVAL
    last_script_update = time.time()

    while True:
        current_time = time.time()

        # Manage USB and other non-boot drives
        manage_drives()

        # Check and mount drives from fstab
        check_and_mount_fstab_drives()

        # Check for log updates every 24 hours
        if current_time - last_log_update >= LOG_UPDATE_INTERVAL:
            monitor_logs()
            last_log_update = current_time

        # Check for script updates every 24 hours
        if current_time - last_script_update >= LOG_UPDATE_INTERVAL:
            update_script()
            last_script_update = current_time

        time.sleep(USB_SCAN_INTERVAL)

if __name__ == "__main__":
    main()
