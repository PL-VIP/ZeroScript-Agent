#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
# MacOS_Start.command
# ---------------------------------------------------------------------------
#  Self-contained macOS/Linux launcher for the ZeroScript bridge.
#  Double-clickable in Finder (that is why it is a .command, not a .sh: a .sh
#  opens in an editor instead of running). It has NO dependency on start.py -
#  it finds Python, ensures the one required library is installed, frees a
#  previous bridge still holding the port, then runs agent_bridge.py itself.
#  The Windows equivalent is start.bat.
# ---------------------------------------------------------------------------
set -u

# Run from the folder this script lives in, so agent_bridge.py is found regardless of
# where Finder launched it from.
cd "$(dirname "$0")" || exit 1

BRIDGE_PORT="${ZS_BRIDGE_PORT:-17613}"
LOG_DIR="logs"
LOG_FILE="$LOG_DIR/start.log"
mkdir -p "$LOG_DIR" 2>/dev/null

log() {
    # Best-effort append; never abort the launch if logging fails.
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >>"$LOG_FILE" 2>/dev/null || true
}

pause_and_exit() {
    # A double-clicked .command opens a fresh Terminal window; without this the
    # window closes instantly and any error scrolls away unread.
    echo
    read -n 1 -s -r -p "Press any key to close this window..."
    echo
    exit "${1:-0}"
}

echo
echo "  === ZeroScript Bridge ==="
echo
log "===== MacOS_Start.command launched ====="

# --- 0. agent_bridge.py must be next to us ---------------------------------
# If the user ran this out of a half-extracted download, agent_bridge.py is missing
# and Python would fail with a confusing error. Say the real thing instead.
if [ ! -f "agent_bridge.py" ]; then
    echo "  ERROR: agent_bridge.py was not found next to this launcher."
    echo "  Extract the WHOLE download, then run MacOS_Start.command from that folder."
    log "FATAL: agent_bridge.py missing next to launcher."
    pause_and_exit 1
fi

# --- 1. Find a usable Python (3.10+ with a working pip) ---------------------
echo "  [1/3] Looking for Python 3.10+..."
PY=""
py_ok() {
    command -v "$1" >/dev/null 2>&1 || return 1
    "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1
}
for cand in python3 python; do
    if py_ok "$cand"; then
        PY="$cand"
        break
    fi
done

if [ -z "$PY" ]; then
    echo "  ERROR: Python 3.10 or newer was not found."
    echo "  Install it from https://www.python.org/downloads/ then run this again."
    echo "  (On macOS you can also run:  xcode-select --install)"
    log "FATAL: no Python 3.10+ found."
    pause_and_exit 1
fi
PY_VER="$("$PY" --version 2>&1)"
echo "        Found: $PY ($PY_VER)"
log "Python found: $PY ($PY_VER)"

# --- 2. Ensure the websockets library is installed --------------------------
echo "  [2/3] Checking websockets library..."
if ! "$PY" -c "import websockets" >/dev/null 2>&1; then
    echo "        Installing websockets - first time only..."
    log "websockets missing, installing via pip --user."
    if ! "$PY" -m pip install --user "websockets>=14,<18" >/dev/null 2>&1; then
        # Newer macOS Pythons (Homebrew / python.org) mark the environment as
        # externally managed (PEP 668) and refuse --user. Retry allowing it,
        # since this is the user's own machine and a single small pure-Python
        # dependency, not a system package.
        log "pip --user failed; retrying with --break-system-packages."
        "$PY" -m pip install --user --break-system-packages "websockets>=14,<18" >/dev/null 2>&1 || true
    fi
    if ! "$PY" -c "import websockets" >/dev/null 2>&1; then
        echo
        echo "  ERROR: could not install the 'websockets' library automatically."
        echo "  Install it yourself, then run this again:"
        echo "      $PY -m pip install --user 'websockets>=14,<18'"
        log "FATAL: websockets install failed."
        pause_and_exit 1
    fi
fi
echo "        OK"
log "websockets library OK."

# --- 3. Refuse to replace an unknown process holding the port ---------------
echo "  [3/3] Starting bridge..."
# Never kill an arbitrary process just because it owns the configured port.
if command -v lsof >/dev/null 2>&1; then
    OLD_PID="$(lsof -ti "tcp:$BRIDGE_PORT" -s TCP:LISTEN 2>/dev/null | head -n1)"
    if [ -n "${OLD_PID:-}" ]; then
        echo "  ERROR: port $BRIDGE_PORT is already used by pid $OLD_PID."
        echo "  Close the previous ZeroScript Agent terminal or inspect that process,"
        echo "  then run this launcher again. Nothing was terminated."
        log "Port $BRIDGE_PORT already held by pid $OLD_PID; refused to kill it."
        pause_and_exit 1
    fi
fi

echo
echo "  ############################################################"
echo "  ##   KEEP THIS WINDOW OPEN - DO NOT CLOSE IT              ##"
echo "  ##   ZeroScript stops working if you close it. Just       ##"
echo "  ##   minimize this window and leave it running.           ##"
echo "  ############################################################"
echo
log "Launching agent_bridge.py with $PY"

if [ -n "${1:-}" ]; then
    echo "  Workspace: $1"
    "$PY" agent_bridge.py --workspace "$1"
else
    echo "  Workspace: config.json (default: this repository)"
    "$PY" agent_bridge.py
fi
BRIDGE_EXIT=$?
log "agent_bridge.py exited with code $BRIDGE_EXIT"

echo
if [ "$BRIDGE_EXIT" -ne 0 ]; then
    echo "  Bridge stopped with ERROR code $BRIDGE_EXIT - scroll up for the Python"
    echo "  error and include this whole window in any bug report (logs/start.log)."
else
    echo "  Bridge stopped normally."
fi
pause_and_exit "$BRIDGE_EXIT"
