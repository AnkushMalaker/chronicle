#!/bin/bash

# Simple storage setup script for speaker recognition service
# Makes shared storage optional and easy to configure

set -e

# Load environment variables
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load-env.sh"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}Speaker Recognition Storage Setup${NC}"
echo -e "${BLUE}=================================${NC}"
echo

# Check if we're in the right directory
if [ ! -f "skaffold.yaml" ]; then
    echo -e "${RED}Error: Please run this script from the project root directory${NC}"
    exit 1
fi

echo -e "${YELLOW}This script will help you set up storage for the speaker recognition service.${NC}"
echo -e "${YELLOW}You have two options:${NC}"
echo
echo -e "${BLUE}1. Simple (Recommended)${NC} - Use existing cluster storage"
echo -e "   • Uses whatever storage class is available in your cluster"
echo -e "   • Each pod downloads models independently"
echo -e "   • Works out of the box with most Kubernetes clusters"
echo
echo -e "${BLUE}2. Shared Storage${NC} - Optimize for multiple pods"
echo -e "   • All pods share the same model cache"
echo -e "   • Faster startup after first pod"
echo -e "   • Requires ReadWriteOnce or ReadWriteMany storage"
echo

read -p "Choose option (1 or 2): " choice

case $choice in
    1)
        echo -e "${GREEN}Setting up simple storage configuration...${NC}"

        # Update values.yaml to disable shared models
        sed -i 's/enabled: true/enabled: false/' extras/speaker-recognition/charts/values.yaml

        echo -e "${GREEN}✅ Simple storage configured!${NC}"
        echo -e "${YELLOW}Each pod will download models independently.${NC}"
        echo
        echo -e "${BLUE}Next steps:${NC}"
        echo -e "1. Deploy the application:"
        echo -e "   ${YELLOW}skaffold run --profile speaker-recognition --default-repo=${CONTAINER_REGISTRY:-localhost:32000}${NC}"
        echo
        ;;

    2)
        echo -e "${GREEN}Setting up shared storage configuration...${NC}"

        # Check available storage classes
        echo -e "${YELLOW}Available storage classes:${NC}"
        kubectl get storageclass

        echo
        read -p "Enter storage class name (or press Enter for 'openebs-hostpath'): " storage_class
        storage_class=${storage_class:-"openebs-hostpath"}

        # Update values.yaml to enable shared models
        sed -i 's/enabled: false/enabled: true/' extras/speaker-recognition/charts/values.yaml
        sed -i "s/storageClassName: \"openebs-hostpath\"/storageClassName: \"${storage_class}\"/" extras/speaker-recognition/charts/values.yaml

        echo -e "${GREEN}✅ Shared storage configured!${NC}"
        echo -e "${YELLOW}Storage class: ${storage_class}${NC}"
        echo
        echo -e "${BLUE}Next steps:${NC}"
        echo -e "1. Deploy the application:"
        echo -e "   ${YELLOW}skaffold run --profile speaker-recognition --default-repo=${CONTAINER_REGISTRY:-localhost:32000}${NC}"
        echo -e "2. Monitor model download:"
        echo -e "   ${YELLOW}kubectl logs -n speech -l app.kubernetes.io/component=speaker -f${NC}"
        echo
        ;;

    *)
        echo -e "${RED}Invalid choice. Please run the script again and choose 1 or 2.${NC}"
        exit 1
        ;;
esac

echo -e "${GREEN}Storage setup complete!${NC}"
echo -e "${BLUE}You can now deploy the speaker recognition service.${NC}"
