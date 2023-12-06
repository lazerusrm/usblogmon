#!/usr/bin/env python3

import os
import subprocess
import pyudev
import logging
import time
import glob
import requests
import hashlib
import sys

# Logging setup
logging.basicConfig(level=logging.INFO, format=''%(asctime)s - %(levelname)s - %(message)s'')

# Constants
LOG_DIRS = ["/var/log"]
MAX_LOG_SIZE = 20 * 10**6  # 20 MB
GITHUB_SCRIPT_URL = "https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"

def detect_usb_drives():
    context = pyudev.Context()
    usb_drives = []

    for device in context.list_devices(subsystem='block', DEVTYPE='disk'):
        if device.get('ID_BUS') == 'usb':
            usb_drives.append(device.device_node)

    return usb_drives

def get_partitions(drive):
    try:
        result = subprocess.run(["lsblk", "-b", "-o", "NAME,SIZE", "-n", drive], capture_output=True, text=True, check=True)
        output = result.stdout.strip().split('\n')
    except subprocess.CalledProcessError as e:
        logging.error(f"Error getting partitions for {drive}: {e}")
        return []

    partitions = []
    for line in output[1:]:
        parts = line.strip().split()
        if len(parts) == 2:
            partition, size = parts
            if int(size) >= 100 * 10**9:
                partitions.append("/dev/" + partition)

    return partitions

def mount_drive(partition, mount_point):
    try:
        os.makedirs(mount_point, exist_ok=True)
        subprocess.run(["mount", partition, mount_point], check=True)
        logging.info(f"Mounted {partition} at {mount_point}")
    except Exception as e:
        logging.error(f"Failed to mount {partition}: {e}")

def find_log_files():
    log_files = []
    for log_dir in LOG_DIRS:
        for root, dirs, files in os.walk(log_dir):
            for file in files:
                if file.endswith(".log"):
                    log_files.append(os.path.join(root, file))
    return log_files

def remove_zipped_logs():
    for log_dir in LOG_DIRS:
        for zipped_log in glob.glob(os.path.join(log_dir, '*.gz')):
            try:
                os.remove(zipped_log)
                logging.info(f"Removed zipped log file: {zipped_log}")
            except Exception as e:
                logging.error(f"Error removing zipped log file {zipped_log}: {e}")

def delete_large_logs():
    log_files = find_log_files()
    for log_file in log_files:
        try:
            if os.path.getsize(log_file) > MAX_LOG_SIZE:
                os.remove(log_file)
                logging.info(f"Removed large log file: {log_file}")
        except Exception as e:
            logging.error(f"Error checking/deleting log file {log_file}: {e}")

def monitor_logs():
    remove_zipped_logs()
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
    while True:
        manage_usb_drives()
        monitor_logs()
        update_script()
        time.sleep(86400)  # Wait for 1 day (86400 seconds) before next update check

if __name__ == "__main__":
    main()