import axios from 'axios'
import { getStorageKey } from '../utils/storage'

// Get backend URL from environment or auto-detect based on current location
const getBackendUrl = () => {
  const { protocol, hostname, port } = window.location
  console.log('Protocol:', protocol)
  console.log('Hostname:', hostname)
  console.log('Port:', port)

  const isStandardPort = (protocol === 'https:' && (port === '' || port === '443')) ||
                         (protocol === 'http:' && (port === '' || port === '80'))

  // Check if we have a base path (Caddy path-based routing)
  const basePath = import.meta.env.BASE_URL
  console.log('Base path from Vite:', basePath)

  if (isStandardPort && basePath && basePath !== '/') {
    // We're using Caddy path-based routing - use the base path
    console.log('Using Caddy path-based routing with base path')
    return basePath.replace(/\/$/, '')
  }

  // If explicitly set in environment, use that (for direct backend access)
  if (import.meta.env.VITE_BACKEND_URL !== undefined && import.meta.env.VITE_BACKEND_URL !== '') {
    console.log('Using explicit VITE_BACKEND_URL')
    return import.meta.env.VITE_BACKEND_URL
  }

  if (isStandardPort) {
    // We're being accessed through nginx proxy or standard proxy
    console.log('Using standard proxy - relative URLs')
    return ''
  }

  // Development mode - direct access to dev server
  if (port === '5173') {
    console.log('Development mode - using localhost:8000')
    return 'http://localhost:8000'
  }

  // Fallback
  console.log('Fallback - using hostname:8000')
  return `${protocol}//${hostname}:8000`
}

const BACKEND_URL = getBackendUrl()
console.log('VITE_BACKEND_URL:', import.meta.env.VITE_BACKEND_URL)

console.log('🌐 API: Backend URL configured as:', BACKEND_URL || 'Same origin (relative URLs)')

// Export BACKEND_URL for use in other components
export { BACKEND_URL }

export const api = axios.create({
  baseURL: BACKEND_URL,
  timeout: 60000,  // Increased to 60 seconds for heavy processing scenarios
})

// Add request interceptor to include auth token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem(getStorageKey('token'))
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Add response interceptor to handle auth errors
api.interceptors.response.use(
  (response) => response,
  (error) => {
    // Only clear token and redirect on actual 401 responses, not on timeouts
    if (error.response?.status === 401) {
      // Token expired or invalid, redirect to login
      console.warn('🔐 API: 401 Unauthorized - clearing token and redirecting to login')
      localStorage.removeItem(getStorageKey('token'))
      window.location.href = '/login'
    } else if (error.code === 'ECONNABORTED') {
      // Request timeout - don't logout, just log it
      console.warn('⏱️ API: Request timeout - server may be busy')
    } else if (!error.response) {
      // Network error - don't logout
      console.warn('🌐 API: Network error - server may be unreachable')
    }
    return Promise.reject(error)
  }
)

// API endpoints
export const authApi = {
  login: async (email: string, password: string) => {
    const formData = new FormData()
    formData.append('username', email)
    formData.append('password', password)
    // Login with JWT for API calls
    const jwtResponse = await api.post('/auth/jwt/login', formData)
    // Also try to set cookie for audio file access (may fail cross-origin, that's ok)
    try {
      await api.post('/auth/cookie/login', formData)
    } catch {
      // Cookie auth may fail cross-origin, audio playback will use token fallback
    }
    return jwtResponse
  },
  getMe: () => api.get('/users/me'),
}

export const conversationsApi = {
  getAll: () => api.get('/api/conversations'),
  getById: (id: string) => api.get(`/api/conversations/${id}`),
  delete: (id: string) => api.delete(`/api/conversations/${id}`),

  // Reprocessing endpoints
  reprocessTranscript: (conversationId: string) => api.post(`/api/conversations/${conversationId}/reprocess-transcript`),
  reprocessMemory: (conversationId: string, transcriptVersionId: string = 'active') => api.post(`/api/conversations/${conversationId}/reprocess-memory`, null, {
    params: { transcript_version_id: transcriptVersionId }
  }),

  // Version management
  activateTranscriptVersion: (conversationId: string, versionId: string) => api.post(`/api/conversations/${conversationId}/activate-transcript/${versionId}`),
  activateMemoryVersion: (conversationId: string, versionId: string) => api.post(`/api/conversations/${conversationId}/activate-memory/${versionId}`),
  getVersionHistory: (conversationId: string) => api.get(`/api/conversations/${conversationId}/versions`),
}

export const memoriesApi = {
  getAll: (userId?: string) => api.get('/api/memories', { params: userId ? { user_id: userId } : {} }),
  getById: (id: string, userId?: string) => api.get(`/api/memories/${id}`, { params: userId ? { user_id: userId } : {} }),
  getUnfiltered: (userId?: string) => api.get('/api/memories/unfiltered', { params: userId ? { user_id: userId } : {} }),
  search: (query: string, userId?: string, limit: number = 20, scoreThreshold?: number) =>
    api.get('/api/memories/search', {
      params: {
        query,
        ...(userId && { user_id: userId }),
        limit,
        ...(scoreThreshold !== undefined && { score_threshold: scoreThreshold / 100 }) // Convert percentage to decimal
      }
    }),
  delete: (id: string) => api.delete(`/api/memories/${id}`),
  deleteAll: () => api.delete('/api/admin/memory/delete-all'),
}

export const usersApi = {
  getAll: () => api.get('/api/users'),
  create: (userData: any) => api.post('/api/users', userData),
  update: (id: string, userData: any) => api.put(`/api/users/${id}`, userData),
  delete: (id: string) => api.delete(`/api/users/${id}`),
}

export const systemApi = {
  getHealth: () => api.get('/health'),
  getReadiness: () => api.get('/readiness'),
  getMetrics: () => api.get('/api/metrics'),
  getProcessorStatus: () => api.get('/api/processor/status'),
  getProcessorTasks: () => api.get('/api/processor/tasks'),
  getActiveClients: () => api.get('/api/clients/active'),
  getDiarizationSettings: () => api.get('/api/diarization-settings'),
  saveDiarizationSettings: (settings: any) => api.post('/api/diarization-settings', settings),
  
  // Memory Configuration Management
  getMemoryConfigRaw: () => api.get('/api/admin/memory/config/raw'),
  updateMemoryConfigRaw: (configYaml: string) =>
    api.post('/api/admin/memory/config/raw', configYaml, {
      headers: { 'Content-Type': 'text/plain' }
    }),
  validateMemoryConfig: (configYaml: string) =>
    api.post('/api/admin/memory/config/validate/raw', configYaml, {
      headers: { 'Content-Type': 'text/plain' }
    }),
  reloadMemoryConfig: () => api.post('/api/admin/memory/config/reload'),

  // Memory Provider Management
  getMemoryProvider: () => api.get('/api/admin/memory/provider'),
  setMemoryProvider: (provider: string) => api.post('/api/admin/memory/provider', { provider }),
}

export const queueApi = {
  // Consolidated dashboard endpoint - replaces individual getJobs, getStats, getStreamingStatus calls
  getDashboard: (expandedSessions: string[] = []) => api.get('/api/queue/dashboard', {
    params: { expanded_sessions: expandedSessions.join(',') }
  }),

  // Individual endpoints (kept for debugging and specific use cases)
  getJob: (jobId: string) => api.get(`/api/queue/jobs/${jobId}`),
  retryJob: (jobId: string, force: boolean = false) =>
    api.post(`/api/queue/jobs/${jobId}/retry`, { force }),
  cancelJob: (jobId: string) => api.delete(`/api/queue/jobs/${jobId}`),

  // Cleanup operations
  cleanupStuckWorkers: () => api.post('/api/streaming/cleanup'),
  cleanupOldSessions: (maxAgeSeconds: number = 3600) => api.post(`/api/streaming/cleanup-sessions?max_age_seconds=${maxAgeSeconds}`),

  // Job flush operations
  flushJobs: (flushAll: boolean, body: any) => {
    const endpoint = flushAll ? '/api/queue/flush-all' : '/api/queue/flush'
    return api.post(endpoint, body)
  },

  // Legacy endpoints - kept for backward compatibility but not used in Queue page
  // getJobs: (params: URLSearchParams) => api.get(`/api/queue/jobs?${params}`),
  // getJobsBySession: (sessionId: string) => api.get(`/api/queue/jobs/by-session/${sessionId}`),
  // getStats: () => api.get('/api/queue/stats'),
  // getStreamingStatus: () => api.get('/api/streaming/status'),
}

export const uploadApi = {
  uploadAudioFiles: (files: FormData, onProgress?: (progress: number) => void) =>
    api.post('/api/audio/upload', files, {
      headers: { 'Content-Type': 'multipart/form-data' },
      timeout: 300000, // 5 minutes
      onUploadProgress: (progressEvent) => {
        if (onProgress && progressEvent.total) {
          const progress = Math.round((progressEvent.loaded * 100) / progressEvent.total)
          onProgress(progress)
        }
      },
    }),

  uploadFromGDriveFolder: (payload: { gdrive_folder_id: string; device_name?: string; auto_generate_client?: boolean }) =>
    api.post('/api/audio/upload_audio_from_gdrive', null, {
      params: {
        gdrive_folder_id: payload.gdrive_folder_id,
        device_name: payload.device_name,
        auto_generate_client: payload.auto_generate_client,
      },
      timeout: 300000,
    }),
}

export const obsidianApi = {
  uploadZip: (file: File, onProgress?: (progress: number) => void) => {
    const form = new FormData()
    form.append('file', file)
    return api.post('/api/obsidian/upload_zip', form, {
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (e) => {
        if (onProgress && e.total) {
          onProgress(Math.round((e.loaded * 100) / e.total))
        }
      },
      timeout: 300000,
    })
  },
  start: (jobId: string) => api.post('/api/obsidian/start', { job_id: jobId }),
  status: (jobId: string) => api.get('/api/obsidian/status', { params: { job_id: jobId } }),
  cancel: (jobId: string) => api.post('/api/obsidian/cancel', { job_id: jobId }),
}


export const chatApi = {
  // Session management
  createSession: (title?: string) => api.post('/api/chat/sessions', { title }),
  getSessions: (limit = 50) => api.get('/api/chat/sessions', { params: { limit } }),
  getSession: (sessionId: string) => api.get(`/api/chat/sessions/${sessionId}`),
  updateSession: (sessionId: string, title: string) => api.put(`/api/chat/sessions/${sessionId}`, { title }),
  deleteSession: (sessionId: string) => api.delete(`/api/chat/sessions/${sessionId}`),
  
  // Messages
  getMessages: (sessionId: string, limit = 100) => api.get(`/api/chat/sessions/${sessionId}/messages`, { params: { limit } }),
  
  // Memory extraction
  extractMemories: (sessionId: string) => api.post(`/api/chat/sessions/${sessionId}/extract-memories`),
  
  // Statistics
  getStatistics: () => api.get('/api/chat/statistics'),
  
  // Health check
  getHealth: () => api.get('/api/chat/health'),
  
  // Streaming chat (returns EventSource for Server-Sent Events)
  sendMessage: (message: string, sessionId?: string, includeObsidianMemory?: boolean) => {
    const requestBody: any = { message }
    if (sessionId) {
      requestBody.session_id = sessionId
    }
    if (includeObsidianMemory) {
      requestBody.include_obsidian_memory = includeObsidianMemory
    }
    
    return fetch(`${BACKEND_URL}/api/chat/send`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${localStorage.getItem(getStorageKey('token'))}`
      },
      body: JSON.stringify(requestBody)
    })
  }
}

export const speakerApi = {
  // Get current user's speaker configuration
  getSpeakerConfiguration: () => api.get('/api/speaker-configuration'),
  
  // Update current user's speaker configuration
  updateSpeakerConfiguration: (primarySpeakers: Array<{speaker_id: string, name: string, user_id: number}>) => 
    api.post('/api/speaker-configuration', primarySpeakers),
    
  // Get enrolled speakers from speaker recognition service  
  getEnrolledSpeakers: () => api.get('/api/enrolled-speakers'),
  
  // Check speaker service status (admin only)
  getSpeakerServiceStatus: () => api.get('/api/speaker-service-status'),
}
