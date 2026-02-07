# Chronicle Kubernetes Setup Guide

This guide walks you through setting up Chronicle from scratch on a fresh Ubuntu system, including MicroK8s installation, Docker registry configuration, and deployment via Skaffold.

## System Architecture

- **Build Machine**: Your development machine with Docker (for building images)
- **Kubernetes Node (k8s_control_plane)**: Ubuntu server running MicroK8s cluster
- **Docker Registry**: Runs on the Kubernetes node for image storage

## Table of Contents

1. [System Requirements](#system-requirements)
2. [Ubuntu Installation](#ubuntu-installation)
3. [MicroK8s Installation](#microk8s-installation)
4. [MicroK8s Registry Setup](#microk8s-registry-setup)
5. [Repository Setup](#repository-setup)
6. [Environment Configuration](#environment-configuration)
7. [Deployment](#deployment)
8. [Verification](#verification)
9. [Troubleshooting](#troubleshooting)

## System Requirements

### Build Machine (Your Development Machine)
- **OS**: macOS, Linux, or Windows with WSL2
- **Docker**: Docker Desktop or Docker Engine
- **Tools**: Git, curl/wget

### Kubernetes Node (k8s_control_plane)
- **Hardware**: Minimum 8GB RAM, 4 CPU cores, 50GB storage
- **Network**: Static IP configuration (recommended: 192.168.1.42)
- **OS**: Ubuntu 22.04 LTS or later
- **Architecture**: x86_64 (AMD64)

## Ubuntu Installation

**Run on: Kubernetes Node (k8s_control_plane)**

1. **Download Ubuntu Server 22.04 LTS**
   ```bash
   # Download from https://ubuntu.com/download/server
   # Or use wget:
   wget https://releases.ubuntu.com/22.04/ubuntu-22.04.3-live-server-amd64.iso
   ```

2. **Install Ubuntu Server**
   - Boot from USB/DVD
   - Choose "Install Ubuntu Server"
   - Configure network with static IP (recommended: 192.168.1.42) 
   - Set hostname (e.g., `k8s_control_plane`)
   - Create user account
   - Install OpenSSH server

3. **Post-Installation Setup**
   ```bash
   # Update system
   sudo apt update && sudo apt upgrade -y
   
   # Install essential packages
   sudo apt install -y curl wget git vim htop tree
   
   # Configure firewall
   sudo ufw allow ssh
   sudo ufw allow 6443  # Kubernetes API
   sudo ufw allow 32000  # Docker registry
   sudo ufw enable
   ```

## MicroK8s Installation

**Run on: Kubernetes Node (k8s_control_plane)**

1. **Install MicroK8s**
   ```bash
   # Install MicroK8s
   sudo snap install microk8s --classic
   
   # Add user to microk8s group
   sudo usermod -a -G microk8s $USER
   sudo chown -f -R $USER ~/.kube
   
   # Log out and back in, or run:
   newgrp microk8s
   ```

2. **Configure as Control Plane**
   ```bash
   # Start MicroK8s
   sudo microk8s start
   
   # Wait for all services to be ready
   sudo microk8s status --wait-ready
   
   # Generate join token for worker nodes
   sudo microk8s add-node
   # This will output a command like:
   # sudo microk8s join 192.168.1.42:25000/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   # Save this command for worker node setup
   ```

3. **Start MicroK8s**
   ```bash
   # Start MicroK8s
   sudo microk8s start
   
   # Wait for all services to be ready
   sudo microk8s status --wait-ready
   ```

4. **Enable Required Add-ons**
   ```bash
   # Enable essential add-ons
   sudo microk8s enable dns
   sudo microk8s enable ingress
   sudo microk8s enable storage
   sudo microk8s enable metrics-server
   
   # Wait for add-ons to be ready
   sudo microk8s status --wait-ready
   ```

5. **Configure kubectl**
   ```bash
   # Create kubectl alias
   echo 'alias kubectl="microk8s kubectl"' >> ~/.bashrc
   source ~/.bashrc
   
   # Verify installation
   kubectl get nodes
   kubectl get pods -A
   ```

## Worker Node Installation

**Run on: Each Worker Node**

1. **Install Ubuntu Server (Same as Control Plane)**
   ```bash
   # Follow the same Ubuntu installation steps as the control plane
   # Use different hostname (e.g., k8s_worker_01, k8s_worker_02)
   # Configure static IP (e.g., 192.168.1.43, 192.168.1.44)
   # Ensure network connectivity to control plane (192.168.1.42)
   ```

2. **Install MicroK8s**
   ```bash
   # Install MicroK8s
   sudo snap install microk8s --classic
   
   # Add user to microk8s group
   sudo usermod -a -G microk8s $USER
   sudo chown -f -R $USER ~/.kube
   
   # Log out and back in, or run:
   newgrp microk8s
   ```

3. **Join the Cluster**
   ```bash
   # Use the join command from the control plane
   # Replace with your actual join token
   sudo microk8s join 192.168.1.42:25000/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   
   # Wait for node to join
   sudo microk8s status --wait-ready
   ```

4. **Verify Node Status**
   ```bash
   # On the control plane, verify the worker node joined
   kubectl get nodes
   
   # The worker node should show as Ready
   # Example output:
   # NAME                STATUS   ROLES    AGE   VERSION
   # k8s_control_plane   Ready    <none>   1h    v1.28.0
   # k8s_worker_01      Ready    <none>   5m    v1.28.0
   ```

5. **Configure Worker Node for Registry Access**
   ```bash
   # From your build machine, configure the worker node
   ./configure-insecure-registry-remote.sh 192.168.1.43
   
   # Repeat for each worker node with their respective IPs
   ```

## MicroK8s Registry Setup

**Run on: Kubernetes Node (k8s_control_plane)**

1. **Enable Built-in Registry**
   ```bash
   # Enable the built-in MicroK8s registry (not enabled by default)
   sudo microk8s enable registry
   
   # Wait for registry to be ready
   sudo microk8s status --wait-ready
   
   # Verify registry is running
   kubectl get pods -n container-registry
   ```

2. **Configure Registry Access**
   ```bash
   # The registry runs on port 32000 by default
   # Verify it's accessible
   curl http://localhost:32000/v2/
   ```

3. **Configure Remote Access (from Build Machine)**
   ```bash
   # From your build machine, configure MicroK8s to trust the insecure registry
   chmod +x scripts/configure-insecure-registry-remote.sh
   
   # Run the configuration script with your node IP address
   # Usage: ./scripts/configure-insecure-registry-remote.sh <ip_address> [ssh_user]
   ./scripts/configure-insecure-registry-remote.sh 192.168.1.42
   
   # Or with custom SSH user:
   # ./scripts/configure-insecure-registry-remote.sh 192.168.1.42 myuser
   ```

## Storage Configuration

**Run on: Kubernetes Node (k8s_control_plane)**

1. **Install OpenEBS Hostpath Provisioner**

   the default hostpath provisioner seems to have a bug in it that makes it not work. If you want to use the default
   one, then you will need to update the charts to use that for the PVC.

   ```bash
   # Apply the hostpath provisioner
   kubectl apply -f k8s-manifests/hostpath-provisioner-official.yaml
   
   # Verify storage class
   kubectl get storageclass
   ```

## Repository Setup

**Run on: Build Machine (Your Development Machine)**

### **Directory Structure**
```
chronicle/
├── scripts/                    # Kubernetes deployment and management scripts
│   ├── deploy-all-services.sh  # Deploy all services
│   ├── cluster-status.sh       # Check cluster health
│   ├── setup-nvidia-operator.sh # Setup GPU support
│   ├── test-gpu-pod.sh        # Test GPU access
│   ├── cleanup-all.sh          # Remove all deployments
│   ├── configure-insecure-registry-remote.sh # Configure registry access
│   └── generate-configmap.sh   # Generate ConfigMap templates
├── k8s-manifests/             # Standalone Kubernetes manifests
│   └── hostpath-provisioner-official.yaml # Storage provisioner
├── skaffold.env.template      # Configuration template
├── skaffold.yaml              # Skaffold deployment configuration
├── init.sh                    # Docker Compose setup
└── deploy-speaker-recognition.sh # Standalone speaker recognition
```

### **Repository Setup**

1. **Clone Repository**
   ```bash
   # Clone Chronicle repository with submodules
   git clone --recursive https://github.com/chronicle-ai/chronicle.git
   cd chronicle

   # If you already cloned without --recursive, initialize submodules:
   # git submodule update --init --recursive

   # Verify template files are present
   ls -la skaffold.env.template
   ls -la backends/advanced/.env.template
   ```

   > **Note:** The `--recursive` flag downloads the optional Mycelia submodule (an alternative memory backend with timeline visualization). Most deployments use the default Chronicle memory system and don't need Mycelia.

2. **Install Required Tools**
   
   **kubectl** (required for Skaffold and Helm):
   - Visit: https://kubernetes.io/docs/tasks/tools/
   - Follow the official installation guide for your platform
   
   **Skaffold**:
   - Visit: https://skaffold.dev/docs/install/
   - Follow the official installation guide 
   
   **Helm**:
   - Visit: https://helm.sh/docs/intro/install/
   - Follow the official installation guide 
   
   **Verify installations:**
   ```bash
   kubectl version --client
   skaffold version
   helm version
   ```

## Environment Configuration

**Important**: Never commit your actual `.env` or `skaffold.env` files to version control. Only the `.template` files should be committed.

**Run on: Build Machine (Your Development Machine)**

1. **Create Environment File**
   ```bash
   # Copy template (if it exists)
   # cp backends/advanced/.env.template backends/advanced/.env
   
   # Note: Most environment variables are automatically set by Skaffold during deployment
   # including MONGODB_URI, QDRANT_BASE_URL, and other Kubernetes-specific values
   ```

2. **Configure Skaffold Environment**
   ```bash
   # Copy the template file
   cp skaffold.env.template skaffold.env
   
   # Edit skaffold.env with your specific values
   vim skaffold.env
   
   # Essential variables to configure:
   REGISTRY=192.168.1.42:32000  # Use IP address for immediate access
   # Alternative: REGISTRY=k8s_control_plane:32000 (requires adding 'k8s_control_plane 192.168.1.42' to /etc/hosts)
   BACKEND_IP=192.168.1.42
   BACKEND_NODEPORT=30270
   WEBUI_NODEPORT=31011
   
   # Optional: Configure speaker recognition service
   HF_TOKEN=hf_your_huggingface_token_here
   DEEPGRAM_API_KEY=your_deepgram_api_key_here
   
   # Note: MONGODB_URI and QDRANT_BASE_URL are automatically generated
   # by Skaffold based on your infrastructure namespace and service names
   ```

3. **Configuration Variables Reference**
   
   **Required Variables:**
   - `REGISTRY`: Docker registry for image storage
   - `BACKEND_IP`: IP address of your Kubernetes control plane
   - `BACKEND_NODEPORT`: Port for backend service (30000-32767)
   - `WEBUI_NODEPORT`: Port for WebUI service (30000-32767)
   - `INFRASTRUCTURE_NAMESPACE`: Namespace for MongoDB and Qdrant
   - `APPLICATION_NAMESPACE`: Namespace for your application
   
   **Optional Variables (for Speaker Recognition):**
   - `HF_TOKEN`: Hugging Face token for Pyannote models
   - `DEEPGRAM_API_KEY`: Deepgram API key for speech-to-text
   - `COMPUTE_MODE`: GPU or CPU mode for ML services
   - `SIMILARITY_THRESHOLD`: Speaker identification threshold
   
   **Automatically Generated:**
   - `MONGODB_URI`: Generated from infrastructure namespace
   - `QDRANT_BASE_URL`: Generated from infrastructure namespace
   - `IMAGE_REPO_*`: Generated from Skaffold build process
   - `IMAGE_TAG_*`: Generated from Skaffold build process

5. **Generate ConfigMap (if needed)**
   ```bash
   # Note: Most environment variables are handled by Skaffold automatically
   # If you need custom environment variables, you can:
   
   # Option 1: Use the script (if it exists)
   # chmod +x scripts/generate-helm-configmap.sh
   # ./scripts/generate-helm-configmap.sh
   
   # Option 2: Add them directly to the Helm chart values
   # Edit backends/charts/advanced-backend/values.yaml
   ```

## Available Scripts

**Run on: Build Machine (Your Development Machine)**

The following scripts are available in the `scripts/` folder to simplify common operations:

### **Deployment Scripts**
```bash
# Deploy all services in the correct order
./scripts/deploy-all-services.sh

# Deploy with custom registry
./scripts/deploy-all-services.sh 192.168.1.43:32000

# Check cluster status and health
./scripts/cluster-status.sh

# Check status of specific namespace
./scripts/cluster-status.sh chronicle
```

### **Setup Scripts**
```bash
# Configure insecure registry access for remote nodes
./scripts/configure-insecure-registry-remote.sh 192.168.1.42

# Setup NVIDIA GPU operator
./scripts/setup-nvidia-operator.sh
```

### **Maintenance Scripts**
```bash
# Clean up all deployments (with confirmation)
./scripts/cleanup-all.sh

# Force cleanup without confirmation
./scripts/cleanup-all.sh --force
```

### **GPU Testing Scripts**
```bash
# Test GPU access in Kubernetes
./scripts/test-gpu-pod.sh

# Test with specific GPU count
./scripts/test-gpu-pod.sh 2
```

### **Configuration Scripts**
```bash
# Generate ConfigMap data template from .env file
./scripts/generate-configmap.sh

# This creates a Helm template that can be included in charts
# Output: backends/charts/advanced-backend/templates/env-data.yaml
```

**Note**: All scripts automatically read configuration from `skaffold.env` and provide helpful error messages if configuration is missing.

**When to use generate-configmap.sh**: Use this script when you want to:
- Create environment-specific ConfigMaps from `.env` files
- Generate Helm templates that can be included in other charts
- Manage environment variables in a more maintainable way
- Override the default hardcoded values in the Helm charts

**Removed Scripts**: The following scripts were removed as they are no longer needed:
- `generate-helm-configmap.sh` - Environment variables are now handled automatically by Skaffold

## Kubernetes Manifests

**Location**: `k8s-manifests/` directory

This directory contains standalone Kubernetes manifests that are not managed by Skaffold:

- **`hostpath-provisioner-official.yaml`** - OpenEBS hostpath storage provisioner
  - Applied manually: `kubectl apply -f k8s-manifests/hostpath-provisioner-official.yaml`
  - Creates the `openebs-hostpath` storage class used by all services

## Deployment

**Run on: Build Machine (Your Development Machine)**

1. **Deploy All Services (Recommended)**
   ```bash
   # Deploy everything in the correct order
   ./scripts/deploy-all-services.sh
   
   # This will automatically:
   # - Deploy infrastructure (MongoDB, Qdrant)
   # - Deploy main application (Backend, WebUI)
   # - Deploy additional services (if configured)
   # - Wait for each service to be ready
   # - Verify all deployments
   ```

2. **Manual Deployment (Alternative)**
   ```bash
   # Deploy infrastructure first
   skaffold run --profile=infrastructure
   
   # Wait for infrastructure to be ready
   kubectl get pods -n root
   
   # Deploy main application
   skaffold run --profile=advanced-backend --default-repo=192.168.1.42:32000
   
   # Monitor deployment
   skaffold run --profile=advanced-backend --default-repo=192.168.1.42:32000 --tail
   ```

3. **Verify Deployment**
   ```bash
   # Check all resources
   kubectl get all -n chronicle
   kubectl get all -n root
   
   # Check Ingress
   kubectl get ingress -n chronicle
   
   # Check services
   kubectl get svc -n chronicle
   ```

## Multi-Node Cluster Management

**Run on: Build Machine (Your Development Machine)**

### **Cluster Status and Management**
```bash
# View all nodes in the cluster
kubectl get nodes -o wide

# Get detailed node information
kubectl describe node k8s_control_plane
kubectl describe node k8s_worker_01

# Check node resources
kubectl top nodes

# View node labels and taints
kubectl get nodes --show-labels
```

### **Pod Scheduling and Distribution**
```bash
# Check pod distribution across nodes
kubectl get pods -A -o wide

# Force pod scheduling to specific node (if needed)
kubectl label node k8s_worker_01 node-role.kubernetes.io/worker=true

# Check node capacity and allocatable resources
kubectl describe node k8s_worker_01 | grep -A 5 "Allocated resources"
```

### **Worker Node Maintenance**
```bash
# Drain a worker node before maintenance
kubectl drain k8s_worker_01 --ignore-daemonsets --delete-emptydir-data

# After maintenance, uncordon the node
kubectl uncordon k8s_worker_01

# Remove a worker node from the cluster
kubectl delete node k8s_worker_01
```

## Additional Services Deployment

**Run on: Build Machine (Your Development Machine)**

### 1. **Speaker Recognition Service**

```bash
# Deploy speaker recognition with GPU support
skaffold run --profile=speaker-recognition --default-repo=k8s_control_plane:32000

# Monitor deployment
skaffold run --profile=speaker-recognition --default-repo=k8s_control_plane:32000 --tail

# Verify deployment
kubectl get pods -n speech
kubectl get svc -n speech
```

### 2. **ASR (Automatic Speech Recognition) Services**

```bash
# Deploy ASR services (Moonshine, Parakeet)
skaffold run --profile=asr-services --default-repo=k8s_control_plane:32000

# Monitor deployment
skaffold run --profile=asr-services --default-repo=k8s_control_plane:32000 --tail

# Verify deployment
kubectl get pods -n asr
kubectl get svc -n asr
```

### 3. **NVIDIA GPU Operator Setup**

**Prerequisites:**
- NVIDIA GPU(s) installed on the Kubernetes node
- NVIDIA drivers installed on the host system

**Installation:**
```bash
# Use the automated setup script
./scripts/setup-nvidia-operator.sh

# Or manually install:
# Add NVIDIA Helm repository
helm repo add nvidia https://helm.ngc.nvidia.com/nvidia
helm repo update

# Install NVIDIA GPU Operator
helm install gpu-operator nvidia/gpu-operator \
  --namespace gpu-operator \
  --create-namespace \
  --set driver.enabled=false \
  --set toolkit.enabled=false

# Wait for GPU operator to be ready
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=gpu-operator -n gpu-operator --timeout=300s

# Verify GPU detection
kubectl get nodes -o json | jq '.items[] | {name: .metadata.name, gpu: .status.allocatable."nvidia.com/gpu"}'

# Check GPU operator pods
kubectl get pods -n gpu-operator
```

**GPU-Enabled Pod Configuration:**
```yaml
# Example pod spec with GPU access
spec:
  containers:
  - name: gpu-container
    image: nvidia/cuda:11.8-base-ubuntu20.04
    resources:
      limits:
        nvidia.com/gpu: 1
    command: ["nvidia-smi"]
```

**Test GPU Access:**
```bash
# Test GPU functionality
./scripts/test-gpu-pod.sh

# Test with multiple GPUs
./scripts/test-gpu-pod.sh 2
```

## Verification

**Run on: Build Machine (Your Development Machine)**

1. **Check Application Health**
   ```bash
   # Check backend health
   curl -k https://chronicle.192-168-1-42.nip.io:32623/health
   
   # Check WebUI
   curl -k https://chronicle.192-168-1-42.nip.io:32623/
   ```

2. **Access WebUI**
   - Open browser to: `https://chronicle.192-168-1-42.nip.io:32623/`
   - Accept self-signed certificate warning
   - Create admin user account
   - Test audio recording functionality

3. **Test WebSocket Connection**
   - Open browser console
   - Check for WebSocket connection success
   - Verify audio recording works

## Troubleshooting

### Common Issues

1. **Registry Access Issues**
   ```bash
   # Test registry connectivity (run on Kubernetes node)
   curl http://k8s_control_plane:32000/v2/
   
   # Check MicroK8s containerd config (run on Kubernetes node)
   sudo cat /var/snap/microk8s/current/args/certs.d/k8s_control_plane:32000/hosts.toml
   ```

2. **Storage Issues**
   ```bash
   # Check storage class (run on build machine)
   kubectl get storageclass
   
   # Check persistent volumes (run on build machine)
   kubectl get pv
   kubectl get pvc -A
   ```

3. **Ingress Issues**
   ```bash
   # Check Ingress controller (run on build machine)
   kubectl get pods -n ingress-nginx
   
   # Check Ingress configuration (run on build machine)
   kubectl describe ingress -n chronicle
   ```

4. **Build Issues**
   ```bash
   # Clean and rebuild (run on build machine)
   skaffold clean
   skaffold run --profile=advanced-backend --default-repo=k8s_control_plane:32000
   ```

5. **GPU Issues**
   ```bash
   # Check GPU operator status (run on build machine)
   kubectl get pods -n gpu-operator
   kubectl describe pod -n gpu-operator <pod-name>
   
   # Check GPU detection on nodes
   kubectl get nodes -o json | jq '.items[] | {name: .metadata.name, gpu: .status.allocatable."nvidia.com/gpu"}'
   
   # Check GPU operator logs
   kubectl logs -n gpu-operator deployment/gpu-operator
   
   # Verify NVIDIA drivers on host (run on Kubernetes node)
   nvidia-smi
   ```

6. **Multi-Node Cluster Issues**
   ```bash
   # Check node connectivity (run on build machine)
   kubectl get nodes
   kubectl describe node <node-name>
   
   # Check node status and conditions
   kubectl get nodes -o json | jq '.items[] | {name: .metadata.name, status: .status.conditions[] | select(.type=="Ready") | .status, message: .message}'
   
   # Check if pods can be scheduled
   kubectl get pods -A -o wide
   kubectl describe pod <pod-name> -n <namespace>
   
   # Check node resources and capacity
   kubectl top nodes
   kubectl describe node <node-name> | grep -A 10 "Allocated resources"
   
   # Verify network connectivity between nodes
   # Run on each node:
   ping <other-node-ip>
   curl -s http://<other-node-ip>:32000/v2/  # Registry access
   ```

### Useful Commands

```bash
# View logs (run on build machine)
kubectl logs -n chronicle deployment/advanced-backend
kubectl logs -n chronicle deployment/webui

# Port forward for debugging (run on build machine)
kubectl port-forward -n chronicle svc/advanced-backend 8000:8000
kubectl port-forward -n chronicle svc/webui 8080:80

# Check resource usage (run on build machine)
kubectl top pods -n chronicle
kubectl top nodes

# Restart deployments (run on build machine)
kubectl rollout restart deployment/advanced-backend -n chronicle
kubectl rollout restart deployment/webui -n chronicle
```

## Maintenance

**Run on: Build Machine (Your Development Machine)**

1. **Regular Updates**
   ```bash
   # Update Docker images
   skaffold run --profile=advanced-backend --default-repo=k8s_control_plane:32000
   ```

**Run on: Kubernetes Node (k8s_control_plane)**

1. **System Updates**
   ```bash
   # Update system packages
   sudo apt update && sudo apt upgrade -y
   
   # Update MicroK8s
   sudo snap refresh microk8s
   ```

2. **Backup Configuration**
   ```bash
   # Backup environment files (run on build machine)
   cp backends/advanced/.env backends/advanced/.env.backup
   cp skaffold.env skaffold.env.backup
   
   # Backup Kubernetes manifests (run on build machine)
   kubectl get all -n chronicle -o yaml > chronicle-backup.yaml
   kubectl get all -n root -o yaml > infrastructure-backup.yaml
   ```

## Alternative: Docker Compose Setup

If you prefer to use Docker Compose instead of Kubernetes, use the `init.sh` script:

**Run on: Build Machine (Your Development Machine)**

```bash
# Make script executable
chmod +x init.sh

# Run interactive setup
./init.sh
```

This will guide you through setting up Chronicle using Docker Compose instead of Kubernetes.

## Speaker Recognition Deployment

For standalone speaker recognition deployment (without full Kubernetes setup):

**Run on: Build Machine (Your Development Machine)**

```bash
# Make script executable
chmod +x deploy-speaker-recognition.sh

# Run deployment
./deploy-speaker-recognition.sh
```

This script handles speaker recognition service deployment with proper environment configuration.

## Support

For additional support:
- Check the main [README.md](README.md)
- Review [CLAUDE.md](CLAUDE.md) for development notes
- Check [README-skaffold.md](README-skaffold.md) for Skaffold-specific information

---

**Note**: This setup supports both single-node and multi-node MicroK8s clusters. For production use, multi-node clusters provide better reliability, scalability, and resource distribution. The worker node installation section above provides complete instructions for expanding your cluster.
