#!/usr/bin/env python3
"""
Generate Kubernetes configuration files (ConfigMap and Secret)
"""

import os
import sys
from pathlib import Path

# Add lib directory to path
sys.path.append(str(Path(__file__).parent / "lib"))

from env_utils import classify_secrets, get_resolved_env_vars


def generate_k8s_manifests(namespace: str = "chronicle"):
    """Generate Kubernetes ConfigMap and Secret manifests"""
    print(f"Generating Kubernetes ConfigMap and Secret for namespace {namespace}...")

    # Create output directory
    output_dir = Path("k8s-manifests")
    output_dir.mkdir(exist_ok=True)

    # Get all resolved environment variables
    all_vars = get_resolved_env_vars()
    config_vars, secret_vars = classify_secrets(all_vars)

    # Generate ConfigMap
    configmap_path = output_dir / "configmap.yaml"
    with open(configmap_path, "w") as f:
        f.write("apiVersion: v1\n")
        f.write("kind: ConfigMap\n")
        f.write("metadata:\n")
        f.write(f"  name: chronicle-config\n")
        f.write(f"  namespace: {namespace}\n")
        f.write("  labels:\n")
        f.write("    app.kubernetes.io/name: chronicle\n")
        f.write("    app.kubernetes.io/component: config\n")
        f.write("data:\n")

        for var_name in sorted(config_vars.keys()):
            var_value = config_vars[var_name]
            # Escape quotes in values
            escaped_value = var_value.replace('"', '\\"')
            f.write(f'  {var_name}: "{escaped_value}"\n')

    # Generate Secret
    secret_path = output_dir / "secrets.yaml"
    with open(secret_path, "w") as f:
        f.write("apiVersion: v1\n")
        f.write("kind: Secret\n")
        f.write("type: Opaque\n")
        f.write("metadata:\n")
        f.write(f"  name: chronicle-secrets\n")
        f.write(f"  namespace: {namespace}\n")
        f.write("  labels:\n")
        f.write("    app.kubernetes.io/name: chronicle\n")
        f.write("    app.kubernetes.io/component: secrets\n")
        f.write("data:\n")

        import base64

        for var_name in sorted(secret_vars.keys()):
            var_value = secret_vars[var_name]
            # Base64 encode the value
            encoded_value = base64.b64encode(var_value.encode()).decode()
            f.write(f"  {var_name}: {encoded_value}\n")

    print("Generated:")
    print(f"  - {configmap_path}")
    print(f"  - {secret_path}")
    print("")
    print("To apply these manifests:")
    print(f"  kubectl apply -f {configmap_path}")
    print(f"  kubectl apply -f {secret_path}")


def main():
    """Main entry point"""
    namespace = sys.argv[1] if len(sys.argv) > 1 else "chronicle"
    generate_k8s_manifests(namespace)


if __name__ == "__main__":
    main()
