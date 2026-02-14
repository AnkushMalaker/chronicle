import { useState } from 'react'
import Conversations from './Conversations'
import Archive from './Archive'

export default function ConversationsRouter() {
  const [activeTab, setActiveTab] = useState<'classic' | 'archive'>('classic')

  return (
    <div>
      {/* Tab Navigation */}
      <div className="mb-6 border-b border-gray-200 dark:border-gray-700">
        <nav className="-mb-px flex space-x-8">
          <button
            onClick={() => setActiveTab('classic')}
            className={`
              py-4 px-1 border-b-2 font-medium text-sm transition-colors
              ${activeTab === 'classic'
                ? 'border-blue-600 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300 dark:text-gray-400 dark:hover:text-gray-300'
              }
            `}
          >
            Classic View
          </button>
          <button
            onClick={() => setActiveTab('archive')}
            className={`
              py-4 px-1 border-b-2 font-medium text-sm transition-colors
              ${activeTab === 'archive'
                ? 'border-orange-600 text-orange-600 dark:text-orange-400'
                : 'border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300 dark:text-gray-400 dark:hover:text-gray-300'
              }
            `}
          >
            Archive
          </button>
        </nav>
      </div>

      {/* Content */}
      {activeTab === 'classic' ? (
        <Conversations />
      ) : (
        <Archive />
      )}
    </div>
  )
}
