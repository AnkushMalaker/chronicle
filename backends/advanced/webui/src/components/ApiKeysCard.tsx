import { useEffect, useState } from 'react'
import { Key, Plus, Trash2, Copy, Check } from 'lucide-react'
import { apiKeysApi } from '../services/api'
import { Alert, Button, IconButton, Input, Modal } from './ui'

interface ApiKey {
  id: string
  name: string
  key_prefix: string
  created_at: string
  last_used_at: string | null
  expires_at: string | null
}

function formatDate(value: string | null): string {
  if (!value) return 'Never'
  return new Date(value).toLocaleString()
}

/**
 * Mint and revoke long-lived API keys.
 *
 * These exist for clients that store one credential and never see a login form
 * again (Handy dictation, relays, sync daemons). A JWT expires after 24h; an
 * API key does not. Both are sent as `Authorization: Bearer <token>`, so a
 * client with only an "API key" field works unchanged.
 */
export default function ApiKeysCard() {
  const [keys, setKeys] = useState<ApiKey[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [showCreate, setShowCreate] = useState(false)
  const [newName, setNewName] = useState('')
  const [expiresInDays, setExpiresInDays] = useState('')
  const [creating, setCreating] = useState(false)
  // Held in memory only — the backend never returns the token again.
  const [mintedToken, setMintedToken] = useState('')
  const [copied, setCopied] = useState(false)

  const load = async () => {
    try {
      const response = await apiKeysApi.list()
      setKeys(response.data)
      setError('')
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to load API keys')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const create = async () => {
    if (!newName.trim()) return
    setCreating(true)
    try {
      const days = expiresInDays.trim() ? parseInt(expiresInDays, 10) : undefined
      const response = await apiKeysApi.create(newName.trim(), days)
      setMintedToken(response.data.token)
      setShowCreate(false)
      setNewName('')
      setExpiresInDays('')
      setCopied(false)
      await load()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to create API key')
    } finally {
      setCreating(false)
    }
  }

  const revoke = async (key: ApiKey) => {
    if (!confirm(`Revoke "${key.name}"? Any client using it will stop working immediately.`)) return
    try {
      await apiKeysApi.revoke(key.id)
      await load()
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to revoke API key')
    }
  }

  const copyToken = async () => {
    await navigator.clipboard.writeText(mintedToken)
    setCopied(true)
  }

  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 flex items-center">
          <Key className="h-5 w-5 mr-2 text-blue-600" />
          API Keys
        </h3>
        <Button variant="primary" size="sm" onClick={() => setShowCreate(true)}>
          <Plus className="h-4 w-4 mr-1" />
          New Key
        </Button>
      </div>

      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        Long-lived credentials for clients that can't log in again — dictation apps, relays,
        sync daemons. Send as <code className="text-xs">Authorization: Bearer &lt;key&gt;</code>,
        the same header a JWT uses, so anywhere that asks for an "API key" works. Unlike a login
        token these don't expire after 24 hours.
      </p>

      {error && <Alert tone="danger" className="mb-4">{error}</Alert>}

      {loading ? (
        <div className="text-sm text-gray-500 dark:text-gray-400">Loading…</div>
      ) : keys.length === 0 ? (
        <div className="text-sm text-gray-500 dark:text-gray-400 p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
          No API keys yet.
        </div>
      ) : (
        <div className="space-y-2">
          {keys.map(key => (
            <div
              key={key.id}
              className="flex items-center justify-between p-3 bg-gray-50 dark:bg-gray-700 rounded-md"
            >
              <div className="min-w-0">
                <div className="font-medium text-gray-900 dark:text-gray-100 truncate">
                  {key.name}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 font-mono">
                  chrn_{key.key_prefix}_…
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Last used: {formatDate(key.last_used_at)} · Expires: {formatDate(key.expires_at)}
                </div>
              </div>
              <IconButton label={`Revoke ${key.name}`} danger onClick={() => revoke(key)}>
                <Trash2 className="h-4 w-4" />
              </IconButton>
            </div>
          ))}
        </div>
      )}

      {showCreate && (
        <Modal
          open
          onClose={() => setShowCreate(false)}
          title="New API Key"
          maxWidthClassName="max-w-md"
          footer={
            <>
              <Button variant="secondary" size="md" onClick={() => setShowCreate(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                size="md"
                onClick={create}
                disabled={creating || !newName.trim()}
              >
                {creating ? 'Creating…' : 'Create Key'}
              </Button>
            </>
          }
        >
          <div className="space-y-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                Name <span className="text-red-500">*</span>
              </label>
              <Input
                value={newName}
                onChange={e => setNewName(e.target.value)}
                placeholder="e.g. Handy dictation (laptop)"
                autoFocus
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
                Expires after (days)
              </label>
              <Input
                type="number"
                value={expiresInDays}
                onChange={e => setExpiresInDays(e.target.value)}
                placeholder="Leave blank to never expire"
              />
            </div>
          </div>
        </Modal>
      )}

      {mintedToken && (
        <Modal
          open
          onClose={() => setMintedToken('')}
          title="Copy your API key"
          maxWidthClassName="max-w-lg"
          footer={
            <Button variant="primary" size="md" onClick={() => setMintedToken('')}>
              Done
            </Button>
          }
        >
          <Alert tone="warning" className="mb-3">
            This is the only time the key is shown. It isn't stored in a recoverable form —
            if you lose it, revoke the key and mint a new one.
          </Alert>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-xs font-mono break-all p-3 bg-gray-100 dark:bg-gray-900 rounded-md">
              {mintedToken}
            </code>
            <IconButton label="Copy API key" onClick={copyToken}>
              {copied ? <Check className="h-4 w-4 text-green-600" /> : <Copy className="h-4 w-4" />}
            </IconButton>
          </div>
        </Modal>
      )}
    </div>
  )
}
