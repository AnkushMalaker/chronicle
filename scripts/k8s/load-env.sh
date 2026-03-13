#!/bin/bash

# Common environment loading function for k8s scripts
# This script loads environment variables from config.env and makes them available to other scripts

# Get the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Get the project root directory (two levels up from scripts/k8s)
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Path to config.env
CONFIG_ENV="$PROJECT_ROOT/config.env"

# Check if config.env exists
if [ ! -f "$CONFIG_ENV" ]; then
    echo "❌ Error: config.env not found at $CONFIG_ENV"
    echo "Please make sure you're running this from the project root or that config.env exists"
    exit 1
fi

# Load environment variables from config.env
# This handles both simple assignments and computed values
load_config_env() {
    # Source the config.env file to get all variables
    set -a  # automatically export all variables
    source "$CONFIG_ENV"
    set +a  # stop automatically exporting

    # Export commonly used variables for k8s scripts
    export SPEAKER_NODE="${SPEAKER_NODE:-}"
    export CONTAINER_REGISTRY="${CONTAINER_REGISTRY:-localhost:32000}"
    export INFRASTRUCTURE_NAMESPACE="${INFRASTRUCTURE_NAMESPACE:-root}"
    export APPLICATION_NAMESPACE="${APPLICATION_NAMESPACE:-chronicle}"
    export STORAGE_CLASS="${STORAGE_CLASS:-openebs-hostpath}"
}

# Function to get a specific variable with a default value
get_config_var() {
    local var_name="$1"
    local default_value="${2:-}"

    # Load config if not already loaded
    if [ -z "${CONFIG_LOADED:-}" ]; then
        load_config_env
        export CONFIG_LOADED=1
    fi

    # Return the variable value or default
    eval "echo \${$var_name:-$default_value}"
}

# Function to validate required variables
validate_required_vars() {
    local missing_vars=()

    # Check for required variables
    if [ -z "${SPEAKER_NODE:-}" ]; then
        missing_vars+=("SPEAKER_NODE")
    fi

    if [ -z "${CONTAINER_REGISTRY:-}" ]; then
        missing_vars+=("CONTAINER_REGISTRY")
    fi

    if [ ${#missing_vars[@]} -gt 0 ]; then
        echo "❌ Error: Missing required environment variables:"
        for var in "${missing_vars[@]}"; do
            echo "   - $var"
        done
        echo ""
        echo "Please check your config.env file and ensure these variables are set."
        exit 1
    fi
}

# Auto-load config when this script is sourced
if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
    # Script is being sourced, load the config
    load_config_env
    export CONFIG_LOADED=1
fi
