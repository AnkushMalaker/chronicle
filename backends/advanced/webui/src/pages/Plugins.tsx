import { useState } from 'react'
import { Code, Layout, Sparkles } from 'lucide-react'
import PluginSettings from '../components/PluginSettings'
import PluginSettingsForm from '../components/PluginSettingsForm'
import PluginAssistant from '../components/plugins/PluginAssistant'

type ViewMode = 'form' | 'assistant' | 'yaml'

export default function Plugins() {
  const [viewMode, setViewMode] = useState<ViewMode>('form')

  const tabs: { key: ViewMode; label: string; icon: React.ReactNode }[] = [
    { key: 'form', label: 'Form', icon: <Layout className="h-4 w-4" /> },
    { key: 'assistant', label: 'AI Assistant', icon: <Sparkles className="h-4 w-4" /> },
    { key: 'yaml', label: 'YAML', icon: <Code className="h-4 w-4" /> },
  ]

  return (
    <div className="p-6">
      {/* View Mode Toggle */}
      <div className="mb-6 flex justify-end">
        <div className="inline-flex rounded-lg border border-gray-300 dark:border-gray-600 overflow-hidden">
          {tabs.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setViewMode(tab.key)}
              className={`flex items-center space-x-2 px-4 py-2 text-sm transition-colors ${
                viewMode === tab.key
                  ? 'bg-blue-600 text-white'
                  : 'bg-gray-100 dark:bg-gray-700 text-gray-700 dark:text-gray-200 hover:bg-gray-200 dark:hover:bg-gray-600'
              }`}
            >
              {tab.icon}
              <span>{tab.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Content */}
      {viewMode === 'form' && <PluginSettingsForm />}
      {viewMode === 'assistant' && <PluginAssistant />}
      {viewMode === 'yaml' && <PluginSettings />}
    </div>
  )
}
