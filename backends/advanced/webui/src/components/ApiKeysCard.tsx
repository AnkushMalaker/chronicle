import { Key } from 'lucide-react'
import ApiKeysPanel from './ApiKeysPanel'

/**
 * Settings-page card for the logged-in user's own API keys.
 *
 * Long-lived credentials for clients that store one secret and never see a
 * login form again (Handy dictation, relays, sync daemons). Admins manage other
 * users' keys from the Users page instead.
 */
export default function ApiKeysCard() {
  return (
    <div className="bg-white dark:bg-gray-800 rounded-lg border border-gray-200 dark:border-gray-700 p-6">
      <h3 className="text-lg font-semibold text-gray-900 dark:text-gray-100 mb-4 flex items-center">
        <Key className="h-5 w-5 mr-2 text-blue-600" />
        API Keys
      </h3>

      <p className="text-sm text-gray-600 dark:text-gray-400 mb-4">
        Long-lived credentials for clients that can't log in again — dictation apps, relays,
        sync daemons. Send as <code className="text-xs">Authorization: Bearer &lt;key&gt;</code>,
        the same header a JWT uses, so anywhere that asks for an "API key" works. Unlike a login
        token these don't expire after 24 hours.
      </p>

      <ApiKeysPanel />
    </div>
  )
}
