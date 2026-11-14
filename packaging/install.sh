#!/bin/bash
# Universal installer for Lost Island (Linux).
#   from a checkout:  sudo ./packaging/install.sh [--autostart]
#   from anywhere:    curl -fsSL https://raw.githubusercontent.com/Mariakevin/lost-island-next/main/packaging/install.sh | sudo bash
set -euo pipefail

REPO="https://github.com/Mariakevin/lost-island-next"
PREFIX=/usr/local

say() { printf '\033[1;33m[lost-island]\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { say "run me with sudo"; exit 1; }

# ---- dependencies ----------------------------------------------------------
if command -v pacman >/dev/null; then
  pacman -S --noconfirm --needed alsa-utils python python-gobject python-cairo gtk4 \
    gtk4-layer-shell libadwaita libpulse
elif command -v apt-get >/dev/null; then
  apt-get update
  apt-get install -y alsa-utils python3 python3-gi python3-gi-cairo gir1.2-gtk-4.0 \
    gir1.2-adw-1 pulseaudio-utils
  if apt-cache show libgtk4-layer-shell0 2>/dev/null \
    | grep -F -x 'Package: libgtk4-layer-shell0' >/dev/null; then
    apt-get install -y libgtk4-layer-shell0
  else
    say "libgtk4-layer-shell0 is unavailable; the island will use its floating-window fallback"
  fi
elif command -v dnf >/dev/null; then
  dnf install -y alsa-utils python3 python3-gobject python3-cairo gtk4 gtk4-layer-shell \
    libadwaita pulseaudio-utils
elif command -v zypper >/dev/null; then
  zypper install -y python3 python3-gobject python3-cairo gtk4 \
    alsa-utils gtk4-layer-shell libadwaita pulseaudio-utils
else
  say "unknown distro — install GTK4, gtk4-layer-shell, libadwaita and PyGObject yourself, then re-run"
fi

if ! command -v python3 >/dev/null 2>&1; then
  say "Python 3 is required but was not found"
  exit 1
fi
PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:3])))')
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))'; then
  say "Python 3.11 or newer is required; found $PYTHON_VERSION"
  exit 1
fi

# ---- source ----------------------------------------------------------------
SRC=$(cd "$(dirname "${BASH_SOURCE[0]:-.}")/.." 2>/dev/null && pwd || true)
if [ ! -f "$SRC/lostisland/app.py" ]; then
  say "fetching sources"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  git clone --depth 1 "$REPO" "$TMP/lost-island"
  SRC="$TMP/lost-island"
fi

# ---- install ---------------------------------------------------------------
say "installing to $PREFIX"
rm -rf "$PREFIX/lib/lost-island"
mkdir -p "$PREFIX/lib/lost-island" "$PREFIX/bin"
cp -r "$SRC/lostisland" "$PREFIX/lib/lost-island/"
find "$PREFIX/lib/lost-island" -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

cat > "$PREFIX/bin/lost-island" <<EOF
#!/bin/sh
PYTHONPATH=$PREFIX/lib/lost-island exec python3 -m lostisland "\$@"
EOF
chmod 755 "$PREFIX/bin/lost-island"

install -Dm644 "$SRC/data/lost-island.desktop" /usr/share/applications/lost-island.desktop
install -Dm644 "$SRC/data/lost-island.svg" /usr/share/icons/hicolor/scalable/apps/lost-island.svg
install -Dm644 "$SRC/data/lost-island-mic-led.conf" \
  /usr/lib/modules-load.d/90-lost-island-mic-led.conf
install -Dm644 "$SRC/data/99-lost-island-mic-led.rules" \
  /usr/lib/udev/rules.d/99-lost-island-mic-led.rules
if command -v modprobe >/dev/null 2>&1 && ! modprobe snd_ctl_led; then
  say "could not load snd_ctl_led now; the module-load setting will retry at boot"
fi
if command -v udevadm >/dev/null 2>&1 && [ -S /run/udev/control ]; then
  udevadm control --reload-rules
  udevadm trigger --action=add --subsystem-match=leds --sysname-match='hda::micmute'
else
  say "udev is not running; reboot after installing to apply the microphone LED rule"
fi
sed "s|/usr/bin/lost-island|$PREFIX/bin/lost-island|" \
  "$SRC/data/lost-island.service" > /usr/lib/systemd/user/lost-island.service

say "installed. start with: lost-island"
if [ "${1:-}" = "--autostart" ]; then
  U=${SUDO_USER:-}
  if [ -n "$U" ]; then
    USER_ID=$(id -u "$U")
    USER_RUNTIME="/run/user/$USER_ID"
    if [ -S "$USER_RUNTIME/bus" ]; then
      if runuser -u "$U" -- env \
        XDG_RUNTIME_DIR="$USER_RUNTIME" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=$USER_RUNTIME/bus" \
        systemctl --user enable --now lost-island.service; then
        say "autostart enabled for $U"
      else
        say "could not finish autostart setup for $U; run 'systemctl --user enable --now lost-island.service' in that user's session"
      fi
    else
      say "no active user session found; run 'systemctl --user enable --now lost-island.service' from $U's session to enable autostart"
    fi
  else
    say "no invoking user was detected; run 'systemctl --user enable --now lost-island.service' from your session to enable autostart"
  fi
else
  say "autostart: systemctl --user enable --now lost-island.service"
fi
