# Chronicle Port Configuration & User Journey

## User Journey: Git Clone to Running Services

### 1. Clone & Setup
```bash
git clone <repo>
cd chronicle

# Configure all services (using convenience script)
./wizard.sh

# Start all configured services
./start.sh
```

### 2. Service Access Points

## HTTP Mode (Default - No SSL Required)

| Service | API Port | Web UI Port | Access URL |
|---------|----------|-------------|------------|
| **Advanced Backend** | 8000 | 5173 | http://localhost:8000 (API)<br>http://localhost:5173 (Dashboard) |
| **Speaker Recognition** | 8085 | 5175* | http://localhost:8085 (API)<br>http://localhost:5175 (WebUI) |
| **Parakeet ASR** | 8767 | - | http://localhost:8767 (API) |
| **OpenMemory MCP** | 8765 | 8765 | http://localhost:8765 (API + WebUI) |

*Note: Speaker Recognition WebUI port is configurable via REACT_UI_PORT (default varies by mode)

**🌐 Main Dashboard**: http://localhost:5173  
**🎤 Speaker Recognition**: http://localhost:5174  
**❌ No microphone access** - browsers require HTTPS for microphone

---

## HTTPS Mode (For Microphone Access)

| Service | HTTP Port | HTTPS Port | Access URL | Microphone Access |
|---------|-----------|------------|------------|-------------------|
| **Advanced Backend** | 80→443 | 443 | https://localhost/ (Main)<br>https://localhost/api/ (API) | ✅ Yes |
| **Speaker Recognition** | 8081→8444 | 8444 | https://localhost:8444/ (Main)<br>https://localhost:8444/api/ (API) | ✅ Yes |

**IMPORTANT**: nginx services start automatically with the standard docker compose command

**🌐 Main Dashboard**: https://localhost/ (Advanced Backend with SSL)  
**🎤 Speaker Recognition**: https://localhost:8444/ (Speaker Recognition with SSL)  
**✅ Full microphone access** - both services secured with SSL

### Port Details (HTTPS Mode)
- **Advanced Backend nginx**: Ports 80 (HTTP redirect) + 443 (HTTPS)
- **Speaker Recognition nginx**: Ports 8081 (HTTP redirect) + 8444 (HTTPS)
- **No port conflicts** - different port ranges for each service

---

## Why Two Modes?

### HTTP Mode (Default)
✅ **Simple setup** - No SSL certificates needed  
✅ **Development friendly** - Quick start for testing  
❌ **No microphone access** - Browsers require HTTPS for microphone

### HTTPS Mode (Advanced)
✅ **Microphone access** - Browsers allow mic access over HTTPS  
✅ **Production ready** - Secure for real deployments  
❌ **Complex setup** - Requires SSL certificate generation

---

## Configuration Files

### Speaker Recognition Modes

The speaker recognition service supports both modes via configuration:

**HTTP Mode (.env)**:
```bash
REACT_UI_PORT=5174  # Direct HTTP access
REACT_UI_HTTPS=false
```

**HTTPS Mode (.env)**:
```bash
REACT_UI_PORT=5175  # Internal HTTPS port (proxied through nginx)
REACT_UI_HTTPS=true
# nginx provides external access on ports 8081 (HTTP redirect) and 8444 (HTTPS)
# Start with: docker compose up -d
```

---

## Service Management Commands

**Convenience Scripts (Recommended):**
```bash
# Check what's running
./status.sh

# Start all configured services
./start.sh

# Restart all services
./restart.sh

# Stop all services
./stop.sh
```

**Note**: Convenience scripts wrap the longer `uv run --with-requirements setup-requirements.txt python` commands for ease of use.

<details>
<summary>Full commands (click to expand)</summary>

```bash
# Check what's running
uv run --with-requirements setup-requirements.txt python services.py status

# Start all services
uv run --with-requirements setup-requirements.txt python services.py start --all --build

# Start only specific services
uv run --with-requirements setup-requirements.txt python services.py start backend speaker-recognition

# Restart all services
uv run --with-requirements setup-requirements.txt python services.py restart --all

# Restart specific services
uv run --with-requirements setup-requirements.txt python services.py restart backend

# Stop all services
uv run --with-requirements setup-requirements.txt python services.py stop --all
```

</details>

**Important:** Use `restart` for configuration changes (.env updates). For code changes, use `stop` + `start --build` to rebuild images.

---

## Microphone Access Requirements

For **speaker recognition** and **live audio features** to work:

1. **Local development**: Use HTTP mode, access via `http://localhost:5174`
   - Some browsers allow localhost microphone access over HTTP
   
2. **Production/Remote access**: Use HTTPS mode, access via `https://localhost:8444`
   - All browsers require HTTPS for microphone access over network

3. **Mixed setup**: Keep backend on HTTP, only enable HTTPS for speaker recognition when needed

---

## Port Conflict Resolution

If you encounter port conflicts:

1. **Check running services**: `uv run --with-requirements setup-requirements.txt python services.py status`
2. **Stop conflicting services**: `uv run --with-requirements setup-requirements.txt python services.py stop --all`
3. **Change ports in .env files** if needed
4. **Restart services**: `uv run --with-requirements setup-requirements.txt python services.py restart --all` or `./restart.sh`

---

## Summary: Default User Experience

After `git clone` and running init + services:

🌐 **Main Application**: http://localhost:5173  
🎤 **Speaker Recognition**: http://localhost:5174 (HTTP) or https://localhost:8444 (HTTPS)  
🔧 **Backend API**: http://localhost:8000  
📝 **ASR Service**: http://localhost:8767  
🧠 **Memory Service**: http://localhost:8765