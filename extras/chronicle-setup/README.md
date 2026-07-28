# chronicle-setup

Shared helpers for Chronicle's setup path — `wizard.py`, every service `init.py`,
and plugin `setup.py` scripts.

```python
from chronicle_setup import read_env_value, prompt_with_existing_masked
```

| Module | What lives there |
|--------|------------------|
| `env` | Read values out of a `.env`, recognise placeholders, mask secrets |
| `prompts` | Interactive prompts that offer the existing value back |
| `system` | Tailscale identity, TLS cert issuance/renewal, CUDA detection |
| `chronicle_api` | Mint a long-lived API key from a running backend |

Everything public is re-exported from the top level; the module split is an
implementation detail.

## Why it is a package

This was `setup_utils.py` at the repository root, imported via
`sys.path.insert(0, <repo root>)` in each script. That only resolved when the
script was launched from its own directory, and it is the reason setup scripts
also read `.env` relative to the current directory.

It is installed through `setup-requirements.txt`, which lists it as the relative
path `./extras/chronicle-setup`. **uv resolves that against the working
directory, not against the requirements file**, so setup commands must run from
the repository root:

```bash
uv run --with-requirements setup-requirements.txt python extras/asr-services/init.py
```

The scripts locate their own files from `__file__`, so running them from the
root is purely a packaging constraint — it does not change which `.env` they
write.
