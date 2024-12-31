#!/usr/bin/env python3

import os
import sys
import time
import logging
import requests
import subprocess

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# ============================================================================
# Configuration
# ============================================================================
# 1) **Fine-Grained PAT** (read-only for your private OmniDeploy repo)
FINE_GRAINED_PAT = "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"

# 2) **Private OmniDeploy Installer URL**
#    Replace this with the raw URL of your private 'install.sh'
PRIVATE_OMNIDEPLOY_URL = "https://raw.githubusercontent.com/lazerusrm/Omnideploy/main/install.sh"

# 3) **Local Path** to download and store the new installer
NEW_SCRIPT_PATH = "/opt/omnideploy/install.sh"

# 4) How often to check for OmniDeploy migration (in seconds) — default once per day
SCRIPT_UPDATE_INTERVAL = 86400

# In this example, we loop every 600s (10 minutes) to see if a day has passed.
LOOP_SLEEP = 600

# ============================================================================
# Utility Functions
# ============================================================================
def run_command(cmd):
    """
    Runs a command in the shell with error checking.
    """
    logging.debug(f"Running command: {cmd}")
    return subprocess.run(cmd, shell=True, check=True)

def download_private_file(url, dest_path, pat):
    """
    Downloads a file from a private GitHub URL using a Fine-Grained PAT.
    Saves the file to 'dest_path'.
    """
    logging.info(f"Attempting to download from private repo: {url}")
    headers = {"Authorization": f"token {pat}"}

    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()  # Raise an exception if not 2xx

    with open(dest_path, "wb") as f:
        f.write(resp.content)

    logging.info(f"Saved new script to {dest_path}")
    return dest_path

def install_omnideploy():
    """
    Main function to download and install the new OmniDeploy system
    from the private repository, then retire this script.
    """
    logging.info("Starting OmniDeploy installation process...")

    # Download the new installer
    try:
        download_private_file(PRIVATE_OMNIDEPLOY_URL, NEW_SCRIPT_PATH, FINE_GRAINED_PAT)
    except Exception as e:
        logging.error(f"Failed to download OmniDeploy installer: {e}")
        return False

    # Make the script executable
    os.chmod(NEW_SCRIPT_PATH, 0o755)

    # Execute the installer
    try:
        logging.info(f"Running installer: {NEW_SCRIPT_PATH}")
        run_command(NEW_SCRIPT_PATH)
        logging.info("OmniDeploy installed successfully.")
    except Exception as e:
        logging.error(f"Failed to run OmniDeploy installer: {e}")
        return False

    return True

# ============================================================================
# Main Execution: Minimal Loop
# ============================================================================
def main():
    """
    This replaces the old usblogmon main loop with minimal daily checks
    to migrate to OmniDeploy. Once successful, we exit.
    """
    last_update_check = 0
    while True:
        now = time.time()
        # Check if it's time to attempt the migration
        if now - last_update_check >= SCRIPT_UPDATE_INTERVAL:
            success = install_omnideploy()
            if success:
                logging.info("Migration to OmniDeploy complete. Exiting usb_log_manager.")
                sys.exit(0)  # Retire this old script for good
            else:
                logging.warning("OmniDeploy install failed. Will retry later.")
            last_update_check = now

        time.sleep(LOOP_SLEEP)  # Sleep ~10 minutes, then loop again

if __name__ == "__main__":
    main()
