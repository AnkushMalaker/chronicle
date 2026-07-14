// serviceManager.ts — client for the host service-manager agent (edge/service_manager.py).
//
// Two transports, auto-selected:
//   • via backend proxy (/api/admin/services*) using the existing admin JWT —
//     works whenever the backend is up, needs no SM token.
//   • direct to the SM agent (host:8775) using a stored SM token — the only path
//     that works when the BACKEND ITSELF is down (i.e. the "start the backend"
//     case). The SM URL is taken from storage, or derived from the backend host
//     on :8775 and confirmed via the unauthed /health probe.

import { fetchAuthed, deriveBaseUrl } from './auth';
import { getServiceManagerUrl, getServiceManagerToken } from '../utils/storage';

export interface ServiceInfo {
  name: string;
  // The agent reports health as a string, not a running boolean.
  health?: 'healthy' | 'partial' | 'starting' | 'unhealthy' | 'stopped' | string;
  enabled?: boolean;
  health_detail?: string;
  node?: string;
  remote?: boolean;
  provider?: unknown;
  description?: string;
  ports?: unknown;
  [key: string]: unknown;
}

export interface ServicesResult {
  available: boolean;
  reason?: string;
  services?: ServiceInfo[];
  [key: string]: unknown;
}

export interface ServiceOperation {
  id?: string;
  node?: string | null;
  status?: string;
  [key: string]: unknown;
}

export interface ServiceActionResult {
  operation?: ServiceOperation;
  [key: string]: unknown;
}

/** Derive the SM URL from a backend URL by swapping to its host on :8775.
 * The agent serves plain HTTP on the tailnet (no TLS), so we always use http. */
export const deriveServiceManagerUrl = (backendUrl: string): string | null => {
  try {
    const base = deriveBaseUrl(backendUrl);
    const u = new URL(base);
    return `http://${u.hostname}:8775`;
  } catch {
    return null;
  }
};

/** Resolve the SM base URL: stored value first, else derived from the backend. */
const resolveSmUrl = async (backendUrl: string): Promise<string | null> => {
  const stored = await getServiceManagerUrl();
  if (stored) return stored.replace(/\/+$/, '');
  return deriveServiceManagerUrl(backendUrl);
};

/** True if the SM agent answers its unauthed /health. */
export const isServiceManagerReachable = async (backendUrl: string): Promise<boolean> => {
  const smUrl = await resolveSmUrl(backendUrl);
  if (!smUrl) return false;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 6000);
    const resp = await fetch(`${smUrl}/health`, { signal: controller.signal });
    clearTimeout(timer);
    return resp.ok;
  } catch {
    return false;
  }
};

const directHeaders = (token: string): HeadersInit => ({
  'Content-Type': 'application/json',
  Authorization: `Bearer ${token}`,
});

interface NodeInfo {
  host?: string;
  tailscale?: { dns?: string | null; ip?: string | null };
}

/** Probe a candidate backend base URL's /health (short timeout). */
const probeHealth = async (baseUrl: string): Promise<boolean> => {
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    const resp = await fetch(`${baseUrl}/health`, { signal: controller.signal });
    clearTimeout(timer);
    // 200 (healthy) or 401/403 (reachable but needs auth) both prove the host.
    return resp.ok || resp.status === 401 || resp.status === 403;
  } catch {
    return false;
  }
};

/**
 * Recover the backend HTTP base URL when it's been lost but the service-manager
 * URL + token survive. The SM host IS the backend host, so we ask the SM /node
 * for its canonical Tailscale name/IP and probe the usual backend addresses
 * (HTTPS on :443, HTTP on :8000) until one answers /health. Returns the working
 * HTTP base URL, or null if recovery isn't possible.
 *
 * The token alone cannot recover anything — it carries no address — which is why
 * we always persist the SM URL beside it.
 */
export const recoverBackendUrl = async (): Promise<string | null> => {
  const smUrl = (await getServiceManagerUrl())?.replace(/\/+$/, '');
  const token = await getServiceManagerToken();
  if (!smUrl) return null;

  // Candidate hosts, best first: the SM URL's own host always works as a
  // fallback; /node may add the canonical MagicDNS name and tailnet IP.
  const hosts: string[] = [];
  try {
    hosts.push(new URL(smUrl).hostname);
  } catch {
    // ignore malformed SM URL
  }

  if (token) {
    try {
      const resp = await fetch(`${smUrl}/node`, { headers: directHeaders(token) });
      if (resp.ok) {
        const node = (await resp.json()) as NodeInfo;
        const dns = node.tailscale?.dns;
        const ip = node.tailscale?.ip;
        if (dns) hosts.unshift(dns);
        if (ip) hosts.push(ip);
      }
    } catch (e) {
      console.log('[ServiceManager] /node lookup failed during recovery:', e);
    }
  }

  // De-dupe while preserving order, then probe candidate URLs.
  const seen = new Set<string>();
  for (const host of hosts) {
    if (!host || seen.has(host)) continue;
    seen.add(host);
    for (const candidate of [`https://${host}`, `http://${host}:8000`]) {
      if (await probeHealth(candidate)) {
        console.log('[ServiceManager] Recovered backend URL:', candidate);
        return candidate;
      }
    }
  }

  return null;
};

/**
 * List host-managed services. Prefers the backend proxy; on proxy failure (e.g.
 * backend down) falls back to a direct SM call when a token is available.
 */
export const listServices = async (backendUrl: string): Promise<ServicesResult> => {
  const base = deriveBaseUrl(backendUrl);

  // Transport 1: backend proxy (admin JWT).
  try {
    const resp = await fetchAuthed(`${base}/api/admin/services`);
    if (resp.ok) {
      return (await resp.json()) as ServicesResult;
    }
  } catch (e) {
    console.log('[ServiceManager] proxy listServices failed, trying direct:', e);
  }

  // Transport 2: direct to SM (needs URL + token).
  const smUrl = await resolveSmUrl(backendUrl);
  const token = await getServiceManagerToken();
  if (smUrl && token) {
    const resp = await fetch(`${smUrl}/services`, { headers: directHeaders(token) });
    if (resp.ok) {
      return { available: true, ...(await resp.json()) };
    }
    return { available: false, reason: `sm_http_${resp.status}` };
  }

  return { available: false, reason: 'unreachable' };
};

/** Start/stop/restart a service. Proxy first, direct fallback. */
export const serviceAction = async (
  backendUrl: string,
  name: string,
  action: 'start' | 'stop' | 'restart',
  node?: string
): Promise<ServiceActionResult> => {
  const base = deriveBaseUrl(backendUrl);
  const body = JSON.stringify(node ? { node } : {});

  try {
    const resp = await fetchAuthed(`${base}/api/admin/services/${name}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body,
    });
    if (resp.ok) return (await resp.json()) as ServiceActionResult;
    // A 4xx/5xx from a reachable backend is a real error — surface it.
    if (resp.status !== 502 && resp.status !== 503) {
      throw new Error(`Service action failed (HTTP ${resp.status})`);
    }
  } catch (e) {
    console.log('[ServiceManager] proxy serviceAction failed, trying direct:', e);
  }

  const smUrl = await resolveSmUrl(backendUrl);
  const token = await getServiceManagerToken();
  if (smUrl && token) {
    const resp = await fetch(`${smUrl}/services/${name}/${action}`, {
      method: 'POST',
      headers: directHeaders(token),
      body,
    });
    if (resp.ok) return (await resp.json()) as ServiceActionResult;
    throw new Error(`Service action failed (HTTP ${resp.status})`);
  }

  throw new Error('Cannot reach the service manager (no stored token).');
};

/** Poll a long-running start/stop/build operation. Proxy first, direct fallback. */
export const getOperation = async (
  backendUrl: string,
  operationId: string,
  node?: string | null
): Promise<ServiceOperation> => {
  const base = deriveBaseUrl(backendUrl);
  const query = node ? `?node=${encodeURIComponent(node)}` : '';

  try {
    const resp = await fetchAuthed(`${base}/api/admin/services/operations/${operationId}${query}`);
    if (resp.ok) return (await resp.json()) as ServiceOperation;
  } catch (e) {
    console.log('[ServiceManager] proxy getOperation failed, trying direct:', e);
  }

  const smUrl = await resolveSmUrl(backendUrl);
  const token = await getServiceManagerToken();
  if (smUrl && token) {
    const resp = await fetch(`${smUrl}/operations/${operationId}`, { headers: directHeaders(token) });
    if (resp.ok) return (await resp.json()) as ServiceOperation;
  }

  throw new Error('Cannot poll operation status.');
};
