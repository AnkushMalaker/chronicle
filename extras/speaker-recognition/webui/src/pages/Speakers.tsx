import { useState, useEffect, useCallback, useRef } from 'react'
import { Search, Download, Trash2, Eye, BarChart3, User, Clock, CheckCircle, XCircle, Upload, FileJson } from 'lucide-react'
import { useUser } from '../contexts/UserContext'
import { apiService } from '../services/api'
import { formatDuration } from '../utils/audioUtils'
import EmbeddingPlot from '../components/EmbeddingPlot'

interface Speaker {
  id: string
  name: string
  user_id?: string
  created_at?: string
  updated_at?: string
  audio_sample_count?: number
  total_audio_duration?: number
  average_quality?: number
  enrollment_status: 'pending' | 'completed' | 'failed'
  last_enrollment?: string
}

interface SpeakerStats {
  total_speakers: number
  total_audio_samples: number
  total_duration: number
  average_quality: number
  speakers_by_status: {
    pending: number
    completed: number
    failed: number
  }
}

export default function Speakers() {
  const { user } = useUser()
  const [speakers, setSpeakers] = useState<Speaker[]>([])
  const [stats, setStats] = useState<SpeakerStats | null>(null)
  const [isLoading, setIsLoading] = useState(true)
  const [searchTerm, setSearchTerm] = useState('')
  const [statusFilter, setStatusFilter] = useState<'all' | 'pending' | 'completed' | 'failed'>('all')
  const [sortBy, setSortBy] = useState<'name' | 'created_at' | 'audio_sample_count' | 'total_audio_duration' | 'average_quality'>('name')
  const [sortOrder, setSortOrder] = useState<'asc' | 'desc'>('asc')
  const [selectedSpeaker, setSelectedSpeaker] = useState<Speaker | null>(null)
  const [showDeleteConfirm, setShowDeleteConfirm] = useState<string | null>(null)
  const [activeTab, setActiveTab] = useState<'speakers' | 'analysis'>('speakers')
  const [showImportDialog, setShowImportDialog] = useState(false)
  const [importFile, setImportFile] = useState<File | null>(null)
  const [importMergeStrategy, setImportMergeStrategy] = useState<'skip' | 'replace'>('skip')
  const [importLoading, setImportLoading] = useState(false)
  const fileInputRef = useRef<HTMLInputElement>(null)

  const loadSpeakers = useCallback(async () => {
    if (!user) return

    setIsLoading(true)
    try {
      const speakersResponse = await apiService.get('/speakers')

      // Backend returns { speakers: [...] }, extract the speakers array
      const allSpeakers = speakersResponse.data.speakers || []

      // Filter speakers by user - treat users as "folders" by using speaker ID prefixes
      const userPrefix = `user_${user.id}_`
      const userSpeakers = allSpeakers.filter((speaker: any) => {
        // Filter speakers that belong to this user based on ID prefix
        return speaker.id?.startsWith(userPrefix)
      }).map((speaker: any) => ({
        // Map backend speaker data to frontend format
        id: speaker.id,
        name: speaker.name,
        user_id: user.id.toString(),
        created_at: speaker.created_at || null,
        updated_at: speaker.updated_at || null,
        audio_sample_count: speaker.audio_sample_count || 0,
        total_audio_duration: speaker.total_audio_duration || 0,
        average_quality: null, // Not provided by backend yet
        enrollment_status: 'completed' as const, // If speaker exists, it's completed
        last_enrollment: null // Not provided by backend yet
      }))

      // Calculate stats from filtered speakers
      const stats = {
        total_speakers: userSpeakers.length,
        total_audio_samples: userSpeakers.reduce((sum, s) => sum + (s.audio_sample_count || 0), 0),
        total_duration: userSpeakers.reduce((sum, s) => sum + (s.total_audio_duration || 0), 0),
        average_quality: 0, // Not available from backend yet
        speakers_by_status: {
          pending: 0,
          completed: userSpeakers.length,
          failed: 0
        }
      }

      setSpeakers(userSpeakers)
      setStats(stats)
    } catch (error) {
      console.error('Failed to load speakers:', error)
      // Set empty state when API fails
      setSpeakers([])
      setStats({
        total_speakers: 0,
        total_audio_samples: 0,
        total_duration: 0,
        average_quality: 0,
        speakers_by_status: {
          pending: 0,
          completed: 0,
          failed: 0
        }
      })
    } finally {
      setIsLoading(false)
    }
  }, [user])

  useEffect(() => {
    loadSpeakers()
  }, [loadSpeakers])

  const filteredAndSortedSpeakers = useCallback(() => {
    let filtered = speakers.filter(speaker => {
      const matchesSearch = speaker.name.toLowerCase().includes(searchTerm.toLowerCase())
      const matchesStatus = statusFilter === 'all' || speaker.enrollment_status === statusFilter
      return matchesSearch && matchesStatus
    })

    filtered.sort((a, b) => {
      let aValue = a[sortBy]
      let bValue = b[sortBy]

      if (typeof aValue === 'string') {
        aValue = aValue.toLowerCase()
        bValue = (bValue as string).toLowerCase()
      }

      if (sortOrder === 'asc') {
        return aValue < bValue ? -1 : aValue > bValue ? 1 : 0
      } else {
        return aValue > bValue ? -1 : aValue < bValue ? 1 : 0
      }
    })

    return filtered
  }, [speakers, searchTerm, statusFilter, sortBy, sortOrder])

  const deleteSpeaker = useCallback(async (speakerId: string) => {
    if (!user) return

    // Ensure user can only delete their own speakers
    const userPrefix = `user_${user.id}_`
    if (!speakerId.startsWith(userPrefix)) {
      alert('You can only delete your own speakers.')
      return
    }

    try {
      await apiService.delete(`/speakers/${speakerId}`)
      setSpeakers(prev => prev.filter(s => s.id !== speakerId))
      setShowDeleteConfirm(null)
      loadSpeakers() // Reload to update stats
    } catch (error) {
      console.error('Failed to delete speaker:', error)
      alert('Failed to delete speaker. Please try again.')
    }
  }, [loadSpeakers, user])

  const exportSpeakerData = useCallback(async (speaker: Speaker) => {
    try {
      const response = await apiService.get(`/speakers/${speaker.id}/export`, {
        responseType: 'blob'
      })

      const blob = new Blob([response.data], { type: 'application/zip' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `speaker_${speaker.name.replace(/\s+/g, '_')}_data.zip`
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (error) {
      console.error('Failed to export speaker data:', error)
      alert('Failed to export speaker data. Please try again.')
    }
  }, [])

  const exportAllSpeakers = useCallback(async () => {
    if (!user) return

    try {
      const response = await apiService.get('/speakers/export', {
        params: { user_id: user.id }
      })

      const timestamp = new Date().toISOString().split('T')[0]
      const filename = `speakers_backup_${timestamp}.json`

      const blob = new Blob([JSON.stringify(response.data, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      document.body.removeChild(a)
      URL.revokeObjectURL(url)
    } catch (error) {
      console.error('Failed to export speakers:', error)
      alert('Failed to export speakers. Please try again.')
    }
  }, [user])

  const handleImportFile = useCallback((event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0]
    if (file && file.type === 'application/json') {
      setImportFile(file)
      setShowImportDialog(true)
    } else {
      alert('Please select a valid JSON file')
    }
    // Reset input so same file can be selected again
    if (fileInputRef.current) {
      fileInputRef.current.value = ''
    }
  }, [])

  const performImport = useCallback(async () => {
    if (!importFile || !user) return

    setImportLoading(true)
    try {
      const formData = new FormData()
      formData.append('file', importFile)
      formData.append('merge_strategy', importMergeStrategy)

      const response = await apiService.post('/speakers/import', formData, {
        headers: { 'Content-Type': 'multipart/form-data' }
      })

      const result = response.data
      alert(`Import successful!\n\nImported: ${result.imported}\nSkipped: ${result.skipped}\nReplaced: ${result.replaced}\n${result.errors.length > 0 ? '\nErrors:\n' + result.errors.join('\n') : ''}`)

      // Reload speakers list
      await loadSpeakers()
      setShowImportDialog(false)
      setImportFile(null)
    } catch (error: any) {
      console.error('Import failed:', error)
      alert(`Import failed: ${error.response?.data?.detail || error.message}`)
    } finally {
      setImportLoading(false)
    }
  }, [importFile, importMergeStrategy, user, loadSpeakers])

  const getQualityColor = useCallback((quality: number) => {
    if (quality >= 25) return 'text-green-600 bg-green-100 dark:bg-green-900 dark:text-green-200'
    if (quality >= 20) return 'text-blue-600 bg-blue-100 dark:bg-blue-900 dark:text-blue-200'
    if (quality >= 15) return 'text-yellow-600 bg-yellow-100 dark:bg-yellow-900 dark:text-yellow-200'
    return 'text-red-600 bg-red-100 dark:bg-red-900 dark:text-red-200'
  }, [])

  const getQualityLabel = useCallback((quality: number) => {
    if (quality >= 30) return 'Excellent'
    if (quality >= 20) return 'Good'
    if (quality >= 15) return 'Fair'
    return 'Poor'
  }, [])

  const getStatusColor = useCallback((status: string) => {
    switch (status) {
      case 'completed': return 'text-green-600 bg-green-100 dark:bg-green-900 dark:text-green-200'
      case 'pending': return 'text-yellow-600 bg-yellow-100 dark:bg-yellow-900 dark:text-yellow-200'
      case 'failed': return 'text-red-600 bg-red-100 dark:bg-red-900 dark:text-red-200'
      default: return 'text-gray-600 bg-gray-100 dark:bg-gray-700 dark:text-gray-200'
    }
  }, [])

  const getStatusIcon = useCallback((status: string) => {
    switch (status) {
      case 'completed': return <CheckCircle className="h-4 w-4" />
      case 'failed': return <XCircle className="h-4 w-4" />
      default: return <Clock className="h-4 w-4" />
    }
  }, [])

  if (!user) {
    return (
      <div className="text-center py-12">
        <p className="text-muted">Please select a user to continue.</p>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="text-center py-12">
        <div className="inline-block animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 dark:border-blue-400"></div>
        <p className="mt-2 text-gray-600 dark:text-gray-300">Loading speakers...</p>
      </div>
    )
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="heading-lg">👥 Speaker Management</h1>
        <div className="flex space-x-2">
          <button
            onClick={exportAllSpeakers}
            className="flex items-center space-x-2 px-4 py-2 bg-green-600 text-white rounded-md hover:bg-green-700 dark:bg-green-700 dark:hover:bg-green-800"
            title="Export all speakers to JSON"
          >
            <Download className="h-4 w-4" />
            <span>Export All</span>
          </button>
          <label
            htmlFor="import-file"
            className="flex items-center space-x-2 px-4 py-2 bg-purple-600 text-white rounded-md hover:bg-purple-700 dark:bg-purple-700 dark:hover:bg-purple-800 cursor-pointer"
            title="Import speakers from JSON"
          >
            <Upload className="h-4 w-4" />
            <span>Import</span>
          </label>
          <input
            ref={fileInputRef}
            id="import-file"
            type="file"
            accept=".json"
            onChange={handleImportFile}
            className="hidden"
          />
          <button
            onClick={loadSpeakers}
            className="px-4 py-2 bg-blue-600 text-white rounded-md hover:bg-blue-700 dark:bg-blue-700 dark:hover:bg-blue-800"
          >
            Refresh
          </button>
        </div>
      </div>

      <p className="text-gray-600 dark:text-gray-300">
        Manage enrolled speakers and view their quality metrics.
      </p>

      {/* Tab Navigation */}
      <div className="border-b border-gray-200 dark:border-gray-700">
        <nav className="-mb-px flex space-x-8">
          <button
            onClick={() => setActiveTab('speakers')}
            className={`py-2 px-1 border-b-2 font-medium text-sm ${
              activeTab === 'speakers'
                ? 'border-blue-500 text-blue-600 dark:border-blue-400 dark:text-blue-400'
                : 'border-transparent text-muted hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
            }`}
          >
            👥 Speakers List
          </button>
          <button
            onClick={() => setActiveTab('analysis')}
            className={`py-2 px-1 border-b-2 font-medium text-sm ${
              activeTab === 'analysis'
                ? 'border-blue-500 text-blue-600 dark:border-blue-400 dark:text-blue-400'
                : 'border-transparent text-muted hover:text-gray-700 dark:hover:text-gray-200 hover:border-gray-300 dark:hover:border-gray-600'
            }`}
          >
            📊 Embedding Analysis
          </button>
        </nav>
      </div>

      {/* Tab Content */}
      {activeTab === 'speakers' && (
        <>
          {/* Statistics Cards */}
          {stats && (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
          <div className="p-4 card">
            <div className="text-sm font-medium text-muted">Total Speakers</div>
            <div className="heading-lg">{stats.total_speakers}</div>
          </div>
          <div className="p-4 card">
            <div className="text-sm font-medium text-muted">Audio Samples</div>
            <div className="heading-lg">{stats.total_audio_samples}</div>
          </div>
          <div className="p-4 card">
            <div className="text-sm font-medium text-muted">Total Duration</div>
            <div className="heading-lg">{formatDuration(stats.total_duration)}</div>
          </div>
          <div className="p-4 card">
            <div className="text-sm font-medium text-muted">Avg Quality</div>
            <div className="heading-lg">{stats.average_quality.toFixed(1)} dB</div>
          </div>
        </div>
      )}

      {/* Filters and Search */}
      <div className="bg-white border rounded-lg p-4 dark:bg-gray-800 dark:border-gray-700">
        <div className="grid md:grid-cols-4 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Search</label>
            <div className="relative">
              <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400 dark:text-gray-500" />
              <input
                type="text"
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                placeholder="Search speakers..."
                className="pl-10 w-full px-3 py-2 border border-gray-300 rounded-md dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200"
              />
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Status</label>
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as any)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200"
            >
              <option value="all">All Statuses</option>
              <option value="completed">Completed</option>
              <option value="pending">Pending</option>
              <option value="failed">Failed</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Sort By</label>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value as any)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200"
            >
              <option value="name">Name</option>
              <option value="created_at">Created Date</option>
              <option value="audio_sample_count">Audio Count</option>
              <option value="total_audio_duration">Duration</option>
              <option value="average_quality">Quality</option>
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Order</label>
            <select
              value={sortOrder}
              onChange={(e) => setSortOrder(e.target.value as any)}
              className="w-full px-3 py-2 border border-gray-300 rounded-md dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200"
            >
              <option value="asc">Ascending</option>
              <option value="desc">Descending</option>
            </select>
          </div>
        </div>
      </div>

      {/* Speakers List */}
      {filteredAndSortedSpeakers().length > 0 ? (
        <div className="bg-white border rounded-lg overflow-hidden dark:bg-gray-800 dark:border-gray-700">
          <div className="overflow-x-auto">
            <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
              <thead className="bg-gray-50 dark:bg-gray-700">
                <tr>
                  <th className="px-6 py-3 text-left text-xs font-medium text-muted uppercase tracking-wider">
                    Speaker
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-muted uppercase tracking-wider">
                    Status
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-muted uppercase tracking-wider">
                    Audio Count
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-muted uppercase tracking-wider">
                    Duration
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-muted uppercase tracking-wider">
                    Quality
                  </th>
                  <th className="px-6 py-3 text-left text-xs font-medium text-muted uppercase tracking-wider">
                    Created
                  </th>
                  <th className="px-6 py-3 text-right text-xs font-medium text-muted uppercase tracking-wider">
                    Actions
                  </th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-200 dark:divide-gray-700">
                {filteredAndSortedSpeakers().map((speaker) => (
                  <tr key={speaker.id} className="hover:bg-gray-50 dark:hover:bg-gray-700">
                    <td className="px-6 py-4 whitespace-nowrap">
                      <div className="flex items-center">
                        <div className="flex-shrink-0 h-10 w-10">
                          <div className="h-10 w-10 rounded-full bg-blue-100 dark:bg-blue-900 flex items-center justify-center">
                            <User className="h-5 w-5 text-blue-600 dark:text-blue-400" />
                          </div>
                        </div>
                        <div className="ml-4">
                          <div className="text-sm font-medium text-primary">{speaker.name}</div>
                          <div className="text-sm text-muted">ID: {speaker.id}</div>
                        </div>
                      </div>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      <span className={`inline-flex items-center space-x-1 px-2 py-1 rounded-full text-xs font-medium ${getStatusColor(speaker.enrollment_status)}`}>
                        {getStatusIcon(speaker.enrollment_status)}
                        <span>{speaker.enrollment_status}</span>
                      </span>
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-primary">
                      {speaker.audio_sample_count || 0} samples
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-primary">
                      {speaker.total_audio_duration ? formatDuration(speaker.total_audio_duration) : 'N/A'}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap">
                      {speaker.average_quality ? (
                        <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${getQualityColor(speaker.average_quality)}`}>
                          {speaker.average_quality.toFixed(1)} dB ({getQualityLabel(speaker.average_quality)})
                        </span>
                      ) : (
                        <span className="text-xs text-muted">N/A</span>
                      )}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-sm text-muted">
                      {speaker.created_at ? new Date(speaker.created_at).toLocaleDateString() : 'N/A'}
                    </td>
                    <td className="px-6 py-4 whitespace-nowrap text-right text-sm font-medium">
                      <div className="flex justify-end space-x-2">
                        <button
                          onClick={() => setSelectedSpeaker(speaker)}
                          className="p-1 text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
                          title="View Details"
                        >
                          <Eye className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() => exportSpeakerData(speaker)}
                          className="p-1 text-green-600 hover:text-green-800 dark:text-green-400 dark:hover:text-green-300"
                          title="Export Data"
                        >
                          <Download className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() => setShowDeleteConfirm(speaker.id)}
                          className="p-1 text-red-600 hover:text-red-800 dark:text-red-400 dark:hover:text-red-300"
                          title="Delete Speaker"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : (
        <div className="text-center py-12">
          <User className="h-16 w-16 text-gray-300 dark:text-gray-500 mx-auto mb-4" />
          <h3 className="heading-sm mb-2">No Speakers Found</h3>
          <p className="text-muted">
            {speakers.length === 0
              ? "No speakers have been enrolled yet."
              : "No speakers match your current filters."}
          </p>
        </div>
      )}

      {/* Speaker Details Modal */}
      {selectedSpeaker && (
        <div className="fixed inset-0 bg-black bg-opacity-50 dark:bg-opacity-70 flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-lg max-w-2xl w-full max-h-[80vh] overflow-y-auto dark:bg-gray-800">
            <div className="p-6 border-b dark:border-gray-700">
              <div className="flex items-center justify-between">
                <h2 className="text-xl font-semibold text-primary">Speaker Details</h2>
                <button
                  onClick={() => setSelectedSpeaker(null)}
                  className="text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300"
                >
                  ×
                </button>
              </div>
            </div>
            <div className="p-6 space-y-6">
              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="block text-sm font-medium text-muted">Name</label>
                  <p className="text-primary">{selectedSpeaker.name}</p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-muted">Status</label>
                  <span className={`inline-flex items-center space-x-1 px-2 py-1 rounded-full text-xs font-medium ${getStatusColor(selectedSpeaker.enrollment_status)}`}>
                    {getStatusIcon(selectedSpeaker.enrollment_status)}
                    <span>{selectedSpeaker.enrollment_status}</span>
                  </span>
                </div>
                <div>
                  <label className="block text-sm font-medium text-muted">Audio Samples</label>
                  <p className="text-primary">{selectedSpeaker.audio_sample_count || 0}</p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-muted">Total Duration</label>
                  <p className="text-primary">{formatDuration(selectedSpeaker.total_audio_duration || 0)}</p>
                </div>
                    <div>
                      <label className="block text-sm font-medium text-muted">Average Quality</label>
                      {selectedSpeaker.average_quality ? <span className={`inline-flex items-center px-2 py-1 rounded-full text-xs font-medium ${getQualityColor(selectedSpeaker.average_quality)}`}>
                        {selectedSpeaker.average_quality.toFixed(1)} dB ({getQualityLabel(selectedSpeaker.average_quality)})
                      </span> : <span className="text-sm text-muted">N/A</span>}
                    </div>
                <div>
                  <label className="block text-sm font-medium text-muted">Created</label>
                  <p className="text-primary">{new Date(selectedSpeaker.created_at).toLocaleString()}</p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-muted">Last Updated</label>
                  <p className="text-primary">{new Date(selectedSpeaker.updated_at).toLocaleString()}</p>
                </div>
                <div>
                  <label className="block text-sm font-medium text-muted">Last Enrollment</label>
                  <p className="text-primary">{new Date(selectedSpeaker.last_enrollment).toLocaleString()}</p>
                </div>
              </div>
            </div>
          </div>
        </div>
      )}
        </>
      )}

      {/* Analysis Tab */}
      {activeTab === 'analysis' && (
        <EmbeddingPlot
          dataSource={{ type: 'speakers', userId: user?.id }}
          onRefresh={loadSpeakers}
        />
      )}

      {/* Delete Confirmation Modal */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 bg-black bg-opacity-50 dark:bg-opacity-70 flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-lg max-w-md w-full dark:bg-gray-800">
            <div className="p-6">
              <h3 className="heading-sm mb-4">Delete Speaker</h3>
              <p className="text-gray-600 dark:text-gray-300 mb-6">
                Are you sure you want to delete this speaker? This action cannot be undone and will remove all associated audio data.
              </p>
              <div className="flex justify-end space-x-3">
                <button
                  onClick={() => setShowDeleteConfirm(null)}
                  className="px-4 py-2 text-gray-700 border border-gray-300 rounded-md hover:bg-gray-50 dark:text-gray-300 dark:border-gray-600 dark:hover:bg-gray-700"
                >
                  Cancel
                </button>
                <button
                  onClick={() => deleteSpeaker(showDeleteConfirm)}
                  className="px-4 py-2 bg-red-600 text-white rounded-md hover:bg-red-700 dark:bg-red-700 dark:hover:bg-red-800"
                >
                  Delete
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Import Dialog Modal */}
      {showImportDialog && (
        <div className="fixed inset-0 bg-black bg-opacity-50 dark:bg-opacity-70 flex items-center justify-center p-4 z-50">
          <div className="bg-white rounded-lg max-w-lg w-full dark:bg-gray-800">
            <div className="p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="heading-sm">Import Speakers</h3>
                <button
                  onClick={() => {
                    setShowImportDialog(false)
                    setImportFile(null)
                  }}
                  className="text-gray-400 hover:text-gray-600 dark:text-gray-500 dark:hover:text-gray-300"
                >
                  ×
                </button>
              </div>

              {importFile && (
                <div className="mb-6">
                  <div className="flex items-center space-x-3 p-3 bg-gray-50 rounded-lg dark:bg-gray-700">
                    <FileJson className="h-8 w-8 text-blue-600 dark:text-blue-400" />
                    <div>
                      <p className="font-medium text-primary">{importFile.name}</p>
                      <p className="text-sm text-muted">{(importFile.size / 1024).toFixed(1)} KB</p>
                    </div>
                  </div>
                </div>
              )}

              <div className="mb-6">
                <h4 className="text-sm font-medium text-primary mb-3">Conflict Resolution</h4>
                <p className="text-sm text-secondary mb-3">
                  How should existing speakers be handled?
                </p>
                <div className="space-y-2">
                  <label className="flex items-center">
                    <input
                      type="radio"
                      value="skip"
                      checked={importMergeStrategy === 'skip'}
                      onChange={(e) => setImportMergeStrategy(e.target.value as 'skip' | 'replace')}
                      className="mr-2"
                    />
                    <span className="text-sm">
                      <span className="font-medium">Skip existing speakers</span> - Keep current data unchanged
                    </span>
                  </label>
                  <label className="flex items-center">
                    <input
                      type="radio"
                      value="replace"
                      checked={importMergeStrategy === 'replace'}
                      onChange={(e) => setImportMergeStrategy(e.target.value as 'skip' | 'replace')}
                      className="mr-2"
                    />
                    <span className="text-sm">
                      <span className="font-medium">Replace existing speakers</span> - Overwrite with imported data
                    </span>
                  </label>
                </div>
              </div>

              <div className="flex justify-end space-x-3">
                <button
                  onClick={() => {
                    setShowImportDialog(false)
                    setImportFile(null)
                  }}
                  className="px-4 py-2 text-gray-700 border border-gray-300 rounded-md hover:bg-gray-50 dark:text-gray-300 dark:border-gray-600 dark:hover:bg-gray-700"
                  disabled={importLoading}
                >
                  Cancel
                </button>
                <button
                  onClick={performImport}
                  disabled={!importFile || importLoading}
                  className="px-4 py-2 bg-purple-600 text-white rounded-md hover:bg-purple-700 disabled:opacity-50 disabled:cursor-not-allowed flex items-center space-x-2"
                >
                  {importLoading ? (
                    <>
                      <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white"></div>
                      <span>Importing...</span>
                    </>
                  ) : (
                    <>
                      <Upload className="h-4 w-4" />
                      <span>Import Speakers</span>
                    </>
                  )}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
