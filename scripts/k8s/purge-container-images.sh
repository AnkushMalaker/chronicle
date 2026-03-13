#!/bin/bash

# Script to purge unused container images from the local container runtime
# Only removes images that are not currently being used by any pods or deployments
#
# Usage: ./purge-container-images.sh [pass=PASSWORD]
# Example: ./purge-container-images.sh pass=mysudopassword

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

echo "🔍 Analyzing container images..."

# Create temporary files
ALL_IMAGES_FILE="/tmp/all_container_images.txt"
USED_IMAGES_FILE="/tmp/used_images.txt"
UNUSED_IMAGES_FILE="/tmp/unused_container_images.txt"

# Clean up temp files on exit
trap 'rm -f "$ALL_IMAGES_FILE" "$USED_IMAGES_FILE" "$UNUSED_IMAGES_FILE"' EXIT

echo "📋 Getting all container images from $NODE_NAME..."

# Get all container images from the configured node using microk8s ctr
if [[ -n "$SUDO_PASSWORD" ]]; then
    # Use password with sudo -S
    if echo "$SUDO_PASSWORD" | ssh "$NODE_NAME" "sudo -S microk8s ctr images ls" | awk 'NR>1 {print $1}' > "$ALL_IMAGES_FILE" 2>/dev/null; then
        echo "   Using $NODE_NAME microk8s ctr images (with provided password)"
        echo "   Found $(wc -l < "$ALL_IMAGES_FILE") images on $NODE_NAME"
    else
        echo "   ❌ Could not access microk8s container runtime on $NODE_NAME with provided password."
        exit 1
    fi
else
    # Interactive sudo
    echo "   Note: You may be prompted for your sudo password on $NODE_NAME"
    if ssh -t "$NODE_NAME" "sudo microk8s ctr images ls" | awk 'NR>1 {print $1}' > "$ALL_IMAGES_FILE"; then
        echo "   Using $NODE_NAME microk8s ctr images (with sudo)"
        echo "   Found $(wc -l < "$ALL_IMAGES_FILE") images on $NODE_NAME"
    else
        echo "   ❌ Could not access microk8s container runtime on $NODE_NAME."
        echo "   Make sure you can run: ssh $NODE_NAME 'sudo microk8s ctr images ls'"
        echo "   Or try: ./purge-container-images.sh pass=yourpassword"
        exit 1
    fi
fi

if [ ! -s "$ALL_IMAGES_FILE" ]; then
    echo "❌ No container images found"
    exit 1
fi

echo "🎯 Identifying currently used images..."

# Get images used by running pods
kubectl get pods --all-namespaces -o jsonpath='{range .items[*]}{.spec.containers[*].image}{"\n"}{end}' | \
    grep -v "^$" | sort | uniq > "$USED_IMAGES_FILE"

# Get images used by deployments (including those not currently running)
kubectl get deployments --all-namespaces -o jsonpath='{range .items[*]}{.spec.template.spec.containers[*].image}{"\n"}{end}' | \
    grep -v "^$" | sort | uniq >> "$USED_IMAGES_FILE"

# Get images used by daemonsets
kubectl get daemonsets --all-namespaces -o jsonpath='{range .items[*]}{.spec.template.spec.containers[*].image}{"\n"}{end}' | \
    grep -v "^$" | sort | uniq >> "$USED_IMAGES_FILE"

# Get images used by statefulsets
kubectl get statefulsets --all-namespaces -o jsonpath='{range .items[*]}{.spec.template.spec.containers[*].image}{"\n"}{end}' | \
    grep -v "^$" | sort | uniq >> "$USED_IMAGES_FILE"

# Remove duplicates from used images
sort "$USED_IMAGES_FILE" | uniq > "${USED_IMAGES_FILE}.tmp" && mv "${USED_IMAGES_FILE}.tmp" "$USED_IMAGES_FILE"

echo "🔍 Finding unused container images..."

# Find images that are in container runtime but not used
comm -23 <(sort "$ALL_IMAGES_FILE") <(sort "$USED_IMAGES_FILE") > "$UNUSED_IMAGES_FILE"

UNUSED_COUNT=$(wc -l < "$UNUSED_IMAGES_FILE")
TOTAL_COUNT=$(wc -l < "$ALL_IMAGES_FILE")
USED_COUNT=$(wc -l < "$USED_IMAGES_FILE")

echo "📊 Container Image Analysis Results:"
echo "   Total container images: $TOTAL_COUNT"
echo "   Currently used images: $USED_COUNT"
echo "   Unused container images: $UNUSED_COUNT"

if [ "$UNUSED_COUNT" -eq 0 ]; then
    echo "✅ No unused container images found. Nothing to purge."
    exit 0
fi

echo ""
echo "🗑️  Unused container images to be purged:"
cat "$UNUSED_IMAGES_FILE" | while read -r image; do
    echo "   - $image"
done

echo ""
read -p "❓ Do you want to proceed with purging these $UNUSED_COUNT unused container images? (y/N): " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "❌ Container purge cancelled by user."
    exit 0
fi

echo "🧹 Purging unused container images..."

PURGED_COUNT=0
FAILED_COUNT=0

while IFS= read -r image || [[ -n "$image" ]]; do
    echo "   Removing container image: $image"

    # Use microk8s ctr to remove the image
    if [[ -n "$SUDO_PASSWORD" ]]; then
        # Use password with sudo -S
        if echo "$SUDO_PASSWORD" | ssh "$NODE_NAME" "sudo -S microk8s ctr images rm '$image'" 2>/dev/null; then
            ((PURGED_COUNT++))
            echo "   ✅ Successfully removed container image: $image"
        else
            ((FAILED_COUNT++))
            echo "   ❌ Failed to remove container image: $image"
        fi
    else
        # Interactive sudo
        if ssh -t "$NODE_NAME" "sudo microk8s ctr images rm '$image'" 2>/dev/null; then
            ((PURGED_COUNT++))
            echo "   ✅ Successfully removed container image: $image"
        else
            ((FAILED_COUNT++))
            echo "   ❌ Failed to remove container image: $image"
        fi
    fi
done < "$UNUSED_IMAGES_FILE"

echo ""
echo "🎉 Container purge completed!"
echo "   Successfully purged: $PURGED_COUNT container images"
echo "   Failed to purge: $FAILED_COUNT container images"
echo "   Remaining container images: $((TOTAL_COUNT - PURGED_COUNT))"
