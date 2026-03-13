#!/bin/bash

# Script to cleanup the registry storage that's taking up 67GB
# This is the actual file storage behind the registry, not just the image metadata

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

echo "🎯 Registry Storage Cleanup"
echo "Found: container-registry-registry-claim-pvc using 67GB"

# Parse password parameter
SUDO_PASSWORD=""
if [[ $1 == pass=* ]]; then
    SUDO_PASSWORD="${1#pass=}"
    echo "🔑 Using provided password for sudo commands"
fi

# Function to run SSH command with or without password
run_ssh_sudo() {
    local cmd="$1"
    if [[ -n "$SUDO_PASSWORD" ]]; then
        echo "$SUDO_PASSWORD" | ssh "$NODE_NAME" "sudo -S $cmd" 2>/dev/null
    else
        ssh -t "$NODE_NAME" "sudo $cmd"
    fi
}

echo ""
echo "📊 Current disk usage:"
ssh "$NODE_NAME" "df -h / | tail -1"

echo ""
echo "📁 Registry storage location:"
REGISTRY_PVC="/var/snap/microk8s/common/default-storage/container-registry-registry-claim-pvc-e010ef32-6d7e-49dd-8ebb-a4ac7542c080"
run_ssh_sudo "du -sh '$REGISTRY_PVC'"

echo ""
echo "🔍 What's inside the registry storage:"
run_ssh_sudo "du -sh '$REGISTRY_PVC'/* | sort -hr | head -10"

echo ""
echo "⚠️  DANGER ZONE - Registry Data Cleanup Options:"
echo ""
echo "Option 1: Clean up registry garbage (safest):"
echo "   This removes unreferenced blobs but keeps valid data"
echo ""
echo "Option 2: Delete specific repository data:"
echo "   This removes data for specific repositories"
echo ""
echo "Option 3: Nuclear option - delete all registry data:"
echo "   This will delete ALL registry data (you'll lose all your images)"

echo ""
read -p "🤔 Which option do you want? (1/2/3/exit): " choice

case $choice in
    1)
        echo "🧹 Attempting registry garbage collection..."
        echo "Looking for registry garbage collection methods..."

        # Try to find registry processes or containers
        echo "Registry processes:"
        ssh "$NODE_NAME" "ps aux | grep registry || echo 'No registry processes found'"

        echo ""
        echo "💡 Manual cleanup needed:"
        echo "1. Find the registry container/pod:"
        echo "   kubectl get pods --all-namespaces | grep registry"
        echo "2. Run garbage collection in the registry:"
        echo "   kubectl exec -it <registry-pod> -- registry garbage-collect /etc/docker/registry/config.yml"
        ;;

    2)
        echo "📦 Repository cleanup..."
        run_ssh_sudo "ls -la '$REGISTRY_PVC/docker/registry/v2/repositories/'"
        echo ""
        echo "💡 To delete specific repositories:"
        echo "   sudo rm -rf '$REGISTRY_PVC/docker/registry/v2/repositories/<repo-name>'"
        ;;

    3)
        echo "☢️  NUCLEAR OPTION - This will delete ALL registry data!"
        echo "You will lose all your container images in the registry!"
        echo ""
        read -p "Are you ABSOLUTELY sure? Type 'DELETE_ALL_REGISTRY_DATA': " confirm

        if [[ "$confirm" == "DELETE_ALL_REGISTRY_DATA" ]]; then
            echo "💣 Deleting all registry data..."
            run_ssh_sudo "rm -rf '$REGISTRY_PVC'/*"
            echo "✅ Registry data deleted."
            echo "📊 New disk usage:"
            ssh "$NODE_NAME" "df -h / | tail -1"
        else
            echo "❌ Cancelled - incorrect confirmation"
        fi
        ;;

    *)
        echo "👋 Exiting without changes"
        ;;
esac

echo ""
echo "💾 Final disk usage:"
ssh "$NODE_NAME" "df -h / | tail -1"
