# Local OMI BT

Connect to an OMI device over Bluetooth and stream audio to the Chronicle backend.

## Prerequisites

- **Python 3.12+** (managed via `uv`)
- **Opus codec library** (required by `opuslib`)

### Installing Opus

**macOS (Homebrew):**
```bash
brew install opus
```

**Linux (Debian/Ubuntu):**
```bash
sudo apt install libopus-dev
```

## Usage

```bash
./start.sh
```

Or run directly:
```bash
uv run --with-requirements requirements.txt python connect-omi.py
```

### macOS: Opus library not found

If you see `Could not find Opus library`, you need to tell the dynamic linker where to find it. The `start.sh` script handles this automatically, but if running manually:

```bash
DYLD_LIBRARY_PATH="$(brew --prefix opus)/lib" uv run --with-requirements requirements.txt python connect-omi.py
```
