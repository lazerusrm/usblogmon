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

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Constants
LOG_DIRS = ["/var/log"]
MAX_LOG_SIZE = 20 * 10**6  # 20 MB
GITHUB_SCRIPT_URL = "https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"
USB_SCAN_INTERVAL = 180  # 3 minutes in seconds
LOG_UPDATE_INTERVAL = 86400  # 24 hours in seconds

def detect_usb_drives():
    context = pyudev.Context()
    usb_drives = []

    for device in context.list_devices(subsystem='block', DEVTYPE='disk'):
        if device.get('ID_BUS') == 'usb':
            usb_drives.append(device.device_node)

    return usb_drives

def get_partitions(drive):
    try:
        # Use '-o NAME' to get a plain list of device names
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
        # Use 'findmnt' to check if the partition is already mounted
        subprocess.run(["findmnt", partition], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        # If 'findmnt' fails, the partition is not mounted
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

def manage_usb_drives():
    drives = detect_usb_drives()

    if not drives:
        logging.info("No USB drives detected.")

    for drive in drives:
        partitions = get_partitions(drive)
        for partition in partitions:
            friendly_name = "usb_drive_" + partition.split('/')[-1]
            mount_point = f"/mnt/{friendly_name}"
            mount_drive(partition, mount_point)

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
    last_log_update = time.time() - LOG_UPDATE_INTERVAL  # Forces immediate log check on first run
    last_script_update = time.time()

    while True:
        current_time = time.time()

        # Manage USB Drives
        manage_usb_drives()

        # Check for log updates every 24 hours
        if current_time - last_log_update >= LOG_UPDATE_INTERVAL:
            monitor_logs()
            last_log_update = current_time

        # Check for script updates every 24 hours
        if current_time - last_script_update >= LOG_UPDATE_INTERVAL:
            update_script()
            last_script_update = current_time

        time.sleep(USB_SCAN_INTERVAL)  # Wait for 3 minutes before next USB scan



if __name__ == "__main__":
    main()
