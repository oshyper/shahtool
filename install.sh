#!/data/data/com.termux/files/usr/bin/bash
# Shahtool installer for Termux.
# Usage: curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/Shahtool/main/install.sh | bash
set -euo pipefail

REPO="${SHAHTOOL_REPO:-YOUR_GITHUB_USERNAME/Shahtool}"
APP_DIR="${SHAHTOOL_DIR:-$HOME/.shahtool/app}"
R='\033[0;31m'; G='\033[0;32m'; I='\033[0;90m'; N='\033[0m'
LOG="$(mktemp)"

step() {  # step "message" command...
    local msg="$1"; shift
    echo -e "${I}[..]${N} $msg..."
    if "$@" >"$LOG" 2>&1; then
        echo -e "     └─> ${G}[OK]${N}\n"
    else
        echo -e "     └─> ${R}[FAILED]${N}"
        echo -e "${R}Last lines of the error:${N}"; tail -n 15 "$LOG"; exit 1
    fi
}

if [ -z "${PREFIX:-}" ] || [[ "$PREFIX" != *com.termux* ]]; then
    echo "This installer is for Termux. On a PC use: pip install git+https://github.com/$REPO.git"; exit 1
fi
if [ ! -d "$HOME/storage" ]; then
    echo -e "\nRun this first, then run the installer again:\n  termux-setup-storage\n"; exit 1
fi
if [ -z "${SHAHTOOL_SKIP_API_CHECK:-}" ] && \
   ! cmd package list packages --user 0 com.termux.api </dev/null 2>/dev/null | grep -q com.termux.api; then
    echo -e "\nThe Termux:API app is not installed. Install it first (needed for USB access).\n"; exit 1
fi
case "$(dpkg --print-architecture)" in
    aarch64|arm) ;;
    *) echo "This architecture is not supported"; exit 1 ;;
esac

export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o Dpkg::Options::=--force-confold)
step "Updating package lists"        apt-get update "${APT_OPTS[@]}"
step "Upgrading packages"            apt-get upgrade "${APT_OPTS[@]}"
step "Installing python, git, libusb" pkg install "${APT_OPTS[@]}" python git libusb
step "Installing archive tools"      pkg install "${APT_OPTS[@]}" p7zip unrar unzip tar
step "Installing termux-adb and termux-fastboot" bash -c 'curl -fsS https://raw.githubusercontent.com/nohajc/termux-adb/master/install.sh | bash'
step "Linking fastboot and adb"      bash -c 'ln -sf "$PREFIX/bin/termux-fastboot" "$PREFIX/bin/fastboot" && ln -sf "$PREFIX/bin/termux-adb" "$PREFIX/bin/adb"'

if [ -d "$APP_DIR/.git" ]; then
    step "Updating Shahtool" git -C "$APP_DIR" pull --ff-only
else
    mkdir -p "$(dirname "$APP_DIR")"
    step "Downloading Shahtool" git clone --depth 1 "https://github.com/$REPO.git" "$APP_DIR"
fi

cat > "$PREFIX/bin/shahtool" <<LAUNCH
#!$PREFIX/bin/bash
export PYTHONPATH="$APP_DIR\${PYTHONPATH:+:\$PYTHONPATH}"
exec python3 -m shahtool "\$@"
LAUNCH
chmod +x "$PREFIX/bin/shahtool"
ln -sf "$PREFIX/bin/shahtool" "$PREFIX/bin/ritiktool" 2>/dev/null || true
rm -f "$LOG"

echo -e "${G}✔ Installed.${N}"
echo -e "  Terminal menu : ${G}shahtool${N}"
echo -e "  Web GUI       : ${G}shahtool gui${N}  (then open the link in your browser)\n"
