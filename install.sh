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
"$PY" -m pip install --quiet -r requirements.txt
echo "  ✓ Packages installed"

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

# 5. Install the always-on control panel + note about scheduling
PORT=8080
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
  systemctl --user enable --now frameart-gui.service
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
