#!/usr/bin/env bash
#
# Copy the integration to a Home Assistant OS instance over SSH and restart core.
#
# Requires the "Advanced SSH & Web Terminal" add-on with key authentication.
# Works from Git Bash on Windows as well as Linux/macOS - it only uses ssh, tar
# and standard shell builtins, so nothing extra needs installing on either end.
#
# Configure with environment variables (or a .env.deploy file beside this
# script, which is gitignored):
#
#   HAOS_HOST      hostname or IP of the Home Assistant instance   (required)
#   HAOS_USER      SSH user                                        (default: root)
#   HAOS_PORT      SSH port                                        (default: 22)
#   HAOS_CONFIG    config directory on the instance                (default: autodetect)
#
# Usage:
#   scripts/deploy.sh                # copy and restart Home Assistant Core
#   scripts/deploy.sh --no-restart   # copy only
#   scripts/deploy.sh --logs         # copy, restart, then follow the log

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE_DIR="$REPO_ROOT/custom_components/butterflymx"

# shellcheck source=/dev/null
[ -f "$REPO_ROOT/scripts/.env.deploy" ] && . "$REPO_ROOT/scripts/.env.deploy"

HAOS_USER="${HAOS_USER:-root}"
HAOS_PORT="${HAOS_PORT:-22}"

RESTART=1
FOLLOW_LOGS=0
for arg in "$@"; do
    case "$arg" in
        --no-restart) RESTART=0 ;;
        --logs) FOLLOW_LOGS=1 ;;
        # Print the header block, stopping at the first line that is not a
        # comment, rather than a line range that goes stale when it is edited.
        -h|--help)
            awk 'NR>1 && /^#/ {sub(/^# ?/, ""); print; next} NR>1 {exit}' \
                "${BASH_SOURCE[0]}"
            exit 0
            ;;
        *) echo "unknown argument: $arg" >&2; exit 2 ;;
    esac
done

if [ -z "${HAOS_HOST:-}" ]; then
    echo "error: set HAOS_HOST (e.g. HAOS_HOST=homeassistant.local scripts/deploy.sh)" >&2
    echo "       or create scripts/.env.deploy with HAOS_HOST=..." >&2
    exit 2
fi

if [ ! -d "$SOURCE_DIR" ]; then
    echo "error: $SOURCE_DIR not found" >&2
    exit 1
fi

ssh_cmd() {
    ssh -p "$HAOS_PORT" -o BatchMode=yes "$HAOS_USER@$HAOS_HOST" "$@"
}

echo "==> Connecting to $HAOS_USER@$HAOS_HOST:$HAOS_PORT"

# Newer Home Assistant OS mounts the config directory at /homeassistant and
# symlinks /config to it; older versions only have /config.
if [ -z "${HAOS_CONFIG:-}" ]; then
    HAOS_CONFIG="$(ssh_cmd 'if [ -d /homeassistant ]; then echo /homeassistant; elif [ -d /config ]; then echo /config; fi')"
    if [ -z "$HAOS_CONFIG" ]; then
        echo "error: could not find the config directory; set HAOS_CONFIG explicitly" >&2
        exit 1
    fi
fi

TARGET="$HAOS_CONFIG/custom_components/butterflymx"
echo "==> Deploying to $TARGET"

# Replace the directory outright so files deleted locally do not linger.
ssh_cmd "mkdir -p '$HAOS_CONFIG/custom_components' && rm -rf '$TARGET' && mkdir -p '$TARGET'"

# Stream the component over, excluding caches.
tar -C "$REPO_ROOT/custom_components" \
    --exclude='__pycache__' --exclude='*.pyc' \
    -czf - butterflymx \
    | ssh_cmd "tar -C '$HAOS_CONFIG/custom_components' -xzf -"

VERSION="$(sed -n 's/.*"version": *"\([^"]*\)".*/\1/p' "$SOURCE_DIR/manifest.json")"
FILE_COUNT="$(ssh_cmd "find '$TARGET' -type f | wc -l" | tr -d ' \r')"
echo "==> Copied $FILE_COUNT files (version $VERSION)"

if [ "$RESTART" -eq 1 ]; then
    echo "==> Restarting Home Assistant Core (this takes ~30s)"
    # A config-entry reload will not re-import changed Python modules, so a
    # full core restart is the only reliable way to pick up code changes.
    ssh_cmd 'ha core restart'
    echo "==> Restart requested"
else
    echo "==> Skipped restart; changes to .py files need one to take effect"
fi

if [ "$FOLLOW_LOGS" -eq 1 ]; then
    echo "==> Following core log (Ctrl-C to stop)"
    ssh_cmd 'ha core logs --follow' || true
fi
