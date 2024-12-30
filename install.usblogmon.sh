#!/usr/bin/env bash
#
# ============================================================================
# Combined Installer (with Proxmox Detection):
#   1) Set system timezone to MST (warn if not) and record in summary
#   2) Check container -> append "-CT" to RMM agent name
#   3) Install or skip Tactical RMM (with default key if no input in 30s)
#   4) If not Proxmox, install USB Log Manager
#   5) If not Proxmox, check or install Nx Witness
#   6) Print final summary
# ============================================================================
set -euo pipefail

PROMPT_TIMEOUT=30
DEFAULT_RMM_API_KEY="F7ZOUL3MUDRIPMZI49BTF5NNAR9GS0VO"

NX5_LATEST="5.1.5.39242"
NX6_LATEST="6.0.1.39873"

NX6_X64="https://updates.networkoptix.com/default/6.0.1.39873/linux/nxwitness-server-6.0.1.39873-linux_x64.deb"
NX6_ARM="https://updates.networkoptix.com/default/6.0.1.39873/arm/nxwitness-server-6.0.1.39873-linux_arm64.deb"
NX5_X64="https://updates.networkoptix.com/default/5.1.5.39242/linux/nxwitness-server-5.1.5.39242-linux_x64.deb"
NX5_ARM="https://updates.networkoptix.com/default/5.1.5.39242/arm/nxwitness-server-5.1.5.39242-linux_arm64.deb"

TIMEZONE_MSG=""
RMM_MSG=""
USB_MSG="Skipped (not installed)."
NX_MSG="Not installed or not changed."

# ----------------------------------------------------------------------------
# Check if Proxmox
# ----------------------------------------------------------------------------
function is_proxmox_host() {
  if command -v pveversion &>/dev/null; then
    return 0
  fi
  if [[ -d "/etc/pve" ]]; then
    return 0
  fi
  return 1
}

# ----------------------------------------------------------------------------
# Prompt with Timeout
# ----------------------------------------------------------------------------
prompt_with_timeout() {
  local prompt_msg="$1"
  local default_val="$2"
  local __resultvar="$3"

  read -t "${PROMPT_TIMEOUT}" -p "$prompt_msg" user_input || true
  if [[ -z "${user_input:-}" ]]; then
    eval "$__resultvar=\"$default_val\""
  else
    eval "$__resultvar=\"$user_input\""
  fi
}

# ----------------------------------------------------------------------------
# Nx Witness Install/Upgrade
# ----------------------------------------------------------------------------
install_nx_witness() {
    local deb_url="$1"
    local old_ver="${2:-}"
    local new_ver=""  

    echo "==============================================================="
    echo "Installing (or upgrading) Nx Witness from: $deb_url"
    echo "==============================================================="

    apt-get update -y
    apt-get install -y dpkg

    curl -fsSL "$deb_url" -o /tmp/nxwitness-server.deb
    dpkg -i /tmp/nxwitness-server.deb || true
    apt-get -f -y install

    systemctl enable networkoptix-mediaserver || true
    systemctl start networkoptix-mediaserver || true

    local build_info="/opt/networkoptix/mediaserver/build_info.json"
    if [[ -f "$build_info" ]]; then
        new_ver="$(grep '"version":' "$build_info" | cut -d '"' -f 4)"
    fi

    if [[ -n "$old_ver" && -n "$new_ver" && "$old_ver" != "$new_ver" ]]; then
        NX_MSG="Upgraded Nx Witness from $old_ver to $new_ver"
    elif [[ -z "$old_ver" && -n "$new_ver" ]]; then
        NX_MSG="Installed Nx Witness version $new_ver"
    else
        NX_MSG="Installed or upgraded Nx Witness. (Could not read version info.)"
    fi

    if systemctl is-active --quiet networkoptix-mediaserver; then
        NX_MSG+=" (Service is running.)"
    else
        NX_MSG+=" (WARNING: Service is NOT running!)"
    fi

    echo "==============================================================="
    echo "Nx Witness install/upgrade complete. Service status:"
    systemctl status networkoptix-mediaserver --no-pager || true
}

check_installed_nx_version() {
    local build_info="/opt/networkoptix/mediaserver/build_info.json"
    if [[ -f "$build_info" ]]; then
        local installed_version
        installed_version="$(grep '"version":' "$build_info" 2>/dev/null | cut -d '"' -f 4 || true)"
        echo "$installed_version"
    else
        echo ""
    fi
}

compare_versions_and_prompt_upgrade() {
    local installed_version="$1"
    local channel="$2"

    local latest_version x64_url arm_url
    if [[ "$channel" == "5" ]]; then
        latest_version="$NX5_LATEST"
        x64_url="$NX5_X64"
        arm_url="$NX5_ARM"
    else
        latest_version="$NX6_LATEST"
        x64_url="$NX6_X64"
        arm_url="$NX6_ARM"
    fi

    echo "Nx Witness is installed with version: $installed_version"
    echo "Latest $channel.x is: $latest_version"

    if dpkg --compare-versions "$installed_version" lt "$latest_version"; then
        echo "A newer Nx Witness $channel version is available."
        local upgrade_choice=""
        prompt_with_timeout \
          "Do you want to upgrade to $latest_version? [y/N]: " \
          "N" \
          upgrade_choice

        case "${upgrade_choice^^}" in
          Y|YES)
            if [[ -n "$CONTAINER_SUFFIX" ]]; then
                install_nx_witness "$x64_url" "$installed_version"
            else
                if [[ "$ARCH" == "amd64" ]]; then
                    install_nx_witness "$x64_url" "$installed_version"
                else
                    install_nx_witness "$arm_url" "$installed_version"
                fi
            fi
            ;;
          *)
            NX_MSG="Nx Witness is already installed (v$installed_version), upgrade skipped."
            if systemctl is-active --quiet networkoptix-mediaserver; then
                NX_MSG+=" Service is running."
            else
                NX_MSG+=" Service is NOT running!"
            fi
            ;;
        esac
    else
        echo "Nx Witness is up-to-date."
        NX_MSG="Nx Witness is up-to-date (v$installed_version)."
        if systemctl is-active --quiet networkoptix-mediaserver; then
            NX_MSG+=" (Service is running.)"
        else
            NX_MSG+=" (Service is NOT running!)"
        fi
    fi
}

# ----------------------------------------------------------------------------
# MAIN SCRIPT
# ----------------------------------------------------------------------------

# 0) Check if Proxmox
IS_PROXMOX=false
if is_proxmox_host; then
  IS_PROXMOX=true
fi

# 1) Prompt for RMM key with timeout
user_provided_key=""
prompt_with_timeout \
  "Enter your Tactical RMM API Key (leave blank to use default) [Default in $PROMPT_TIMEOUT s]: " \
  "$DEFAULT_RMM_API_KEY" \
  user_provided_key

RMM_API_KEY="$user_provided_key"

# 2) Timezone
TIMEZONE_MSG="System timezone not changed."
if command -v timedatectl &>/dev/null; then
    CURRENT_TZ="$(timedatectl show --property=Timezone --value || true)"
    if [ "$CURRENT_TZ" != "America/Denver" ]; then
        TIMEZONE_MSG="System timezone changed from $CURRENT_TZ to America/Denver (MST)."
        timedatectl set-timezone America/Denver
    else
        TIMEZONE_MSG="System timezone is already America/Denver (MST)."
    fi
else
    TIMEZONE_MSG="WARNING: 'timedatectl' not found. Could not set or verify timezone."
fi

# 3) Check container => suffix
CONTAINER_SUFFIX=""
if command -v systemd-detect-virt &>/dev/null; then
    if systemd-detect-virt --container &>/dev/null; then
        CONTAINER_SUFFIX="-CT"
    fi
else
    if [ -f "/.dockerenv" ] || grep -q "docker\|container" /proc/1/cgroup 2>/dev/null; then
        CONTAINER_SUFFIX="-CT"
    fi
fi

AGENT_NAME="$(hostname)${CONTAINER_SUFFIX}"

# 4) Detect architecture
ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m || true)"
case "$ARCH" in
  amd64|x86_64)   ARCH="amd64" ;;
  arm64|aarch64)  ARCH="arm64" ;;
  *)
    echo "Unsupported architecture: $ARCH"
    exit 1
    ;;
esac

# 5) Tactical RMM
RMM_INSTALLED=false
if systemctl is-active --quiet tacticalagent 2>/dev/null; then
    echo "==============================================================="
    echo "Tactical RMM agent already installed/running. Skipping RMM install."
    echo "==============================================================="
    RMM_INSTALLED=true
    RMM_MSG="Tactical RMM agent was already installed and running."
else
    echo "==============================================================="
    echo "Installing Tactical RMM agent for:"
    echo "  Client: \"Industrial Camera Systems\""
    echo "  Site:   \"Onboarded\""
    echo "  Arch:   \"${ARCH}\""
    echo "  Agent:  \"${AGENT_NAME}\""
    echo "==============================================================="
    RMM_INSTALL_SCRIPT_URL="https://${RMM_DOMAIN}/api/v3/software/linuxagent/?arch=${ARCH}&client=Industrial%20Camera%20Systems&site=Onboarded&apitoken=${RMM_API_KEY}&agentname=${AGENT_NAME// /%20}"

    curl -fsSL "${RMM_INSTALL_SCRIPT_URL}" -o /tmp/tactical_rmm_install.sh
    chmod +x /tmp/tactical_rmm_install.sh
    /tmp/tactical_rmm_install.sh || {
       echo "Error: Tactical RMM install script failed."
       RMM_MSG="Tactical RMM installation FAILED."
       exit 1
    }

    RMM_MSG="Tactical RMM installed successfully (Agent: ${AGENT_NAME})."
    RMM_INSTALLED=true
fi

# If RMM installed, parse config
if $RMM_INSTALLED; then
    if [[ -f "/etc/tacticalagent/tacticalagent.conf" ]]; then
        parse_client="$(grep -E '^client\s*=' /etc/tacticalagent/tacticalagent.conf | cut -d '=' -f2 | xargs || true)"
        parse_site="$(grep -E '^site\s*=' /etc/tacticalagent/tacticalagent.conf | cut -d '=' -f2 | xargs || true)"
        if [[ -n "$parse_client" && -n "$parse_site" ]]; then
            RMM_MSG+=" (Currently configured for client: $parse_client, site: $parse_site.)"
        fi
    fi
fi

# 6) USB Log Manager
if $IS_PROXMOX; then
  # Skip on Proxmox
  USB_MSG="Skipped USB Log Manager (Proxmox Host)."
else
  # Install USB Log Manager
  echo "==============================================================="
  echo "         USB Log Manager Installer"
  echo "==============================================================="
  USB_MSG="USB Log Manager installed successfully."

  if [[ "$(id -u)" -ne 0 ]]; then
      echo "Error: Please run as root!"
      USB_MSG="USB Log Manager installation FAILED (not root)."
      exit 1
  fi

  echo "Updating package list with apt-get update..."
  apt-get update -y

  echo "Installing required packages via apt-get ..."
  apt-get install -y \
    python3 \
    python3-requests \
    python3-pyudev \
    parted \
    e2fsprogs \
    curl

  SCRIPT_URL="https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"
  INSTALL_DIR="/opt/usblogmon"
  INSTALL_SCRIPT="${INSTALL_DIR}/usb_log_manager.py"
  SERVICE_FILE="/etc/systemd/system/usblogmon.service"

  echo "Creating installation directory at ${INSTALL_DIR} ..."
  mkdir -p "${INSTALL_DIR}"

  echo "Downloading usb_log_manager.py from ${SCRIPT_URL} ..."
  curl -fsSL "${SCRIPT_URL}" -o "${INSTALL_SCRIPT}"
  chmod 755 "${INSTALL_SCRIPT}"

  echo "Creating systemd service file at ${SERVICE_FILE} ..."
  cat > "${SERVICE_FILE}" << EOF
[Unit]
Description=USB Log Monitoring and Drive Management Service
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${INSTALL_SCRIPT}
Restart=always

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable usblogmon.service
  systemctl start usblogmon.service

  echo "Service status (systemctl status usblogmon):"
  systemctl status usblogmon.service --no-pager || true
fi

# 7) Nx Witness Section
if $IS_PROXMOX; then
  NX_MSG="Nx Witness skipped (Proxmox host)."
else
  INSTALLED_NX_VERSION="$(check_installed_nx_version)"
  if [[ -n "$INSTALLED_NX_VERSION" ]]; then
    echo "==============================================================="
    echo " Nx Witness appears to be installed (version: $INSTALLED_NX_VERSION)."
    major_ver="${INSTALLED_NX_VERSION%%.*}"
    case "$major_ver" in
      "5") compare_versions_and_prompt_upgrade "$INSTALLED_NX_VERSION" "5" ;;
      "6") compare_versions_and_prompt_upgrade "$INSTALLED_NX_VERSION" "6" ;;
      *)
        echo "Unknown Nx major version: $major_ver. Skipping upgrade check."
        NX_MSG="Nx Witness installed (v$INSTALLED_NX_VERSION), no upgrade attempted."
        if systemctl is-active --quiet networkoptix-mediaserver; then
          NX_MSG+=" Service is running."
        else
          NX_MSG+=" Service is NOT running!"
        fi
        ;;
    esac
  else
    echo "==============================================================="
    echo "Do you want to install Nx Witness Media Server?"
    echo "  1) Install version 5 (latest: $NX5_LATEST)"
    echo "  2) Install version 6 (latest: $NX6_LATEST)"
    echo "  3) Skip"
    user_choice=""
    prompt_with_timeout \
      "Enter your choice [1/2/3] (default=3 in $PROMPT_TIMEOUT s): " \
      "3" \
      user_choice

    case "$user_choice" in
      1)
        echo "You chose Nx Witness version 5..."
        if [[ -n "$CONTAINER_SUFFIX" ]]; then
          install_nx_witness "$NX5_X64"
        else
          if [[ "$ARCH" == "amd64" ]]; then
            install_nx_witness "$NX5_X64"
          else
            install_nx_witness "$NX5_ARM"
          fi
        fi
        ;;
      2)
        echo "You chose Nx Witness version 6..."
        if [[ -n "$CONTAINER_SUFFIX" ]]; then
          install_nx_witness "$NX6_X64"
        else
          if [[ "$ARCH" == "amd64" ]]; then
            install_nx_witness "$NX6_X64"
          else
            install_nx_witness "$NX6_ARM"
          fi
        fi
        ;;
      3|*)
        echo "Skipping Nx Witness installation."
        NX_MSG="Nx Witness not installed."
        ;;
    esac
  fi
fi

# 8) Final Summary
echo "==============================================================="
echo "All done."
echo "==============================================================="
echo
echo "================ Installation Summary ================"
echo "1) Timezone:  $TIMEZONE_MSG"
echo "2) RMM:       $RMM_MSG"
echo "3) USB Log Manager: $USB_MSG"
echo "4) Nx Witness: $NX_MSG"
echo "======================================================="
echo
