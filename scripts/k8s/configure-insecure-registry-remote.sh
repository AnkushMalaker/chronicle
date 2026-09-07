#!/bin/bash

# Configure MicroK8s nodes for insecure HTTP registry access
# Run this script from your build machine to configure remote MicroK8s nodes
#
# Usage: ./scripts/configure-insecure-registry-remote.sh <ip_address> [ssh_user]
# Example: ./scripts/configure-insecure-registry-remote.sh 192.168.1.42
# Example: ./scripts/configure-insecure-registry-remote.sh 192.168.1.42 myuser

set -e

# Check command line arguments
if [ $# -lt 1 ]; then
    echo "Usage: $0 <ip_address> [ssh_user]"
    echo "Example: $0 192.168.1.42"
    echo "Example: $0 192.168.1.42 myuser"
    echo ""
    echo "Note: Registry address is automatically read from skaffold.env"
    exit 1
fi

# Validate IP address format
if [[ ! $1 =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "❌ Error: Invalid IP address format: $1"
    echo "Please provide a valid IPv4 address (e.g., 192.168.1.42)"
    exit 1
fi

# Configuration
# Read registry from skaffold.env or use default
if [ -f "skaffold.env" ]; then
    REGISTRY=$(grep "^REGISTRY=" skaffold.env | cut -d'=' -f2)
else
    REGISTRY="192.168.1.42:32000"  # Default fallback
fi

K8S_NODE_IP="$1"
SSH_USER="${2:-$USER}"  # Use second argument or default to current user

echo "🔧 Configuring MicroK8s nodes for insecure HTTP registry: $REGISTRY"
echo "Target node IP: $K8S_NODE_IP"
echo "SSH user: $SSH_USER"

# Function to configure a single node
configure_node() {
    local node=$1
    local user=$2

    echo "📋 Configuring node: $node"

    # Create the containerd configuration directory and file
    ssh -o StrictHostKeyChecking=no "$user@$node" "
        echo 'Creating MicroK8s containerd configuration...'
        sudo mkdir -p /var/snap/microk8s/current/args/certs.d/$REGISTRY

        echo 'Creating hosts.toml configuration...'
        sudo tee /var/snap/microk8s/current/args/certs.d/$REGISTRY/hosts.toml > /dev/null <<EOF
[host.\"http://$REGISTRY\"]
  capabilities = [\"pull\", \"resolve\", \"push\"]
  plain_http = true
  skip_verify = true
EOF

        echo 'Verifying configuration...'
        sudo cat /var/snap/microk8s/current/args/certs.d/$REGISTRY/hosts.toml

        echo 'Restarting MicroK8s containerd...'
        sudo snap restart microk8s.daemon-containerd

        echo 'Waiting for containerd to restart...'
        sleep 10

        echo 'Testing registry access...'
        curl -s http://$REGISTRY/v2/ > /dev/null && echo '✅ Registry accessible via HTTP' || echo '❌ Registry not accessible'

        echo 'Configuration complete on $node'
    "
}

# Configure the main Kubernetes node
echo "🚀 Starting configuration..."
configure_node "$K8S_NODE_IP" "$SSH_USER"

echo ""
echo "✅ MicroK8s configuration complete!"
echo ""
echo "🧪 Testing registry access from $K8S_NODE_IP..."
ssh -o StrictHostKeyChecking=no "$SSH_USER@$K8S_NODE_IP" "curl -s http://$REGISTRY/v2/ > /dev/null && echo '✅ Registry accessible' || echo '❌ Registry not accessible'"

echo ""
echo "🚀 Now you can deploy your application!"
echo "Run: skaffold run --profile=backend --default-repo=$REGISTRY"
