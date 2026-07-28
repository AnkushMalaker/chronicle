import { useCallback, useEffect, useState } from 'react'
import { Plus, Trash2, Copy, Check } from 'lucide-react'
import { apiKeysApi } from '../services/api'
import { Alert, Button, IconButton, Input, Modal } from './ui'

export interface ApiKey {
  id: string
  user_id: string
  name: string
  key_prefix: string
  created_at: string
  last_used_at: string | null
  expires_at: string | null
}

function formatDate(value: string | null, empty = 'Never'): string {
  if (!value) return empty
  return new Date(value).toLocaleString()
}

/**
 * List, mint and revoke API keys — for the logged-in user, or (admins only) for
 * another user via `userId`.
 *
 * Existing keys show only their public prefix: Chronicle stores sha256(secret),
 * so a key's token genuinely cannot be re-displayed. The full token appears
 * exactly once, in the modal shown right after minting.
 */
export default function ApiKeysPanel({
  userId,
  emptyHint = 'No API keys yet.',
}: {
  userId?: string
  emptyHint?: string
}) {
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

  const load = useCallback(async () => {
    try {
      const response = await apiKeysApi.list(userId)
      setKeys(response.data)
      setError('')
    } catch (e: any) {
      setError(e?.response?.data?.detail || 'Failed to load API keys')
    } finally {
      setLoading(false)
    }
  }, [userId])

  useEffect(() => { load() }, [load])

  const create = async () => {
    if (!newName.trim()) return
    setCreating(true)
    try {
      const days = expiresInDays.trim() ? parseInt(expiresInDays, 10) : undefined
      const response = await apiKeysApi.create(newName.trim(), days, userId)
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
    <div>
      {error && <Alert tone="danger" className="mb-3">{error}</Alert>}

      {loading ? (
        <div className="text-sm text-gray-500 dark:text-gray-400">Loading…</div>
      ) : keys.length === 0 ? (
        <div className="text-sm text-gray-500 dark:text-gray-400 p-3 bg-gray-50 dark:bg-gray-700 rounded-md">
          {emptyHint}
        </div>
      ) : (
        <div className="space-y-2">
          {keys.map(key => (
            <div
              key={key.id}
              className="flex items-center justify-between gap-3 p-3 bg-gray-50 dark:bg-gray-700 rounded-md"
            >
              <div className="min-w-0">
                <div className="font-medium text-gray-900 dark:text-gray-100 truncate">
                  {key.name}
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 font-mono">
                  chrn_{key.key_prefix}_…
                </div>
                <div className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                  Created {formatDate(key.created_at)} · Last used{' '}
                  {formatDate(key.last_used_at, 'never')} · Expires{' '}
                  {formatDate(key.expires_at)}
                </div>
              </div>
              <IconButton label={`Revoke ${key.name}`} danger onClick={() => revoke(key)}>
                <Trash2 className="h-4 w-4" />
              </IconButton>
            </div>
          ))}
        </div>
      )}

      <div className="mt-3">
        <Button variant="secondary" size="sm" onClick={() => setShowCreate(true)}>
          <Plus className="h-4 w-4 mr-1" />
          New Key
        </Button>
      </div>

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
