# SSL Certificates & HTTPS

Chronicle uses automatic HTTPS setup for secure microphone access and remote connections.

## Why HTTPS is Needed

Modern browsers require HTTPS for:
- **Microphone access** over network (not localhost)
- **Secure WebSocket connections** (WSS)
- **Remote access** via Tailscale/VPN
- **Production deployments**

## SSL Implementation

### Advanced Backend → Caddy

The main backend uses **Caddy** for automatic HTTPS:

**Configuration**: `backends/advanced/Caddyfile`
**Activation**: Caddy starts when using `--profile https` or when wizard enables HTTPS
**Certificate**: Self-signed for local/Tailscale IPs, automatic Let's Encrypt for domains

**Ports**:
- `443` - HTTPS (main access)
- `80` - HTTP (redirects to HTTPS)

**Access**: `https://localhost` or `https://your-tailscale-ip`

### Speaker Recognition → nginx

The speaker recognition service uses **nginx** for HTTPS:

**Configuration**: `extras/speaker-recognition/nginx.conf`
**Certificate**: Self-signed via `ssl/generate-ssl.sh`

**Ports**:
- `8444` - HTTPS
- `8081` - HTTP (redirects to HTTPS)

**Access**: `https://localhost:8444`

## Setup via Wizard

When you run `./wizard.sh`, the setup wizard:
1. Asks if you want to enable HTTPS
2. Prompts for your Tailscale IP or domain
3. Generates SSL certificates automatically
4. Configures Caddy/nginx as needed
5. Updates CORS settings for HTTPS origins

**No manual setup required** - the wizard handles everything.

## Browser Certificate Warnings

Since we use self-signed certificates for local/Tailscale IPs, browsers will show security warnings:

1. Click "Advanced"
2. Click "Proceed to localhost (unsafe)" or similar
3. Microphone access will now work

For production with real domains, Caddy automatically obtains valid Let's Encrypt certificates.

## Troubleshooting

**HTTPS not working**:
- Check Caddy/nginx containers are running: `docker compose ps`
- Verify certificates exist: `ls backends/advanced/ssl/` or `ls extras/speaker-recognition/ssl/`
- Check you're using `https://` not `http://`

**Microphone not accessible**:
- Ensure you're accessing via HTTPS (not HTTP)
- Accept browser certificate warning
- Verify you're not using `localhost` from remote device (use Tailscale IP instead)
