#!/usr/bin/env bash
# Installs ACP as a supervised, boot-surviving systemd --user service, and
# (unless --no-claude-code is given) registers ACP's Claude Code host
# adapter via deploy/claude_code/install.py.
#
# Assumes this repository is checked out at $HOME/agent-compression-protocol
# and agent-api-lane-protocol is checked out as a sibling at
# $HOME/agent-api-lane-protocol (the unit file's %h-relative AALP_HOME
# assumes exactly that layout; run agent-api-lane-protocol/deploy/
# install.sh first so aalp.service exists for this unit's After=/Wants=).
#
# Idempotent: safe to re-run after a `git pull` that changes acp.service.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

if [ "$REPO_ROOT" != "$HOME/agent-compression-protocol" ]; then
    echo "warning: this checkout is at $REPO_ROOT, not \$HOME/agent-compression-protocol" >&2
    echo "         acp.service's %h-relative paths will not resolve correctly." >&2
fi

mkdir -p "$UNIT_DIR"
cp "$REPO_ROOT/deploy/acp.service" "$UNIT_DIR/acp.service"

systemctl --user daemon-reload
systemctl --user enable --now acp.service

echo
echo "acp.service installed and started:"
systemctl --user status acp.service --no-pager -l | head -8

if [ "$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null)" != "yes" ]; then
    echo
    echo "note: user lingering is not enabled, so acp.service will stop at logout"
    echo "      and not start on boot. To fix: sudo loginctl enable-linger $(id -un)"
fi

if [ "${1:-}" != "--no-claude-code" ]; then
    echo
    echo "Registering the Claude Code host adapter (mcpServers + Bash deny):"
    python3 "$REPO_ROOT/deploy/claude_code/install.py"
fi
