# Shared Models Storage

## Quick Setup

**Use the setup script:**
```bash
./scripts/setup-storage.sh
```

## Manual Configuration

### Simple Mode (Default)
Each pod downloads models independently. Works with any storage class.

```yaml
global:
  sharedModels:
    enabled: false
```

### Shared Mode (Optimized)
All pods share the same model cache. Requires ReadWriteOnce storage.

```yaml
global:
  sharedModels:
    enabled: true
    size: 20Gi
    storageClassName: "openebs-hostpath"  # Or your storage class
```

## Benefits

| Mode | Storage | Startup Time | Network Usage |
|------|---------|--------------|---------------|
| Simple | 3×10GB = 30GB | Each pod: 5-10 min | 3× downloads |
| Shared | 1×20GB = 20GB | First pod: 5-10 min, Others: immediate | 1× download |

## Troubleshooting

**Check storage:**
```bash
kubectl get pvc -n speech
kubectl describe pvc shared-models-cache -n speech
```

**Check models:**
```bash
kubectl exec -n speech deployment/speaker-recognition-speaker -- ls -la /models
```

**Switch modes:**
```bash
# Enable shared storage
sed -i 's/enabled: false/enabled: true/' extras/speaker-recognition/charts/values.yaml

# Disable shared storage
sed -i 's/enabled: true/enabled: false/' extras/speaker-recognition/charts/values.yaml
```
