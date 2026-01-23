import { useState } from 'react'
import { Code, Layout } from 'lucide-react'
import PluginSettings from '../components/PluginSettings'
import PluginSettingsForm from '../components/PluginSettingsForm'

export default function Plugins() {
  const [useFormUI, setUseFormUI] = useState(true)

  return (
    <div className="p-6">
      {/* Toggle Button */}
      <div className="mb-6 flex justify-end">
        <button
          onClick={() => setUseFormUI(!useFormUI)}
          className="flex items-center space-x-2 px-4 py-2 bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 rounded-lg hover:bg-gray-200 dark:hover:bg-gray-600 transition-colors border border-gray-300 dark:border-gray-600"
        >
          {useFormUI ? (
            <>
              <Code className="h-4 w-4" />
              <span>Advanced: Edit YAML</span>
            </>
          ) : (
            <>
              <Layout className="h-4 w-4" />
              <span>← Back to Form</span>
            </>
          )}
        </button>
      </div>

      {/* Content */}
      {useFormUI ? (
        <PluginSettingsForm />
      ) : (
        <PluginSettings />
      )}
    </div>
  )
}
