import { useState } from 'react'
import { Code, Layout, Sparkles } from 'lucide-react'
import PluginSettings from '../components/PluginSettings'
import PluginSettingsForm from '../components/PluginSettingsForm'
import PluginAssistant from '../components/plugins/PluginAssistant'
import { Tabs } from '../components/ui'

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
        <Tabs
          tabs={tabs.map((tab) => ({ value: tab.key, label: tab.label, icon: tab.icon }))}
          value={viewMode}
          onChange={setViewMode}
          variant="pill"
        />
      </div>

      {/* Content */}
      {viewMode === 'form' && <PluginSettingsForm />}
      {viewMode === 'assistant' && <PluginAssistant />}
      {viewMode === 'yaml' && <PluginSettings />}
    </div>
  )
}
