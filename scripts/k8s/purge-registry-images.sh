#!/bin/bash

# Script to purge unused images from the Docker registry
# Only removes images that are not currently being used by any pods or deployments

set -e

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-env.sh"

echo "🔍 Analyzing registry images..."

# Get registry from config.env
REGISTRY="${CONTAINER_REGISTRY:-localhost:32000}"
echo "📦 Registry: $REGISTRY"

# Create temporary files
ALL_IMAGES_FILE="/tmp/all_registry_images.txt"
USED_IMAGES_FILE="/tmp/used_images.txt"
UNUSED_IMAGES_FILE="/tmp/unused_registry_images.txt"

# Clean up temp files on exit
trap 'rm -f "$ALL_IMAGES_FILE" "$USED_IMAGES_FILE" "$UNUSED_IMAGES_FILE"' EXIT

echo "📋 Getting all images from registry..."
# Get all images from the registry via Docker Registry HTTP API
{
  for repo in $(curl -s "http://$REGISTRY/v2/_catalog" | jq -r '.repositories[]'); do
    curl -s "http://$REGISTRY/v2/$repo/tags/list" | jq -r --arg repo "$repo" '.tags[]? | "\($repo):\(.)"'
  done
} | sed "s/^/$REGISTRY\//" > "$ALL_IMAGES_FILE"

if [ ! -s "$ALL_IMAGES_FILE" ]; then
    echo "❌ No images found in registry $REGISTRY"
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

echo "🔍 Finding unused registry images..."

# Find images that are in registry but not used
comm -23 <(sort "$ALL_IMAGES_FILE") <(sort "$USED_IMAGES_FILE") > "$UNUSED_IMAGES_FILE"

UNUSED_COUNT=$(wc -l < "$UNUSED_IMAGES_FILE")
TOTAL_COUNT=$(wc -l < "$ALL_IMAGES_FILE")
USED_COUNT=$(wc -l < "$USED_IMAGES_FILE")

echo "📊 Registry Image Analysis Results:"
echo "   Total images in registry: $TOTAL_COUNT"
echo "   Currently used images: $USED_COUNT"
echo "   Unused registry images: $UNUSED_COUNT"

if [ "$UNUSED_COUNT" -eq 0 ]; then
    echo "✅ No unused registry images found. Nothing to purge."
    exit 0
fi

echo ""
echo "🗑️  Unused registry images to be purged:"
cat "$UNUSED_IMAGES_FILE" | while read -r image; do
    echo "   - $image"
done

echo ""
read -p "❓ Do you want to proceed with purging these $UNUSED_COUNT unused registry images? (y/N): " -n 1 -r
echo

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo "❌ Registry purge cancelled by user."
    exit 0
fi

echo "🧹 Purging unused registry images..."

PURGED_COUNT=0
FAILED_COUNT=0

while IFS= read -r image || [[ -n "$image" ]]; do
    echo "   Removing from registry: $image"

    # Extract repository and tag from full image name (registry/repo:tag)
    repo_tag=$(echo "$image" | sed "s|^$REGISTRY/||")
    repo=$(echo "$repo_tag" | cut -d':' -f1)
    tag=$(echo "$repo_tag" | cut -d':' -f2)

    # Delete using Docker Registry API
    # First get the digest
    digest=$(curl -s -I -H "Accept: application/vnd.docker.distribution.manifest.v2+json" \
              "http://$REGISTRY/v2/$repo/manifests/$tag" | \
              grep -i docker-content-digest | cut -d' ' -f2 | tr -d '\r')

    if [[ -n "$digest" ]]; then
        if curl -s -X DELETE "http://$REGISTRY/v2/$repo/manifests/$digest" >/dev/null 2>&1; then
            ((PURGED_COUNT++))
            echo "   ✅ Successfully removed from registry: $image"
        else
            ((FAILED_COUNT++))
            echo "   ❌ Failed to remove from registry: $image"
        fi
    else
        ((FAILED_COUNT++))
        echo "   ❌ Failed to get digest for: $image"
    fi
done < "$UNUSED_IMAGES_FILE"

echo ""
echo "🎉 Registry purge completed!"
echo "   Successfully purged: $PURGED_COUNT registry images"
echo "   Failed to purge: $FAILED_COUNT registry images"
echo "   Remaining registry images: $((TOTAL_COUNT - PURGED_COUNT))"

if [ "$PURGED_COUNT" -gt 0 ]; then
    echo ""
    echo "🗑️  Running garbage collection to free disk space..."

    # Try to run garbage collection on the registry
    # This requires access to the registry container or filesystem
    NODE_NAME="${SPEAKER_NODE:-}"
    if [ -n "$NODE_NAME" ]; then
        if ssh "$NODE_NAME" "sudo microk8s kubectl exec -n kube-system deployment/registry -- registry garbage-collect /etc/docker/registry/config.yml" 2>/dev/null; then
            echo "   ✅ Garbage collection completed successfully"
        elif ssh "$NODE_NAME" "sudo docker exec \$(sudo docker ps -q --filter ancestor=registry:2) registry garbage-collect /etc/docker/registry/config.yml" 2>/dev/null; then
            echo "   ✅ Garbage collection completed successfully"
        else
            echo "   ⚠️  Could not run garbage collection automatically"
            echo "   💡 To free disk space, manually run:"
            echo "      ssh $NODE_NAME 'sudo microk8s kubectl exec -n kube-system deployment/registry -- registry garbage-collect /etc/docker/registry/config.yml'"
            echo "   OR:"
            echo "      ssh $NODE_NAME 'sudo docker exec \$(sudo docker ps -q --filter ancestor=registry:2) registry garbage-collect /etc/docker/registry/config.yml'"
        fi
    else
        echo "   ⚠️  Could not run garbage collection automatically (SPEAKER_NODE not configured)"
        echo "   💡 To free disk space, manually run garbage collection on your registry node"
    fi
fi
