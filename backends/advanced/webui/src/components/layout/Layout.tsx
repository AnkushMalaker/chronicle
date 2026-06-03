import { Link, useLocation, Outlet } from 'react-router-dom'
import { Music, MessageSquare, MessageCircle, Users, Upload, Settings, LogOut, Sun, Moon, Shield, Radio, Layers, Puzzle, Zap, Activity, Network, Sparkles } from 'lucide-react'
import { useAuth } from '../../contexts/AuthContext'
import { useTheme } from '../../contexts/ThemeContext'
import { useSSE, SSEStatus } from '../../hooks/useSSE'
import GlobalRecordingIndicator from './GlobalRecordingIndicator'
import UserLoopModal from '../UserLoopModal'

export default function Layout() {
  const location = useLocation()
  const { user, logout, isAdmin } = useAuth()
  const { isDark, toggleTheme } = useTheme()

  // Single SSE connection for real-time updates across all pages
  const sseStatus = useSSE()

  const navigationItems = [
    { path: '/live-record', label: 'Live Record', icon: Radio },
    { path: '/chat', label: 'Chat', icon: MessageCircle },
    { path: '/conversations', label: 'Conversations', icon: MessageSquare },
    { path: '/users', label: 'User Management', icon: Users },

    ...(isAdmin ? [
      { path: '/upload', label: 'Upload Audio', icon: Upload },
      { path: '/data-cleaning', label: 'Data Cleaning', icon: Sparkles },
      { path: '/queue', label: 'Queue & Events', icon: Layers },
      { path: '/plugins', label: 'Plugins', icon: Puzzle },
      { path: '/finetuning', label: 'Fine-tuning', icon: Zap },
      { path: '/network', label: 'Network', icon: Network },
      { path: '/system', label: 'System Status', icon: Activity },
      { path: '/settings', label: 'Settings', icon: Settings },
    ] : []),
  ]

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900">
      {/* Header */}
      <header className="bg-white dark:bg-gray-800 shadow-sm border-b border-gray-200 dark:border-gray-700">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
          <div className="flex justify-between items-center h-16">
            <div className="flex items-center space-x-4">
              <Music className="h-8 w-8 text-blue-600" />
              <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
                Chronicle Dashboard
              </h1>
            </div>
            <div className="flex items-center space-x-4">
              {/* SSE connection status */}
              <SSEIndicator status={sseStatus} />

              {/* Global Recording Indicator */}
              <GlobalRecordingIndicator />

              <button
                onClick={toggleTheme}
                className="p-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors text-gray-600 dark:text-gray-300"
                aria-label="Toggle theme"
              >
                {isDark ? <Sun className="h-5 w-5" /> : <Moon className="h-5 w-5" />}
              </button>

              {/* User info */}
              <div className="flex items-center space-x-2 text-sm text-gray-700 dark:text-gray-300">
                <div className="flex items-center space-x-1">
                  {isAdmin && <Shield className="h-4 w-4 text-blue-600" />}
                  <span>{user?.name || user?.email}</span>
                </div>
              </div>

              <button
                onClick={logout}
                className="flex items-center space-x-2 px-3 py-2 rounded-lg hover:bg-gray-100 dark:hover:bg-gray-700 transition-colors text-gray-600 dark:text-gray-300"
                aria-label="Logout"
              >
                <LogOut className="h-4 w-4" />
                <span className="text-sm">Logout</span>
              </button>
            </div>
          </div>
        </div>
      </header>

      <div className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="flex flex-col lg:flex-row gap-8">
          {/* Sidebar Navigation */}
          <nav className="lg:w-64 flex-shrink-0">
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-4">
              <ul className="space-y-2">
                {navigationItems.map(({ path, label, icon: Icon }) => (
                  <li key={path}>
                    <Link
                      to={path}
                      className={`flex items-center space-x-3 px-3 py-2 rounded-md text-sm font-medium transition-colors ${
                        location.pathname === path
                          ? 'bg-blue-100 text-blue-900 dark:bg-blue-900 dark:text-blue-100'
                          : 'text-gray-700 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
                      }`}
                    >
                      <Icon className="h-5 w-5" />
                      <span>{label}</span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          </nav>

          {/* Main Content */}
          <main className="flex-1 min-w-0">
            <div className="bg-white dark:bg-gray-800 rounded-lg shadow-sm border border-gray-200 dark:border-gray-700 p-6">
              <Outlet />
            </div>
          </main>
        </div>
      </div>

      {/* Footer */}
      <footer className="bg-white dark:bg-gray-800 border-t border-gray-200 dark:border-gray-700 mt-auto">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-4">
          <div className="text-center text-sm text-gray-500 dark:text-gray-400">
            🎵 Chronicle Dashboard v1.0 | AI-powered personal audio system
          </div>
        </div>
      </footer>

      {/* User Loop: AI suggestion review modal (auto-opens when suggestions exist) */}
      <UserLoopModal />
    </div>
  )
}

const sseStatusConfig: Record<SSEStatus, { color: string; label: string }> = {
  connected:    { color: 'bg-green-500', label: 'Live' },
  connecting:   { color: 'bg-gray-400',  label: 'Connecting' },
  reconnecting: { color: 'bg-gray-400',  label: 'Reconnecting' },
  error:        { color: 'bg-red-500',   label: 'Disconnected' },
}

function SSEIndicator({ status }: { status: SSEStatus }) {
  const { color, label } = sseStatusConfig[status]
  return (
    <div className="flex items-center space-x-1.5" title={`Live updates: ${label}`}>
      <span className={`h-2 w-2 rounded-full ${color}`} />
      <span className="text-xs text-gray-500 dark:text-gray-400">{label}</span>
    </div>
  )
}
