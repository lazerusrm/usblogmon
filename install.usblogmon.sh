#!/usr/bin/env bash
#
# ============================================================================
# Universal Installer + Nx + USB + Tailscale + Summaries:
#   1) Update packages, install dependencies (incl. sudo if missing)
#   2) Add Tailscale repo & GPG keys, apt-get update again
#   3) Install Tailscale
#   4) Prompt for Tailscale auth key (default interactive)
#   5) Detect Proxmox => skip Nx/USB if found
#   6) Check if RMM agent is installed:
#       - If running => skip
#       - Otherwise => remove old agent/mesh, install new agent/mesh
#   7) Prompt for Nx Witness (5 pinned or 6 pinned/dynamic), skip if Proxmox
#   8) Install USB Log Manager if not Proxmox
#   9) Print final summary (Timezone, RMM, Nx, USB, Tailscale)
#
# For ARM, set agentDL / meshDL to your ARM links. For x86, set them accordingly.
# ============================================================================
set -euo pipefail

###############################################################################
# Configuration
###############################################################################
DEFAULT_RMM_API_KEY="97e3b76a7f14d7b214ae846e51c3f2f57def2ba7e7c92b7fe1eebf99d66bf71d"
DEFAULT_TIMEZONE="America/Denver"

# Nx pinned version 5
NX5_LATEST="5.1.5.39242"
NX5_X64="https://updates.networkoptix.com/default/5.1.5.39242/linux/nxwitness-server-5.1.5.39242-linux_x64.deb"
NX5_ARM="https://updates.networkoptix.com/default/5.1.5.39242/arm/nxwitness-server-5.1.5.39242-linux_arm64.deb"

# Nx pinned version 6 (hard-coded) - or dynamic if you prefer
NX6_LATEST="6.0.1.39873"
NX6_X64="https://updates.networkoptix.com/default/6.0.1.39873/linux/nxwitness-server-6.0.1.39873-linux_x64.deb"
NX6_ARM="https://updates.networkoptix.com/default/6.0.1.39873/arm/nxwitness-server-6.0.1.39873-linux_arm64.deb"

# Tactical RMM + Mesh
apiURL="https://api.industrialcamera.com"

# For ARM use these:
agentDL="https://agents.tacticalrmm.com/api/v2/agents/?version=2.8.0&arch=arm64&token=a0db14ae-c125-4c9e-93ef-20971a905664&plat=linux&api=api.industrialcamera.com"
meshDL="https://mesh.industrialcamera.com/meshagents?id=GJu9MrM4KZvvQ0kAr6llxrYMdKtvBVI3gQd7G6@j1oiaeB\$IXwcRdfE0qgi3fet7&installflags=2&meshinstall=26"

# If x86, you could switch to these:
# agentDL="https://agents.tacticalrmm.com/api/v2/agents/?version=2.8.0&arch=amd64&token=a0db14ae-c125-4c9e-93ef-20971a905664&plat=linux&api=api.industrialcamera.com"
# meshDL="https://mesh.industrialcamera.com/meshagents?id=GJu9MrM4KZvvQ0kAr6llxrYMdKtvBVI3gQd7G6@j1oiaeB\$IXwcRdfE0qgi3fet7&installflags=2&meshinstall=6"

clientID="14"
siteID="139"
agentType="server"

agentBinPath="/usr/local/bin"
binName="tacticalagent"
agentBin="${agentBinPath}/${binName}"
agentConf="/etc/tacticalagent"
agentSvcName="tacticalagent.service"
agentSysD="/etc/systemd/system/${agentSvcName}"
agentDir="/opt/tacticalagent"

meshDir="/opt/tacticalmesh"
meshSystemBin="${meshDir}/meshagent"
meshSvcName="meshagent.service"
meshSysD="/lib/systemd/system/${meshSvcName}"

# Distros for mesh locale logic
deb=(ubuntu debian raspbian kali linuxmint)
rhe=(fedora rocky centos rhel amzn arch opensuse)

###############################################################################
# Summary Variables
###############################################################################
TIMEZONE_MSG=""
RMM_MSG="Not installed or changed."
NX_MSG="Not installed or not changed."
USB_MSG="Skipped (not installed)."
TAILSCALE_MSG="Not installed or changed."

###############################################################################
# Prompt / Helper Functions
###############################################################################
function prompt_with_timeout() {
  local prompt_msg="$1"
  local default_val="$2"
  local __resultvar="$3"

  read -t 30 -p "$prompt_msg" user_input || true
  if [[ -z "${user_input:-}" ]]; then
    eval "$__resultvar=\"$default_val\""
  else
    eval "$__resultvar=\"$user_input\""
  fi
}

function is_proxmox_host() {
  if command -v pveversion &>/dev/null; then
    return 0
  fi
  [[ -d "/etc/pve" ]] && return 0
  return 1
}

###############################################################################
# Nx Witness Install
###############################################################################
function install_nx_witness() {
  local deb_url="$1"
  local old_ver="${2:-}"

  echo "==============================================================="
  echo "Installing (or upgrading) Nx Witness from: $deb_url"
  echo "==============================================================="
  apt-get update -y
  apt-get install -y dpkg

  curl -fsSL "$deb_url" -o /tmp/nxwitness-server.deb

  # dpkg might error => we ignore exit code
  dpkg -i /tmp/nxwitness-server.deb || true
  apt-get -f -y install

  systemctl enable networkoptix-mediaserver || true
  systemctl start networkoptix-mediaserver || true

  local build_info="/opt/networkoptix/mediaserver/var/log/build_info.json"
  if [[ ! -f "$build_info" ]]; then
    build_info="/opt/networkoptix/mediaserver/build_info.json"
  fi

  local new_ver=""
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

  # Check if Nx is running
  if systemctl is-active --quiet networkoptix-mediaserver; then
    NX_MSG+=" (Service is running.)"
  else
    NX_MSG+=" (WARNING: Service is NOT running!)"
  fi

  echo "==============================================================="
  echo "Nx Witness install/upgrade complete. Service status:"
  systemctl status networkoptix-mediaserver --no-pager || true
}

function check_installed_nx_version() {
  local build_info="/opt/networkoptix/mediaserver/var/log/build_info.json"
  if [[ ! -f "$build_info" ]]; then
    build_info="/opt/networkoptix/mediaserver/build_info.json"
  fi
  if [[ -f "$build_info" ]]; then
    grep '"version":' "$build_info" 2>/dev/null | cut -d '"' -f 4 || true
  else
    echo ""
  fi
}

###############################################################################
# Mesh + Agent Removal / Installation
###############################################################################
function RemoveOldAgent() {
  # Remove old agent only if it was previously installed
  if [[ -f "${agentSysD}" ]]; then
    systemctl disable ${agentSvcName} || true
    systemctl stop ${agentSvcName} || true
    rm -f "${agentSysD}"
    systemctl daemon-reload
  fi
  [[ -f "${agentConf}" ]] && rm -f "${agentConf}"
  [[ -f "${agentBin}" ]] && rm -f "${agentBin}"
  [[ -d "${agentDir}" ]] && rm -rf "${agentDir}"
}

function RemoveMesh() {
  # Remove old mesh only if it was previously installed
  if [[ -f "${meshSystemBin}" ]]; then
    env XAUTHORITY=foo DISPLAY=bar "${meshSystemBin}" -uninstall || true
    sleep 1
  fi
  if [[ -f "${meshSysD}" ]]; then
    systemctl stop "${meshSvcName}" >/dev/null 2>&1 || true
    systemctl disable "${meshSvcName}" >/dev/null 2>&1 || true
    rm -f "${meshSysD}"
  fi
  rm -rf "${meshDir}"
  systemctl daemon-reload
}

function set_locale_deb() {
  locale-gen "en_US.UTF-8" || true
  localectl set-locale LANG=en_US.UTF-8 || true
}

function set_locale_rhel() {
  localedef -c -i en_US -f UTF-8 en_US.UTF-8 >/dev/null 2>&1 || true
  localectl set-locale LANG=en_US.UTF-8 || true
}

function InstallMesh() {
  local distroID=""
  local distroIDLIKE=""
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release 2>/dev/null || true
    distroID="${ID:-}"
    distroIDLIKE="${ID_LIKE:-}"
    if [[ " ${deb[*]} " =~ " ${distroID} " ]] || [[ " ${deb[*]} " =~ " ${distroIDLIKE} " ]]; then
      set_locale_deb
    elif [[ " ${rhe[*]} " =~ " ${distroID} " ]]; then
      set_locale_rhel
    else
      set_locale_rhel
    fi
  fi
  local meshTmpDir="/root/meshtemp"
  mkdir -p "$meshTmpDir"

  local meshTmpBin="${meshTmpDir}/meshagent"
  wget --no-check-certificate -q -O "${meshTmpBin}" "${meshDL}"
  chmod +x "${meshTmpBin}"
  mkdir -p "${meshDir}"
  env LC_ALL=en_US.UTF-8 LANGUAGE=en_US XAUTHORITY=foo DISPLAY=bar \
    "${meshTmpBin}" -install --installPath="${meshDir}"
  sleep 1
  rm -rf "${meshTmpDir}"
}

###############################################################################
# Tailscale Installation
###############################################################################
function install_tailscale() {
  echo "==============================================================="
  echo "Installing Tailscale..."
  echo "==============================================================="
  apt-get install -y tailscale
  systemctl enable tailscaled
  systemctl start tailscaled
}

function configure_tailscale() {
  echo "==============================================================="
  echo "Configuring Tailscale..."
  echo "==============================================================="
  local TAILSCALE_KEY=""
  prompt_with_timeout \
    "Enter your Tailscale auth key (optional - leave blank for interactive) [Default in 30s: none]: " \
    "" \
    TAILSCALE_KEY

  if [[ -n "$TAILSCALE_KEY" ]]; then
    if tailscale up --authkey="${TAILSCALE_KEY}"; then
      TAILSCALE_MSG="Tailscale installed and connected via auth key."
    else
      TAILSCALE_MSG="Tailscale installed, but auth key was invalid or failed. Run 'tailscale up' manually."
    fi
  else
    echo "No Tailscale auth key; using interactive login..."
    if tailscale up; then
      TAILSCALE_MSG="Tailscale installed and connected (interactive)."
    else
      TAILSCALE_MSG="Tailscale installed, but interactive login was skipped/failed. Run 'tailscale up' manually."
    fi
  fi
}

###############################################################################
# Tactical RMM Installation (only if not already active)
###############################################################################
function do_TacticalRMM_Install() {
  # Check if agent is active => skip
  if systemctl is-active --quiet "${agentSvcName}" 2>/dev/null; then
    echo "Tactical RMM agent is already installed and running. Skipping re-install."
    RMM_MSG="Tactical RMM agent already installed/running. Skipped re-install."
    return
  fi

  echo "Removing any old agent or mesh..."
  RemoveMesh
  RemoveOldAgent

  echo "Downloading tactical agent from: ${agentDL}"
  mkdir -p "${agentBinPath}"
  if ! wget -q -O "${agentBin}" "${agentDL}"; then
    echo "ERROR: Unable to download the Tactical RMM agent"
    RMM_MSG="Tactical RMM agent download failed."
    exit 1
  fi
  chmod +x "${agentBin}"

  echo "Downloading and installing mesh agent..."
  InstallMesh
  sleep 2

  echo "Getting mesh node id from the tactical agent..."
  local MESH_NODE_ID
  MESH_NODE_ID="$(env XAUTHORITY=foo DISPLAY=bar "${agentBin}" -m nixmeshnodeid || true)"

  # Build suffix if Proxmox or container
  local SUFFIXES=""
  if $IS_PROXMOX; then
    SUFFIXES="${SUFFIXES}-PXMX"
  fi

  # container check
  if command -v systemd-detect-virt &>/dev/null; then
    if systemd-detect-virt --container &>/dev/null; then
      SUFFIXES="${SUFFIXES}-CT"
    fi
  else
    if [[ -f "/.dockerenv" ]] || grep -q "docker\|container" /proc/1/cgroup 2>/dev/null; then
      SUFFIXES="${SUFFIXES}-CT"
    fi
  fi
  SUFFIXES="$(echo "${SUFFIXES}" | sed 's/^-*//')"

  local AGENT_NAME
  if [[ -n "$SUFFIXES" ]]; then
    AGENT_NAME="$(hostname)-${SUFFIXES}"
  else
    AGENT_NAME="$(hostname)"
  fi

  echo "Installing the tactical agent..."
  local INSTALL_CMD
  INSTALL_CMD="${agentBin} -m install"
  INSTALL_CMD+=" -api ${apiURL}"
  INSTALL_CMD+=" -client-id ${clientID}"
  INSTALL_CMD+=" -site-id ${siteID}"
  INSTALL_CMD+=" -agent-type ${agentType}"
  INSTALL_CMD+=" -auth ${RMM_API_KEY}"
  INSTALL_CMD+=" --desc \"${AGENT_NAME}\""

  if [[ -n "$MESH_NODE_ID" ]]; then
    INSTALL_CMD+=" --meshnodeid ${MESH_NODE_ID}"
  fi

  if ! eval "${INSTALL_CMD}"; then
    echo "ERROR: Tactical RMM agent installation failed."
    RMM_MSG="Tactical RMM agent install failed."
    exit 1
  fi

  echo "Creating systemd service for the agent..."
  cat > "${agentSysD}" <<EOF
[Unit]
Description=Tactical RMM Linux Agent

[Service]
Type=simple
ExecStart=${agentBin} -m svc
User=root
Group=root
Restart=always
RestartSec=5s
LimitNOFILE=1000000
KillMode=process

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${agentSvcName}"
  systemctl start "${agentSvcName}"
  RMM_MSG="Tactical RMM agent + Mesh installed and started (Name: ${AGENT_NAME})."
}

###############################################################################
# MAIN
###############################################################################

# 1) Update + install base dependencies
echo "==============================================================="
echo "Updating system packages and installing base dependencies..."
echo "==============================================================="
apt-get update -y
apt-get install -y \
  sudo \
  curl \
  gnupg2 \
  apt-transport-https \
  ca-certificates \
  lsb-release \
  python3 \
  python3-requests \
  parted \
  e2fsprogs \
  s-tui \
  stress

# 2) Add Tailscale repo + GPG key, then apt-get update
echo "==============================================================="
echo "Adding Tailscale APT repository and GPG key..."
echo "==============================================================="
curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
  | tee /usr/share/keyrings/tailscale-archive-keyring.gpg >/dev/null

curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
  | tee /etc/apt/sources.list.d/tailscale.list

echo "==============================================================="
echo "Updating packages again to include Tailscale repo..."
echo "==============================================================="
apt-get update -y

# 3) Install Tailscale
install_tailscale

# 4) Configure Tailscale
configure_tailscale

# 5) Prompt for RMM API Key
RMM_API_KEY=""
prompt_with_timeout \
  "Enter your Tactical RMM API Key (leave blank to use default) [Default in 30s: ${DEFAULT_RMM_API_KEY}]: " \
  "${DEFAULT_RMM_API_KEY}" \
  RMM_API_KEY

# 6) Prompt for Timezone
TIMEZONE=""
prompt_with_timeout \
  "Enter your desired Timezone (e.g. America/Denver) [Default in 30s: ${DEFAULT_TIMEZONE}]: " \
  "${DEFAULT_TIMEZONE}" \
  TIMEZONE

# 6a) Set Timezone
TIMEZONE_MSG="Timezone is already ${TIMEZONE}."
if command -v timedatectl &>/dev/null; then
  CURRENT_TZ="$(timedatectl show --property=Timezone --value || true)"
  if [[ "${CURRENT_TZ}" != "${TIMEZONE}" ]]; then
    echo "Changing timezone from ${CURRENT_TZ} to ${TIMEZONE}."
    timedatectl set-timezone "${TIMEZONE}"
    TIMEZONE_MSG="Changed timezone from ${CURRENT_TZ} to ${TIMEZONE}."
  fi
else
  echo "WARNING: timedatectl not found; skipping timezone configuration."
  TIMEZONE_MSG="WARNING: timedatectl not found. Skipped timezone set."
fi

# 7) Detect Proxmox => skip Nx + USB if found
IS_PROXMOX=false
if is_proxmox_host; then
  IS_PROXMOX=true
  echo "Detected Proxmox => skipping Nx Witness & USB Log Manager."
fi

# 8) Install Tactical RMM (with Mesh) if not active
do_TacticalRMM_Install

# 9) Nx Logic
if $IS_PROXMOX; then
  NX_MSG="Nx Witness skipped (Proxmox host)."
else
  echo "==============================================================="
  local INSTALLED_NX_VERSION
  INSTALLED_NX_VERSION="$(check_installed_nx_version)"
  if [[ -n "$INSTALLED_NX_VERSION" ]]; then
    echo "Nx Witness is installed (version: $INSTALLED_NX_VERSION)."
    major_ver="${INSTALLED_NX_VERSION%%.*}"
    case "$major_ver" in
      "5")
        echo "Nx 5 is installed; skipping detailed upgrade logic for brevity."
        NX_MSG="Nx 5 is installed (v${INSTALLED_NX_VERSION})."
        ;;
      "6")
        echo "Nx 6 is installed; skipping detailed upgrade logic for brevity."
        NX_MSG="Nx 6 is installed (v${INSTALLED_NX_VERSION})."
        ;;
      *)
        echo "Unknown Nx major version: $major_ver => skipping Nx upgrade."
        NX_MSG="Nx installed (v${INSTALLED_NX_VERSION}), no upgrade attempted."
        ;;
    esac
  else
    echo "Do you want to install Nx Witness Media Server?"
    echo "  1) Install version 5 (latest: $NX5_LATEST)"
    echo "  2) Install version 6 (pinned $NX6_LATEST)"
    echo "  3) Skip"
    user_choice=""
    prompt_with_timeout "Enter your choice [1/2/3] (default=3 in 30s): " "3" user_choice

    ARCH="$(dpkg --print-architecture 2>/dev/null || uname -m || true)"
    case "$ARCH" in
      amd64|x86_64) ARCH="amd64" ;;
      arm64|aarch64) ARCH="arm64" ;;
      *)
        echo "Unsupported Nx arch: $ARCH"
        ;;
    esac

    case "$user_choice" in
      1)
        echo "You chose Nx Witness 5..."
        if [[ "$ARCH" == "arm64" ]]; then
          install_nx_witness "$NX5_ARM"
        else
          install_nx_witness "$NX5_X64"
        fi
        ;;
      2)
        echo "You chose Nx Witness 6 (hard-coded version $NX6_LATEST)..."
        if [[ "$ARCH" == "arm64" ]]; then
          install_nx_witness "$NX6_ARM"
        else
          install_nx_witness "$NX6_X64"
        fi
        ;;
      3|*)
        echo "Skipping Nx Witness install."
        NX_MSG="Nx Witness not installed."
        ;;
    esac
  fi
fi

# 10) USB Log Manager
if $IS_PROXMOX; then
  USB_MSG="USB Log Manager skipped (Proxmox Host)."
else
  echo "==============================================================="
  echo " USB Log Manager Installer"
  echo "==============================================================="
  USB_MSG="USB Log Manager installed successfully."
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "Error: Please run as root!"
    USB_MSG="USB Log Manager installation FAILED (not root)."
    exit 1
  fi

  SCRIPT_URL="https://raw.githubusercontent.com/lazerusrm/usblogmon/main/usb_log_manager.py"
  INSTALL_DIR="/opt/usblogmon"
  INSTALL_SCRIPT="${INSTALL_DIR}/usb_log_manager.py"
  SERVICE_FILE="/etc/systemd/system/usblogmon.service"

  mkdir -p "${INSTALL_DIR}"
  curl -fsSL "${SCRIPT_URL}" -o "${INSTALL_SCRIPT}"
  chmod 755 "${INSTALL_SCRIPT}"

  cat > "${SERVICE_FILE}" <<EOF
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
fi

###############################################################################
# Final Summary
###############################################################################
echo "==============================================================="
echo "All Done."
echo "==============================================================="
echo
echo "******************** Summary ********************"
echo " Timezone:        ${TIMEZONE_MSG}"
echo " RMM:             ${RMM_MSG}"
echo " Nx Witness:      ${NX_MSG}"
echo " USB Log Manager: ${USB_MSG}"
if [[ "$IS_CONTAINER" = true ]]; then
  echo " Tailscale:       SKIPPED: Container environment"
else
  echo " Tailscale:       ${TAILSCALE_MSG}"
fi
echo "*************************************************"
