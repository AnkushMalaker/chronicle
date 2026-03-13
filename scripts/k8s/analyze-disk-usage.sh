#!/bin/bash

# Script to analyze disk space usage on the configured node
# Helps identify where disk space is being consumed
#
# Usage: ./analyze-disk-usage.sh [pass=PASSWORD]
# Example: ./analyze-disk-usage.sh pass=mysudopassword

set -e

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-env.sh"

# Get the node name from config
NODE_NAME="${SPEAKER_NODE:-}"
if [ -z "$NODE_NAME" ]; then
    echo "❌ Error: SPEAKER_NODE not configured in config.env"
    echo "Please set SPEAKER_NODE in your config.env file"
    exit 1
fi

# Parse password parameter
SUDO_PASSWORD=""
if [[ $1 == pass=* ]]; then
    SUDO_PASSWORD="${1#pass=}"
    echo "🔑 Using provided password for sudo commands"
fi

echo "💾 Analyzing disk space usage on $NODE_NAME..."

# Function to run SSH command with or without password
run_ssh_command() {
    local cmd="$1"
    local use_sudo="${2:-false}"

    if [[ -n "$SUDO_PASSWORD" && "$use_sudo" == "true" ]]; then
        echo "$SUDO_PASSWORD" | ssh "$NODE_NAME" "sudo -S $cmd" 2>/dev/null
    elif [[ "$use_sudo" == "true" ]]; then
        ssh -t "$NODE_NAME" "sudo $cmd"
    else
        ssh "$NODE_NAME" "$cmd"
    fi
}

echo ""
echo "📊 Overall disk usage:"
run_ssh_command "df -h"

echo ""
echo "🔍 Top 10 largest directories (this may take a moment):"
if [[ -n "$SUDO_PASSWORD" ]]; then
    echo "$SUDO_PASSWORD" | ssh "$NODE_NAME" "sudo -S du -h --max-depth=2 / 2>/dev/null | sort -hr | head -20"
else
    echo "   Note: You may be prompted for sudo password to scan system directories"
    ssh -t "$NODE_NAME" "sudo du -h --max-depth=2 / 2>/dev/null | sort -hr | head -20"
fi

echo ""
echo "🐳 Docker/Container related storage:"

# Check Docker system usage
echo "📦 Docker system usage:"
run_ssh_command "docker system df" false || echo "   Docker command not available or failed"

echo ""
echo "🖼️  Docker images space usage:"
run_ssh_command "docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}' | head -10" false || echo "   Docker images command failed"

echo ""
echo "📁 Container/Docker directories:"
run_ssh_command "du -sh /var/lib/docker 2>/dev/null" true || echo "   Could not access /var/lib/docker"
run_ssh_command "du -sh /var/lib/containerd 2>/dev/null" true || echo "   Could not access /var/lib/containerd"
run_ssh_command "du -sh /var/snap/microk8s 2>/dev/null" true || echo "   Could not access /var/snap/microk8s"

echo ""
echo "🗂️  MicroK8s specific storage:"
run_ssh_command "du -sh /var/snap/microk8s/common/var/lib/containerd 2>/dev/null" true || echo "   Could not access microk8s containerd"
run_ssh_command "du -sh /var/snap/microk8s/current/var/kubernetes/storage 2>/dev/null" true || echo "   Could not access microk8s storage"

echo ""
echo "📝 Log files:"
run_ssh_command "du -sh /var/log 2>/dev/null" true || echo "   Could not access /var/log"
run_ssh_command "du -sh /var/snap/microk8s/current/var/log 2>/dev/null" true || echo "   Could not access microk8s logs"

echo ""
echo "🧹 Temporary and cache directories:"
run_ssh_command "du -sh /tmp /var/tmp 2>/dev/null" false || echo "   Could not access temp directories"
run_ssh_command "du -sh ~/.cache ~/.local 2>/dev/null" false || echo "   Could not access user cache"

echo ""
echo "💿 Snap packages:"
run_ssh_command "du -sh /var/lib/snapd 2>/dev/null" true || echo "   Could not access snapd storage"
run_ssh_command "snap list --all" false || echo "   Snap command failed"

echo ""
echo "🎯 Registry storage (if using local registry):"
run_ssh_command "du -sh /var/lib/registry 2>/dev/null" true || echo "   No local registry storage found"

echo ""
echo "📊 Summary - Largest space consumers found:"
echo "Run 'df -h' and 'du -sh /*' on $NODE_NAME for detailed analysis"
echo ""
echo "💡 Common cleanup targets:"
echo "   - Docker images: docker system prune -a"
echo "   - Docker containers: docker container prune"
echo "   - Docker volumes: docker volume prune"
echo "   - MicroK8s images: microk8s ctr images rm <image>"
echo "   - Log files: journalctl --vacuum-time=7d"
echo "   - Snap packages: snap remove --purge <package>"
