#!/usr/bin/env bash
# The Frame Machine — one-step installer for macOS and Linux.
#   ./install.sh
set -e
cd "$(dirname "$0")"
DIR="$(pwd)"
echo
echo "  🖼   The Frame Machine installer"
echo "  ────────────────────────────────"

# 1. Python
if ! command -v python3 >/dev/null 2>&1; then
  echo "  ✗ Python 3 isn't installed. Install it (macOS: 'brew install python', or python.org) and re-run."
  exit 1
fi
PY="$(command -v python3)"
echo "  ✓ Python: $PY"

# 2. Dependencies
echo "  • Installing Python packages (this can take a minute)…"
"$PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
# Debian 12 / Ubuntu 24 / Raspberry Pi OS Bookworm mark the system Python "externally managed"
# (PEP 668) and refuse a plain install; retry with the documented opt-out before giving up.
"$PY" -m pip install --quiet -r requirements.txt \
  || "$PY" -m pip install --quiet --break-system-packages -r requirements.txt
echo "  ✓ Packages installed"

# 2b. On Linux, make sure a serif font exists (the placard needs one; macOS has Georgia).
#     `**` isn't recursive without globstar, so match with find instead.
if [ "$(uname)" != "Darwin" ]; then
  if ! find /usr/share/fonts \( -name 'LiberationSerif-Regular.ttf' -o -name 'DejaVuSerif.ttf' \) \
         -print -quit 2>/dev/null | grep -q . \
     && ! fc-list 2>/dev/null | grep -qiE "liberation serif|dejavu serif"; then
    echo "  • No serif font found — installing one for the captions…"
    (sudo apt-get install -y fonts-liberation >/dev/null 2>&1) \
      || echo "  ⚠ Couldn't auto-install. For nicer captions run: sudo apt install fonts-liberation"
  fi
fi

# 2c. TV discovery pings the subnet and reads the ARP table to find the TV by MAC. net-tools
#     is no longer installed by default on Debian-family systems, and without it the push just
#     reports "Frame not found" forever.
MISSING=""
for TOOL in ping arp; do
  command -v "$TOOL" >/dev/null 2>&1 || MISSING="$MISSING $TOOL"
done
if [ -n "$MISSING" ]; then
  echo "  • Missing network tools ($MISSING) — installing them…"
  (sudo apt-get install -y iputils-ping net-tools >/dev/null 2>&1) || true
  for TOOL in ping arp; do
    command -v "$TOOL" >/dev/null 2>&1 \
      || echo "  ⚠ No '$TOOL'. The TV can't be found by MAC until you install it (sudo apt install iputils-ping net-tools)."
  done
fi

# 3. TV MAC address
CFG="$HOME/.config/frame"; mkdir -p "$CFG"
EXISTING_MAC="$("$PY" - <<'PY' 2>/dev/null || true
import json,os
p=os.path.expanduser("~/.config/frame/config.json")
print(json.load(open(p)).get("mac","") if os.path.exists(p) else "")
PY
)"
if [ -n "$EXISTING_MAC" ]; then
  echo "  ✓ Using saved TV MAC: $EXISTING_MAC"
  MAC="$EXISTING_MAC"
else
  echo
  echo "  Find your Frame's WIRELESS MAC on the TV: Settings → General/Support → About This TV"
  echo "  (or in your router's device list). It looks like  a0:d0:5b:12:34:56"
  read -r -p "  Enter the TV's wireless MAC: " MAC
fi

# 4. Write config
"$PY" - "$MAC" <<'PY'
import json,os,sys
p=os.path.expanduser("~/.config/frame/config.json")
c=json.load(open(p)) if os.path.exists(p) else {}
c["mac"]=sys.argv[1].strip()
json.dump(c,open(p,"w"),indent=2)
print("  ✓ Saved settings to ~/.config/frame/config.json")
PY

# 4b. Optional: the AI "made-up caption" voices need an Anthropic API key.
KEYFILE="$CFG/anthropic_key.txt"
if [ -s "$KEYFILE" ]; then
  echo "  ✓ Anthropic key already set — made-up captions enabled"
else
  echo
  echo "  The fun 'made-up caption' voices (pirate, noir, topical…) are written by Claude and"
  echo "  need an Anthropic API key — a few cents a month. You can skip and add it later."
  printf "  Set one up now? [y/N] "; read -r WANTKEY
  case "$WANTKEY" in
    y|Y)
      echo "    1. Open  https://console.anthropic.com/settings/keys  and sign in (or sign up)."
      echo "    2. Click 'Create Key', give it a name, then copy the key (it starts with sk-ant-)."
      printf "    Paste your key (it won't display): "; read -rs KEY; echo
      if [ -n "$KEY" ]; then
        (umask 077; printf '%s' "$KEY" > "$KEYFILE")
        echo "  ✓ Key saved to $KEYFILE — made-up captions enabled."
      else
        echo "  • No key entered — skipping. Captions will use the museum's real text."
      fi ;;
    *)
      echo "  • Skipped. Add a key any time by putting it in $KEYFILE" ;;
  esac
fi

# 4c. Optional: Unsplash photography needs a free access key (Wikimedia and NASA don't).
UKEYFILE="$CFG/unsplash_key.txt"
if [ -s "$UKEYFILE" ]; then
  echo "  ✓ Unsplash key already set — Unsplash photos enabled"
else
  echo
  echo "  Modern art comes from Wikimedia and NASA with no setup. Unsplash (the best-looking"
  echo "  photography) needs a free access key. You can skip and add it later in the panel."
  printf "  Set one up now? [y/N] "; read -r WANTUKEY
  case "$WANTUKEY" in
    y|Y)
      echo "    1. Open  https://unsplash.com/developers  and sign in (or sign up)."
      echo "    2. Click 'New Application', accept the terms, name it, then copy its Access Key."
      printf "    Paste the Access Key (it won't display): "; read -rs UKEY; echo
      if [ -n "$UKEY" ]; then
        (umask 077; printf '%s' "$UKEY" > "$UKEYFILE")
        echo "  ✓ Key saved to $UKEYFILE — Unsplash photos enabled."
      else
        echo "  • No key entered — skipping. The modern mix uses Wikimedia and NASA."
      fi ;;
    *)
      echo "  • Skipped. Add a key any time in the panel (Source → Unsplash access key)." ;;
  esac
fi

# 5. Install the always-on control panel + note about scheduling
# The panel's port: PORT=… on the command line wins; otherwise a re-run keeps the port the
# installed service already uses (8080 is often taken, e.g. by a Docker container, and a
# re-run must not move the panel onto it); a first install uses 8080.
saved_port() {
  "$PY" - <<'PY' 2>/dev/null || true
import os, plistlib, re
home = os.path.expanduser("~")
args = []
try:
    with open(os.path.join(home, "Library/LaunchAgents/com.frameart.gui.plist"), "rb") as f:
        args = [str(a) for a in plistlib.load(f).get("ProgramArguments") or []]
except Exception:
    try:
        with open(os.path.join(home, ".config/systemd/user/frameart-gui.service")) as f:
            line = next((l for l in f if l.startswith("ExecStart=")), "")
        args = line.split()
    except Exception:
        pass
port = next((args[i + 1] for i, a in enumerate(args[:-1]) if a == "--port"), "")
m = re.fullmatch(r"\d{1,5}", port.strip())
print(port.strip() if m and 0 < int(port) < 65536 else "")
PY
}
if [ -n "${PORT:-}" ]; then
  case "$PORT" in
    *[!0-9]*) echo "  ✗ PORT must be a number, got '$PORT'."; exit 1 ;;
  esac
  echo "  ✓ Panel port: $PORT (from PORT=)"
else
  PORT="$(saved_port)"
  if [ -n "$PORT" ]; then
    echo "  ✓ Panel port: $PORT (kept from the installed panel)"
  else
    PORT=8080
    echo "  ✓ Panel port: $PORT (run with PORT=… to choose another)"
  fi
fi
OS="$(uname)"
if [ "$OS" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.frameart.gui.plist"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.frameart.gui</string>
  <key>ProgramArguments</key>
  <array><string>$PY</string><string>$DIR/app.py</string><string>--port</string><string>$PORT</string></array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>KeepAlive</key><true/><key>RunAtLoad</key><true/>
  <key>EnvironmentVariables</key><dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>StandardOutPath</key><string>$HOME/Library/Logs/frameart-gui.log</string>
  <key>StandardErrorPath</key><string>$HOME/Library/Logs/frameart-gui.log</string>
</dict></plist>
EOF
  launchctl bootout "gui/$(id -u)/com.frameart.gui" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "  ✓ Control panel installed as a background service"
  HOST="$(scutil --get LocalHostName 2>/dev/null || hostname -s)"
elif command -v systemctl >/dev/null 2>&1; then
  UNIT="$HOME/.config/systemd/user"; mkdir -p "$UNIT"
  cat > "$UNIT/frameart-gui.service" <<EOF
[Unit]
Description=The Frame Machine control panel
[Service]
ExecStart=$PY $DIR/app.py --port $PORT
WorkingDirectory=$DIR
Restart=always
[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable frameart-gui.service
  # restart, not just start: on a re-run (an update) the old panel would keep running old code
  systemctl --user restart frameart-gui.service
  echo "  ✓ Control panel installed as a systemd --user service"
  HOST="$(hostname -s)"
else
  echo "  • Auto-start not configured for this OS. Start the panel yourself with:"
  echo "      $PY $DIR/app.py --port $PORT"
  HOST="$(hostname -s 2>/dev/null || echo localhost)"
fi

echo
echo "  ✅ Done! Open the control panel:"
echo "        http://$HOST.local:$PORT      (or http://localhost:$PORT on this machine)"
echo
echo "  First time only:"
echo "    1. Make sure the TV is ON, then click “Change the art now”."
echo "       The TV shows an “Allow” prompt the first time — accept it."
echo "    2. Choose how often it changes and click “Save settings”."
echo
