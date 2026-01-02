# Setup Scripts Guide

This document explains the different setup scripts available in Friend-Lite and when to use each one.

## Script Overview

| Script | Purpose | When to Use |
|--------|---------|-------------|
| `init.py` | **Main interactive setup wizard** | **Recommended for all users** - First time setup with guided configuration (located at repo root). Memory now configured in `config/config.yml`. |
| `setup-https.sh` | HTTPS certificate generation | **Optional** - When you need secure connections for microphone access |

## Main Setup Script: `init.py`

**Purpose**: Interactive wizard that configures all services with guided prompts.

### What it does:
- ✅ **Authentication Setup**: Admin email/password with secure key generation
- ✅ **Transcription Provider Selection**: Choose between Deepgram, Mistral, or Offline (Parakeet)
- ✅ **LLM Provider Configuration**: Choose between OpenAI (recommended) or Ollama
- ✅ **Memory Provider Setup**: Choose between Friend-Lite Native or OpenMemory MCP
- ✅ **API Key Collection**: Prompts for required keys with helpful links to obtain them
- ✅ **Optional Services**: Speaker Recognition, network configuration
- ✅ **Configuration Validation**: Creates complete .env with all settings

### Usage:
```bash
# From repository root
python backends/advanced/init.py
```

### Example Flow:
```
🚀 Friend-Lite Interactive Setup
===============================================

► Authentication Setup
----------------------
Admin email [admin@example.com]: john@company.com
Admin password (min 8 chars): ********
✅ Admin account configured

► Speech-to-Text Configuration
-------------------------------
Choose your transcription provider:
  1) Deepgram (recommended - high quality, requires API key)
  2) Mistral (Voxtral models - requires API key)
  3) Offline (Parakeet ASR - requires GPU, runs locally)
  4) None (skip transcription setup)
Enter choice (1-4) [1]: 1

Get your API key from: https://console.deepgram.com/
Deepgram API key: dg_xxxxxxxxxxxxx
✅ Deepgram configured

► LLM Provider Configuration
----------------------------
Choose your LLM provider for memory extraction:
  1) OpenAI (GPT-4, GPT-3.5 - requires API key)
  2) Ollama (local models - requires Ollama server)
  3) Skip (no memory extraction)
Enter choice (1-3) [1]: 1

Get your API key from: https://platform.openai.com/api-keys
OpenAI API key: sk-xxxxxxxxxxxxx
OpenAI model [gpt-4o-mini]: gpt-4o-mini
✅ OpenAI configured

...continues through all configuration sections...

► Configuration Summary
-----------------------
✅ Admin Account: john@company.com
✅ Transcription: deepgram
✅ LLM Provider: openai
✅ Memory Provider: friend_lite
✅ Backend URL: http://localhost:8000
✅ Dashboard URL: http://localhost:5173

► Next Steps
------------
1. Start the main services:
   docker compose up --build -d

2. Access the dashboard:
   http://localhost:5173

Setup complete! 🎉
```

## HTTPS Setup Script: `setup-https.sh`

**Purpose**: Generate SSL certificates and configure nginx for secure HTTPS access.

### When needed:
- **Microphone access** from browsers (HTTPS required)
- **Remote access** via Tailscale or network
- **Production deployments** requiring secure connections

### Usage:
```bash
cd backends/advanced
./setup-https.sh 100.83.66.30  # Your Tailscale or network IP
```

### What it does:
- Generates self-signed SSL certificates for your IP
- Configures nginx proxy for HTTPS access
- Configures nginx for automatic HTTPS access
- Provides HTTPS URLs for dashboard access

### After HTTPS setup:
```bash
# Start services with HTTPS
docker compose up --build -d

# Access via HTTPS
https://localhost/
https://100.83.66.30/  # Your configured IP
```


## Recommended Setup Flow

### New Users (Recommended):
1. **Run main setup**: `python backends/advanced/init.py`
2. **Start services**: `docker compose up --build -d`
3. **Optional HTTPS**: `./setup-https.sh your-ip` (if needed)

### Manual Configuration (Advanced):
1. **Copy template**: `cp .env.template .env`
2. **Edit manually**: Configure all providers and keys
3. **Start services**: `docker compose up --build -d`

## Script Locations

Setup scripts are located as follows:
```
.                     # Project root
├── init.py           # Main interactive setup wizard (repo root)
└── backends/advanced/
    ├── setup-https.sh    # HTTPS certificate generation  
    ├── .env.template     # Environment template
    └── docker-compose.yml
```

## Getting Help

- **Setup Issues**: See `Docs/quickstart.md` for detailed documentation
- **Configuration**: See `MEMORY_PROVIDERS.md` for provider comparisons
- **Troubleshooting**: Check `CLAUDE.md` for common issues
- **HTTPS Problems**: Ensure your IP is accessible and not behind firewall

## Key Benefits of New Setup

✅ **No more guessing**: Interactive prompts guide you through every choice  
✅ **API key validation**: Links provided to obtain required keys  
✅ **Provider selection**: Choose best services for your needs  
✅ **Complete configuration**: Creates working .env with all settings  
✅ **Next steps guidance**: Clear instructions for starting services  
✅ **No manual editing**: Reduces errors from manual .env editing
